"""Integration tests for ``mediacat.web.routes`` against the real app and a test Postgres.

Covers login/logout (rate limiter, proxy-aware client IP, rehash), user
management (incl. last-admin protection) and self-registration, the
dashboard numbers and carousels, the global search in both layouts, the
review queue, the token browser and the layout switch.

Known bugs are pinned with ``xfail(strict=True, reason="BUG: ...")``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.enums import (
    MediaFormat,
    ReviewReason,
    ReviewStatus,
    RevisionSource,
    TokenStatus,
    UserRole,
)
from mediacat.db.models import (
    ArtistBio,
    AuditLog,
    Country,
    Label,
    MediaObject,
    ReviewItem,
    Token,
    TokenRevision,
    User,
)
from mediacat.web import routes
from mediacat.web.auth import LoginRateLimiter, needs_rehash, verify_password

from .conftest import USER_PASSWORD, ClientFactory

NON_ADMIN_ROLES = ("reviewer", "contributor", "viewer")
SESSION_COOKIE = "mediacat_session"
T0 = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(
    client: httpx.AsyncClient, username: str, password: str = USER_PASSWORD, **headers: str
) -> httpx.Response:
    return await client.post(
        "/login", data={"username": username, "password": password}, headers=headers
    )


def _set_cookie(r: httpx.Response, name: str) -> str | None:
    """The raw ``Set-Cookie`` header for *name*, if the response sets it."""
    for raw in r.headers.get_list("set-cookie"):
        if raw.startswith(f"{name}="):
            return raw
    return None


async def _user(db: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(User.username == username).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _add_user(
    db: AsyncSession,
    username: str,
    password_hash: str,
    role: UserRole = UserRole.VIEWER,
    **kw: Any,
) -> User:
    u = User(
        username=username,
        email=f"{username}@test.invalid",
        password_hash=password_hash,
        role=role,
        is_active=kw.pop("is_active", True),
        **kw,
    )
    db.add(u)
    await db.commit()
    return u


async def _add_token(db: AsyncSession, **kw: Any) -> Token:
    kw.setdefault("media_format", MediaFormat.VINYL)
    kw.setdefault("status", TokenStatus.ACTIVE)
    t = Token(**kw)
    db.add(t)
    await db.commit()
    return t


async def _add_review(
    db: AsyncSession,
    token: Token,
    status: ReviewStatus = ReviewStatus.PENDING,
    **kw: Any,
) -> ReviewItem:
    kw.setdefault("reason", ReviewReason.LOW_CONFIDENCE)
    r = ReviewItem(token_id=token.id, status=status, **kw)
    db.add(r)
    await db.commit()
    return r


async def _review(db: AsyncSession, review_id: uuid.UUID) -> ReviewItem:
    stmt = select(ReviewItem).where(ReviewItem.id == review_id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


def _stat(html: str, label: str) -> str:
    m = re.search(
        r'<span class="stat-value">([^<]*)</span>\s*<span class="stat-label">'
        + re.escape(label)
        + "</span>",
        html,
    )
    assert m, f"no stat card {label!r}"
    return m.group(1).strip()


def _dashboard_sections(html: str) -> dict[str, list[str]]:
    """Carousel heading → card titles in page order."""
    sections: dict[str, list[str]] = {}
    for chunk in html.split('<section class="recent-section')[1:]:
        m = re.search(r"<h2>([^<]+)</h2>", chunk) or re.search(
            r'genre-section-dot"></span>([^<]+)\s*</h2>', chunk
        )
        assert m
        sections[m.group(1).strip()] = re.findall(r'class="recent-title">([^<]*)<', chunk)
    return sections


def _result_ids(html: str) -> list[str]:
    """Pressing ids of a classic search result list, in order."""
    return re.findall(r'<a href="/catalogue/([0-9a-f-]{36})"\s+class="search-result-row"', html)


def _panel_album_ids(html: str) -> list[str]:
    """Album/track hit ids of a 3-panel search response, in order."""
    return re.findall(r'<a class="sp-artist-item" href="/catalogue/([0-9a-f-]{36})"', html)


def _panel_artists(html: str) -> list[str]:
    return [s.strip() for s in re.findall(r'class="sp-artist-item-name">([^<]*)<', html)]


def _token_list_titles(html: str) -> list[str]:
    return re.findall(r'<td><a href="/catalogue/[0-9a-f-]{36}">([^<]*)</a></td>', html)


class _BrokenSessionFactory:
    """Stands in for ``db_session_factory`` when Postgres is down."""

    def __call__(self) -> _BrokenSessionFactory:
        return self

    async def __aenter__(self) -> AsyncSession:
        msg = "connection refused (simulated outage)"
        raise ConnectionRefusedError(msg)

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
async def caddy(app: FastAPI, users: dict[str, User]) -> AsyncIterator[httpx.AsyncClient]:
    """Anonymous client whose peer is Caddy on the Compose network (a trusted proxy)."""
    transport = httpx.ASGITransport(app=app, client=("172.18.0.2", 40000))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_health_probes_are_public(anon: httpx.AsyncClient, path: str) -> None:
    r = await anon.get(path)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


async def test_login_page_renders_form(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/login")
    assert r.status_code == 200
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text
    assert "alert-danger" not in r.text


async def test_login_success_sets_session_cookie_and_last_login(
    app: FastAPI, anon: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    before = datetime.now(UTC)
    r = await _login(anon, "reviewer-user")

    assert r.status_code == 303
    assert r.headers["location"] == "/"
    raw = _set_cookie(r, SESSION_COOKIE)
    assert raw is not None
    assert "HttpOnly" in raw
    assert "Max-Age=86400" in raw
    assert "samesite=lax" in raw.lower()
    session = app.state.session_manager.validate_session(anon.cookies[SESSION_COOKIE])
    assert session == {"user_id": str(users["reviewer"].id), "role": "reviewer"}

    row = await _user(db, "reviewer-user")
    assert row is not None
    assert row.last_login_at is not None
    assert row.last_login_at >= before - timedelta(seconds=5)
    assert row.failed_login_count == 0
    other = await _user(db, "viewer-user")
    assert other is not None
    assert other.last_login_at is None

    # The cookie the browser now holds opens the app.
    assert (await anon.get("/")).status_code == 200


async def test_login_cookie_uses_per_user_session_timeout(
    anon: httpx.AsyncClient, db: AsyncSession, password_hash: str
) -> None:
    await _add_user(db, "shortlived", password_hash, session_timeout_seconds=900)
    r = await _login(anon, "shortlived")
    assert r.status_code == 303
    raw = _set_cookie(r, SESSION_COOKIE)
    assert raw is not None
    assert "Max-Age=900" in raw


async def test_login_wrong_password_is_401_without_session(
    anon: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    r = await _login(anon, "admin-user", "not-the-password")
    assert r.status_code == 401
    assert "Invalid username or password." in r.text
    assert _set_cookie(r, SESSION_COOKIE) is None
    row = await _user(db, "admin-user")
    assert row is not None
    assert row.last_login_at is None


async def test_login_unknown_user_is_401(anon: httpx.AsyncClient, users: dict[str, User]) -> None:
    r = await _login(anon, "nobody")
    assert r.status_code == 401
    assert "Invalid username or password." in r.text
    assert _set_cookie(r, SESSION_COOKIE) is None


@pytest.mark.parametrize(
    "attrs",
    [{"is_active": False}, {"deleted_at": T0}],
    ids=["inactive", "soft-deleted"],
)
async def test_login_rejects_inactive_and_deleted_users_even_with_right_password(
    anon: httpx.AsyncClient, db: AsyncSession, password_hash: str, attrs: dict[str, Any]
) -> None:
    await _add_user(db, "gone", password_hash, **attrs)
    r = await _login(anon, "gone")
    assert r.status_code == 401
    assert "Invalid username or password." in r.text
    assert _set_cookie(r, SESSION_COOKIE) is None
    row = await _user(db, "gone")
    assert row is not None
    assert row.last_login_at is None


async def test_login_locks_username_after_threshold_failures(
    app: FastAPI, caddy: httpx.AsyncClient, db: AsyncSession
) -> None:
    app.state.login_rate_limiter = LoginRateLimiter(max_attempts=3, window_seconds=900)
    # Each attempt from a different (proxied) IP: only the username key locks.
    for i in range(3):
        r = await _login(caddy, "viewer-user", "wrong", **{"X-Forwarded-For": f"203.0.113.{i}"})
        assert r.status_code == 401

    r = await _login(caddy, "viewer-user", **{"X-Forwarded-For": "203.0.113.99"})
    assert r.status_code == 429
    assert "Account temporarily locked" in r.text
    assert _set_cookie(r, SESSION_COOKIE) is None
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.last_login_at is None

    # Other accounts are unaffected.
    r = await _login(caddy, "reviewer-user", **{"X-Forwarded-For": "203.0.113.99"})
    assert r.status_code == 303


async def test_successful_login_resets_the_failure_counter(
    app: FastAPI, make_client: ClientFactory, users: dict[str, User]
) -> None:
    app.state.login_rate_limiter = LoginRateLimiter(max_attempts=3, window_seconds=900)
    client = await make_client("anon")
    for _ in range(2):
        assert (await _login(client, "viewer-user", "wrong")).status_code == 401
    assert (await _login(client, "viewer-user")).status_code == 303
    client.cookies.clear()  # log out again (a logged-in POST would need the CSRF header)
    for _ in range(2):
        assert (await _login(client, "viewer-user", "wrong")).status_code == 401
    # 4 failures in the window, but never 3 in a row: still allowed.
    assert (await _login(client, "viewer-user")).status_code == 303


async def test_login_locks_client_ip_at_configured_threshold(
    caddy: httpx.AsyncClient,
) -> None:
    """Documented: 10 failures per IP in 15 min (config security.login.lockout_threshold).

    Behind Caddy the client address comes from X-Forwarded-For.
    """
    attacker = {"X-Forwarded-For": "198.51.100.7"}
    for i in range(10):
        r = await _login(caddy, f"guess{i}", "x", **attacker)
        assert r.status_code == 401, f"attempt {i + 1} locked too early"

    r = await _login(caddy, "admin-user", **attacker)
    assert r.status_code == 429
    # The same account from another address still logs in.
    r = await _login(caddy, "admin-user", **{"X-Forwarded-For": "198.51.100.8"})
    assert r.status_code == 303


async def test_forwarded_for_is_ignored_from_untrusted_peer(
    app: FastAPI, anon: httpx.AsyncClient, users: dict[str, User]
) -> None:
    # The anon client connects from 127.0.0.1, which config/app.yaml does not trust.
    assert not any(
        n.version == 4 and n.overlaps(ip_network("127.0.0.1/32"))
        for n in app.state.trusted_proxy_networks
    )
    app.state.login_rate_limiter = LoginRateLimiter(max_attempts=2, window_seconds=900)
    for i in range(2):
        r = await _login(anon, f"guess{i}", "x", **{"X-Forwarded-For": f"192.0.2.{i}"})
        assert r.status_code == 401
    # A spoofed header does not dodge the lock on the real peer address.
    r = await _login(anon, "admin-user", **{"X-Forwarded-For": "192.0.2.200"})
    assert r.status_code == 429


async def test_login_upgrades_an_outdated_password_hash(
    anon: httpx.AsyncClient, db: AsyncSession
) -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(USER_PASSWORD)
    assert needs_rehash(weak)
    await _add_user(db, "legacy", weak)

    assert (await _login(anon, "legacy")).status_code == 303

    row = await _user(db, "legacy")
    assert row is not None
    assert row.password_hash != weak
    assert not needs_rehash(row.password_hash)
    assert verify_password(USER_PASSWORD, row.password_hash)


async def test_login_with_bootstrap_admin_from_memory_store(
    app: FastAPI, anon: httpx.AsyncClient, db: AsyncSession
) -> None:
    routes.seed_admin("bootadmin", "boot-password-1")
    r = await _login(anon, "bootadmin", "boot-password-1")
    assert r.status_code == 303
    session = app.state.session_manager.validate_session(anon.cookies[SESSION_COOKIE])
    assert session == {"user_id": routes._users_store["bootadmin"]["id"], "role": "admin"}
    # Deterministic id: same username → same UUID on every restart.
    assert (
        routes._users_store["bootadmin"]["id"]
        == uuid.uuid5(routes._DEV_USER_NAMESPACE, "bootadmin").hex
    )
    anon.cookies.clear()  # log out (a logged-in POST would need the CSRF header)
    assert (await _login(anon, "bootadmin", "wrong")).status_code == 401


async def test_logout_clears_the_session_cookie(admin: httpx.AsyncClient) -> None:
    r = await admin.get("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    raw = _set_cookie(r, SESSION_COOKIE)
    assert raw is not None
    assert re.match(rf'{SESSION_COOKIE}=(""|);', raw)
    assert "Max-Age=0" in raw


async def test_db_account_lock_is_enforced(
    anon: httpx.AsyncClient, db: AsyncSession, password_hash: str
) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    await _add_user(db, "locked", password_hash, locked_until=until)
    r = await _login(anon, "locked")
    assert r.status_code in (401, 429)
    assert _set_cookie(r, SESSION_COOKIE) is None


async def test_failed_login_is_counted_on_the_user_row(
    anon: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    assert (await _login(anon, "viewer-user", "wrong")).status_code == 401
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.failed_login_count == 1


async def test_per_user_session_timeout_is_enforced_server_side(
    anon: httpx.AsyncClient, db: AsyncSession, password_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from itsdangerous import TimestampSigner

    now = [int(datetime.now(UTC).timestamp())]
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _self: now[0])
    await _add_user(db, "shortlived", password_hash, session_timeout_seconds=900)
    assert (await _login(anon, "shortlived")).status_code == 303
    assert (await anon.get("/")).status_code == 200

    now[0] += 1000  # past the user's 15 minutes; a client may keep the cookie anyway
    r = await anon.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Audit trail (db/audit.py: "Every significant action ... calls write_audit")
# ---------------------------------------------------------------------------


async def _audit_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(AuditLog.id)))).scalar_one()


async def test_login_is_audited(
    anon: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    assert (await _login(anon, "admin-user")).status_code == 303
    assert await _audit_count(db) >= 1


async def test_review_decision_is_audited(reviewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await _add_token(db, title="Audit me")
    item = await _add_review(db, token)
    r = await reviewer.post(f"/reviews/{item.id}/approve", data={"comment": "ok"})
    assert r.status_code == 303
    assert (await _review(db, item.id)).status == ReviewStatus.APPROVED
    assert await _audit_count(db) >= 1


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


async def test_user_list_shows_db_users(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession, password_hash: str
) -> None:
    await _add_user(db, "sleeper", password_hash, is_active=False)
    await _add_user(db, "erased", password_hash, deleted_at=T0)
    seen = users["reviewer"]
    seen.last_login_at = datetime(2025, 3, 4, 5, 6, tzinfo=UTC)
    await db.commit()

    r = await admin.get("/users")
    assert r.status_code == 200
    names = re.findall(r"<tr>\s*<td>([^<]+)</td>", r.text)
    assert names == ["admin-user", "contributor-user", "reviewer-user", "sleeper", "viewer-user"]
    assert "viewer@test.invalid" in r.text
    assert '<span class="badge badge-contributor">contributor</span>' in r.text
    assert "2025-03-04 05:06" in r.text
    sleeper = await _user(db, "sleeper")
    assert sleeper is not None
    assert f'action="/users/{sleeper.id}/activate"' in r.text
    assert f'action="/users/{users["viewer"].id}/deactivate"' in r.text
    assert "erased" not in r.text


@pytest.mark.parametrize("role", NON_ADMIN_ROLES)
async def test_user_management_is_admin_only(
    make_client: ClientFactory, users: dict[str, User], db: AsyncSession, role: str
) -> None:
    client = await make_client(role)
    target = users["viewer"].id
    calls: list[tuple[str, str, dict[str, str] | None]] = [
        ("GET", "/users", None),
        ("GET", "/users/new", None),
        (
            "POST",
            "/users/new",
            {"username": "sneaky", "email": "s@test.invalid", "password": "password123"},
        ),
        ("GET", f"/users/{target}/edit", None),
        ("POST", f"/users/{target}/edit", {"role": "admin", "is_active": "on"}),
        ("POST", f"/users/{target}/deactivate", {}),
        ("POST", f"/users/{target}/activate", {}),
    ]
    for method, url, data in calls:
        r = await client.request(method, url, data=data)
        assert r.status_code == 403, f"{method} {url}"
        assert r.json() == {"detail": "Insufficient permissions"}

    assert await _user(db, "sneaky") is None
    viewer = await _user(db, "viewer-user")
    assert viewer is not None
    assert (viewer.role, viewer.is_active) == (UserRole.VIEWER, True)


async def test_create_user_form_offers_the_roles(admin: httpx.AsyncClient) -> None:
    r = await admin.get("/users/new")
    assert r.status_code == 200
    for role in ("admin", "contributor", "reviewer", "viewer"):
        assert f'value="{role}"' in r.text


async def test_create_user_persists_and_can_log_in(
    app: FastAPI, admin: httpx.AsyncClient, anon: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin.post(
        "/users/new",
        data={
            "username": "newbie",
            "email": "newbie@test.invalid",
            "password": "correct-horse",
            "role": "reviewer",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/users"

    row = await _user(db, "newbie")
    assert row is not None
    assert (row.email, row.role, row.is_active) == ("newbie@test.invalid", UserRole.REVIEWER, True)
    assert row.session_timeout_seconds == 86400
    assert verify_password("correct-horse", row.password_hash)

    assert (await _login(anon, "newbie", "correct-horse")).status_code == 303
    session = app.state.session_manager.validate_session(anon.cookies[SESSION_COOKIE])
    assert session is not None
    assert uuid.UUID(session["user_id"]) == row.id
    assert session["role"] == "reviewer"


async def test_create_user_with_unknown_role_becomes_viewer(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin.post(
        "/users/new",
        data={
            "username": "rooty",
            "email": "rooty@test.invalid",
            "password": "password123",
            "role": "service",
        },
    )
    assert r.status_code == 303
    row = await _user(db, "rooty")
    assert row is not None
    assert row.role == UserRole.VIEWER


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        (
            {"username": "viewer-user", "email": "dup@test.invalid", "password": "password123"},
            409,
            "Username &#39;viewer-user&#39; or that e-mail address is already in use.",
        ),
        (
            {"username": "shorty", "email": "shorty@test.invalid", "password": "short"},
            400,
            "Password must be at least 8 characters.",
        ),
    ],
    ids=["duplicate-username", "short-password"],
)
async def test_create_user_validation(
    admin: httpx.AsyncClient,
    users: dict[str, User],
    db: AsyncSession,
    form: dict[str, str],
    status: int,
    message: str,
) -> None:
    before = (await db.execute(select(func.count(User.id)))).scalar_one()
    r = await admin.post("/users/new", data=form)
    assert r.status_code == status
    assert message in r.text
    assert (await db.execute(select(func.count(User.id)))).scalar_one() == before
    assert form["username"] not in routes._users_store


@pytest.mark.parametrize("endpoint", ["/users/new", "/register"])
@pytest.mark.usefixtures("registration_open")
async def test_duplicate_email_is_rejected(
    make_client: ClientFactory, users: dict[str, User], db: AsyncSession, endpoint: str
) -> None:
    client = await make_client("admin" if endpoint == "/users/new" else "anon")
    form = {
        "username": "ghost",
        "email": users["viewer"].email,
        "password": "password123",
        "confirm_password": "password123",
    }
    r = await client.post(endpoint, data=form)
    assert await _user(db, "ghost") is None
    assert r.status_code in (400, 409)
    anon = await make_client("anon")
    assert (await _login(anon, "ghost", "password123")).status_code == 401


async def test_edit_form_shows_the_user(admin: httpx.AsyncClient, users: dict[str, User]) -> None:
    r = await admin.get(f"/users/{users['contributor'].id}/edit")
    assert r.status_code == 200
    assert "Edit user: contributor-user" in r.text
    assert '<option value="contributor" selected>' in r.text
    assert re.search(r'<option value="86400"\s+selected>', r.text)
    assert f'action="/users/{users["contributor"].id}/edit"' in r.text


@pytest.mark.parametrize("user_id", [str(uuid.uuid4()), "not-a-uuid"], ids=["unknown", "malformed"])
async def test_edit_unknown_user_is_404(
    admin: httpx.AsyncClient, users: dict[str, User], user_id: str
) -> None:
    assert (await admin.get(f"/users/{user_id}/edit")).status_code == 404
    r = await admin.post(f"/users/{user_id}/edit", data={"role": "viewer"})
    assert r.status_code == 404


async def test_edit_changes_role_timeout_and_active(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    uid = users["viewer"].id
    r = await admin.post(
        f"/users/{uid}/edit",
        data={"role": "contributor", "session_timeout_seconds": "3600", "is_active": "on"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/users"
    row = await _user(db, "viewer-user")
    assert row is not None
    assert (row.role, row.session_timeout_seconds, row.is_active) == (
        UserRole.CONTRIBUTOR,
        3600,
        True,
    )

    r = await admin.post(
        f"/users/{uid}/edit",
        data={"role": "superuser", "session_timeout_seconds": "10", "is_active": "off"},
    )
    assert r.status_code == 303
    row = await _user(db, "viewer-user")
    assert row is not None
    # Unknown role → viewer; timeout clamped to [300, 604800]; "off" deactivates.
    assert (row.role, row.session_timeout_seconds, row.is_active) == (UserRole.VIEWER, 300, False)

    await admin.post(f"/users/{uid}/edit", data={"session_timeout_seconds": "99999999"})
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.session_timeout_seconds == 604800


async def test_admin_sets_a_new_password_and_lifts_the_lock(
    app: FastAPI,
    admin: httpx.AsyncClient,
    anon: httpx.AsyncClient,
    users: dict[str, User],
    db: AsyncSession,
) -> None:
    app.state.login_lockout = (3, 900)
    for _ in range(3):
        await _login(anon, "viewer-user", "wrong")
    uid = users["viewer"].id
    r = await admin.post(
        f"/users/{uid}/edit", data={"role": "viewer", "is_active": "on", "new_password": "short"}
    )
    assert r.status_code == 400
    assert "at least 8 characters" in r.text

    r = await admin.post(
        f"/users/{uid}/edit",
        data={"role": "viewer", "is_active": "on", "new_password": "fresh-secret-1"},
    )
    assert r.status_code == 303
    row = await _user(db, "viewer-user")
    assert row is not None
    assert (row.failed_login_count, row.locked_until) == (0, None)
    assert verify_password("fresh-secret-1", row.password_hash)
    assert (await _login(anon, "viewer-user", USER_PASSWORD)).status_code == 401
    assert (await _login(anon, "viewer-user", "fresh-secret-1")).status_code == 303


async def test_edit_without_password_keeps_it(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, users: dict[str, User]
) -> None:
    r = await admin.post(
        f"/users/{users['viewer'].id}/edit",
        data={"role": "viewer", "is_active": "on", "new_password": ""},
    )
    assert r.status_code == 303
    assert (await _login(anon, "viewer-user")).status_code == 303


async def test_role_change_applies_at_next_login(
    app: FastAPI,
    admin: httpx.AsyncClient,
    anon: httpx.AsyncClient,
    users: dict[str, User],
) -> None:
    r = await admin.post(
        f"/users/{users['viewer'].id}/edit", data={"role": "reviewer", "is_active": "on"}
    )
    assert r.status_code == 303
    assert (await _login(anon, "viewer-user")).status_code == 303
    session = app.state.session_manager.validate_session(anon.cookies[SESSION_COOKIE])
    assert session is not None
    assert session["role"] == "reviewer"


async def test_edit_form_without_active_checkbox_deactivates(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    # What forms.js sends when the box is unticked: FormData omits the field.
    r = await admin.post(
        f"/users/{users['viewer'].id}/edit",
        data={"role": "viewer", "session_timeout_seconds": "86400"},
    )
    assert r.status_code == 303
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.is_active is False


@pytest.mark.parametrize(
    "form",
    [{"role": "viewer", "is_active": "on"}, {"role": "admin", "is_active": "off"}],
    ids=["demote", "deactivate"],
)
async def test_last_admin_cannot_be_demoted_or_deactivated_via_edit(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession, form: dict[str, str]
) -> None:
    r = await admin.post(f"/users/{users['admin'].id}/edit", data=form)
    assert r.status_code == 409
    assert "Cannot demote or deactivate the last active admin account." in r.text
    row = await _user(db, "admin-user")
    assert row is not None
    assert (row.role, row.is_active) == (UserRole.ADMIN, True)


async def test_admin_can_be_demoted_while_another_admin_is_active(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession, password_hash: str
) -> None:
    await _add_user(db, "admin2", password_hash, role=UserRole.ADMIN)
    r = await admin.post(f"/users/{users['admin'].id}/edit", data={"role": "viewer"})
    assert r.status_code == 303
    row = await _user(db, "admin-user")
    assert row is not None
    assert row.role == UserRole.VIEWER
    # admin2 is now the last one.
    admin2 = await _user(db, "admin2")
    assert admin2 is not None
    r = await admin.post(f"/users/{admin2.id}/edit", data={"role": "viewer"})
    assert r.status_code == 409


async def test_inactive_admin_can_be_edited_when_one_admin_is_active(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession, password_hash: str
) -> None:
    dormant = await _add_user(db, "dormant", password_hash, role=UserRole.ADMIN, is_active=False)
    # Change only the timeout; the account stays an inactive admin.
    r = await admin.post(
        f"/users/{dormant.id}/edit",
        data={"role": "admin", "session_timeout_seconds": "3600", "is_active": "off"},
    )
    assert r.status_code == 303
    row = await _user(db, "dormant")
    assert row is not None
    assert row.session_timeout_seconds == 3600


async def test_deactivate_and_reactivate_a_user(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    uid = users["viewer"].id
    r = await admin.post(f"/users/{uid}/deactivate")
    assert r.status_code == 303
    assert r.headers["location"] == "/users"
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.is_active is False
    assert f'action="/users/{uid}/activate"' in (await admin.get("/users")).text
    assert (await _login(anon, "viewer-user")).status_code == 401

    r = await admin.post(f"/users/{uid}/activate")
    assert r.status_code == 303
    assert r.headers["location"] == "/users"
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.is_active is True
    assert (await _login(anon, "viewer-user")).status_code == 303


async def test_last_admin_cannot_be_deactivated(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession, password_hash: str
) -> None:
    r = await admin.post(f"/users/{users['admin'].id}/deactivate")
    assert r.status_code == 303
    assert r.headers["location"] == "/users?error=last_admin"
    row = await _user(db, "admin-user")
    assert row is not None
    assert row.is_active is True

    await _add_user(db, "admin2", password_hash, role=UserRole.ADMIN)
    r = await admin.post(f"/users/{users['admin'].id}/deactivate")
    assert r.headers["location"] == "/users"
    row = await _user(db, "admin-user")
    assert row is not None
    assert row.is_active is False


async def test_user_created_in_ui_and_deactivated_cannot_log_in(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, db: AsyncSession
) -> None:
    form = {"username": "temp", "email": "temp@test.invalid", "password": "password123"}
    assert (await admin.post("/users/new", data=form)).status_code == 303
    row = await _user(db, "temp")
    assert row is not None
    assert f'action="/users/{row.id}/deactivate"' in (await admin.get("/users")).text

    assert (await admin.post(f"/users/{row.id}/deactivate")).status_code == 303
    row = await _user(db, "temp")
    assert row is not None
    assert row.is_active is False
    assert (await _login(anon, "temp", "password123")).status_code == 401


async def test_user_created_in_ui_and_demoted_logs_in_with_new_role(
    app: FastAPI, admin: httpx.AsyncClient, anon: httpx.AsyncClient, db: AsyncSession
) -> None:
    form = {
        "username": "exadmin",
        "email": "exadmin@test.invalid",
        "password": "password123",
        "role": "admin",
    }
    assert (await admin.post("/users/new", data=form)).status_code == 303
    row = await _user(db, "exadmin")
    assert row is not None
    r = await admin.post(f"/users/{row.id}/edit", data={"role": "viewer", "is_active": "on"})
    assert r.status_code == 303
    row = await _user(db, "exadmin")
    assert row is not None
    assert row.role == UserRole.VIEWER

    assert (await _login(anon, "exadmin", "password123")).status_code == 303
    session = app.state.session_manager.validate_session(anon.cookies[SESSION_COOKIE])
    assert session is not None
    assert session["role"] == "viewer"


# ---------------------------------------------------------------------------
# User management without a database (bootstrap / in-memory store)
# ---------------------------------------------------------------------------


async def test_user_management_falls_back_to_the_memory_store(
    app: FastAPI, admin: httpx.AsyncClient, password_hash: str
) -> None:
    app.state.db_session_factory = None
    boot_id = uuid.uuid4().hex
    helper_id = uuid.uuid4().hex
    routes._users_store.update(
        {
            "boot": {
                "id": boot_id,
                "username": "boot",
                "email": "boot@localhost",
                "password_hash": password_hash,
                "role": "admin",
                "is_active": True,
            },
            "helper": {
                "id": helper_id,
                "username": "helper",
                "email": "helper@localhost",
                "password_hash": password_hash,
                "role": "viewer",
                "is_active": True,
            },
        }
    )

    r = await admin.get("/users")
    assert r.status_code == 200
    assert re.findall(r"<tr>\s*<td>([^<]+)</td>", r.text) == ["boot", "helper"]
    assert password_hash not in r.text

    r = await admin.get(f"/users/{helper_id}/edit")
    assert r.status_code == 200
    assert "Edit user: helper" in r.text

    r = await admin.post(
        f"/users/{helper_id}/edit",
        data={"role": "reviewer", "session_timeout_seconds": "1800", "is_active": "on"},
    )
    assert r.status_code == 303
    helper = routes._users_store["helper"]
    assert (helper["role"], helper["session_timeout_seconds"]) == ("reviewer", 1800)

    # Last-admin protection counts memory admins when there is no DB.
    r = await admin.post(f"/users/{boot_id}/edit", data={"role": "viewer"})
    assert r.status_code == 409
    r = await admin.post(f"/users/{boot_id}/deactivate")
    assert r.headers["location"] == "/users?error=last_admin"
    assert routes._users_store["boot"]["is_active"] is True

    r = await admin.post(f"/users/{helper_id}/deactivate")
    assert r.headers["location"] == "/users"
    assert helper["is_active"] is False
    r = await admin.post(f"/users/{helper_id}/activate")
    assert r.headers["location"] == "/users"
    assert helper["is_active"] is True


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


@pytest.fixture
def registration_open(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on ``security.self_registration`` (off by default) for one test."""
    monkeypatch.setattr(app.state, "self_registration", True, raising=False)


