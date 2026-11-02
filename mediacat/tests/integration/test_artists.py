"""Integration tests for the Artists tab (``mediacat.web.artists``).

Real app + test database.  Outbound services are stubbed: MusicBrainz and
Discogs over :class:`~.fakes.FakeHttp`, Wikipedia and Spotify by replacing their
client classes / methods, the biography pipeline by replacing
``artists.resolve_artist_bio``.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.enums import ImageRegion, MediaFormat, TokenStatus
from mediacat.db.models import ArtistBio, MediaObject, SiteSetting, Token
from mediacat.ingestion import spotify as spotify_mod
from mediacat.ingestion import wikipedia as wikipedia_mod
from mediacat.ingestion.artist_bio import BioResult
from mediacat.llm.adapter import HybridLlm
from mediacat.web import artists

from .fakes import FakeHttp, StubLlmBackend

MB = "https://musicbrainz.org/ws/2"
DISCOGS = "https://api.discogs.com"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def add_token(
    db: AsyncSession,
    artist: str | None,
    title: str | None,
    year: int | None = None,
    **kw: Any,
) -> Token:
    kw.setdefault("status", TokenStatus.ACTIVE)
    t = Token(media_format=MediaFormat.VINYL, artist=artist, title=title, year=year, **kw)
    db.add(t)
    await db.flush()
    return t


async def add_image(
    db: AsyncSession, token: Token, *, primary: bool = False, region: ImageRegion | None = None
) -> MediaObject:
    h = uuid.uuid4().hex * 2
    m = MediaObject(
        token_id=token.id,
        content_hash=h,
        bucket="media-originals",
        object_key=f"{h[:2]}/{h}.png",
        mime_type="image/png",
        size_bytes=10,
        is_primary_cover=primary,
        region=region,
    )
    db.add(m)
    await db.flush()
    return m


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    db.add(SiteSetting(key=key, value=value))
    await db.flush()


async def bio_row(db: AsyncSession, name: str) -> ArtistBio | None:
    db.expire_all()
    return (
        await db.execute(select(ArtistBio).where(ArtistBio.artist_name == name))
    ).scalar_one_or_none()


def names_in_order(html: str, names: list[str]) -> list[str]:
    """``names`` sorted by their first appearance in a rendered card list."""
    pos = {n: html.index(f'<div class="artist-card-name">{n}</div>') for n in names}
    return sorted(names, key=pos.__getitem__)


@pytest.fixture(autouse=True)
def _no_bio_jobs() -> None:
    artists._bio_jobs.clear()


# ---------------------------------------------------------------------------
# Artist index / search
# ---------------------------------------------------------------------------


@pytest.fixture
async def catalogue(db: AsyncSession) -> dict[str, Any]:
    floyd1 = await add_token(db, "Pink Floyd", "The Dark Side of the Moon", 1973, personal_rating=6)
    floyd2 = await add_token(db, "Pink Floyd", "The Wall", 1979)
    await add_token(
        db, "Pink Floyd", "Deleted Copy", 1960, deleted_at=datetime.now(UTC) - timedelta(days=1)
    )
    beatles = await add_token(
        db, "The Beatles", "Abbey Road", 1969, personal_rating=10, discogs_community_rating=4.1
    )
    await add_token(
        db, "The Beatles", "Let It Be", 1970, discogs_community_rating=4.7, personal_rating=None
    )
    await add_token(db, "Abba", None, None, discogs_community_rating=3.0)
    await add_token(db, "Abba", "Arrival", 1976)
    await add_token(db, "Abba", "Waterloo", 1974)
    await add_token(db, "Ghost Act", "Gone", 1980, deleted_at=datetime.now(UTC))
    await add_token(db, "Merged Act", "Dup", 1981, status=TokenStatus.MERGED)
    await add_token(db, None, "No Artist", 1990)
    await add_token(db, "AC/DC", "Back in Black", 1980)
    await db.commit()
    return {"floyd1": floyd1, "floyd2": floyd2, "beatles": beatles}


async def test_index_lists_live_artists_with_counts_and_year_ranges(
    viewer: httpx.AsyncClient, catalogue: dict[str, Any]
) -> None:
    r = await viewer.get("/artists")
    assert r.status_code == 200
    html = r.text
    # Soft-deleted and merged tokens are excluded (also from the counts).
    assert "Ghost Act" not in html
    assert "Merged Act" not in html
    assert "Deleted Copy" not in html
    assert names_in_order(html, ["The Beatles", "Pink Floyd", "Abba"]) == [
        "Abba",
        "Pink Floyd",
        "The Beatles",
    ]

    def card(name: str) -> str:
        start = html.index(f'<div class="artist-card-name">{name}</div>')
        return html[start : html.index("</div>", html.index("artist-card-meta", start))]

    assert re.search(r"2 pressings\s*&nbsp;·&nbsp;1973–1979", card("Pink Floyd"))
    assert re.search(r"3 pressings\s*&nbsp;·&nbsp;1974–1976", card("Abba"))
    assert re.search(r"1 pressing\s*&nbsp;·&nbsp;1980\s", card("AC/DC"))
    assert "★ 5.0" in card("The Beatles")  # max personal rating 10 / 2
    # Names with "/" are linked via the query-parameter route, percent-encoded.
    assert 'hx-get="/artists/detail?artist=AC%2FDC"' in html


async def test_index_without_artists_shows_empty_state(viewer: httpx.AsyncClient) -> None:
    r = await viewer.get("/artists")
    assert r.status_code == 200
    assert "No artists in the catalogue yet." in r.text


async def test_index_filter_is_case_insensitive_substring(
    viewer: httpx.AsyncClient, catalogue: dict[str, Any]
) -> None:
    html = (await viewer.get("/artists", params={"q": "  FLOYD "})).text
    assert '<div class="artist-card-name">Pink Floyd</div>' in html
    assert "The Beatles" not in html
    assert 'value="  FLOYD "' in html

    html = (await viewer.get("/artists", params={"q": "zzz"})).text
    assert 'No artists found for "zzz".' in html


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("count", ["Abba", "Pink Floyd", "AC/DC"]),  # 3, 2, 1 pressings
        ("rating", ["The Beatles", "Pink Floyd", "Abba"]),  # 10, 6, none
        ("discogs_rating", ["The Beatles", "Abba", "Pink Floyd"]),  # 4.7, 3.0, none
        ("bogus", ["Abba", "Pink Floyd", "The Beatles"]),  # unknown → by name
    ],
)
async def test_index_sort_orders(
    viewer: httpx.AsyncClient, catalogue: dict[str, Any], sort: str, expected: list[str]
) -> None:
    html = (await viewer.get("/artists", params={"sort": sort})).text
    assert names_in_order(html, list(reversed(expected))) == expected
    if sort != "bogus":
        assert re.search(rf'value="{sort}"\s*selected', html)


async def test_index_prefers_cached_portrait_then_primary_cover(
    viewer: httpx.AsyncClient, db: AsyncSession, catalogue: dict[str, Any]
) -> None:
    floyd2 = catalogue["floyd2"]
    any_img = await add_image(db, catalogue["floyd1"])  # not primary, earlier year
    cover = await add_image(db, floyd2, primary=True)
    beatles_img = await add_image(db, catalogue["beatles"])
    db.add(ArtistBio(artist_name="The Beatles", thumbnail_url="https://upload.example/b.jpg"))
    db.add(ArtistBio(artist_name="Abba", thumbnail_url=None))
    await db.commit()

    html = (await viewer.get("/artists")).text
    assert f'src="/catalogue/{floyd2.id}/images/{cover.id}"' in html
    assert f"/images/{any_img.id}" not in html
    assert 'src="https://upload.example/b.jpg" alt="The Beatles"' in html
    assert f"/images/{beatles_img.id}" not in html  # the portrait wins
    # Abba and AC/DC have neither: initial placeholder.
    assert html.count('<div class="artist-card-no-img"><span>A</span></div>') == 2


async def test_search_partial_filters_by_name(
    viewer: httpx.AsyncClient, catalogue: dict[str, Any]
) -> None:
    r = await viewer.get("/artists/search", params={"q": "beat"})
    assert r.status_code == 200
    assert '<div class="artist-card-name">The Beatles</div>' in r.text
    assert "Pink Floyd" not in r.text


async def test_index_three_panel_layout_lists_artists_in_the_left_column(
    viewer: httpx.AsyncClient, catalogue: dict[str, Any]
) -> None:
    viewer.cookies.set("mc_layout", "3panel")
    html = (await viewer.get("/artists")).text
    assert 'id="sp-artist-list"' in html
    assert '<div class="sp-artist-item-name">Abba</div>' in html
    assert 'hx-get="/artists/detail?artist=AC%2FDC"' in html  # first item auto-loads


async def test_artists_page_requires_login(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/artists")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Artist detail / albums partials
# ---------------------------------------------------------------------------


async def test_detail_lists_albums_by_year_with_cover_fallback(
    viewer: httpx.AsyncClient, db: AsyncSession, catalogue: dict[str, Any]
) -> None:
    floyd1, floyd2 = catalogue["floyd1"], catalogue["floyd2"]
    other = await add_image(db, floyd1)  # only image of floyd1 → used as fallback
    await add_image(db, floyd2)
    primary = await add_image(db, floyd2, primary=True)
    undated = await add_token(db, "Pink Floyd", "Animals", None)
    await db.commit()

    r = await viewer.get("/artists/Pink%20Floyd/detail")
    assert r.status_code == 200
    html = r.text
    assert '<h2 class="artist-detail-name">Pink Floyd</h2>' in html
    assert 'hx-get="/artists/info?artist=Pink%20Floyd"' in html
    titles = re.findall(r'<div class="artist-album-title">([^<]+)</div>', html)
    assert titles == ["The Dark Side of the Moon", "The Wall", "Animals"]  # undated last
    assert f'src="/catalogue/{floyd1.id}/images/{other.id}"' in html
    assert f'src="/catalogue/{floyd2.id}/images/{primary.id}"' in html
    assert f'href="/catalogue/{undated.id}"' in html
    assert "Deleted Copy" not in html


async def test_detail_by_query_handles_slash_names(
    viewer: httpx.AsyncClient, catalogue: dict[str, Any]
) -> None:
    r = await viewer.get("/artists/detail", params={"artist": "AC/DC"})
    assert r.status_code == 200
    assert '<h2 class="artist-detail-name">AC/DC</h2>' in r.text
    assert "Back in Black" in r.text


async def test_detail_for_unknown_artist_says_none_stored(viewer: httpx.AsyncClient) -> None:
    r = await viewer.get("/artists/detail", params={"artist": "Nobody"})
    assert r.status_code == 200
    assert "You have no albums by Nobody in the database yet." in r.text


async def test_detail_three_panel_opens_brief_in_right_panel(
    viewer: httpx.AsyncClient, catalogue: dict[str, Any]
) -> None:
    viewer.cookies.set("mc_layout", "3panel")
    html = (await viewer.get("/artists/detail", params={"artist": "Pink Floyd"})).text
    assert f'hx-get="/catalogue/{catalogue["floyd1"].id}/brief"' in html


async def test_albums_partial_shows_only_primary_covers(
    viewer: httpx.AsyncClient, db: AsyncSession, catalogue: dict[str, Any]
) -> None:
    floyd1, floyd2 = catalogue["floyd1"], catalogue["floyd2"]
    non_primary = await add_image(db, floyd1)
    primary = await add_image(db, floyd2, primary=True)
    await db.commit()

    for resp in (
        await viewer.get("/artists/Pink%20Floyd/albums"),
        await viewer.get("/artists/albums", params={"artist": "Pink Floyd"}),
    ):
        assert resp.status_code == 200
        html = resp.text
        assert re.findall(r'<div class="artist-album-title">([^<]+)</div>', html) == [
            "The Dark Side of the Moon",
            "The Wall",
        ]
        assert f"/images/{primary.id}" in html
        assert f"/images/{non_primary.id}" not in html
        assert '<div class="artist-album-no-cover">' in html  # floyd1: no primary

    html = (await viewer.get("/artists/albums", params={"artist": "Nobody"})).text
    assert "You have no albums by Nobody in the database." in html


# ---------------------------------------------------------------------------
# Biography (/info)
# ---------------------------------------------------------------------------


@pytest.fixture
def bio_lookup(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub for ``resolve_artist_bio``: set ``ctl["result"]``; calls are recorded."""
    ctl: dict[str, Any] = {
        "calls": [],
        "result": BioResult(
            extract="First paragraph.\n\nSecond paragraph.",
            source="MusicBrainz → Wikipedia",
            found=True,
            description="English rock band",
            url="https://en.wikipedia.org/wiki/Pink_Floyd",
            thumbnail_url="https://upload.wikimedia.org/pf.jpg",
        ),
    }

    async def fake(name: str, *, llm: Any = None, discogs_token: str | None = None) -> BioResult:
        ctl["calls"].append({"name": name, "llm": llm, "discogs_token": discogs_token})
        result: BioResult = ctl["result"]
        return result

    monkeypatch.setattr(artists, "resolve_artist_bio", fake)
    return ctl


