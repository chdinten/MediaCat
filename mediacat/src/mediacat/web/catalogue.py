"""Catalogue routes -- Artist â†' Album â†' Pressing hierarchy.

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

import asyncio
import io
import logging
import os
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import exists, func, or_, select
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
from mediacat.ingestion.discogs_extras import (
    collection_view,
    group_runouts,
    mark_matching_variants,
    release_extra,
    variant_runout_fill,
)
from mediacat.web.routes import _ctx, _require_role, _tmpl

logger = logging.getLogger(__name__)

catalogue_router = APIRouter(prefix="/catalogue", tags=["catalogue"])

_USER_AGENT = "MediaCat/1.0 (catalogue browser)"
_DISCOGS_BASE = "https://api.discogs.com"
_MB_BASE = "https://musicbrainz.org/ws/2"
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB
_DISCOGS_IMAGE_LIMIT = 8  # max images to fetch per Discogs release


def _get_discogs_connector(request: Request) -> Any:
    """Return a DiscogsConnector from app state or create one on-the-fly."""
    from mediacat.ingestion.discogs import DiscogsConnector

    connectors = getattr(request.app.state, "connectors", {}) or {}
    dc = connectors.get("discogs")
    if dc:
        return dc

    import os
    import pathlib

    # Token resolution order: Docker secret file → env var
    token = ""  # empty until a token is found  # nosec B105
    for secret_path in ("/run/secrets/discogs_token", "/run/secrets/discogs_token.txt"):
        p = pathlib.Path(secret_path)
        if p.exists():
            token = p.read_text(encoding="utf-8").strip()
            break
    if not token:
        token = os.environ.get("DISCOGS_TOKEN", "")

    auth = f"Discogs token={token}" if token else None
    return DiscogsConnector(
        name="discogs_catalogue",
        base_url="https://api.discogs.com",
        auth_header=auth,
        rate_limit=55,
    )


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Internal helpers
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


def _sf(request: Request) -> Any:
    """The app's async_sessionmaker (set in the lifespan)."""
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


async def _get_or_create_country(db: Any, name: str) -> uuid.UUID | None:
    """Return the ID of a Country matching *name*, creating one if absent."""
    name = (name or "").strip()
    if not name:
        return None
    row = (
        await db.execute(select(Country).where(func.lower(Country.name) == name.lower()))
    ).scalar_one_or_none()
    if row:
        country_id: uuid.UUID = row.id
        return country_id
    # Generate deterministic synthetic alpha codes; retry on collision
    letters = re.sub(r"[^A-Za-z]", "", name).upper()
    a2_base = letters[:2] if len(letters) >= 2 else letters.ljust(2, "X")
    a3_base = letters[:3] if len(letters) >= 3 else letters.ljust(3, "X")
    for variant in range(10):
        sfx = str(variant) if variant > 0 else ""
        # The suffix replaces the last letter(s), so it survives the length cut.
        _a2 = a2_base[: 2 - len(sfx)] + sfx
        _a3 = a3_base[: 3 - len(sfx)] + sfx
        clash = (
            await db.execute(
                select(Country.id).where(or_(Country.alpha2 == _a2, Country.alpha3 == _a3))
            )
        ).scalar_one_or_none()
        if not clash:
            c = Country(alpha2=_a2, alpha3=_a3, name=name)
            db.add(c)
            await db.flush()
            logger.info("Created new country record: %r (%s/%s)", name, _a2, _a3)
            return c.id
    logger.warning("Could not create country record for name=%r", name)
    return None


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
    images: list[Any],  # raw Discogs JSON: entries are checked below
) -> int:
    """Download Discogs images and store them in MinIO. Returns count stored."""
    if not store or not images:
        return 0

    from mediacat.storage.object_store import ALLOWED_MIME_TYPES

    rows = (
        await db.execute(
            select(
                MediaObject.content_hash, MediaObject.source_url, MediaObject.is_primary_cover
            ).where(MediaObject.token_id == token_id)
        )
    ).all()
    existing_hashes: set[str] = {r.content_hash for r in rows if r.content_hash}
    existing_urls: set[str] = {r.source_url for r in rows if r.source_url}
    has_existing_primary: bool = any(r.is_primary_cover for r in rows)

    # Discogs types are "primary" (cover) and "secondary" (everything else).
    # Secondary images are often label shots or runout photos — assign sensible
    # default regions so the user's reassign dropdown starts closer to the truth.
    _secondary_region_sequence = [
        ImageRegion.COVER_BACK,
        ImageRegion.LABEL_A,
        ImageRegion.LABEL_B,
    ]
    primary_done = False
    secondary_count = 0
    stored = 0
    headers = _discogs_auth_header()

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, img in enumerate(images[:_DISCOGS_IMAGE_LIMIT]):
            if not isinstance(img, dict):
                continue
            url = img.get("uri") or img.get("uri150") or ""
            if not url or not url.startswith("http"):
                continue

            # Skip images already stored by source URL — avoids redundant downloads.
            if url in existing_urls:
                # Still advance region counters so relative ordering stays consistent.
                img_type = img.get("type", "secondary")
                if img_type == "primary":
                    primary_done = True
                else:
                    secondary_count += 1
                continue

            # Space requests to respect Discogs CDN rate limits.
            if idx > 0:
                await asyncio.sleep(0.5)

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
                r = await client.get(url, headers=headers)
                if r.status_code != 200 or len(r.content) < 1000:
                    logger.debug(
                        "Discogs image skip url=%s status=%d size=%d",
                        url,
                        r.status_code,
                        len(r.content),
                    )
                    continue
                mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                if mime not in ALLOWED_MIME_TYPES:
                    mime = "image/jpeg"
                obj = await store.put_image(r.content, mime)
                if obj.content_hash in existing_hashes:
                    existing_urls.add(url)
                    continue
                existing_hashes.add(obj.content_hash)
                existing_urls.add(url)
                set_primary = region == ImageRegion.COVER_FRONT and not has_existing_primary
                if set_primary:
                    has_existing_primary = True
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
                        is_primary_cover=set_primary,
                        source_url=url,
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
    loaded: Token = token
    return loaded


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Artist browser
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


# ── Facet filter helpers ──────────────────────────────────────────────────────


def _pagination_qs(
    q: str,
    fmt: str,
    genres: str,
    year_from: str,
    year_to: str,
    country_q: str,
) -> str:
    """URL-encoded query string for pagination links (empty params omitted)."""
    return urllib.parse.urlencode(
        {
            k: v
            for k, v in {
                "q": q,
                "fmt": fmt,
                "genres": genres,
                "year_from": year_from,
                "year_to": year_to,
                "country_q": country_q,
            }.items()
            if v
        }
    )


def _catalogue_url(**params: str) -> str:
    """Build /catalogue URL, omitting empty-string params."""
    filtered = {k: v for k, v in params.items() if v}
    return "/catalogue?" + urllib.parse.urlencode(filtered) if filtered else "/catalogue"


def _genre_toggle_url(
    genre: str,
    active_genres: list[str],
    q: str,
    fmt: str,
    year_from: str,
    year_to: str,
    country_q: str,
) -> str:
    genre_lower = genre.lower()
    current_lower = [g.lower() for g in active_genres]
    if genre_lower in current_lower:
        new_genres = [g for g in active_genres if g.lower() != genre_lower]
    else:
        new_genres = [*active_genres, genre]
    return _catalogue_url(
        q=q,
        fmt=fmt,
        genres=",".join(new_genres),
        year_from=year_from,
        year_to=year_to,
        country_q=country_q,
    )


def _fmt_toggle_url(
    fmt_name: str,
    current_fmt: str,
    genres: str,
    q: str,
    year_from: str,
    year_to: str,
    country_q: str,
) -> str:
    new_fmt = "" if current_fmt == fmt_name else fmt_name
    return _catalogue_url(
        q=q,
        fmt=new_fmt,
        genres=genres,
        year_from=year_from,
        year_to=year_to,
        country_q=country_q,
    )


def _decade_toggle_url(
    decade: int,
    year_from: str,
    year_to: str,
    q: str,
    fmt: str,
    genres: str,
    country_q: str,
) -> str:
    dec_from = str(decade)
    dec_to = str(decade + 9)
    if year_from == dec_from and year_to == dec_to:
        return _catalogue_url(q=q, fmt=fmt, genres=genres, country_q=country_q)
    return _catalogue_url(
        q=q,
        fmt=fmt,
        genres=genres,
        year_from=dec_from,
        year_to=dec_to,
        country_q=country_q,
    )


