"""Integration tests for the admin hub (``mediacat.web.admin``).

Covers the API Tokens tab, user creation, the admin tabs, housekeeping
(search / soft delete / recover / purge), backup export + import and the
Discogs collection import (with a fake connector: no network).

User edit / role / deactivate / last-admin protection live in
``web/routes.py`` and are tested elsewhere.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.enums import ImageRegion, ImportJobStatus, MediaFormat, TokenStatus, UserRole
from mediacat.db.models import CollectionImportJob, MediaObject, SiteSetting, Token, User
from mediacat.ingestion.base import FetchResult
from mediacat.ingestion.discogs import DiscogsConnector
from mediacat.web import admin, catalogue
from mediacat.web.auth import verify_password

# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_import_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh module-level job registries per test; no politeness sleeps."""
    monkeypatch.setattr(admin, "_stop_flags", {})
    monkeypatch.setattr(admin, "_import_tasks", set())

    class _NoSleepAsyncio:
        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(_delay: float, result: Any = None) -> Any:
            return result

    monkeypatch.setattr(admin, "asyncio", _NoSleepAsyncio())


@pytest.fixture
async def admin_client(
    make_client: Callable[[str], Awaitable[httpx.AsyncClient]],
) -> httpx.AsyncClient:
    """The harness ``admin`` client (renamed: ``admin`` is the module under test here)."""
    return await make_client("admin")


async def _add_token(db: AsyncSession, **kw: Any) -> Token:
    kw.setdefault("media_format", MediaFormat.VINYL)
    kw.setdefault("status", TokenStatus.ACTIVE)
    t = Token(**kw)
    db.add(t)
    await db.commit()
    return t


async def _get_token(db: AsyncSession, token_id: uuid.UUID) -> Token | None:
    stmt = select(Token).where(Token.id == token_id).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _job(db: AsyncSession, job_id: str) -> CollectionImportJob:
    stmt = (
        select(CollectionImportJob)
        .where(CollectionImportJob.id == uuid.UUID(job_id))
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one()


async def _setting(db: AsyncSession, key: str) -> str | None:
    return (
        await db.execute(select(SiteSetting.value).where(SiteSetting.key == key))
    ).scalar_one_or_none()


def _release(rid: str, *, artist: str = "Pink Floyd", title: str | None = None) -> dict[str, Any]:
    """A Discogs release payload as the API returns it."""
    return {
        "id": int(rid),
        "title": title or f"Album {rid}",
        "year": 1979,
        "artists": [{"name": artist}],
        "labels": [{"name": "Harvest", "catno": f"SHVL {rid}"}],
        "formats": [{"name": "Vinyl", "qty": "2"}],
        "identifiers": [
            {"type": "Matrix / Runout", "value": f"SHVL-{rid} A-1", "description": "Side A"},
            {"type": "Barcode", "value": f"50999{rid}"},
        ],
        "country": "Testonia",
        "genres": ["Rock"],
        "styles": ["Prog Rock"],
        "tracklist": [{"position": "A1", "title": "In the Flesh?"}],
        "images": [{"type": "primary", "uri": f"https://i.discogs.test/{rid}.jpg"}],
    }


_FIELD_DEFS = [{"id": 4, "name": "Runout LP1 A", "type": "textarea", "position": 1}]


class FakeDiscogs:
    """Stands in for the DiscogsConnector the import builds (no network).

    *pages* lists the release ids on each collection page.  ``on_release``
    is an optional async hook run at the start of ``fetch_release``.
    """

    def __init__(
        self,
        pages: list[list[str]],
        *,
        identity: str = "collector",
        fail_ids: set[str] | None = None,
        collection_error: str | None = None,
        opened: bool = True,
    ) -> None:
        self.pages = pages
        self.identity = identity
        self.fail_ids = fail_ids or set()
        self.collection_error = collection_error
        self.releases: dict[str, dict[str, Any]] = {
            rid: _release(rid) for page in pages for rid in page
        }
        self.fetched: list[str] = []
        self.collection_pages: list[int] = []
        self.on_release: Callable[[str], Awaitable[None]] | None = None
        self._client: object | None = object() if opened else None
        self.open_calls = 0
        self.close_calls = 0

    async def open(self) -> None:
        self.open_calls += 1
        self._client = object()

    async def close(self) -> None:
        self.close_calls += 1
        self._client = None

    async def fetch_identity(self) -> str:
        return self.identity

    async def fetch_collection_fields(self, _username: str) -> list[dict[str, Any]]:
        return list(_FIELD_DEFS)

    async def fetch_user_collection(
        self, _username: str, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        self.collection_pages.append(page)
        if self.collection_error:
            return {"releases": [], "pages": 0, "items": 0, "error": self.collection_error}
        ids = self.pages[page - 1] if page <= len(self.pages) else []
        return {
            "releases": [
                {
                    "release_id": rid,
                    "instance_id": 9000 + int(rid),
                    "folder_id": 1,
                    "notes": [{"field_id": 4, "value": f"runout {rid}"}],
                }
                for rid in ids
            ],
            "pages": len(self.pages),
            "items": sum(len(p) for p in self.pages),
        }

    async def fetch_release(self, rid: str) -> FetchResult:
        self.fetched.append(rid)
        if self.on_release is not None:
            await self.on_release(rid)
        if rid in self.fail_ids:
            msg = "HTTP 403 Forbidden"
            raise RuntimeError(msg)
        return FetchResult(
            source="discogs", external_id=rid, raw_payload=self.releases[rid], normalised={}
        )


class DiscogsStub:
    """Monkeypatched ``_get_discogs_connector``: hands out *conn*, records tokens."""

    def __init__(self) -> None:
        self.conn = FakeDiscogs([["101"]])
        self.tokens: list[str] = []

    def __call__(self, _request: Any, override_token: str = "") -> FakeDiscogs:
        self.tokens.append(override_token)
        return self.conn


@pytest.fixture
def discogs(monkeypatch: pytest.MonkeyPatch) -> DiscogsStub:
    stub = DiscogsStub()
    monkeypatch.setattr(admin, "_get_discogs_connector", stub)
    return stub


@pytest.fixture
def image_imports(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, list[Any]]]:
    """Stub the Discogs image download (it lives in catalogue.py and uses httpx)."""
    calls: list[tuple[uuid.UUID, list[Any]]] = []

    async def _fake(_store: Any, db: AsyncSession, token_id: uuid.UUID, images: list[Any]) -> int:
        # Like the real helper: adds rows, the caller commits when count > 0.
        calls.append((token_id, images))
        db.add(
            MediaObject(
                token_id=token_id,
                content_hash=uuid.uuid4().hex * 2,
                bucket="media-originals",
                object_key=f"{token_id}.jpg",
                mime_type="image/jpeg",
                size_bytes=1234,
                region=ImageRegion.COVER_FRONT,
                source_url=images[0]["uri"],
            )
        )
        return 1

    monkeypatch.setattr(catalogue, "_import_discogs_images", _fake)
    return calls


@pytest.fixture
def import_tasks(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Coroutine[Any, Any, None]]]:
    """Capture import coroutines instead of scheduling them; tests await them."""
    coros: list[Coroutine[Any, Any, None]] = []
    monkeypatch.setattr(admin, "_start_import_task", lambda coro, name: coros.append(coro))  # noqa: ARG005
    yield coros
    for c in coros:
        c.close()  # never-awaited ones; a no-op for finished coroutines