async def test_info_cache_miss_runs_lookup_saves_row_and_reuses_it(
    viewer: httpx.AsyncClient, db: AsyncSession, app: Any, bio_lookup: dict[str, Any]
) -> None:
    await set_setting(db, "discogs_token", "dc-secret")
    await db.commit()
    llm = HybridLlm(StubLlmBackend(reply="unused"))
    app.state.llm = llm

    r = await viewer.get("/artists/Pink%20Floyd/info")
    assert r.status_code == 200
    assert '<p class="artist-wiki-desc">English rock band</p>' in r.text
    assert "<p>First paragraph.</p><p>Second paragraph.</p>" in r.text
    assert 'src="https://upload.wikimedia.org/pf.jpg"' in r.text
    assert ">Wikipedia ↗</a>" in r.text
    assert "via MusicBrainz → Wikipedia" in r.text
    assert bio_lookup["calls"] == [{"name": "Pink Floyd", "llm": llm, "discogs_token": "dc-secret"}]

    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert row.extract == "First paragraph.\n\nSecond paragraph."
    assert row.description == "English rock band"
    assert row.wiki_url == "https://en.wikipedia.org/wiki/Pink_Floyd"
    assert row.thumbnail_url == "https://upload.wikimedia.org/pf.jpg"
    assert row.bio_source == "MusicBrainz → Wikipedia"
    assert row.fetched_at is not None
    await asyncio.sleep(0)  # let the job's done-callback run
    assert "Pink Floyd" not in artists._bio_jobs

    # Second load is served from the cached row (edited here to prove it).
    row.extract = "Cached text."
    await db.commit()
    r2 = await viewer.get("/artists/info", params={"artist": "Pink Floyd"})
    assert "<p>Cached text.</p>" in r2.text
    assert len(bio_lookup["calls"]) == 1