async def test_register_is_off_by_default(anon: httpx.AsyncClient, db: AsyncSession) -> None:
    assert (await anon.get("/register")).status_code == 404
    form = {
        "username": "uninvited",
        "email": "uninvited@test.invalid",
        "password": "long-enough",
        "confirm_password": "long-enough",
    }
    assert (await anon.post("/register", data=form)).status_code == 404
    assert await _user(db, "uninvited") is None


@pytest.mark.usefixtures("registration_open")
async def test_register_page_is_public(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/register")
    assert r.status_code == 200
    assert 'name="confirm_password"' in r.text


@pytest.mark.usefixtures("registration_open")
async def test_register_creates_an_active_viewer(
    app: FastAPI, anon: httpx.AsyncClient, db: AsyncSession
) -> None:
    form = {
        "username": "new.fan-1",
        "email": "fan@test.invalid",
        "password": "long-enough",
        "confirm_password": "long-enough",
    }
    r = await anon.post("/register", data=form)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?registered=1"
    row = await _user(db, "new.fan-1")
    assert row is not None
    assert (row.role, row.is_active, row.email) == (UserRole.VIEWER, True, "fan@test.invalid")

    assert (await _login(anon, "new.fan-1", "long-enough")).status_code == 303
    session = app.state.session_manager.validate_session(anon.cookies[SESSION_COOKIE])
    assert session is not None
    assert session["role"] == "viewer"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"username": "ab"}, "Username must be at least 3 characters"),
        ({"username": "bad name!"}, "Username must be at least 3 characters"),
        ({"username": "viewer-user"}, "That username or e-mail address is already registered."),
        ({"password": "short", "confirm_password": "short"}, "Password must be at least 8"),
        ({"confirm_password": "different-one"}, "Passwords do not match."),
    ],
    ids=["too-short", "bad-chars", "taken", "short-password", "mismatch"],
)
@pytest.mark.usefixtures("registration_open")
async def test_register_validation(
    anon: httpx.AsyncClient,
    users: dict[str, User],
    db: AsyncSession,
    overrides: dict[str, str],
    message: str,
) -> None:
    form = {
        "username": "candidate",
        "email": "candidate@test.invalid",
        "password": "long-enough",
        "confirm_password": "long-enough",
    } | overrides
    before = (await db.execute(select(func.count(User.id)))).scalar_one()
    r = await anon.post("/register", data=form)
    assert r.status_code == 400
    assert message in r.text
    assert (await db.execute(select(func.count(User.id)))).scalar_one() == before


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@pytest.fixture
async def dashboard_tokens(db: AsyncSession) -> dict[str, Token]:
    """14 live pressings (T01 oldest … T14 newest) plus a deleted and a merged one.

    Recent = T07…T14.  Top rated (outside recent) = T03 (9), T01 (8), T05 (7).
    Genre carousels (outside both) = Jazz [T04, T02], Rock [T06].
    """
    label = Label(name="Blue Note", name_normalised="blue note")
    db.add(label)
    await db.flush()
    spec: dict[int, dict[str, Any]] = {
        1: {"personal_rating": 8.0, "year": 1959},
        2: {"genres": ["Jazz"], "year": 1970},
        3: {"personal_rating": 9.0, "genres": ["Rock"]},
        4: {"genres": ["Jazz", "Rock"], "year": 1980},
        5: {"personal_rating": 7.0},
        6: {"genres": ["Rock"]},
        10: {"genres": ["Rock"]},
        14: {"personal_rating": 10.0, "year": 1999, "label_id": label.id},
    }
    artists = ["Artist A", "Artist B", "Artist C", "Artist D", "Artist E", None]
    tokens: dict[str, Token] = {}
    for i in range(1, 15):
        name = f"T{i:02d}"
        t = Token(
            title=name,
            artist=artists[i % len(artists)],
            media_format=MediaFormat.VINYL if i <= 10 else MediaFormat.CD,
            status=TokenStatus.ACTIVE,
            created_at=T0 + timedelta(minutes=i),
            **spec.get(i, {}),
        )
        db.add(t)
        tokens[name] = t
    # Newest of all and highly rated: must still never show up.
    db.add(
        Token(
            title="Deleted",
            artist="Ghost",
            media_format=MediaFormat.VINYL,
            year=1901,
            personal_rating=10.0,
            genres=["Jazz"],
            deleted_at=T0,
            created_at=T0 + timedelta(days=1),
        )
    )
    db.add(
        Token(
            title="Merged",
            artist="Twin",
            media_format=MediaFormat.CD,
            status=TokenStatus.MERGED,
            year=1902,
            genres=["Jazz"],
            created_at=T0 + timedelta(days=1),
        )
    )
    await db.commit()
    db.add(
        MediaObject(
            token_id=tokens["T13"].id,
            content_hash="ab" * 32,
            bucket="media-originals",
            object_key="ab/ab/cover.png",
            mime_type="image/png",
            size_bytes=10,
            is_primary_cover=True,
        )
    )
    await db.commit()
    return tokens