async def _drain(coros: list[Coroutine[Any, Any, None]]) -> None:
    while coros:
        await coros.pop(0)


async def _start_import(client: httpx.AsyncClient, username: str = "collector", **form: str) -> str:
    r = await client.post("/admin/import/start", data={"discogs_username": username, **form})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/admin?tab=import&job=")
    return loc.rsplit("=", 1)[1]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

_ADMIN_ROUTES: list[tuple[str, str]] = [
    ("GET", "/admin"),
    ("GET", "/admin?tab=import"),
    ("GET", f"/admin/import/status/{uuid.uuid4()}"),
    ("GET", "/admin/import/imported-releases"),
    ("GET", "/admin/backup/export"),
    ("GET", "/admin/housekeeping/search?q=x"),
    ("POST", "/admin/import/start"),
    ("POST", f"/admin/import/{uuid.uuid4()}/pause"),
    ("POST", f"/admin/import/{uuid.uuid4()}/resume"),
    ("POST", "/admin/api-tokens/save"),
    ("POST", "/admin/housekeeping/bulk-delete"),
    ("POST", "/admin/housekeeping/recover"),
    ("POST", "/admin/housekeeping/purge-expired"),
]

_FORMS: dict[str, dict[str, str]] = {
    "/admin/import/start": {"discogs_username": "collector"},
    "/admin/api-tokens/save": {"discogs_token": "stolen-token"},
    "/admin/housekeeping/bulk-delete": {"token_ids": str(uuid.uuid4()), "confirm": "1"},
    "/admin/housekeeping/recover": {"token_id": str(uuid.uuid4())},
}


@pytest.mark.parametrize("role", ["reviewer", "contributor", "viewer"])
async def test_non_admin_roles_are_denied_on_every_admin_route(
    make_client: Callable[[str], Awaitable[httpx.AsyncClient]],
    db: AsyncSession,
    role: str,
) -> None:
    client = await make_client(role)
    for method, url in _ADMIN_ROUTES:
        r = await client.request(method, url, data=_FORMS.get(url.split("?")[0]))
        assert r.status_code == 403, (method, url, r.status_code)
    upload = {"file": ("b.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip")}
    r = await client.post("/admin/backup/import", files=upload)
    assert r.status_code == 403
    # Nothing was written by the denied POSTs.
    assert await _setting(db, "discogs_token") is None
    assert (await db.execute(select(func.count(CollectionImportJob.id)))).scalar_one() == 0


async def test_anonymous_admin_request_redirects_to_login(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/admin?tab=backup")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    r = await anon.post("/admin/users/new", data={"username": "x"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_viewer_cannot_create_an_admin_user(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await viewer.post(
        "/admin/users/new",
        data={
            "username": "escalated",
            "email": "escalated@test.invalid",
            "password": "long-enough-pw",
            "role": "admin",
        },
    )
    created = (
        await db.execute(select(User).where(User.username == "escalated"))
    ).scalar_one_or_none()
    assert created is None
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Admin home tabs
# ---------------------------------------------------------------------------


async def test_users_tab_lists_live_users_only(
    admin_client: httpx.AsyncClient, db: AsyncSession, users: dict[str, User]
) -> None:
    gone = User(
        username="departed-user",
        email="gone@test.invalid",
        password_hash="x",
        role=UserRole.VIEWER,
        deleted_at=datetime.now(UTC),
    )
    db.add(gone)
    await db.commit()

    r = await admin_client.get("/admin")
    assert r.status_code == 200
    body = r.text
    for u in users.values():
        assert f"<td>{u.username}</td>" in body
        assert f"<td>{u.email}</td>" in body
        assert f"/users/{u.id}/edit" in body
    assert "departed-user" not in body
    assert 'href="/admin?tab=users"         class="admin-tab admin-tab--active"' in body