async def test_info_refresh_looks_up_again_and_updates_the_row(
    viewer: httpx.AsyncClient, db: AsyncSession, bio_lookup: dict[str, Any]
) -> None:
    db.add(ArtistBio(artist_name="Pink Floyd", extract="Old bio.", bio_source="Discogs"))
    await db.commit()
    bio_lookup["result"] = BioResult(extract="New bio.", source="Discogs", found=True)

    r = await viewer.get("/artists/info", params={"artist": "Pink Floyd", "refresh": "true"})
    assert "<p>New bio.</p>" in r.text
    assert bio_lookup["calls"][0]["discogs_token"] is None  # nothing configured
    assert bio_lookup["calls"][0]["llm"] is None
    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert (row.extract, row.description, row.wiki_url) == ("New bio.", None, None)


async def test_info_not_found_statement_is_cached_and_shown_as_statement(
    viewer: httpx.AsyncClient, db: AsyncSession, bio_lookup: dict[str, Any]
) -> None:
    bio_lookup["result"] = BioResult(
        extract="No biography was found: MusicBrainz has no entry.", source="Not found", found=False
    )
    for _ in range(2):
        html = (await viewer.get("/artists/info", params={"artist": "Obscure"})).text
        assert 'class="artist-wiki artist-wiki--none"' in html
        assert '<p class="artist-wiki-none">No biography was found' in html
        assert "↻ Try again" in html
        await asyncio.sleep(0)
    assert len(bio_lookup["calls"]) == 1
    row = await bio_row(db, "Obscure")
    assert row is not None
    assert row.bio_source == "Not found"


