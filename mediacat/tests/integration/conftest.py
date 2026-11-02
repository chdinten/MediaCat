"""Integration-test harness: a throwaway Postgres, the real app, role clients.

Database
--------
By default the session starts its own ``postgres:16-alpine`` container
(``docker run --rm``, data on tmpfs, published on a random 127.0.0.1 port)
and removes it at the end.  It never connects to the Compose Postgres or
touches ``/srv/mediacat/postgres``.

Inside it the production role setup (``deploy/initdb/01-roles.sql``) runs
against the database ``mediacat_test``, then ``alembic upgrade head`` runs
as ``mediacat_migrator``.  The app connects as ``mediacat_app``, so table
grants and the append-only ``audit_log`` are the real ones.

``MEDIACAT_TEST_DB_URL`` (``postgresql://user:pw@host:port/mediacat_test``)
uses an existing server instead: the harness drops and recreates
``mediacat_test`` there and uses that one role for everything.  Any other
database name is refused.  ``MEDIACAT_TEST_DB=off`` skips the integration
tests; so does a missing or stopped Docker.

Isolation
---------
Before every test all tables are truncated and the rows the migrations
seed (countries, symbols, …) are copied back from a snapshot taken right
after ``upgrade head``.  Tests are independent of order.

App
---
Each test gets a fresh ``create_app()``.  The lifespan does not run;
``app.state`` is wired here instead: the test DB, :class:`FakeObjectStore`,
no vision/LLM backend, no connectors.  Requests go through the full
middleware stack (session cookie, ``X-CSRF-Token``, RBAC, headers) via
``httpx.AsyncClient`` + ``ASGITransport``.  Outbound network is blocked by
:class:`NetworkGuard`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mediacat.db.engine import get_engine, get_session_factory
from mediacat.db.enums import UserRole
from mediacat.db.models import User
from mediacat.web import routes
from mediacat.web.app import create_app
from mediacat.web.auth import hash_password

from .fakes import FakeObjectStore, NetworkGuard

TEST_DB_NAME = "mediacat_test"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_INTEGRATION_DIR = Path(__file__).resolve().parent
_SEED_SCHEMA = "_test_seed"
_PG_IMAGE = os.environ.get("MEDIACAT_TEST_PG_IMAGE", "postgres:16-alpine")

# Password every fixture user logs in with; random per run.
USER_PASSWORD = secrets.token_urlsafe(12)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark everything under tests/integration with ``integration``."""
    for item in items:
        if _INTEGRATION_DIR in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.integration)


# ---------------------------------------------------------------------------
# Database lifecycle (session scope)
# ---------------------------------------------------------------------------


class _UnavailableError(Exception):
    """No database to test against: skip, don't fail."""


@dataclass(frozen=True)
class IntegrationDb:
    admin_dsn: str  # superuser (container) / given role (external): reset + seed snapshot
    migrator_dsn: str  # runs alembic
    app_dsn: str  # what the app connects with
    reset_sql: str = ""


def _with_db(dsn: str, dbname: str, *, user: str | None = None, pw: str | None = None) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{user}:{pw}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, parts.fragment))


def assert_test_db(dsn: str) -> None:
    """Refuse any DSN whose database is not the test database."""
    name = urlsplit(dsn).path.lstrip("/")
    if name != TEST_DB_NAME:
        msg = f"refusing to run integration tests against database {name!r} (only {TEST_DB_NAME!r})"
        raise pytest.UsageError(msg)


def _docker(*args: str, stdin: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", *args],  # noqa: S607 - docker from PATH
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if check and proc.returncode != 0:
        msg = f"docker {args[0]} failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return proc.stdout.strip()


async def _wait_for(dsn: str, wait_s: float = 60.0) -> None:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
            await conn.close()
            return
        except (TimeoutError, OSError, asyncpg.PostgresError):
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.3)


