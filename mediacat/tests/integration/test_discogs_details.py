"""Integration tests for ``mediacat.web.discogs_details``.

Real app + test database.  Discogs is answered by :class:`~.fakes.FakeHttp`; the
collection client is the real ``DiscogsConnector`` (built by
``admin._get_discogs_connector`` from the token stored in SiteSetting), with
retries and the rate limiter turned off so nothing waits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from mediacat.db.enums import ImageRegion, ImportJobStatus, MediaFormat, TokenStatus
from mediacat.db.models import CollectionImportJob, Country, MediaObject, SiteSetting, Token
from mediacat.ingestion.discogs_extras import merge_collection
from mediacat.web import admin, discogs_details, print_sheet

from .fakes import FakeHttp, FakeObjectStore, NetworkGuard, json_body, png_bytes

API = "https://api.discogs.com"
RELEASE_ID = 370444

FIELDS = [
    {
        "id": 1,
        "name": "Media Condition",
        "type": "dropdown",
        "options": ["Mint (M)"],
        "position": 1,
    },
    {"id": 5, "name": "Runout / Matrix LP1 - A", "type": "textarea", "options": [], "position": 4},
    {"id": 6, "name": "Runout / Matrix LP1 - B", "type": "textarea", "options": [], "position": 5},
    {"id": 7, "name": "Autographed", "type": "dropdown", "options": ["Yes", "No"], "position": 6},
    {"id": 8, "name": "Signed by", "type": "textarea", "options": [], "position": 7},
]
INSTANCE = {
    "instance_id": 99,
    "folder_id": 1,
    "notes": [{"field_id": 5, "value": "OLD A"}, {"field_id": 6, "value": "SH B-1"}],
}
RELEASE = {
    "id": RELEASE_ID,
    "formats": [{"name": "Vinyl", "qty": "2"}],
    "identifiers": [
        {"type": "Barcode", "value": "5099902894" + "3"},
        {"type": "Matrix / Runout", "value": "SHVL 804 A-1", "description": "Side A, variant 1"},
        {"type": "Matrix / Runout", "value": "SHVL 804 B-1", "description": "Side B, variant 1"},
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stored_collection(**overrides: Any) -> dict[str, Any]:
    coll = merge_collection(None, username="me", fields=FIELDS, instance=INSTANCE)
    coll.update(overrides)
    return coll


async def add_token(db: AsyncSession, **kw: Any) -> Token:
    kw.setdefault("status", TokenStatus.ACTIVE)
    kw.setdefault("artist", "Pink Floyd")
    kw.setdefault("title", "The Dark Side of the Moon")
    t = Token(media_format=MediaFormat.VINYL, **kw)
    db.add(t)
    await db.commit()
    return t


async def reload(db: AsyncSession, token: Token) -> Token:
    token_id = token.id
    db.expire_all()
    return (await db.execute(select(Token).where(Token.id == token_id))).scalar_one()


async def set_discogs_token(db: AsyncSession, value: str = "dc-secret") -> None:
    db.add(SiteSetting(key="discogs_token", value=value))
    await db.commit()


def query(location: str) -> dict[str, str]:
    parts = urlsplit(location)
    return {k: v[0] for k, v in parse_qs(parts.query).items()} | {"#": parts.fragment}


class _NoWait:
    async def acquire(self) -> None:
        return None


@pytest.fixture
def connectors(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Real collection connectors, one attempt per request and no rate-limit wait."""
    real = admin._get_discogs_connector
    made: list[Any] = []

    def fast(request: Any, override_token: str = "") -> Any:
        c = real(request, override_token)
        c._max_retries = 1
        c._rate_limiter = _NoWait()
        made.append(c)
        return c

    monkeypatch.setattr(admin, "_get_discogs_connector", fast)
    return made


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discogs_details, "RATE_LIMIT_WAITS", (0.0, 0.0, 0.0))
    discogs_details._PREVIEW_CACHE.clear()


def field_url(field_id: int, *, folder: int = 1, instance: int = 99) -> str:
    return (
        f"{API}/users/me/collection/folders/{folder}/releases/{RELEASE_ID}"
        f"/instances/{instance}/fields/{field_id}"
    )


# ---------------------------------------------------------------------------
# "My copy": save locally
# ---------------------------------------------------------------------------