async def test_info_without_portrait_falls_back_to_first_cover(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    catalogue: dict[str, Any],
    bio_lookup: dict[str, Any],
) -> None:
    cover = await add_image(db, catalogue["floyd2"], primary=True)
    await db.commit()
    bio_lookup["result"] = BioResult(
        extract="A bio.", source="Homepage (AI summary)", found=True, url="https://pf.example"
    )
    html = (await viewer.get("/artists/info", params={"artist": "Pink Floyd"})).text
    assert f'src="/catalogue/{catalogue["floyd2"].id}/images/{cover.id}"' in html
    assert "Written by the local AI" in html
    assert ">Source ↗</a>" in html

    # Cached path takes the same fallback.
    await asyncio.sleep(0)
    html = (await viewer.get("/artists/info", params={"artist": "Pink Floyd"})).text
    assert f"/images/{cover.id}" in html
    assert len(bio_lookup["calls"]) == 1


async def test_info_empty_result_renders_no_biography(
    viewer: httpx.AsyncClient, bio_lookup: dict[str, Any]
) -> None:
    bio_lookup["result"] = BioResult(extract="", source="", found=False)
    html = (await viewer.get("/artists/info", params={"artist": "AC/DC"})).text
    assert "No biography found for AC/DC." in html
    assert "artist=AC/DC&amp;refresh=true" in html or "artist=AC%2FDC&amp;refresh=true" in html
    assert bio_lookup["calls"][0]["name"] == "AC/DC"


async def test_info_cached_bio_backfills_missing_wikipedia_image(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    bio_lookup: dict[str, Any],
) -> None:
    db.add(
        ArtistBio(
            artist_name="Pink Floyd",
            extract="Cached bio.",
            wiki_url="https://en.wikipedia.org/wiki/Pink_Floyd#History?x=1",
            bio_source="Wikipedia",
        )
    )
    await db.commit()
    asked: list[str] = []

    async def fake_image(self: Any, title: str) -> str | None:
        asked.append(title)
        return "https://upload.wikimedia.org/new.jpg"

    monkeypatch.setattr(wikipedia_mod.WikipediaConnector, "fetch_artist_image_url", fake_image)
    html = (await viewer.get("/artists/info", params={"artist": "Pink Floyd"})).text
    assert asked == ["Pink Floyd"]
    assert 'src="https://upload.wikimedia.org/new.jpg"' in html
    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert row.thumbnail_url == "https://upload.wikimedia.org/new.jpg"
    assert bio_lookup["calls"] == []


