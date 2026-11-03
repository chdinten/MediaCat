"""Admin routes — user management hub, collection import, admin utilities.

All routes require the 'admin' role.  The /admin page replaces the top-level
/users nav link and hosts tabbed sub-pages:

  GET  /admin                              → redirect to /admin?tab=users
  GET  /admin?tab=users                    → user management table
  GET  /admin?tab=import                   → collection import page
  POST /admin/import/start                 → start a new import job
  GET  /admin/import/status/{job_id}       → JSON status for polling
  POST /admin/import/{job_id}/pause        → request job pause
  POST /admin/import/{job_id}/resume       → resume a paused/failed job
  GET  /admin/backup/export               → download catalogue ZIP backup
  POST /admin/backup/import               → upload and restore a catalogue ZIP backup
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import logging
import re
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from mediacat.web.routes import _ctx, _require_role, _tmpl

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TIMEOUT_OPTIONS = [
    (900, "15 minutes"),
    (1800, "30 minutes"),
    (3600, "1 hour"),
    (14400, "4 hours"),
    (28800, "8 hours"),
    (86400, "24 hours"),
    (604800, "7 days"),
]

_VALID_ROLES = ("admin", "contributor", "reviewer", "viewer")


def _sf(request: Request) -> Any:
    """The app's async_sessionmaker (set in the lifespan)."""
    return request.app.state.db_session_factory


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------


