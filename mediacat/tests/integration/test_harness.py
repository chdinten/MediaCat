"""The harness itself: isolation, guards, fakes, middleware wiring."""

from __future__ import annotations

import inspect
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.enums import MediaFormat
from mediacat.db.models import Country, Token
from mediacat.storage.object_store import ObjectStore, ObjectStoreError

from .conftest import TEST_DB_NAME, IntegrationDb, assert_test_db
from .fakes import FakeObjectStore, NetworkGuard, png_bytes


async def test_connected_to_test_db_as_app_role(
    db: AsyncSession, test_database: IntegrationDb
) -> None:
    expected_user = urlsplit(test_database.app_dsn).username
    row = (await db.execute(text("SELECT current_database(), current_user"))).one()
    assert tuple(row) == (TEST_DB_NAME, expected_user)


async def test_schema_is_at_alembic_head(db: AsyncSession) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from .conftest import PROJECT_ROOT

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("path_separator", "os")  # alembic.ini predates the option
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head


async def test_audit_log_is_append_only_for_app_role(
    db: AsyncSession, test_database: IntegrationDb
) -> None:
    if test_database.app_dsn == test_database.migrator_dsn:
        pytest.skip("MEDIACAT_TEST_DB_URL mode uses one role for everything")
    row = (
        await db.execute(
            text(
                "SELECT has_table_privilege('audit_log', 'INSERT'),"
                " has_table_privilege('audit_log', 'UPDATE'),"
                " has_table_privilege('audit_log', 'DELETE')"
            )
        )
    ).one()
    assert tuple(row) == (True, False, False)


@pytest.mark.parametrize("run", [1, 2])
async def test_each_test_starts_clean(db: AsyncSession, run: int) -> None:
    # Both runs see an empty tokens table and the full seeded country list,
    # whatever the other run (or any earlier test) wrote.
    assert (await db.execute(select(func.count(Token.id)))).scalar_one() == 0
    seeded = (await db.execute(select(func.count(Country.id)))).scalar_one()
    assert seeded > 50
    db.add(Token(media_format=MediaFormat.VINYL, title=f"run {run}"))
    db.add(Country(alpha2=f"Q{run}", alpha3=f"QQ{run}", name=f"Testland {run}"))
    await db.commit()


def test_refuses_non_test_database() -> None:
    with pytest.raises(pytest.UsageError, match="refusing"):
        assert_test_db("postgresql://u:p@127.0.0.1:5432/mediacat")
    assert_test_db(f"postgresql://u:p@127.0.0.1:5432/{TEST_DB_NAME}")


async def test_network_guard_blocks_outbound_http(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = NetworkGuard()
    guard.install(monkeypatch)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError, match="blocked"):
            await client.get("https://api.discogs.com/releases/1")
    assert guard.blocked == ["https://api.discogs.com/releases/1"]
    with pytest.raises(pytest.fail.Exception, match="Unstubbed"):
        guard.check()
    guard.allow_blocked()
    guard.check()


def test_fake_store_matches_object_store_interface() -> None:
    public = {
        n for n, _ in inspect.getmembers(ObjectStore, inspect.isfunction) if not n.startswith("_")
    }
    assert public <= set(dir(FakeObjectStore))
    for name in public:
        real = inspect.signature(getattr(ObjectStore, name))
        fake = inspect.signature(getattr(FakeObjectStore, name))
        assert list(real.parameters) == list(fake.parameters), name


async def test_fake_store_validates_like_minio() -> None:
    store = FakeObjectStore()
    with pytest.raises(ObjectStoreError):
        await store.put_image(b"not an image", "image/png")
    with pytest.raises(ObjectStoreError):
        await store.put_image(png_bytes(), "application/pdf")
    obj = await store.put_image(png_bytes(10, 20), "image/png")
    assert (obj.width_px, obj.height_px) == (10, 20)
    assert await store.get_object(obj.object_key) == png_bytes(10, 20)


async def test_unauthenticated_request_redirects_to_login(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/catalogue")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_post_without_csrf_header_is_rejected(admin: httpx.AsyncClient) -> None:
    r = await admin.post("/layout/set", data={"layout": "3panel"}, headers={"X-CSRF-Token": "x"})
    assert r.status_code == 403
    assert r.json() == {"detail": "CSRF validation failed"}


async def test_security_headers_on_app_responses(admin: httpx.AsyncClient) -> None:
    r = await admin.get("/")
    assert r.status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-request-id"]
