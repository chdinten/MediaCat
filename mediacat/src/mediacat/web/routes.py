"""FastAPI route definitions — health, auth, dashboard, review, tokens, users.

Session and CSRF are handled by :class:`~mediacat.web.middleware.SessionMiddleware`.
Routes access session data via ``request.state.session`` and the CSRF token
via ``request.state.csrf_token``.

Authentication flow:
1. ``GET /login`` renders the login form with a CSRF token.
2. ``POST /login`` verifies credentials (Argon2id), sets a signed session
   cookie, records login in the audit log.
3. ``SessionMiddleware`` validates the cookie on every subsequent request
   and redirects to ``/login`` if invalid.
4. ``GET /logout`` deletes the cookie.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mediacat.web.auth import hash_password, needs_rehash, verify_password

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

health_router = APIRouter(tags=["health"])
auth_router = APIRouter(tags=["auth"])
dashboard_router = APIRouter(tags=["dashboard"])
review_router = APIRouter(tags=["review"])
token_router = APIRouter(tags=["tokens"])
user_router = APIRouter(prefix="/users", tags=["users"])

_templates: Jinja2Templates | None = None


def set_templates(t: Jinja2Templates) -> None:
    """Called by the app factory to set the shared template engine."""
    global _templates  # noqa: PLW0603
    _templates = t


def _tmpl() -> Jinja2Templates:
    if _templates is None:
        msg = "Templates not initialised"
        raise RuntimeError(msg)
    return _templates


# ---------------------------------------------------------------------------
# Template context helper
# ---------------------------------------------------------------------------


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    """Build the Jinja2 template context with CSRF and user info."""
    session = getattr(request.state, "session", None) or {}
    csrf_token = getattr(request.state, "csrf_token", "")
    return {
        "request": request,
        "user": session,
        "csrf_token": csrf_token,
        "is_htmx": request.headers.get("hx-request") == "true",
        **extra,
    }


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For from Caddy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _require_role(request: Request, *roles: str) -> None:
    """Raise 403 if the current user's role is not in the allowed set."""
    user_role = getattr(request.state, "user_role", None)
    if user_role not in roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@health_router.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness probe for Docker / Caddy health checks."""
    return JSONResponse({"status": "ok"})


@health_router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe."""
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Auth — login / logout
# ---------------------------------------------------------------------------

# In-memory user store for bootstrap (before DB is available).
# In production, this is replaced by DB queries.
# Seeded by create-admin CLI or the /users/seed endpoint.
_users_store: dict[str, dict[str, Any]] = {}


def seed_admin(username: str, password: str, email: str = "admin@localhost") -> None:
    """Seed an admin user into the in-memory store.

    Called at startup or via CLI. In production, users live in the DB.
    """
    _users_store[username] = {
        "id": uuid.uuid4().hex,
        "username": username,
        "email": email,
        "password_hash": hash_password(password),
        "role": "admin",
        "is_active": True,
        "failed_login_count": 0,
        "locked_until": None,
    }
    logger.info("Seeded admin user: %s", username)


# Seed a default admin ONLY in dev mode (controlled by env var).
# In production, use the /users/seed endpoint or the create-admin CLI.

