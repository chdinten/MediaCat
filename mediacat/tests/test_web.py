"""Tests for :mod:`mediacat.web`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediacat.web.app import create_app
from mediacat.web.auth import (
    CsrfProtection,
    LoginRateLimiter,
    SessionManager,
    hash_password,
    needs_rehash,
    verify_password,
)
from mediacat.web.routes import seed_admin

# Test password — never used outside tests
_TEST_ADMIN_PW = "test-admin-pw-x9k2m"

# ===========================================================================
# Auth — password hashing
# ===========================================================================


def test_hash_and_verify_password() -> None:
    pw = "correct-horse-battery-staple"
    hashed = hash_password(pw)
    assert hashed != pw
    assert verify_password(pw, hashed)


def test_verify_wrong_password() -> None:
    hashed = hash_password("right")
    assert not verify_password("wrong", hashed)


def test_verify_invalid_hash() -> None:
    assert not verify_password("anything", "not-a-real-hash")


def test_needs_rehash_fresh() -> None:
    hashed = hash_password("test")
    assert not needs_rehash(hashed)


# ===========================================================================
# Auth — session manager
# ===========================================================================


def test_session_create_and_validate() -> None:
    sm = SessionManager("test-secret-key-1234567890abcdef")
    token = sm.create_session("user-123", "reviewer")
    result = sm.validate_session(token)
    assert result is not None
    assert result["user_id"] == "user-123"
    assert result["role"] == "reviewer"


def test_session_invalid_token() -> None:
    sm = SessionManager("test-secret-key-1234567890abcdef")
    assert sm.validate_session("garbage") is None


@pytest.mark.slow
def test_session_expired() -> None:
    sm = SessionManager("test-secret-key-1234567890abcdef", max_age_seconds=1)
    token = sm.create_session("user-123", "admin")
    import time

    time.sleep(1.5)
    assert sm.validate_session(token) is None


# ===========================================================================
# Auth — CSRF
# ===========================================================================


def test_csrf_generate_and_validate() -> None:
    csrf = CsrfProtection("csrf-secret-key")
    token = csrf.generate_token("session-abc")
    assert csrf.validate_token(token, "session-abc")


def test_csrf_wrong_session() -> None:
    csrf = CsrfProtection("csrf-secret-key")
    token = csrf.generate_token("session-abc")
    assert not csrf.validate_token(token, "session-xyz")


def test_csrf_tampered_token() -> None:
    csrf = CsrfProtection("csrf-secret-key")
    assert not csrf.validate_token("tampered", "session-abc")


# ===========================================================================
# Auth — login rate limiter
# ===========================================================================


def test_login_rate_limiter_allows_under_threshold() -> None:
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    rl.record_failure("user1")
    rl.record_failure("user1")
    assert not rl.is_locked("user1")


def test_login_rate_limiter_locks_at_threshold() -> None:
    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(3):
        rl.record_failure("user1")
    assert rl.is_locked("user1")


def test_login_rate_limiter_clear() -> None:
    rl = LoginRateLimiter(max_attempts=2, window_seconds=60)
    rl.record_failure("user1")
    rl.record_failure("user1")
    assert rl.is_locked("user1")
    rl.clear("user1")
    assert not rl.is_locked("user1")


def test_login_rate_limiter_unknown_user_not_locked() -> None:
    rl = LoginRateLimiter(max_attempts=3)
    assert not rl.is_locked("nobody")


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def client() -> TestClient:
    seed_admin("admin", _TEST_ADMIN_PW)
    app = create_app()
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    """A TestClient with a valid session cookie (admin)."""
    csrf = client.app.state.csrf_protection  # type: ignore[union-attr]
    csrf_token = csrf.generate_token("anonymous")

    resp = client.post(
        "/login",
        data={"username": "admin", "password": _TEST_ADMIN_PW, "csrf_token": csrf_token},
    )
    assert resp.status_code == 303
    assert "mediacat_session" in resp.cookies

    # The TestClient auto-persists cookies from responses, so the session
    # cookie is already set. Clear any duplicates from the jar.
    session_val = resp.cookies["mediacat_session"]
    client.cookies.clear()
    client.cookies.set("mediacat_session", session_val)
    return client


def _get_csrf(auth_client: TestClient) -> str:
    """Compute the CSRF token for the current session cookie."""
    csrf = auth_client.app.state.csrf_protection  # type: ignore[union-attr]
    # Iterate the cookie jar to get the value without CookieConflict
    for cookie in auth_client.cookies.jar:
        if cookie.name == "mediacat_session":
            return csrf.generate_token(cookie.value)
    return ""


# ===========================================================================
# Routes — health (public, no auth required)
# ===========================================================================


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200


# ===========================================================================
# Routes — auth flow
# ===========================================================================


def test_login_page_renders(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert 'name="csrf_token"' in resp.text


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_with_valid_credentials(client: TestClient) -> None:
    csrf = client.app.state.csrf_protection  # type: ignore[union-attr]
    csrf_token = csrf.generate_token("anonymous")
    resp = client.post(
        "/login",
        data={"username": "admin", "password": _TEST_ADMIN_PW, "csrf_token": csrf_token},
    )
    assert resp.status_code == 303
    assert "mediacat_session" in resp.cookies


def test_login_with_wrong_password(client: TestClient) -> None:
    csrf = client.app.state.csrf_protection  # type: ignore[union-attr]
    csrf_token = csrf.generate_token("anonymous")
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": csrf_token},
    )
    assert resp.status_code == 401
    assert "Invalid" in resp.text


def test_login_with_nonexistent_user(client: TestClient) -> None:
    csrf = client.app.state.csrf_protection  # type: ignore[union-attr]
    csrf_token = csrf.generate_token("anonymous")
    resp = client.post(
        "/login",
        data={"username": "nobody", "password": "test", "csrf_token": csrf_token},
    )
    assert resp.status_code == 401


def test_logout_clears_cookie(auth_client: TestClient) -> None:
    resp = auth_client.get("/logout")
    assert resp.status_code == 303


# ===========================================================================
# Routes — authenticated pages
# ===========================================================================


def test_dashboard_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


def test_reviews_page_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/reviews")
    assert resp.status_code == 200
    assert "Review queue" in resp.text


def test_reviews_htmx_partial(auth_client: TestClient) -> None:
    resp = auth_client.get("/reviews", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "<!DOCTYPE" not in resp.text


def test_review_detail_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/reviews/test-review-id")
    assert resp.status_code == 200


def test_tokens_page_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/tokens")
    assert resp.status_code == 200
    assert "Token registry" in resp.text


def test_token_detail_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/tokens/test-token-id")
    assert resp.status_code == 200


# ===========================================================================
# Routes — review actions with CSRF
# ===========================================================================


def test_approve_with_valid_csrf(auth_client: TestClient) -> None:
    csrf_token = _get_csrf(auth_client)
    resp = auth_client.post(
        "/reviews/test-id/approve",
        data={"comment": "looks good"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code in (200, 303)


def test_approve_without_csrf_rejected(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/reviews/test-id/approve",
        data={"comment": "ok"},
        headers={"X-CSRF-Token": "invalid"},
    )
    assert resp.status_code == 403


# ===========================================================================
# Routes — user management (admin only)
# ===========================================================================


def test_user_list_admin_only(auth_client: TestClient) -> None:
    resp = auth_client.get("/users")
    assert resp.status_code == 200
    assert "admin" in resp.text


def test_user_create_form(auth_client: TestClient) -> None:
    resp = auth_client.get("/users/new")
    assert resp.status_code == 200
    assert "Create user" in resp.text


def test_user_create_submit(auth_client: TestClient) -> None:
    csrf_token = _get_csrf(auth_client)
    resp = auth_client.post(
        "/users/new",
        data={
            "username": "reviewer1",
            "email": "rev@test.com",
            "password": "securepass123",
            "role": "reviewer",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303


def test_user_create_short_password(auth_client: TestClient) -> None:
    csrf_token = _get_csrf(auth_client)
    resp = auth_client.post(
        "/users/new",
        data={
            "username": "badpw",
            "email": "bp@test.com",
            "password": "short",
            "role": "viewer",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 400
    assert "8 characters" in resp.text


def test_user_create_duplicate(auth_client: TestClient) -> None:
    csrf_token = _get_csrf(auth_client)
    resp = auth_client.post(
        "/users/new",
        data={
            "username": "admin",
            "email": "dup@test.com",
            "password": "password123",
            "role": "viewer",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 409


# ===========================================================================
# Middleware — security headers
# ===========================================================================


def test_security_headers_present(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert "content-security-policy" in resp.headers
    assert "x-content-type-options" in resp.headers
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" in resp.headers
    assert resp.headers["x-frame-options"] == "DENY"
    assert "x-request-id" in resp.headers


def test_request_id_propagated(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"X-Request-ID": "custom-rid-123"})
    assert resp.headers["x-request-id"] == "custom-rid-123"


def test_request_id_generated_when_missing(client: TestClient) -> None:
    resp = client.get("/healthz")
    rid = resp.headers["x-request-id"]
    assert len(rid) == 32