@admin_router.get("", response_class=HTMLResponse)
async def admin_home(request: Request, tab: str = "users") -> HTMLResponse:
    _require_role(request, "admin")
    if tab not in (
        "users",
        "import",
        "backup",
        "docs",
        "debug",
        "layout",
        "housekeeping",
        "api-tokens",
        "streaming",
    ):
        tab = "users"

    users: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []

    try:
        from sqlalchemy import select as _sel

        from mediacat.db.models import User as _UModel

        async with _sf(request)() as db:
            rows = (
                (
                    await db.execute(
                        _sel(_UModel).where(_UModel.deleted_at.is_(None)).order_by(_UModel.username)
                    )
                )
                .scalars()
                .all()
            )
            users = [
                {
                    "id": str(u.id),
                    "username": u.username,
                    "email": u.email,
                    "role": str(u.role),
                    "is_active": u.is_active,
                    "session_timeout_seconds": getattr(u, "session_timeout_seconds", 86400),
                    "last_login_at": (
                        u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else None
                    ),
                    "failed_login_count": u.failed_login_count or 0,
                }
                for u in rows
            ]
    except Exception as exc:
        logger.warning("admin_home users query failed: %s", exc)
        from mediacat.web.routes import _users_store

        mask = "***"  # display mask, not a password  # nosec B105
        users = [{**u, "password_hash": mask} for u in _users_store.values()]

    if tab == "import":
        try:
            from sqlalchemy import select as _sel
            from sqlalchemy import update as _upd

            from mediacat.db.enums import ImportJobStatus
            from mediacat.db.models import CollectionImportJob

            async with _sf(request)() as db:
                # Mark any running/pending jobs that have no active asyncio task as failed.
                # These are zombies left over from a previous process or app restart.
                zombie_statuses = (ImportJobStatus.RUNNING, ImportJobStatus.PENDING)
                zombie_rows = (
                    (
                        await db.execute(
                            _sel(CollectionImportJob).where(
                                CollectionImportJob.status.in_(zombie_statuses)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for zj in zombie_rows:
                    if str(zj.id) not in _stop_flags:
                        await db.execute(
                            _upd(CollectionImportJob)
                            .where(CollectionImportJob.id == zj.id)
                            .values(
                                status=ImportJobStatus.FAILED,
                                error_message="Aborted — app was restarted or process was interrupted",
                                updated_at=datetime.now(UTC),
                            )
                        )
                        logger.info("Marked zombie import job %s as failed", zj.id)
                if zombie_rows:
                    await db.commit()

                job_rows = (
                    (
                        await db.execute(
                            _sel(CollectionImportJob).order_by(
                                CollectionImportJob.created_at.desc()
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                jobs = [
                    {
                        "id": str(j.id),
                        "discogs_username": j.discogs_username,
                        "status": str(j.status),
                        "total_releases": j.total_releases,
                        "imported_count": j.imported_count,
                        "skipped_count": j.skipped_count,
                        "failed_count": j.failed_count,
                        "last_page": j.last_page,
                        "error_message": j.error_message,
                        "created_at": j.created_at.strftime("%Y-%m-%d %H:%M")
                        if j.created_at
                        else None,
                        "updated_at": j.updated_at.strftime("%Y-%m-%d %H:%M")
                        if j.updated_at
                        else None,
                    }
                    for j in job_rows
                ]
        except Exception as exc:
            logger.warning("admin_home jobs query failed: %s", exc)

    discogs_import_count: int = 0
    if tab == "import":
        try:
            from sqlalchemy import func as _func
            from sqlalchemy import select as _sel

            from mediacat.db.models import Token

            async with _sf(request)() as db:
                discogs_import_count = (
                    await db.execute(
                        _sel(_func.count(Token.id)).where(
                            Token.discogs_release_id.isnot(None),
                            Token.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
        except Exception as exc:
            logger.warning("discogs_import_count query failed: %s", exc)

    deleted_pressings: list[dict[str, Any]] = []
    if tab == "housekeeping":
        try:
            from datetime import timedelta

            from sqlalchemy import select as _sel

            from mediacat.db.models import Token

            cutoff = datetime.now(UTC) - timedelta(days=30)
            async with _sf(request)() as db:
                rows = (
                    await db.execute(
                        _sel(
                            Token.id,
                            Token.artist,
                            Token.title,
                            Token.year,
                            Token.media_format,
                            Token.catalog_number,
                            Token.deleted_at,
                        )
                        .where(Token.deleted_at.isnot(None))
                        .where(Token.deleted_at >= cutoff)
                        .order_by(Token.deleted_at.desc())
                        .limit(200)
                    )
                ).all()
            deleted_pressings = [
                {
                    "id": str(r.id),
                    "artist": r.artist or "Unknown",
                    "title": r.title or "Untitled",
                    "year": r.year,
                    "fmt": r.media_format.value if r.media_format else "",
                    "catalog_number": r.catalog_number or "",
                    "deleted_at": r.deleted_at.strftime("%Y-%m-%d %H:%M") if r.deleted_at else "",
                    "expires_in_days": max(
                        0, (r.deleted_at + timedelta(days=30) - datetime.now(UTC)).days
                    )
                    if r.deleted_at
                    else 0,
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("admin_home housekeeping query failed: %s", exc)

    api_credentials: list[dict[str, Any]] = []
    if tab == "api-tokens":
        api_credentials = await _load_api_credentials(request)

    background: dict[str, Any] = {}
    if tab == "layout":
        from mediacat.web.background import admin_view

        background = await admin_view(request)

    streaming: dict[str, Any] = {}
    if tab == "streaming":
        from mediacat.web.streaming import admin_view as streaming_admin_view

        streaming = await streaming_admin_view(request)

    context = _ctx(
        request,
        tab=tab,
        users=users,
        jobs=jobs,
        active_job_ids=set(_stop_flags.keys()),
        timeout_options=_TIMEOUT_OPTIONS,
        roles=list(_VALID_ROLES),
        deleted_pressings=deleted_pressings,
        discogs_import_count=discogs_import_count,
        api_credentials=api_credentials,
        background=background,
        streaming=streaming,
    )
    return _tmpl().TemplateResponse(request=request, name="admin.html", context=context)


# ---------------------------------------------------------------------------
# User management (inline in admin page, AJAX target)
# ---------------------------------------------------------------------------


@admin_router.post("/users/new", response_model=None)
async def admin_user_create(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
) -> RedirectResponse:
    _require_role(request, "admin")
    from mediacat.web.auth import hash_password
    from mediacat.web.routes import _users_store

    safe_role = role if role in _VALID_ROLES else "viewer"

    if len(password) < 8:
        return RedirectResponse(url="/admin?tab=users&error=password_short", status_code=303)

    # Check uniqueness on username and email
    try:
        from sqlalchemy import select as _sel

        from mediacat.db.models import User as _UModel

        async with _sf(request)() as db:
            existing = (
                await db.execute(
                    _sel(_UModel.id, _UModel.username, _UModel.email).where(
                        (_UModel.username == username) | (_UModel.email == email)
                    )
                )
            ).first()
        if existing:
            if existing.username == username:
                return RedirectResponse(
                    url="/admin?tab=users&error=username_taken", status_code=303
                )
            return RedirectResponse(url="/admin?tab=users&error=email_taken", status_code=303)
    except Exception as exc:
        logger.warning("admin_user_create uniqueness check failed: %s", exc)
        if username in _users_store:
            return RedirectResponse(url="/admin?tab=users&error=username_taken", status_code=303)

    uid = uuid.uuid4()
    pw = hash_password(password)
    try:
        from sqlalchemy.dialects.postgresql import insert as _ins

        from mediacat.db.enums import UserRole as _UR
        from mediacat.db.models import User as _UModel

        async with _sf(request)() as db:
            await db.execute(
                _ins(_UModel)
                .values(
                    id=uid,
                    username=username,
                    email=email,
                    password_hash=pw,
                    role=_UR(safe_role),
                    is_active=True,
                    session_timeout_seconds=86400,
                )
                .on_conflict_do_nothing(index_elements=["username"])
            )
            await db.commit()
    except Exception as exc:
        logger.error("admin_user_create DB insert failed: %s", exc)
        return RedirectResponse(url="/admin?tab=users&error=db_error", status_code=303)

    _users_store[username] = {
        "id": uid.hex,
        "username": username,
        "email": email,
        "password_hash": pw,
        "role": safe_role,
        "is_active": True,
        "failed_login_count": 0,
        "locked_until": None,
        "session_timeout_seconds": 86400,
    }
    logger.info("Admin created user: %s role=%s by=%s", username, safe_role, request.state.user_id)
    return RedirectResponse(url="/admin?tab=users&msg=user_created", status_code=303)


# ---------------------------------------------------------------------------
# API tokens management
# ---------------------------------------------------------------------------


@admin_router.post("/api-tokens/save", response_model=None)
async def admin_api_tokens_save(request: Request) -> RedirectResponse:
    """Upsert API credentials submitted from the admin API Tokens tab.

    A blank value means "leave unchanged" — it will not overwrite an existing
    stored token.  To clear a token, the user must explicitly type a single
    space (which is then stripped to empty string and saved as empty).
    """
    _require_role(request, "admin")
    from sqlalchemy.dialects.postgresql import insert as _pg_insert

    from mediacat.db.models import SiteSetting

    form = await request.form()
    updates: dict[str, str] = {}
    for key in _KNOWN_CREDENTIAL_KEYS:
        raw = form.get(key)
        if raw is None or not isinstance(raw, str):
            continue
        # A truly blank field means "don't touch it"
        if raw == "":
            continue
        # A single space → explicit clear
        updates[key] = raw.strip()

    if updates:
        try:
            async with _sf(request)() as db:
                for key, value in updates.items():
                    await db.execute(
                        _pg_insert(SiteSetting)
                        .values(key=key, value=value)
                        .on_conflict_do_update(
                            index_elements=["key"],
                            set_={"value": value, "updated_at": datetime.now(UTC)},
                        )
                    )
                await db.commit()
            logger.info("API credentials updated: %s", sorted(updates.keys()))
        except Exception:
            logger.exception("admin_api_tokens_save failed")
            return RedirectResponse(url="/admin?tab=api-tokens&error=db", status_code=303)

    return RedirectResponse(url="/admin?tab=api-tokens&msg=saved", status_code=303)


# ---------------------------------------------------------------------------
# Collection import
# ---------------------------------------------------------------------------

# In-process stop signals: job_id → asyncio.Event (set = stop requested)
_stop_flags: dict[str, asyncio.Event] = {}
# Strong references to running import tasks: the event loop keeps only weak
# ones, so an unreferenced task could be garbage-collected mid-import.
_import_tasks: set[asyncio.Task[None]] = set()


def _start_import_task(coro: Any, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _import_tasks.add(task)
    task.add_done_callback(_import_tasks.discard)


@admin_router.post("/import/start", response_model=None)
async def import_start(
    request: Request,
    discogs_username: str = Form(...),
    discogs_token: str = Form(""),
    reimport: str = Form(""),
) -> RedirectResponse:
    _require_role(request, "admin")

    # Strip URL prefix if user pasted a full Discogs profile URL
    # e.g. "https://www.discogs.com/user/Chi_Master" → "Chi_Master"
    raw = discogs_username.strip()
    clean = re.sub(r"^(?:https?://)?(?:www\.)?discogs\.com/(?:user/)?", "", raw).strip("/").strip()
    if not clean:
        return RedirectResponse(url="/admin?tab=import&error=no_username", status_code=303)
    discogs_username = clean

    # Use provided token or fall back to DB / configured app token
    token_to_use = discogs_token.strip() or await _resolve_discogs_token(request)
    do_reimport = reimport == "1"

    try:
        from mediacat.db.enums import ImportJobStatus
        from mediacat.db.models import CollectionImportJob

        async with _sf(request)() as db:
            job = CollectionImportJob(
                discogs_username=discogs_username.strip(),
                status=ImportJobStatus.PENDING,
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = str(job.id)
    except Exception as exc:
        logger.error("Failed to create import job: %s", exc)
        return RedirectResponse(url="/admin?tab=import&error=db_error", status_code=303)

    stop_event = asyncio.Event()
    _stop_flags[job_id] = stop_event
    sf = _sf(request)
    store = getattr(request.app.state, "object_store", None)
    discogs_conn = _get_discogs_connector(request, token_to_use)

    _start_import_task(
        _run_import(
            job_id,
            discogs_username.strip(),
            discogs_conn,
            sf,
            store,
            stop_event,
            reimport=do_reimport,
        ),
        name=f"collection-import-{job_id[:8]}",
    )

    logger.info(
        "Collection import started: job=%s user=%s reimport=%s by=%s",
        job_id,
        discogs_username,
        do_reimport,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/admin?tab=import&job={job_id}", status_code=303)


@admin_router.get("/import/imported-releases", response_class=HTMLResponse)
async def import_imported_releases(request: Request, page: int = 1) -> HTMLResponse:
    """HTMX partial: table of all tokens imported from Discogs (have a discogs_release_id)."""
    _require_role(request, "admin")
    page_size = 100
    offset = (page - 1) * page_size
    rows: list[dict[str, Any]] = []
    total = 0
    try:
        from sqlalchemy import func as _func
        from sqlalchemy import select as _sel

        from mediacat.db.models import Token

        async with _sf(request)() as db:
            total = (
                await db.execute(
                    _sel(_func.count(Token.id)).where(
                        Token.discogs_release_id.isnot(None),
                        Token.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            result = (
                await db.execute(
                    _sel(
                        Token.id,
                        Token.discogs_release_id,
                        Token.artist,
                        Token.title,
                        Token.year,
                        Token.media_format,
                    )
                    .where(Token.discogs_release_id.isnot(None), Token.deleted_at.is_(None))
                    .order_by(Token.artist.asc(), Token.title.asc())
                    .offset(offset)
                    .limit(page_size)
                )
            ).all()
        rows = [
            {
                "id": str(r.id),
                "discogs_release_id": r.discogs_release_id,
                "discogs_url": f"https://www.discogs.com/release/{r.discogs_release_id}",
                "artist": r.artist or "—",
                "title": r.title or "—",
                "year": r.year or "",
                "fmt": r.media_format.value if r.media_format else "",
            }
            for r in result
        ]
    except Exception as exc:
        logger.warning("import_imported_releases query failed: %s", exc)

    has_more = (page * page_size) < total
    html_parts = [
        '<table class="admin-table" style="width:100%;font-size:0.82rem">',
        "<thead><tr>"
        "<th>Discogs ID</th><th>Discogs URL</th>"
        "<th>Artist</th><th>Title</th><th>Year</th><th>Format</th>"
        "<th>Detail</th>"
        "</tr></thead><tbody>",
    ]
    for r in rows:
        html_parts.append(
            f"<tr>"
            f"<td><code>{r['discogs_release_id']}</code></td>"
            f'<td><a href="{r["discogs_url"]}" target="_blank" rel="noopener noreferrer" '
            f'   style="font-size:0.78rem">{r["discogs_release_id"]} ↗</a></td>'
            # Artist and title come from Discogs / user input: escape them.
            f"<td>{html.escape(r['artist'])}</td>"
            f"<td>{html.escape(r['title'])}</td>"
            f"<td>{r['year']}</td>"
            f"<td>{r['fmt']}</td>"
            f'<td><a href="/catalogue/{r["id"]}" class="btn btn-ghost btn-sm" style="font-size:0.72rem">View</a></td>'
            f"</tr>"
        )
    html_parts.append("</tbody></table>")
    if has_more:
        html_parts.append(
            f'<div style="text-align:center;margin-top:0.75rem">'
            f'<button class="btn btn-ghost btn-sm" '
            f'  hx-get="/admin/import/imported-releases?page={page + 1}" '
            f'  hx-target="closest div" hx-swap="outerHTML">Load more…</button>'
            f"</div>"
        )
    elif not rows:
        html_parts = [
            '<p class="text-muted" style="font-style:italic">No Discogs-imported pressings found.</p>'
        ]

    return HTMLResponse("".join(html_parts))


@admin_router.get("/import/status/{job_id}")
async def import_status(request: Request, job_id: str) -> JSONResponse:
    _require_role(request, "admin")
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        from sqlalchemy import select as _sel

        from mediacat.db.models import CollectionImportJob

        async with _sf(request)() as db:
            job = (
                await db.execute(_sel(CollectionImportJob).where(CollectionImportJob.id == jid))
            ).scalar_one_or_none()
            if not job:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse(
                {
                    "id": str(job.id),
                    "status": str(job.status),
                    "total_releases": job.total_releases,
                    "imported_count": job.imported_count,
                    "skipped_count": job.skipped_count,
                    "failed_count": job.failed_count,
                    "last_page": job.last_page,
                    "error_message": job.error_message,
                    "updated_at": job.updated_at.isoformat() if job.updated_at else None,
                }
            )
    except Exception as exc:
        logger.warning("import_status failed for %s: %s", job_id, exc)
        return JSONResponse({"error": "status unavailable"}, status_code=500)


@admin_router.post("/import/{job_id}/pause", response_model=None)
async def import_pause(request: Request, job_id: str) -> RedirectResponse:
    _require_role(request, "admin")
    flag = _stop_flags.get(job_id)
    if flag:
        flag.set()
    try:
        from sqlalchemy import update as _upd

        from mediacat.db.enums import ImportJobStatus
        from mediacat.db.models import CollectionImportJob

        async with _sf(request)() as db:
            await db.execute(
                _upd(CollectionImportJob)
                .where(CollectionImportJob.id == uuid.UUID(job_id))
                .values(status=ImportJobStatus.PAUSED, updated_at=datetime.now(UTC))
            )
            await db.commit()
    except Exception as exc:
        logger.warning("import_pause DB update failed: %s", exc)
    return RedirectResponse(url=f"/admin?tab=import&job={job_id}", status_code=303)


@admin_router.post("/import/{job_id}/resume", response_model=None)
async def import_resume(
    request: Request,
    job_id: str,
    discogs_token: str = Form(""),
) -> RedirectResponse:
    _require_role(request, "admin")
    try:
        from sqlalchemy import select as _sel
        from sqlalchemy import update as _upd

        from mediacat.db.enums import ImportJobStatus
        from mediacat.db.models import CollectionImportJob

        async with _sf(request)() as db:
            job = (
                await db.execute(
                    _sel(CollectionImportJob).where(CollectionImportJob.id == uuid.UUID(job_id))
                )
            ).scalar_one_or_none()
            if not job:
                return RedirectResponse(url="/admin?tab=import", status_code=303)

            username = job.discogs_username
            await db.execute(
                _upd(CollectionImportJob)
                .where(CollectionImportJob.id == uuid.UUID(job_id))
                .values(
                    status=ImportJobStatus.PENDING, error_message=None, updated_at=datetime.now(UTC)
                )
            )
            await db.commit()
    except Exception as exc:
        logger.error("import_resume failed: %s", exc)
        return RedirectResponse(url=f"/admin?tab=import&job={job_id}", status_code=303)

    token_to_use = discogs_token.strip() or await _resolve_discogs_token(request)
    stop_event = asyncio.Event()
    _stop_flags[job_id] = stop_event
    sf = _sf(request)
    store = getattr(request.app.state, "object_store", None)
    discogs_conn = _get_discogs_connector(request, token_to_use)

    _start_import_task(
        _run_import(job_id, username, discogs_conn, sf, store, stop_event),
        name=f"collection-import-resume-{job_id[:8]}",
    )
    return RedirectResponse(url=f"/admin?tab=import&job={job_id}", status_code=303)


# ---------------------------------------------------------------------------
# API credentials helpers
# ---------------------------------------------------------------------------

# Ordered list of known credentials shown in the UI.
_API_CREDENTIALS_META: list[dict[str, str]] = [
    {
        "key": "discogs_token",
        "label": "Discogs Personal Access Token",
        "hint": "From discogs.com → Settings → Developers → Generate new token. "
        "Required for price data and collection import.",
        "url": "https://www.discogs.com/settings/developers",
        "secret": "true",  # "secret" is a yes/no display flag  # nosec B105
    },
    {
        "key": "spotify_client_id",
        "label": "Spotify Client ID",
        "hint": "From developer.spotify.com → Dashboard → your app. "
        "Used for artist discography and Spotify streaming links.",
        "url": "https://developer.spotify.com/dashboard",
        "secret": "false",  # "secret" is a yes/no display flag  # nosec B105
    },
    {
        "key": "spotify_client_secret",
        "label": "Spotify Client Secret",
        "hint": "Paired with the Client ID above. Keep this secret.",
        "url": "https://developer.spotify.com/dashboard",
        "secret": "true",  # "secret" is a yes/no display flag  # nosec B105
    },
    {
        "key": "musicbrainz_user_agent",
        "label": "MusicBrainz User-Agent",
        "hint": "Optional. Format: AppName/version (contact@example.com). "
        "MusicBrainz needs no auth token but requires a meaningful User-Agent.",
        "url": "https://musicbrainz.org/doc/MusicBrainz_API",
        "secret": "false",  # "secret" is a yes/no display flag  # nosec B105
    },
]

_KNOWN_CREDENTIAL_KEYS: frozenset[str] = frozenset(m["key"] for m in _API_CREDENTIALS_META)


async def _load_api_credentials(request: Request) -> list[dict[str, Any]]:
    """Load current credential values from DB and merge with metadata."""
    from sqlalchemy import select as _sel

    from mediacat.db.models import SiteSetting

    stored: dict[str, str] = {}
    try:
        async with _sf(request)() as db:
            rows = (await db.execute(_sel(SiteSetting))).scalars().all()
            stored = {r.key: r.value for r in rows}
    except Exception as exc:
        logger.warning("_load_api_credentials DB query failed: %s", exc)

    result = []
    for meta in _API_CREDENTIALS_META:
        raw = stored.get(meta["key"], "")
        is_secret = meta["secret"] == "true"  # noqa: S105 - a flag, not a password
        if raw and is_secret:
            masked = "•" * (len(raw) - 4) + raw[-4:] if len(raw) > 4 else "•" * len(raw)
        else:
            masked = raw
        result.append(
            {
                **meta,
                "value": masked,
                "is_set": bool(raw),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Import background task
# ---------------------------------------------------------------------------


def _get_app_discogs_token(request: Request) -> str:
    """Read the configured Discogs token from app state or secret file."""
    try:
        connectors = getattr(request.app.state, "connectors", {}) or {}
        dc = connectors.get("discogs")
        if dc and getattr(dc, "_auth_header", None):
            # "Discogs token=XXXX" → extract XXXX
            hdr = dc._auth_header
            if "token=" in hdr:
                return str(hdr.split("token=", 1)[1].strip())
    except Exception as exc:
        logger.debug("no Discogs token on the app connector: %s", exc)
    return ""


async def _resolve_discogs_token(request: Request) -> str:
    """Return the best available Discogs token.

    Priority: DB site_settings → app connector state → env / secret file.
    """
    from sqlalchemy import select as _sel

    from mediacat.db.models import SiteSetting

    try:
        async with _sf(request)() as db:
            row = (
                await db.execute(_sel(SiteSetting.value).where(SiteSetting.key == "discogs_token"))
            ).scalar_one_or_none()
        if row:
            return str(row)
    except Exception as exc:
        logger.debug("site_settings Discogs token lookup failed: %s", exc)
    return _get_app_discogs_token(request)


def _get_discogs_connector(
    request: Request, override_token: str = ""
) -> Any:  # empty means "use the stored token"  # nosec B107
    """Return a fresh DiscogsConnector for collection imports.

    Always creates a dedicated connector so the import's circuit-breaker state
    is isolated from the app's shared connector (used for single-release lookups).
    The auth token comes from the override, or falls back to the app-configured token.
    """
    from mediacat.ingestion.discogs import DiscogsConnector

    token = override_token
    if not token:
        token = _get_app_discogs_token(request)

    auth = f"Discogs token={token}" if token else None
    # Anonymous Discogs API limit is 25 req/min; use 20 to stay safe.
    # Authenticated limit is 60 req/min; use 40 to leave headroom.
    # Higher CB threshold because a few 403s on individual releases shouldn't
    # abort the whole import — the CB is for persistent service outages.
    connector = DiscogsConnector(
        name="discogs_collection",
        base_url="https://api.discogs.com",
        auth_header=auth,
        rate_limit=20 if not auth else 40,
        cb_threshold=15,
    )
    return connector


async def _refresh_existing_extras(
    connector: Any,
    sf: Any,
    token_id: Any,
    *,
    has_identifiers: bool,
    coll_ctx: dict[str, Any] | None,
    rel: dict[str, Any],
) -> None:
    """Skipped (already catalogued) release: keep its Discogs extras current.

    Custom field values arrive with the collection listing, so they cost no
    API call.  Runout identifiers need the release itself; it is fetched only
    when the token has none yet (tokens catalogued before identifiers were
    stored), so a re-run of the import backfills them once.
    """
    release = None
    if not has_identifiers:
        try:
            release = (await connector.fetch_release(str(rel.get("release_id")))).raw_payload
        except Exception as exc:
            logger.warning("Identifier backfill failed for %s: %s", rel.get("release_id"), exc)
    if release is None and not coll_ctx:
        return
    try:
        await _store_discogs_extras(sf, token_id, release=release, coll_ctx=coll_ctx, rel=rel)
    except Exception as exc:
        logger.warning("Discogs extras update failed for %s: %s", rel.get("release_id"), exc)


async def _store_discogs_extras(
    sf: Any,
    token_id: Any,
    *,
    release: dict[str, Any] | None,
    coll_ctx: dict[str, Any] | None,
    rel: dict[str, Any],
) -> None:
    """Store runout identifiers and collection custom fields in ``Token.extra``.

    *rel* is the collection listing entry (carries instance_id / folder_id /
    notes), so no extra API call is needed for the custom field values.
    """
    from sqlalchemy import select as _sel

    from mediacat.db.models import Token
    from mediacat.ingestion.discogs_extras import merge_collection, release_extra

    async with sf() as db:
        t = (await db.execute(_sel(Token).where(Token.id == token_id))).scalar_one_or_none()
        if t is None:
            return
        extra = dict(t.extra or {})
        if release is not None:
            extra["discogs"] = release_extra(release)
        if coll_ctx:
            extra["discogs_collection"] = merge_collection(
                extra.get("discogs_collection"),
                username=coll_ctx["username"],
                fields=coll_ctx["fields"],
                instance=rel if rel.get("instance_id") else None,
            )
        t.extra = extra
        await db.commit()


async def _run_import(
    job_id: str,
    discogs_username: str,
    connector: Any,
    sf: Any,
    store: Any,
    stop_event: asyncio.Event,
    reimport: bool = False,
) -> None:
    """Background task: import all releases from a Discogs user collection."""
    from sqlalchemy import select as _sel
    from sqlalchemy import update as _upd

    from mediacat.db.enums import ImportJobStatus, TokenStatus
    from mediacat.db.models import CollectionImportJob, Token
    from mediacat.web.catalogue import _import_discogs_images

    # The connector's token ("Discogs token=XXXX") also authenticates image downloads.
    image_token = str(getattr(connector, "_auth_header", "") or "").partition("token=")[2].strip()
    _opened_locally = False
    if not getattr(connector, "_client", None):
        await connector.open()
        _opened_locally = True

    async def _mark(status: ImportJobStatus, **extra: Any) -> None:
        try:
            async with sf() as db:
                vals: dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC), **extra}
                await db.execute(
                    _upd(CollectionImportJob)
                    .where(CollectionImportJob.id == uuid.UUID(job_id))
                    .values(**vals)
                )
                await db.commit()
        except Exception as exc:
            logger.warning("_mark job %s failed: %s", job_id, exc)

    try:
        await _mark(ImportJobStatus.RUNNING)

        # Collection custom field definitions — fetched once per run.  Only
        # possible when the token belongs to the collection owner.
        coll_ctx: dict[str, Any] | None = None
        try:
            from mediacat.ingestion.discogs_extras import clean_field_defs

            owner = await connector.fetch_identity()
            if owner and owner.lower() == discogs_username.lower():
                coll_ctx = {
                    "username": owner,
                    "fields": clean_field_defs(await connector.fetch_collection_fields(owner)),
                }
        except Exception as exc:
            logger.info("Collection custom fields unavailable for import: %s", exc)

        # Fetch page 1 to get total count + start page from last_page
        async with sf() as db:
            job = (
                await db.execute(
                    _sel(CollectionImportJob).where(CollectionImportJob.id == uuid.UUID(job_id))
                )
            ).scalar_one_or_none()
            start_page = job.last_page if job else 1
            imported = job.imported_count if job else 0
            skipped = job.skipped_count if job else 0
            failed = job.failed_count if job else 0
        # On resume, the releases before this position were already counted.
        resume_done = imported + skipped + failed
        per_page = 50

        page = start_page
        pages_total = None
        failed_ids: list[str] = []

        while True:
            if stop_event.is_set():
                await _mark(ImportJobStatus.PAUSED)
                return

            coll = await connector.fetch_user_collection(
                discogs_username, page=page, per_page=per_page
            )
            if coll.get("error") or not coll.get("releases"):
                error_msg = coll.get("error") or f"No releases returned on page {page}"
                if "403" in str(error_msg):
                    error_msg = (
                        error_msg
                        + " — your Discogs collection may be private or require authentication."
                        + " Provide a personal access token to import it."
                    )
                if pages_total is None:
                    await _mark(ImportJobStatus.FAILED, error_message=error_msg[:500])
                    return
                # Mid-import page failure — save progress, report clearly
                await _mark(
                    ImportJobStatus.FAILED,
                    error_message=f"Page {page} failed: {error_msg[:400]}",
                    imported_count=imported,
                    skipped_count=skipped,
                    failed_count=failed,
                    last_page=page,
                )
                return

            if pages_total is None:
                pages_total = coll.get("pages", 1)
                total_items = coll.get("items", 0)
                await _mark(
                    ImportJobStatus.RUNNING,
                    total_releases=total_items,
                )

            consecutive_failures = 0
            for pos, rel in enumerate(coll["releases"]):
                if (page - 1) * per_page + pos < resume_done:
                    continue  # handled before the pause
                if stop_event.is_set():
                    await _mark(
                        ImportJobStatus.PAUSED,
                        imported_count=imported,
                        skipped_count=skipped,
                        failed_count=failed,
                        last_page=page,
                    )
                    return

                release_id_str = str(rel.get("release_id", ""))
                if not release_id_str or release_id_str == "0":
                    failed += 1
                    continue

                release_id_int = int(release_id_str)

                # Check if already imported
                existing_id = None
                try:
                    async with sf() as db:
                        existing = (
                            await db.execute(
                                _sel(Token.id, Token.extra)
                                .where(
                                    Token.discogs_release_id == release_id_int,
                                    Token.deleted_at.is_(None),
                                )
                                .limit(1)
                            )
                        ).first()
                    existing_id = existing[0] if existing else None
                    if existing_id and not reimport:
                        await _refresh_existing_extras(
                            connector,
                            sf,
                            existing_id,
                            has_identifiers="discogs" in (existing[1] or {}),
                            coll_ctx=coll_ctx,
                            rel=rel,
                        )
                        skipped += 1
                        consecutive_failures = 0
                        continue
                except Exception as exc:
                    logger.warning(
                        "Skip-check failed for release %s: %s — skipping to avoid duplicate",
                        release_id_str,
                        exc,
                    )
                    skipped += 1
                    consecutive_failures = 0
                    continue

                # Import or re-fetch the release
                try:
                    fetch_result = await connector.fetch_release(release_id_str)
                    data = fetch_result.raw_payload
                    from mediacat.web.catalogue import _get_or_create_country, _map_discogs_fields

                    mapped = _map_discogs_fields(data)
                    country_name = mapped.pop("country_name", "") or ""
                    country_id = None
                    if country_name:
                        async with sf() as db:
                            country_id = await _get_or_create_country(db, country_name)
                            await db.commit()

                    if existing_id and reimport:
                        # Update enrichment fields on the existing token
                        _skip_on_update = {
                            "id",
                            "status",
                            "discogs_release_id",
                            "artist",
                            "title",
                            "year",
                            "country_id",
                            "media_format",
                        }
                        update_vals = {
                            k: v
                            for k, v in mapped.items()
                            if hasattr(Token, k) and k not in _skip_on_update and v is not None
                        }
                        if country_id:
                            async with sf() as db:
                                no_country = (
                                    await db.execute(
                                        _sel(Token.id).where(
                                            Token.id == existing_id, Token.country_id.is_(None)
                                        )
                                    )
                                ).scalar_one_or_none()
                            if no_country:
                                update_vals["country_id"] = country_id
                        if update_vals:
                            async with sf() as db:
                                await db.execute(
                                    _upd(Token).where(Token.id == existing_id).values(**update_vals)
                                )
                                await db.commit()
                        token_id = existing_id
                    else:
                        token_id = uuid.uuid4()
                        _explicit = {"id", "status", "discogs_release_id"}
                        async with sf() as db:
                            token = Token(
                                id=token_id,
                                status=TokenStatus.ACTIVE,
                                discogs_release_id=release_id_int,
                                country_id=country_id,
                                **{
                                    k: v
                                    for k, v in mapped.items()
                                    if hasattr(Token, k) and k not in _explicit
                                },
                            )
                            db.add(token)
                            await db.commit()

                    try:
                        await _store_discogs_extras(
                            sf, token_id, release=data, coll_ctx=coll_ctx, rel=rel
                        )
                    except Exception as exc:
                        logger.warning(
                            "Discogs extras store failed for release %s: %s", release_id_str, exc
                        )

                    # Import images (up to 4 to stay conservative)
                    if store and data.get("images"):
                        try:
                            async with sf() as db:
                                count = await _import_discogs_images(
                                    store, db, token_id, data["images"][:4], image_token
                                )
                                if count:
                                    await db.commit()
                        except Exception as exc:
                            logger.warning(
                                "Image import failed for release %s: %s", release_id_str, exc
                            )

                    imported += 1
                    consecutive_failures = 0
                    await asyncio.sleep(1.5)  # extra politeness between releases

                except Exception as exc:
                    err_str = str(exc)
                    logger.warning("Import failed for release %s: %s", release_id_str, err_str)
                    failed += 1
                    failed_ids.append(release_id_str)
                    consecutive_failures += 1
                    # Abort early if many consecutive failures — likely an auth or API issue
                    if consecutive_failures >= 8:
                        hint = (
                            " — provide a Discogs personal access token to fix this"
                            if "403" in err_str or "circuit breaker" in err_str
                            else ""
                        )
                        ids_hint = (
                            f" Failed IDs: {', '.join(failed_ids[-20:])}" if failed_ids else ""
                        )
                        await _mark(
                            ImportJobStatus.FAILED,
                            error_message=(
                                f"Aborted after {consecutive_failures} consecutive failures."
                                f" Last error: {err_str[:300]}{hint}{ids_hint}"
                            ),
                            imported_count=imported,
                            skipped_count=skipped,
                            failed_count=failed,
                            last_page=page,
                        )
                        return
                    await asyncio.sleep(0.5)

            await _mark(
                ImportJobStatus.RUNNING,
                imported_count=imported,
                skipped_count=skipped,
                failed_count=failed,
                last_page=page,
            )

            if pages_total and page >= pages_total:
                break
            page += 1

        failed_msg = (
            f"Failed release IDs (re-run import to retry): {', '.join(failed_ids)}"
            if failed_ids
            else None
        )
        await _mark(
            ImportJobStatus.COMPLETED,
            imported_count=imported,
            skipped_count=skipped,
            failed_count=failed,
            last_page=page,
            error_message=failed_msg,
        )
        logger.info(
            "Collection import completed: job=%s imported=%d skipped=%d failed=%d%s",
            job_id,
            imported,
            skipped,
            failed,
            f" failed_ids={failed_ids}" if failed_ids else "",
        )

    except Exception as exc:
        logger.error("Collection import task crashed: job=%s error=%s", job_id, exc)
        await _mark(
            ImportJobStatus.FAILED,
            error_message=str(exc)[:500],
            imported_count=imported if "imported" in dir() else 0,
        )
    finally:
        if _opened_locally:
            with contextlib.suppress(Exception):
                await connector.close()
        _stop_flags.pop(job_id, None)


# ---------------------------------------------------------------------------
# Backup & Restore
# ---------------------------------------------------------------------------

# Tables exported in dependency order (parents before children).
_BACKUP_TABLES = [
    "users",
    "countries",
    "labels",
    "manufacturers",
    "tokens",
    "token_revisions",
    "media_objects",
    "ocr_artifacts",
    "token_price_history",
    "token_symbols",
    "review_items",
    "streaming_links",
]

_JSON_DEFAULT = str  # fallback serialiser for uuid / datetime / Decimal

# Columns pointing at a table restored later (tokens ↔ token_revisions):
# inserted as NULL, then set once every table is back.
_RESTORE_LATER: dict[str, tuple[str, ...]] = {"tokens": ("current_revision_id",)}


async def _column_types(db: Any, table: str) -> dict[str, str]:
    """``{column: data_type}`` of *table* from information_schema."""
    from sqlalchemy import text as _text

    rows = await db.execute(
        _text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {r.column_name: r.data_type for r in rows}


# Column types the export wrote as text (besides timestamps).
_FROM_TEXT: dict[str, Callable[[str], Any]] = {
    "date": date.fromisoformat,
    "uuid": uuid.UUID,
    "numeric": Decimal,
}


def _restore_value(value: Any, data_type: str) -> Any:
    """Undo the export's text encoding so asyncpg gets the column's Python type."""
    if value is None:
        return None
    if data_type in ("json", "jsonb"):
        return json.dumps(value)  # asyncpg takes JSON parameters as text
    if not isinstance(value, str):
        return value
    if data_type.startswith("timestamp"):
        return datetime.fromisoformat(value)
    convert = _FROM_TEXT.get(data_type)
    return convert(value) if convert else value


@admin_router.get("/backup/export")
async def backup_export(request: Request) -> StreamingResponse:
    """Stream a ZIP archive containing JSON dumps of all catalogue tables."""
    _require_role(request, "admin")

    from sqlalchemy import text as _text

    buf = io.BytesIO()
    manifest: dict[str, Any] = {
        "mediacat_backup": True,
        "exported_at": datetime.now(UTC).isoformat(),
        "tables": _BACKUP_TABLES,
    }

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, default=_JSON_DEFAULT, indent=2))

        for table in _BACKUP_TABLES:
            try:
                async with _sf(request)() as db:
                    result = await db.execute(_text(f"SELECT * FROM {table}"))  # noqa: S608  # table from _BACKUP_TABLES  # nosec B608
                    cols = list(result.keys())
                    rows = result.fetchall()

                records: list[dict[str, Any]] = []
                for row in rows:
                    rec: dict[str, Any] = {}
                    for col, raw_val in zip(cols, row, strict=False):
                        val = raw_val
                        if hasattr(val, "isoformat"):
                            val = val.isoformat()
                        elif isinstance(val, uuid.UUID):
                            val = str(val)
                        rec[col] = val
                    records.append(rec)

                zf.writestr(f"{table}.json", json.dumps(records, default=_JSON_DEFAULT))
                logger.info("backup_export: %s — %d rows", table, len(records))
            except Exception as exc:
                logger.warning("backup_export: skipped table %s: %s", table, exc)
                zf.writestr(f"{table}.json", json.dumps([]))

    buf.seek(0)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"mediacat_backup_{stamp}.zip"

    return StreamingResponse(
        io.BytesIO(buf.getvalue()),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@admin_router.post("/backup/import", response_model=None)
async def backup_import(
    request: Request,
    file: UploadFile = File(...),
) -> RedirectResponse:
    """Restore catalogue data from an uploaded ZIP backup."""
    _require_role(request, "admin")

    raw = await file.read()
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        return RedirectResponse(url="/admin?tab=backup&error=invalid_zip", status_code=303)

    from sqlalchemy import text as _text

    results: dict[str, int] = {}
    errors: list[str] = []
    later: list[tuple[str, str, Any, Any]] = []  # (table, column, row id, value)

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        # Validate manifest
        if "manifest.json" not in zf.namelist():
            return RedirectResponse(url="/admin?tab=backup&error=missing_manifest", status_code=303)
        manifest = json.loads(zf.read("manifest.json"))
        if not manifest.get("mediacat_backup"):
            return RedirectResponse(url="/admin?tab=backup&error=invalid_manifest", status_code=303)

        ident_re = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

        for table in _BACKUP_TABLES:
            fname = f"{table}.json"
            if fname not in zf.namelist():
                logger.info("backup_import: no file for table %s — skipping", table)
                continue
            records: list[dict[str, Any]] = json.loads(zf.read(fname))
            if not records:
                results[table] = 0
                continue

            # Union of keys, first-seen order: a key missing from the first record still counts.
            raw_cols = list(dict.fromkeys(k for rec in records for k in rec))
            invalid = [c for c in raw_cols if not ident_re.match(c)]
            if invalid:
                logger.warning(
                    "backup_import: rejected %s — invalid column names: %s", table, invalid
                )
                errors.append(f"{table}: invalid column names in backup")
                continue

            async with _sf(request)() as db:
                col_types = await _column_types(db, table)
            unknown = [c for c in raw_cols if c not in col_types]
            if unknown:
                errors.append(f"{table}: columns not in this schema: {', '.join(unknown)}")
                continue

            cols = raw_cols
            col_list = ", ".join(cols)
            placeholders = ", ".join(f":{c}" for c in cols)
            sql = _text(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"  # noqa: S608  # table from _BACKUP_TABLES, columns regex-checked  # nosec B608
            )
            inserted = 0
            for rec in records:
                try:
                    params = {c: _restore_value(rec.get(c), col_types[c]) for c in cols}
                    for col in _RESTORE_LATER.get(table, ()):
                        if params.get(col) is not None:
                            later.append((table, col, params["id"], params[col]))
                            params[col] = None
                    async with _sf(request)() as db:
                        result = await db.execute(sql, params)
                        await db.commit()
                    inserted += result.rowcount or 0  # 0 when the row already exists
                except Exception as exc:
                    errors.append(f"{table}: {exc}")
            results[table] = inserted
            logger.info("backup_import: %s — %d/%d rows inserted", table, inserted, len(records))

    for table, col, row_id, value in later:
        try:
            async with _sf(request)() as db:
                await db.execute(
                    _text(f"UPDATE {table} SET {col} = :v WHERE id = :id AND {col} IS NULL"),  # noqa: S608  # names from _RESTORE_LATER  # nosec B608
                    {"v": value, "id": row_id},
                )
                await db.commit()
        except Exception as exc:
            errors.append(f"{table}.{col}: {exc}")

    total = sum(results.values())
    logger.info(
        "backup_import complete: %d rows restored across %d tables; %d errors",
        total,
        len(results),
        len(errors),
    )
    if errors:
        logger.warning("backup_import errors: %s", errors[:10])

    return RedirectResponse(url=f"/admin?tab=backup&msg=import_done&rows={total}", status_code=303)


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

_HK_RETENTION_DAYS = 30


@admin_router.get("/housekeeping/search", response_class=HTMLResponse)
async def housekeeping_search(
    request: Request,
    q: str = Query(""),
    fmt: str = Query(""),
) -> HTMLResponse:
    """HTMX: return a table of active pressings matching the search."""
    _require_role(request, "admin")
    from sqlalchemy import or_
    from sqlalchemy import select as _sel

    from mediacat.db.enums import TokenStatus
    from mediacat.db.models import Token

    try:
        async with _sf(request)() as db:
            stmt = (
                _sel(
                    Token.id,
                    Token.artist,
                    Token.title,
                    Token.year,
                    Token.media_format,
                    Token.catalog_number,
                    Token.discogs_release_id,
                )
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
            )
            if q.strip():
                stmt = stmt.where(
                    or_(Token.artist.ilike(f"%{q.strip()}%"), Token.title.ilike(f"%{q.strip()}%"))
                )
            if fmt in ("vinyl", "cd"):
                stmt = stmt.where(Token.media_format == fmt)
            rows = (
                await db.execute(stmt.order_by(Token.artist.asc(), Token.title.asc()).limit(200))
            ).all()
    except Exception as exc:
        logger.error("housekeeping_search error: %s", exc)
        return HTMLResponse(
            '<p class="text-muted" style="color:var(--danger)">Search failed — see server log.</p>'
        )

    if not rows:
        return HTMLResponse('<p class="text-muted">No pressings found.</p>')

    import html as _html

    def _row(r: Any) -> str:
        fmt_val = r.media_format.value if r.media_format else ""
        return (
            f'<tr><td><input type="checkbox" class="hk-check" name="token_ids" value="{r.id}"></td>'
            f"<td>{_html.escape(r.artist or '')}</td>"
            f'<td><a href="/catalogue/{r.id}" target="_blank">{_html.escape(r.title or "")}</a></td>'
            f"<td>{r.year or '—'}</td>"
            f"<td>{fmt_val.upper()}</td>"
            f'<td class="text-muted" style="font-size:.82em">{_html.escape(r.catalog_number or "")}</td>'
            f'<td class="text-muted" style="font-size:.78em">{r.discogs_release_id or ""}</td>'
            f"</tr>"
        )

    body = "".join(_row(r) for r in rows)
    return HTMLResponse(
        f'<p class="text-muted" style="font-size:.82em;margin-bottom:.4rem">{len(rows)} pressing(s) found</p>'
        f'<table class="hk-table"><thead><tr>'
        f'<th><input type="checkbox" id="hk-check-all" title="Select all"></th>'
        f"<th>Artist</th><th>Title</th><th>Year</th><th>Format</th>"
        f"<th>Cat. No.</th><th>Discogs ID</th></tr></thead><tbody>{body}</tbody></table>"
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@admin_router.post("/housekeeping/bulk-delete", response_model=None)
async def housekeeping_bulk_delete(
    request: Request,
    token_ids: list[str] = Form(default=[]),
    confirm: str = Form(""),
) -> Any:
    """Soft-delete or confirm soft-delete of multiple pressings."""
    _require_role(request, "admin")
    from sqlalchemy import select as _sel

    from mediacat.db.models import Token

    ids = [i.strip() for i in token_ids if _is_uuid(i.strip())]
    if not ids:
        return RedirectResponse(url="/admin?tab=housekeeping&msg=no_selection", status_code=303)

    if confirm != "1":
        # First step: show confirmation
        try:
            async with _sf(request)() as db:
                rows = (
                    await db.execute(
                        _sel(Token.id, Token.artist, Token.title, Token.year, Token.media_format)
                        .where(Token.id.in_(ids))
                        .where(Token.deleted_at.is_(None))
                    )
                ).all()
        except Exception as exc:
            logger.error("housekeeping bulk-delete confirm query failed: %s", exc)
            rows = []

        import html as _html

        items_html = "".join(
            f"<li>{_html.escape(r.artist or '')} — {_html.escape(r.title or '')}"
            f" ({r.year or '?'}, {r.media_format.value if r.media_format else '?'})</li>"
            for r in rows
        )
        hidden_ids = "".join(f'<input type="hidden" name="token_ids" value="{i}">' for i in ids)

        ctx = _ctx(
            request,
            tab="housekeeping",
            users=[],
            jobs=[],
            active_job_ids=set(),
            timeout_options=_TIMEOUT_OPTIONS,
            roles=list(_VALID_ROLES),
            deleted_pressings=[],
            pending_delete_items=items_html,
            pending_delete_count=len(rows),
            pending_delete_hidden=hidden_ids,
        )
        return _tmpl().TemplateResponse(request=request, name="admin.html", context=ctx)

    # Confirmed: soft-delete
    from sqlalchemy import update as _upd

    from mediacat.db.enums import TokenStatus
    from mediacat.db.models import Token

    deleted_count = 0
    try:
        async with _sf(request)() as db:
            result = await db.execute(
                _upd(Token)
                .where(Token.id.in_(ids))
                .where(Token.deleted_at.is_(None))
                .values(deleted_at=datetime.now(UTC), status=TokenStatus.ARCHIVED)
            )
            deleted_count = result.rowcount
            await db.commit()
        logger.info(
            "Housekeeping: soft-deleted %d tokens by %s", deleted_count, request.state.user_id
        )
    except Exception as exc:
        logger.error("housekeeping bulk-delete failed: %s", exc)
        return RedirectResponse(url="/admin?tab=housekeeping&error=delete_failed", status_code=303)

    return RedirectResponse(
        url=f"/admin?tab=housekeeping&msg=deleted&count={deleted_count}", status_code=303
    )


@admin_router.post("/housekeeping/recover", response_model=None)
async def housekeeping_recover(
    request: Request,
    token_id: str = Form(...),
) -> RedirectResponse:
    """Un-delete a soft-deleted pressing."""
    _require_role(request, "admin")
    from sqlalchemy import update as _upd

    from mediacat.db.enums import TokenStatus
    from mediacat.db.models import Token

    try:
        async with _sf(request)() as db:
            await db.execute(
                _upd(Token)
                .where(Token.id == token_id.strip())
                .where(Token.deleted_at.isnot(None))
                .values(deleted_at=None, status=TokenStatus.ACTIVE)
            )
            await db.commit()
        logger.info("Housekeeping: recovered token %s by %s", token_id, request.state.user_id)
    except Exception as exc:
        logger.error("housekeeping recover failed: %s", exc)
        return RedirectResponse(url="/admin?tab=housekeeping&error=recover_failed", status_code=303)

    return RedirectResponse(url="/admin?tab=housekeeping&msg=recovered", status_code=303)


@admin_router.post("/housekeeping/purge-expired", response_model=None)
async def housekeeping_purge_expired(request: Request) -> RedirectResponse:
    """Hard-delete tokens that have been soft-deleted for more than 30 days."""
    _require_role(request, "admin")
    from datetime import timedelta

    from sqlalchemy import delete as _del

    from mediacat.db.models import Token

    cutoff = datetime.now(UTC) - timedelta(days=_HK_RETENTION_DAYS)
    try:
        async with _sf(request)() as db:
            result = await db.execute(
                _del(Token).where(Token.deleted_at.isnot(None)).where(Token.deleted_at < cutoff)
            )
            purged = result.rowcount
            await db.commit()
        logger.info(
            "Housekeeping: purged %d tokens older than %d days by %s",
            purged,
            _HK_RETENTION_DAYS,
            request.state.user_id,
        )
    except Exception as exc:
        logger.error("housekeeping purge-expired failed: %s", exc)
        return RedirectResponse(url="/admin?tab=housekeeping&error=purge_failed", status_code=303)

    return RedirectResponse(
        url=f"/admin?tab=housekeeping&msg=purged&count={purged}", status_code=303
    )
