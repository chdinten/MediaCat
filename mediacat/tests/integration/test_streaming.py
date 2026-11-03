"""Streaming-service links: icons route, lookups, manual corrections, admin settings and job."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.enums import MediaFormat, TokenStatus
from mediacat.db.models import AuditLog, SiteSetting, StreamingLink, Token
from mediacat.ingestion import spotify as spotify_mod
from mediacat.ingestion import streaming as st
from mediacat.web import streaming as web_st

from .fakes import FakeHttp

DEEZER = "https://api.deezer.com"
ITUNES = "https://itunes.apple.com"
SP_ID = "4LH4d3cOWNNsVw41Gqt2kv"
SP_URL = f"https://open.spotify.com/album/{SP_ID}"
BARCODE = "724384260927"  # UPC-12, tried as EAN-13 too


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    web_st._lookup_tasks.clear()
    web_st._failed_at.clear()
    monkeypatch.setattr(web_st, "_job", None)
    for throttle in st.THROTTLES.values():
        monkeypatch.setattr(throttle, "interval", 0.0)


async def add_token(db: AsyncSession, artist: str, title: str, **kw: Any) -> Token:
    kw.setdefault("status", TokenStatus.ACTIVE)
    t = Token(media_format=MediaFormat.VINYL, artist=artist, title=title, **kw)
    db.add(t)
    await db.flush()
    return t


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    db.add(SiteSetting(key=key, value=value))
    await db.commit()


async def spotify_url(db: AsyncSession, token: Token) -> str | None:
    return (await db.execute(select(Token.spotify_url).where(Token.id == token.id))).scalar_one()


async def links(db: AsyncSession) -> dict[str, StreamingLink]:
    db.expire_all()
    return {r.service: r for r in (await db.execute(select(StreamingLink))).scalars()}


def deezer_album(album_id: int = 7, title: str = "Animals", artist: str = "Pink Floyd") -> Any:
    return {"id": album_id, "title": title, "artist": {"name": artist}}


def itunes(*albums: tuple[int, str, str]) -> Any:
    return {
        "resultCount": len(albums),
        "results": [
            {
                "wrapperType": "collection",
                "collectionId": cid,
                "collectionName": title,
                "artistName": artist,
                "collectionViewUrl": f"https://music.apple.com/ch/album/x/{cid}?uo=4",
            }
            for cid, title, artist in albums
        ],
    }


def stub_barcode_hits(fake_http: FakeHttp) -> None:
    fake_http.reply(f"{DEEZER}/album/upc:{BARCODE}", json=deezer_album())
    fake_http.reply(f"{ITUNES}/lookup", json=itunes((99, "Animals (Remastered)", "Pink Floyd")))


def stub_misses(fake_http: FakeHttp) -> None:
    fake_http.reply(f"{DEEZER}/search/album", json={"data": [], "total": 0})
    fake_http.reply(f"{ITUNES}/search", json={"resultCount": 0, "results": []})


@pytest.fixture
async def animals(db: AsyncSession) -> Token:
    t = await add_token(db, "Pink Floyd", "Animals", year=1977, barcode=BARCODE)
    await db.commit()
    return t


class FakeSpotify:
    albums: ClassVar[list[dict[str, Any]]] = []
    queries: ClassVar[list[str]] = []

    def __init__(self, client_id: str, client_secret: str) -> None:
        pass

    async def search_albums(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        type(self).queries.append(query)
        return [] if query.startswith("upc:") else type(self).albums


@pytest.fixture
def fake_spotify(monkeypatch: pytest.MonkeyPatch) -> type[FakeSpotify]:
    cls = type("Spotify", (FakeSpotify,), {"albums": [], "queries": []})
    monkeypatch.setattr(spotify_mod, "SpotifyClient", cls)
    return cls


async def icons(
    client: httpx.AsyncClient, artist: str = "Pink Floyd", album: str = "Animals"
) -> str:
    r = await client.get("/streaming/icons", params={"artist": artist, "album": album})
    assert r.status_code == 200
    return r.text


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


async def test_icons_barcode_match_without_spotify_credentials(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, animals: Token
) -> None:
    stub_barcode_hits(fake_http)
    html = await icons(viewer)
    assert 'href="https://www.deezer.com/album/7"' in html
    assert 'href="https://music.apple.com/ch/album/x/99"' in html
    assert 'aria-label="Open in Deezer"' in html
    assert 'aria-label="Open in Apple Music"' in html
    # One window per service, reused for the next album.
    assert 'target="mediacat-deezer" referrerpolicy="no-referrer"' in html
    assert 'target="mediacat-apple"' in html
    assert "noopener" not in html
    assert "Spotify" not in html  # needs an account, none configured
    assert "/static/streaming/apple-music.svg" in html
    assert all("spotify" not in str(r.url) for r in fake_http.requests)
    apple_lookup = next(r for r in fake_http.requests if r.url.path == "/lookup")
    assert apple_lookup.url.params["country"] == "CH"
    assert apple_lookup.url.params["upc"] == BARCODE

    rows = await links(db)
    assert set(rows) == {"deezer", "apple"}
    assert (rows["deezer"].status, rows["deezer"].match_method) == ("found", "barcode")
    assert rows["apple"].external_id == "99"
    assert rows["apple"].artist_key == "pink floyd"

    # Cached: no further requests.
    sent = len(fake_http.requests)
    assert 'href="https://www.deezer.com/album/7"' in await icons(viewer)
    assert len(fake_http.requests) == sent


async def test_icons_use_discogs_barcode_and_text_search(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    await add_token(
        db,
        "Pink Floyd",
        "Meddle",
        extra={"discogs": {"identifiers": [{"type": "Barcode", "value": "5 099902 987729"}]}},
    )
    await db.commit()
    fake_http.reply(f"{DEEZER}/album/upc:5099902987729", json={"error": {"code": 800}})
    fake_http.reply(
        f"{DEEZER}/search/album",
        json={
            "data": [deezer_album(3, "Meddle (Remastered)"), deezer_album(4, "Meddle", "Tribute")]
        },
    )
    fake_http.reply(f"{ITUNES}/lookup", json={"resultCount": 0, "results": []})
    fake_http.reply(f"{ITUNES}/search", json=itunes((5, "Medley", "Pink Floyd")))

    html = await icons(viewer, album="Meddle")
    assert 'href="https://www.deezer.com/album/3"' in html
    assert "music.apple.com" not in html  # "Medley" is not confident enough
    rows = await links(db)
    assert (rows["deezer"].match_method, rows["deezer"].match_score) == ("search", 0.97)
    assert rows["apple"].status == "not_found"
    search = next(r for r in fake_http.requests if r.url.path == "/search/album")
    assert search.url.params["q"] == 'artist:"Pink Floyd" album:"Meddle"'


async def test_not_found_is_cached_then_rechecked_after_30_days(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    await add_token(db, "Obscure Band", "Bootleg Live")
    await db.commit()
    stub_misses(fake_http)
    html = await icons(viewer, "Obscure Band", "Bootleg Live")
    assert "stream-link " not in html
    assert "Edit streaming links" in html
    rows = await links(db)
    assert {r.status for r in rows.values()} == {"not_found"}

    sent = len(fake_http.requests)
    await icons(viewer, "Obscure Band", "Bootleg Live")
    assert len(fake_http.requests) == sent

    rows["deezer"].checked_at = datetime.now(UTC) - timedelta(days=31)
    await db.commit()
    await icons(viewer, "Obscure Band", "Bootleg Live")
    assert len(fake_http.requests) == sent + 1  # only the stale Deezer row


async def test_spotify_shown_once_configured_and_token_kept_in_step(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: FakeHttp,
    fake_spotify: type[FakeSpotify],
    animals: Token,
) -> None:
    await set_setting(db, "spotify_client_id", "cid")
    await set_setting(db, "spotify_client_secret", "secret")
    await set_setting(db, "streaming", json.dumps({"services": {"deezer": False, "apple": False}}))
    fake_spotify.albums = [
        {
            "id": SP_ID,
            "name": "Animals",
            "artists": [{"name": "Pink Floyd"}],
            "available_markets": ["CH"],
        }
    ]
    html = await icons(viewer)
    assert f'href="{SP_URL}"' in html
    assert 'aria-label="Open in Spotify"' in html
    assert "spotify-white.svg" in html
    assert "spotify-black.svg" in html
    assert fake_spotify.queries == [
        f"upc:{BARCODE}",
        f"upc:0{BARCODE}",
        "album:Animals artist:Pink Floyd",
    ]
    assert fake_http.requests == []  # Deezer / Apple switched off
    assert await spotify_url(db, animals) == SP_URL


async def test_legacy_spotify_url_is_reused(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_spotify: type[FakeSpotify]
) -> None:
    await add_token(db, "Pink Floyd", "Animals", spotify_url=SP_URL)
    await set_setting(db, "spotify_client_id", "cid")
    await set_setting(db, "spotify_client_secret", "secret")
    await set_setting(db, "streaming", json.dumps({"services": {"deezer": False, "apple": False}}))
    html = await icons(viewer)
    assert f'href="{SP_URL}"' in html
    assert fake_spotify.queries == []


async def test_failed_lookup_is_not_cached_and_cools_down(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, animals: Token
) -> None:
    fake_http.reply(f"{DEEZER}/album/upc:{BARCODE}", status=503, json={})
    fake_http.reply(f"{ITUNES}/lookup", json=itunes((99, "Animals", "Pink Floyd")))
    html = await icons(viewer)
    assert "music.apple.com" in html
    assert "deezer.com" not in html
    assert set(await links(db)) == {"apple"}
    sent = len(fake_http.requests)
    await icons(viewer)
    assert len(fake_http.requests) == sent  # Deezer is cooling down


async def test_slow_lookup_answers_pending_and_polls(
    viewer: httpx.AsyncClient, db: AsyncSession, animals: Token, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def slow(query: st.AlbumQuery, country: str, creds: dict[str, str]) -> st.Match | None:
        await release.wait()
        return st.Match("https://www.deezer.com/album/1", "1", "search", 1.0)

    monkeypatch.setitem(web_st.FINDERS, "deezer", slow)
    monkeypatch.setitem(web_st.FINDERS, "apple", slow)
    monkeypatch.setattr(web_st, "WAIT_S", 0.05)

    html = await icons(viewer)
    assert 'hx-trigger="load delay:3s"' in html
    assert "Looking up streaming links" in html
    assert 'hx-get="/streaming/icons?artist=Pink+Floyd&amp;album=Animals"' in html
    (task,) = web_st._lookup_tasks.values()
    release.set()
    await task
    html = await icons(viewer)
    assert "load delay" not in html
    assert 'href="https://www.deezer.com/album/1"' in html


async def test_untitled_album_and_anonymous(
    viewer: httpx.AsyncClient, anon: httpx.AsyncClient, fake_http: FakeHttp
) -> None:
    html = await icons(viewer, album="")
    assert "stream-links--none" in html
    assert fake_http.requests == []
    r = await anon.get("/streaming/icons", params={"artist": "A", "album": "B"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_album_pages_render_lazy_slots(
    viewer: httpx.AsyncClient, db: AsyncSession, animals: Token
) -> None:
    slot = 'hx-get="/streaming/icons?artist=Pink%20Floyd&amp;album=Animals"'
    dash = (await viewer.get("/")).text
    assert slot in dash
    assert 'hx-trigger="intersect once"' in dash
    assert slot in (await viewer.get(f"/catalogue/{animals.id}")).text
    assert slot in (await viewer.get(f"/catalogue/{animals.id}/preview")).text
    album = await viewer.get(
        "/catalogue/browse/album", params={"artist": "Pink Floyd", "album": "Animals"}
    )
    assert slot in album.text


# ---------------------------------------------------------------------------
# Edit links (any logged-in user)
# ---------------------------------------------------------------------------


async def test_edit_page_lists_available_services(
    viewer: httpx.AsyncClient, db: AsyncSession, animals: Token
) -> None:
    r = await viewer.get(
        "/streaming/edit", params={"artist": "Pink Floyd", "album": "Animals", "back": "/x?y=1"}
    )
    assert r.status_code == 200
    assert 'name="url_deezer"' in r.text
    assert 'name="url_apple"' in r.text
    assert 'name="url_spotify"' not in r.text
    assert "Not shown (switched off or not configured): Spotify" in r.text
    assert 'value="/x?y=1"' in r.text
    r = await viewer.get("/streaming/edit", params={"artist": "Pink Floyd", "album": ""})
    assert r.status_code == 400


async def test_manual_override_survives_auto_matching(
    viewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, animals: Token
) -> None:
    r = await viewer.post(
        "/streaming/edit",
        data={
            "artist": "Pink Floyd",
            "album": "Animals",
            "back": "https://evil.test/catalogue?artist=Pink+Floyd",
            "mode_deezer": "url",
            "url_deezer": "https://www.deezer.com/fr/album/555?utm_source=x",
            "mode_apple": "none",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/catalogue?artist=Pink+Floyd"
    rows = await links(db)
    assert (rows["deezer"].status, rows["deezer"].url) == (
        "manual",
        "https://www.deezer.com/album/555",
    )
    assert rows["deezer"].updated_by is not None
    assert (rows["apple"].status, rows["apple"].url) == ("manual", None)

    stub_barcode_hits(fake_http)
    html = await icons(viewer)
    assert 'href="https://www.deezer.com/album/555"' in html
    assert "music.apple.com" not in html
    assert fake_http.requests == []  # manual rows are never looked up

    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "streaming_links.edit"))
    ).scalar_one()
    assert audit.detail is not None
    assert audit.detail["links"] == {"deezer": "https://www.deezer.com/album/555", "apple": "none"}

    # Back to automatic: the manual row goes, the next view looks the album up again.
    r = await viewer.post(
        "/streaming/edit",
        data={
            "artist": "Pink Floyd",
            "album": "Animals",
            "mode_deezer": "auto",
            "mode_apple": "auto",
        },
    )
    assert r.status_code == 303
    assert await links(db) == {}
    html = await icons(viewer)
    assert 'href="https://www.deezer.com/album/7"' in html


async def test_auto_save_never_overwrites_manual(
    db: AsyncSession, session_factory: Any, animals: Token
) -> None:
    async with session_factory() as s:
        await web_st.save_manual(s, "Pink Floyd", "Animals", "deezer", None, None, None)
        await web_st.save_auto(
            s,
            "pink floyd",
            "ANIMALS",
            "deezer",
            st.Match("https://www.deezer.com/album/1", "1", "search", 1.0),
        )
        await s.commit()
    rows = await links(db)
    assert (rows["deezer"].status, rows["deezer"].url) == ("manual", None)


async def test_invalid_link_is_rejected(
    contributor: httpx.AsyncClient, db: AsyncSession, animals: Token
) -> None:
    r = await contributor.post(
        "/streaming/edit",
        data={
            "artist": "Pink Floyd",
            "album": "Animals",
            "mode_deezer": "url",
            "url_deezer": "https://evil.test/album/1",
            "mode_apple": "none",
        },
    )
    assert r.status_code == 400
    assert "That is not a Deezer album link." in r.text
    assert 'value="https://evil.test/album/1"' in r.text
    assert await links(db) == {}  # nothing saved, not even the valid "none"


async def test_manual_spotify_link_sets_token_spotify_url(
    viewer: httpx.AsyncClient, db: AsyncSession, animals: Token
) -> None:
    await set_setting(db, "spotify_client_id", "cid")
    await set_setting(db, "spotify_client_secret", "secret")
    r = await viewer.post(
        "/streaming/edit",
        data={
            "artist": "Pink Floyd",
            "album": "Animals",
            "mode_spotify": "url",
            "url_spotify": f"spotify:album:{SP_ID}",
            "mode_deezer": "none",
            "mode_apple": "none",
        },
    )
    assert r.status_code == 303
    assert await spotify_url(db, animals) == SP_URL
    r = await viewer.post(
        "/streaming/edit",
        data={"artist": "Pink Floyd", "album": "Animals", "mode_spotify": "none"},
    )
    assert await spotify_url(db, animals) is None


async def test_edit_needs_login_and_csrf(
    anon: httpx.AsyncClient, make_client: Any, app: Any, animals: Token
) -> None:
    r = await anon.post("/streaming/edit", data={"artist": "Pink Floyd", "album": "Animals"})
    assert r.status_code == 303
    viewer = await make_client("viewer")
    viewer.headers.pop("X-CSRF-Token")
    r = await viewer.post("/streaming/edit", data={"artist": "Pink Floyd", "album": "Animals"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


async def test_admin_tab_and_settings(
    admin: httpx.AsyncClient, viewer: httpx.AsyncClient, session_factory: Any
) -> None:
    page = await admin.get("/admin", params={"tab": "streaming"})
    assert page.status_code == 200
    assert 'name="service_deezer"' in page.text
    assert "missing — icons hidden" in page.text  # Spotify without credentials
    assert 'value="CH"' in page.text
    assert "Not run since the app started." in page.text

    r = await admin.post("/admin/streaming/settings", data={"service_apple": "on", "country": "de"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?tab=streaming&st_msg=saved"
    cfg = await web_st.load_config(session_factory)
    assert cfg.country == "DE"
    assert cfg.enabled == {"spotify": False, "deezer": False, "apple": True}

    r = await admin.post("/admin/streaming/settings", data={"country": "Germany"})
    assert r.headers["location"] == "/admin?tab=streaming&st_error=country"
    assert (
        await viewer.post("/admin/streaming/settings", data={"country": "CH"})
    ).status_code == 403
    assert (await viewer.get("/admin/streaming/match/status")).status_code == 403
    assert (await viewer.post("/admin/streaming/match")).status_code == 403


async def test_match_all_job(
    admin: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, animals: Token
) -> None:
    await add_token(db, "Pink Floyd", "ANIMALS")  # same album, other spelling
    await add_token(db, "Obscure Band", "Bootleg Live")
    await add_token(db, "Nobody", "")  # untitled: skipped
    await db.commit()
    stub_barcode_hits(fake_http)
    stub_misses(fake_http)

    r = await admin.post("/admin/streaming/match")
    assert r.status_code == 200
    assert 'id="streaming-job"' in r.text
    assert web_st._job is not None
    assert web_st._job.task is not None
    await web_st._job.task
    status = (await admin.get("/admin/streaming/match/status")).text
    assert "Finished — 2 albums" in status
    assert "Deezer: 1 found, 1 not found" in status
    assert "Apple Music: 1 found, 1 not found" in status
    assert "every 2s" not in status  # stops polling
    assert len((await db.execute(select(StreamingLink))).scalars().all()) == 4

    # A second run finds everything cached.
    sent = len(fake_http.requests)
    await admin.post("/admin/streaming/match")
    await web_st._job.task  # type: ignore[union-attr]
    assert len(fake_http.requests) == sent
    assert "0 looked up" in (await admin.get("/admin/streaming/match/status")).text

    # "Re-check not found" looks the misses up again.
    await admin.post("/admin/streaming/match", data={"recheck": "on"})
    await web_st._job.task  # type: ignore[union-attr]
    assert len(fake_http.requests) == sent + 2
    page = (await admin.get("/admin", params={"tab": "streaming"})).text
    assert "<td>1</td>" in page


async def test_match_job_stop(
    admin: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3):
        await add_token(db, "Band", f"Album {i}")
    await db.commit()
    gate = asyncio.Event()

    async def finder(query: st.AlbumQuery, country: str, creds: dict[str, str]) -> st.Match | None:
        await gate.wait()
        return None

    monkeypatch.setitem(web_st.FINDERS, "deezer", finder)
    monkeypatch.setitem(web_st.FINDERS, "apple", finder)
    await admin.post("/admin/streaming/match")
    job = web_st._job
    assert job is not None
    for _ in range(200):  # wait until the first album's lookup is under way
        if job.current:
            break
        await asyncio.sleep(0.01)
    running = (await admin.get("/admin/streaming/match/status")).text
    assert "Band — Album 0" in running
    assert 'hx-trigger="every 2s"' in running
    assert "Stop" in running
    await admin.post("/admin/streaming/match/stop")
    gate.set()
    await web_st._job.task  # type: ignore[union-attr]
    assert "Stopped after 1 of 3 albums" in (await admin.get("/admin/streaming/match/status")).text