if os.environ.get("MEDIACAT_ENV", "dev") == "dev":
    _dev_pw = os.environ.get("MEDIACAT_DEV_ADMIN_PASSWORD", "")
    if _dev_pw:
        seed_admin("admin", _dev_pw)
        logger.info("Dev admin seeded from MEDIACAT_DEV_ADMIN_PASSWORD")
    else:
        logger.warning(
            "MEDIACAT_ENV=dev but MEDIACAT_DEV_ADMIN_PASSWORD not set; "
            "no default admin created. Set the env var or use /users/seed."
        )


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the login form."""
    return _tmpl().TemplateResponse(
        request=request,
        name="login.html",
        context=_ctx(request, error=None),
    )


@auth_router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse | HTMLResponse:
    """Authenticate user, set session cookie, redirect to dashboard."""
    rate_limiter = request.app.state.login_rate_limiter
    session_mgr = request.app.state.session_manager
    ip = _client_ip(request)

    # Rate limiting by username and IP
    if rate_limiter.is_locked(username) or rate_limiter.is_locked(ip):
        return _tmpl().TemplateResponse(
            request=request,
            name="login.html",
            context=_ctx(request, error="Account temporarily locked. Try again later."),
            status_code=429,
        )

    # Lookup user
    user = _users_store.get(username)
    if not user or not user.get("is_active", False):
        rate_limiter.record_failure(username)
        rate_limiter.record_failure(ip)
        return _tmpl().TemplateResponse(
            request=request,
            name="login.html",
            context=_ctx(request, error="Invalid username or password."),
            status_code=401,
        )

    # Verify password
    if not verify_password(password, user["password_hash"]):
        rate_limiter.record_failure(username)
        rate_limiter.record_failure(ip)
        user["failed_login_count"] = user.get("failed_login_count", 0) + 1
        logger.warning("Failed login for user=%s from ip=%s", username, ip)
        return _tmpl().TemplateResponse(
            request=request,
            name="login.html",
            context=_ctx(request, error="Invalid username or password."),
            status_code=401,
        )

    # Check if password needs rehash (params changed)
    if needs_rehash(user["password_hash"]):
        user["password_hash"] = hash_password(password)

    # Successful login
    rate_limiter.clear(username)
    rate_limiter.clear(ip)
    user["failed_login_count"] = 0
    user["last_login_at"] = datetime.now(UTC).isoformat()

    # Create session cookie
    session_token = session_mgr.create_session(user["id"], user["role"])
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=session_mgr.cookie_name,
        value=session_token,
        httponly=True,
        secure=session_mgr.cookie_secure,
        samesite="lax",
        max_age=86400,
    )

    logger.info("Successful login: user=%s role=%s ip=%s", username, user["role"], ip)
    return response


@auth_router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear the session cookie and redirect to login."""
    session_mgr = request.app.state.session_manager
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(session_mgr.cookie_name)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@dashboard_router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Main dashboard."""
    context = _ctx(
        request,
        pending_count=0,
        recent_jobs=[],
        stats={"tokens": len(_users_store), "reviews_pending": 0, "jobs_running": 0},
    )
    return _tmpl().TemplateResponse(request=request, name="dashboard.html", context=context)


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------


@review_router.get("/reviews", response_class=HTMLResponse)
async def review_list(
    request: Request,
    status_filter: str = Query("pending", alias="status"),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    """List review items, filterable by status."""
    context = _ctx(
        request,
        reviews=[],
        current_status=status_filter,
        page=page,
        total_pages=1,
    )
    template = "partials/review_list.html" if _is_htmx(request) else "reviews.html"
    return _tmpl().TemplateResponse(request=request, name=template, context=context)


@review_router.get("/reviews/{review_id}", response_class=HTMLResponse)
async def review_detail(request: Request, review_id: str) -> HTMLResponse:
    """Show a single review item with diff view and action buttons."""
    context = _ctx(
        request,
        review={"id": review_id, "status": "pending"},
        token=None,
        revisions=[],
    )
    return _tmpl().TemplateResponse(request=request, name="review_detail.html", context=context)


@review_router.post("/reviews/{review_id}/approve", response_model=None)
async def review_approve(
    request: Request,
    review_id: str,
    comment: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Approve a review item."""
    _require_role(request, "admin", "reviewer")
    user_id = request.state.user_id
    safe_comment = comment.replace("\n", " ").replace("\r", " ")[:200]
    logger.info("Review %s approved by user=%s comment=%s", review_id, user_id, safe_comment)

    if _is_htmx(request):
        return HTMLResponse('<div class="alert alert-success">Review approved.</div>')
    return RedirectResponse(url="/reviews", status_code=303)