async def test_dashboard_stats_and_carousels(
    viewer: httpx.AsyncClient, dashboard_tokens: dict[str, Token], db: AsyncSession
) -> None:
    r = await viewer.get("/")
    assert r.status_code == 200
    html = r.text

    assert _stat(html, "Pressings") == "14"
    assert _stat(html, "Vinyl") == "10"
    assert _stat(html, "CD") == "4"
    assert _stat(html, "Artists") == "5"  # distinct, non-null; Ghost/Twin excluded
    assert _stat(html, "Oldest") == "1959"
    assert _stat(html, "Jazz") == "2"
    assert _stat(html, "Rock") == "1"

    sections = _dashboard_sections(html)
    assert list(sections) == ["Recently added", "Top rated", "Jazz", "Rock"]
    assert sections["Recently added"] == [f"T{i:02d}" for i in range(14, 6, -1)]
    assert sections["Top rated"] == ["T03", "T01", "T05"]
    assert sections["Jazz"] == ["T04", "T02"]  # rating, then year desc
    assert sections["Rock"] == ["T06"]
    assert "Deleted" not in html
    assert "Merged" not in html

    assert "1999" in html
    assert "Blue Note" in html
    assert "★ 4.5/5" in html  # T03: 9/10
    cover = (
        await db.execute(
            select(MediaObject.id).where(MediaObject.token_id == dashboard_tokens["T13"].id)
        )
    ).scalar_one()
    assert f'src="/catalogue/{dashboard_tokens["T13"].id}/images/{cover}"' in html


