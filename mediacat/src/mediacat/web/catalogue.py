"""Catalogue routes — Artist → Album → Pressing hierarchy.

URL structure
-------------
GET  /catalogue                                        artist browser
GET  /catalogue/new                                    create form
POST /catalogue/new                                    create Token + TokenRevision
GET  /catalogue/search                                 Discogs / MusicBrainz HTMX search
GET  /catalogue/merge/search                           live catalogue search for merge (HTMX)
GET  /catalogue/merge                                  merge selection UI
POST /catalogue/merge                                  execute merge
GET  /catalogue/artists/{artist}/albums                albums partial (HTMX)
GET  /catalogue/artists/{artist}/albums/{t}/pressings  pressings partial (HTMX)
GET  /catalogue/{token_id}                             pressing detail
GET  /catalogue/{token_id}/edit                        edit form
POST /catalogue/{token_id}/edit                        save new revision
POST /catalogue/{token_id}/archive                     set status=archived
POST /catalogue/{token_id}/flag-review                 create manual ReviewItem
POST /catalogue/{token_id}/images/{image_id}/analyse   run vision analysis, save OcrArtifact
POST /catalogue/{token_id}/images/{image_id}/delete    hard-delete a media object
POST /catalogue/{token_id}/images/{image_id}/set-cover mark as primary cover thumbnail
"""

from __future__ import annotations

import io
import logging
import re
import urllib.parse
import uuid
from datetime import UTC, datetime
from typing import Any

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from mediacat.db.enums import ImageRegion, MediaFormat, OcrEngine, RevisionSource, TokenStatus
from mediacat.db.models import (
    Country,
    MediaObject,
    OcrArtifact,
    Token,
    TokenRevision,
    TokenSymbol,
)
from mediacat.web.routes import _ctx, _require_role, _tmpl

logger = logging.getLogger(__name__)

catalogue_router = APIRouter(prefix="/catalogue", tags=["catalogue"])

_USER_AGENT = "MediaCat/1.0 (catalogue browser)"
_DISCOGS_BASE = "https://api.discogs.com"
_MB_BASE = "https://musicbrainz.org/ws/2"
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB
_DISCOGS_IMAGE_LIMIT = 8  # max images to fetch per Discogs release


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _sf(request: Request):  # type: ignore[return]
    return request.app.state.db_session_factory


def _user_uuid(request: Request) -> uuid.UUID | None:
    uid = getattr(request.state, "user_id", None)
    if not uid:
        return None
    try:
        return uuid.UUID(str(uid))
    except (ValueError, AttributeError):
        return None


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_year(value: str) -> tuple[int | None, str | None]:
    if not value.strip():
        return None, None
    try:
        y = int(value.strip())
        if not 1900 <= y <= 2100:
            return None, "Year must be between 1900 and 2100."
        return y, None
    except ValueError:
        return None, "Year must be a number."


def _build_full_runout_text(parsed: dict[str, Any]) -> str | None:
    """Reconstruct a space-separated flat string from all non-None parsed components.

    Produces "10AA6305231 1Y 320" instead of just the matrix_number field value.
    """
    _ordered = [
        "matrix_number",
        "stamper_code",
        "sid_mastering",
        "sid_mould",
        "lacquer_cutter",
        "pressing_plant",
        "other_etchings",
    ]
    parts = []
    for k in _ordered:
        entry = parsed.get(k)
        if isinstance(entry, dict):
            v = (entry.get("value") or "").strip()
            if v:
                parts.append(v)
    return " ".join(parts) or None


def _build_parsed_from_ocr(metadata: dict[str, Any], confidence: float) -> dict[str, Any]:
    """Convert OcrArtifact.metadata_ into the matrix_runout_parsed structure.

    Each field is {"value": <str|None>, "confidence": <float|None>, "source": <str|None>}.
    SID codes from the vision response are split into mastering (IFPI Lxxxx) and
    mould (IFPI Mxxxx / shorter numeric) entries heuristically.
    """
    sid_codes: list[dict[str, Any]] = metadata.get("sid_codes") or []
    sid_mastering: str | None = None
    sid_mould: str | None = None
    for sid in sid_codes:
        code = (sid.get("code") or "").strip()
        kind = (sid.get("type") or "").lower()
        if not code:
            continue
        if "master" in kind:
            sid_mastering = code
        elif "mould" in kind or "mold" in kind:
            sid_mould = code
        elif sid_mastering is None:
            sid_mastering = code
        elif sid_mould is None:
            sid_mould = code

    def _field(value: str | None, conf: float | None = None) -> dict[str, Any]:
        v = value.strip() if value else None
        return {
            "value": v,
            "confidence": conf if conf is not None else (confidence if v else None),
            "source": "vision" if v else None,
        }

    return {
        "matrix_number": _field(metadata.get("matrix_number")),
        "stamper_code": _field(metadata.get("stamper_code")),
        "sid_mastering": _field(sid_mastering),
        "sid_mould": _field(sid_mould),
        "lacquer_cutter": _field(metadata.get("lacquer_cut_info")),
        "pressing_plant": _field(metadata.get("pressing_plant_hint")),
        "other_etchings": _field(metadata.get("other_etchings")),
    }


# Human-readable labels for each parsed field key shown in the UI.
_PARSED_FIELD_LABELS: dict[str, str] = {
    "matrix_number": "Matrix / catalog ref",
    "stamper_code": "Stamper code",
    "sid_mastering": "SID mastering (IFPI)",
    "sid_mould": "SID mould (IFPI)",
    "lacquer_cutter": "Lacquer cutter",
    "pressing_plant": "Pressing plant",
    "other_etchings": "Other etchings",
}


def _revision_data(
    token: Token,
    label_name: str,
    manufacturer_name: str,
) -> dict[str, Any]:
    return {
        "artist": token.artist,
        "title": token.title,
        "year": token.year,
        "media_format": token.media_format.value if token.media_format else None,
        "catalog_number": token.catalog_number,
        "barcode": token.barcode,
        "matrix_runout": token.matrix_runout,
        "matrix_runout_b": token.matrix_runout_b,
        "country_id": str(token.country_id) if token.country_id else None,
        "label_name": label_name.strip() or None,
        "manufacturer_name": manufacturer_name.strip() or None,
        "discogs_release_id": token.discogs_release_id,
        "musicbrainz_release_id": token.musicbrainz_release_id,
    }


async def _import_discogs_images(
    store: Any,
    db: Any,
    token_id: uuid.UUID,
    images: list[dict[str, Any]],
) -> int:
    """Download Discogs images and store them in MinIO. Returns count stored."""
    if not store or not images:
        return 0

    from mediacat.storage.object_store import ALLOWED_MIME_TYPES

    existing: set[str] = set(
        (await db.execute(select(MediaObject.content_hash).where(MediaObject.token_id == token_id)))
        .scalars()
        .all()
    )

    # Track how many of each type we've seen to assign sensible default regions.
    # Discogs types are "primary" (cover) and "secondary" (everything else).
    # Secondary images are often label shots or runout photos — we assign
    # cover_back / label_a / label_b / other in order so the user's reassign
    # dropdown starts closer to the truth.
    _secondary_region_sequence = [
        ImageRegion.COVER_BACK,
        ImageRegion.LABEL_A,
        ImageRegion.LABEL_B,
    ]
    primary_done = False
    secondary_count = 0
    stored = 0

    async with httpx.AsyncClient(timeout=20.0) as client:
        for img in images[:_DISCOGS_IMAGE_LIMIT]:
            if not isinstance(img, dict):
                continue
            url = img.get("uri") or img.get("uri150") or ""
            if not url or not url.startswith("http"):
                continue

            img_type = img.get("type", "secondary")
            if img_type == "primary" and not primary_done:
                region = ImageRegion.COVER_FRONT
                primary_done = True
            elif img_type == "secondary":
                if secondary_count < len(_secondary_region_sequence):
                    region = _secondary_region_sequence[secondary_count]
                else:
                    region = ImageRegion.OTHER
                secondary_count += 1
            else:
                region = ImageRegion.OTHER

            try:
                r = await client.get(url, headers={"User-Agent": _USER_AGENT})
                if r.status_code != 200 or len(r.content) < 1000:
                    continue
                mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                if mime not in ALLOWED_MIME_TYPES:
                    mime = "image/jpeg"
                obj = await store.put_image(r.content, mime)
                if obj.content_hash in existing:
                    continue
                existing.add(obj.content_hash)
                db.add(
                    MediaObject(
                        token_id=token_id,
                        content_hash=obj.content_hash,
                        bucket=obj.bucket,
                        object_key=obj.object_key,
                        mime_type=obj.mime_type,
                        size_bytes=obj.size_bytes,
                        width_px=obj.width_px,
                        height_px=obj.height_px,
                        region=region,
                    )
                )
                stored += 1
            except Exception as exc:
                logger.warning("Failed to fetch Discogs image %s: %s", url, exc)

    return stored


