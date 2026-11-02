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

import ipaddress
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

# Fixed namespace for deterministic dev-admin UUIDs.
# Same username → same UUID on every restart → FK on token_revisions always resolves.
_DEV_USER_NAMESPACE = uuid.UUID("b3e2f1a0-cafe-4321-beef-000000000000")

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
search_router = APIRouter(tags=["search"])

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


_LAYOUT_COOKIE = "mc_layout"
_VALID_LAYOUTS = {"classic", "3panel"}


def _layout_base(request: Request) -> str:
    """Return the base template name based on the mc_layout cookie."""
    layout = request.cookies.get(_LAYOUT_COOKIE, "classic")
    if layout not in _VALID_LAYOUTS:
        layout = "classic"
    return "base_3panel.html" if layout == "3panel" else "base.html"


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    """Build the Jinja2 template context with CSRF, user info, and layout selection."""
    session = getattr(request.state, "session", None) or {}
    csrf_token = getattr(request.state, "csrf_token", "")
    return {
        "request": request,
        "user": session,
        "csrf_token": csrf_token,
        "is_htmx": request.headers.get("hx-request") == "true",
        "layout_base": _layout_base(request),
        **extra,
    }


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _client_ip(request: Request) -> str:
    """Extract client IP, only trusting X-Forwarded-For from known proxy CIDRs."""
    connecting = request.client.host if request.client else None
    if connecting:
        trusted_nets = getattr(request.app.state, "trusted_proxy_networks", [])
        try:
            addr = ipaddress.ip_address(connecting)
            if any(addr in net for net in trusted_nets):
                forwarded = request.headers.get("x-forwarded-for")
                if forwarded:
                    return forwarded.split(",")[0].strip()
        except ValueError:
            pass
    return connecting or "unknown"


def _same_user_id(a: str, b: str) -> bool:
    """Compare user ids however they are spelt (the memory store keeps ``.hex``, URLs dashes)."""
    try:
        return uuid.UUID(str(a)) == uuid.UUID(str(b))
    except ValueError:
        return a == b


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
        "id": uuid.uuid5(_DEV_USER_NAMESPACE, username).hex,
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
    return await _login_response(request)