async def test_dashboard_with_empty_catalogue(viewer: httpx.AsyncClient) -> None:
    r = await viewer.get("/")
    assert r.status_code == 200
    for label in ("Pressings", "Vinyl", "CD", "Artists"):
        assert _stat(r.text, label) == "0"
    assert 'class="stat-label">Oldest' not in r.text
    assert "No pressings catalogued yet." in r.text
    assert _dashboard_sections(r.text) == {}


# ---------------------------------------------------------------------------
# Pages survive a database outage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "headers", "expect"),
    [
        ("/", {}, "No pressings catalogued yet."),
        ("/reviews", {}, "No reviews matching this filter."),
        ("/tokens", {}, "No tokens found"),
        ("/search?q=floyd", {}, "No results found"),
        ("/search?q=floyd", {"HX-Request": "true"}, "No results found"),
    ],
    ids=["dashboard", "reviews", "tokens", "search", "search-3panel"],
)
async def test_pages_render_empty_when_the_database_fails(
    app: FastAPI,
    make_client: ClientFactory,
    path: str,
    headers: dict[str, str],
    expect: str,
) -> None:
    client = await make_client("admin")
    client.cookies.set("mc_layout", "3panel")
    app.state.db_session_factory = _BrokenSessionFactory()
    r = await client.get(path, headers=headers)
    assert r.status_code == 200
    assert expect in r.text