def _start_container() -> tuple[str, str, str]:
    """Start the throwaway container; return (name, superuser DSN, app-role password)."""
    if shutil.which("docker") is None:
        msg = "docker not found"
        raise _UnavailableError(msg)
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:  # noqa: S607
        msg = "docker daemon not reachable"
        raise _UnavailableError(msg)

    name = f"mediacat-test-pg-{uuid.uuid4().hex[:8]}"
    su_pw = secrets.token_hex(16)
    app_pw = secrets.token_hex(16)
    _docker(
        "run", "-d", "--rm", "--name", name,
        "--label", "mediacat.test=1",
        "-e", f"POSTGRES_PASSWORD={su_pw}",
        "-e", f"POSTGRES_DB={TEST_DB_NAME}",
        "-p", "127.0.0.1::5432",
        "--tmpfs", "/var/lib/postgresql/data",
        _PG_IMAGE,
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    )  # fmt: skip
    try:
        port = _docker("port", name, "5432/tcp").splitlines()[0].rsplit(":", 1)[1]
        su_dsn = f"postgresql://postgres:{su_pw}@127.0.0.1:{port}/{TEST_DB_NAME}"
        asyncio.run(_wait_for(su_dsn))
        # The production role script reads the app password from a Docker secret
        # and grants CONNECT on "mediacat"; point both at the test setup.
        _docker(
            "exec", name, "sh", "-c",
            f"mkdir -p /run/secrets && printf %s {app_pw} > /run/secrets/postgres_app_password",
        )  # fmt: skip
        script = (PROJECT_ROOT / "deploy/initdb/01-roles.sql").read_text(encoding="utf-8")
        script = script.replace("DATABASE mediacat ", f"DATABASE {TEST_DB_NAME} ")
        _docker(
            "exec", "-i", name, "psql", "-q", "-v", "ON_ERROR_STOP=1",
            "-U", "postgres", "-d", TEST_DB_NAME,
            stdin=script,
        )  # fmt: skip
    except BaseException:
        _docker("rm", "-f", name, check=False)
        raise
    return name, su_dsn, app_pw