async def _login_response(
    request: Request, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    from mediacat.web.background import page_background

    return _tmpl().TemplateResponse(
        request=request,
        name="login.html",
        context=_ctx(request, error=error, background=await page_background(request)),
        status_code=status_code,
    )


@auth_router.post("/login", response_model=None)
async def login_submit(  # noqa: PLR0915 - linear auth flow (rate limit, lockout, audit)
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse | HTMLResponse:
    """Authenticate user, set session cookie, redirect to dashboard."""
    rate_limiter = request.app.state.login_rate_limiter
    session_mgr = request.app.state.session_manager
    ip = _client_ip(request)

    # Rate limiting by username and IP
    if await rate_limiter.is_locked(username) or await rate_limiter.is_locked(ip):
        return await _login_response(request, "Account temporarily locked. Try again later.", 429)

    # Lookup user — check in-memory bootstrap store first, then DB
    user: dict[str, Any] | None = _users_store.get(username)
    if user is None:
        try:
            from sqlalchemy import select as _sel

            from mediacat.db.models import User as _UModel

            sf = getattr(request.app.state, "db_session_factory", None)
            if sf:
                async with sf() as _db:
                    _row = (
                        await _db.execute(
                            _sel(_UModel).where(
                                _UModel.username == username,
                                _UModel.deleted_at.is_(None),
                            )
                        )
                    ).scalar_one_or_none()
                    if _row:
                        user = {
                            "id": str(_row.id),
                            "username": _row.username,
                            "email": _row.email,
                            "password_hash": _row.password_hash,
                            "role": str(_row.role),
                            "is_active": _row.is_active,
                            "session_timeout_seconds": getattr(
                                _row, "session_timeout_seconds", 86400
                            ),
                            "failed_login_count": _row.failed_login_count or 0,
                            "locked_until": _row.locked_until,
                            "_db_id": str(_row.id),
                        }
        except Exception as _exc:
            logger.warning("DB user lookup failed during login: %s", _exc)

    if not user or not user.get("is_active", False):
        await rate_limiter.record_failure(username)
        await rate_limiter.record_failure(ip)
        return await _login_response(request, "Invalid username or password.", 401)

    # Verify password
    if not verify_password(password, user["password_hash"]):
        await rate_limiter.record_failure(username)
        await rate_limiter.record_failure(ip)
        user["failed_login_count"] = user.get("failed_login_count", 0) + 1
        logger.warning("Failed login for user=%s from ip=%s", username, ip)
        return await _login_response(request, "Invalid username or password.", 401)

    # Check if password needs rehash (params changed)
    new_hash = hash_password(password) if needs_rehash(user["password_hash"]) else None

    # Successful login
    await rate_limiter.clear(username)
    await rate_limiter.clear(ip)
    user["failed_login_count"] = 0
    user["last_login_at"] = datetime.now(UTC).isoformat()
    if new_hash:
        user["password_hash"] = new_hash

    # Persist last_login_at (and optional rehash) to DB
    try:
        from sqlalchemy import select as _sel
        from sqlalchemy import update as _upd

        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                _vals: dict[str, Any] = {
                    "last_login_at": datetime.now(UTC),
                    "failed_login_count": 0,
                }
                if new_hash:
                    _vals["password_hash"] = new_hash
                await _db.execute(_upd(_UModel).where(_UModel.username == username).values(**_vals))
                await _db.commit()
    except Exception as _exc:
        logger.debug("DB last_login_at update skipped: %s", _exc)

    # Create session cookie — respect per-user session timeout
    session_timeout = user.get("session_timeout_seconds", 86400)
    session_token = session_mgr.create_session(user["id"], user["role"])
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=session_mgr.cookie_name,
        value=session_token,
        httponly=True,
        secure=session_mgr.cookie_secure,
        samesite="lax",
        max_age=session_timeout,
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
    from mediacat.web.background import page_background

    stats: dict[str, Any] = {"total": 0, "vinyl": 0, "cd": 0, "artists": 0, "oldest_year": None}
    recent: list[Any] = []
    top_rated: list[Any] = []
    genre_carousels: list[Any] = []
    genre_stats: list[Any] = []
    try:
        from sqlalchemy import func, select

        from mediacat.db.enums import MediaFormat, TokenStatus
        from mediacat.db.models import Token

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as db:
                base_filter = [
                    Token.deleted_at.is_(None),
                    Token.status != TokenStatus.MERGED,
                ]
                stats["total"] = (
                    await db.execute(select(func.count(Token.id)).where(*base_filter))
                ).scalar_one()
                stats["vinyl"] = (
                    await db.execute(
                        select(func.count(Token.id)).where(
                            *base_filter, Token.media_format == MediaFormat.VINYL
                        )
                    )
                ).scalar_one()
                stats["cd"] = (
                    await db.execute(
                        select(func.count(Token.id)).where(
                            *base_filter, Token.media_format == MediaFormat.CD
                        )
                    )
                ).scalar_one()
                stats["artists"] = (
                    await db.execute(
                        select(func.count(func.distinct(Token.artist))).where(
                            *base_filter, Token.artist.isnot(None)
                        )
                    )
                ).scalar_one()
                from sqlalchemy.orm import selectinload

                recent = list(
                    (
                        await db.execute(
                            select(Token)
                            .options(
                                selectinload(Token.label),
                                selectinload(Token.media_objects),
                            )
                            .where(*base_filter)
                            .order_by(Token.created_at.desc())
                            .limit(8)
                        )
                    )
                    .scalars()
                    .all()
                )
                _recent_ids = {t.id for t in recent}
                top_rated = list(
                    (
                        await db.execute(
                            select(Token)
                            .options(
                                selectinload(Token.label),
                                selectinload(Token.media_objects),
                            )
                            .where(
                                *base_filter,
                                Token.personal_rating.isnot(None),
                                Token.id.notin_(_recent_ids),
                            )
                            .order_by(Token.personal_rating.desc())
                            .limit(8)
                        )
                    )
                    .scalars()
                    .all()
                )

                # Genre carousels: one query, group by primary genre in Python.
                # Ordered by rating then year so each genre carousel is sorted best-first.
                tagged = list(
                    (
                        await db.execute(
                            select(Token)
                            .options(
                                selectinload(Token.label),
                                selectinload(Token.media_objects),
                            )
                            .where(*base_filter, Token.genres.isnot(None))
                            .order_by(
                                Token.personal_rating.desc().nulls_last(),
                                Token.year.desc().nulls_last(),
                                Token.title.asc(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                from collections import defaultdict

                _by_genre: dict[str, list[Any]] = defaultdict(list)
                _dedup_ids = {t.id for t in recent} | {t.id for t in top_rated}
                for t in tagged:
                    if t.genres and t.id not in _dedup_ids:
                        _by_genre[t.genres[0]].append(t)
                # Sort genres by album count descending; cap at 12 carousels
                _sorted_genres = sorted(_by_genre.items(), key=lambda kv: -len(kv[1]))
                genre_carousels = [
                    {"genre": g, "tokens": ts[:8]} for g, ts in _sorted_genres if ts
                ][:12]
                genre_stats = [{"genre": g, "count": len(ts)} for g, ts in _sorted_genres if ts][:5]
                stats["oldest_year"] = (
                    await db.execute(
                        select(func.min(Token.year)).where(*base_filter, Token.year.isnot(None))
                    )
                ).scalar_one()

    except Exception as exc:
        logger.warning("Dashboard DB query failed: %s", exc)

    context = _ctx(
        request,
        stats=stats,
        recent=recent,
        top_rated=top_rated,
        genre_carousels=genre_carousels,
        genre_stats=genre_stats,
        background=await page_background(request),
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
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from mediacat.db.enums import ReviewStatus
    from mediacat.db.models import ReviewItem, Token

    reviews: list[Any] = []
    valid_statuses = {s.value for s in ReviewStatus}
    if status_filter not in valid_statuses:
        status_filter = "pending"
    try:
        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as db:
                reviews = list(
                    (
                        await db.execute(
                            select(ReviewItem)
                            .options(
                                selectinload(ReviewItem.token).options(
                                    selectinload(Token.media_objects),
                                    selectinload(Token.country),
                                    selectinload(Token.label),
                                )
                            )
                            .where(ReviewItem.status == ReviewStatus(status_filter))
                            .order_by(ReviewItem.created_at.desc())
                            .limit(50)
                        )
                    )
                    .scalars()
                    .all()
                )
    except Exception as exc:
        logger.warning("Review list DB query failed: %s", exc)

    context = _ctx(
        request,
        reviews=reviews,
        current_status=status_filter,
        page=page,
        total_pages=1,
    )
    template = "partials/review_list.html" if _is_htmx(request) else "reviews.html"
    return _tmpl().TemplateResponse(request=request, name=template, context=context)


@review_router.get("/reviews/{review_id}", response_class=HTMLResponse)
async def review_detail(request: Request, review_id: str) -> HTMLResponse:
    """Show a single review item with criteria checklist and action buttons."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from mediacat.db.models import ReviewItem, Token

    try:
        rid = uuid.UUID(review_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Review not found") from None

    review = None
    token = None
    revisions: list[Any] = []
    try:
        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as db:
                review = (
                    await db.execute(
                        select(ReviewItem)
                        .options(
                            selectinload(ReviewItem.token).options(
                                selectinload(Token.media_objects),
                                selectinload(Token.revisions),
                                selectinload(Token.country),
                                selectinload(Token.label),
                            )
                        )
                        .where(ReviewItem.id == rid)
                    )
                ).scalar_one_or_none()
                if review:
                    token = review.token
                    revisions = list(reversed(token.revisions)) if token else []
    except Exception as exc:
        logger.warning("Review detail DB query failed: %s", exc)

    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    context = _ctx(
        request,
        review=review,
        token=token,
        revisions=revisions,
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

    from sqlalchemy import select

    from mediacat.db.enums import ReviewStatus
    from mediacat.db.models import ReviewItem

    try:
        rid = uuid.UUID(review_id)
        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as db:
                item = (
                    await db.execute(select(ReviewItem).where(ReviewItem.id == rid))
                ).scalar_one_or_none()
                if item:
                    item.status = ReviewStatus.APPROVED
                    item.resolution_comment = safe_comment
                    item.resolved_at = datetime.now(UTC)
                    await db.commit()
    except Exception as exc:
        logger.warning("Review approve DB update failed: %s", exc)

    logger.info("Review %s approved by user=%s", review_id, user_id)
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

    from sqlalchemy import select

    from mediacat.db.enums import ReviewStatus
    from mediacat.db.models import ReviewItem

    try:
        rid = uuid.UUID(review_id)
        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as db:
                item = (
                    await db.execute(select(ReviewItem).where(ReviewItem.id == rid))
                ).scalar_one_or_none()
                if item:
                    item.status = ReviewStatus.REJECTED
                    item.resolution_comment = safe_comment
                    item.resolved_at = datetime.now(UTC)
                    await db.commit()
    except Exception as exc:
        logger.warning("Review reject DB update failed: %s", exc)

    logger.info("Review %s rejected by user=%s", review_id, user_id)
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
    from sqlalchemy import func, or_, select
    from sqlalchemy.orm import selectinload

    from mediacat.db.enums import MediaFormat, TokenStatus
    from mediacat.db.models import Token

    page_size = 50
    offset = (page - 1) * page_size
    tokens: list[Any] = []
    total_pages = 1

    try:
        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as db:
                base = (
                    select(Token)
                    .options(selectinload(Token.label), selectinload(Token.country))
                    .where(Token.deleted_at.is_(None))
                    .where(Token.status != TokenStatus.MERGED)
                )
                if q:
                    try:
                        token_uuid = uuid.UUID(q.strip())
                        base = base.where(Token.id == token_uuid)
                    except ValueError:
                        base = base.where(
                            or_(
                                Token.title.ilike(f"%{q}%"),
                                Token.artist.ilike(f"%{q}%"),
                                Token.barcode.ilike(f"%{q}%"),
                                Token.catalog_number.ilike(f"%{q}%"),
                            )
                        )
                if media in ("vinyl", "cd"):
                    base = base.where(Token.media_format == MediaFormat(media))

                total: int = (
                    await db.execute(select(func.count()).select_from(base.subquery()))
                ).scalar_one()
                total_pages = max(1, (total + page_size - 1) // page_size)

                tokens = list(
                    (
                        await db.execute(
                            base.order_by(Token.artist.asc(), Token.title.asc())
                            .offset(offset)
                            .limit(page_size)
                        )
                    )
                    .scalars()
                    .all()
                )
    except Exception as exc:
        logger.warning("Token list DB query failed: %s", exc)

    context = _ctx(
        request,
        tokens=tokens,
        query=q,
        media_filter=media,
        page=page,
        total_pages=total_pages,
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


def _user_row_to_dict(u: Any) -> dict[str, Any]:
    """Convert a DB User ORM row to the dict shape expected by templates."""
    return {
        "id": str(u.id),
        "username": u.username,
        "email": u.email,
        "role": str(u.role),
        "is_active": u.is_active,
        "session_timeout_seconds": getattr(u, "session_timeout_seconds", 86400),
        "last_login_at": (u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else None),
        "failed_login_count": u.failed_login_count or 0,
    }


@user_router.get("", response_class=HTMLResponse)
async def user_list(request: Request) -> HTMLResponse:
    """List all users (admin only)."""
    _require_role(request, "admin")
    try:
        from sqlalchemy import select as _sel

        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if not sf:
            raise RuntimeError("no db")
        async with sf() as _db:
            rows = (
                (
                    await _db.execute(
                        _sel(_UModel).where(_UModel.deleted_at.is_(None)).order_by(_UModel.username)
                    )
                )
                .scalars()
                .all()
            )
            users: list[dict[str, Any]] = [_user_row_to_dict(r) for r in rows]
    except Exception as _exc:
        logger.warning("user_list DB query failed, using memory store: %s", _exc)
        mask = "***"  # display mask, not a password  # nosec B105
        users = [{**u, "password_hash": mask} for u in _users_store.values()]
    context = _ctx(request, users=users)
    return _tmpl().TemplateResponse(request=request, name="users.html", context=context)


@user_router.get("/new", response_class=HTMLResponse)
async def user_create_form(request: Request) -> HTMLResponse:
    """Render the create-user form (admin only)."""
    _require_role(request, "admin")
    context = _ctx(request, error=None, roles=["admin", "contributor", "reviewer", "viewer"])
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

    # Check uniqueness in both in-memory store and DB
    _taken = username in _users_store
    if not _taken:
        try:
            from sqlalchemy import select as _sel

            from mediacat.db.models import User as _UModel

            sf = getattr(request.app.state, "db_session_factory", None)
            if sf:
                async with sf() as _db:
                    _existing = (
                        await _db.execute(_sel(_UModel.id).where(_UModel.username == username))
                    ).scalar_one_or_none()
                    _taken = _existing is not None
        except Exception as exc:
            logger.debug("username availability check failed: %s", exc)

    if _taken:
        context = _ctx(
            request,
            error=f"Username '{username}' already exists.",
            roles=["admin", "contributor", "reviewer", "viewer"],
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
            roles=["admin", "contributor", "reviewer", "viewer"],
        )
        return _tmpl().TemplateResponse(
            request=request,
            name="user_form.html",
            context=context,
            status_code=400,
        )

    safe_role = role if role in ("admin", "contributor", "reviewer", "viewer") else "viewer"
    uid = uuid.uuid4()
    pw_hash = hash_password(password)

    # Persist to DB
    try:
        from sqlalchemy.dialects.postgresql import insert as _pg_ins

        from mediacat.db.enums import UserRole as _UR
        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                await _db.execute(
                    _pg_ins(_UModel)
                    .values(
                        id=uid,
                        username=username,
                        email=email,
                        password_hash=pw_hash,
                        role=_UR(safe_role),
                        is_active=True,
                        session_timeout_seconds=86400,
                    )
                    .on_conflict_do_nothing()
                )
                await _db.commit()
    except Exception as _exc:
        logger.warning("DB user insert failed: %s", _exc)

    # Mirror in memory so the same process can see the user immediately
    _users_store[username] = {
        "id": uid.hex,
        "username": username,
        "email": email,
        "password_hash": pw_hash,
        "role": safe_role,
        "is_active": True,
        "failed_login_count": 0,
        "locked_until": None,
        "session_timeout_seconds": 86400,
    }
    logger.info("User created: %s role=%s by=%s", username, safe_role, request.state.user_id)
    return RedirectResponse(url="/users", status_code=303)


@user_router.get("/{user_id}/edit", response_class=HTMLResponse)
async def user_edit_form(request: Request, user_id: str) -> HTMLResponse:
    """Render the edit-user form (admin only)."""
    _require_role(request, "admin")
    target: dict[str, Any] | None = None
    try:
        from sqlalchemy import select as _sel

        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                _row = (
                    await _db.execute(
                        _sel(_UModel).where(
                            _UModel.id == uuid.UUID(user_id),
                            _UModel.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if _row:
                    target = _user_row_to_dict(_row)
    except Exception as _exc:
        logger.warning("user_edit_form DB lookup failed: %s", _exc)

    if target is None:
        target = next((u for u in _users_store.values() if _same_user_id(u["id"], user_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    context = _ctx(
        request,
        target=target,
        roles=["admin", "contributor", "reviewer", "viewer"],
        timeout_options=[
            (900, "15 minutes"),
            (1800, "30 minutes"),
            (3600, "1 hour"),
            (14400, "4 hours"),
            (28800, "8 hours"),
            (86400, "24 hours"),
            (604800, "7 days"),
        ],
        error=None,
    )
    return _tmpl().TemplateResponse(request=request, name="user_edit.html", context=context)


async def _active_admin_count(request: Request) -> int:
    """Return the number of active admin accounts (DB-backed with in-memory fallback)."""
    try:
        from sqlalchemy import func
        from sqlalchemy import select as _sel

        from mediacat.db.enums import UserRole as _UR
        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                result = await _db.execute(
                    _sel(func.count())
                    .select_from(_UModel)
                    .where(
                        _UModel.role == _UR.ADMIN,
                        _UModel.is_active.is_(True),
                        _UModel.deleted_at.is_(None),
                    )
                )
                return result.scalar_one() or 0
    except Exception as _exc:
        logger.warning("_active_admin_count DB query failed: %s", _exc)
    return sum(
        1 for u in _users_store.values() if u.get("role") == "admin" and u.get("is_active", False)
    )


@user_router.post("/{user_id}/edit", response_model=None)
async def user_edit_submit(
    request: Request,
    user_id: str,
    role: str = Form("viewer"),
    session_timeout_seconds: int = Form(86400),
    is_active: str = Form("on"),
) -> RedirectResponse | HTMLResponse:
    """Update user role and session timeout (admin only)."""
    _require_role(request, "admin")

    # Load the target user from DB first, fall back to in-memory
    target: dict[str, Any] | None = None
    try:
        from sqlalchemy import select as _sel

        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                _row = (
                    await _db.execute(
                        _sel(_UModel).where(
                            _UModel.id == uuid.UUID(user_id),
                            _UModel.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if _row:
                    target = _user_row_to_dict(_row)
    except Exception as _exc:
        logger.warning("user_edit DB lookup failed: %s", _exc)

    if target is None:
        target = next((u for u in _users_store.values() if _same_user_id(u["id"], user_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_active = is_active == "on"
    new_role = role if role in ("admin", "contributor", "reviewer", "viewer") else "viewer"
    new_timeout = max(300, min(session_timeout_seconds, 604800))

    # Protect the last admin: can't demote or deactivate the final active admin
    if target.get("role") == "admin":
        losing_admin = (not new_active) or (new_role != "admin")
        if losing_admin and await _active_admin_count(request) <= 1:
            context = _ctx(
                request,
                target=target,
                roles=["admin", "contributor", "reviewer", "viewer"],
                timeout_options=[
                    (900, "15 minutes"),
                    (1800, "30 minutes"),
                    (3600, "1 hour"),
                    (14400, "4 hours"),
                    (28800, "8 hours"),
                    (86400, "24 hours"),
                    (604800, "7 days"),
                ],
                error="Cannot demote or deactivate the last active admin account.",
            )
            return _tmpl().TemplateResponse(
                request=request, name="user_edit.html", context=context, status_code=409
            )

    # Persist changes to DB
    try:
        from sqlalchemy import update as _upd

        from mediacat.db.enums import UserRole as _UR
        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                await _db.execute(
                    _upd(_UModel)
                    .where(_UModel.id == uuid.UUID(user_id))
                    .values(
                        role=_UR(new_role),
                        is_active=new_active,
                        session_timeout_seconds=new_timeout,
                    )
                )
                await _db.commit()
    except Exception as _exc:
        logger.warning("user_edit DB update failed: %s", _exc)

    # Mirror to in-memory store if the user is there
    mem_target = next((u for u in _users_store.values() if _same_user_id(u["id"], user_id)), None)
    if mem_target:
        mem_target["role"] = new_role
        mem_target["session_timeout_seconds"] = new_timeout
        mem_target["is_active"] = new_active

    logger.info("User updated: %s by=%s", target["username"], request.state.user_id)
    return RedirectResponse(url="/users", status_code=303)


@user_router.post("/{user_id}/deactivate")
async def user_deactivate(request: Request, user_id: str) -> RedirectResponse:
    """Deactivate a user (admin only). Blocked if this would leave zero active admins."""
    _require_role(request, "admin")

    # Load target to check role
    target_role: str = ""
    target_username: str = ""
    try:
        from sqlalchemy import select as _sel

        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                _row = (
                    await _db.execute(
                        _sel(_UModel).where(
                            _UModel.id == uuid.UUID(user_id),
                            _UModel.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if _row:
                    target_role = str(_row.role)
                    target_username = _row.username
    except Exception as _exc:
        logger.warning("user_deactivate DB lookup failed: %s", _exc)

    if not target_username:
        for u in _users_store.values():
            if _same_user_id(u["id"], user_id):
                target_role = u.get("role", "")
                target_username = u.get("username", "")
                break

    if target_role == "admin" and await _active_admin_count(request) <= 1:
        logger.warning(
            "Blocked deactivation of last admin %s by=%s", target_username, request.state.user_id
        )
        return RedirectResponse(url="/users?error=last_admin", status_code=303)

    try:
        from sqlalchemy import update as _upd

        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                await _db.execute(
                    _upd(_UModel).where(_UModel.id == uuid.UUID(user_id)).values(is_active=False)
                )
                await _db.commit()
    except Exception as _exc:
        logger.warning("user_deactivate DB update failed: %s", _exc)

    for u in _users_store.values():
        if _same_user_id(u["id"], user_id):
            u["is_active"] = False
            break

    logger.info("User deactivated: %s by=%s", target_username, request.state.user_id)
    return RedirectResponse(url="/users", status_code=303)


@user_router.post("/{user_id}/activate")
async def user_activate(request: Request, user_id: str) -> RedirectResponse:
    """Reactivate a user (admin only)."""
    _require_role(request, "admin")

    try:
        from sqlalchemy import update as _upd

        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                await _db.execute(
                    _upd(_UModel).where(_UModel.id == uuid.UUID(user_id)).values(is_active=True)
                )
                await _db.commit()
    except Exception as _exc:
        logger.warning("user_activate DB update failed: %s", _exc)

    for u in _users_store.values():
        if _same_user_id(u["id"], user_id):
            u["is_active"] = True
            logger.info("User activated: %s by=%s", u["username"], request.state.user_id)
            break

    return RedirectResponse(url="/users", status_code=303)


# ---------------------------------------------------------------------------
# Public self-registration
# ---------------------------------------------------------------------------


register_router = APIRouter(tags=["auth"])


@register_router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request) -> HTMLResponse:
    """Public registration form — creates a viewer account."""
    return _tmpl().TemplateResponse(
        request=request,
        name="register.html",
        context=_ctx(request, error=None),
    )


@register_router.post("/register", response_model=None)
async def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse | HTMLResponse:
    """Create a new viewer account (public, no auth required)."""

    def _err(msg: str) -> HTMLResponse:
        return _tmpl().TemplateResponse(
            request=request,
            name="register.html",
            context=_ctx(request, error=msg),
            status_code=400,
        )

    if (
        len(username) < 3
        or not username.replace("_", "").replace("-", "").replace(".", "").isalnum()
    ):
        return _err("Username must be at least 3 characters (letters, numbers, ._- only).")

    # Check uniqueness in both DB and in-memory store
    _username_taken = username in _users_store
    if not _username_taken:
        try:
            from sqlalchemy import select as _sel

            from mediacat.db.models import User as _UModel

            sf = getattr(request.app.state, "db_session_factory", None)
            if sf:
                async with sf() as _db:
                    _existing = (
                        await _db.execute(_sel(_UModel.id).where(_UModel.username == username))
                    ).scalar_one_or_none()
                    _username_taken = _existing is not None
        except Exception as exc:
            logger.debug("username availability check failed: %s", exc)
    if _username_taken:
        return _err(f"Username '{username}' is already taken.")
    if len(password) < 8:
        return _err("Password must be at least 8 characters.")
    if password != confirm_password:
        return _err("Passwords do not match.")

    uid = uuid.uuid4()
    pw_hash = hash_password(password)

    # Persist to DB
    try:
        from sqlalchemy.dialects.postgresql import insert as _pg_ins

        from mediacat.db.enums import UserRole as _UR
        from mediacat.db.models import User as _UModel

        sf = getattr(request.app.state, "db_session_factory", None)
        if sf:
            async with sf() as _db:
                await _db.execute(
                    _pg_ins(_UModel)
                    .values(
                        id=uid,
                        username=username,
                        email=email,
                        password_hash=pw_hash,
                        role=_UR.VIEWER,
                        is_active=True,
                        session_timeout_seconds=86400,
                    )
                    .on_conflict_do_nothing()
                )
                await _db.commit()
    except Exception as _exc:
        logger.warning("register DB insert failed: %s", _exc)

    _users_store[username] = {
        "id": uid.hex,
        "username": username,
        "email": email,
        "password_hash": pw_hash,
        "role": "viewer",
        "is_active": True,
        "failed_login_count": 0,
        "locked_until": None,
        "session_timeout_seconds": 86400,
    }
    logger.info("Self-registration: user=%s email=%s", username, email)
    return RedirectResponse(url="/login?registered=1", status_code=303)


# ---------------------------------------------------------------------------
# Global search  (3panel layout top-bar)
# ---------------------------------------------------------------------------

#: Top-bar search scopes.  "all" covers artists and albums; track titles have
#: their own scope so a song search is not drowned in artist/album hits.
SEARCH_SCOPES = ("all", "artists", "albums", "tracks")
_SEARCH_LIMIT = 100


def _like_pattern(q: str) -> str:
    """ILIKE pattern matching *q* literally anywhere (``%``/``_`` match themselves)."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _live_tokens(stmt: Any) -> Any:
    """Restrict a token query to live (not deleted, not merged) pressings."""
    from mediacat.db.enums import TokenStatus
    from mediacat.db.models import Token

    return stmt.where(Token.deleted_at.is_(None)).where(Token.status != TokenStatus.MERGED)


async def _search_artists(db: Any, pattern: str) -> list[dict[str, Any]]:
    """Artists whose name matches, with pressing count, years and an image."""
    import urllib.parse

    from sqlalchemy import func, select

    from mediacat.db.models import ArtistBio, MediaObject, Token

    artist_rows = (
        await db.execute(
            _live_tokens(
                select(
                    Token.artist,
                    func.count(Token.id).label("cnt"),
                    func.min(Token.year).label("yr_min"),
                    func.max(Token.year).label("yr_max"),
                )
            )
            .where(Token.artist.ilike(pattern, escape="\\"))
            .group_by(Token.artist)
            .order_by(Token.artist.asc())
            .limit(50)
        )
    ).all()

    artist_names = [r.artist for r in artist_rows]
    cover_by_artist: dict[str, str] = {}
    portrait_by_artist: dict[str, str] = {}
    if artist_names:
        cover_rows = (
            await db.execute(
                _live_tokens(
                    select(Token.artist, MediaObject.id, MediaObject.token_id).join(
                        MediaObject, MediaObject.token_id == Token.id
                    )
                )
                .where(Token.artist.in_(artist_names))
                .order_by(
                    Token.artist.asc(),
                    MediaObject.is_primary_cover.desc().nulls_last(),
                    Token.year.asc().nulls_last(),
                )
            )
        ).all()
        for row in cover_rows:
            if row.artist not in cover_by_artist:
                cover_by_artist[row.artist] = f"/catalogue/{row.token_id}/images/{row.id}"

        bio_rows = (
            await db.execute(
                select(ArtistBio.artist_name, ArtistBio.thumbnail_url)
                .where(ArtistBio.artist_name.in_(artist_names))
                .where(ArtistBio.thumbnail_url.isnot(None))
            )
        ).all()
        portrait_by_artist = {r.artist_name: r.thumbnail_url for r in bio_rows}

    return [
        {
            "name": r.artist,
            "pressing_count": r.cnt,
            "year_range": (
                str(r.yr_min) + (f"–{r.yr_max}" if r.yr_max != r.yr_min else "") if r.yr_min else ""
            ),
            "enc": urllib.parse.quote(r.artist or "", safe=""),
            "portrait_url": portrait_by_artist.get(r.artist),
            "cover_url": cover_by_artist.get(r.artist),
        }
        for r in artist_rows
    ]


async def _cover_urls(db: Any, token_ids: list[Any]) -> dict[str, str]:
    """Cover image URL per token id (primary cover first)."""
    from sqlalchemy import select

    from mediacat.db.models import MediaObject

    if not token_ids:
        return {}
    rows = (
        await db.execute(
            select(MediaObject.id, MediaObject.token_id)
            .where(MediaObject.token_id.in_(token_ids))
            .order_by(MediaObject.is_primary_cover.desc().nulls_last(), MediaObject.id.asc())
        )
    ).all()
    covers: dict[str, str] = {}
    for row in rows:
        covers.setdefault(str(row.token_id), f"/catalogue/{row.token_id}/images/{row.id}")
    return covers


def _hit_row(r: Any, covers: dict[str, str]) -> dict[str, Any]:
    """Common fields of an album or track hit."""
    import urllib.parse

    return {
        "id": str(r.id),
        "artist": r.artist or "",
        "enc_artist": urllib.parse.quote(r.artist or "", safe=""),
        "title": r.title or "",
        "year": r.year,
        "media_format": r.media_format,
        "cover_url": covers.get(str(r.id)),
    }


async def _search_pressings(
    db: Any, pattern: str, *, by_artist: bool, by_album: bool
) -> list[dict[str, Any]]:
    """Pressings whose artist and/or album title / catalogue number matches."""
    from sqlalchemy import or_, select

    from mediacat.db.models import Token

    fields = ([Token.artist] if by_artist else []) + (
        [Token.title, Token.catalog_number] if by_album else []
    )
    rows = (
        await db.execute(
            _live_tokens(
                select(
                    Token.id,
                    Token.artist,
                    Token.title,
                    Token.year,
                    Token.media_format,
                    Token.catalog_number,
                )
            )
            .where(or_(*(f.ilike(pattern, escape="\\") for f in fields)))
            .order_by(Token.title.asc(), Token.artist.asc(), Token.year.asc().nulls_last())
            .limit(_SEARCH_LIMIT + 1)
        )
    ).all()
    covers = await _cover_urls(db, [r.id for r in rows])
    return [_hit_row(r, covers) | {"catalog_number": r.catalog_number} for r in rows]


async def _search_tracks(db: Any, pattern: str) -> list[dict[str, Any]]:
    """Tracks (from each pressing's ``tracklist``) whose title matches."""
    from sqlalchemy import case, cast, func, literal, select, true
    from sqlalchemy.dialects.postgresql import JSONB

    from mediacat.db.models import Token

    # Guard against a non-array tracklist: jsonb_array_elements raises on one.
    tracks_array = case(
        (func.jsonb_typeof(Token.tracklist) == "array", Token.tracklist),
        else_=cast(literal("[]"), JSONB),
    )
    track = func.jsonb_array_elements(tracks_array).table_valued("value").lateral("track")
    track_title = track.c.value.op("->>")("title")
    rows = (
        await db.execute(
            _live_tokens(
                select(
                    Token.id,
                    Token.artist,
                    Token.title,
                    Token.year,
                    Token.media_format,
                    track_title.label("track_title"),
                    track.c.value.op("->>")("position").label("position"),
                )
                .select_from(Token)
                .join(track, true())
            )
            .where(track_title.ilike(pattern, escape="\\"))
            .order_by(func.lower(track_title), Token.artist.asc(), Token.year.asc().nulls_last())
            .limit(_SEARCH_LIMIT + 1)
        )
    ).all()
    covers = await _cover_urls(db, list({r.id for r in rows}))
    return [
        _hit_row(r, covers) | {"track_title": r.track_title or "", "position": r.position or ""}
        for r in rows
    ]


def _first_artist(
    artists: list[dict[str, Any]], albums: list[dict[str, Any]], tracks: list[dict[str, Any]]
) -> dict[str, str]:
    """The artist the centre panel opens with: the first artist hit, else the
    artist of the first album or track hit."""
    if artists:
        return {"enc": artists[0]["enc"], "name": artists[0]["name"]}
    for hit in (albums or tracks)[:1]:
        return {"enc": hit["enc_artist"], "name": hit["artist"]}
    return {"enc": "", "name": ""}


def _single_pressing_hit(
    artists: list[dict[str, Any]], albums: list[dict[str, Any]], tracks: list[dict[str, Any]]
) -> str | None:
    """Pressing id when the search found exactly one album or track and no artist."""
    hits = albums + tracks
    if not artists and len(hits) == 1:
        return str(hits[0]["id"])
    return None


@search_router.get("/search", response_class=HTMLResponse)
async def global_search(
    request: Request, q: str = Query(default=""), scope: str = Query(default="all")
) -> HTMLResponse:
    """Search artists, album titles / catalogue numbers, or track titles.

    ``scope`` is one of :data:`SEARCH_SCOPES`: ``all`` (artists and albums),
    ``artists``, ``albums`` or ``tracks``.  Returns an HTMX partial or full page.
    """
    q = q.strip()[:200]  # cap query length
    if scope not in SEARCH_SCOPES:
        scope = "all"
    pattern = _like_pattern(q)
    sf = getattr(request.app.state, "db_session_factory", None)

    # ── 3panel layout: grouped hit list in the left panel ───────────────────
    if _is_htmx(request) and _layout_base(request) == "base_3panel.html" and q and sf:
        try:
            artists: list[dict[str, Any]] = []
            albums: list[dict[str, Any]] = []
            tracks: list[dict[str, Any]] = []
            async with sf() as db:
                if scope in ("all", "artists"):
                    artists = await _search_artists(db, pattern)
                if scope in ("all", "albums"):
                    albums = await _search_pressings(db, pattern, by_artist=False, by_album=True)
                if scope == "tracks":
                    tracks = await _search_tracks(db, pattern)
            # One album/track and nothing else: open its detail page directly
            single = _single_pressing_hit(artists, albums, tracks)
            if single:
                return HTMLResponse("", headers={"HX-Redirect": f"/catalogue/{single}"})
            ctx = _ctx(
                request,
                q=q,
                scope=scope,
                artists=artists,
                albums=albums[:_SEARCH_LIMIT],
                tracks=tracks[:_SEARCH_LIMIT],
                albums_more=len(albums) > _SEARCH_LIMIT,
                tracks_more=len(tracks) > _SEARCH_LIMIT,
                first=_first_artist(artists, albums, tracks),
            )
            return _tmpl().TemplateResponse(
                request=request,
                name="partials/search_3panel.html",
                context=ctx,
            )
        except Exception as exc:
            logger.warning("global_search (3panel) error: %s", exc)

    # ── Classic layout or empty query: flat pressing list ───────────────────
    results: list[dict[str, Any]] = []
    if q and sf:
        try:
            async with sf() as db:
                if scope == "tracks":
                    results = await _search_tracks(db, pattern)
                else:
                    results = await _search_pressings(
                        db,
                        pattern,
                        by_artist=scope in ("all", "artists"),
                        by_album=scope in ("all", "albums"),
                    )
                    results.sort(key=lambda r: (r["artist"].casefold(), r["title"].casefold()))
                results = results[:_SEARCH_LIMIT]
        except Exception as exc:
            logger.warning("global_search error: %s", exc)

    ctx = _ctx(request, q=q, scope=scope, results=results)

    if _is_htmx(request):
        return _tmpl().TemplateResponse(
            request=request,
            name="partials/global_search_results.html",
            context=ctx,
        )
    # Full-page fallback (non-HTMX)
    return _tmpl().TemplateResponse(
        request=request,
        name="search.html",
        context=ctx,
    )


# ---------------------------------------------------------------------------
# Layout switcher  (sets mc_layout cookie; used by admin Layout tab)
# ---------------------------------------------------------------------------


@search_router.post("/layout/set", response_model=None)
async def layout_set(request: Request, layout: str = Form("classic")) -> RedirectResponse:
    """Set the UI layout preference cookie. Admin-only."""
    _require_role(request, "admin")
    if layout not in _VALID_LAYOUTS:
        layout = "classic"
    response = RedirectResponse(url="/admin?tab=layout&msg=layout_saved", status_code=303)
    response.set_cookie(
        _LAYOUT_COOKIE,
        layout,
        max_age=60 * 60 * 24 * 365,  # 1 year
        httponly=False,  # JS must NOT read it, but it's non-sensitive
        samesite="lax",
    )
    logger.info("Layout switched to %r by %s", layout, getattr(request.state, "user_id", "?"))
    return response