async def test_review_detail_is_404_when_the_database_fails(
    app: FastAPI, admin: httpx.AsyncClient
) -> None:
    app.state.db_session_factory = _BrokenSessionFactory()
    assert (await admin.get(f"/reviews/{uuid.uuid4()}")).status_code == 404


async def test_user_list_uses_memory_store_when_the_database_fails(
    app: FastAPI, admin: httpx.AsyncClient
) -> None:
    routes._users_store["boot"] = {
        "id": uuid.uuid4().hex,
        "username": "boot",
        "email": "boot@localhost",
        "password_hash": "secret-hash",
        "role": "admin",
        "is_active": True,
    }
    app.state.db_session_factory = _BrokenSessionFactory()
    r = await admin.get("/users")
    assert r.status_code == 200
    assert "boot@localhost" in r.text
    assert "secret-hash" not in r.text


# ---------------------------------------------------------------------------
# Global search
# ---------------------------------------------------------------------------


@pytest.fixture
async def search_tokens(db: AsyncSession) -> dict[str, Token]:
    tokens = {
        "dsotm": Token(
            title="The Dark Side of the Moon",
            artist="Pink Floyd",
            year=1973,
            media_format=MediaFormat.VINYL,
            catalog_number="SHVL 804",
            tracklist=[
                {"position": "A1", "title": "Speak to Me"},
                {"position": "B1", "title": "Money"},
            ],
        ),
        "wall": Token(
            title="The Wall",
            artist="Pink Floyd",
            year=1979,
            media_format=MediaFormat.CD,
            catalog_number="CDS 7 46036",
            tracklist=[{"position": "1", "title": "Another Brick in the Wall"}],
        ),
        "abba": Token(
            title="Arrival",
            artist="ABBA",
            year=1976,
            media_format=MediaFormat.VINYL,
            catalog_number="POLS 272",
            tracklist=[{"position": "A1", "title": "Money, Money, Money"}],
        ),
        "floyd_title": Token(
            title="Floyd Tribute",
            artist="Various",
            year=2001,
            media_format=MediaFormat.CD,
            catalog_number="100% PURE",
            tracklist={"not": "a list"},  # malformed tracklist must not break track search
        ),
        "deleted": Token(
            title="Money for Nothing",
            artist="Pink Floyd",
            media_format=MediaFormat.VINYL,
            catalog_number="SHVL 999",
            tracklist=[{"position": "A1", "title": "Money"}],
            deleted_at=T0,
        ),
        "merged": Token(
            title="Dark Side duplicate",
            artist="Pink Floyd",
            media_format=MediaFormat.VINYL,
            status=TokenStatus.MERGED,
            tracklist=[{"position": "A1", "title": "Money"}],
        ),
    }
    db.add_all(tokens.values())
    db.add(ArtistBio(artist_name="Pink Floyd", thumbnail_url="https://upload.wikimedia.org/pf.jpg"))
    await db.commit()
    db.add(
        MediaObject(
            token_id=tokens["abba"].id,
            content_hash="cd" * 32,
            bucket="media-originals",
            object_key="cd/cd/arrival.png",
            mime_type="image/png",
            size_bytes=10,
            is_primary_cover=True,
        )
    )
    await db.commit()
    return tokens


def _ids(tokens: dict[str, Token], *names: str) -> list[str]:
    return [str(tokens[n].id) for n in names]