async def test_unknown_tab_falls_back_to_users(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.get("/admin?tab=nonsense")
    assert r.status_code == 200
    assert "admin@test.invalid" in r.text


@pytest.mark.parametrize(
    ("tab", "marker"),
    [
        ("import", 'action="/admin/import/start"'),
        ("backup", "/admin/backup/export"),
        ("docs", "<h2>Documentation</h2>"),
        ("debug", "<h2>Genre diagnostics</h2>"),
        ("layout", "<h2>Background</h2>"),
        ("housekeeping", "/admin/housekeeping/search"),
        ("api-tokens", 'action="/admin/api-tokens/save"'),
    ],
)
async def test_each_tab_renders(admin_client: httpx.AsyncClient, tab: str, marker: str) -> None:
    r = await admin_client.get(f"/admin?tab={tab}")
    assert r.status_code == 200
    assert marker in r.text
    assert re.search(rf'href="/admin\?tab={tab}"\s+class="admin-tab admin-tab--active"', r.text)
    # Only the users tab shows the user table.
    assert "admin@test.invalid" not in r.text


async def test_import_tab_marks_zombie_jobs_failed_and_counts_imports(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    zombie = CollectionImportJob(discogs_username="ghost", status=ImportJobStatus.RUNNING)
    live = CollectionImportJob(discogs_username="alive", status=ImportJobStatus.RUNNING)
    done = CollectionImportJob(discogs_username="finished", status=ImportJobStatus.COMPLETED)
    db.add_all([zombie, live, done])
    await db.commit()
    admin._stop_flags[str(live.id)] = asyncio.Event()  # a task owns this one
    for i in range(3):
        await _add_token(db, title=f"T{i}", discogs_release_id=500 + i)
    await _add_token(db, title="deleted", discogs_release_id=600, deleted_at=datetime.now(UTC))
    await _add_token(db, title="manual")

    r = await admin_client.get("/admin?tab=import")
    assert r.status_code == 200

    assert (await _job(db, str(zombie.id))).status == ImportJobStatus.FAILED
    assert "app was restarted" in ((await _job(db, str(zombie.id))).error_message or "")
    assert (await _job(db, str(live.id))).status == ImportJobStatus.RUNNING
    assert (await _job(db, str(done.id))).status == ImportJobStatus.COMPLETED
    for name in ("ghost", "alive", "finished"):
        assert f'<span class="import-job-user">{name}</span>' in r.text
    assert "3 pressings" in r.text


async def test_housekeeping_tab_lists_recently_deleted_only(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    now = datetime.now(UTC)
    await _add_token(
        db, artist="Recent", title="Deleted Recently", deleted_at=now - timedelta(days=2)
    )
    await _add_token(
        db, artist="Old", title="Deleted Long Ago", deleted_at=now - timedelta(days=45)
    )
    await _add_token(db, artist="Alive", title="Still Here")

    r = await admin_client.get("/admin?tab=housekeeping")
    assert r.status_code == 200
    assert "Deleted Recently" in r.text
    assert "27d" in r.text  # expires in 30 - 2 days (rounded down)
    assert "Deleted Long Ago" not in r.text
    assert "Still Here" not in r.text


# ---------------------------------------------------------------------------
# User creation
# ---------------------------------------------------------------------------


async def test_admin_creates_user_with_hashed_password(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin_client.post(
        "/admin/users/new",
        data={
            "username": "newbie",
            "email": "newbie@test.invalid",
            "password": "correct horse",
            "role": "reviewer",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?tab=users&msg=user_created"
    u = (await db.execute(select(User).where(User.username == "newbie"))).scalar_one()
    assert u.email == "newbie@test.invalid"
    assert u.role == UserRole.REVIEWER
    assert u.is_active is True
    assert u.session_timeout_seconds == 86400
    assert u.password_hash != "correct horse"
    assert verify_password("correct horse", u.password_hash)

    page = await admin_client.get(r.headers["location"])
    assert "<td>newbie</td>" in page.text


async def test_create_user_with_unknown_role_becomes_viewer(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin_client.post(
        "/admin/users/new",
        data={
            "username": "sneaky",
            "email": "s@test.invalid",
            "password": "12345678",
            "role": "root",
        },
    )
    assert r.headers["location"].endswith("msg=user_created")
    u = (await db.execute(select(User).where(User.username == "sneaky"))).scalar_one()
    assert u.role == UserRole.VIEWER


@pytest.mark.parametrize(
    ("form", "error"),
    [
        (
            {"username": "shorty", "email": "short@test.invalid", "password": "1234567"},
            "password_short",
        ),
        (
            {"username": "admin-user", "email": "fresh@test.invalid", "password": "12345678"},
            "username_taken",
        ),
        (
            {"username": "fresh", "email": "viewer@test.invalid", "password": "12345678"},
            "email_taken",
        ),
    ],
)
async def test_create_user_rejections(
    admin_client: httpx.AsyncClient, db: AsyncSession, form: dict[str, str], error: str
) -> None:
    before = (await db.execute(select(func.count(User.id)))).scalar_one()
    r = await admin_client.post("/admin/users/new", data=form)
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin?tab=users&error={error}"
    assert (await db.execute(select(func.count(User.id)))).scalar_one() == before
    page = await admin_client.get(r.headers["location"])
    assert "alert-danger" in page.text


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------


async def test_api_tokens_save_stores_values(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin_client.post(
        "/admin/api-tokens/save",
        data={
            "discogs_token": "  dg-secret-ABCD  ",
            "spotify_client_id": "sp-client-id",
            "not_a_known_key": "ignored",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?tab=api-tokens&msg=saved"
    assert await _setting(db, "discogs_token") == "dg-secret-ABCD"
    assert await _setting(db, "spotify_client_id") == "sp-client-id"
    assert await _setting(db, "not_a_known_key") is None


async def test_api_tokens_blank_keeps_and_space_clears(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await admin_client.post(
        "/admin/api-tokens/save",
        data={"discogs_token": "original-token", "spotify_client_secret": "sp-secret"},
    )
    await admin_client.post(
        "/admin/api-tokens/save",
        data={"discogs_token": "", "spotify_client_secret": " ", "musicbrainz_user_agent": "MC/1"},
    )
    assert await _setting(db, "discogs_token") == "original-token"
    assert await _setting(db, "spotify_client_secret") == ""
    assert await _setting(db, "musicbrainz_user_agent") == "MC/1"

    # Overwrite is an upsert, not a duplicate row.
    await admin_client.post("/admin/api-tokens/save", data={"discogs_token": "rotated"})
    assert await _setting(db, "discogs_token") == "rotated"
    n = (
        await db.execute(select(func.count()).where(SiteSetting.key == "discogs_token"))
    ).scalar_one()
    assert n == 1


async def test_api_tokens_save_with_nothing_to_change(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin_client.post("/admin/api-tokens/save", data={"discogs_token": ""})
    assert r.headers["location"] == "/admin?tab=api-tokens&msg=saved"
    assert (await db.execute(select(func.count()).select_from(SiteSetting))).scalar_one() == 0


async def test_api_tokens_tab_masks_secrets(admin_client: httpx.AsyncClient) -> None:
    await admin_client.post(
        "/admin/api-tokens/save",
        data={
            "discogs_token": "supersecret-WXYZ",
            "spotify_client_secret": "abc",
            "spotify_client_id": "visible-client-id",
        },
    )
    r = await admin_client.get("/admin?tab=api-tokens")
    assert r.status_code == 200
    assert "supersecret" not in r.text
    assert "•" * 12 + "WXYZ" in r.text
    assert 'placeholder="•••"' in r.text  # short secret fully masked
    assert "visible-client-id" in r.text  # not a secret
    assert 'placeholder="Not set"' in r.text  # MusicBrainz UA


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


async def test_housekeeping_search_filters(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    a = await _add_token(db, artist="Miles Davis", title="Kind of Blue", catalog_number="CL 1355")
    b = await _add_token(db, artist="Davis Group", title="<b>Bold</b>", media_format=MediaFormat.CD)
    await _add_token(db, artist="Miles Davis", title="Gone", deleted_at=datetime.now(UTC))
    await _add_token(db, artist="Miles Davis", title="Dupe", status=TokenStatus.MERGED)
    await _add_token(db, artist="Coltrane", title="Blue Train")

    r = await admin_client.get("/admin/housekeeping/search", params={"q": "davis"})
    assert r.status_code == 200
    assert "2 pressing(s) found" in r.text
    assert f'value="{a.id}"' in r.text
    assert f'value="{b.id}"' in r.text
    assert "Gone" not in r.text
    assert "Dupe" not in r.text
    assert "Coltrane" not in r.text
    assert "&lt;b&gt;Bold&lt;/b&gt;" in r.text
    assert "<b>Bold</b>" not in r.text
    assert "CL 1355" in r.text

    r = await admin_client.get("/admin/housekeeping/search", params={"q": "davis", "fmt": "cd"})
    assert "1 pressing(s) found" in r.text
    assert f'value="{b.id}"' in r.text

    r = await admin_client.get("/admin/housekeeping/search", params={"q": "blue"})
    assert "2 pressing(s) found" in r.text  # matches titles too

    r = await admin_client.get("/admin/housekeeping/search")
    assert "3 pressing(s) found" in r.text

    r = await admin_client.get("/admin/housekeeping/search", params={"q": "nobody"})
    assert "No pressings found." in r.text


async def test_bulk_delete_without_selection(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.post("/admin/housekeeping/bulk-delete", data={"confirm": "1"})
    assert r.headers["location"] == "/admin?tab=housekeeping&msg=no_selection"


async def test_bulk_delete_asks_for_confirmation_first(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await _add_token(db, artist="Kraftwerk", title="Autobahn", year=1974)
    r = await admin_client.post("/admin/housekeeping/bulk-delete", data={"token_ids": [str(t.id)]})
    assert r.status_code == 200
    assert "delete 1 pressing(s)?" in r.text
    assert "Kraftwerk — Autobahn (1974, vinyl)" in r.text
    assert f'<input type="hidden" name="token_ids" value="{t.id}">' in r.text
    still = await _get_token(db, t.id)
    assert still is not None
    assert still.deleted_at is None


async def test_bulk_delete_soft_deletes_and_recover_restores(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    t1 = await _add_token(db, artist="A", title="One")
    t2 = await _add_token(db, artist="B", title="Two")
    keep = await _add_token(db, artist="C", title="Three")
    already = await _add_token(db, artist="D", title="Four", deleted_at=datetime.now(UTC))

    r = await admin_client.post(
        "/admin/housekeeping/bulk-delete",
        data={"token_ids": [str(t1.id), str(t2.id), str(already.id)], "confirm": "1"},
    )
    assert r.headers["location"] == "/admin?tab=housekeeping&msg=deleted&count=2"
    for tid in (t1.id, t2.id):
        tok = await _get_token(db, tid)
        assert tok is not None  # soft delete: row kept
        assert tok.deleted_at is not None
        assert tok.status == TokenStatus.ARCHIVED
    kept = await _get_token(db, keep.id)
    assert kept is not None
    assert kept.deleted_at is None

    page = await admin_client.get("/admin?tab=housekeeping")
    assert "One" in page.text
    assert "Two" in page.text

    r = await admin_client.post("/admin/housekeeping/recover", data={"token_id": str(t1.id)})
    assert r.headers["location"] == "/admin?tab=housekeeping&msg=recovered"
    back = await _get_token(db, t1.id)
    assert back is not None
    assert back.deleted_at is None
    assert back.status == TokenStatus.ACTIVE
    still = await _get_token(db, t2.id)
    assert still is not None
    assert still.deleted_at is not None


async def test_recover_with_invalid_id_reports_failure(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.post("/admin/housekeeping/recover", data={"token_id": "not-a-uuid"})
    assert r.headers["location"] == "/admin?tab=housekeeping&error=recover_failed"


async def test_purge_hard_deletes_only_expired(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    now = datetime.now(UTC)
    expired = await _add_token(db, title="expired", deleted_at=now - timedelta(days=31))
    ancient = await _add_token(db, title="ancient", deleted_at=now - timedelta(days=400))
    recent = await _add_token(db, title="recent", deleted_at=now - timedelta(days=29))
    alive = await _add_token(db, title="alive")
    db.add(
        MediaObject(
            token_id=expired.id,
            content_hash="a" * 64,
            bucket="media-originals",
            object_key="aa/aa/x.png",
            mime_type="image/png",
            size_bytes=10,
            region=ImageRegion.COVER_FRONT,
        )
    )
    await db.commit()

    r = await admin_client.post("/admin/housekeeping/purge-expired")
    assert r.headers["location"] == "/admin?tab=housekeeping&msg=purged&count=2"
    assert await _get_token(db, expired.id) is None
    assert await _get_token(db, ancient.id) is None
    assert await _get_token(db, recent.id) is not None
    assert await _get_token(db, alive.id) is not None
    n_media = (await db.execute(select(func.count(MediaObject.id)))).scalar_one()
    assert n_media == 0  # children cascade with the token

    r = await admin_client.post("/admin/housekeeping/purge-expired")
    assert r.headers["location"].endswith("msg=purged&count=0")


# ---------------------------------------------------------------------------
# Backup & restore
# ---------------------------------------------------------------------------


async def _export(client: httpx.AsyncClient) -> bytes:
    r = await client.get("/admin/backup/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith('attachment; filename="mediacat_backup_')
    return r.content


async def test_backup_export_contains_manifest_and_tables(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await _add_token(db, artist="Can", title="Tago Mago", year=1971, genres=["Krautrock"])
    zf = zipfile.ZipFile(io.BytesIO(await _export(admin_client)))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["mediacat_backup"] is True
    assert manifest["tables"] == admin._BACKUP_TABLES
    assert set(zf.namelist()) == {"manifest.json", *(f"{n}.json" for n in admin._BACKUP_TABLES)}
    tokens = json.loads(zf.read("tokens.json"))
    assert [(r["id"], r["title"], r["year"], r["genres"]) for r in tokens] == [
        (str(t.id), "Tago Mago", 1971, ["Krautrock"])
    ]
    assert len(json.loads(zf.read("users.json"))) == 4
    assert len(json.loads(zf.read("countries.json"))) > 50  # migration seed


async def test_backup_round_trip_restores_deleted_rows(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await _add_token(
        db,
        artist="Neu!",
        title="Neu! 75",
        year=1975,
        catalog_number="1C 062-29 571",
        tracklist=[{"position": "A1", "title": "Isi"}],
        extra={"discogs": {"identifiers": []}},
        created_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
    )
    archive = await _export(admin_client)

    await db.delete(t)
    await db.commit()
    assert await _get_token(db, t.id) is None

    r = await admin_client.post(
        "/admin/backup/import", files={"file": ("backup.zip", archive, "application/zip")}
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin?tab=backup&msg=import_done&rows=")

    restored = await _get_token(db, t.id)
    assert restored is not None
    assert (restored.artist, restored.title, restored.year, restored.catalog_number) == (
        "Neu!",
        "Neu! 75",
        1975,
        "1C 062-29 571",
    )
    assert restored.tracklist == [{"position": "A1", "title": "Isi"}]
    assert restored.extra == {"discogs": {"identifiers": []}}
    assert restored.created_at == datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    assert restored.media_format == MediaFormat.VINYL
    # Only the one missing row was inserted; everything else already existed.
    assert r.headers["location"].endswith("rows=1")


async def test_backup_import_inserts_missing_rows_and_keeps_existing_ones(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Hand-made records without timestamp columns (see the round-trip xfail).
    existing = await _add_token(db, title="Edited locally")
    new_id = uuid.uuid4()
    archive = _zip(
        {
            "manifest.json": {"mediacat_backup": True},
            "tokens.json": [
                {"id": str(existing.id), "media_format": "vinyl", "status": "active",
                 "title": "Stale backup title", "year": None, "discogs_release_id": None},
                {"id": str(new_id), "media_format": "cd", "status": "active",
                 "title": "Restored", "year": 1999, "discogs_release_id": 77},
            ],
        }
    )  # fmt: skip
    r = await admin_client.post(
        "/admin/backup/import", files={"file": ("b.zip", archive, "application/zip")}
    )
    # The "rows" figure counts the skipped conflicting row too (it is attempts, not inserts).
    assert r.headers["location"].startswith("/admin?tab=backup&msg=import_done&rows=")
    tok = await _get_token(db, existing.id)
    assert tok is not None
    assert tok.title == "Edited locally"  # ON CONFLICT DO NOTHING
    restored = await _get_token(db, new_id)
    assert restored is not None
    assert (restored.title, restored.year, restored.media_format) == (
        "Restored",
        1999,
        MediaFormat.CD,
    )
    assert restored.discogs_release_id == 77
    assert (await db.execute(select(func.count(Token.id)))).scalar_one() == 2


def _zip(entries: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content if isinstance(content, str) else json.dumps(content))
    return buf.getvalue()


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        (b"definitely not a zip", "/admin?tab=backup&error=invalid_zip"),
        (_zip({"tokens.json": []}), "/admin?tab=backup&error=missing_manifest"),
        (_zip({"manifest.json": {"other": True}}), "/admin?tab=backup&error=invalid_manifest"),
    ],
)
async def test_backup_import_rejects_bad_archives(
    admin_client: httpx.AsyncClient, payload: bytes, location: str
) -> None:
    r = await admin_client.post(
        "/admin/backup/import", files={"file": ("b.zip", payload, "application/zip")}
    )
    assert r.status_code == 303
    assert r.headers["location"] == location
    page = await admin_client.get(location)
    assert "alert-danger" in page.text


async def test_backup_import_skips_tables_with_unsafe_column_names(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    hostile = [{"id": str(uuid.uuid4()), "title) VALUES (1); DROP TABLE tokens; --": "x"}]
    good_id = uuid.uuid4()
    archive = _zip(
        {
            "manifest.json": {"mediacat_backup": True},
            "tokens.json": hostile,
            "labels.json": [],
            "manufacturers.json": [
                {
                    "id": str(good_id),
                    "name": "EMI Electrola",
                    "name_normalised": "emi electrola",
                    "is_confirmed": False,
                }
            ],
        }
    )
    r = await admin_client.post(
        "/admin/backup/import", files={"file": ("b.zip", archive, "application/zip")}
    )
    assert r.headers["location"].endswith("rows=1")
    from mediacat.db.models import Manufacturer

    m = (await db.execute(select(Manufacturer).where(Manufacturer.id == good_id))).scalar_one()
    assert m.name == "EMI Electrola"
    assert (await db.execute(select(func.count(Token.id)))).scalar_one() == 0


# ---------------------------------------------------------------------------
# Collection import
# ---------------------------------------------------------------------------


async def test_import_start_rejects_empty_username(
    admin_client: httpx.AsyncClient, db: AsyncSession, discogs: DiscogsStub
) -> None:
    r = await admin_client.post(
        "/admin/import/start", data={"discogs_username": "https://www.discogs.com/user/"}
    )
    assert r.headers["location"] == "/admin?tab=import&error=no_username"
    assert (await db.execute(select(func.count(CollectionImportJob.id)))).scalar_one() == 0
    assert discogs.tokens == []


async def test_full_import_creates_tokens_with_extras(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    discogs.conn = FakeDiscogs([["101", "102"], ["103"]])
    job_id = await _start_import(
        admin_client, "https://www.discogs.com/user/collector/", discogs_token=" tok-123 "
    )
    assert discogs.tokens == ["tok-123"]
    job = await _job(db, job_id)
    assert job.discogs_username == "collector"
    assert job.status == ImportJobStatus.PENDING
    assert job_id in admin._stop_flags

    await _drain(import_tasks)

    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.COMPLETED
    assert (job.total_releases, job.imported_count, job.skipped_count, job.failed_count) == (
        3,
        3,
        0,
        0,
    )
    assert job.last_page == 2
    assert job.error_message is None
    assert discogs.conn.collection_pages == [1, 2]
    assert job_id not in admin._stop_flags

    tokens = (await db.execute(select(Token).order_by(Token.discogs_release_id))).scalars().all()
    assert [t.discogs_release_id for t in tokens] == [101, 102, 103]
    t = tokens[0]
    assert (t.artist, t.title, t.year, t.media_format, t.status) == (
        "Pink Floyd", "Album 101", 1979, MediaFormat.VINYL, TokenStatus.ACTIVE,
    )  # fmt: skip
    assert t.catalog_number == "SHVL 101"
    assert t.matrix_runout == "SHVL-101 A-1"
    assert t.barcode == "50999101"
    assert t.tracklist == [{"position": "A1", "title": "In the Flesh?"}]
    assert t.country_id is not None
    assert tokens[1].country_id == t.country_id  # country created once, then reused
    assert t.extra is not None
    assert t.extra["discogs"]["identifiers"]
    coll = t.extra["discogs_collection"]
    assert coll["username"] == "collector"
    assert coll["instance_id"] == 9101
    assert coll["values"] == {"4": "runout 101"}
    assert sorted(tid for tid, _ in image_imports) == sorted(x.id for x in tokens)
    assert image_imports[0][1][0]["type"] == "primary"
    media = (await db.execute(select(MediaObject.token_id, MediaObject.source_url))).all()
    assert sorted(str(m.token_id) for m in media) == sorted(str(x.id) for x in tokens)
    assert "https://i.discogs.test/101.jpg" in {m.source_url for m in media}

    status = (await admin_client.get(f"/admin/import/status/{job_id}")).json()
    assert status["status"] == "completed"
    assert (status["imported_count"], status["total_releases"], status["last_page"]) == (3, 3, 2)

    page = await admin_client.get(f"/admin?tab=import&job={job_id}")
    assert "3 / 3" in page.text
    assert "import-job-card--active" in page.text


async def test_image_failure_does_not_fail_the_release(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
) -> None:
    async def _broken(*_a: Any) -> int:
        msg = "CDN down"
        raise RuntimeError(msg)

    monkeypatch.setattr(catalogue, "_import_discogs_images", _broken)
    discogs.conn = FakeDiscogs([["101"]])
    job_id = await _start_import(admin_client)
    await _drain(import_tasks)
    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.COMPLETED
    assert (job.imported_count, job.failed_count) == (1, 0)
    assert (await db.execute(select(Token.discogs_release_id))).scalar_one() == 101


async def test_import_without_identity_match_skips_collection_fields(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    discogs.conn = FakeDiscogs([["101"]], identity="someone-else")
    await _start_import(admin_client, "collector")
    await _drain(import_tasks)
    t = (await db.execute(select(Token))).scalar_one()
    assert t.extra is not None
    assert "discogs" in t.extra
    assert "discogs_collection" not in t.extra


async def test_import_uses_stored_token_then_app_connector_token(
    admin_client: httpx.AsyncClient,
    app: Any,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
) -> None:
    class _AppConnector:
        _auth_header = "Discogs token=from-app-config"

    app.state.connectors = {"discogs": _AppConnector()}
    await _start_import(admin_client)
    await admin_client.post("/admin/api-tokens/save", data={"discogs_token": "from-db"})
    await _start_import(admin_client)
    await _start_import(admin_client, discogs_token="from-form")
    assert discogs.tokens == ["from-app-config", "from-db", "from-form"]


async def test_import_skips_existing_and_backfills_identifiers(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    old = await _add_token(db, artist="Mine", title="Keep me", discogs_release_id=101)
    discogs.conn = FakeDiscogs([["101", "102"]])
    job_id = await _start_import(admin_client)
    await _drain(import_tasks)

    job = await _job(db, job_id)
    assert (job.imported_count, job.skipped_count) == (1, 1)
    tok = await _get_token(db, old.id)
    assert tok is not None
    assert (tok.artist, tok.title) == ("Mine", "Keep me")  # not overwritten
    assert tok.extra is not None
    assert tok.extra["discogs"]["identifiers"]  # backfilled from the release
    assert tok.extra["discogs_collection"]["values"] == {"4": "runout 101"}
    assert discogs.conn.fetched == ["101", "102"]
    n = (await db.execute(select(func.count(Token.id)))).scalar_one()
    assert n == 2

    # Second run: identifiers present, so no release fetch for skipped ones.
    discogs.conn = FakeDiscogs([["101", "102"]])
    job2 = await _start_import(admin_client)
    await _drain(import_tasks)
    j2 = await _job(db, job2)
    assert (j2.imported_count, j2.skipped_count) == (0, 2)
    assert discogs.conn.fetched == []


async def test_reimport_updates_enrichment_but_not_identity_fields(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    old = await _add_token(
        db, artist="My Artist", title="My Title", year=2001, discogs_release_id=101
    )
    discogs.conn = FakeDiscogs([["101"]])
    job_id = await _start_import(admin_client, reimport="1")
    await _drain(import_tasks)

    job = await _job(db, job_id)
    assert (job.imported_count, job.skipped_count) == (1, 0)
    tok = await _get_token(db, old.id)
    assert tok is not None
    assert (tok.artist, tok.title, tok.year) == ("My Artist", "My Title", 2001)
    assert tok.catalog_number == "SHVL 101"
    assert tok.barcode == "50999101"
    assert tok.country_id is not None  # filled because it was empty
    assert (await db.execute(select(func.count(Token.id)))).scalar_one() == 1


async def test_private_collection_fails_with_hint(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
) -> None:
    discogs.conn = FakeDiscogs([], collection_error="HTTP 403 Forbidden")
    job_id = await _start_import(admin_client)
    await _drain(import_tasks)
    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.FAILED
    assert job.error_message is not None
    assert job.error_message.startswith("HTTP 403 Forbidden")
    assert "may be private" in job.error_message


async def test_mid_import_page_failure_keeps_progress(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    discogs.conn = FakeDiscogs([["101"], []])  # page 2 comes back empty
    job_id = await _start_import(admin_client)
    await _drain(import_tasks)
    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.FAILED
    assert job.error_message == "Page 2 failed: No releases returned on page 2"
    assert (job.imported_count, job.last_page) == (1, 2)


async def test_some_failed_releases_are_listed_on_completion(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    discogs.conn = FakeDiscogs([["101", "102", "0", "103"]], fail_ids={"102"})
    job_id = await _start_import(admin_client)
    await _drain(import_tasks)
    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.COMPLETED
    assert (job.imported_count, job.failed_count) == (2, 2)  # "0" is invalid, 102 raised
    assert job.error_message == "Failed release IDs (re-run import to retry): 102"


async def test_consecutive_failures_abort_the_import(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
) -> None:
    ids = [str(200 + i) for i in range(10)]
    discogs.conn = FakeDiscogs([ids], fail_ids=set(ids))
    job_id = await _start_import(admin_client)
    await _drain(import_tasks)
    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.FAILED
    assert job.failed_count == 8
    assert job.error_message is not None
    assert job.error_message.startswith("Aborted after 8 consecutive failures.")
    assert "provide a Discogs personal access token" in job.error_message
    assert "Failed IDs: 200, 201" in job.error_message
    assert len(discogs.conn.fetched) == 8


async def test_import_crash_marks_job_failed(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        msg = "collection endpoint exploded"
        raise RuntimeError(msg)

    discogs.conn.fetch_user_collection = _boom  # type: ignore[method-assign]
    job_id = await _start_import(admin_client)
    await _drain(import_tasks)
    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.FAILED
    assert job.error_message == "collection endpoint exploded"
    assert job_id not in admin._stop_flags


async def test_import_opens_and_closes_an_unopened_connector(
    admin_client: httpx.AsyncClient,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    discogs.conn = FakeDiscogs([["101"]], opened=False)
    await _start_import(admin_client)
    await _drain(import_tasks)
    assert (discogs.conn.open_calls, discogs.conn.close_calls) == (1, 1)


async def test_import_runs_as_background_task(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    """The real _start_import_task: the task is referenced until it finishes."""
    discogs.conn = FakeDiscogs([["101"]])
    job_id = await _start_import(admin_client)
    tasks = list(admin._import_tasks)
    assert len(tasks) == 1
    assert tasks[0].get_name() == f"collection-import-{job_id[:8]}"
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    await asyncio.sleep(0)  # let the done-callback run
    assert admin._import_tasks == set()
    assert (await _job(db, job_id)).status == ImportJobStatus.COMPLETED


async def test_pause_and_resume_finish_without_duplicates(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    conn = FakeDiscogs([["101", "102", "103"]])
    discogs.conn = conn
    job_id = await _start_import(admin_client)

    async def _pause_on_102(rid: str) -> None:
        if rid == "102":
            r = await admin_client.post(f"/admin/import/{job_id}/pause")
            assert r.headers["location"] == f"/admin?tab=import&job={job_id}"

    conn.on_release = _pause_on_102
    await _drain(import_tasks)

    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.PAUSED
    assert job.imported_count == 2  # 102 was in flight and finished
    assert conn.fetched == ["101", "102"]
    assert job_id not in admin._stop_flags

    conn.on_release = None
    r = await admin_client.post(f"/admin/import/{job_id}/resume", data={"discogs_token": "t2"})
    assert r.headers["location"] == f"/admin?tab=import&job={job_id}"
    assert discogs.tokens[-1] == "t2"
    assert (await _job(db, job_id)).status == ImportJobStatus.PENDING
    await _drain(import_tasks)

    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.COMPLETED
    ids = (
        (await db.execute(select(Token.discogs_release_id).order_by(Token.discogs_release_id)))
        .scalars()
        .all()
    )
    assert ids == [101, 102, 103]


async def test_resumed_import_progress_does_not_exceed_total(
    admin_client: httpx.AsyncClient,
    db: AsyncSession,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
    image_imports: list[tuple[uuid.UUID, list[Any]]],
) -> None:
    conn = FakeDiscogs([["101", "102", "103"]])
    discogs.conn = conn
    job_id = await _start_import(admin_client)

    async def _pause_on_102(rid: str) -> None:
        if rid == "102":
            await admin_client.post(f"/admin/import/{job_id}/pause")

    conn.on_release = _pause_on_102
    await _drain(import_tasks)
    conn.on_release = None
    await admin_client.post(f"/admin/import/{job_id}/resume")
    await _drain(import_tasks)

    job = await _job(db, job_id)
    assert job.status == ImportJobStatus.COMPLETED
    done = job.imported_count + job.skipped_count + job.failed_count
    assert done == job.total_releases == 3
    assert (job.imported_count, job.skipped_count) == (3, 0)


async def test_pause_without_running_task_still_marks_paused(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    job = CollectionImportJob(discogs_username="idle", status=ImportJobStatus.FAILED)
    db.add(job)
    await db.commit()
    r = await admin_client.post(f"/admin/import/{job.id}/pause")
    assert r.status_code == 303
    assert (await _job(db, str(job.id))).status == ImportJobStatus.PAUSED


async def test_resume_unknown_job_redirects_without_starting(
    admin_client: httpx.AsyncClient,
    discogs: DiscogsStub,
    import_tasks: list[Coroutine[Any, Any, None]],
) -> None:
    r = await admin_client.post(f"/admin/import/{uuid.uuid4()}/resume")
    assert r.headers["location"] == "/admin?tab=import"
    assert import_tasks == []
    assert discogs.tokens == []


async def test_import_status_unknown_job(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.get(f"/admin/import/status/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json() == {"error": "not found"}


def test_real_connector_factory_uses_override_or_app_token() -> None:
    app_conn = SimpleNamespace(_auth_header="Discogs token=app-tok")
    state = SimpleNamespace(connectors={"discogs": app_conn})
    req: Any = SimpleNamespace(app=SimpleNamespace(state=state))
    conn = admin._get_discogs_connector(req, "override")
    assert isinstance(conn, DiscogsConnector)
    assert conn._auth_header == "Discogs token=override"
    assert conn._rate_limiter._rate == 40
    assert admin._get_discogs_connector(req)._auth_header == "Discogs token=app-tok"

    state.connectors = {}
    anon_conn = admin._get_discogs_connector(req)
    assert anon_conn._auth_header is None
    assert anon_conn._rate_limiter._rate == 20


# ---------------------------------------------------------------------------
# Imported releases partial
# ---------------------------------------------------------------------------


async def test_imported_releases_lists_discogs_tokens(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin_client.get("/admin/import/imported-releases")
    assert "No Discogs-imported pressings found." in r.text

    t = await _add_token(db, artist="Faust", title="IV", year=1973, discogs_release_id=4242)
    await _add_token(
        db, artist="Gone", title="Deleted", discogs_release_id=1, deleted_at=datetime.now(UTC)
    )
    await _add_token(db, artist="Manual", title="No Discogs")

    r = await admin_client.get("/admin/import/imported-releases")
    assert r.status_code == 200
    assert "https://www.discogs.com/release/4242" in r.text
    assert f'href="/catalogue/{t.id}"' in r.text
    assert "<td>Faust</td>" in r.text
    assert "<td>1973</td>" in r.text
    assert "<td>vinyl</td>" in r.text
    assert "Deleted" not in r.text
    assert "No Discogs" not in r.text
    assert "Load more" not in r.text


async def test_imported_releases_paginates(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    db.add_all(
        Token(
            media_format=MediaFormat.CD,
            artist=f"Artist {i:03d}",
            title="T",
            discogs_release_id=i + 1,
        )
        for i in range(101)
    )
    await db.commit()
    r = await admin_client.get("/admin/import/imported-releases")
    assert r.text.count("<tr><td><code>") == 100
    assert 'hx-get="/admin/import/imported-releases?page=2"' in r.text
    r2 = await admin_client.get("/admin/import/imported-releases?page=2")
    assert r2.text.count("<tr><td><code>") == 1
    assert "Artist 100" in r2.text
    assert "Load more" not in r2.text


async def test_imported_releases_escapes_html(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await _add_token(
        db, artist='<img src=x onerror="alert(1)">', title="<i>t</i>", discogs_release_id=7
    )
    r = await admin_client.get("/admin/import/imported-releases")
    assert "<img src=x" not in r.text
    assert "&lt;img src=x" in r.text


async def test_backup_full_round_trip_restores_every_table_exactly(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """Export → wipe every backed-up table → import gives back identical rows."""
    from decimal import Decimal

    from sqlalchemy import text

    from mediacat.db.models import TokenPriceHistory

    from .fakes import png_bytes

    r = await admin_client.post(
        "/catalogue/new",
        data={"artist": "Can", "title": "Ege Bamyasi", "year": "1972", "country_name": "Germany"},
    )
    tid = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
    await admin_client.post(
        f"/catalogue/{tid}/images", files={"file": ("c.png", png_bytes(), "image/png")}
    )
    await admin_client.post(f"/catalogue/{tid}/flag-review")
    db.add(
        TokenPriceHistory(
            token_id=tid,
            source="discogs_marketplace_stats",
            fetched_at=datetime(2025, 1, 2, 3, 4, 5, 678901, tzinfo=UTC),
            lowest_price=Decimal("12.50"),
            num_for_sale=3,
            currency="EUR",
            condition_prices={"_median": 20.0},
        )
    )
    await db.commit()

    async def snapshot() -> dict[str, list[dict[str, Any]]]:
        out = {}
        for table in admin._BACKUP_TABLES:
            rows = (await db.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).mappings()  # noqa: S608
            out[table] = [dict(r) for r in rows]
        return out

    before = await snapshot()
    token_row = before["tokens"][0]
    assert token_row["current_revision_id"] is not None  # the revision pointer has to survive
    assert all(
        before[t]
        for t in (
            "users",
            "tokens",
            "token_revisions",
            "media_objects",
            "review_items",
            "token_price_history",
        )
    )

    archive = await _export(admin_client)
    for table in reversed(admin._BACKUP_TABLES):
        await db.execute(text(f"DELETE FROM {table}"))  # noqa: S608
    await db.commit()

    r = await admin_client.post(
        "/admin/backup/import", files={"file": ("backup.zip", archive, "application/zip")}
    )
    total = sum(len(rows) for rows in before.values())
    assert r.headers["location"] == f"/admin?tab=backup&msg=import_done&rows={total}"
    assert await snapshot() == before


async def test_import_status_of_malformed_id_is_404_without_details(
    admin_client: httpx.AsyncClient,
) -> None:
    r = await admin_client.get("/admin/import/status/not-a-uuid")
    assert r.status_code == 404
    assert r.json() == {"error": "not found"}


async def test_bulk_delete_ignores_malformed_ids(
    admin_client: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await _add_token(db, artist="Kept")
    r = await admin_client.post(
        "/admin/housekeeping/bulk-delete", data={"token_ids": ['"><script>x</script>']}
    )
    assert r.headers["location"] == "/admin?tab=housekeeping&msg=no_selection"
    r = await admin_client.post(
        "/admin/housekeeping/bulk-delete", data={"token_ids": [str(t.id), "<b>"]}
    )
    assert r.status_code == 200
    assert "<b>" not in r.text
    assert f'value="{t.id}"' in r.text
    restored = await _get_token(db, t.id)
    assert restored is not None
    assert restored.deleted_at is None  # first step only asks for confirmation