async def test_info_backfill_failure_keeps_cached_bio_and_uses_cover(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: dict[str, Any],
    bio_lookup: dict[str, Any],
) -> None:
    cover = await add_image(db, catalogue["floyd1"])
    db.add(
        ArtistBio(
            artist_name="Pink Floyd",
            extract="Cached bio.",
            wiki_url="https://en.wikipedia.org/wiki/Pink_Floyd",
        )
    )
    await db.commit()

    async def broken(self: Any, title: str) -> str | None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(wikipedia_mod.WikipediaConnector, "fetch_artist_image_url", broken)
    html = (await viewer.get("/artists/info", params={"artist": "Pink Floyd"})).text
    assert "<p>Cached bio.</p>" in html
    assert f"/images/{cover.id}" in html
    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert row.thumbnail_url is None
    assert bio_lookup["calls"] == []


async def test_info_cache_write_failure_still_returns_the_bio(
    viewer: httpx.AsyncClient, db: AsyncSession, bio_lookup: dict[str, Any]
) -> None:
    # A 600-char name does not fit artist_bios.artist_name (500): the save fails.
    name = "x" * 600
    html = (await viewer.get("/artists/info", params={"artist": name})).text
    assert "<p>First paragraph.</p>" in html
    assert len(bio_lookup["calls"]) == 1
    assert await bio_row(db, name) is None


# ---------------------------------------------------------------------------
# Discography
# ---------------------------------------------------------------------------


MB_RELEASE_GROUPS = [
    {"id": "rg-wall", "title": "The Wall", "primary-type": "Album", "first-release-date": "1979"},
    {
        "id": "rg-dsotm",
        "title": "The Dark Side of the Moon",
        "primary-type": "Album",
        "first-release-date": "1973-03-01",
    },
    {
        "id": "rg-comp",
        "title": "Echoes",
        "primary-type": "Compilation",
        "first-release-date": "2001",
    },
    {"id": "rg-dup", "title": "The Wall", "primary-type": "Album", "first-release-date": "1980"},
    {"id": "rg-empty", "title": "  ", "primary-type": "Album", "first-release-date": "1970"},
    {"id": "rg-ep", "title": "Arnold Layne", "primary-type": "Single", "first-release-date": "x"},
    {"id": "", "title": "Untyped", "primary-type": None},
]


def stub_musicbrainz(fake_http: FakeHttp, groups: list[dict[str, Any]] | None = None) -> None:
    fake_http.add("GET", f"{MB}/artist", httpx.Response(200, json={"artists": [{"id": "mbid-pf"}]}))
    fake_http.add(
        "GET",
        f"{MB}/artist/mbid-pf",
        httpx.Response(200, json={"release-groups": groups or MB_RELEASE_GROUPS}),
    )


class FakeSpotifyClient:
    """Stand-in for ``mediacat.ingestion.spotify.SpotifyClient``."""

    instances: ClassVar[list[FakeSpotifyClient]] = []
    artist: ClassVar[dict[str, Any] | None] = {"id": "sp-pf", "name": "Pink Floyd"}
    albums: ClassVar[list[dict[str, Any]]] = []
    error: ClassVar[Exception | None] = None

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.credentials = (client_id, client_secret)
        self.calls: list[tuple[str, str]] = []
        type(self).instances.append(self)

    async def resolve_artist(self, name: str) -> tuple[dict[str, Any] | None, list[Any]]:
        self.calls.append(("resolve_artist", name))
        if self.error:
            raise self.error
        return self.artist, []

    async def fetch_artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        self.calls.append(("fetch_artist_albums", artist_id))
        return self.albums


@pytest.fixture
def fake_spotify(monkeypatch: pytest.MonkeyPatch) -> type[FakeSpotifyClient]:
    cls = type("Spotify", (FakeSpotifyClient,), {"instances": [], "albums": [], "error": None})
    monkeypatch.setattr(spotify_mod, "SpotifyClient", cls)
    return cls


def disco_rows(html: str) -> list[tuple[str, str, bool]]:
    """(year, title, owned) for each rendered discography row."""
    out = []
    for m in re.finditer(r'<tr class="disco-row ([^"]*)">(.*?)</tr>', html, re.S):
        year = re.search(r'<td class="disco-year text-muted">([^<]*)</td>', m.group(2))
        title = re.search(r'<td class="disco-title">(.*?)</td>', m.group(2), re.S)
        assert year
        assert title
        text = re.sub(r"<span.*?</span>|<[^>]+>", "", title.group(1), flags=re.S).strip()
        out.append((year.group(1), text, "disco-row--owned" in m.group(1)))
    return out


