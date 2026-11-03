"""Discogs pressing details — runout variants, collection fields, album sheet.

URL structure
-------------
POST /catalogue/{token_id}/discogs-details/refresh   re-fetch identifiers + collection fields
POST /catalogue/{token_id}/discogs-collection        save collection fields (optionally write back)
GET  /catalogue/{token_id}/print.pdf                 A4 album sheet as PDF
GET  /catalogue/new/discogs-preview/{release_id}     images + first runout variant (Add pressing cards)

Collection custom fields ("Runout / Matrix LP1 - A", "Autographed",
"Signed by", …) live on the *collection instance* of a release, so reading and
writing them needs the personal access token configured under
Admin → API Tokens.  Release identifiers are public and need no token.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from mediacat.db.models import Token
from mediacat.ingestion.discogs_extras import (
    clean_field_defs,
    collection_view,
    display_artist,
    lp_letters,
    merge_collection,
    release_extra,
    release_preview,
    track_lp,
)
from mediacat.web.routes import _ctx, _require_role, _tmpl

logger = logging.getLogger(__name__)

discogs_details_router = APIRouter(prefix="/catalogue", tags=["catalogue"])


def _sf(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.db_session_factory


async def running_discogs_import(request: Request) -> dict[str, Any] | None:
    """The Discogs collection import in progress, if any (for the album page banner).

    An import uses the whole Discogs API allowance, so on-demand Discogs calls
    (refresh, load variants, sync, write-back) get rate-limited meanwhile.  A
    job counts only while it reports progress: a "running" row untouched for
    10 minutes is treated as stale.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from mediacat.db.enums import ImportJobStatus  # noqa: PLC0415
    from mediacat.db.models import CollectionImportJob  # noqa: PLC0415

    try:
        async with _sf(request)() as db:
            job = (
                await db.execute(
                    select(CollectionImportJob)
                    .where(
                        CollectionImportJob.status == ImportJobStatus.RUNNING,
                        CollectionImportJob.updated_at > datetime.now(UTC) - timedelta(minutes=10),
                    )
                    .order_by(CollectionImportJob.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception as exc:
        logger.debug("import status check skipped: %s", exc)
        return None
    if job is None:
        return None
    done = (job.imported_count or 0) + (job.skipped_count or 0) + (job.failed_count or 0)
    return {"username": job.discogs_username, "done": done, "total": job.total_releases or 0}


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Not found") from None


async def _load(request: Request, token_id: str, *, media: bool = False) -> Token:
    opts = [selectinload(Token.country)]
    if media:
        opts.append(selectinload(Token.media_objects))
    async with _sf(request)() as db:
        token = (
            await db.execute(
                select(Token)
                .options(*opts)
                .where(Token.id == _parse_uuid(token_id), Token.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="Not found")
    return token  # type: ignore[no-any-return]


async def _save_extra(request: Request, token_id: uuid.UUID, updates: dict[str, Any]) -> None:
    """Replace top-level keys of ``Token.extra`` inside a fresh session."""
    async with _sf(request)() as db:
        t = (await db.execute(select(Token).where(Token.id == token_id))).scalar_one()
        extra = dict(t.extra or {})
        extra.update(updates)
        t.extra = extra
        await db.commit()


# ---------------------------------------------------------------------------
# Discogs access helpers (shared with sync / import)
# ---------------------------------------------------------------------------


async def open_collection_connector(request: Request) -> Any | None:
    """Return an opened DiscogsConnector using the admin-configured token.

    ``None`` when no token is configured — collection fields are private.
    """
    from mediacat.web.admin import (  # noqa: PLC0415 - avoid circular import
        _get_discogs_connector,
        _resolve_discogs_token,
    )

    token = await _resolve_discogs_token(request)
    if not token:
        return None
    connector = _get_discogs_connector(request, token)
    await connector.open()
    return connector


async def fetch_collection_state(
    connector: Any,
    release_id: int,
    existing: dict[str, Any] | None,
    *,
    username: str | None = None,
    fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch field definitions + this release's collection instance and merge.

    *username* and *fields* may be passed in by bulk callers to avoid
    re-fetching them for every release.
    """
    username = username or await connector.fetch_identity()
    if fields is None:
        fields = clean_field_defs(await connector.fetch_collection_fields(username))
    instances = await connector.fetch_collection_instances(username, release_id)
    return merge_collection(
        existing, username=username, fields=fields, instance=instances[0] if instances else None
    )


RATE_LIMIT_WAITS = (2.0, 4.0, 6.0)  # seconds between 429 retries (Discogs uses a 60 s window)
RATE_LIMITED_MSG = (
    "identifiers (Discogs rate limit reached; a running collection import uses the whole "
    "allowance. Try again in a minute, or let the import finish, it stores the variants too)"
)


async def _fetch_release_patiently(release_id: int, token: str | None = None) -> dict[str, Any]:
    """Fetch a release, riding out a short 429 burst.

    Discogs allows ~60 requests a minute per token; a collection import in
    progress uses all of it, so a manual refresh retries a few times before
    giving up (the final 429 propagates).
    """
    from mediacat.web.catalogue import _fetch_discogs_release  # noqa: PLC0415 - circular

    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            return await _fetch_discogs_release(release_id, token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or wait is None:
                raise
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


async def refresh_token_discogs_details(
    request: Request, token: Token, release: dict[str, Any] | None = None
) -> list[str]:
    """Re-fetch identifiers and collection fields for *token*; returns errors."""
    errors: list[str] = []
    updates: dict[str, Any] = {}
    rid = token.discogs_release_id
    if not rid:
        return ["no Discogs release ID"]

    try:
        if release is not None:
            data = release
        else:
            from mediacat.web.catalogue import _discogs_token  # noqa: PLC0415 - circular

            data = await _fetch_release_patiently(rid, await _discogs_token(request))
        updates["discogs"] = release_extra(data)
    except Exception as exc:
        logger.warning("identifier refresh failed token=%s: %s", token.id, exc)
        rate_limited = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429
        errors.append(RATE_LIMITED_MSG if rate_limited else "identifiers")

    connector = None
    try:
        connector = await open_collection_connector(request)
        if connector is None:
            errors.append("collection (no Discogs token configured)")
        else:
            updates["discogs_collection"] = await fetch_collection_state(
                connector, rid, (token.extra or {}).get("discogs_collection")
            )
    except Exception as exc:
        logger.warning("collection refresh failed token=%s: %s", token.id, exc)
        errors.append("collection")
    finally:
        if connector is not None:
            await connector.close()

    if updates:
        await _save_extra(request, token.id, updates)
    return errors


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@discogs_details_router.post("/{token_id}/discogs-details/refresh", response_model=None)
async def discogs_details_refresh(request: Request, token_id: str) -> RedirectResponse:
    """Re-fetch runout identifiers and collection custom fields from Discogs."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load(request, token_id)
    if not token.discogs_release_id:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_discogs_id", status_code=303)
    errors = await refresh_token_discogs_details(request, token)
    qs = "msg=discogs_details_refreshed"
    if errors:
        qs = "msg=discogs_details_partial&err=" + urllib.parse.quote(", ".join(errors))
    return RedirectResponse(url=f"/catalogue/{token_id}?{qs}#discogs-copy", status_code=303)


async def _write_back(request: Request, token: Token, coll: dict[str, Any]) -> tuple[str, str, int]:
    """Push changed collection fields to Discogs; updates ``coll`` in place.

    Returns ``(msg, error, fields_written)`` for the redirect query string.
    """
    values = coll.get("values") or {}
    remote = dict(coll.get("discogs_values") or {})
    pending = [
        f for f in coll["fields"] if values.get(str(f["id"]), "") != remote.get(str(f["id"]), "")
    ]
    if not coll.get("instance_id") or not coll.get("folder_id"):
        return "writeback_not_in_collection", "", 0
    if not pending:
        return "writeback_nothing", "", 0

    msg, err, written = "writeback_ok", "", 0
    connector = None
    try:
        connector = await open_collection_connector(request)
        if connector is None:
            return "writeback_failed", "no Discogs token configured", 0
        for f in pending:
            fid = str(f["id"])
            await connector.set_collection_field(
                coll["username"],
                folder_id=int(coll["folder_id"]),
                release_id=token.discogs_release_id or 0,
                instance_id=int(coll["instance_id"]),
                field_id=int(f["id"]),
                value=values.get(fid, ""),
            )
            remote[fid] = values.get(fid, "")
            written += 1
    except Exception as exc:
        logger.warning("Discogs write-back failed token=%s: %s", token.id, exc)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        msg, err = "writeback_failed", f"HTTP {status}" if status else str(exc)[:120]
    finally:
        if connector is not None:
            await connector.close()
    coll["discogs_values"] = remote
    logger.info(
        "Discogs write-back token=%s fields=%d by=%s", token.id, written, request.state.user_id
    )
    return msg, err, written


@discogs_details_router.post("/{token_id}/discogs-collection", response_model=None)
async def discogs_collection_save(request: Request, token_id: str) -> RedirectResponse:
    """Save collection field values locally; with op=writeback push to Discogs."""
    _require_role(request, "admin", "reviewer", "contributor")
    form = await request.form()
    writeback = form.get("op") == "writeback"
    if writeback:
        _require_role(request, "admin", "reviewer")

    token = await _load(request, token_id)
    coll = dict((token.extra or {}).get("discogs_collection") or {})
    if not coll.get("fields"):
        return RedirectResponse(
            url=f"/catalogue/{token_id}?msg=collection_not_loaded#discogs-copy", status_code=303
        )

    values = dict(coll.get("values") or {})
    for f in coll["fields"]:
        key = f"f_{f['id']}"
        if key in form:
            values[str(f["id"])] = str(form.get(key) or "").strip()
    coll["values"] = values

    msg, err, count = "collection_saved", "", 0
    if writeback:
        msg, err, count = await _write_back(request, token, coll)

    await _save_extra(request, token.id, {"discogs_collection": coll})
    qs = f"msg={msg}&n={count}" + (f"&err={urllib.parse.quote(err)}" if err else "")
    return RedirectResponse(url=f"/catalogue/{token_id}?{qs}#discogs-copy", status_code=303)


# ---------------------------------------------------------------------------
# Album sheet PDF
# ---------------------------------------------------------------------------


def build_sheet_data(token: Token) -> dict[str, Any]:
    """Assemble the album sheet content for :func:`build_album_sheet`."""
    extra = token.extra or {}
    view = collection_view(extra)
    known = view["known"]

    tracks_by_lp: dict[int, list[tuple[str, str]]] = {}
    for t in token.tracklist or []:
        if isinstance(t, dict) and t.get("title"):
            tracks_by_lp.setdefault(track_lp(t), []).append(
                (str(t.get("position") or ""), str(t["title"]))
            )

    # Collection fields first; LP 1 falls back to MediaCat's own matrix fields.
    runout = {
        (1, "A"): known.get("runout_lp1_a") or token.matrix_runout or "",
        (1, "B"): known.get("runout_lp1_b") or token.matrix_runout_b or "",
        (2, "A"): known.get("runout_lp2_a") or "",
        (2, "B"): known.get("runout_lp2_b") or "",
    }

    lp_total = (extra.get("discogs") or {}).get("format_qty") or max(tracks_by_lp, default=1)
    lps = []
    for lp in (1, 2):
        has_content = tracks_by_lp.get(lp) or runout[(lp, "A")] or runout[(lp, "B")]
        if lp == 1 or lp_total >= lp or has_content:
            a, b = lp_letters(lp)
            lps.append(
                {
                    "lp": lp,
                    "tracks": tracks_by_lp.get(lp, []),
                    "runouts": [(f"Side {a}", runout[(lp, "A")]), (f"Side {b}", runout[(lp, "B")])],
                }
            )

    return {
        "artist": display_artist(token.artist) or None,
        "title": token.title,
        "year": token.year,
        "country": token.country.name if token.country else None,
        "lps": lps,
        "lp_total": max(int(lp_total or 1), max(tracks_by_lp, default=1)),
        "signed": view["signed"],
        "signed_by": view["signed_by"],
        "signed_on": view["signed_on"],
        "price_estimate": view["price_estimate"],
    }


def sheet_filename(token: Token) -> str:
    base = " - ".join(p for p in (token.artist, token.title) if p) or "album"
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", base).strip(" .")[:150]
    return f"{base}.pdf"


@discogs_details_router.get("/{token_id}/print.pdf")
async def album_sheet_pdf(request: Request, token_id: str) -> Response:
    """Render the A4 album sheet for printing / saving as PDF."""
    # Lazy: the app still boots on an image built before reportlab was added.
    from mediacat.web.print_sheet import build_album_sheet  # noqa: PLC0415

    token = await _load(request, token_id, media=True)

    cover_bytes: bytes | None = None
    media = list(token.media_objects or [])
    cover = (
        next((m for m in media if m.is_primary_cover), None)
        or next((m for m in media if m.region and m.region.value == "cover_front"), None)
        or next(iter(media), None)
    )
    store = getattr(request.app.state, "object_store", None)
    if cover and store:
        try:
            cover_bytes = await store.get_object(
                cover.object_key, cover.bucket, max_bytes=30_000_000
            )
        except Exception as exc:
            logger.warning("Album sheet: cover fetch failed token=%s: %s", token_id, exc)

    pdf = build_album_sheet(build_sheet_data(token), cover_bytes)
    filename = sheet_filename(token)
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{urllib.parse.quote(filename)}"
            ),
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Add pressing: release preview cards
# ---------------------------------------------------------------------------

#: Release previews by Discogs id: (monotonic time, preview).  Re-searching or
#: switching views does not spend the shared ~60 requests/minute again.
_PREVIEW_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
_PREVIEW_TTL = 3600.0
_PREVIEW_MAX = 500


async def _fetch_release_as(release_id: int, token: str) -> dict[str, Any]:
    """Fetch a release with the admin-configured token, riding out 429 bursts."""
    from mediacat.web.catalogue import _DISCOGS_BASE, _USER_AGENT  # noqa: PLC0415 - circular

    headers = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        for wait in (*RATE_LIMIT_WAITS, None):
            r = await client.get(f"{_DISCOGS_BASE}/releases/{release_id}", headers=headers)
            if r.status_code != 429 or wait is None:
                r.raise_for_status()
                return r.json()  # type: ignore[no-any-return]
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


@discogs_details_router.get("/new/discogs-preview/{release_id}", response_class=HTMLResponse)
async def discogs_release_preview(request: Request, release_id: int) -> HTMLResponse:
    """Images and first runout variant of a search result (HTMX, lazy per card)."""
    _require_role(request, "admin", "reviewer", "contributor")
    from mediacat.web.admin import _resolve_discogs_token  # noqa: PLC0415 - circular

    preview: dict[str, Any] | None = None
    error = ""
    cached = _PREVIEW_CACHE.get(release_id)
    if cached and time.monotonic() - cached[0] < _PREVIEW_TTL:
        preview = cached[1]
    else:
        try:
            data = await _fetch_release_as(release_id, await _resolve_discogs_token(request))
            preview = release_preview(data)
            if len(_PREVIEW_CACHE) >= _PREVIEW_MAX:
                _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
            _PREVIEW_CACHE[release_id] = (time.monotonic(), preview)
        except httpx.HTTPStatusError as exc:
            error = (
                "Discogs rate limit reached (a running collection import uses the allowance)."
                if exc.response.status_code == 429
                else f"Discogs returned HTTP {exc.response.status_code}."
            )
        except httpx.HTTPError as exc:
            logger.warning("Discogs preview %s failed: %s", release_id, exc)
            error = "Could not reach Discogs."
    return _tmpl().TemplateResponse(
        request=request,
        name="partials/discogs_preview.html",
        context=_ctx(request, preview=preview, error=error, release_id=release_id),
    )