@pytest.mark.parametrize(
    ("q", "scope", "expected"),
    [
        # all = artist OR title OR catalogue no.; sorted by artist then title
        ("floyd", "all", ["dsotm", "wall", "floyd_title"]),
        ("floyd", "artists", ["dsotm", "wall"]),
        ("floyd", "albums", ["floyd_title"]),
        ("shvl", "albums", ["dsotm"]),
        ("money", "tracks", ["dsotm", "abba"]),  # ordered by track title
        ("100%", "all", ["floyd_title"]),  # % matches literally
        ("10_%", "all", []),
        ("floyd", "bogus", ["dsotm", "wall", "floyd_title"]),  # unknown scope → all
    ],
)
async def test_classic_search_scopes(
    viewer: httpx.AsyncClient,
    search_tokens: dict[str, Token],
    q: str,
    scope: str,
    expected: list[str],
) -> None:
    r = await viewer.get("/search", params={"q": q, "scope": scope}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert _result_ids(r.text) == _ids(search_tokens, *expected)
    if not expected:
        assert "No results found" in r.text


async def test_classic_track_hits_show_position_and_album(
    viewer: httpx.AsyncClient, search_tokens: dict[str, Token]
) -> None:
    r = await viewer.get("/search", params={"q": "speak", "scope": "tracks"})
    assert r.status_code == 200
    assert "<strong>A1 Speak to Me</strong>" in r.text
    assert "Pink Floyd — The Dark Side of the Moon" in r.text
    assert re.search(r"\b1 result\s*</div>", r.text)
    # Non-HTMX: full page around the result list.
    assert "<html" in r.text.lower()


async def test_search_without_query_prompts(viewer: httpx.AsyncClient) -> None:
    r = await viewer.get("/search", params={"q": "   "}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Enter a term to search" in r.text
    assert "<html" not in r.text.lower()


@pytest.fixture
async def panel(make_client: ClientFactory) -> httpx.AsyncClient:
    """Viewer in the 3-panel layout sending HTMX requests."""
    client = await make_client("viewer")
    client.cookies.set("mc_layout", "3panel")
    client.headers["HX-Request"] = "true"
    return client


async def test_panel_search_groups_artists_and_albums(
    panel: httpx.AsyncClient, search_tokens: dict[str, Token]
) -> None:
    r = await panel.get("/search", params={"q": "floyd"})
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers
    html = r.text
    assert 'id="sp-left" hx-swap-oob="innerHTML"' in html
    assert _panel_artists(html)[:1] == ["Pink Floyd"]
    assert "2 pressings" in html
    assert "1973–1979" in html
    assert 'src="https://upload.wikimedia.org/pf.jpg"' in html  # bio portrait
    assert _panel_album_ids(html) == _ids(search_tokens, "floyd_title")
    assert 'hx-get="/artists/detail?artist=Pink%20Floyd"' in html  # centre opens first artist
    assert "Money for Nothing" not in html
    assert "duplicate" not in html


async def test_panel_artist_hit_uses_cover_when_no_portrait(
    panel: httpx.AsyncClient, search_tokens: dict[str, Token], db: AsyncSession
) -> None:
    r = await panel.get("/search", params={"q": "abba", "scope": "artists"})
    assert r.status_code == 200
    assert _panel_artists(r.text) == ["ABBA"]
    assert "1 pressing" in r.text
    cover = (
        await db.execute(
            select(MediaObject.id).where(MediaObject.token_id == search_tokens["abba"].id)
        )
    ).scalar_one()
    assert f'src="/catalogue/{search_tokens["abba"].id}/images/{cover}"' in r.text


@pytest.mark.parametrize(
    ("q", "scope", "target"),
    [("arrival", "albums", "abba"), ("pols 272", "all", "abba"), ("speak", "tracks", "dsotm")],
)
async def test_panel_single_pressing_hit_redirects(
    panel: httpx.AsyncClient,
    search_tokens: dict[str, Token],
    q: str,
    scope: str,
    target: str,
) -> None:
    r = await panel.get("/search", params={"q": q, "scope": scope})
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == f"/catalogue/{search_tokens[target].id}"
    assert r.text == ""


async def test_panel_track_search_lists_every_matching_track(
    panel: httpx.AsyncClient, search_tokens: dict[str, Token]
) -> None:
    r = await panel.get("/search", params={"q": "money", "scope": "tracks"})
    assert r.status_code == 200
    # "Money" (Pink Floyd) sorts before "Money, Money, Money" (ABBA).
    assert _panel_album_ids(r.text) == _ids(search_tokens, "dsotm", "abba")
    assert '<span class="sp-hit-pos">B1</span>Money' in r.text
    assert '<span class="sp-hit-pos">A1</span>Money, Money, Money' in r.text
    assert "Speak to Me" not in r.text
    assert 'hx-get="/artists/detail?artist=Pink%20Floyd"' in r.text


async def test_panel_search_without_hits_suggests_tracks(
    panel: httpx.AsyncClient, search_tokens: dict[str, Token]
) -> None:
    r = await panel.get("/search", params={"q": "zeppelin"})
    assert r.status_code == 200
    assert "No artists or albums match" in r.text
    assert "Try the <strong>Tracks</strong> search" in r.text
    assert "HX-Redirect" not in r.headers


async def test_panel_search_caps_album_hits(panel: httpx.AsyncClient, db: AsyncSession) -> None:
    db.add_all(
        Token(title=f"Bootleg {i:03d}", artist=f"Band {i}", media_format=MediaFormat.VINYL)
        for i in range(routes._SEARCH_LIMIT + 1)
    )
    await db.commit()
    r = await panel.get("/search", params={"q": "bootleg", "scope": "albums"})
    assert r.status_code == 200
    assert len(_panel_album_ids(r.text)) == routes._SEARCH_LIMIT
    assert f"Showing the first {routes._SEARCH_LIMIT} albums" in r.text


async def test_search_in_3panel_without_htmx_renders_full_page(
    make_client: ClientFactory, search_tokens: dict[str, Token]
) -> None:
    client = await make_client("viewer")
    client.cookies.set("mc_layout", "3panel")
    r = await client.get("/search", params={"q": "arrival"})
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers
    assert _result_ids(r.text) == _ids(search_tokens, "abba")
    assert 'id="sp-layout-root"' in r.text


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


@pytest.fixture
async def reviews(db: AsyncSession) -> dict[str, ReviewItem]:
    gb = (await db.execute(select(Country).where(Country.alpha2 == "GB"))).scalar_one()
    label = Label(name="Harvest", name_normalised="harvest")
    db.add(label)
    await db.flush()
    t1 = await _add_token(
        db,
        title="Meddle",
        artist="Pink Floyd",
        year=1971,
        country_id=gb.id,
        label_id=label.id,
        catalog_number="SHVL 795",
        matrix_runout="A//1",
    )
    t2 = await _add_token(db, title="Animals", artist="Pink Floyd", media_format=MediaFormat.CD)
    t3 = await _add_token(db, title="Ummagumma", artist="Pink Floyd")
    db.add(
        TokenRevision(
            token_id=t1.id,
            revision_number=1,
            source=RevisionSource.HUMAN,
            data={"title": "Meddle"},
            comment="initial cataloguing",
        )
    )
    await db.commit()
    return {
        "older": await _add_review(db, t2, created_at=T0, reason=ReviewReason.CONFLICT),
        "newer": await _add_review(db, t1, created_at=T0 + timedelta(hours=1)),
        "approved": await _add_review(
            db,
            t3,
            ReviewStatus.APPROVED,
            resolution_comment="matrix verified",
            resolved_at=T0,
        ),
    }


def _review_titles(html: str) -> list[str]:
    return re.findall(r'class="review-item-title">([^<]*)<', html)


async def test_review_list_filters_by_status(
    viewer: httpx.AsyncClient, reviews: dict[str, ReviewItem], db: AsyncSession
) -> None:
    gb = (await db.execute(select(Country.name).where(Country.alpha2 == "GB"))).scalar_one()
    r = await viewer.get("/reviews")
    assert r.status_code == 200
    assert "<h1>Review queue</h1>" in r.text
    assert _review_titles(r.text) == ["Meddle", "Animals"]  # newest first
    assert gb in r.text
    assert "Harvest" in r.text
    assert "conflict" in r.text

    r = await viewer.get("/reviews", params={"status": "approved"})
    assert _review_titles(r.text) == ["Ummagumma"]

    r = await viewer.get("/reviews", params={"status": "rejected"})
    assert "No reviews matching this filter." in r.text

    r = await viewer.get("/reviews", params={"status": "drop table"})
    assert _review_titles(r.text) == ["Meddle", "Animals"]


async def test_review_list_htmx_returns_partial(
    viewer: httpx.AsyncClient, reviews: dict[str, ReviewItem]
) -> None:
    r = await viewer.get("/reviews", params={"status": "approved"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<h1>Review queue</h1>" not in r.text
    assert _review_titles(r.text) == ["Ummagumma"]
    assert f'href="/reviews/{reviews["approved"].id}"' in r.text


async def test_review_detail_for_reviewer_offers_decisions(
    reviewer: httpx.AsyncClient, reviews: dict[str, ReviewItem]
) -> None:
    rid = reviews["newer"].id
    r = await reviewer.get(f"/reviews/{rid}")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Meddle</h1>" in html
    assert "SHVL 795" in html
    assert "A//1" in html
    assert "Rev 1" in html
    assert "initial cataloguing" in html
    assert f'action="/reviews/{rid}/approve"' in html
    assert f'action="/reviews/{rid}/reject"' in html


async def test_review_detail_for_viewer_has_no_decisions(
    viewer: httpx.AsyncClient, reviews: dict[str, ReviewItem]
) -> None:
    rid = reviews["newer"].id
    r = await viewer.get(f"/reviews/{rid}")
    assert r.status_code == 200
    assert "<h1>Meddle</h1>" in r.text
    assert f'action="/reviews/{rid}/approve"' not in r.text


async def test_resolved_review_shows_outcome(
    reviewer: httpx.AsyncClient, reviews: dict[str, ReviewItem]
) -> None:
    rid = reviews["approved"].id
    r = await reviewer.get(f"/reviews/{rid}")
    assert r.status_code == 200
    assert "This review was <strong>approved</strong>" in r.text
    assert "matrix verified" in r.text
    assert f'action="/reviews/{rid}/approve"' not in r.text


@pytest.mark.parametrize("review_id", [str(uuid.uuid4()), "nope"], ids=["unknown", "malformed"])
async def test_review_detail_unknown_is_404(
    viewer: httpx.AsyncClient, reviews: dict[str, ReviewItem], review_id: str
) -> None:
    assert (await viewer.get(f"/reviews/{review_id}")).status_code == 404


@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_approve_resolves_the_review(
    make_client: ClientFactory, reviews: dict[str, ReviewItem], db: AsyncSession, role: str
) -> None:
    client = await make_client(role)
    rid = reviews["newer"].id
    before = datetime.now(UTC)
    r = await client.post(f"/reviews/{rid}/approve", data={"comment": "Looks\r\nfine " + "x" * 300})
    assert r.status_code == 303
    assert r.headers["location"] == "/reviews"

    item = await _review(db, rid)
    assert item.status == ReviewStatus.APPROVED
    assert item.resolution_comment is not None
    assert item.resolution_comment.startswith("Looks  fine x")  # newlines flattened
    assert len(item.resolution_comment) == 200
    assert item.resolved_at is not None
    assert item.resolved_at >= before - timedelta(seconds=5)
    assert (await _review(db, reviews["older"].id)).status == ReviewStatus.PENDING

    listing = await client.get("/reviews")
    assert _review_titles(listing.text) == ["Animals"]


async def test_reject_resolves_the_review(
    reviewer: httpx.AsyncClient, reviews: dict[str, ReviewItem], db: AsyncSession
) -> None:
    rid = reviews["older"].id
    r = await reviewer.post(f"/reviews/{rid}/reject", data={"comment": "wrong pressing"})
    assert r.status_code == 303
    assert r.headers["location"] == "/reviews"
    item = await _review(db, rid)
    assert (item.status, item.resolution_comment) == (ReviewStatus.REJECTED, "wrong pressing")
    assert item.resolved_at is not None


@pytest.mark.parametrize(
    ("action", "status", "fragment"),
    [
        ("approve", ReviewStatus.APPROVED, "Review approved."),
        ("reject", ReviewStatus.REJECTED, "Review rejected."),
    ],
)
async def test_review_decision_over_htmx_returns_alert(
    reviewer: httpx.AsyncClient,
    reviews: dict[str, ReviewItem],
    db: AsyncSession,
    action: str,
    status: ReviewStatus,
    fragment: str,
) -> None:
    rid = reviews["newer"].id
    r = await reviewer.post(f"/reviews/{rid}/{action}", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert fragment in r.text
    item = await _review(db, rid)
    assert item.status == status
    assert item.resolution_comment == ""


@pytest.mark.parametrize("role", ["contributor", "viewer"])
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_review_decisions_need_reviewer_or_admin(
    make_client: ClientFactory,
    reviews: dict[str, ReviewItem],
    db: AsyncSession,
    role: str,
    action: str,
) -> None:
    client = await make_client(role)
    rid = reviews["newer"].id
    r = await client.post(f"/reviews/{rid}/{action}", data={"comment": "sneaky"})
    assert r.status_code == 403
    item = await _review(db, rid)
    assert (item.status, item.resolution_comment, item.resolved_at) == (
        ReviewStatus.PENDING,
        None,
        None,
    )


async def test_review_decision_without_csrf_header_is_refused(
    reviewer: httpx.AsyncClient, reviews: dict[str, ReviewItem], db: AsyncSession
) -> None:
    rid = reviews["newer"].id
    r = await reviewer.post(f"/reviews/{rid}/approve", headers={"X-CSRF-Token": ""})
    assert r.status_code == 403
    assert (await _review(db, rid)).status == ReviewStatus.PENDING


# ---------------------------------------------------------------------------
# Token browser
# ---------------------------------------------------------------------------


async def test_token_list_search_and_filters(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    gb = (await db.execute(select(Country).where(Country.alpha2 == "GB"))).scalar_one()
    keep = {
        "blue": await _add_token(
            db, title="Blue Train", artist="John Coltrane", barcode="0724349", country_id=gb.id
        ),
        "kind": await _add_token(
            db, title="Kind of Blue", artist="Miles Davis", catalog_number="CL 1355"
        ),
        "cd": await _add_token(
            db, title="Bitches Brew", artist="Miles Davis", media_format=MediaFormat.CD
        ),
    }
    await _add_token(db, title="Blue Ghost", artist="Nobody", deleted_at=T0)
    await _add_token(db, title="Blue Twin", artist="Nobody", status=TokenStatus.MERGED)

    async def titles(**params: str) -> list[str]:
        r = await viewer.get("/tokens", params=params)
        assert r.status_code == 200
        return _token_list_titles(r.text)

    # Sorted by artist, then title; deleted and merged hidden.
    assert await titles() == ["Blue Train", "Bitches Brew", "Kind of Blue"]
    assert await titles(q="BLUE") == ["Blue Train", "Kind of Blue"]
    assert await titles(q="davis") == ["Bitches Brew", "Kind of Blue"]
    assert await titles(q="0724") == ["Blue Train"]  # barcode
    assert await titles(q="cl 13") == ["Kind of Blue"]  # catalogue number
    assert await titles(q=str(keep["cd"].id)) == ["Bitches Brew"]
    assert await titles(media="cd") == ["Bitches Brew"]
    assert await titles(media="vinyl", q="miles") == ["Kind of Blue"]
    assert await titles(media="cassette") == ["Blue Train", "Bitches Brew", "Kind of Blue"]

    r = await viewer.get("/tokens", params={"q": "zzz"})
    assert 'No tokens found for "zzz"' in r.text
    r = await viewer.get("/tokens", params={"q": "train"}, headers={"HX-Request": "true"})
    assert "<html" not in r.text.lower()
    assert _token_list_titles(r.text) == ["Blue Train"]
    assert gb.name in r.text


async def test_token_list_paginates(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    db.add_all(
        Token(title=f"Album {i:02d}", artist="Same", media_format=MediaFormat.VINYL)
        for i in range(51)
    )
    await db.commit()
    r = await viewer.get("/tokens")
    assert len(_token_list_titles(r.text)) == 50
    assert "Page 1 of 2" in r.text
    r = await viewer.get("/tokens", params={"page": "2"})
    assert _token_list_titles(r.text) == ["Album 50"]
    assert "Page 2 of 2" in r.text


async def test_token_detail_page_renders_the_id(viewer: httpx.AsyncClient) -> None:
    tid = str(uuid.uuid4())
    r = await viewer.get(f"/tokens/{tid}")
    assert r.status_code == 200
    assert f"<dd>{tid}</dd>" in r.text


# ---------------------------------------------------------------------------
# Layout switch
# ---------------------------------------------------------------------------


async def test_layout_switch_sets_cookie_and_changes_base(admin: httpx.AsyncClient) -> None:
    before = await admin.get("/")
    assert 'id="navbar"' in before.text
    assert 'id="sp-layout-root"' not in before.text

    r = await admin.post("/layout/set", data={"layout": "3panel"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?tab=layout&msg=layout_saved"
    raw = _set_cookie(r, "mc_layout")
    assert raw is not None
    assert raw.startswith("mc_layout=3panel;")
    assert "Max-Age=31536000" in raw
    assert "HttpOnly" not in raw

    after = await admin.get("/")
    assert 'id="sp-layout-root"' in after.text
    assert 'id="navbar"' not in after.text

    r = await admin.post("/layout/set", data={"layout": "<script>"})
    assert admin.cookies["mc_layout"] == "classic"
    assert 'id="navbar"' in (await admin.get("/")).text


async def test_unknown_layout_cookie_falls_back_to_classic(viewer: httpx.AsyncClient) -> None:
    viewer.cookies.set("mc_layout", "fancy")
    r = await viewer.get("/tokens")
    assert 'id="navbar"' in r.text


@pytest.mark.parametrize("role", NON_ADMIN_ROLES)
async def test_layout_switch_is_admin_only(make_client: ClientFactory, role: str) -> None:
    client = await make_client(role)
    r = await client.post("/layout/set", data={"layout": "3panel"})
    assert r.status_code == 403
    assert _set_cookie(r, "mc_layout") is None


# ---------------------------------------------------------------------------
# Account lock on the user row, DB-first login, audit entries
# ---------------------------------------------------------------------------


async def test_repeated_failures_lock_the_account_on_the_user_row(
    app: FastAPI, anon: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    app.state.login_lockout = (3, 900)  # below the rate limiter's 10, so the row lock is what bites
    for _ in range(3):
        assert (await _login(anon, "viewer-user", "wrong")).status_code == 401
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.failed_login_count == 3
    assert row.locked_until is not None
    assert row.locked_until > datetime.now(UTC) + timedelta(minutes=14)

    r = await _login(anon, "viewer-user")  # right password, still locked
    assert r.status_code == 429
    assert _set_cookie(r, SESSION_COOKIE) is None


async def test_expired_lock_lets_the_user_in_and_clears_it(
    anon: httpx.AsyncClient, db: AsyncSession, password_hash: str
) -> None:
    await _add_user(
        db,
        "waited",
        password_hash,
        locked_until=datetime.now(UTC) - timedelta(seconds=1),
        failed_login_count=10,
    )
    assert (await _login(anon, "waited")).status_code == 303
    row = await _user(db, "waited")
    assert row is not None
    assert (row.failed_login_count, row.locked_until) == (0, None)


async def test_soft_deleted_user_cannot_log_in_through_a_memory_copy(
    monkeypatch: pytest.MonkeyPatch, anon: httpx.AsyncClient, db: AsyncSession, password_hash: str
) -> None:
    row = await _add_user(db, "gone", password_hash, deleted_at=datetime.now(UTC))
    monkeypatch.setitem(
        routes._users_store,
        "gone",
        {
            "id": row.id.hex,
            "username": "gone",
            "password_hash": password_hash,
            "role": "admin",
            "is_active": True,
        },
    )
    assert (await _login(anon, "gone")).status_code == 401


async def _audit_rows(db: AsyncSession) -> list[tuple[str, str, Any]]:
    rows = await db.execute(
        select(AuditLog.action, AuditLog.entity_id, AuditLog.user_id).order_by(AuditLog.id)
    )
    return [tuple(r) for r in rows]


async def test_logins_are_audited_with_outcome(
    anon: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    uid = users["viewer"].id
    await _login(anon, "viewer-user", "wrong")
    await _login(anon, "nobody", "x")
    await _login(anon, "viewer-user")
    assert await _audit_rows(db) == [
        ("login.failed", str(uid), None),
        ("login.failed", "nobody", None),
        ("login", str(uid), uid),
    ]


async def test_user_changes_are_audited_with_the_acting_admin(
    admin: httpx.AsyncClient, users: dict[str, User], db: AsyncSession
) -> None:
    uid, admin_id = users["viewer"].id, users["admin"].id
    await admin.post(f"/users/{uid}/edit", data={"role": "reviewer", "is_active": "on"})
    await admin.post(f"/users/{uid}/deactivate")
    await admin.post(f"/users/{uid}/activate")
    rows = await _audit_rows(db)
    assert rows == [
        ("user.update", str(uid), admin_id),
        ("user.deactivate", str(uid), admin_id),
        ("user.activate", str(uid), admin_id),
    ]
    detail = (await db.execute(select(AuditLog.detail).order_by(AuditLog.id))).scalars().first()
    assert detail is not None
    assert detail["role"] == ["viewer", "reviewer"]


@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_deciding_an_unknown_review_is_404(
    reviewer: httpx.AsyncClient, db: AsyncSession, verb: str
) -> None:
    for rid in (uuid.uuid4(), "not-a-uuid"):
        assert (await reviewer.post(f"/reviews/{rid}/{verb}")).status_code == 404
    assert await _audit_rows(db) == []


# ---------------------------------------------------------------------------
# Changing your own password (/account/password)
# ---------------------------------------------------------------------------


def _pw_form(
    current: str = USER_PASSWORD, new: str = "brand-new-pass", confirm: str = ""
) -> dict[str, str]:
    return {"current_password": current, "new_password": new, "confirm_password": confirm or new}


@pytest.mark.parametrize("role", ["admin", "reviewer", "contributor", "viewer"])
async def test_every_role_can_change_their_password(
    make_client: ClientFactory, users: dict[str, User], db: AsyncSession, role: str
) -> None:
    client = await make_client(role)
    page = await client.get("/account/password")
    assert page.status_code == 200
    assert 'name="current_password"' in page.text
    r = await client.post("/account/password", data=_pw_form())
    assert r.status_code == 303
    assert r.headers["location"] == "/account/password?done=1"
    row = await _user(db, users[role].username)
    assert row is not None
    assert verify_password("brand-new-pass", row.password_hash)
    done = await client.get("/account/password?done=1")
    assert "Your password has been changed" in done.text
    anon = await make_client("anon")
    assert (await _login(anon, users[role].username, "brand-new-pass")).status_code == 303


@pytest.mark.parametrize(
    ("form", "message"),
    [
        (_pw_form(current="wrong-one"), "The current password is not correct."),
        (_pw_form(new="short"), "at least 8 characters"),
        (_pw_form(confirm="something-else"), "The new passwords do not match."),
        (_pw_form(new=USER_PASSWORD), "must differ from the current one"),
    ],
    ids=["wrong-current", "too-short", "mismatch", "unchanged"],
)
async def test_password_change_validation(
    viewer: httpx.AsyncClient,
    users: dict[str, User],
    db: AsyncSession,
    form: dict[str, str],
    message: str,
) -> None:
    before = await _user(db, "viewer-user")
    assert before is not None
    old_hash = before.password_hash
    r = await viewer.post("/account/password", data=form)
    assert r.status_code == 400
    assert message in r.text
    row = await _user(db, "viewer-user")
    assert row is not None
    assert row.password_hash == old_hash


async def test_password_change_guesses_are_rate_limited(
    app: FastAPI, viewer: httpx.AsyncClient, users: dict[str, User], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app.state, "login_rate_limiter", LoginRateLimiter(max_attempts=2, window_seconds=900)
    )
    for _ in range(2):
        assert (
            await viewer.post("/account/password", data=_pw_form(current="guess"))
        ).status_code == 400
    r = await viewer.post("/account/password", data=_pw_form())  # right password, but locked
    assert r.status_code == 429
    assert "Too many wrong attempts" in r.text


async def test_password_change_needs_a_login(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/account/password")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_role_chip_links_to_the_password_page(viewer: httpx.AsyncClient) -> None:
    assert '<a href="/account/password" class="nav-role"' in (await viewer.get("/")).text


# ---------------------------------------------------------------------------
# API schema
# ---------------------------------------------------------------------------


async def test_swagger_ui_is_off_and_the_schema_needs_a_login(
    anon: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    r = await anon.get("/api/openapi.json")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert (await viewer.get("/api/openapi.json")).json()["info"]["title"] == "MediaCat"
    assert (await viewer.get("/api/docs")).status_code == 404