async def test_discography_from_musicbrainz_is_annotated_and_cached(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, catalogue: dict[str, Any]
) -> None:
    stub_musicbrainz(fake_http)
    r = await viewer.get("/artists/Pink%20Floyd/discography")
    assert r.status_code == 200
    html = r.text
    # Sorted by date, compilations and duplicate/empty titles dropped.
    assert disco_rows(html) == [
        ("1973", "The Dark Side of the Moon", True),
        ("1979", "The Wall", True),
        ("—", "Untyped", False),  # no date sorts as "9999"
        ("—", "Arnold Layne", False),  # unparsable date "x": sorts last, no year
    ]
    assert "https://musicbrainz.org/release-group/rg-dsotm" in html
    assert "Source: MusicBrainz · Spotify (not configured)" in html
    search = fake_http.sent("GET", f"{MB}/artist?")[0]
    assert search.url.params["query"] == 'artist:"Pink Floyd"'
    assert search.headers["user-agent"].startswith("MediaCat/")

    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert [a["title"] for a in row.disco_data or []] == [
        "The Dark Side of the Moon",
        "The Wall",
        "Untyped",
        "Arnold Layne",
    ]
    assert row.disco_data[2]["url"] is None  # type: ignore[index]  # no release-group id
    assert row.disco_source == "MusicBrainz · Spotify (not configured)"
    assert row.disco_fetched_at is not None

    # Same day: served from the cache, re-annotated against the current DB.
    n_requests = len(fake_http.requests)
    await add_token(db, "Pink Floyd", "Arnold Layne", 1967)
    await db.commit()
    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert len(fake_http.requests) == n_requests
    assert ("—", "Arnold Layne", True) in disco_rows(html)

    # refresh=true fetches again.
    html = (
        await viewer.get("/artists/discography", params={"artist": "Pink Floyd", "refresh": "1"})
    ).text
    assert len(fake_http.requests) == n_requests + 2
    assert len(disco_rows(html)) == 4