async def _fetch_facets(
    db: Any,
    q: str,
    fmt: str,
    year_from: str,
    year_to: str,
    country_q: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return genre/format/decade counts for the current (non-genre) filters."""
    from sqlalchemy import text as _text

    base_clauses: list[str] = [
        "t.deleted_at IS NULL",
        "t.status != 'merged'",
        "t.artist IS NOT NULL",
    ]
    base_params: dict[str, Any] = {}

    if q.strip():
        base_clauses.append("(t.artist ILIKE :q_like OR t.title ILIKE :q_like)")
        base_params["q_like"] = f"%{q.strip()}%"
    if year_from.strip().isdigit():
        base_clauses.append("t.year >= :year_from")
        base_params["year_from"] = int(year_from.strip())
    if year_to.strip().isdigit():
        base_clauses.append("t.year <= :year_to")
        base_params["year_to"] = int(year_to.strip())
    if country_q.strip():
        base_clauses.append(
            "t.country_id IN (SELECT id FROM countries WHERE name ILIKE :country_like)"
        )
        base_params["country_like"] = f"%{country_q.strip()}%"

    # Format filter applies to genre + decade queries; NOT to format counts (show all formats)
    full_clauses = base_clauses.copy()
    full_params = base_params.copy()
    if fmt in ("vinyl", "cd"):
        full_clauses.append("t.media_format::text = :fmt")  # enum vs varchar bind
        full_params["fmt"] = fmt

    full_where = " AND ".join(full_clauses)
    base_where = " AND ".join(base_clauses)

    def _sql(sql_str: str, params: dict[str, Any]) -> Any:
        s = _text(sql_str)
        return s.bindparams(**params) if params else s

    genre_rows = (
        await db.execute(
            _sql(
                f"SELECT g.value AS genre, count(t.id) AS cnt "  # noqa: S608  # fixed clause fragments; values are bound params  # nosec B608
                f"FROM tokens t "
                f"CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(t.genres, '[]'::jsonb)) AS g(value) "
                f"WHERE {full_where} "
                f"GROUP BY g.value ORDER BY cnt DESC, g.value LIMIT 30",
                full_params,
            )
        )
    ).all()

    format_rows = (
        await db.execute(
            _sql(
                f"SELECT t.media_format::text AS fmt, count(t.id) AS cnt "  # noqa: S608  # fixed clause fragments; values are bound params  # nosec B608
                f"FROM tokens t "
                f"WHERE {base_where} AND t.media_format IS NOT NULL "
                f"GROUP BY t.media_format ORDER BY cnt DESC",
                base_params,
            )
        )
    ).all()

    decade_rows = (
        await db.execute(
            _sql(
                f"SELECT (floor(t.year::numeric / 10) * 10)::int AS decade, count(t.id) AS cnt "  # noqa: S608  # fixed clause fragments; values are bound params  # nosec B608
                f"FROM tokens t "
                f"WHERE {full_where} AND t.year IS NOT NULL AND t.year BETWEEN 1900 AND 2100 "
                f"GROUP BY decade ORDER BY decade",
                full_params,
            )
        )
    ).all()

    return {
        "genres": [{"name": r.genre, "count": r.cnt} for r in genre_rows],
        "formats": [{"name": r.fmt, "count": r.cnt} for r in format_rows],
        "decades": [
            {"label": f"{r.decade}s", "decade": r.decade, "count": r.cnt} for r in decade_rows
        ],
    }


@catalogue_router.get("", response_class=HTMLResponse)
async def catalogue_index(
    request: Request,
    q: str = Query(""),
    fmt: str = Query(""),
    genres: str = Query(""),
    year_from: str = Query(""),
    year_to: str = Query(""),
    country_q: str = Query(""),
    page: int = Query(1, ge=1),
    artist: str = Query(""),
) -> HTMLResponse:
    """Browse artists grouped from the Token table."""
    page_size = 24
    offset = (page - 1) * page_size

    from sqlalchemy import text as _text

    genre_list = [g.strip() for g in genres.split(",") if g.strip()]

    # Build the genre WHERE fragment (shared by both query paths)
    _genre_sql: str = ""
    _genre_params: dict[str, str] = {}
    if genre_list:
        _parts = []
        for _i, _g in enumerate(genre_list):
            _pn = f"genre_{_i}"
            _parts.append(
                f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(tokens.genres) _ge"  # noqa: S608  # placeholder names only  # nosec B608
                f" WHERE lower(_ge) = :{_pn})"
            )
            _genre_params[_pn] = _g.lower()
        _genre_sql = "(" + " OR ".join(_parts) + ")"

    facets_genres: list[dict[str, Any]] = []
    facets_formats: list[dict[str, Any]] = []
    facets_decades: list[dict[str, Any]] = []

    try:
        async with _sf(request)() as db:
            _raw = await _fetch_facets(db, q, fmt, year_from, year_to, country_q)
            _gl_lower = {g.lower() for g in genre_list}
            facets_genres = [
                {
                    "name": _g["name"],
                    "count": _g["count"],
                    "active": _g["name"].lower() in _gl_lower,
                    "url": _genre_toggle_url(
                        _g["name"], genre_list, q, fmt, year_from, year_to, country_q
                    ),
                }
                for _g in _raw["genres"]
            ]
            facets_formats = [
                {
                    "name": _f["name"],
                    "count": _f["count"],
                    "active": fmt == _f["name"],
                    "url": _fmt_toggle_url(
                        _f["name"], fmt, genres, q, year_from, year_to, country_q
                    ),
                }
                for _f in _raw["formats"]
            ]
            facets_decades = [
                {
                    "label": _d["label"],
                    "decade": _d["decade"],
                    "count": _d["count"],
                    "active": year_from == str(_d["decade"]) and year_to == str(_d["decade"] + 9),
                    "url": _decade_toggle_url(
                        _d["decade"], year_from, year_to, q, fmt, genres, country_q
                    ),
                }
                for _d in _raw["decades"]
            ]

            if genre_list:
                # ── Genre filter active: flat pressing list (individual tokens) ──
                _cover_sq = exists(
                    select(MediaObject.id)
                    .where(MediaObject.token_id == Token.id)
                    .where(MediaObject.is_primary_cover.is_(True))
                ).label("has_cover")
                token_q = (
                    select(
                        Token.id,
                        Token.artist,
                        Token.title,
                        Token.year,
                        Token.media_format,
                        Token.genres,
                        Token.catalog_number,
                        Token.country_id,
                        Token.matrix_runout,
                        Token.matrix_runout_b,
                        Token.tracklist,
                        _cover_sq,
                    )
                    .where(Token.deleted_at.is_(None))
                    .where(Token.status != TokenStatus.MERGED)
                    .where(Token.artist.isnot(None))
                    .where(_text(_genre_sql).bindparams(**_genre_params))
                )
                if q:
                    token_q = token_q.where(
                        or_(Token.artist.ilike(f"%{q}%"), Token.title.ilike(f"%{q}%"))
                    )
                if fmt in ("vinyl", "cd"):
                    token_q = token_q.where(Token.media_format == fmt)
                if year_from.strip().isdigit():
                    token_q = token_q.where(Token.year >= int(year_from.strip()))
                if year_to.strip().isdigit():
                    token_q = token_q.where(Token.year <= int(year_to.strip()))
                if country_q.strip():
                    from mediacat.db.models import Country as _Country

                    token_q = token_q.where(
                        Token.country_id.in_(
                            select(_Country.id).where(_Country.name.ilike(f"%{country_q.strip()}%"))
                        )
                    )

                total: int = (
                    await db.execute(select(func.count()).select_from(token_q.subquery()))
                ).scalar_one()
                token_rows = (
                    await db.execute(
                        token_q.order_by(Token.artist.asc(), Token.title.asc())
                        .offset(offset)
                        .limit(page_size)
                    )
                ).all()

                genre_pressings = [
                    {
                        "id": str(r.id),
                        "artist": r.artist or "?",
                        "title": r.title or "?",
                        "year": r.year,
                        "fmt": r.media_format.value if r.media_format else "",
                        "genres": ", ".join(r.genres) if r.genres else "",
                        "catalog_number": r.catalog_number or "",
                        "completeness": (
                            (1 if r.has_cover else 0)
                            + (1 if r.tracklist else 0)
                            + (1 if r.country_id else 0)
                            + (1 if (r.matrix_runout or r.matrix_runout_b) else 0)
                        ),
                    }
                    for r in token_rows
                ]

                ctx = _ctx(
                    request,
                    artists=[],
                    genre_pressings=genre_pressings,
                    query=q,
                    fmt_filter=fmt,
                    genres_filter=genres,
                    year_from=year_from,
                    year_to=year_to,
                    country_q=country_q,
                    page=page,
                    total_pages=max(1, (total + page_size - 1) // page_size),
                    pagination_qs=_pagination_qs(q, fmt, genres, year_from, year_to, country_q),
                    facets_genres=facets_genres,
                    facets_formats=facets_formats,
                    facets_decades=facets_decades,
                    db_error=False,
                    open_artist="",
                    open_artist_enc="",
                )
                return _tmpl().TemplateResponse(request=request, name="catalogue.html", context=ctx)

            # ── No genre filter: standard artist browser ──
            base = (
                select(
                    Token.artist,
                    func.count(Token.id).label("cnt"),
                    func.min(Token.year).label("yr_min"),
                    func.max(Token.year).label("yr_max"),
                    func.count(Token.id).filter(Token.media_format == "vinyl").label("vinyl_count"),
                    func.count(Token.id).filter(Token.media_format == "cd").label("cd_count"),
                )
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
                .where(Token.artist.isnot(None))
            )
            if q:
                base = base.where(or_(Token.artist.ilike(f"%{q}%"), Token.title.ilike(f"%{q}%")))
            if fmt in ("vinyl", "cd"):
                base = base.where(Token.media_format == fmt)
            if year_from.strip().isdigit():
                base = base.where(Token.year >= int(year_from.strip()))
            if year_to.strip().isdigit():
                base = base.where(Token.year <= int(year_to.strip()))
            if country_q.strip():
                from mediacat.db.models import Country as _Country

                base = base.where(
                    Token.country_id.in_(
                        select(_Country.id).where(_Country.name.ilike(f"%{country_q.strip()}%"))
                    )
                )

            grouped = base.group_by(Token.artist).subquery()
            total = (await db.execute(select(func.count()).select_from(grouped))).scalar_one()

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
            genre_pressings=[],
            query=q,
            fmt_filter=fmt,
            genres_filter=genres,
            year_from=year_from,
            year_to=year_to,
            country_q=country_q,
            page=1,
            total_pages=1,
            pagination_qs=_pagination_qs(q, fmt, genres, year_from, year_to, country_q),
            facets_genres=[],
            facets_formats=[],
            facets_decades=[],
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
            "vinyl_count": r.vinyl_count,
            "cd_count": r.cd_count,
        }
        for r in rows
    ]

    ctx = _ctx(
        request,
        artists=artists,
        genre_pressings=[],
        query=q,
        fmt_filter=fmt,
        genres_filter=genres,
        year_from=year_from,
        year_to=year_to,
        country_q=country_q,
        page=page,
        total_pages=max(1, (total + page_size - 1) // page_size),
        pagination_qs=_pagination_qs(q, fmt, genres, year_from, year_to, country_q),
        facets_genres=facets_genres,
        facets_formats=facets_formats,
        facets_decades=facets_decades,
        db_error=False,
        open_artist=artist,
        open_artist_enc=urllib.parse.quote(artist, safe="") if artist else "",
        has_next=(page * page_size) < total,
        next_page=page + 1,
        fmt=fmt,
        q_enc=urllib.parse.quote(q, safe=""),
        country_q_enc=urllib.parse.quote(country_q, safe=""),
    )
    return _tmpl().TemplateResponse(request=request, name="catalogue.html", context=ctx)


@catalogue_router.get("/artists-partial", response_class=HTMLResponse)
async def catalogue_artists_partial(
    request: Request,
    q: str = Query(""),
    fmt: str = Query(""),
    year_from: str = Query(""),
    year_to: str = Query(""),
    country_q: str = Query(""),
    page: int = Query(2, ge=2),
) -> HTMLResponse:
    """Return the next page of artist cards for infinite scroll (HTMX partial)."""
    page_size = 24
    offset = (page - 1) * page_size

    async with _sf(request)() as db:
        base = (
            select(
                Token.artist,
                func.count(Token.id).label("cnt"),
                func.min(Token.year).label("yr_min"),
                func.max(Token.year).label("yr_max"),
                func.count(Token.id).filter(Token.media_format == "vinyl").label("vinyl_count"),
                func.count(Token.id).filter(Token.media_format == "cd").label("cd_count"),
            )
            .where(Token.deleted_at.is_(None))
            .where(Token.status != TokenStatus.MERGED)
            .where(Token.artist.isnot(None))
        )
        if q:
            base = base.where(or_(Token.artist.ilike(f"%{q}%"), Token.title.ilike(f"%{q}%")))
        if fmt in ("vinyl", "cd"):
            base = base.where(Token.media_format == fmt)
        if year_from.strip().isdigit():
            base = base.where(Token.year >= int(year_from.strip()))
        if year_to.strip().isdigit():
            base = base.where(Token.year <= int(year_to.strip()))
        if country_q.strip():
            from mediacat.db.models import Country as _Country

            base = base.where(
                Token.country_id.in_(
                    select(_Country.id).where(_Country.name.ilike(f"%{country_q.strip()}%"))
                )
            )

        grouped = base.group_by(Token.artist).subquery()
        total = (await db.execute(select(func.count()).select_from(grouped))).scalar_one()
        rows = (
            await db.execute(
                base.group_by(Token.artist)
                .order_by(Token.artist.asc())
                .offset(offset)
                .limit(page_size)
            )
        ).all()

    artists = [
        {
            "name": r.artist,
            "pressing_count": r.cnt,
            "year_range": (
                str(r.yr_min) + (f"-{r.yr_max}" if r.yr_max != r.yr_min else "") if r.yr_min else ""
            ),
            "enc": urllib.parse.quote(r.artist, safe=""),
            "vinyl_count": r.vinyl_count,
            "cd_count": r.cd_count,
        }
        for r in rows
    ]

    return _tmpl().TemplateResponse(
        request=request,
        name="partials/artist_rows.html",
        context={
            "request": request,
            "artists": artists,
            "has_next": (page * page_size) < total,
            "next_page": page + 1,
            "q_enc": urllib.parse.quote(q, safe=""),
            "fmt": fmt,
            "year_from": year_from,
            "year_to": year_to,
            "country_q_enc": urllib.parse.quote(country_q, safe=""),
        },
    )


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Create
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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
    _require_role(request, "admin", "reviewer", "contributor")
    prefill: dict[str, Any] = {}
    duplicate: dict[str, Any] | None = None
    search_results: list[dict[str, Any]] = []
    search_error: str | None = scan_error.strip() or None

    # â"€â"€ Smart detection: resolve q into the right search type â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    q_raw = q.strip()
    if q_raw and not discogs_id and not mb_id:
        if _UUID_RE.match(q_raw):
            mb_id = q_raw  # UUID â†' MusicBrainz
            q_raw = ""
        elif q_raw.isdigit() and len(q_raw) in (8, 12, 13):
            pass  # barcode â†' text search (handled below as barcode)
        elif q_raw.isdigit():
            discogs_id = q_raw  # short integer â†' Discogs release ID
            q_raw = ""

    # â"€â"€ External API calls (before opening DB session) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    if discogs_id.strip() and not search_error:
        did = _parse_int(discogs_id.strip())
        if did:
            try:
                prefill = _map_discogs_fields(await _fetch_discogs_release(did))
            except httpx.HTTPStatusError as exc:
                search_error = (
                    f"Discogs release {discogs_id} not found."
                    if exc.response.status_code == 404
                    else "Discogs API error -- try again."
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
                    else "MusicBrainz API error -- try again."
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
                    "Discogs rate limit reached -- please wait a moment before searching again."
                )
            else:
                logger.warning("New-form search HTTP %d: q=%r", exc.response.status_code, q_raw)
                search_error = "External search unavailable -- API returned an error."
        except Exception as exc:
            logger.warning(
                "New-form external search failed: q=%r source=%r: %s", q_raw, source, exc
            )
            search_error = "External search unavailable -- check your connection."

    # â"€â"€ DB: countries + duplicate checks â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    async with _sf(request)() as db:
        countries = (await db.execute(select(Country).order_by(Country.name))).scalars().all()

        # Single-result prefill: check for duplicate by external ID
        if prefill and not search_error:
            did_int = _parse_int(str(prefill.get("discogs_release_id") or ""))
            mbid_str = prefill.get("musicbrainz_release_id") or ""
            dup_token: Token | None = None
            if did_int:
                # Several live rows may share a release id (copies, merged duplicates).
                dup_token = (
                    (
                        await db.execute(
                            select(Token)
                            .where(Token.discogs_release_id == did_int)
                            .where(Token.deleted_at.is_(None))
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
            elif mbid_str:
                dup_token = (
                    (
                        await db.execute(
                            select(Token)
                            .where(Token.musicbrainz_release_id == mbid_str)
                            .where(Token.deleted_at.is_(None))
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
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

            existing_d: set[Any] = set()
            existing_mb: set[Any] = set()
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
    country_name: str = Form(""),
    label_name: str = Form(""),
    manufacturer_name: str = Form(""),
    discogs_release_id: str = Form(""),
    musicbrainz_release_id: str = Form(""),
    comment: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    _require_role(request, "admin", "reviewer", "contributor")
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
    did = _parse_int(discogs_release_id)

    async with _sf(request)() as db:
        cid = await _get_or_create_country(db, country_name)
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
        await db.flush()  # assigns revision.id (a client-side default)
        token.current_revision_id = revision.id
        await db.commit()
        new_id = str(token.id)

    logger.info("Token created: %s by user=%s", new_id, request.state.user_id)
    return RedirectResponse(url=f"/catalogue/{new_id}", status_code=303)


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# External search (Discogs / MusicBrainz)
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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
                error = "Discogs rate limit reached -- please wait a moment before searching again."
            else:
                logger.warning("External search HTTP %d for q=%r", exc.response.status_code, q)
                error = "Search unavailable -- external API returned an error."
        except Exception as exc:
            logger.warning("External search failed for q=%r source=%r: %s", q, source, exc)
            error = "Search unavailable -- could not reach the external API."

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
        # Discogs search returns "Artist -- Title" in the title field
        if " -- " in raw:
            artist_part, title_part = raw.split(" -- ", 1)
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


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Merge
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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
    wid = _parse_uuid(winner_id)
    lid = _parse_uuid(loser_id)
    if wid is not None and wid == lid:
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
        await db.flush()  # assigns revision.id (a client-side default)
        winner.current_revision_id = revision.id
        await db.commit()

    logger.info(
        "Merged loser=%s into winner=%s by user=%s", loser_id, winner_id, request.state.user_id
    )
    return RedirectResponse(url=f"/catalogue/{winner_id}", status_code=303)


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Barcode scan from cover image
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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
                "Barcode scanning unavailable -- rebuild the app image (make build-app)."
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
        # Unique match -- pre-fill directly
        return RedirectResponse(
            url=f"/catalogue/new?discogs_id={results[0]['id']}",
            status_code=303,
        )

    # Multiple matches or no match -- show results list (barcode as search query)
    return RedirectResponse(
        url="/catalogue/new?q=" + urllib.parse.quote(barcode_value),
        status_code=303,
    )


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Discogs import
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


async def _discogs_barcode_search(barcode: str) -> list[dict[str, Any]]:
    """Search Discogs by exact barcode -- more precise than a text query."""
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
        if " -- " in raw:
            artist_part, title_part = raw.split(" -- ", 1)
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


def _discogs_auth_header() -> dict[str, str]:
    """Return auth + user-agent headers for Discogs API and CDN requests."""
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    token = os.environ.get("DISCOGS_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    return headers


async def _fetch_discogs_release(release_id: int) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_DISCOGS_BASE}/releases/{release_id}",
            headers=_discogs_auth_header(),
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

    raw_genres = list(data.get("genres") or []) + list(data.get("styles") or [])

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
        "genres": _sanitize_genre_list(raw_genres),
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
            genres=fields.get("genres"),
            extra={"discogs": release_extra(data)},
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
        await db.flush()  # assigns revision.id (a client-side default)
        token.current_revision_id = revision.id

        store = getattr(request.app.state, "object_store", None)
        img_count = 0
        try:
            img_count = await _import_discogs_images(store, db, token.id, data.get("images") or [])
        except Exception as exc:
            logger.warning("Discogs image import failed for release %d: %s", release_id, exc)

        await db.commit()
        new_id = str(token.id)

    # Collection custom fields (best effort — needs the admin Discogs token)
    try:
        from mediacat.web.discogs_details import refresh_token_discogs_details

        await refresh_token_discogs_details(request, token, release=data)
    except Exception as exc:
        logger.warning("Collection field fetch failed for release %d: %s", release_id, exc)

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
        await db.flush()  # assigns revision.id (a client-side default)
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


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# HTMX drill-down partials
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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
                .order_by(func.min(Token.year).asc().nulls_last(), Token.title.asc())
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

    for token in tokens:
        score = (
            (1 if any(mo.is_primary_cover for mo in token.media_objects) else 0)
            + (1 if token.tracklist else 0)
            + (1 if token.country_id else 0)
            + (1 if (token.matrix_runout or token.matrix_runout_b) else 0)
        )
        token.completeness = score

    ctx = _ctx(request, artist=name, album_title=album, tokens=tokens)
    return _tmpl().TemplateResponse(
        request=request, name="partials/pressing_list.html", context=ctx
    )


@catalogue_router.get("/albums", response_class=HTMLResponse)
async def artist_albums_by_query(
    request: Request,
    artist: str = Query(""),
) -> HTMLResponse:
    """Query-param version of artist_albums — safe for names containing '/'."""
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
                .order_by(func.min(Token.year).asc().nulls_last(), Token.title.asc())
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


@catalogue_router.get("/pressings", response_class=HTMLResponse)
async def album_pressings_by_query(
    request: Request,
    artist: str = Query(""),
    title: str = Query(""),
) -> HTMLResponse:
    """Query-param version of album_pressings — safe for names containing '/'."""
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

    for token in tokens:
        score = (
            (1 if any(mo.is_primary_cover for mo in token.media_objects) else 0)
            + (1 if token.tracklist else 0)
            + (1 if token.country_id else 0)
            + (1 if (token.matrix_runout or token.matrix_runout_b) else 0)
        )
        token.completeness = score

    ctx = _ctx(request, artist=name, album_title=album, tokens=tokens)
    return _tmpl().TemplateResponse(
        request=request, name="partials/pressing_list.html", context=ctx
    )


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Pressing detail
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


@catalogue_router.get("/autocomplete/countries", response_class=HTMLResponse)
async def autocomplete_countries(
    request: Request,
    country_q: str = Query(""),
) -> HTMLResponse:
    """Return <option> elements for the country datalist (HTMX target)."""
    import html as _html

    term = country_q.strip()
    if len(term) < 2:
        return HTMLResponse("")
    try:
        async with _sf(request)() as db:
            rows = (
                (
                    await db.execute(
                        select(Country.name)
                        .where(Country.name.ilike(f"%{term}%"))
                        .order_by(Country.name)
                        .limit(12)
                    )
                )
                .scalars()
                .all()
            )
    except SQLAlchemyError:
        return HTMLResponse("")
    return HTMLResponse("".join(f"<option value='{_html.escape(n)}'>" for n in rows))


@catalogue_router.get("/facets")
async def catalogue_facets(
    request: Request,
    q: str = Query(""),
    fmt: str = Query(""),
    year_from: str = Query(""),
    year_to: str = Query(""),
    country_q: str = Query(""),
) -> JSONResponse:
    """Return genre/format/decade counts as JSON for the faceted filter pills."""
    _require_role(request)
    try:
        async with _sf(request)() as db:
            data = await _fetch_facets(db, q, fmt, year_from, year_to, country_q)
        return JSONResponse(data)
    except SQLAlchemyError as exc:
        logger.error("Facets endpoint DB error: %s", exc)
        return JSONResponse({"genres": [], "formats": [], "decades": []})


@catalogue_router.get("/debug/facets", response_class=HTMLResponse)
async def debug_facets(
    request: Request,
    q: str = Query(""),
    fmt: str = Query(""),
    genres: str = Query(""),
    year_from: str = Query(""),
    year_to: str = Query(""),
    country_q: str = Query(""),
) -> HTMLResponse:
    """Debug view: raw facets data, active params, and timing."""
    import json
    import time

    t0 = time.monotonic()
    try:
        async with _sf(request)() as db:
            data = await _fetch_facets(db, q, fmt, year_from, year_to, country_q)
    except SQLAlchemyError as exc:
        return HTMLResponse(f"<p class='diag-none'>DB error: {exc}</p>")
    elapsed_ms = (time.monotonic() - t0) * 1000

    active_params = {
        "q": q,
        "fmt": fmt,
        "genres": genres,
        "year_from": year_from,
        "year_to": year_to,
        "country_q": country_q,
    }
    active_display = {k: v for k, v in active_params.items() if v}

    lines = [
        "<div class='diag-section'>",
        "<p><strong>Active filters passed to facets query:</strong> "
        + (
            " &nbsp;·&nbsp; ".join(f"<code>{k}={v!r}</code>" for k, v in active_display.items())
            or "<em>none</em>"
        )
        + f" &nbsp;&mdash;&nbsp; <span class='text-muted'>fetched in {elapsed_ms:.1f}&thinsp;ms</span></p>",
        "<p class='text-muted' style='font-size:.8em'>Note: <code>genres</code> is shown but "
        "<strong>not</strong> applied to the facet counts — it reflects only the current URL "
        "so you can see what the template received.</p>",
        "</div>",
        "<div class='diag-section'>",
        f"<p><strong>Genres &mdash; {len(data['genres'])} distinct value(s)</strong></p>",
        "<table class='diag-table'><thead><tr><th>Genre</th><th>Count</th></tr></thead><tbody>",
    ]
    for g in data["genres"]:
        lines.append(f"<tr><td><code>{g['name']}</code></td><td>{g['count']}</td></tr>")
    if not data["genres"]:
        lines.append("<tr><td colspan='2'><em>No genres in DB yet</em></td></tr>")
    lines.append("</tbody></table></div>")

    lines += [
        "<div class='diag-section'>",
        f"<p><strong>Formats &mdash; {len(data['formats'])} found</strong></p>",
        "<table class='diag-table'><thead><tr><th>Format</th><th>Count</th></tr></thead><tbody>",
    ]
    for f in data["formats"]:
        lines.append(f"<tr><td><code>{f['name']}</code></td><td>{f['count']}</td></tr>")
    lines.append("</tbody></table></div>")

    lines += [
        "<div class='diag-section'>",
        f"<p><strong>Decades &mdash; {len(data['decades'])} found</strong></p>",
        "<table class='diag-table'><thead><tr><th>Decade</th><th>Count</th></tr></thead><tbody>",
    ]
    for d in data["decades"]:
        lines.append(f"<tr><td><code>{d['label']}</code></td><td>{d['count']}</td></tr>")
    if not data["decades"]:
        lines.append("<tr><td colspan='2'><em>No year data yet</em></td></tr>")
    lines.append("</tbody></table></div>")

    lines += [
        "<div class='diag-section'>",
        "<p><strong>Raw JSON response from <code>/catalogue/facets</code>:</strong></p>",
        f"<pre class='diag-json'>{json.dumps(data, indent=2)}</pre>",
        "</div>",
    ]
    return HTMLResponse("\n".join(lines))


@catalogue_router.get("/debug/token-genres", response_class=HTMLResponse)
async def debug_token_genres(request: Request, token_id: str = Query("")) -> HTMLResponse:
    """Return genres stored for a given token UUID (HTML fragment for HTMX)."""
    from sqlalchemy import text as _text

    if not token_id.strip():
        return HTMLResponse("<p class='text-muted'>Enter a token UUID above.</p>")

    tid = token_id.strip()
    async with _sf(request)() as db:
        rows = (
            await db.execute(
                select(Token.id, Token.artist, Token.title, Token.genres)
                .where(Token.deleted_at.is_(None))
                .where(_text("tokens.id::text ILIKE :pat").bindparams(pat=f"{tid}%"))
                .limit(10)
            )
        ).all()

    if not rows:
        return HTMLResponse(f"<p class='diag-none'>No token found matching <code>{tid}</code></p>")

    html_parts: list[str] = []
    for row in rows:
        genres_raw = row.genres
        genres_display = ", ".join(genres_raw) if genres_raw else "<em>NULL — no genres stored</em>"
        html_parts.append(
            f"<div class='diag-token-row'>"
            f"<code class='diag-uuid'>{row.id}</code>&nbsp;"
            f"<strong>{row.artist or '?'}</strong> — {row.title or '?'}<br>"
            f"<span class='diag-label'>genres JSONB raw:</span> <code>{genres_raw!r}</code><br>"
            f"<span class='diag-label'>Rendered:</span> {genres_display}"
            f"</div>"
        )
    return HTMLResponse("\n".join(html_parts))


@catalogue_router.get("/debug/genre-search", response_class=HTMLResponse)
async def debug_genre_search(request: Request, genre: str = Query("")) -> HTMLResponse:
    """Show which tokens the genre filter finds for a given string (HTMX fragment)."""
    from sqlalchemy import text as _text

    genre = genre.strip()
    if not genre:
        return HTMLResponse("<p class='text-muted'>Enter a genre string above.</p>")

    async with _sf(request)() as db:
        sql = _text(
            "SELECT id, artist, title, genres "
            "FROM tokens "
            "WHERE deleted_at IS NULL "
            "AND EXISTS ("
            "  SELECT 1 FROM jsonb_array_elements_text(tokens.genres) _ge"
            "  WHERE lower(_ge) = :genre"
            ") ORDER BY artist LIMIT 50"
        ).bindparams(genre=genre.lower())
        rows = (await db.execute(sql)).all()

    if not rows:
        return HTMLResponse(
            f"<p class='diag-none'>No tokens matched <code>{genre!r}</code> "
            f"(searched lower-cased: <code>{genre.lower()!r}</code>)</p>"
        )

    lines = [
        f"<p class='diag-ok'><strong>{len(rows)}</strong> token(s) match "
        f"<code>{genre.lower()!r}</code>:</p>",
        "<ul class='diag-list'>",
    ]
    for row in rows:
        stored = ", ".join(row.genres) if row.genres else "—"
        lines.append(
            f"<li><code class='diag-uuid'>{row.id}</code> — "
            f"<strong>{row.artist or '?'}</strong> / {row.title or '?'}"
            f"&nbsp;|&nbsp;genres: <code>{stored}</code></li>"
        )
    lines.append("</ul>")
    return HTMLResponse("\n".join(lines))


@catalogue_router.get("/debug/all-genres", response_class=HTMLResponse)
async def debug_all_genres(request: Request) -> HTMLResponse:
    """List every distinct genre stored in the DB with token counts (HTMX fragment)."""
    from sqlalchemy import text as _text

    DATALIST_GENRES = [
        "Rock",
        "Pop",
        "Jazz",
        "Classical",
        "Electronic",
        "Hip-Hop",
        "R&B",
        "Soul",
        "Funk",
        "Blues",
        "Folk",
        "Country",
        "Reggae",
        "Metal",
        "Punk",
        "Alternative",
        "Indie",
        "Ambient",
        "Experimental",
        "World",
        "Latin",
        "Psychedelic Rock",
        "Progressive Rock",
        "Hard Rock",
        "Krautrock",
        "New Wave",
        "Post-Punk",
        "Synthpop",
        "Disco",
        "House",
        "Techno",
    ]
    datalist_lower = {g.lower() for g in DATALIST_GENRES}

    async with _sf(request)() as db:
        sql = _text(
            "SELECT g.value AS genre, count(t.id) AS cnt "
            "FROM tokens t, jsonb_array_elements_text(t.genres) AS g(value) "
            "WHERE t.deleted_at IS NULL "
            "GROUP BY g.value ORDER BY cnt DESC, g.value"
        )
        rows = (await db.execute(sql)).all()

    if not rows:
        return HTMLResponse("<p class='diag-none'>No genres stored in the database yet.</p>")

    lines = [
        f"<p><strong>{len(rows)} distinct genre value(s)</strong> stored across all tokens.</p>",
        "<table class='diag-table'><thead><tr>"
        "<th>Exact DB value</th><th>Tokens</th><th>In filter dropdown?</th>"
        "</tr></thead><tbody>",
    ]
    for row in rows:
        in_list = "yes" if row.genre.lower() in datalist_lower else "NO — not in dropdown"
        row_class = "" if row.genre.lower() in datalist_lower else " class='diag-mismatch'"
        lines.append(
            f"<tr{row_class}>"
            f"<td><code>{row.genre}</code></td>"
            f"<td>{row.cnt}</td>"
            f"<td>{in_list}</td></tr>"
        )
    lines.append("</tbody></table>")
    return HTMLResponse("\n".join(lines))


@catalogue_router.get("/{token_id}", response_class=HTMLResponse)
async def pressing_detail(request: Request, token_id: str) -> HTMLResponse:
    token = await _load_token(request, token_id)

    # slug â†' Symbol lookup for rendering matrix_runout_parts in the template
    symbol_by_slug: dict[str, Any] = {
        ts.symbol.slug: ts.symbol for ts in token.token_symbols if ts.symbol is not None
    }

    # Pre-filter images relevant to the manufacturing/stamping section
    _runout_regions = {
        "runout_a",
        "runout_b",
        "matrix",
        "label_a",
        "label_b",
        "label_c",
        "label_d",
        "label_e",
        "label_f",
        "label_g",
        "label_h",
    }
    runout_images = [
        img for img in token.media_objects if img.region and img.region.value in _runout_regions
    ]

    # Map image_id â†' latest OcrArtifact (most recent by created_at) for the template
    ocr_artifacts_by_image: dict[str, OcrArtifact] = {}
    for img in runout_images:
        if img.ocr_artifacts:
            latest = max(img.ocr_artifacts, key=lambda a: a.created_at or 0)
            ocr_artifacts_by_image[str(img.id)] = latest

    vision_available = getattr(request.app.state, "vision", None) is not None

    # Derive header display text from parsed breakdown when available.
    # Falls back to the plain stored text so old records still display.
    def _display(parsed: dict[str, Any] | None, fallback: str | None) -> str | None:
        return _build_full_runout_text(parsed or {}) or fallback

    # Group tracklist by vinyl side (A, B, Câ€¦) when positions start with a
    # letter, otherwise fall back to disc number grouping.
    tracklist_discs: list[dict[str, Any]] = []
    if token.tracklist:
        import re
        from collections import defaultdict

        key_order: list[Any] = []
        by_key: dict[Any, list[Any]] = defaultdict(list)
        for track in token.tracklist:
            pos = str(track.get("position") or "")
            m = re.match(r"^([A-Za-z]+)", pos)
            key = ("side", m.group(1).upper()) if m else ("disc", track.get("disc", 1))
            if key not in key_order:
                key_order.append(key)
            by_key[key].append(track)

        tracklist_discs = [
            {"label_type": k[0], "disc": k[1], "tracks": by_key[k]} for k in key_order
        ]

    # All genres: DB values merged with a standard curated list, sorted
    _DATALIST_GENRES = [
        "Rock",
        "Pop",
        "Jazz",
        "Classical",
        "Electronic",
        "Hip-Hop",
        "R&B",
        "Soul",
        "Funk",
        "Blues",
        "Folk",
        "Country",
        "Reggae",
        "Metal",
        "Punk",
        "Alternative",
        "Indie",
        "Ambient",
        "Experimental",
        "World",
        "Latin",
        "Psychedelic Rock",
        "Progressive Rock",
        "Hard Rock",
        "Krautrock",
        "New Wave",
        "Post-Punk",
        "Synthpop",
        "Disco",
        "House",
        "Techno",
    ]
    all_genres: list[str] = []
    try:
        from sqlalchemy import text as _gtxt

        async with _sf(request)() as _gdb:
            _grows = (
                await _gdb.execute(
                    _gtxt(
                        "SELECT DISTINCT g.value AS genre "
                        "FROM tokens t, jsonb_array_elements_text(t.genres) AS g(value) "
                        "WHERE t.deleted_at IS NULL ORDER BY genre"
                    )
                )
            ).all()
        _seen: set[str] = set()
        _merged: list[str] = []
        for r in _grows:
            _seen.add(r.genre.lower())
            _merged.append(r.genre)
        for g in _DATALIST_GENRES:
            if g.lower() not in _seen:
                _merged.append(g)
        all_genres = sorted(_merged, key=str.casefold)
    except Exception as _exc:
        logger.debug("all_genres load skipped: %s", _exc)
        all_genres = sorted(_DATALIST_GENRES, key=str.casefold)

    # Load user-assisted sales history imports for this token
    sales_imports: list[Any] = []
    try:
        from mediacat.db.models import DiscogsSalesImport as _DSI

        async with _sf(request)() as _db:
            _imports = (
                (
                    await _db.execute(
                        select(_DSI)
                        .where(_DSI.token_id == token.id)
                        .order_by(_DSI.captured_at.desc())
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
            sales_imports = list(_imports)
    except Exception as _exc:
        logger.debug("sales_imports load skipped: %s", _exc)

    from mediacat.web.discogs_details import running_discogs_import

    collection = collection_view(token.extra)
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
        tracklist_discs=tracklist_discs,
        sales_imports=sales_imports,
        all_genres=all_genres,
        collection=collection,
        runout_groups=mark_matching_variants(
            group_runouts(((token.extra or {}).get("discogs") or {}).get("identifiers")),
            collection["known"],
        ),
        discogs_details_loaded=bool((token.extra or {}).get("discogs")),
        variant_runout_fill=variant_runout_fill,
        discogs_import=await running_discogs_import(request),
    )
    return _tmpl().TemplateResponse(request=request, name="pressing_detail.html", context=ctx)


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Edit
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


@catalogue_router.get("/{token_id}/brief", response_class=HTMLResponse)
async def pressing_brief(request: Request, token_id: str) -> HTMLResponse:
    """Compact pressing overview for the right panel in the 3panel layout."""
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
                    selectinload(Token.media_objects),
                )
                .where(Token.id == tid)
                .where(Token.deleted_at.is_(None))
            )
        ).scalar_one_or_none()

    if token is None:
        raise HTTPException(status_code=404, detail="Not found")

    cover = next(
        (m for m in token.media_objects if m.is_primary_cover),
        next(iter(token.media_objects), None),
    )
    cover_url = f"/catalogue/{token.id}/images/{cover.id}" if cover else None

    ctx = _ctx(request, token=token, cover_url=cover_url, collection=collection_view(token.extra))
    return _tmpl().TemplateResponse(
        request=request, name="partials/pressing_brief.html", context=ctx
    )


@catalogue_router.get("/{token_id}/peek", response_class=HTMLResponse)
async def pressing_peek(request: Request, token_id: str) -> HTMLResponse:
    """Hover-peek fragment for the right panel in the catalogue pressing list."""
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    async with _sf(request)() as db:
        token = (
            await db.execute(
                select(Token)
                .options(
                    selectinload(Token.country),
                    selectinload(Token.media_objects),
                )
                .where(Token.id == tid)
                .where(Token.deleted_at.is_(None))
            )
        ).scalar_one_or_none()

    if token is None:
        raise HTTPException(status_code=404, detail="Not found")

    cover = next(
        (m for m in token.media_objects if m.is_primary_cover),
        next(iter(token.media_objects), None),
    )
    cover_url = f"/catalogue/{token.id}/images/{cover.id}" if cover else None
    tracklist_count = len(token.tracklist) if token.tracklist else 0

    ctx = _ctx(
        request,
        token=token,
        cover_url=cover_url,
        tracklist_count=tracklist_count,
        collection=collection_view(token.extra),
    )
    return _tmpl().TemplateResponse(
        request=request, name="partials/pressing_peek.html", context=ctx
    )


@catalogue_router.get("/{token_id}/edit", response_class=HTMLResponse)
async def token_edit_form(
    request: Request,
    token_id: str,
    back: str = "",
) -> HTMLResponse:
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
        back=back,
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
    country_name: str = Form(""),
    label_name: str = Form(""),
    manufacturer_name: str = Form(""),
    discogs_release_id: str = Form(""),
    musicbrainz_release_id: str = Form(""),
    comment: str = Form(""),
    back: str = Form(""),
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

        cid = await _get_or_create_country(db, country_name)
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
        await db.flush()  # assigns revision.id (a client-side default)
        token.current_revision_id = revision.id
        await db.commit()

    logger.info(
        "Token edited: %s rev=%d by user=%s", token_id, rev_count + 1, request.state.user_id
    )
    # Local paths only: browsers read "//host" and "/\host" as another site.
    back = back.strip()
    local = back.startswith("/") and not back.startswith(("//", "/\\"))
    redirect_to = back if local else f"/catalogue/{token_id}"
    return RedirectResponse(url=redirect_to, status_code=303)


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Archive
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Flag for review
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Delete (soft-delete)
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Refresh images from Discogs
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


@catalogue_router.post("/{token_id}/refresh-discogs", response_model=None)
async def token_refresh_discogs(request: Request, token_id: str) -> RedirectResponse:
    """Fetch and store images from Discogs for an existing pressing."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)

    if not token.discogs_release_id:
        raise HTTPException(status_code=400, detail="No Discogs release ID on this pressing")

    store = getattr(request.app.state, "object_store", None)
    if not store:
        raise HTTPException(
            status_code=503, detail="Object store not configured -- MinIO unavailable"
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

    try:
        async with _sf(request)() as db:
            count = await _import_discogs_images(store, db, token.id, images)
            if count:
                await db.commit()
    except Exception as exc:
        logger.error("Discogs image refresh failed for token=%s: %s", token_id, exc)
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=refresh_error", status_code=303)

    logger.info(
        "Refreshed %d images from Discogs for token=%s by user=%s",
        count,
        token_id,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?refreshed={count}", status_code=303)


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Update image region
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


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

    return RedirectResponse(url=f"/catalogue/{token_id}", status_code=303)


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Image upload
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

_RUNOUT_REGIONS = frozenset(
    {
        "runout_a",
        "runout_b",
        "matrix",
        "label_a",
        "label_b",
        "label_c",
        "label_d",
        "label_e",
        "label_f",
        "label_g",
        "label_h",
    }
)
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
    _require_role(request, "admin", "reviewer", "contributor")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    region = region if region in _VALID_REGIONS else "other"

    # Guard: object store must be available
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        logger.warning("Image upload attempted but object store not configured")
        raise HTTPException(
            status_code=503, detail="Image storage is not configured. Check MinIO settings."
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

        # Same bytes already attached: (token_id, content_hash) is unique.
        if (
            await db.execute(
                select(MediaObject.id).where(
                    MediaObject.token_id == tid, MediaObject.content_hash == stored.content_hash
                )
            )
        ).first():
            return RedirectResponse(url=f"/catalogue/{token_id}?msg=image_exists", status_code=303)

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

    logger.info(
        "Image uploaded for token=%s region=%s object=%s/%s by user=%s",
        token_id,
        region,
        stored.bucket,
        stored.object_key,
        request.state.user_id,
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
                detail="No analysis results for this image yet -- run AI analysis first",
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


# Valid parsed field keys -- guards against arbitrary key injection.
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
                f"Reason: {reason_code}" + (f" -- {reason_text}" if reason_text else "")
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


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Genres
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


_GENRE_MAX_LEN = 100
_GENRE_MAX_COUNT = 30
_GENRE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_genre(raw: str) -> str | None:
    """Strip control characters, collapse whitespace, enforce length cap."""
    cleaned = _GENRE_CTRL_RE.sub("", str(raw))
    cleaned = " ".join(cleaned.split())
    return cleaned[:_GENRE_MAX_LEN] if cleaned else None


def _sanitize_genre_list(raw: list[Any] | None) -> list[str] | None:
    """Sanitise and deduplicate a list of genre strings from any source."""
    if not raw:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        g = _sanitize_genre(item)
        if g and g.lower() not in seen:
            seen.add(g.lower())
            result.append(g)
        if len(result) >= _GENRE_MAX_COUNT:
            break
    return result or None


def _merge_genres(existing: list[Any] | None, incoming: list[Any] | None) -> list[str] | None:
    """Add incoming genres to existing without removing user choices.

    Deduplicates case-insensitively; preserves the order of existing entries.
    If no existing genres yet, incoming becomes the full list.
    """
    incoming = _sanitize_genre_list(incoming)
    if not incoming:
        return existing or None
    if not existing:
        return incoming
    seen = {g.lower() for g in existing}
    merged = list(existing)
    for g in incoming:
        if g.lower() not in seen:
            seen.add(g.lower())
            merged.append(g)
    return merged or None


@catalogue_router.post("/{token_id}/genres", response_model=None)
async def save_genres(
    request: Request,
    token_id: str,
    genres: str = Form(""),
) -> RedirectResponse:
    """Save the genre list for a pressing (comma-separated string from the UI)."""
    _require_role(request, "admin", "reviewer", "contributor")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    raw_list = [g.strip() for g in genres.split(",") if g.strip()]
    genre_list = _sanitize_genre_list(raw_list)

    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")
        token.genres = genre_list
        await db.commit()

    logger.info("Genres saved for token=%s by user=%s", token_id, request.state.user_id)
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=genres_saved", status_code=303)


@catalogue_router.post("/{token_id}/import-genres", response_model=None)
async def import_genres(request: Request, token_id: str) -> RedirectResponse:
    """Fetch genres from Discogs (primary) or MusicBrainz (fallback) and save them."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)
    tid = token.id

    genres: list[str] = []

    if token.discogs_release_id:
        try:
            data = await _fetch_discogs_release(token.discogs_release_id)
            _seen: set[str] = set()
            for g in list(data.get("genres", [])) + list(data.get("styles", [])):
                if g not in _seen:
                    _seen.add(g)
                    genres.append(g)
        except Exception as exc:
            logger.warning("Discogs genre import failed for token=%s: %s", token_id, exc)

    if not genres and token.musicbrainz_release_id:
        try:
            data = await _fetch_mb_release(token.musicbrainz_release_id)
            genres_raw = data.get("genres", []) or data.get("tags", [])
            genres = [
                g.get("name", "").title()
                for g in sorted(genres_raw, key=lambda x: x.get("count", 0), reverse=True)
                if g.get("name")
            ]
        except Exception as exc:
            logger.warning("MusicBrainz genre import failed for token=%s: %s", token_id, exc)

    if not genres:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_genres_found", status_code=303)

    async with _sf(request)() as db:
        t = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404, detail="Not found")
        t.genres = _merge_genres(t.genres, genres)
        await db.commit()

    logger.info("Genres imported for token=%s: %s", token_id, genres)
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=genres_imported", status_code=303)


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Ratings
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


@catalogue_router.post("/{token_id}/rating", response_model=None)
async def save_rating(
    request: Request,
    token_id: str,
    rating: str = Form(""),
) -> RedirectResponse:
    """Save the personal rating (0--10, step 0.5) for a pressing."""
    _require_role(request, "admin", "reviewer", "contributor")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")

    rating_float: float | None = None
    try:
        v = float(rating)
        if 0.0 <= v <= 10.0:
            rating_float = round(v * 2) / 2  # snap to nearest 0.5
    except (ValueError, TypeError):
        pass

    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")
        token.personal_rating = rating_float
        await db.commit()

    logger.info(
        "Rating saved token=%s rating=%s by user=%s", token_id, rating_float, request.state.user_id
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=rating_saved", status_code=303)


@catalogue_router.post("/{token_id}/import-discogs-rating", response_model=None)
async def import_discogs_rating(request: Request, token_id: str) -> RedirectResponse:
    """Fetch the Discogs community rating and save it to the token."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)

    if not token.discogs_release_id:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_discogs_id", status_code=303)

    try:
        data = await _fetch_discogs_release(token.discogs_release_id)
    except Exception as exc:
        logger.warning("Discogs rating import failed for token=%s: %s", token_id, exc)
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=discogs_error", status_code=303)

    community = data.get("community", {})
    rating_obj = community.get("rating", {})
    avg = rating_obj.get("average")
    count = rating_obj.get("count")

    if avg is None:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_discogs_rating", status_code=303)

    async with _sf(request)() as db:
        t = (
            await db.execute(select(Token).where(Token.id == token.id, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404, detail="Not found")
        t.discogs_community_rating = float(avg)
        t.discogs_community_votes = int(count) if count is not None else None
        await db.commit()

    logger.info(
        "Discogs community rating imported token=%s rating=%.2f votes=%s", token_id, avg, count
    )
    return RedirectResponse(
        url=f"/catalogue/{token_id}?msg=discogs_rating_imported", status_code=303
    )


# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# Tracklist import
# â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


@catalogue_router.post("/{token_id}/import-tracks", response_model=None)
async def import_tracks(
    request: Request,
    token_id: str,
    source: str = Form("auto"),
) -> RedirectResponse:
    """Import tracklist from Discogs (primary) or MusicBrainz (fallback).

    source: "discogs" | "musicbrainz" | "auto" (tries Discogs first)
    """
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)
    tid = token.id
    tracklist: list[dict[str, Any]] = []
    mb_url: str | None = None

    use_discogs = source in ("discogs", "auto")
    use_mb = source in ("musicbrainz", "auto")

    if use_discogs and token.discogs_release_id:
        try:
            data = await _fetch_discogs_release(token.discogs_release_id)
            from mediacat.ingestion.discogs import _extract_tracklist as _dtl

            tracklist = _dtl(data.get("tracklist", []))
        except Exception as exc:
            logger.warning("Discogs tracklist import failed for token=%s: %s", token_id, exc)

    # "auto": MusicBrainz only when Discogs gave nothing.
    if use_mb and (not tracklist or source == "musicbrainz") and token.musicbrainz_release_id:
        try:
            mb_data = await _fetch_mb_release(token.musicbrainz_release_id)
            from mediacat.ingestion.musicbrainz import _extract_tracklist as _mbtl

            mb_tracks = _mbtl(mb_data.get("media", []))
            if mb_tracks:
                tracklist = mb_tracks
                mb_url = f"https://musicbrainz.org/release/{token.musicbrainz_release_id}"
        except Exception as exc:
            logger.warning("MusicBrainz tracklist import failed for token=%s: %s", token_id, exc)

    if not tracklist:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_tracks_found", status_code=303)

    async with _sf(request)() as db:
        t = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404, detail="Not found")
        t.tracklist = tracklist
        if mb_url:
            t.musicbrainz_release_url = mb_url
        await db.commit()

    logger.info(
        "Tracklist imported for token=%s tracks=%d by user=%s",
        token_id,
        len(tracklist),
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=tracks_imported", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Market price fetch
# ─────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/{token_id}/fetch-price", response_model=None)
async def fetch_price(request: Request, token_id: str) -> RedirectResponse:
    """Fetch current Discogs market price data and store a time-series snapshot."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)

    if not token.discogs_release_id:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_discogs_id", status_code=303)

    dc = _get_discogs_connector(request)
    rid = str(token.discogs_release_id)
    opened = False
    if not getattr(dc, "_client", None):
        await dc.open()
        opened = True
    try:
        stats = await dc.fetch_marketplace_stats(rid)
        suggestions = await dc.fetch_price_suggestions(rid)
    finally:
        if opened:
            await dc.close()

    currency = stats.get("currency") or suggestions.get("currency")
    condition_prices = dict(suggestions.get("condition_prices") or {})
    # Store median from marketplace stats inside condition_prices JSON
    if stats.get("median_price") is not None:
        condition_prices["_median"] = stats["median_price"]
    # Mark explicitly when API returned no data at all
    if not stats.get("has_data", True) and not condition_prices:
        condition_prices["_no_data"] = True

    try:
        from datetime import UTC
        from datetime import datetime as _dt

        from mediacat.db.models import TokenPriceHistory

        async with _sf(request)() as db:
            snap = TokenPriceHistory(
                token_id=token.id,
                source="discogs_marketplace_stats",
                fetched_at=_dt.now(UTC),
                lowest_price=stats.get("lowest_price"),
                num_for_sale=stats.get("num_for_sale"),
                currency=currency,
                condition_prices=condition_prices or None,
            )
            db.add(snap)
            await db.commit()
    except Exception as exc:
        logger.warning("Price snapshot save failed for token=%s: %s", token_id, exc)
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=price_save_error", status_code=303)

    logger.info(
        "Price snapshot stored token=%s lowest=%s median=%s currency=%s conditions=%s",
        token_id,
        stats.get("lowest_price"),
        stats.get("median_price"),
        currency,
        list(condition_prices.keys()),
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=price_fetched", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Sales history import (user-assisted copy-and-paste)
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_URL_PREFIX = "https://www.discogs.com/sell/history/"


@catalogue_router.post("/{token_id}/import-sales-history", response_class=HTMLResponse)
async def import_sales_history_preview(
    request: Request,
    token_id: str,
    raw_text: str = Form(""),
    source_url: str = Form(""),
    store_raw_text: bool = Form(False),
) -> HTMLResponse:
    """Parse and preview a user-pasted Discogs sales-history table.

    Returns an HTML partial — no database writes.  The user reviews the
    preview and then POSTs to /confirm to store the import.
    """
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)

    from mediacat.ingestion.discogs_sales_parser import (
        PARSER_VERSION,
        apply_outlier_detection,
        parse_sales_text,
    )
    from mediacat.ingestion.valuation_confidence import calculate_confidence, compute_statistics

    source_url_clean = source_url.strip() or None
    source_url_error: str | None = None
    if source_url_clean and not source_url_clean.startswith(_SOURCE_URL_PREFIX):
        source_url_error = f"URL must start with {_SOURCE_URL_PREFIX}"
        source_url_clean = None

    result = parse_sales_text(raw_text, expected_release_id=token.discogs_release_id)
    result = apply_outlier_detection(result)
    stats = compute_statistics(result.valid_rows)
    confidence = calculate_confidence(result.valid_rows)

    ctx = _ctx(
        request,
        token=token,
        result=result,
        stats=stats,
        confidence=confidence,
        raw_text=raw_text,
        source_url=source_url_clean or source_url.strip(),
        source_url_error=source_url_error,
        store_raw_text=store_raw_text,
        parser_version=PARSER_VERSION,
    )
    return _tmpl().TemplateResponse(
        request=request,
        name="partials/sales_history_preview.html",
        context=ctx,
    )


@catalogue_router.post("/{token_id}/import-sales-history/confirm", response_model=None)
async def import_sales_history_confirm(
    request: Request,
    token_id: str,
    raw_text: str = Form(""),
    source_url: str = Form(""),
    store_raw_text: bool = Form(False),
) -> RedirectResponse:
    """Re-parse and store a confirmed Discogs sales-history import."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)

    import uuid as _uuid_mod

    from mediacat.ingestion.discogs_sales_parser import (
        PARSER_VERSION,
        apply_outlier_detection,
        parse_sales_text,
    )
    from mediacat.ingestion.valuation_confidence import calculate_confidence, compute_statistics

    result = parse_sales_text(raw_text, expected_release_id=token.discogs_release_id)
    if result.has_hard_error:
        logger.warning("sales-history confirm rejected: hard error for token=%s", token_id)
        return RedirectResponse(
            url=f"/catalogue/{token_id}?msg=sales_import_parse_error", status_code=303
        )

    result = apply_outlier_detection(result)
    stats = compute_statistics(result.valid_rows)
    confidence = calculate_confidence(result.valid_rows)

    source_url_clean = source_url.strip() or None
    if source_url_clean and not source_url_clean.startswith(_SOURCE_URL_PREFIX):
        source_url_clean = None

    non_outlier_rows = [r for r in result.valid_rows if not r.is_outlier]
    non_outlier_dates = [r.sold_date for r in non_outlier_rows]
    newest_date = max(non_outlier_dates) if non_outlier_dates else None
    oldest_date = min(non_outlier_dates) if non_outlier_dates else None
    currency = result.valid_rows[0].currency if result.valid_rows else None

    rows_json = [
        {
            "sold_date": r.sold_date.isoformat(),
            "sold_price": str(r.sold_price),
            "currency": r.currency,
            "media_condition": r.media_condition,
            "sleeve_condition": r.sleeve_condition,
            "is_outlier": r.is_outlier,
        }
        for r in result.valid_rows
    ]
    diagnostics = {
        "parser_version": PARSER_VERSION,
        "rows_found": len(result.valid_rows) + len(result.discarded_rows),
        "rows_valid": len(result.valid_rows),
        "rows_discarded": len(result.discarded_rows),
        "outlier_count": result.outlier_count,
        "discard_reasons": [
            {"row_index": d.row_index, "reason": d.reason} for d in result.discarded_rows
        ],
    }

    try:
        imported_by = _uuid_mod.UUID(str(request.state.user_id))
    except (ValueError, AttributeError):
        imported_by = None

    from datetime import UTC
    from datetime import datetime as _dt

    from mediacat.db.models import DiscogsSalesImport as _DSI

    async with _sf(request)() as db:
        db.add(
            _DSI(
                token_id=token.id,
                imported_by=imported_by,
                captured_at=_dt.now(UTC),
                discogs_release_id=token.discogs_release_id,
                source_url=source_url_clean,
                raw_import_text=raw_text if store_raw_text else None,
                raw_storage_consented=store_raw_text,
                parser_diagnostics=diagnostics,
                parsed_sales_rows=rows_json,
                row_count=stats["row_count"],
                low_sold_price=stats["low"],
                median_sold_price=stats["median"],
                high_sold_price=stats["high"],
                newest_sold_date=newest_date,
                oldest_sold_date=oldest_date,
                currency=currency,
                outlier_count=result.outlier_count or None,
                source_confidence=confidence.source_confidence,
                valuation_confidence=confidence.valuation_confidence,
                valuation_confidence_notes=confidence.notes,
            )
        )
        await db.commit()

    logger.info(
        "sales-history import stored token=%s rows=%d valuation=%s by=%s",
        token_id,
        stats["row_count"],
        confidence.valuation_confidence,
        request.state.user_id,
    )
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=sales_imported", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Sync all from Discogs (confirmation + execution)
# ─────────────────────────────────────────────────────────────────────────────


@catalogue_router.get("/{token_id}/sync-discogs", response_class=HTMLResponse)
async def sync_discogs_confirm(request: Request, token_id: str) -> HTMLResponse:
    """Show a confirmation page before performing a full Discogs sync."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)

    price_count = 0
    try:
        from mediacat.db.models import TokenPriceHistory

        async with _sf(request)() as db:
            from sqlalchemy import func as _func

            price_count = (
                await db.execute(
                    select(_func.count())
                    .select_from(TokenPriceHistory)
                    .where(TokenPriceHistory.token_id == token.id)
                )
            ).scalar_one() or 0
    except Exception as exc:
        logger.debug("price history count skipped: %s", exc)

    context = _ctx(request, token=token, price_count=price_count)
    return _tmpl().TemplateResponse(request=request, name="sync_confirm.html", context=context)


@catalogue_router.post("/{token_id}/sync-discogs", response_model=None)
async def sync_discogs_execute(request: Request, token_id: str) -> RedirectResponse:
    """Execute a full sync: genres, rating, tracklist, images, price snapshot."""
    _require_role(request, "admin", "reviewer", "contributor")
    token = await _load_token(request, token_id)

    if not token.discogs_release_id:
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=no_discogs_id", status_code=303)

    errors: list[str] = []

    try:
        data = await _fetch_discogs_release(token.discogs_release_id)
    except Exception as exc:
        logger.warning("sync-discogs fetch failed for token=%s: %s", token_id, exc)
        return RedirectResponse(url=f"/catalogue/{token_id}?msg=discogs_error", status_code=303)

    tid = token.id
    async with _sf(request)() as db:
        t = (
            await db.execute(select(Token).where(Token.id == tid, Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404)

        # Genres — merge with existing user choices, never overwrite
        try:
            raw_genres = list(data.get("genres", [])) + list(data.get("styles", []))
            t.genres = _merge_genres(t.genres, raw_genres)
        except Exception as exc:
            errors.append(f"genres: {exc}")

        # Country
        try:
            country_str = (data.get("country") or "").strip()
            if country_str and not t.country_id:
                t.country_id = await _get_or_create_country(db, country_str)
        except Exception as exc:
            errors.append(f"country: {exc}")

        # Community rating
        try:
            community = data.get("community", {})
            rating_obj = community.get("rating", {})
            avg = rating_obj.get("average")
            votes = rating_obj.get("count")
            if avg is not None:
                t.discogs_community_rating = float(avg)
                t.discogs_community_votes = int(votes) if votes is not None else None
        except Exception as exc:
            errors.append(f"rating: {exc}")

        # Tracklist
        try:
            from mediacat.ingestion.discogs import _extract_tracklist as _dtl

            tracklist = _dtl(data.get("tracklist", []))
            if tracklist:
                t.tracklist = tracklist
        except Exception as exc:
            errors.append(f"tracklist: {exc}")

        await db.commit()

    # Images (separate session to avoid long transaction)
    store = getattr(request.app.state, "object_store", None)
    if store and data.get("images"):
        try:
            async with _sf(request)() as db:
                count = await _import_discogs_images(store, db, tid, data["images"])
                if count:
                    await db.commit()
        except Exception as exc:
            errors.append(f"images: {exc}")

    # Price snapshot
    try:
        dc = _get_discogs_connector(request)
        rid = str(token.discogs_release_id)
        _dc_opened = False
        if not getattr(dc, "_client", None):
            await dc.open()
            _dc_opened = True
        try:
            stats = await dc.fetch_marketplace_stats(rid)
            suggestions = await dc.fetch_price_suggestions(rid)
        finally:
            if _dc_opened:
                await dc.close()
        currency = stats.get("currency") or suggestions.get("currency")
        _cond = dict(suggestions.get("condition_prices") or {})
        if stats.get("median_price") is not None:
            _cond["_median"] = stats["median_price"]
        from datetime import UTC
        from datetime import datetime as _dt

        from mediacat.db.models import TokenPriceHistory

        async with _sf(request)() as db:
            db.add(
                TokenPriceHistory(
                    token_id=tid,
                    source="discogs_marketplace_stats",
                    fetched_at=_dt.now(UTC),
                    lowest_price=stats.get("lowest_price"),
                    num_for_sale=stats.get("num_for_sale"),
                    currency=currency,
                    condition_prices=_cond or None,
                )
            )
            await db.commit()
    except Exception as exc:
        errors.append(f"price: {exc}")

    # Runout identifiers + collection custom fields (stored in Token.extra)
    try:
        from mediacat.web.discogs_details import refresh_token_discogs_details

        errors.extend(await refresh_token_discogs_details(request, token, release=data))
    except Exception as exc:
        errors.append(f"discogs details: {exc}")

    if errors:
        logger.warning("sync-discogs partial errors token=%s: %s", token_id, errors)

    logger.info("sync-discogs complete token=%s by=%s", token_id, request.state.user_id)
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=sync_complete", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Vinyl material
# ─────────────────────────────────────────────────────────────────────────────


@catalogue_router.post("/{token_id}/vinyl-material", response_model=None)
async def save_vinyl_material(
    request: Request,
    token_id: str,
    vinyl_material: str = Form(""),
) -> RedirectResponse:
    """Save the vinyl material type for a pressing."""
    _require_role(request, "admin", "reviewer", "contributor")
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")
    async with _sf(request)() as db:
        token = (
            await db.execute(select(Token).where(Token.id == tid).where(Token.deleted_at.is_(None)))
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail="Not found")
        token.vinyl_material = vinyl_material.strip() or None
        await db.commit()
    return RedirectResponse(url=f"/catalogue/{token_id}?msg=vinyl_material_saved", status_code=303)