async def test_collection_save_stores_values_in_token_extra(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await add_token(
        db,
        discogs_release_id=RELEASE_ID,
        extra={"discogs": {"format_qty": 1}, "discogs_collection": stored_collection()},
    )
    r = await contributor.post(
        f"/catalogue/{token.id}/discogs-collection",
        data={"op": "save", "f_5": "  NEW A  ", "f_7": "Yes", "f_999": "ignored", "x": "y"},
    )
    assert r.status_code == 303
    assert query(r.headers["location"]) == {
        "msg": "collection_saved",
        "n": "0",
        "#": "discogs-copy",
    }
    assert r.headers["location"].startswith(f"/catalogue/{token.id}?")

    extra = (await reload(db, token)).extra or {}
    coll = extra["discogs_collection"]
    assert coll["values"] == {"1": "", "5": "NEW A", "6": "SH B-1", "7": "Yes", "8": ""}
    assert coll["discogs_values"] == {"1": "", "5": "OLD A", "6": "SH B-1", "7": "", "8": ""}
    assert "999" not in coll["values"]
    assert extra["discogs"] == {"format_qty": 1}  # other extra keys kept


async def test_collection_save_before_fields_are_loaded(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await add_token(db, discogs_release_id=RELEASE_ID, extra={"note": "x"})
    r = await contributor.post(f"/catalogue/{token.id}/discogs-collection", data={"f_5": "A"})
    assert query(r.headers["location"])["msg"] == "collection_not_loaded"
    assert (await reload(db, token)).extra == {"note": "x"}


@pytest.mark.parametrize("token_id", ["not-a-uuid", str(uuid.uuid4())])
async def test_collection_save_unknown_token_is_404(
    contributor: httpx.AsyncClient, token_id: str
) -> None:
    r = await contributor.post(f"/catalogue/{token_id}/discogs-collection", data={"f_5": "A"})
    assert r.status_code == 404


async def test_collection_save_soft_deleted_token_is_404(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await add_token(
        db, extra={"discogs_collection": stored_collection()}, deleted_at=datetime.now(UTC)
    )
    r = await contributor.post(f"/catalogue/{token.id}/discogs-collection", data={"f_5": "A"})
    assert r.status_code == 404


async def test_viewer_cannot_save_collection_fields(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    coll = stored_collection()
    token = await add_token(db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": coll})
    r = await viewer.post(f"/catalogue/{token.id}/discogs-collection", data={"f_5": "HACK"})
    assert r.status_code == 403
    assert (await reload(db, token)).extra == {"discogs_collection": coll}


async def test_contributor_cannot_write_back(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    coll = stored_collection()
    token = await add_token(db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": coll})
    r = await contributor.post(
        f"/catalogue/{token.id}/discogs-collection", data={"op": "writeback", "f_5": "NEW"}
    )
    assert r.status_code == 403
    assert (await reload(db, token)).extra == {"discogs_collection": coll}
    assert fake_http.requests == []


# ---------------------------------------------------------------------------
# "My copy": write back to Discogs
# ---------------------------------------------------------------------------


async def test_write_back_posts_only_changed_fields(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, connectors: list[Any]
) -> None:
    await set_discogs_token(db)
    token = await add_token(
        db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": stored_collection()}
    )
    fake_http.add("POST", field_url(5), httpx.Response(204))
    fake_http.add("POST", field_url(8), httpx.Response(204))

    r = await reviewer.post(
        f"/catalogue/{token.id}/discogs-collection",
        data={"op": "writeback", "f_5": "05 249 A - 1", "f_6": "SH B-1", "f_8": "Roger Waters"},
    )
    assert query(r.headers["location"]) == {"msg": "writeback_ok", "n": "2", "#": "discogs-copy"}

    # What went to Discogs: one POST per changed field, with the stored token.
    assert [(q.method, str(q.url), json_body(q)) for q in fake_http.requests] == [
        ("POST", field_url(5), {"value": "05 249 A - 1"}),
        ("POST", field_url(8), {"value": "Roger Waters"}),
    ]
    assert {q.headers["authorization"] for q in fake_http.requests} == {"Discogs token=dc-secret"}

    coll = ((await reload(db, token)).extra or {})["discogs_collection"]
    assert coll["values"]["5"] == coll["discogs_values"]["5"] == "05 249 A - 1"
    assert coll["values"]["8"] == coll["discogs_values"]["8"] == "Roger Waters"
    assert all(c._client is None for c in connectors)  # connector closed


async def test_admin_can_write_back_a_previously_saved_local_edit(
    admin: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, connectors: list[Any]
) -> None:
    await set_discogs_token(db)
    coll = stored_collection()
    coll["values"]["7"] = "Yes"  # saved locally earlier, not on Discogs yet
    token = await add_token(db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": coll})
    fake_http.add("POST", field_url(7), httpx.Response(204))

    r = await admin.post(f"/catalogue/{token.id}/discogs-collection", data={"op": "writeback"})
    assert query(r.headers["location"])["n"] == "1"
    assert [json_body(q) for q in fake_http.requests] == [{"value": "Yes"}]
    stored = ((await reload(db, token)).extra or {})["discogs_collection"]
    assert stored["discogs_values"]["7"] == "Yes"


async def test_write_back_with_nothing_changed(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    token = await add_token(
        db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": stored_collection()}
    )
    r = await reviewer.post(
        f"/catalogue/{token.id}/discogs-collection", data={"op": "writeback", "f_5": "OLD A"}
    )
    assert query(r.headers["location"])["msg"] == "writeback_nothing"
    assert fake_http.requests == []


async def test_write_back_for_a_release_not_in_the_collection(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    coll = merge_collection(None, username="me", fields=FIELDS, instance=None)
    token = await add_token(db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": coll})
    r = await reviewer.post(
        f"/catalogue/{token.id}/discogs-collection", data={"op": "writeback", "f_5": "NEW"}
    )
    assert query(r.headers["location"])["msg"] == "writeback_not_in_collection"
    assert fake_http.requests == []
    # The local edit is still saved.
    stored = ((await reload(db, token)).extra or {})["discogs_collection"]
    assert stored["values"]["5"] == "NEW"
    assert stored["discogs_values"]["5"] == ""


async def test_write_back_without_a_discogs_token(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    token = await add_token(
        db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": stored_collection()}
    )
    r = await reviewer.post(
        f"/catalogue/{token.id}/discogs-collection", data={"op": "writeback", "f_5": "NEW"}
    )
    q = query(r.headers["location"])
    assert (q["msg"], q["n"], q["err"]) == ("writeback_failed", "0", "no Discogs token configured")
    stored = ((await reload(db, token)).extra or {})["discogs_collection"]
    assert stored["values"]["5"] == "NEW"
    assert stored["discogs_values"]["5"] == "OLD A"  # still pending
    assert fake_http.requests == []


async def test_write_back_failure_midway_records_what_was_written(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, connectors: list[Any]
) -> None:
    await set_discogs_token(db)
    token = await add_token(
        db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": stored_collection()}
    )
    fake_http.add("POST", field_url(5), httpx.Response(204))
    fake_http.add("POST", field_url(6), httpx.Response(403, json={"message": "forbidden"}))

    r = await reviewer.post(
        f"/catalogue/{token.id}/discogs-collection",
        data={"op": "writeback", "f_5": "NEW A", "f_6": "NEW B", "f_8": "Gilmour"},
    )
    q = query(r.headers["location"])
    assert (q["msg"], q["n"], q["err"]) == ("writeback_failed", "1", "HTTP 403")
    assert [str(x.url) for x in fake_http.requests] == [field_url(5), field_url(6)]  # stops at 403

    stored = ((await reload(db, token)).extra or {})["discogs_collection"]
    assert (stored["values"]["5"], stored["discogs_values"]["5"]) == ("NEW A", "NEW A")
    assert (stored["values"]["6"], stored["discogs_values"]["6"]) == ("NEW B", "SH B-1")
    assert (stored["values"]["8"], stored["discogs_values"]["8"]) == ("Gilmour", "")
    assert all(c._client is None for c in connectors)


async def test_write_back_transport_error_is_reported(
    reviewer: httpx.AsyncClient,
    db: AsyncSession,
    network_guard: NetworkGuard,
    connectors: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No stub: the network guard refuses the connection (offline Discogs).
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    network_guard.allow_blocked()
    await set_discogs_token(db)
    token = await add_token(
        db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": stored_collection()}
    )
    r = await reviewer.post(
        f"/catalogue/{token.id}/discogs-collection", data={"op": "writeback", "f_5": "NEW"}
    )
    q = query(r.headers["location"])
    assert q["msg"] == "writeback_failed"
    assert "network blocked" in q["err"]
    assert network_guard.blocked == [field_url(5)]


# ---------------------------------------------------------------------------
# Details refresh
# ---------------------------------------------------------------------------


def stub_collection_reads(fake_http: FakeHttp, instance: dict[str, Any] | None = INSTANCE) -> None:
    fake_http.add("GET", f"{API}/oauth/identity", httpx.Response(200, json={"username": "me"}))
    fake_http.add(
        "GET",
        f"{API}/users/me/collection/fields",
        httpx.Response(200, json={"fields": [*FIELDS, {"name": "no id"}]}),
    )
    fake_http.add(
        "GET",
        f"{API}/users/me/collection/releases/{RELEASE_ID}",
        httpx.Response(200, json={"releases": [instance]})
        if instance
        else httpx.Response(404, json={"message": "Release not in collection"}),
    )


async def test_refresh_stores_identifiers_and_collection_fields(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, connectors: list[Any]
) -> None:
    await set_discogs_token(db)
    local = stored_collection()
    local["values"]["8"] = "Nick Mason"  # local edit not written back: survives
    token = await add_token(
        db, discogs_release_id=RELEASE_ID, extra={"discogs_collection": local, "keep": 1}
    )
    fake_http.add("GET", f"{API}/releases/{RELEASE_ID}", httpx.Response(200, json=RELEASE))
    instance = {**INSTANCE, "notes": [{"field_id": 5, "value": "REMOTE A"}]}
    stub_collection_reads(fake_http, instance)

    r = await contributor.post(f"/catalogue/{token.id}/discogs-details/refresh")
    assert r.status_code == 303
    assert query(r.headers["location"]) == {"msg": "discogs_details_refreshed", "#": "discogs-copy"}

    extra = (await reload(db, token)).extra or {}
    assert extra["keep"] == 1
    assert extra["discogs"]["format_qty"] == 2
    assert [i["value"] for i in extra["discogs"]["identifiers"]] == [
        "50999028943",
        "SHVL 804 A-1",
        "SHVL 804 B-1",
    ]
    coll = extra["discogs_collection"]
    assert [f["id"] for f in coll["fields"]] == [1, 5, 6, 7, 8]  # the id-less field dropped
    assert (coll["username"], coll["instance_id"], coll["folder_id"]) == ("me", 99, 1)
    assert coll["values"]["5"] == "REMOTE A"
    assert coll["values"]["6"] == ""  # cleared on Discogs
    assert coll["values"]["8"] == "Nick Mason"
    assert coll["discogs_values"]["8"] == ""
    # The public release call carries no personal token; the collection calls do.
    release_req = fake_http.sent("GET", f"{API}/releases/")[0]
    assert "authorization" not in release_req.headers
    assert fake_http.sent("GET", f"{API}/oauth/identity")[0].headers["authorization"] == (
        "Discogs token=dc-secret"
    )
    assert all(c._client is None for c in connectors)


async def test_refresh_release_not_in_collection(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, connectors: list[Any]
) -> None:
    await set_discogs_token(db)
    token = await add_token(db, discogs_release_id=RELEASE_ID)
    fake_http.add("GET", f"{API}/releases/{RELEASE_ID}", httpx.Response(200, json=RELEASE))
    stub_collection_reads(fake_http, instance=None)

    r = await contributor.post(f"/catalogue/{token.id}/discogs-details/refresh")
    assert query(r.headers["location"])["msg"] == "discogs_details_refreshed"
    coll = ((await reload(db, token)).extra or {})["discogs_collection"]
    assert coll["in_collection"] is False
    assert coll["instance_id"] is None
    assert set(coll["values"].values()) == {""}


async def test_refresh_without_discogs_token_saves_identifiers_only(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    token = await add_token(db, discogs_release_id=RELEASE_ID)
    fake_http.add("GET", f"{API}/releases/{RELEASE_ID}", httpx.Response(200, json=RELEASE))

    r = await reviewer.post(f"/catalogue/{token.id}/discogs-details/refresh")
    q = query(r.headers["location"])
    assert q["msg"] == "discogs_details_partial"
    assert q["err"] == "collection (no Discogs token configured)"
    extra = (await reload(db, token)).extra or {}
    assert extra["discogs"]["format_qty"] == 2
    assert "discogs_collection" not in extra


async def test_refresh_rate_limited_after_retries(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    token = await add_token(db, discogs_release_id=RELEASE_ID, extra={"old": True})
    fake_http.add("GET", f"{API}/releases/{RELEASE_ID}", httpx.Response(429))

    r = await reviewer.post(f"/catalogue/{token.id}/discogs-details/refresh")
    q = query(r.headers["location"])
    assert q["msg"] == "discogs_details_partial"
    assert q["err"] == (
        f"{discogs_details.RATE_LIMITED_MSG}, collection (no Discogs token configured)"
    )
    assert len(fake_http.sent("GET", f"{API}/releases/")) == 4  # 1 + 3 retries
    assert (await reload(db, token)).extra == {"old": True}  # nothing to save


async def test_refresh_collection_and_release_errors(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp, connectors: list[Any]
) -> None:
    await set_discogs_token(db, "revoked")
    token = await add_token(db, discogs_release_id=RELEASE_ID, extra={"old": True})
    fake_http.add("GET", f"{API}/releases/{RELEASE_ID}", httpx.Response(404))
    fake_http.add("GET", f"{API}/oauth/identity", httpx.Response(401))

    r = await reviewer.post(f"/catalogue/{token.id}/discogs-details/refresh")
    q = query(r.headers["location"])
    assert q["err"] == "identifiers, collection"
    assert len(fake_http.sent("GET", f"{API}/releases/")) == 1  # only 429 is retried
    assert (await reload(db, token)).extra == {"old": True}
    assert all(c._client is None for c in connectors)


async def test_refresh_without_release_id(contributor: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await add_token(db)
    r = await contributor.post(f"/catalogue/{token.id}/discogs-details/refresh")
    assert r.status_code == 303
    assert r.headers["location"] == f"/catalogue/{token.id}?msg=no_discogs_id"


async def test_refresh_helper_reports_missing_release_id(app: FastAPI) -> None:
    token = SimpleNamespace(id=uuid.uuid4(), discogs_release_id=None, extra=None)
    errors = await discogs_details.refresh_token_discogs_details(
        _request(app),
        token,  # type: ignore[arg-type]
    )
    assert errors == ["no Discogs release ID"]


async def test_viewer_cannot_refresh(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await add_token(db, discogs_release_id=RELEASE_ID)
    r = await viewer.post(f"/catalogue/{token.id}/discogs-details/refresh")
    assert r.status_code == 403


async def test_refresh_unknown_token_is_404(contributor: httpx.AsyncClient) -> None:
    r = await contributor.post(f"/catalogue/{uuid.uuid4()}/discogs-details/refresh")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Album sheet PDF
# ---------------------------------------------------------------------------


@pytest.fixture
def sheets(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict[str, Any], bytes | None]]:
    """Record what the PDF builder is given (the PDF text itself is compressed)."""
    real = print_sheet.build_album_sheet
    seen: list[tuple[dict[str, Any], bytes | None]] = []

    def spy(sheet: dict[str, Any], cover: bytes | None = None) -> bytes:
        seen.append((sheet, cover))
        return real(sheet, cover)

    monkeypatch.setattr(print_sheet, "build_album_sheet", spy)
    return seen


async def add_image(
    db: AsyncSession,
    store: FakeObjectStore,
    token: Token,
    colour: str,
    *,
    primary: bool = False,
    region: ImageRegion | None = None,
    stored: bool = True,
) -> bytes:
    data = png_bytes(colour=colour)
    obj = await store.put_image(data, "image/png")
    if not stored:
        del store.objects[(obj.bucket, obj.object_key)]
    db.add(
        MediaObject(
            token_id=token.id,
            content_hash=obj.content_hash,
            bucket=obj.bucket,
            object_key=obj.object_key,
            mime_type="image/png",
            size_bytes=obj.size_bytes,
            is_primary_cover=primary,
            region=region,
        )
    )
    await db.commit()
    return data


async def test_print_pdf_renders_sheet_from_db(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    object_store: FakeObjectStore,
    sheets: list[tuple[dict[str, Any], bytes | None]],
) -> None:
    gb = (await db.execute(select(Country).where(Country.alpha2 == "GB"))).scalar_one()
    coll = stored_collection()
    coll["values"].update({"5": "LOCAL A", "7": "Yes", "8": "David Gilmour"})
    token = await add_token(
        db,
        title="The Wall",
        year=1979,
        country_id=gb.id,
        matrix_runout="FALLBACK A",
        matrix_runout_b="SHDW 411 B",
        tracklist=[
            {"position": "A1", "title": "In the Flesh?"},
            {"position": "C1", "title": "Hey You"},
            {"position": "D1", "title": ""},
            "junk",
        ],
        extra={"discogs_collection": coll, "discogs": {"format_qty": 2}},
    )
    await add_image(db, object_store, token, "blue")
    primary = await add_image(db, object_store, token, "red", primary=True)

    r = await viewer.get(f"/catalogue/{token.id}/print.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"] == (
        'attachment; filename="Pink Floyd - The Wall.pdf"; '
        "filename*=UTF-8''Pink%20Floyd%20-%20The%20Wall.pdf"
    )
    assert r.content.startswith(b"%PDF")

    ((sheet, cover),) = sheets
    assert cover == primary
    assert (sheet["artist"], sheet["title"], sheet["year"]) == ("Pink Floyd", "The Wall", 1979)
    assert sheet["country"] == gb.name
    assert sheet["lp_total"] == 2
    assert sheet["lps"] == [
        {
            "lp": 1,
            "tracks": [("A1", "In the Flesh?")],
            # collection fields win over the token's own matrix_runout / _b
            "runouts": [("Side A", "LOCAL A"), ("Side B", "SH B-1")],
        },
        {"lp": 2, "tracks": [("C1", "Hey You")], "runouts": [("Side C", ""), ("Side D", "")]},
    ]
    assert (sheet["signed"], sheet["signed_by"]) == (True, "David Gilmour")


@pytest.mark.parametrize("case", ["cover_front", "first", "missing"])
async def test_print_pdf_cover_choice(
    viewer: httpx.AsyncClient,
    db: AsyncSession,
    object_store: FakeObjectStore,
    sheets: list[tuple[dict[str, Any], bytes | None]],
    case: str,
) -> None:
    token = await add_token(db, title="Animals", matrix_runout="SHVL 815 A")
    first = await add_image(db, object_store, token, "green", stored=case != "missing")
    front = None
    if case == "cover_front":
        front = await add_image(db, object_store, token, "white", region=ImageRegion.COVER_FRONT)
    r = await viewer.get(f"/catalogue/{token.id}/print.pdf")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    ((sheet, cover),) = sheets
    assert cover == {"cover_front": front, "first": first, "missing": None}[case]
    assert sheet["lps"] == [{"lp": 1, "tracks": [], "runouts": [("Side A", "SHVL 815 A"),
                                                                 ("Side B", "")]}]  # fmt: skip


async def test_print_pdf_filename_is_sanitised(
    viewer: httpx.AsyncClient, db: AsyncSession, sheets: list[Any]
) -> None:
    token = await add_token(db, artist="Sigur Rós", title='Ágætis byrjun: "Live"/Remaster')
    r = await viewer.get(f"/catalogue/{token.id}/print.pdf")
    disp = r.headers["content-disposition"]
    assert 'filename="Sigur R_s - _g_tis byrjun_ _Live_Remaster.pdf"' in disp
    assert unquote(disp.split("filename*=UTF-8''", 1)[1]) == (
        "Sigur Rós - Ágætis byrjun_ _Live_Remaster.pdf"
    )


async def test_print_pdf_untitled_and_deleted(
    viewer: httpx.AsyncClient, db: AsyncSession, sheets: list[Any]
) -> None:
    token = await add_token(db, artist=None, title=None)
    r = await viewer.get(f"/catalogue/{token.id}/print.pdf")
    assert 'filename="album.pdf"' in r.headers["content-disposition"]

    gone = await add_token(db, deleted_at=datetime.now(UTC))
    assert (await viewer.get(f"/catalogue/{gone.id}/print.pdf")).status_code == 404


async def test_print_pdf_needs_login(anon: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await add_token(db)
    r = await anon.get(f"/catalogue/{token.id}/print.pdf")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Add pressing: release preview cards (real _fetch_release_as over FakeHttp)
# ---------------------------------------------------------------------------


PREVIEW_RELEASE = {
    "images": [
        {"type": "secondary", "uri": "https://i.discogs.com/b.jpg", "uri150": "https://i.discogs.com/b150.jpg"},
        {"type": "primary", "uri": "https://i.discogs.com/a.jpg", "uri150": "https://i.discogs.com/a150.jpg"},
    ],
    "identifiers": RELEASE["identifiers"],
    "formats": [{"name": "Vinyl", "text": "Gatefold"}],
}  # fmt: skip


async def test_preview_fetches_with_stored_token_and_caches(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: FakeHttp
) -> None:
    await set_discogs_token(db)
    fake_http.add(
        "GET",
        f"{API}/releases/11",
        httpx.Response(429),
        httpx.Response(200, json=PREVIEW_RELEASE),
    )
    r = await contributor.get("/catalogue/new/discogs-preview/11")
    assert r.status_code == 200
    assert 'href="https://i.discogs.com/a.jpg"' in r.text
    assert "SHVL 804 A-1" in r.text
    assert "Gatefold" in r.text
    reqs = fake_http.sent("GET", f"{API}/releases/11")
    assert len(reqs) == 2  # rode out one 429
    assert reqs[0].headers["authorization"] == "Discogs token=dc-secret"

    await contributor.get("/catalogue/new/discogs-preview/11")
    assert len(fake_http.requests) == 2  # cached


async def test_preview_cache_evicts_oldest(
    contributor: httpx.AsyncClient, fake_http: FakeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discogs_details, "_PREVIEW_MAX", 1)
    fake_http.add("GET", f"{API}/releases/1", httpx.Response(200, json={}))
    fake_http.add("GET", f"{API}/releases/2", httpx.Response(200, json={}))
    await contributor.get("/catalogue/new/discogs-preview/1")
    await contributor.get("/catalogue/new/discogs-preview/2")
    assert list(discogs_details._PREVIEW_CACHE) == [2]
    assert "authorization" not in fake_http.requests[0].headers  # no token configured


@pytest.mark.parametrize(
    ("status", "message"),
    [(404, "Discogs returned HTTP 404."), (429, "Discogs rate limit reached")],
)
async def test_preview_http_errors(
    contributor: httpx.AsyncClient, fake_http: FakeHttp, status: int, message: str
) -> None:
    fake_http.add("GET", f"{API}/releases/5", httpx.Response(status))
    r = await contributor.get("/catalogue/new/discogs-preview/5")
    assert r.status_code == 200
    assert message in r.text
    assert 5 not in discogs_details._PREVIEW_CACHE
    assert len(fake_http.requests) == (4 if status == 429 else 1)


async def test_preview_offline(contributor: httpx.AsyncClient, network_guard: NetworkGuard) -> None:
    network_guard.allow_blocked()
    r = await contributor.get("/catalogue/new/discogs-preview/6")
    assert "Could not reach Discogs." in r.text
    assert network_guard.blocked == [f"{API}/releases/6"]


async def test_viewer_cannot_load_previews(viewer: httpx.AsyncClient) -> None:
    assert (await viewer.get("/catalogue/new/discogs-preview/1")).status_code == 403


# ---------------------------------------------------------------------------
# Import banner
# ---------------------------------------------------------------------------


def _request(app: FastAPI) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


async def test_running_discogs_import_reports_progress(app: FastAPI, db: AsyncSession) -> None:
    assert await discogs_details.running_discogs_import(_request(app)) is None
    now = datetime.now(UTC)
    db.add_all(
        [
            CollectionImportJob(discogs_username="done", status=ImportJobStatus.COMPLETED),
            CollectionImportJob(
                discogs_username="stale",
                status=ImportJobStatus.RUNNING,
                updated_at=now - timedelta(minutes=11),
                created_at=now,
            ),
            CollectionImportJob(
                discogs_username="older",
                status=ImportJobStatus.RUNNING,
                created_at=now - timedelta(hours=1),
            ),
            CollectionImportJob(
                discogs_username="me",
                status=ImportJobStatus.RUNNING,
                total_releases=None,
                imported_count=5,
                skipped_count=2,
                failed_count=1,
                created_at=now - timedelta(minutes=1),
            ),
        ]
    )
    await db.commit()
    assert await discogs_details.running_discogs_import(_request(app)) == {
        "username": "me",
        "done": 8,
        "total": 0,
    }


async def test_running_discogs_import_ignores_stale_jobs(app: FastAPI, db: AsyncSession) -> None:
    db.add(
        CollectionImportJob(
            discogs_username="stale",
            status=ImportJobStatus.RUNNING,
            total_releases=10,
            updated_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    await db.commit()
    assert await discogs_details.running_discogs_import(_request(app)) is None


async def test_running_discogs_import_survives_db_errors() -> None:
    def broken() -> Any:
        raise OSError("db down")

    app = SimpleNamespace(state=SimpleNamespace(db_session_factory=broken))
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})
    assert await discogs_details.running_discogs_import(req) is None