async def _load_token(request: Request, token_id: str) -> Token:
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")
    async with _sf(request)() as db:
        token = (
            await db.execute(
                select(Token)
                .options(
                    selectinload(Token.label),
                    selectinload(Token.country),
                    selectinload(Token.manufacturer),
                    selectinload(Token.revisions),
                    selectinload(Token.media_objects).selectinload(MediaObject.ocr_artifacts),
                    selectinload(Token.token_symbols).selectinload(TokenSymbol.symbol),
                )
                .where(Token.id == tid)
                .where(Token.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="Not found")
    return token


# ──────────────────────────────────────────────────────────────────────────────
# Artist browser
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("", response_class=HTMLResponse)
async def catalogue_index(
    request: Request,
    q: str = Query(""),
    fmt: str = Query(""),
    page: int = Query(1, ge=1),
    artist: str = Query(""),
) -> HTMLResponse:
    """Browse artists grouped from the Token table."""
    page_size = 24
    offset = (page - 1) * page_size

    try:
        async with _sf(request)() as db:
            base = (
                select(
                    Token.artist,
                    func.count(Token.id).label("cnt"),
                    func.min(Token.year).label("yr_min"),
                    func.max(Token.year).label("yr_max"),
                )
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
                .where(Token.artist.isnot(None))
            )
            if q:
                base = base.where(or_(Token.artist.ilike(f"%{q}%"), Token.title.ilike(f"%{q}%")))
            if fmt in ("vinyl", "cd"):
                base = base.where(Token.media_format == fmt)

            grouped = base.group_by(Token.artist).subquery()
            total: int = (await db.execute(select(func.count()).select_from(grouped))).scalar_one()

            rows = (
                await db.execute(
                    base.group_by(Token.artist)
                    .order_by(Token.artist.asc())
                    .offset(offset)
                    .limit(page_size)
                )
            ).all()
    except SQLAlchemyError as exc:
        logger.error("Catalogue index DB error (migrations not run?): %s", exc)
        ctx = _ctx(
            request,
            artists=[],
            query=q,
            fmt_filter=fmt,
            page=1,
            total_pages=1,
            db_error=True,
        )
        return _tmpl().TemplateResponse(request=request, name="catalogue.html", context=ctx)

    artists = [
        {
            "name": r.artist,
            "pressing_count": r.cnt,
            "year_range": (
                str(r.yr_min) + (f"-{r.yr_max}" if r.yr_max != r.yr_min else "") if r.yr_min else ""
            ),
            "enc": urllib.parse.quote(r.artist, safe=""),
        }
        for r in rows
    ]

    ctx = _ctx(
        request,
        artists=artists,
        query=q,
        fmt_filter=fmt,
        page=page,
        total_pages=max(1, (total + page_size - 1) // page_size),
        db_error=False,
        open_artist=artist,
        open_artist_enc=urllib.parse.quote(artist, safe="") if artist else "",
    )
    return _tmpl().TemplateResponse(request=request, name="catalogue.html", context=ctx)


# ──────────────────────────────────────────────────────────────────────────────
# Create
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("/new", response_class=HTMLResponse)
async def token_new_form(
    request: Request,
    q: str = Query(""),
    source: str = Query("discogs"),
    limit: int = Query(10, ge=5, le=50),
    year: str = Query(""),
    country: str = Query(""),
    discogs_id: str = Query(""),
    mb_id: str = Query(""),
    scan_error: str = Query(""),
) -> HTMLResponse:
    _require_role(request, "admin", "reviewer")
    prefill: dict[str, Any] = {}
    duplicate: dict[str, Any] | None = None
    search_results: list[dict[str, Any]] = []
    search_error: str | None = scan_error.strip() or None

    # ── Smart detection: resolve q into the right search type ────────────────
    q_raw = q.strip()
    if q_raw and not discogs_id and not mb_id:
        if _UUID_RE.match(q_raw):
            mb_id = q_raw  # UUID → MusicBrainz
            q_raw = ""
        elif q_raw.isdigit() and len(q_raw) in (8, 12, 13):
            pass  # barcode → text search (handled below as barcode)
        elif q_raw.isdigit():
            discogs_id = q_raw  # short integer → Discogs release ID
            q_raw = ""

    # ── External API calls (before opening DB session) ────────────────────────
    if discogs_id.strip() and not search_error:
        did = _parse_int(discogs_id.strip())
        if did:
            try:
                prefill = _map_discogs_fields(await _fetch_discogs_release(did))
            except httpx.HTTPStatusError as exc:
                search_error = (
                    f"Discogs release {discogs_id} not found."
                    if exc.response.status_code == 404
                    else "Discogs API error — try again."
                )
            except Exception:
                search_error = f"Could not reach Discogs for release {discogs_id}."
        else:
            search_error = "Invalid Discogs release ID."

    elif mb_id.strip() and not search_error:
        if _parse_uuid(mb_id.strip()):
            try:
                prefill = _map_mb_fields(await _fetch_mb_release(mb_id.strip()))
            except httpx.HTTPStatusError as exc:
                search_error = (
                    "MusicBrainz release not found."
                    if exc.response.status_code == 404
                    else "MusicBrainz API error — try again."
                )
            except Exception:
                search_error = "Could not reach MusicBrainz."
        else:
            search_error = "Invalid MusicBrainz UUID format."

    elif q_raw and not search_error:
        try:
            yr = year.strip()
            ctr = country.strip()
            # Barcode digits: search Discogs by barcode field for precision
            if q_raw.isdigit() and len(q_raw) in (8, 12, 13):
                search_results = await _discogs_barcode_search(q_raw)
                if not search_results:
                    search_results = await _discogs_search(q_raw, limit, year=yr, country=ctr)
            elif source == "musicbrainz":
                search_results = await _mb_search(q_raw, limit)
            else:
                search_results = await _discogs_search(q_raw, limit, year=yr, country=ctr)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                search_error = (
                    "Discogs rate limit reached — please wait a moment before searching again."
                )
            else:
                logger.warning("New-form search HTTP %d: q=%r", exc.response.status_code, q_raw)
                search_error = "External search unavailable — API returned an error."
        except Exception as exc:
            logger.warning(
                "New-form external search failed: q=%r source=%r: %s", q_raw, source, exc
            )
            search_error = "External search unavailable — check your connection."

    # ── DB: countries + duplicate checks ─────────────────────────────────────
    async with _sf(request)() as db:
        countries = (await db.execute(select(Country).order_by(Country.name))).scalars().all()

        # Single-result prefill: check for duplicate by external ID
        if prefill and not search_error:
            did_int = _parse_int(str(prefill.get("discogs_release_id") or ""))
            mbid_str = prefill.get("musicbrainz_release_id") or ""
            dup_token: Token | None = None
            if did_int:
                dup_token = (
                    await db.execute(
                        select(Token)
                        .where(Token.discogs_release_id == did_int)
                        .where(Token.deleted_at.is_(None))
                    )
                ).scalar_one_or_none()
            elif mbid_str:
                dup_token = (
                    await db.execute(
                        select(Token)
                        .where(Token.musicbrainz_release_id == mbid_str)
                        .where(Token.deleted_at.is_(None))
                    )
                ).scalar_one_or_none()
            if dup_token:
                duplicate = {
                    "id": str(dup_token.id),
                    "artist": dup_token.artist,
                    "title": dup_token.title,
                    "year": dup_token.year,
                }

        # Batch duplicate check for search results list
        if search_results:
            d_ids = [
                r["id"] for r in search_results if r.get("source") == "discogs" and r.get("id")
            ]
            mb_ids_list = [
                r["id"] for r in search_results if r.get("source") == "musicbrainz" and r.get("id")
            ]

            existing_d: set = set()
            existing_mb: set = set()
            if d_ids:
                existing_d = set(
                    (
                        await db.execute(
                            select(Token.discogs_release_id)
                            .where(Token.discogs_release_id.in_(d_ids))
                            .where(Token.deleted_at.is_(None))
                        )
                    )
                    .scalars()
                    .all()
                )
            if mb_ids_list:
                existing_mb = set(
                    (
                        await db.execute(
                            select(Token.musicbrainz_release_id)
                            .where(Token.musicbrainz_release_id.in_(mb_ids_list))
                            .where(Token.deleted_at.is_(None))
                        )
                    )
                    .scalars()
                    .all()
                )
            for r in search_results:
                r["in_catalogue"] = (
                    r.get("id") in existing_d
                    if r.get("source") == "discogs"
                    else r.get("id") in existing_mb
                )

    ctx = _ctx(
        request,
        token=None,
        countries=countries,
        formats=["vinyl", "cd"],
        error=None,
        mode="create",
        prefill=prefill,
        duplicate=duplicate,
        search_results=search_results,
        search_error=search_error,
        search_query=q_raw,
        search_source=source,
        search_limit=limit,
        search_year=year,
        search_country=country,
        search_discogs_id=discogs_id,
        search_mb_id=mb_id,
        label_name=prefill.get("label_name", ""),
        manufacturer_name="",
    )
    return _tmpl().TemplateResponse(request=request, name="token_edit.html", context=ctx)


@catalogue_router.post("/new", response_model=None)
async def token_new_submit(
    request: Request,
    artist: str = Form(""),
    title: str = Form(""),
    year: str = Form(""),
    media_format: str = Form("vinyl"),
    catalog_number: str = Form(""),
    barcode: str = Form(""),
    matrix_runout: str = Form(""),
    matrix_runout_b: str = Form(""),
    country_id: str = Form(""),
    label_name: str = Form(""),
    manufacturer_name: str = Form(""),
    discogs_release_id: str = Form(""),
    musicbrainz_release_id: str = Form(""),
    comment: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    _require_role(request, "admin", "reviewer")
    year_int, err = _parse_year(year)
    if err:
        async with _sf(request)() as db:
            countries = (await db.execute(select(Country).order_by(Country.name))).scalars().all()
        ctx = _ctx(
            request,
            token=None,
            countries=countries,
            formats=["vinyl", "cd"],
            error=err,
            mode="create",
            prefill={},
            duplicate=None,
            search_results=[],
            search_error=None,
            search_query="",
            search_source="discogs",
            search_discogs_id="",
            search_mb_id="",
            label_name=label_name,
            manufacturer_name=manufacturer_name,
        )
        return _tmpl().TemplateResponse(
            request=request, name="token_edit.html", context=ctx, status_code=400
        )

    fmt = MediaFormat(media_format) if media_format in ("vinyl", "cd") else MediaFormat.VINYL
    cid = _parse_uuid(country_id)
    did = _parse_int(discogs_release_id)

    async with _sf(request)() as db:
        token = Token(
            artist=artist.strip() or None,
            title=title.strip() or None,
            year=year_int,
            media_format=fmt,
            status=TokenStatus.ACTIVE,
            catalog_number=catalog_number.strip() or None,
            barcode=barcode.strip() or None,
            matrix_runout=matrix_runout.strip() or None,
            matrix_runout_b=matrix_runout_b.strip() or None,
            country_id=cid,
            discogs_release_id=did,
            musicbrainz_release_id=musicbrainz_release_id.strip() or None,
        )
        db.add(token)
        await db.flush()

        revision = TokenRevision(
            token_id=token.id,
            revision_number=1,
            source=RevisionSource.HUMAN,
            data=_revision_data(token, label_name, manufacturer_name),
            comment=comment.strip()[:500] or None,
            created_by=_user_uuid(request),
        )
        db.add(revision)
        token.current_revision_id = revision.id
        await db.commit()
        new_id = str(token.id)

    logger.info("Token created: %s by user=%s", new_id, request.state.user_id)
    return RedirectResponse(url=f"/catalogue/{new_id}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# External search (Discogs / MusicBrainz)
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("/search", response_class=HTMLResponse)
async def catalogue_search(
    request: Request,
    q: str = Query(""),
    source: str = Query("discogs"),
    limit: int = Query(10, ge=5, le=50),
    year: str = Query(""),
    country: str = Query(""),
) -> HTMLResponse:
    results: list[dict[str, Any]] = []
    error: str | None = None

    if q.strip():
        try:
            results = (
                await _mb_search(q, limit)
                if source == "musicbrainz"
                else await _discogs_search(q, limit, year=year.strip(), country=country.strip())
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                error = "Discogs rate limit reached — please wait a moment before searching again."
            else:
                logger.warning("External search HTTP %d for q=%r", exc.response.status_code, q)
                error = "Search unavailable — external API returned an error."
        except Exception as exc:
            logger.warning("External search failed for q=%r source=%r: %s", q, source, exc)
            error = "Search unavailable — could not reach the external API."

    ctx = _ctx(
        request,
        results=results,
        query=q,
        source=source,
        limit=limit,
        year=year,
        country=country,
        error=error,
    )
    return _tmpl().TemplateResponse(
        request=request, name="partials/search_results.html", context=ctx
    )


async def _discogs_search(
    q: str, limit: int = 10, *, year: str = "", country: str = ""
) -> list[dict[str, Any]]:
    params: dict[str, str | int] = {"q": q, "type": "release", "per_page": limit}
    if year:
        params["year"] = year
    if country:
        params["country"] = country
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_DISCOGS_BASE}/database/search",
            params=params,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
    results = []
    for h in r.json().get("results") or []:
        if not isinstance(h, dict):
            continue
        raw = h.get("title", "")
        # Discogs search returns "Artist – Title" in the title field
        if " – " in raw:
            artist_part, title_part = raw.split(" – ", 1)
        elif " - " in raw:
            artist_part, title_part = raw.split(" - ", 1)
        else:
            artist_part, title_part = "", raw
        catno_raw = h.get("catno")
        results.append(
            {
                "source": "discogs",
                "id": h.get("id"),
                "artist": artist_part.strip(),
                "title": title_part.strip(),
                "year": h.get("year", ""),
                "label": ", ".join(x for x in (h.get("label") or []) if isinstance(x, str)),
                "catno": ", ".join(catno_raw) if isinstance(catno_raw, list) else (catno_raw or ""),
                "format": ", ".join(x for x in (h.get("format") or []) if isinstance(x, str)),
                "country": h.get("country", ""),
                "url": f"https://www.discogs.com/release/{h.get('id', '')}",
            }
        )
    return results


async def _mb_search(q: str, limit: int = 10) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{_MB_BASE}/release/",
            params={"query": q, "limit": limit, "fmt": "json"},
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
    results = []
    for h in r.json().get("releases") or []:
        if not isinstance(h, dict):
            continue
        credits = h.get("artist-credit") or []
        artist_parts: list[str] = []
        for ac in credits:
            if not isinstance(ac, dict):
                continue
            name = ac.get("name") or (ac.get("artist") or {}).get("name", "")
            if name:
                artist_parts.append(name.strip())
            joinphrase = ac.get("joinphrase", "")
            if joinphrase.strip() and artist_parts:
                artist_parts[-1] += joinphrase
        label_info = h.get("label-info") or []
        results.append(
            {
                "source": "musicbrainz",
                "id": h.get("id"),
                "artist": "".join(artist_parts).strip(),
                "title": h.get("title", ""),
                "year": (h.get("date") or "")[:4],
                "label": ", ".join(
                    (lc.get("label") or {}).get("name", "")
                    for lc in label_info
                    if isinstance(lc, dict)
                ),
                "catno": ", ".join(
                    lc.get("catalog-number", "")
                    for lc in label_info
                    if isinstance(lc, dict) and lc.get("catalog-number")
                ),
                "format": "",
                "country": h.get("country", ""),
                "url": f"https://musicbrainz.org/release/{h.get('id', '')}",
            }
        )
    return results


async def _fetch_mb_release(release_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_MB_BASE}/release/{release_id}",
            params={"fmt": "json", "inc": "artist-credits+labels+recordings+media"},
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _map_mb_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Map a MusicBrainz release JSON response to Token field values."""
    credits = data.get("artist-credit") or []
    artist_parts = []
    for ac in credits:
        if not isinstance(ac, dict):
            continue
        name = ac.get("name") or (ac.get("artist") or {}).get("name", "")
        if name:
            artist_parts.append(name.strip())
        joinphrase = ac.get("joinphrase", "")
        if joinphrase.strip() and artist_parts:
            artist_parts[-1] += joinphrase
    artist = "".join(artist_parts).strip() or None

    label_info = data.get("label-info") or []
    label_name = ""
    catno = ""
    first_li = label_info[0] if (label_info and isinstance(label_info[0], dict)) else {}
    if first_li:
        label_name = (first_li.get("label") or {}).get("name", "").strip()
        catno = (first_li.get("catalog-number") or "").strip()

    media = data.get("media") or []
    fmt = "vinyl"
    for m in media:
        if not isinstance(m, dict):
            continue
        mfmt = (m.get("format") or "").lower()
        if "cd" in mfmt or "digital" in mfmt:
            fmt = "cd"
            break
        if "vinyl" in mfmt or "lp" in mfmt or "ep" in mfmt:
            fmt = "vinyl"
            break

    raw_date = data.get("date") or ""
    year: int | None = None
    try:
        year = int(raw_date[:4]) if raw_date else None
    except ValueError:
        pass

    tracklist = []
    for m in media:
        if not isinstance(m, dict):
            continue
        for t in m.get("tracks") or []:
            if not isinstance(t, dict):
                continue
            title = (t.get("title") or (t.get("recording") or {}).get("title", "")).strip()
            if title:
                tracklist.append({"position": t.get("position", ""), "title": title})

    return {
        "artist": artist,
        "title": data.get("title"),
        "year": year,
        "media_format": fmt,
        "catalog_number": catno or None,
        "barcode": data.get("barcode") or None,
        "matrix_runout": None,
        "matrix_runout_b": None,
        "label_name": label_name,
        "musicbrainz_release_id": data.get("id"),
        "country_name": data.get("country", ""),
        "status": data.get("status", ""),
        "tracklist": tracklist,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Merge
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("/merge/search", response_class=HTMLResponse)
async def merge_search(
    request: Request,
    q: str = Query(""),
    side: str = Query("loser"),
    other_id: str = Query(""),
) -> HTMLResponse:
    """Live catalogue search for the merge page (HTMX)."""
    _require_role(request, "admin", "reviewer")
    tokens: list[Token] = []
    q = q.strip()
    if len(q) >= 2:
        other_uuid = _parse_uuid(other_id) if other_id else None
        async with _sf(request)() as db:
            stmt = (
                select(Token)
                .options(
                    selectinload(Token.label),
                    selectinload(Token.country),
                    selectinload(Token.media_objects),
                )
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
                .where(
                    or_(
                        Token.artist.ilike(f"%{q}%"),
                        Token.title.ilike(f"%{q}%"),
                        Token.catalog_number.ilike(f"%{q}%"),
                    )
                )
                .order_by(Token.artist.asc(), Token.title.asc(), Token.year.asc())
                .limit(12)
            )
            if other_uuid:
                stmt = stmt.where(Token.id != other_uuid)
            tokens = list((await db.execute(stmt)).scalars().all())

    ctx = _ctx(request, tokens=tokens, side=side, other_id=other_id, q=q)
    return _tmpl().TemplateResponse(request=request, name="partials/merge_search.html", context=ctx)


@catalogue_router.get("/merge", response_class=HTMLResponse)
async def merge_form(
    request: Request,
    winner_id: str = Query(""),
    loser_id: str = Query(""),
) -> HTMLResponse:
    _require_role(request, "admin", "reviewer")
    winner = await _load_token(request, winner_id) if winner_id else None
    loser = await _load_token(request, loser_id) if loser_id else None
    ctx = _ctx(request, winner=winner, loser=loser, error=None)
    return _tmpl().TemplateResponse(request=request, name="merge.html", context=ctx)


@catalogue_router.post("/merge", response_model=None)
async def merge_submit(
    request: Request,
    winner_id: str = Form(...),
    loser_id: str = Form(...),
    comment: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    _require_role(request, "admin", "reviewer")
    if winner_id == loser_id:
        winner = await _load_token(request, winner_id)
        ctx = _ctx(
            request,
            winner=winner,
            loser=winner,
            error="Cannot merge a pressing with itself.",
        )
        return _tmpl().TemplateResponse(
            request=request, name="merge.html", context=ctx, status_code=400
        )

    wid = _parse_uuid(winner_id)
    lid = _parse_uuid(loser_id)
    if wid is None or lid is None:
        raise HTTPException(status_code=400, detail="Invalid token IDs")

    async with _sf(request)() as db:
        winner = (
            await db.execute(select(Token).where(Token.id == wid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        loser = (
            await db.execute(select(Token).where(Token.id == lid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if not winner or not loser:
            raise HTTPException(status_code=404, detail="Token not found")

        loser.status = TokenStatus.MERGED

        rev_count: int = (
            await db.execute(select(func.count()).where(TokenRevision.token_id == winner.id))
        ).scalar_one()
        merge_comment = f"Merged from {loser_id}. {comment.strip()[:300]}".strip()
        revision = TokenRevision(
            token_id=winner.id,
            revision_number=rev_count + 1,
            source=RevisionSource.HUMAN,
            data=_revision_data(winner, "", ""),
            comment=merge_comment,
            created_by=_user_uuid(request),
        )
        db.add(revision)
        winner.current_revision_id = revision.id
        await db.commit()

    logger.info(
        "Merged loser=%s into winner=%s by user=%s", loser_id, winner_id, request.state.user_id
    )
    return RedirectResponse(url=f"/catalogue/{winner_id}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Barcode scan from cover image
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/scan-cover", response_model=None)
async def scan_cover(
    request: Request,
    file: UploadFile = File(...),
) -> RedirectResponse:
    """Read a barcode from a cover image and redirect to the Add pressing form.

    Uses pyzbar (libzbar) to decode EAN-8, EAN-13, UPC-A, UPC-E, Code-128.
    If one Discogs match is found the form is pre-filled directly.
    If multiple matches or only a raw barcode, the search results list is shown.
    """
    _require_role(request, "admin", "reviewer")
    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return RedirectResponse(
            url="/catalogue/new?scan_error=" + urllib.parse.quote("Image exceeds 30 MB limit."),
            status_code=303,
        )

    # Open with Pillow
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return RedirectResponse(
            url="/catalogue/new?scan_error=" + urllib.parse.quote("Could not open image file."),
            status_code=303,
        )

    # Decode barcodes
    try:
        from pyzbar.pyzbar import decode as zbar_decode

        barcodes = zbar_decode(img)
    except (ImportError, OSError) as exc:
        logger.warning("pyzbar unavailable: %s", exc)
        return RedirectResponse(
            url="/catalogue/new?scan_error="
            + urllib.parse.quote(
                "Barcode scanning unavailable — rebuild the app image (make build-app)."
            ),
            status_code=303,
        )

    if not barcodes:
        return RedirectResponse(
            url="/catalogue/new?scan_error=" + urllib.parse.quote("No barcode detected in image."),
            status_code=303,
        )

    barcode_value = barcodes[0].data.decode("utf-8").strip()
    logger.info("Barcode scan: found %r type=%s", barcode_value, barcodes[0].type)

    # Try an exact Discogs barcode lookup first
    try:
        results = await _discogs_barcode_search(barcode_value)
    except Exception:
        results = []

    if len(results) == 1:
        # Unique match — pre-fill directly
        return RedirectResponse(
            url=f"/catalogue/new?discogs_id={results[0]['id']}",
            status_code=303,
        )

    # Multiple matches or no match — show results list (barcode as search query)
    return RedirectResponse(
        url="/catalogue/new?q=" + urllib.parse.quote(barcode_value),
        status_code=303,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Discogs import
# ──────────────────────────────────────────────────────────────────────────────


async def _discogs_barcode_search(barcode: str) -> list[dict[str, Any]]:
    """Search Discogs by exact barcode — more precise than a text query."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{_DISCOGS_BASE}/database/search",
            params={"barcode": barcode, "type": "release", "per_page": 10},
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
    results = []
    for h in r.json().get("results") or []:
        if not isinstance(h, dict):
            continue
        raw = h.get("title", "")
        if " – " in raw:
            artist_part, title_part = raw.split(" – ", 1)
        elif " - " in raw:
            artist_part, title_part = raw.split(" - ", 1)
        else:
            artist_part, title_part = "", raw
        catno_raw = h.get("catno")
        results.append(
            {
                "source": "discogs",
                "id": h.get("id"),
                "artist": artist_part.strip(),
                "title": title_part.strip(),
                "year": h.get("year", ""),
                "label": ", ".join(x for x in (h.get("label") or []) if isinstance(x, str)),
                "catno": ", ".join(catno_raw) if isinstance(catno_raw, list) else (catno_raw or ""),
                "format": ", ".join(x for x in (h.get("format") or []) if isinstance(x, str)),
                "country": h.get("country", ""),
                "url": f"https://www.discogs.com/release/{h.get('id', '')}",
            }
        )
    return results


async def _fetch_discogs_release(release_id: int) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_DISCOGS_BASE}/releases/{release_id}",
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _map_discogs_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Map a Discogs release JSON response to Token field values."""
    artists = [
        a.get("name", "").strip().rstrip("*")
        for a in (data.get("artists") or [])
        if isinstance(a, dict) and a.get("name")
    ]
    artist = ", ".join(artists) if artists else None

    labels = data.get("labels") or []
    first_label = labels[0] if (labels and isinstance(labels[0], dict)) else {}
    label_name = first_label.get("name", "").strip()
    catno = (first_label.get("catno") or "").strip()
    catno = catno if catno.lower() != "none" else ""

    formats = data.get("formats") or []
    fmt = (
        "vinyl"
        if any("vinyl" in (f.get("name") or "").lower() for f in formats if isinstance(f, dict))
        else "cd"
    )

    identifiers = data.get("identifiers") or []
    matrix_a = matrix_b = barcode = ""
    for ident in identifiers:
        if not isinstance(ident, dict):
            continue
        itype = (ident.get("type") or "").lower()
        val = (ident.get("value") or "").strip()
        if "matrix" in itype or "runout" in itype:
            desc = (ident.get("description") or "").lower()
            if not matrix_a and ("side a" in desc or "side 1" in desc or "a-side" in desc):
                matrix_a = val
            elif not matrix_b and ("side b" in desc or "side 2" in desc or "b-side" in desc):
                matrix_b = val
            elif not matrix_a:
                matrix_a = val
        elif "barcode" in itype and not barcode:
            barcode = val

    return {
        "artist": artist,
        "title": data.get("title"),
        "year": data.get("year"),
        "media_format": fmt,
        "catalog_number": catno or None,
        "barcode": barcode or None,
        "matrix_runout": matrix_a or None,
        "matrix_runout_b": matrix_b or None,
        "label_name": label_name,
        "discogs_release_id": data.get("id"),
        "notes": (data.get("notes") or "")[:500],
        "country_name": data.get("country", ""),
        "tracklist": [
            {"position": t.get("position", ""), "title": t.get("title", "")}
            for t in (data.get("tracklist") or [])
            if isinstance(t, dict) and t.get("title")
        ],
    }


@catalogue_router.get("/import/discogs/{release_id}", response_class=HTMLResponse)
async def import_discogs_preview(request: Request, release_id: int) -> HTMLResponse:
    """Show a preview of what will be imported from a Discogs release."""
    _require_role(request, "admin", "reviewer")
    try:
        data = await _fetch_discogs_release(release_id)
        fields = _map_discogs_fields(data)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Discogs release {release_id} not found")
        raise HTTPException(status_code=502, detail="Discogs API error") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not reach Discogs API") from exc

    ctx = _ctx(request, release_id=release_id, source="discogs", fields=fields)
    return _tmpl().TemplateResponse(request=request, name="import_discogs.html", context=ctx)


@catalogue_router.post("/import/discogs/{release_id}", response_model=None)
async def import_discogs_create(request: Request, release_id: int) -> RedirectResponse:
    """Create a Token from a Discogs release and redirect to the detail page."""
    _require_role(request, "admin", "reviewer")
    try:
        data = await _fetch_discogs_release(release_id)
        fields = _map_discogs_fields(data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not reach Discogs API") from exc

    fmt = MediaFormat.VINYL if fields["media_format"] == "vinyl" else MediaFormat.CD

    async with _sf(request)() as db:
        token = Token(
            artist=fields["artist"],
            title=fields["title"],
            year=fields["year"],
            media_format=fmt,
            status=TokenStatus.ACTIVE,
            catalog_number=fields["catalog_number"],
            barcode=fields["barcode"],
            matrix_runout=fields["matrix_runout"],
            matrix_runout_b=fields["matrix_runout_b"],
            discogs_release_id=release_id,
        )
        db.add(token)
        await db.flush()

        revision = TokenRevision(
            token_id=token.id,
            revision_number=1,
            source=RevisionSource.IMPORT,
            data={k: v for k, v in fields.items() if k != "tracklist"},
            comment=f"Imported from Discogs release {release_id}",
            created_by=_user_uuid(request),
        )
        db.add(revision)
        token.current_revision_id = revision.id

        store = getattr(request.app.state, "object_store", None)
        img_count = 0
        try:
            img_count = await _import_discogs_images(store, db, token.id, data.get("images") or [])
        except Exception as exc:
            logger.warning("Discogs image import failed for release %d: %s", release_id, exc)

        await db.commit()
        new_id = str(token.id)

    logger.info(
        "Token created from Discogs release %d: %s images=%d by user=%s",
        release_id,
        new_id,
        img_count,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{new_id}", status_code=303)


@catalogue_router.get("/import/musicbrainz/{release_id}", response_class=HTMLResponse)
async def import_mb_preview(request: Request, release_id: str) -> HTMLResponse:
    """Show a preview of what will be imported from a MusicBrainz release UUID."""
    _require_role(request, "admin", "reviewer")
    if _parse_uuid(release_id) is None:
        raise HTTPException(status_code=400, detail="Invalid MusicBrainz release UUID")
    try:
        data = await _fetch_mb_release(release_id)
        fields = _map_mb_fields(data)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(
                status_code=404, detail=f"MusicBrainz release {release_id} not found"
            )
        raise HTTPException(status_code=502, detail="MusicBrainz API error") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not reach MusicBrainz API") from exc

    ctx = _ctx(request, release_id=release_id, source="musicbrainz", fields=fields)
    return _tmpl().TemplateResponse(request=request, name="import_discogs.html", context=ctx)


@catalogue_router.post("/import/musicbrainz/{release_id}", response_model=None)
async def import_mb_create(request: Request, release_id: str) -> RedirectResponse:
    """Create a Token from a MusicBrainz release and redirect to the detail page."""
    _require_role(request, "admin", "reviewer")
    if _parse_uuid(release_id) is None:
        raise HTTPException(status_code=400, detail="Invalid MusicBrainz release UUID")
    try:
        data = await _fetch_mb_release(release_id)
        fields = _map_mb_fields(data)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not reach MusicBrainz API") from exc

    fmt = MediaFormat.VINYL if fields["media_format"] == "vinyl" else MediaFormat.CD

    async with _sf(request)() as db:
        token = Token(
            artist=fields["artist"],
            title=fields["title"],
            year=fields["year"],
            media_format=fmt,
            status=TokenStatus.ACTIVE,
            catalog_number=fields["catalog_number"],
            barcode=fields["barcode"],
            matrix_runout=fields["matrix_runout"],
            matrix_runout_b=fields["matrix_runout_b"],
            musicbrainz_release_id=release_id,
        )
        db.add(token)
        await db.flush()

        revision = TokenRevision(
            token_id=token.id,
            revision_number=1,
            source=RevisionSource.IMPORT,
            data={k: v for k, v in fields.items() if k != "tracklist"},
            comment=f"Imported from MusicBrainz release {release_id}",
            created_by=_user_uuid(request),
        )
        db.add(revision)
        token.current_revision_id = revision.id
        await db.commit()
        new_id = str(token.id)

    logger.info(
        "Token created from MusicBrainz release %s: %s by user=%s",
        release_id,
        new_id,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{new_id}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# HTMX drill-down partials
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("/artists/{artist}/albums", response_class=HTMLResponse)
async def artist_albums(request: Request, artist: str) -> HTMLResponse:
    name = urllib.parse.unquote(artist)
    async with _sf(request)() as db:
        rows = (
            await db.execute(
                select(
                    Token.title,
                    func.count(Token.id).label("cnt"),
                    func.min(Token.year).label("year"),
                )
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
                .where(Token.artist == name)
                .group_by(Token.title)
                .order_by(Token.title.asc())
            )
        ).all()

    ctx = _ctx(
        request,
        artist=name,
        enc_artist=urllib.parse.quote(name, safe=""),
        albums=[
            {
                "title": r.title,
                "pressing_count": r.cnt,
                "year": r.year,
                "enc_title": urllib.parse.quote(r.title or "", safe=""),
            }
            for r in rows
        ],
    )
    return _tmpl().TemplateResponse(request=request, name="partials/album_list.html", context=ctx)


@catalogue_router.get("/artists/{artist}/albums/{title}/pressings", response_class=HTMLResponse)
async def album_pressings(request: Request, artist: str, title: str) -> HTMLResponse:
    name = urllib.parse.unquote(artist)
    album = urllib.parse.unquote(title)
    async with _sf(request)() as db:
        tokens = (
            (
                await db.execute(
                    select(Token)
                    .options(
                        selectinload(Token.label),
                        selectinload(Token.country),
                        selectinload(Token.media_objects),
                    )
                    .where(Token.deleted_at.is_(None))
                    .where(Token.status != TokenStatus.MERGED)
                    .where(Token.artist == name)
                    .where(Token.title == album)
                    .order_by(Token.year.asc().nulls_last())
                )
            )
            .scalars()
            .all()
        )

    ctx = _ctx(request, artist=name, album_title=album, tokens=tokens)
    return _tmpl().TemplateResponse(
        request=request, name="partials/pressing_list.html", context=ctx
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pressing detail
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("/{token_id}", response_class=HTMLResponse)
async def pressing_detail(request: Request, token_id: str) -> HTMLResponse:
    token = await _load_token(request, token_id)

    # slug → Symbol lookup for rendering matrix_runout_parts in the template
    symbol_by_slug: dict[str, Any] = {
        ts.symbol.slug: ts.symbol for ts in token.token_symbols if ts.symbol is not None
    }

    # Pre-filter images relevant to the manufacturing/stamping section
    _runout_regions = {"runout_a", "runout_b", "matrix", "label_a", "label_b"}
    runout_images = [
        img for img in token.media_objects if img.region and img.region.value in _runout_regions
    ]

    # Map image_id → latest OcrArtifact (most recent by created_at) for the template
    ocr_artifacts_by_image: dict[str, OcrArtifact] = {}
    for img in runout_images:
        if img.ocr_artifacts:
            latest = max(img.ocr_artifacts, key=lambda a: a.created_at or 0)
            ocr_artifacts_by_image[str(img.id)] = latest

    vision_available = getattr(request.app.state, "vision", None) is not None

    # Derive header display text from parsed breakdown when available.
    # Falls back to the plain stored text so old records still display.
    def _display(parsed: dict | None, fallback: str | None) -> str | None:
        return _build_full_runout_text(parsed or {}) or fallback

    ctx = _ctx(
        request,
        token=token,
        revisions=list(reversed(token.revisions)),
        symbol_by_slug=symbol_by_slug,
        runout_images=runout_images,
        ocr_artifacts_by_image=ocr_artifacts_by_image,
        vision_available=vision_available,
        parsed_field_labels=_PARSED_FIELD_LABELS,
        matrix_runout_display=_display(token.matrix_runout_parsed, token.matrix_runout),
        matrix_runout_b_display=_display(token.matrix_runout_b_parsed, token.matrix_runout_b),
    )
    return _tmpl().TemplateResponse(request=request, name="pressing_detail.html", context=ctx)


# ──────────────────────────────────────────────────────────────────────────────
# Edit
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("/{token_id}/edit", response_class=HTMLResponse)
async def token_edit_form(request: Request, token_id: str) -> HTMLResponse:
    _require_role(request, "admin", "reviewer")
    token = await _load_token(request, token_id)
    async with _sf(request)() as db:
        countries = (await db.execute(select(Country).order_by(Country.name))).scalars().all()
    ctx = _ctx(
        request,
        token=token,
        countries=countries,
        formats=["vinyl", "cd"],
        error=None,
        mode="edit",
        label_name=token.label.name if token.label else "",
        manufacturer_name=token.manufacturer.name if token.manufacturer else "",
    )
    return _tmpl().TemplateResponse(request=request, name="token_edit.html", context=ctx)


@catalogue_router.post("/{token_id}/edit", response_model=None)
async def token_edit_submit(
    request: Request,
    token_id: str,
    artist: str = Form(""),
    title: str = Form(""),
    year: str = Form(""),
    media_format: str = Form("vinyl"),
    catalog_number: str = Form(""),
    barcode: str = Form(""),
    matrix_runout: str = Form(""),
    matrix_runout_b: str = Form(""),
    country_id: str = Form(""),
    label_name: str = Form(""),
    manufacturer_name: str = Form(""),
    discogs_release_id: str = Form(""),
    musicbrainz_release_id: str = Form(""),
    comment: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    _require_role(request, "admin", "reviewer")
    year_int, err = _parse_year(year)
    if err:
        token = await _load_token(request, token_id)
        async with _sf(request)() as db:
            countries = (await db.execute(select(Country).order_by(Country.name))).scalars().all()
        ctx = _ctx(
            request,
            token=token,
            countries=countries,
            formats=["vinyl", "cd"],
            error=err,
            mode="edit",
            label_name=label_name,
            manufacturer_name=manufacturer_name,
        )
        return _tmpl().TemplateResponse(
            request=request, name="token_edit.html", context=ctx, status_code=400
        )

    fmt = MediaFormat(media_format) if media_format in ("vinyl", "cd") else MediaFormat.VINYL
    cid = _parse_uuid(country_id)
    did = _parse_int(discogs_release_id)
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")

        token.artist = artist.strip() or None
        token.title = title.strip() or None
        token.year = year_int
        token.media_format = fmt
        token.catalog_number = catalog_number.strip() or None
        token.barcode = barcode.strip() or None
        token.matrix_runout = matrix_runout.strip() or None
        token.matrix_runout_b = matrix_runout_b.strip() or None
        token.country_id = cid
        token.discogs_release_id = did
        token.musicbrainz_release_id = musicbrainz_release_id.strip() or None

        rev_count: int = (
            await db.execute(select(func.count()).where(TokenRevision.token_id == token.id))
        ).scalar_one()

        revision = TokenRevision(
            token_id=token.id,
            revision_number=rev_count + 1,
            source=RevisionSource.HUMAN,
            data=_revision_data(token, label_name, manufacturer_name),
            comment=comment.strip()[:500] or None,
            created_by=_user_uuid(request),
        )
        db.add(revision)
        token.current_revision_id = revision.id
        await db.commit()

    logger.info(
        "Token edited: %s rev=%d by user=%s", token_id, rev_count + 1, request.state.user_id
    )
    return RedirectResponse(url=f"/catalogue/{token_id}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Archive
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/{token_id}/archive", response_model=None)
async def token_archive(request: Request, token_id: str) -> RedirectResponse:
    _require_role(request, "admin", "reviewer")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")
        token.status = TokenStatus.ARCHIVED
        await db.commit()

    logger.info("Token archived: %s by user=%s", token_id, request.state.user_id)
    return RedirectResponse(url=f"/catalogue/{token_id}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Flag for review
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/{token_id}/flag-review", response_model=None)
async def flag_for_review(request: Request, token_id: str) -> RedirectResponse:
    """Create a manual ReviewItem for this pressing."""
    _require_role(request, "admin", "reviewer")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    from mediacat.db.enums import ReviewReason, ReviewStatus
    from mediacat.db.models import ReviewItem

    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")

        item = ReviewItem(
            token_id=tid,
            status=ReviewStatus.PENDING,
            reason=ReviewReason.MANUAL,
            priority=5,
            details={"manual": True, "requested_by": str(request.state.user_id)},
        )
        db.add(item)
        await db.commit()

    logger.info("Token %s flagged for review by user=%s", token_id, request.state.user_id)
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=review_flagged", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Delete (soft-delete)
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/{token_id}/delete", response_model=None)
async def token_delete(request: Request, token_id: str) -> RedirectResponse:
    _require_role(request, "admin")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")
        token.deleted_at = datetime.now(UTC)
        await db.commit()

    logger.info("Token deleted: %s by user=%s", token_id, request.state.user_id)
    return RedirectResponse(url="/catalogue", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Refresh images from Discogs
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/{token_id}/refresh-discogs", response_model=None)
async def token_refresh_discogs(request: Request, token_id: str) -> RedirectResponse:
    """Fetch and store images from Discogs for an existing pressing."""
    _require_role(request, "admin", "reviewer")
    token = await _load_token(request, token_id)

    if not token.discogs_release_id:
        raise HTTPException(status_code=400, detail="No Discogs release ID on this pressing")

    store = getattr(request.app.state, "object_store", None)
    if not store:
        raise HTTPException(
            status_code=503, detail="Object store not configured — MinIO unavailable"
        )

    try:
        data = await _fetch_discogs_release(token.discogs_release_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"Discogs API error {exc.response.status_code}"
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not reach Discogs API") from exc

    images = data.get("images") or []
    if not images:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_discogs_images", status_code=303)

    async with _sf(request)() as db:
        count = await _import_discogs_images(store, db, token.id, images)
        if count:
            await db.commit()

    logger.info(
        "Refreshed %d images from Discogs for token=%s by user=%s",
        count,
        token_id,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?refreshed={count}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Update image region
# ──────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/{token_id}/images/{image_id}/region", response_model=None)
async def update_image_region(
    request: Request,
    token_id: str,
    image_id: str,
    region: str = Form(...),
) -> RedirectResponse:
    """Reassign the region label of a stored image.

    When reassigned to a runout/label region, queues a background vision
    analysis automatically (same as a fresh upload to that region).
    """
    _require_role(request, "admin", "reviewer")
    tid = _parse_uuid(token_id)
    iid = _parse_uuid(image_id)
    if tid is None or iid is None:
        raise HTTPException(status_code=404, detail="Not found")

    new_region = ImageRegion(region) if region in _VALID_REGIONS else ImageRegion.OTHER

    async with _sf(request)() as db:
        mo = (
            await db.execute(
                select(MediaObject)
                .options(selectinload(MediaObject.token))
                .where(MediaObject.id == iid, MediaObject.token_id == tid)
            )
        ).scalar_one_or_none()
        if mo is None:
            raise HTTPException(status_code=404, detail="Image not found")
        mo.region = new_region
        await db.commit()

        obj_key = mo.object_key
        bucket = mo.bucket
        mime = mo.mime_type
        media_format = (
            mo.token.media_format.value if (mo.token and mo.token.media_format) else "vinyl"
        )

    # Auto-trigger vision if reassigned to a runout/label region and no analysis exists yet
    if new_region.value in _RUNOUT_REGIONS:
        vision = getattr(request.app.state, "vision", None)
        store = getattr(request.app.state, "object_store", None)
        if vision is not None and store is not None:
            from fastapi.background import BackgroundTasks

            session_factory = _sf(request)
            bg = BackgroundTasks()

            async def _bg_region_analyse(
                _mo_id=str(iid),
                _region=new_region.value,
                _obj_key=obj_key,
                _bucket=bucket,
                _mime=mime,
                _mformat=media_format,
                _store=store,
                _vision=vision,
                _sf_=session_factory,
            ):
                try:
                    from mediacat.vision.prompts import get_prompt_for_region

                    img_bytes = await _store.get_object(_obj_key, _bucket)
                    sys_p, user_p, _ = get_prompt_for_region(_region, _mformat)
                    resp = await _vision.transcribe(
                        img_bytes, _mime, user_p, system=sys_p, task=_region
                    )
                    parsed = resp.parsed or {}
                    raw_text = _build_full_runout_text(parsed) or resp.text[:500]
                    async with _sf_() as _db:
                        artifact = OcrArtifact(
                            media_object_id=uuid.UUID(_mo_id),
                            engine=OcrEngine.MANUAL,
                            region=ImageRegion(_region),
                            raw_text=raw_text,
                            confidence=resp.confidence,
                            metadata_=parsed,
                            symbol_candidates=parsed.get("symbol_detections"),
                        )
                        _db.add(artifact)
                        await _db.commit()
                    logger.info(
                        "Vision analysis done (region-reassign) media_object=%s region=%s conf=%.2f",
                        _mo_id,
                        _region,
                        resp.confidence,
                    )
                except Exception as exc:
                    logger.error(
                        "Background vision (region-reassign) failed for media_object=%s: %s",
                        _mo_id,
                        exc,
                    )

            bg.add_task(_bg_region_analyse)
            resp = RedirectResponse(url=f"/catalogue/{token_id}", status_code=303)
            resp.background = bg
            return resp

    return RedirectResponse(url=f"/catalogue/{token_id}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Image upload
# ──────────────────────────────────────────────────────────────────────────────

_RUNOUT_REGIONS = frozenset({"runout_a", "runout_b", "matrix", "label_a", "label_b"})
_VALID_REGIONS = frozenset(r.value for r in ImageRegion)


@catalogue_router.post("/{token_id}/images", response_model=None)
async def upload_image(
    request: Request,
    token_id: str,
    file: UploadFile = File(...),
    region: str = Form("cover_front"),
) -> RedirectResponse | HTMLResponse:
    """Upload an image (cover, label scan, runout photo) for a pressing.

    WAX / runout images (region=runout_a/b or matrix) are routed to the
    vision pipeline for automated transcription.  Other regions are stored
    as-is for reference.
    """
    _require_role(request, "admin", "reviewer")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    region = region if region in _VALID_REGIONS else "other"

    # Guard: object store must be available
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        logger.warning("Image upload attempted but object store not configured")
        ctx = _ctx(request, error="Image storage is not configured. Check MinIO settings.")
        return HTMLResponse(
            content=_tmpl().get_template("pressing_detail.html").render(ctx),
            status_code=503,
        )

    # Read and validate file size
    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 30 MB limit")

    mime = file.content_type or "application/octet-stream"

    from mediacat.storage.object_store import ALLOWED_MIME_TYPES, ObjectStoreError

    if mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image type {mime!r}. Allowed: jpeg, png, tiff, webp",
        )

    try:
        stored = await store.put_image(raw, mime)
    except ObjectStoreError as exc:
        logger.error("Image upload failed for token=%s: %s", token_id, exc)
        raise HTTPException(status_code=500, detail="Image storage failed") from exc

    # Record in DB
    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")

        img_region = ImageRegion(region) if region in _VALID_REGIONS else None
        mo = MediaObject(
            token_id=tid,
            content_hash=stored.content_hash,
            bucket=stored.bucket,
            object_key=stored.object_key,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            width_px=stored.width_px,
            height_px=stored.height_px,
            region=img_region,
        )
        db.add(mo)
        await db.commit()
        mo_id = str(mo.id)
        media_format = token.media_format.value if token.media_format else "vinyl"

    logger.info(
        "Image uploaded for token=%s region=%s object=%s/%s by user=%s",
        token_id,
        region,
        stored.bucket,
        stored.object_key,
        request.state.user_id,
    )

    # Vision pipeline for runout / label regions — run in background on upload
    if region in _RUNOUT_REGIONS:
        vision = getattr(request.app.state, "vision", None)
        if vision is not None:
            from fastapi.background import BackgroundTasks

            session_factory = _sf(request)
            bg = BackgroundTasks()

            async def _bg_analyse(
                _mo_id=mo_id,
                _region=region,
                _obj_key=stored.object_key,
                _bucket=stored.bucket,
                _mime=stored.mime_type,
                _mformat=media_format,
                _store=store,
                _vision=vision,
                _sf_=session_factory,
            ):
                try:
                    from mediacat.vision.prompts import get_prompt_for_region

                    img_bytes = await _store.get_object(_obj_key, _bucket)
                    sys_p, user_p, _ = get_prompt_for_region(_region, _mformat)
                    resp = await _vision.transcribe(
                        img_bytes, _mime, user_p, system=sys_p, task=_region
                    )
                    parsed = resp.parsed or {}
                    raw_text = _build_full_runout_text(parsed) or resp.text[:500]
                    async with _sf_() as _db:
                        artifact = OcrArtifact(
                            media_object_id=uuid.UUID(_mo_id),
                            engine=OcrEngine.MANUAL,
                            region=ImageRegion(_region),
                            raw_text=raw_text,
                            confidence=resp.confidence,
                            metadata_=parsed,
                            symbol_candidates=parsed.get("symbol_detections"),
                        )
                        _db.add(artifact)
                        await _db.commit()
                    logger.info(
                        "Vision analysis done for media_object=%s region=%s conf=%.2f",
                        _mo_id,
                        _region,
                        resp.confidence,
                    )
                except Exception as exc:
                    logger.error(
                        "Background vision analysis failed for media_object=%s: %s", _mo_id, exc
                    )

            bg.add_task(_bg_analyse)
            response = RedirectResponse(url=f"/catalogue/{token_id}?uploaded=1", status_code=303)
            response.background = bg
            return response
        else:
            logger.info(
                "Vision backend not configured — skipping analysis for media_object=%s", mo_id
            )

    return RedirectResponse(url=f"/catalogue/{token_id}?uploaded=1", status_code=303)


@catalogue_router.get("/{token_id}/images/{image_id}")
async def serve_image(request: Request, token_id: str, image_id: str) -> Response:
    tid = _parse_uuid(token_id)
    iid = _parse_uuid(image_id)
    if tid is None or iid is None:
        raise HTTPException(status_code=404, detail="Not found")
    store = getattr(request.app.state, "object_store", None)
    if not store:
        raise HTTPException(status_code=503, detail="Object store not configured")
    async with _sf(request)() as db:
        mo = (
            await db.execute(
                select(MediaObject).where(MediaObject.id == iid, MediaObject.token_id == tid)
            )
        ).scalar_one_or_none()
        if mo is None:
            raise HTTPException(status_code=404, detail="Not found")
    try:
        data = await store.get_object(mo.object_key, mo.bucket)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Image not available") from exc
    return Response(
        content=data,
        media_type=mo.mime_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@catalogue_router.post("/{token_id}/images/{image_id}/analyse", response_class=HTMLResponse)
async def analyse_image(request: Request, token_id: str, image_id: str) -> RedirectResponse:
    """Run vision analysis on a runout/label image and save the OcrArtifact."""
    _require_role(request, "admin", "reviewer")

    tid = _parse_uuid(token_id)
    iid = _parse_uuid(image_id)
    if tid is None or iid is None:
        raise HTTPException(status_code=404, detail="Not found")

    vision = getattr(request.app.state, "vision", None)
    if vision is None:
        raise HTTPException(status_code=503, detail="Vision backend not configured")

    store = getattr(request.app.state, "object_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Object store not configured")

    async with _sf(request)() as db:
        mo = (
            await db.execute(
                select(MediaObject).where(MediaObject.id == iid, MediaObject.token_id == tid)
            )
        ).scalar_one_or_none()
        if mo is None:
            raise HTTPException(status_code=404, detail="Image not found")
        token = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Token not found")

        region_val = mo.region.value if mo.region else "other"
        media_format = token.media_format.value if token.media_format else "vinyl"

        try:
            img_bytes = await store.get_object(mo.object_key, mo.bucket)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="Could not fetch image from storage"
            ) from exc

        from mediacat.vision.prompts import get_prompt_for_region

        system, user_prompt, _ = get_prompt_for_region(region_val, media_format)

        try:
            resp = await vision.transcribe(
                img_bytes, mo.mime_type or "image/jpeg", user_prompt, system=system, task=region_val
            )
        except Exception as exc:
            err_msg = str(exc) or repr(exc) or type(exc).__name__
            logger.error(
                "Vision analysis failed for media_object=%s: %s", image_id, exc, exc_info=True
            )
            raise HTTPException(status_code=502, detail=f"Vision model error: {err_msg}") from exc

        parsed = resp.parsed or {}
        raw_text = _build_full_runout_text(parsed) or resp.text[:500]
        artifact = OcrArtifact(
            media_object_id=iid,
            engine=OcrEngine.MANUAL,
            region=mo.region,
            raw_text=raw_text,
            confidence=resp.confidence,
            metadata_=parsed,
            symbol_candidates=parsed.get("symbol_detections"),
        )
        db.add(artifact)
        await db.commit()

        logger.info(
            "Vision analysis saved for token=%s image=%s region=%s conf=%.2f by user=%s",
            token_id,
            image_id,
            region_val,
            resp.confidence,
            request.state.user_id,
        )

    return RedirectResponse(url=f"/catalogue/{token_id}?msg=analysed", status_code=303)


@catalogue_router.post("/{token_id}/images/{image_id}/apply-ocr", response_class=HTMLResponse)
async def apply_ocr_to_pressing(request: Request, token_id: str, image_id: str) -> RedirectResponse:
    """Write the latest OcrArtifact for a runout image into the Token's matrix fields."""
    _require_role(request, "admin", "reviewer")

    tid = _parse_uuid(token_id)
    iid = _parse_uuid(image_id)
    if tid is None or iid is None:
        raise HTTPException(status_code=404, detail="Not found")

    async with _sf(request)() as db:
        mo = (
            await db.execute(
                select(MediaObject)
                .where(MediaObject.id == iid, MediaObject.token_id == tid)
                .options(selectinload(MediaObject.ocr_artifacts))
            )
        ).scalar_one_or_none()
        if mo is None:
            raise HTTPException(status_code=404, detail="Image not found")
        if not mo.ocr_artifacts:
            raise HTTPException(
                status_code=422,
                detail="No analysis results for this image yet — run AI analysis first",
            )

        artifact = max(mo.ocr_artifacts, key=lambda a: a.created_at or 0)
        if not artifact.raw_text:
            raise HTTPException(status_code=422, detail="Analysis result has no transcribed text")

        token = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Token not found")

        parsed = _build_parsed_from_ocr(artifact.metadata_ or {}, artifact.confidence or 0.0)
        raw_text = _build_full_runout_text(parsed) or artifact.raw_text.strip()
        parts: list[dict[str, Any]] = [{"t": "text", "v": raw_text}]

        side_b = mo.region is not None and mo.region.value == "runout_b"
        if side_b:
            token.matrix_runout_b = raw_text
            token.matrix_runout_b_parts = parts
            token.matrix_runout_b_parsed = parsed
        else:
            token.matrix_runout = raw_text
            token.matrix_runout_parts = parts
            token.matrix_runout_parsed = parsed

        rev_count: int = (
            await db.execute(select(func.count()).where(TokenRevision.token_id == tid))
        ).scalar_one()
        label_name = token.label.name if token.label else ""
        mfr_name = token.manufacturer.name if token.manufacturer else ""
        revision = TokenRevision(
            token_id=tid,
            revision_number=rev_count + 1,
            source=RevisionSource.VISION,
            data=_revision_data(token, label_name, mfr_name),
            comment=f"Applied vision analysis from image {image_id} (conf {artifact.confidence:.0%})",
            created_by=_user_uuid(request),
        )
        db.add(revision)
        await db.commit()

    field = "matrix_runout_b" if side_b else "matrix_runout"
    logger.info(
        "OCR applied to pressing=%s field=%s conf=%.2f by user=%s",
        token_id,
        field,
        artifact.confidence,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=ocr_applied", status_code=303)


@catalogue_router.post("/{token_id}/images/{image_id}/delete", response_model=None)
async def delete_image(request: Request, token_id: str, image_id: str) -> RedirectResponse:
    """Hard-delete a media object row (DB only; MinIO object remains for cleanup later)."""
    _require_role(request, "admin", "reviewer")
    tid = _parse_uuid(token_id)
    iid = _parse_uuid(image_id)
    if tid is None or iid is None:
        raise HTTPException(status_code=404, detail="Not found")

    async with _sf(request)() as db:
        mo = (
            await db.execute(
                select(MediaObject).where(MediaObject.id == iid, MediaObject.token_id == tid)
            )
        ).scalar_one_or_none()
        if mo is None:
            raise HTTPException(status_code=404, detail="Image not found")
        await db.delete(mo)
        await db.commit()

    logger.info(
        "Image deleted: token=%s image=%s by user=%s", token_id, image_id, request.state.user_id
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=image_deleted", status_code=303)


@catalogue_router.post("/{token_id}/images/{image_id}/set-cover", response_model=None)
async def set_primary_cover(request: Request, token_id: str, image_id: str) -> RedirectResponse:
    """Set one image as the primary cover; clear the flag on all others."""
    _require_role(request, "admin", "reviewer")
    tid = _parse_uuid(token_id)
    iid = _parse_uuid(image_id)
    if tid is None or iid is None:
        raise HTTPException(status_code=404, detail="Not found")

    async with _sf(request)() as db:
        images = (
            (await db.execute(select(MediaObject).where(MediaObject.token_id == tid)))
            .scalars()
            .all()
        )
        for img in images:
            img.is_primary_cover = img.id == iid
        await db.commit()

    logger.info(
        "Primary cover set: token=%s image=%s by user=%s", token_id, image_id, request.state.user_id
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=cover_set", status_code=303)


# Valid parsed field keys — guards against arbitrary key injection.
_PARSED_FIELD_KEYS: frozenset[str] = frozenset(_PARSED_FIELD_LABELS)

_CORRECTION_REASON_CODES: frozenset[str] = frozenset(
    {
        "vision_misread",
        "physical_inspection",
        "cross_reference",
        "authoritative_source",
        "other",
    }
)


@catalogue_router.post("/{token_id}/correct-matrix-field", response_class=HTMLResponse)
async def correct_matrix_field(request: Request, token_id: str) -> RedirectResponse:
    """Apply a human correction to one parsed matrix field with a mandatory reason."""
    _require_role(request, "admin", "reviewer")

    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    side = str(form.get("side", "a")).lower()  # "a" or "b"
    field_key = str(form.get("field", "")).strip()
    new_value = str(form.get("value", "")).strip()
    reason_code = str(form.get("reason_code", "other")).strip()
    reason_text = str(form.get("reason_text", "")).strip()[:500]

    if field_key not in _PARSED_FIELD_KEYS:
        raise HTTPException(status_code=422, detail=f"Unknown field: {field_key!r}")
    if reason_code not in _CORRECTION_REASON_CODES:
        reason_code = "other"

    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Token not found")

        # Select the right parsed dict and rebuild it with the corrected field.
        is_b = side == "b"
        existing: dict[str, Any] = dict(
            token.matrix_runout_b_parsed or {} if is_b else token.matrix_runout_parsed or {}
        )
        old_entry = existing.get(field_key, {})
        old_value = old_entry.get("value") if isinstance(old_entry, dict) else None

        existing[field_key] = {
            "value": new_value or None,
            "confidence": 1.0 if new_value else None,
            "source": "human",
        }

        if is_b:
            token.matrix_runout_b_parsed = existing
            # Keep plain text in sync with matrix_number when that field is edited.
            if field_key == "matrix_number":
                token.matrix_runout_b = new_value or token.matrix_runout_b
        else:
            token.matrix_runout_parsed = existing
            if field_key == "matrix_number":
                token.matrix_runout = new_value or token.matrix_runout

        rev_count: int = (
            await db.execute(select(func.count()).where(TokenRevision.token_id == tid))
        ).scalar_one()
        label_name = token.label.name if token.label else ""
        mfr_name = token.manufacturer.name if token.manufacturer else ""
        revision = TokenRevision(
            token_id=tid,
            revision_number=rev_count + 1,
            source=RevisionSource.HUMAN,
            data={
                **_revision_data(token, label_name, mfr_name),
                "correction": {
                    "side": side,
                    "field": field_key,
                    "field_label": _PARSED_FIELD_LABELS.get(field_key, field_key),
                    "old_value": old_value,
                    "new_value": new_value or None,
                    "reason_code": reason_code,
                    "reason_text": reason_text or None,
                },
            },
            comment=(
                f"Corrected {_PARSED_FIELD_LABELS.get(field_key, field_key)} "
                f"(side {'B' if is_b else 'A'}): {old_value!r} → {new_value!r}. "
                f"Reason: {reason_code}" + (f" — {reason_text}" if reason_text else "")
            ),
            created_by=_user_uuid(request),
        )
        db.add(revision)
        await db.commit()

    logger.info(
        "Matrix field corrected: pressing=%s side=%s field=%s reason=%s by user=%s",
        token_id,
        side,
        field_key,
        reason_code,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=field_corrected", status_code=303)
