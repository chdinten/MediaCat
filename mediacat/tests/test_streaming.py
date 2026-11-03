"""Unit tests for streaming-service links: normalisation, matching, response parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from mediacat.ingestion import streaming as st
from mediacat.ingestion.streaming import (
    AlbumQuery,
    Candidate,
    album_key,
    artist_key,
    barcode_match,
    barcode_variants,
    best_match,
    collect_barcodes,
    match_artist,
    match_title,
    parse_deezer_album,
    parse_deezer_search,
    parse_itunes,
    parse_service_url,
    parse_spotify_albums,
    search_title,
    title_score,
)
from mediacat.web import streaming as web_st

SPOTIFY_ID = "4LH4d3cOWNNsVw41Gqt2kv"


def cand(title: str, *artists: str, year: int | None = None, **kw: Any) -> Candidate:
    return Candidate(
        external_id=kw.pop("external_id", title),
        url=kw.pop("url", f"https://example.test/{title}"),
        title=title,
        artists=artists,
        year=year,
        **kw,
    )


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Björk", "bjork"),
        ("  Nirvana (2) ", "nirvana"),
        ("Prince*", "prince"),
        ("The   Beatles", "the beatles"),
        ("", ""),
    ],
)
def test_artist_key(raw: str, key: str) -> None:
    assert artist_key(raw) == key


def test_album_key_keeps_editions_apart() -> None:
    assert album_key("Abbey Road") == album_key("ABBEY  ROAD")
    assert album_key("Abbey Road") != album_key("Abbey Road (Remastered)")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Abbey Road (Remastered 2019)", "Abbey Road"),
        ("The Wall [Deluxe Edition]", "The Wall"),
        ("Rumours - 35th Anniversary Edition", "Rumours"),
        ("Hounds of Love - Remastered", "Hounds of Love"),
        ("Lazarus - EP", "Lazarus"),
        ("Live (At Leeds)", "Live (At Leeds)"),  # not an edition suffix
        ("(Remastered)", "(Remastered)"),  # nothing left: keep the title
    ],
)
def test_search_title(raw: str, expected: str) -> None:
    assert search_title(raw) == expected


def test_match_title_and_artist() -> None:
    assert match_title("The Dark Side of the Moon (2011 Remaster)") == "dark side of the moon"
    assert match_title("Déjà Vu") == match_title("Deja Vu")
    assert match_artist("Simon & Garfunkel") == match_artist("Simon and Garfunkel")
    assert match_artist("The Beatles") == match_artist("Beatles")
    assert match_artist("Nirvana (2)") == match_artist("Nirvana")


@pytest.mark.parametrize(
    ("raw", "variants"),
    [
        ("724384260927", ["724384260927", "0724384260927"]),  # UPC-12 → also EAN-13
        ("0724384260927", ["0724384260927", "724384260927"]),
        ("5 099902 987729", ["5099902987729"]),
        ("12345678", ["12345678"]),
        ("1234", []),
        ("", []),
    ],
)
def test_barcode_variants(raw: str, variants: list[str]) -> None:
    assert barcode_variants(raw) == variants


def test_collect_barcodes_column_then_discogs_identifiers() -> None:
    extra = {
        "discogs": {
            "identifiers": [
                {"type": "Barcode", "value": "5 099902 987729", "description": "Scanned"},
                {"type": "Matrix / Runout", "value": "XYZ 1"},
                {"type": "barcode", "value": "724384260927"},
                {"type": "Barcode", "value": "724384260927"},  # duplicate
                {"type": "Barcode", "value": "n/a"},
                "junk",
            ]
        }
    }
    assert collect_barcodes("5099902987729", extra) == ["5099902987729", "724384260927"]
    assert collect_barcodes(None, None) == []
    assert collect_barcodes(None, {"discogs": "bad"}) == []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_title_score_levels() -> None:
    assert title_score("Abbey Road", "abbey road") == 1.0
    assert title_score("Abbey Road", "Abbey Road (Remastered)") == 0.97
    assert (
        0.88
        < title_score(
            "Sgt. Pepper's Lonely Hearts Club Band", "Sgt Peppers Lonely Hearts Club Band"
        )
        <= 0.97
    )
    assert title_score("Abbey Road", "Let It Be") < 0.5
    assert title_score("", "Abbey Road") == 0.0


def test_best_match_needs_artist_and_title() -> None:
    q = AlbumQuery("The Beatles", "Abbey Road", 1969)
    assert best_match(q, [cand("Abbey Road", "Abbey Road Orchestra")], "CH") is None
    assert best_match(q, [cand("Let It Be", "The Beatles")], "CH") is None
    m = best_match(q, [cand("Abbey Road (Remastered)", "Beatles")], "CH")
    assert m is not None
    assert m.method == "search"
    assert m.score == 0.97


def test_best_match_prefers_exact_title_then_year() -> None:
    q = AlbumQuery("Pink Floyd", "The Wall", 1979)
    candidates = [
        cand("The Wall (Remastered)", "Pink Floyd", year=2011, external_id="remaster"),
        cand("The Wall", "Pink Floyd", year=2016, external_id="late"),
        cand("The Wall", "Pink Floyd", year=1979, external_id="original"),
    ]
    m = best_match(q, candidates, "CH")
    assert m is not None
    assert m.external_id == "original"


def test_best_match_checks_market() -> None:
    q = AlbumQuery("Pink Floyd", "Animals")
    elsewhere = cand("Animals", "Pink Floyd", markets=("US",))
    assert best_match(q, [elsewhere], "CH") is None
    assert best_match(q, [cand("Animals", "Pink Floyd", markets=("CH", "DE"))], "CH") is not None
    assert best_match(q, [cand("Animals", "Pink Floyd", markets=None)], "CH") is not None


def test_barcode_match_still_needs_the_artist() -> None:
    q = AlbumQuery("Pink Floyd", "Meddle")
    assert barcode_match(q, [cand("Something Else", "Other Band")], "CH") is None
    m = barcode_match(q, [cand("Meddle (2011 Remaster)", "Pink Floyd")], "CH")
    assert m is not None
    assert (m.method, m.score) == ("barcode", 1.0)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_spotify_albums() -> None:
    items = [
        {
            "id": SPOTIFY_ID,
            "name": "Discovery",
            "artists": [{"name": "Daft Punk"}, "junk"],
            "release_date": "2001-03-12",
            "available_markets": ["CH", "DE"],
        },
        {"id": "x2", "name": "No markets", "release_date": "1999"},
        {"name": "no id"},
        "junk",
    ]
    first, second = parse_spotify_albums(items)
    assert first.url == f"https://open.spotify.com/album/{SPOTIFY_ID}"
    assert first.artists == ("Daft Punk",)
    assert first.year == 2001
    assert first.markets == ("CH", "DE")
    assert second.markets is None
    assert second.year == 1999


def test_parse_deezer() -> None:
    album = {
        "id": 302127,
        "title": "Discovery",
        "artist": {"name": "Daft Punk"},
        "contributors": [{"name": "Daft Punk"}, {"name": "Romanthony"}],
        "release_date": "2001-03-07",
    }
    (c,) = parse_deezer_album(album)
    assert c.url == "https://www.deezer.com/album/302127"
    assert c.artists == ("Daft Punk", "Daft Punk", "Romanthony")
    assert c.year == 2001
    assert parse_deezer_album({"error": {"type": "DataException", "code": 800}}) == []
    assert parse_deezer_album(None) == []
    found = parse_deezer_search({"data": [album, {"title": "no id"}, "junk"], "total": 3})
    assert [x.external_id for x in found] == ["302127"]
    assert parse_deezer_search({}) == []


def test_parse_itunes() -> None:
    data = {
        "resultCount": 3,
        "results": [
            {
                "wrapperType": "collection",
                "collectionId": 697194953,
                "collectionName": "Discovery",
                "artistName": "Daft Punk",
                "collectionViewUrl": "https://music.apple.com/ch/album/discovery/697194953?uo=4",
                "releaseDate": "2001-03-12T08:00:00Z",
            },
            {"wrapperType": "track", "collectionId": 1},
            {"wrapperType": "collection", "collectionId": 2},  # no URL
        ],
    }
    (c,) = parse_itunes(data)
    assert c.url == "https://music.apple.com/ch/album/discovery/697194953"
    assert (c.external_id, c.year, c.artists) == ("697194953", 2001, ("Daft Punk",))
    assert parse_itunes({"results": None}) == []


@pytest.mark.parametrize(
    ("service", "url", "expected"),
    [
        (
            "spotify",
            f"https://open.spotify.com/album/{SPOTIFY_ID}?si=abc",
            (f"https://open.spotify.com/album/{SPOTIFY_ID}", SPOTIFY_ID),
        ),
        (
            "spotify",
            f"https://open.spotify.com/intl-de/album/{SPOTIFY_ID}",
            (f"https://open.spotify.com/album/{SPOTIFY_ID}", SPOTIFY_ID),
        ),
        (
            "spotify",
            f"spotify:album:{SPOTIFY_ID}",
            (f"https://open.spotify.com/album/{SPOTIFY_ID}", SPOTIFY_ID),
        ),
        ("spotify", "https://open.spotify.com/track/" + SPOTIFY_ID, None),
        ("spotify", "https://evil.test/album/" + SPOTIFY_ID, None),
        (
            "deezer",
            "https://www.deezer.com/fr/album/302127",
            ("https://www.deezer.com/album/302127", "302127"),
        ),
        (
            "deezer",
            "https://deezer.com/album/302127?utm=x",
            ("https://www.deezer.com/album/302127", "302127"),
        ),
        ("deezer", "https://link.deezer.com/s/abc", None),
        (
            "apple",
            "https://music.apple.com/ch/album/discovery/697194953?l=en",
            ("https://music.apple.com/ch/album/discovery/697194953", "697194953"),
        ),
        (
            "apple",
            "https://itunes.apple.com/us/album/id697194953",
            ("https://music.apple.com/us/album/697194953", "697194953"),
        ),
        ("apple", "javascript:alert(1)", None),
        ("deezer", f"https://open.spotify.com/album/{SPOTIFY_ID}", None),  # wrong service
        ("tidal", "https://tidal.com/album/1", None),
    ],
)
def test_parse_service_url(service: str, url: str, expected: tuple[str, str] | None) -> None:
    assert parse_service_url(service, url) == expected


# ---------------------------------------------------------------------------
# Lookups (HTTP stubbed)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    for t in st.THROTTLES.values():
        monkeypatch.setattr(t, "interval", 0.0)


async def test_find_deezer_barcode_first(
    monkeypatch: pytest.MonkeyPatch, no_throttle: None
) -> None:
    calls: list[str] = []

    async def fake_get(service: str, url: str, params: dict[str, Any] | None = None) -> Any:
        calls.append(url)
        if url.endswith("upc:724384260927"):
            return {"error": {"code": 800}}
        if url.endswith("upc:0724384260927"):
            return {"id": 7, "title": "Animals", "artist": {"name": "Pink Floyd"}}
        raise AssertionError(url)

    monkeypatch.setattr(st, "_get_json", fake_get)
    m = await st.find_deezer(
        AlbumQuery("Pink Floyd", "Animals", barcodes=("724384260927",)), "CH", {}
    )
    assert m is not None
    assert (m.url, m.method) == ("https://www.deezer.com/album/7", "barcode")
    assert len(calls) == 2  # no text search needed


async def test_find_apple_falls_back_to_search(
    monkeypatch: pytest.MonkeyPatch, no_throttle: None
) -> None:
    seen: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get(service: str, url: str, params: dict[str, Any] | None = None) -> Any:
        seen.append((url, params))
        if url.endswith("/lookup"):
            return {"resultCount": 0, "results": []}
        return {
            "results": [
                {
                    "wrapperType": "collection",
                    "collectionId": 1,
                    "collectionName": "Animals (2018 Remix)",
                    "artistName": "Pink Floyd",
                    "collectionViewUrl": "https://music.apple.com/ch/album/animals/1?uo=4",
                }
            ]
        }

    monkeypatch.setattr(st, "_get_json", fake_get)
    m = await st.find_apple(
        AlbumQuery("Pink Floyd", "Animals", barcodes=("5099902987729",)), "CH", {}
    )
    assert m is not None
    assert m.method == "search"
    assert seen[0][1] == {"upc": "5099902987729", "entity": "album", "country": "CH"}
    search_params = seen[-1][1]
    assert search_params is not None
    assert search_params["country"] == "CH"
    assert search_params["term"] == "Pink Floyd Animals"


async def test_find_spotify_without_credentials_does_nothing() -> None:
    assert await st.find_spotify(AlbumQuery("A", "B"), "CH", {}) is None


async def test_find_spotify_queries(monkeypatch: pytest.MonkeyPatch, no_throttle: None) -> None:
    queries: list[str] = []

    class FakeClient:
        def __init__(self, cid: str, secret: str) -> None:
            assert (cid, secret) == ("id", "secret")

        async def search_albums(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
            queries.append(q)
            if q.startswith("upc:"):
                return []
            return [
                {
                    "id": SPOTIFY_ID,
                    "name": "Wish You Were Here",
                    "artists": [{"name": "Pink Floyd"}],
                    "available_markets": ["CH"],
                }
            ]

    monkeypatch.setattr(st.spotify_mod, "SpotifyClient", FakeClient)
    creds = {"spotify_client_id": "id", "spotify_client_secret": "secret"}
    q = AlbumQuery("Pink Floyd (2)", "Wish You Were Here (Remastered)", barcodes=("12345678",))
    m = await st.find_spotify(q, "CH", creds)
    assert m is not None
    assert m.external_id == SPOTIFY_ID
    assert queries == ["upc:12345678", "album:Wish You Were Here artist:Pink Floyd"]
    assert await st.find_spotify(q, "JP", creds) is None  # not in that market


async def test_throttle_spaces_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(st.asyncio, "sleep", fake_sleep)
    t = st.Throttle(5.0)
    await t.wait()
    await t.wait()
    assert len(slept) == 1
    assert 4.0 < slept[0] <= 5.0


# ---------------------------------------------------------------------------
# Web helpers
# ---------------------------------------------------------------------------


def test_settings_normalise() -> None:
    assert web_st._normalise(None) == web_st.default_setting()
    s = web_st._normalise({"services": {"apple": False, "spotify": "yes"}, "country": "de"})
    assert s["services"] == {"spotify": True, "deezer": True, "apple": False}
    assert s["country"] == "DE"
    assert web_st._normalise({"country": "Switzerland"})["country"] == "CH"


def test_config_services_need_credentials() -> None:
    cfg = web_st.StreamingConfig({"spotify": True, "deezer": True, "apple": False}, "CH")
    assert cfg.services == ["deezer"]
    cfg.credentials = {"spotify_client_id": "a", "spotify_client_secret": "b"}
    assert cfg.services == ["spotify", "deezer"]


def test_needs_lookup() -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(status="not_found", checked_at=now - timedelta(days=1))
    assert web_st.needs_lookup(None)
    assert not web_st.needs_lookup(row)  # type: ignore[arg-type]
    assert web_st.needs_lookup(row, recheck=True)  # type: ignore[arg-type]
    row.checked_at = (now - timedelta(days=31)).replace(tzinfo=None)
    assert web_st.needs_lookup(row)  # type: ignore[arg-type]
    for status in ("found", "manual"):
        assert not web_st.needs_lookup(SimpleNamespace(status=status), recheck=True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        (
            "https://mediacat.localhost:8443/catalogue?artist=X&album=Y",
            "/catalogue?artist=X&album=Y",
        ),
        ("/catalogue/abc", "/catalogue/abc"),
        ("//evil.test/x", "/x"),  # only the path is kept: stays on this site
        ("https://evil.test//evil.test/", "/"),
        ("javascript:alert(1)", "/"),
        ("/a\\b", "/"),
    ],
)
def test_safe_back(value: str | None, expected: str) -> None:
    assert web_st.safe_back(value) == expected


def test_link_rows_only_shows_links_with_urls_in_service_order() -> None:
    links = {
        "apple": SimpleNamespace(status="found", url="https://music.apple.com/ch/album/1"),
        "spotify": SimpleNamespace(status="manual", url=None),
        "deezer": SimpleNamespace(status="manual", url="https://www.deezer.com/album/2"),
    }
    rows = web_st.link_rows(links, ["spotify", "deezer", "apple"])  # type: ignore[arg-type]
    assert [r["service"] for r in rows] == ["deezer", "apple"]
    assert rows[1]["label"] == "Apple Music"
    assert web_st.link_rows(links, ["deezer"]) == rows[:1]  # type: ignore[arg-type]