async def _recreate_external_db(dsn: str) -> None:
    conn = await asyncpg.connect(_with_db(dsn, "postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


async def _drop_external_db(dsn: str) -> None:
    conn = await asyncpg.connect(_with_db(dsn, "postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
    finally:
        await conn.close()


def _migrate(migrator_dsn: str) -> None:
    assert_test_db(migrator_dsn)
    env = {
        **os.environ,
        "DATABASE_URL": migrator_dsn.replace("postgresql://", "postgresql+asyncpg://"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{proc.stderr[-4000:]}", pytrace=False)


async def _snapshot_seed(admin_dsn: str) -> str:
    """Copy the migration-seeded rows aside; return the per-test reset SQL."""
    conn = await asyncpg.connect(admin_dsn)
    try:
        if await conn.fetchval("SELECT current_database()") != TEST_DB_NAME:
            msg = "connected to the wrong database"
            raise pytest.UsageError(msg)
        tables = [
            r["tablename"]
            for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version' ORDER BY tablename"
            )
        ]
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SEED_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SEED_SCHEMA}")
        seeded = []
        for t in tables:
            if await conn.fetchval(f'SELECT EXISTS (SELECT 1 FROM public."{t}")'):  # noqa: S608
                await conn.execute(f'CREATE TABLE {_SEED_SCHEMA}."{t}" AS TABLE public."{t}"')
                seeded.append(t)
    finally:
        await conn.close()
    sql = "TRUNCATE " + ", ".join(f'public."{t}"' for t in tables) + " RESTART IDENTITY CASCADE;"
    for t in seeded:
        sql += f' INSERT INTO public."{t}" SELECT * FROM {_SEED_SCHEMA}."{t}";'  # noqa: S608
    return sql


@pytest.fixture(scope="session")
def test_database() -> Iterator[IntegrationDb]:
    """The migrated test database for this session (skips if unavailable)."""
    if os.environ.get("MEDIACAT_TEST_DB", "").lower() in {"off", "0", "no", "false"}:
        pytest.skip("integration tests disabled (MEDIACAT_TEST_DB=off)")

    external = os.environ.get("MEDIACAT_TEST_DB_URL", "")
    container: str | None = None
    try:
        if external:
            assert_test_db(external)
            try:
                asyncio.run(_recreate_external_db(external))
            except (OSError, asyncpg.PostgresError) as exc:
                msg = f"MEDIACAT_TEST_DB_URL server not reachable: {exc}"
                raise _UnavailableError(msg) from exc
            db = IntegrationDb(admin_dsn=external, migrator_dsn=external, app_dsn=external)
        else:
            container, su_dsn, app_pw = _start_container()
            db = IntegrationDb(
                admin_dsn=su_dsn,
                migrator_dsn=_with_db(su_dsn, TEST_DB_NAME, user="mediacat_migrator", pw=app_pw),
                app_dsn=_with_db(su_dsn, TEST_DB_NAME, user="mediacat_app", pw=app_pw),
            )
    except _UnavailableError as exc:
        pytest.skip(f"integration tests need Docker or MEDIACAT_TEST_DB_URL: {exc}")

    try:
        for dsn in (db.admin_dsn, db.migrator_dsn, db.app_dsn):
            assert_test_db(dsn)
        _migrate(db.migrator_dsn)
        db = IntegrationDb(
            admin_dsn=db.admin_dsn,
            migrator_dsn=db.migrator_dsn,
            app_dsn=db.app_dsn,
            reset_sql=asyncio.run(_snapshot_seed(db.admin_dsn)),
        )
        yield db
    finally:
        if container:
            _docker("rm", "-f", container, check=False)
        elif external:
            asyncio.run(_drop_external_db(external))


@pytest.fixture(scope="session")
def password_hash() -> str:
    """Argon2 hash of :data:`USER_PASSWORD`, computed once (it costs ~0.1 s)."""
    return hash_password(USER_PASSWORD)


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkGuard]:
    """Block outbound network for every integration test (see :class:`NetworkGuard`)."""
    guard = NetworkGuard()
    guard.install(monkeypatch)
    yield guard
    guard.check()


@pytest.fixture
async def engine(test_database: IntegrationDb) -> AsyncIterator[AsyncEngine]:
    """Clean database (truncate + seed restore) and an engine as the app role."""
    conn = await asyncpg.connect(test_database.admin_dsn)
    try:
        await conn.execute(test_database.reset_sql)
    finally:
        await conn.close()
    eng = get_engine(
        test_database.app_dsn.replace("postgresql://", "postgresql+asyncpg://"),
        pool_size=5,
        max_overflow=5,
    )
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return get_session_factory(engine)


@pytest.fixture
async def db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session for seeding and checking data directly (commit to make it visible)."""
    async with session_factory() as session:
        yield session


@pytest.fixture
def object_store() -> FakeObjectStore:
    return FakeObjectStore()


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    object_store: FakeObjectStore,
) -> FastAPI:
    """A fresh app wired to the test database and fakes."""
    # The in-memory bootstrap store is module state (test_web seeds it): start empty.
    monkeypatch.setattr(routes, "_users_store", {})
    application = create_app()
    application.state.db_engine = engine
    application.state.db_session_factory = session_factory
    application.state.object_store = object_store
    application.state.vision = None
    application.state.llm = None
    application.state.connectors = {}
    return application


@pytest.fixture
async def users(db: AsyncSession, password_hash: str) -> dict[str, User]:
    """One active user per role: ``users["admin"]``, ``["reviewer"]``, ``["contributor"]``, ``["viewer"]``."""
    created: dict[str, User] = {}
    for role in (UserRole.ADMIN, UserRole.REVIEWER, UserRole.CONTRIBUTOR, UserRole.VIEWER):
        u = User(
            username=f"{role.value}-user",
            email=f"{role.value}@test.invalid",
            password_hash=password_hash,
            role=role,
            is_active=True,
        )
        db.add(u)
        created[role.value] = u
    await db.commit()
    return created


ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest.fixture
async def make_client(app: FastAPI, users: dict[str, User]) -> AsyncIterator[ClientFactory]:
    """``await make_client("reviewer")`` → client logged in as that role ("anon" = no session).

    The client sends the session cookie and the matching ``X-CSRF-Token``
    header, as ``forms.js`` does in the browser.  It does not follow redirects.
    """
    clients: list[httpx.AsyncClient] = []

    async def _make(role: str) -> httpx.AsyncClient:
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}
        if role != "anon":
            sm = app.state.session_manager
            cookie = sm.create_session(str(users[role].id), role)
            cookies[sm.cookie_name] = cookie
            headers["X-CSRF-Token"] = app.state.csrf_protection.generate_token(cookie)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
            cookies=cookies,
            follow_redirects=False,
        )
        clients.append(client)
        return client

    yield _make
    for c in clients:
        await c.aclose()


@pytest.fixture
async def admin(make_client: ClientFactory) -> httpx.AsyncClient:
    return await make_client("admin")


@pytest.fixture
async def reviewer(make_client: ClientFactory) -> httpx.AsyncClient:
    return await make_client("reviewer")


@pytest.fixture
async def contributor(make_client: ClientFactory) -> httpx.AsyncClient:
    return await make_client("contributor")


@pytest.fixture
async def viewer(make_client: ClientFactory) -> httpx.AsyncClient:
    return await make_client("viewer")


@pytest.fixture
async def anon(make_client: ClientFactory) -> httpx.AsyncClient:
    return await make_client("anon")