@review_router.post("/reviews/{review_id}/reject", response_model=None)
async def review_reject(
    request: Request,
    review_id: str,
    comment: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Reject a review item."""
    _require_role(request, "admin", "reviewer")
    user_id = request.state.user_id
    safe_comment = comment.replace("\n", " ").replace("\r", " ")[:200]
    logger.info("Review %s rejected by user=%s comment=%s", review_id, user_id, safe_comment)

    if _is_htmx(request):
        return HTMLResponse('<div class="alert alert-warning">Review rejected.</div>')
    return RedirectResponse(url="/reviews", status_code=303)


# ---------------------------------------------------------------------------
# Token browser
# ---------------------------------------------------------------------------


@token_router.get("/tokens", response_class=HTMLResponse)
async def token_list(
    request: Request,
    q: str = Query("", description="Search query"),
    media: str = Query("", description="Filter by media format"),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    """Browse the token registry with search and filters."""
    context = _ctx(
        request,
        tokens=[],
        query=q,
        media_filter=media,
        page=page,
        total_pages=1,
    )
    template = "partials/token_list.html" if _is_htmx(request) else "tokens.html"
    return _tmpl().TemplateResponse(request=request, name=template, context=context)


@token_router.get("/tokens/{token_id}", response_class=HTMLResponse)
async def token_detail(request: Request, token_id: str) -> HTMLResponse:
    """Show a token with its revision history and media objects."""
    context = _ctx(
        request,
        token={"id": token_id},
        revisions=[],
        media_objects=[],
    )
    return _tmpl().TemplateResponse(request=request, name="token_detail.html", context=context)


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------


@user_router.get("", response_class=HTMLResponse)
async def user_list(request: Request) -> HTMLResponse:
    """List all users (admin only)."""
    _require_role(request, "admin")
    users = [{**u, "password_hash": "***"} for u in _users_store.values()]
    context = _ctx(request, users=users)
    return _tmpl().TemplateResponse(request=request, name="users.html", context=context)


@user_router.get("/new", response_class=HTMLResponse)
async def user_create_form(request: Request) -> HTMLResponse:
    """Render the create-user form (admin only)."""
    _require_role(request, "admin")
    context = _ctx(request, error=None, roles=["admin", "reviewer", "viewer"])
    return _tmpl().TemplateResponse(request=request, name="user_form.html", context=context)


@user_router.post("/new", response_model=None)
async def user_create_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
) -> RedirectResponse | HTMLResponse:
    """Create a new user (admin only)."""
    _require_role(request, "admin")

    if username in _users_store:
        context = _ctx(
            request,
            error=f"Username '{username}' already exists.",
            roles=["admin", "reviewer", "viewer"],
        )
        return _tmpl().TemplateResponse(
            request=request,
            name="user_form.html",
            context=context,
            status_code=409,
        )

    if len(password) < 8:
        context = _ctx(
            request,
            error="Password must be at least 8 characters.",
            roles=["admin", "reviewer", "viewer"],
        )
        return _tmpl().TemplateResponse(
            request=request,
            name="user_form.html",
            context=context,
            status_code=400,
        )

    _users_store[username] = {
        "id": uuid.uuid4().hex,
        "username": username,
        "email": email,
        "password_hash": hash_password(password),
        "role": role if role in ("admin", "reviewer", "viewer") else "viewer",
        "is_active": True,
        "failed_login_count": 0,
        "locked_until": None,
    }
    logger.info("User created: %s role=%s by=%s", username, role, request.state.user_id)
    return RedirectResponse(url="/users", status_code=303)


@user_router.post("/{user_id}/deactivate")
async def user_deactivate(request: Request, user_id: str) -> RedirectResponse:
    """Deactivate a user (admin only)."""
    _require_role(request, "admin")

    for user in _users_store.values():
        if user["id"] == user_id:
            user["is_active"] = False
            logger.info("User deactivated: %s by=%s", user["username"], request.state.user_id)
            break

    return RedirectResponse(url="/users", status_code=303)


@user_router.post("/{user_id}/activate")
async def user_activate(request: Request, user_id: str) -> RedirectResponse:
    """Reactivate a user (admin only)."""
    _require_role(request, "admin")

    for user in _users_store.values():
        if user["id"] == user_id:
            user["is_active"] = True
            logger.info("User activated: %s by=%s", user["username"], request.state.user_id)
            break

    return RedirectResponse(url="/users", status_code=303)