@pytest.mark.xfail(
    strict=True,
    reason="BUG: 'in DB' annotation compares titles case-sensitively "
    "(Discogs 'Of The' vs MusicBrainz 'of the'), unlike the casefolded Spotify match",
)
async def test_discography_marks_owned_titles_regardless_of_case(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    # Discogs imports use title case; MusicBrainz uses English title style.
    await add_token(db, "Pink Floyd", "The Dark Side Of The Moon", 1973)
    await db.commit()
    stub_musicbrainz(fake_http)
    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert ("1973", "The Dark Side of the Moon", True) in disco_rows(html)


async def test_discography_cache_from_an_earlier_day_is_refetched(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    db.add(
        ArtistBio(
            artist_name="Pink Floyd",
            disco_data=[{"title": "Stale", "year": 1900}],
            disco_source="MusicBrainz",
            disco_fetched_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    await db.commit()
    stub_musicbrainz(fake_http)
    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert "Stale" not in html
    assert ("1973", "The Dark Side of the Moon", False) in disco_rows(html)
    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert row.disco_fetched_at is not None
    assert row.disco_fetched_at.date() == datetime.now(UTC).date()


async def test_discography_falls_back_to_wikipedia(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: FakeHttp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_http.add("GET", f"{MB}/artist", httpx.Response(503))
    searched: list[str] = []
    parsed: list[str] = []

    async def search(self: Any, name: str) -> list[dict[str, Any]]:
        searched.append(name)
        return [
            {"title": "List of songs by Pink Floyd"},
            {"title": "Pink Floyd discography"},
            {"title": "Pink Floyd"},
        ]

    async def discography(self: Any, title: str) -> list[dict[str, Any]]:
        parsed.append(title)
        return [{"title": "Wish You Were Here", "year": 1975, "source": "wikipedia"}]

    monkeypatch.setattr(wikipedia_mod.WikipediaConnector, "search_artist", search)
    monkeypatch.setattr(wikipedia_mod.WikipediaConnector, "fetch_discography", discography)

    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert searched == ["Pink Floyd"]
    assert parsed == ["Pink Floyd"]  # the page named after the artist, not a list page
    assert disco_rows(html) == [("1975", "Wish You Were Here", False)]
    assert 'href="https://en.wikipedia.org/wiki/Wish_You_Were_Here"' in html
    assert "Source: Wikipedia · Spotify (not configured)" in html
    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert row.disco_source == "Wikipedia · Spotify (not configured)"


async def test_discography_nothing_found_caches_nothing(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: FakeHttp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_http.add("GET", f"{MB}/artist", httpx.Response(200, json={"artists": []}))

    async def search(self: Any, name: str) -> list[dict[str, Any]]:
        return [{"title": "Some Other Band"}]

    monkeypatch.setattr(wikipedia_mod.WikipediaConnector, "search_artist", search)
    html = (await viewer.get("/artists/discography", params={"artist": "Nobody"})).text
    assert "No discography found via Discogs, MusicBrainz, or Wikipedia." in html
    assert await bio_row(db, "Nobody") is None


async def test_discography_survives_both_sources_failing(
    viewer: httpx.AsyncClient, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MusicBrainz: artist without an id; Wikipedia: search raises.
    fake_http.add("GET", f"{MB}/artist", httpx.Response(200, json={"artists": [{"name": "x"}]}))

    async def search(self: Any, name: str) -> list[dict[str, Any]]:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(wikipedia_mod.WikipediaConnector, "search_artist", search)
    r = await viewer.get("/artists/discography", params={"artist": "Nobody"})
    assert r.status_code == 200
    assert "No discography found" in r.text


async def test_wikipedia_discography_without_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def search(self: Any, name: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(wikipedia_mod.WikipediaConnector, "search_artist", search)
    assert await artists._wikipedia_discography("Nobody") == ([], "")


async def test_discography_spotify_enrichment_and_token_backfill(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: FakeHttp,
    fake_spotify: type[FakeSpotifyClient],
    catalogue: dict[str, Any],
) -> None:
    await set_setting(db, "spotify_client_id", " cid ")
    await set_setting(db, "spotify_client_secret", "csecret")
    keep = await add_token(
        db, "Pink Floyd", "The Wall", 1980, spotify_url="https://open.spotify.com/album/mine"
    )
    other_artist = await add_token(db, "Roger Waters", "The Dark Side of the Moon", 2022)
    await db.commit()
    fake_spotify.albums = [
        {"title": "THE DARK SIDE OF THE MOON", "spotify_url": "https://open.spotify.com/album/ds"},
        {"title": "The Wall", "spotify_url": "https://open.spotify.com/album/wall"},
        {"title": "Arnold Layne", "spotify_url": ""},
    ]
    stub_musicbrainz(fake_http)

    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert "Source: MusicBrainz · Spotify (2)" in html
    assert 'href="https://open.spotify.com/album/ds"' in html
    assert 'href="https://open.spotify.com/album/wall"' in html
    (client,) = fake_spotify.instances
    assert client.credentials == ("cid", "csecret")
    assert client.calls == [("resolve_artist", "Pink Floyd"), ("fetch_artist_albums", "sp-pf")]

    db.expire_all()
    spotify = {
        t.id: t.spotify_url
        for t in (await db.execute(select(Token).where(Token.title.isnot(None)))).scalars()
    }
    assert spotify[catalogue["floyd1"].id] == "https://open.spotify.com/album/ds"
    assert spotify[catalogue["floyd2"].id] == "https://open.spotify.com/album/wall"
    assert spotify[keep.id] == "https://open.spotify.com/album/mine"  # never overwritten
    assert spotify[other_artist.id] is None
    row = await bio_row(db, "Pink Floyd")
    assert row is not None
    assert row.disco_data is not None
    assert row.disco_data[0]["spotify_url"] == "https://open.spotify.com/album/ds"


@pytest.mark.parametrize(
    ("artist", "error", "note"),
    [
        (None, None, "Spotify (no match)"),
        ({"id": "sp-pf"}, None, "Spotify (no match)"),  # found, but no title matches
        ({"id": "sp-pf"}, RuntimeError("401 invalid_client"), "Spotify (error)"),
    ],
)
async def test_discography_spotify_without_matches(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: FakeHttp,
    fake_spotify: type[FakeSpotifyClient],
    catalogue: dict[str, Any],
    artist: dict[str, Any] | None,
    error: Exception | None,
    note: str,
) -> None:
    await set_setting(db, "spotify_client_id", "cid")
    await set_setting(db, "spotify_client_secret", "csecret")
    await db.commit()
    fake_spotify.artist = artist
    fake_spotify.error = error
    fake_spotify.albums = [{"title": "Meddle", "spotify_url": "https://open.spotify.com/album/m"}]
    stub_musicbrainz(fake_http)

    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert f"Source: MusicBrainz · {note}" in html
    assert len(disco_rows(html)) == 4  # the discography itself is unaffected
    assert "spotify-link" not in html
    db.expire_all()
    urls = (await db.execute(select(Token.spotify_url))).scalars().all()
    assert set(urls) == {None}


async def test_discography_spotify_needs_both_credentials(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: FakeHttp,
    fake_spotify: type[FakeSpotifyClient],
) -> None:
    await set_setting(db, "spotify_client_id", "cid")
    await set_setting(db, "spotify_client_secret", "   ")
    await db.commit()
    stub_musicbrainz(fake_http)
    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert "Spotify (not configured)" in html
    assert fake_spotify.instances == []


async def test_discography_token_backfill_failure_does_not_break_the_page(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: FakeHttp,
    fake_spotify: type[FakeSpotifyClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await set_setting(db, "spotify_client_id", "cid")
    await set_setting(db, "spotify_client_secret", "csecret")
    await db.commit()
    fake_spotify.albums = [{"title": "The Wall", "spotify_url": "https://open.spotify.com/w"}]
    stub_musicbrainz(fake_http)

    async def broken(*args: Any) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(artists, "_backfill_token_spotify_urls", broken)
    html = (await viewer.get("/artists/discography", params={"artist": "Pink Floyd"})).text
    assert "Spotify (1)" in html
    row = await bio_row(db, "Pink Floyd")
    assert row is not None  # the cache was still written before the backfill


async def test_discography_cache_write_failure_still_renders(
    viewer: httpx.AsyncClient, fake_http: FakeHttp
) -> None:
    stub_musicbrainz(fake_http)
    name = "y" * 600  # longer than artist_bios.artist_name: the cache insert fails
    html = (await viewer.get("/artists/discography", params={"artist": name})).text
    assert len(disco_rows(html)) == 4


# ---------------------------------------------------------------------------
# Discogs helpers (not wired to a route: the discography uses MusicBrainz →
# Wikipedia, the bio pipeline has its own Discogs client)
# ---------------------------------------------------------------------------


async def test_discogs_discography_lists_main_masters(fake_http: FakeHttp) -> None:
    fake_http.add(
        "GET",
        f"{DISCOGS}/database/search",
        httpx.Response(200, json={"results": [{"id": 45467}]}),
    )
    fake_http.add(
        "GET",
        f"{DISCOGS}/artists/45467/releases",
        httpx.Response(
            200,
            json={
                "releases": [
                    {"id": 10362, "title": "The Dark Side Of The Moon", "year": 1973,
                     "role": "Main", "type": "master"},
                    {"id": 1, "title": "Guest Spot", "role": "Appearance"},
                    {"id": 10362, "title": "The Dark Side Of The Moon", "role": "Main"},
                    {"id": None, "title": "No Id", "role": ""},
                    {"id": 5, "title": "", "role": "Main"},
                ]
            },
        ),
    )  # fmt: skip
    albums, source = await artists._discogs_discography("Pink Floyd", token="tok")
    assert source == "Discogs"
    assert albums == [
        {
            "title": "The Dark Side Of The Moon",
            "year": 1973,
            "type": "master",
            "source": "discogs",
            "discogs_master_id": 10362,
            "url": "https://www.discogs.com/master/10362",
        },
        {
            "title": "No Id",
            "year": None,
            "type": "",
            "source": "discogs",
            "discogs_master_id": None,
            "url": None,
        },
    ]
    search = fake_http.requests[0]
    assert search.headers["authorization"] == "Discogs token=tok"
    assert search.url.params["type"] == "artist"
    assert fake_http.requests[1].url.params["type"] == "master"


@pytest.mark.parametrize("results", [[], [{"title": "no id"}]])
async def test_discogs_discography_unknown_artist(fake_http: FakeHttp, results: list[Any]) -> None:
    fake_http.add(
        "GET", f"{DISCOGS}/database/search", httpx.Response(200, json={"results": results})
    )
    assert await artists._discogs_discography("Nobody") == ([], "")
    assert "authorization" not in fake_http.requests[0].headers


async def test_discogs_artist_image_prefers_primary(fake_http: FakeHttp) -> None:
    fake_http.add(
        "GET", f"{DISCOGS}/database/search", httpx.Response(200, json={"results": [{"id": 7}]})
    )
    fake_http.add(
        "GET",
        f"{DISCOGS}/artists/7",
        httpx.Response(
            200,
            json={
                "images": [
                    {"type": "secondary", "uri150": "https://i.discogs.com/s150.jpg"},
                    {"type": "primary", "uri": "https://i.discogs.com/p.jpg", "uri150": ""},
                ]
            },
        ),
    )
    assert await artists._discogs_artist_image("Band", token="t") == "https://i.discogs.com/p.jpg"
    assert fake_http.requests[1].headers["authorization"] == "Discogs token=t"


@pytest.mark.parametrize(
    ("search", "artist"),
    [
        ({"results": []}, None),
        ({"results": [{"id": None}]}, None),
        ({"results": [{"id": 7}]}, {"images": []}),
        ({"results": [{"id": 7}]}, {"images": [{"type": "primary"}]}),
    ],
)
async def test_discogs_artist_image_none(
    fake_http: FakeHttp, search: dict[str, Any], artist: dict[str, Any] | None
) -> None:
    fake_http.add("GET", f"{DISCOGS}/database/search", httpx.Response(200, json=search))
    if artist is not None:
        fake_http.add("GET", f"{DISCOGS}/artists/7", httpx.Response(200, json=artist))
    assert await artists._discogs_artist_image("Band") is None


async def test_discogs_artist_image_swallows_http_errors(fake_http: FakeHttp) -> None:
    fake_http.add("GET", f"{DISCOGS}/database/search", httpx.Response(500))
    assert await artists._discogs_artist_image("Band") is None


def test_discogs_token_from_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    real_path = pathlib.Path
    files: dict[str, str] = {}

    def fake_path(p: str) -> pathlib.Path:
        target = tmp_path / real_path(p).name
        if p in files:
            target.write_text(files[p], encoding="utf-8")
        return target

    with monkeypatch.context() as m:
        m.setattr(pathlib, "Path", fake_path)
        assert artists._discogs_token() is None
        files["/run/secrets/discogs_token.txt"] = "  tok-from-file \n"
        assert artists._discogs_token() == "tok-from-file"
        files["/run/secrets/discogs_token"] = "\n"
        assert artists._discogs_token() is None  # first file wins, even when empty
