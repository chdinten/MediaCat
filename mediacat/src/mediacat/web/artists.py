"""Artists tab routes.

URL structure
-------------
GET  /artists                          artist grid (all known artists in DB)
GET  /artists/search                   HTMX partial: live search
GET  /artists/{artist}/info            HTMX partial: Wikipedia summary + image
GET  /artists/{artist}/discography     HTMX partial: discography list (Discogs → MusicBrainz → Wikipedia)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import ColumnElement, func, select, update

from mediacat.db.enums import TokenStatus
from mediacat.db.models import ArtistBio, MediaObject, SiteSetting, Token
from mediacat.ingestion.artist_bio import BioResult, resolve_artist_bio
from mediacat.web.routes import _ctx, _tmpl

logger = logging.getLogger(__name__)

artists_router = APIRouter(prefix="/artists", tags=["artists"])

_DISCOGS_BASE = "https://api.discogs.com"
_MB_BASE = "https://musicbrainz.org/ws/2"
_USER_AGENT = "MediaCat/1.0 (artists browser)"


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────


def _sf(request: Request) -> Any:
    """The app's async_sessionmaker (set in the lifespan)."""
    return request.app.state.db_session_factory


# ──────────────────────────────────────────────────────────────────────────────
# Artist grid
# ──────────────────────────────────────────────────────────────────────────────


@artists_router.get("", response_class=HTMLResponse)
async def artists_index(
    request: Request,
    q: str = Query(""),
    sort: str = Query("name"),
    artist: str = Query(""),
) -> HTMLResponse:
    """Show a grid of all artists in the catalogue; ``artist`` opens that one (default: the first)."""
    async with _sf(request)() as db:
        # Collect all distinct artists with stats
        base = (
            select(
                Token.artist,
                func.count(Token.id).label("cnt"),
                func.min(Token.year).label("yr_min"),
                func.max(Token.year).label("yr_max"),
                func.max(Token.personal_rating).label("max_rating"),
                func.max(Token.discogs_community_rating).label("max_discogs_rating"),
            )
            .where(Token.deleted_at.is_(None))
            .where(Token.status != TokenStatus.MERGED)
            .where(Token.artist.isnot(None))
        )
        if q.strip():
            base = base.where(Token.artist.ilike(f"%{q.strip()}%"))

        order: ColumnElement[Any]
        if sort == "count":
            order = func.count(Token.id).desc()
        elif sort == "rating":
            order = func.max(Token.personal_rating).desc().nulls_last()
        elif sort == "discogs_rating":
            order = func.max(Token.discogs_community_rating).desc().nulls_last()
        else:
            order = Token.artist.asc()

        rows = (await db.execute(base.group_by(Token.artist).order_by(order))).all()

        # For each artist, try to find a primary cover image from their tokens
        artist_names = [r.artist for r in rows]
        cover_by_artist: dict[str, str] = {}
        portrait_by_artist: dict[str, str] = {}

        if artist_names:
            # Fetch first cover for each artist in one query
            cover_rows = (
                await db.execute(
                    select(Token.artist, MediaObject.id, MediaObject.token_id)
                    .join(MediaObject, MediaObject.token_id == Token.id)
                    .where(Token.deleted_at.is_(None))
                    .where(Token.status != TokenStatus.MERGED)
                    .where(Token.artist.in_(artist_names))
                    # Primary cover first, then any image — so we get a fallback
                    .order_by(
                        Token.artist.asc(),
                        MediaObject.is_primary_cover.desc().nulls_last(),
                        Token.year.asc().nulls_last(),
                    )
                )
            ).all()
            for row in cover_rows:
                if row.artist not in cover_by_artist:
                    cover_by_artist[row.artist] = f"{row.token_id}|{row.id}"

            # Fetch cached artist portraits from Wikipedia bios
            bio_rows = (
                await db.execute(
                    select(ArtistBio.artist_name, ArtistBio.thumbnail_url)
                    .where(ArtistBio.artist_name.in_(artist_names))
                    .where(ArtistBio.thumbnail_url.isnot(None))
                )
            ).all()
            portrait_by_artist = {r.artist_name: r.thumbnail_url for r in bio_rows}

    artists = [
        {
            "name": r.artist,
            "pressing_count": r.cnt,
            "year_range": (
                str(r.yr_min) + (f"–{r.yr_max}" if r.yr_max != r.yr_min else "") if r.yr_min else ""
            ),
            "enc": urllib.parse.quote(r.artist or "", safe=""),
            "portrait_url": portrait_by_artist.get(r.artist),
            "cover_url": (
                "/catalogue/{}/images/{}".format(*cover_by_artist[r.artist].split("|"))
                if r.artist in cover_by_artist
                else None
            ),
            "max_rating": r.max_rating,
            "max_discogs_rating": r.max_discogs_rating,
        }
        for r in rows
    ]

    open_artist = next((a for a in artists if a["name"] == artist), artists[0] if artists else None)
    ctx = _ctx(request, artists=artists, query=q, sort=sort, open_artist=open_artist)
    return _tmpl().TemplateResponse(request=request, name="artists.html", context=ctx)


@artists_router.get("/search", response_class=HTMLResponse)
async def artists_search(
    request: Request,
    q: str = Query(""),
) -> HTMLResponse:
    """HTMX partial: live artist search within the grid page."""
    return await artists_index(request, q=q, sort="name")


@artists_router.get("/{artist}/detail", response_class=HTMLResponse)
async def artist_detail(request: Request, artist: str) -> HTMLResponse:
    """HTMX partial: full artist detail panel (albums + Wikipedia + discography)."""
    name = urllib.parse.unquote(artist)

    async with _sf(request)() as db:
        rows = (
            await db.execute(
                select(Token.title, Token.year, Token.id)
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
                .where(Token.artist == name)
                .order_by(Token.year.asc().nulls_last(), Token.title.asc())
            )
        ).all()

        token_ids = [r.id for r in rows]
        covers: dict[str, str] = {}
        if token_ids:
            cover_rows = (
                await db.execute(
                    select(MediaObject.token_id, MediaObject.id)
                    .where(MediaObject.token_id.in_(token_ids))
                    # Primary cover first, then any image as fallback
                    .order_by(
                        MediaObject.is_primary_cover.desc().nulls_last(),
                        MediaObject.id.asc(),
                    )
                )
            ).all()
            for cr in cover_rows:
                if str(cr.token_id) not in covers:
                    covers[str(cr.token_id)] = str(cr.id)

    albums = [
        {
            "title": r.title,
            "year": r.year,
            "id": str(r.id),
            "cover_image_id": covers.get(str(r.id)),
        }
        for r in rows
    ]

    ctx = _ctx(
        request,
        artist=name,
        enc_artist=urllib.parse.quote(name, safe=""),
        albums=albums,
    )
    return _tmpl().TemplateResponse(
        request=request, name="partials/artist_detail.html", context=ctx
    )


# ──────────────────────────────────────────────────────────────────────────────
# Artist detail partials (HTMX)
# ──────────────────────────────────────────────────────────────────────────────


@artists_router.get("/{artist}/albums", response_class=HTMLResponse)
async def artist_albums(request: Request, artist: str) -> HTMLResponse:
    """HTMX partial: albums stored in this DB for a given artist."""
    name = urllib.parse.unquote(artist)

    async with _sf(request)() as db:
        # Group stored tokens into album titles
        rows = (
            await db.execute(
                select(
                    Token.title,
                    Token.year,
                    Token.id,
                    func.count(Token.id).label("pressing_count"),
                )
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
                .where(Token.artist == name)
                .group_by(Token.title, Token.year, Token.id)
                .order_by(Token.year.asc().nulls_last(), Token.title.asc())
            )
        ).all()

        # Get cover images
        token_ids = [r.id for r in rows]
        covers: dict[str, str] = {}
        if token_ids:
            cover_rows = (
                await db.execute(
                    select(MediaObject.token_id, MediaObject.id)
                    .where(MediaObject.token_id.in_(token_ids))
                    .where(MediaObject.is_primary_cover.is_(True))
                )
            ).all()
            for cr in cover_rows:
                covers[str(cr.token_id)] = str(cr.id)

    albums = [
        {
            "title": r.title,
            "year": r.year,
            "id": str(r.id),
            "cover_image_id": covers.get(str(r.id)),
        }
        for r in rows
    ]

    ctx = _ctx(request, artist=name, enc_artist=urllib.parse.quote(name, safe=""), albums=albums)
    return _tmpl().TemplateResponse(
        request=request, name="partials/artist_albums.html", context=ctx
    )


# Biography lookups in progress, by artist name.  A lookup is a task of its
# own that also saves the result, so it finishes even when the browser leaves
# the page; requests only wait on it (shielded), and a second request for the
# same artist joins the running lookup instead of starting another.
_bio_jobs: dict[str, asyncio.Task[BioResult]] = {}


async def _lookup_and_cache_bio(
    sf: Any, name: str, llm: Any, discogs_token: str | None
) -> BioResult:
    bio = await resolve_artist_bio(name, llm=llm, discogs_token=discogs_token)
    logger.info("Artist bio %r via %s (%d chars)", name, bio.source, len(bio.extract))
    # Cache the outcome, including "not found", so the page explains the gap
    # until the user asks for a refresh.
    try:
        async with sf() as db:
            row = (
                await db.execute(select(ArtistBio).where(ArtistBio.artist_name == name))
            ).scalar_one_or_none()
            if row is None:
                row = ArtistBio(artist_name=name)
                db.add(row)
            row.extract = bio.extract
            row.description = bio.description or None
            row.wiki_url = bio.url or None
            row.thumbnail_url = bio.thumbnail_url
            row.bio_source = bio.source
            row.fetched_at = datetime.now(UTC)
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to cache bio for %r: %s", name, exc)
    return bio


def _bio_job_done(name: str, task: asyncio.Task[BioResult]) -> None:
    if _bio_jobs.get(name) is task:
        del _bio_jobs[name]
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Bio lookup for %r failed: %s", name, task.exception())


async def _start_bio_job(request: Request, name: str) -> asyncio.Task[BioResult]:
    from mediacat.web.admin import _resolve_discogs_token

    token = await _resolve_discogs_token(request) or None
    task = asyncio.create_task(
        _lookup_and_cache_bio(_sf(request), name, getattr(request.app.state, "llm", None), token),
        name=f"artist-bio:{name[:40]}",
    )
    _bio_jobs[name] = task
    task.add_done_callback(lambda t: _bio_job_done(name, t))
    return task


@artists_router.get("/{artist}/info", response_class=HTMLResponse)
async def artist_info(
    request: Request,
    artist: str,
    refresh: bool = Query(default=False),
) -> HTMLResponse:
    """HTMX partial: artist biography.

    Serves the cached DB entry on normal load; looks it up again when
    refresh=true (see :mod:`mediacat.ingestion.artist_bio` for the order).
    While a lookup for the artist is running, every request waits for it.
    """
    name = urllib.parse.unquote(artist)
    running = _bio_jobs.get(name)

    # ── Serve from DB cache unless caller requested a refresh ────────────────
    if not refresh and running is None:
        async with _sf(request)() as db:
            row = (
                await db.execute(select(ArtistBio).where(ArtistBio.artist_name == name))
            ).scalar_one_or_none()
        if row and row.extract:
            summary: dict[str, Any] = {
                "extract": row.extract,
                "description": row.description,
                "url": row.wiki_url,
                "thumbnail_url": row.thumbnail_url,
            }
            # Backfill image for cached bios that predate the pageimages fallback
            if not summary["thumbnail_url"] and row.wiki_url:
                try:
                    from mediacat.ingestion.wikipedia import WikipediaConnector

                    _raw_title = row.wiki_url.split("/wiki/", 1)[-1]
                    _raw_title = _raw_title.split("#")[0].split("?")[0]
                    _wiki_title = urllib.parse.unquote(_raw_title.replace("_", " ")).strip()
                    if _wiki_title and _wiki_title != row.wiki_url:
                        _img = await WikipediaConnector().fetch_artist_image_url(_wiki_title)
                        if _img:
                            summary["thumbnail_url"] = _img
                            async with _sf(request)() as db:
                                await db.execute(
                                    update(ArtistBio)
                                    .where(ArtistBio.artist_name == name)
                                    .values(thumbnail_url=_img)
                                )
                                await db.commit()
                            logger.info("Backfilled image for cached bio %r", name)
                except Exception as exc:
                    logger.warning("Image backfill failed for %r: %s", name, exc)
            fallback_cover = (
                None if summary.get("thumbnail_url") else await _first_cover_url(request, name)
            )
            ctx = _ctx(
                request,
                artist=name,
                summary=summary,
                bio_source=row.bio_source,
                fallback_cover_url=fallback_cover,
            )
            return _tmpl().TemplateResponse(
                request=request, name="partials/artist_info.html", context=ctx
            )

    # ── Look the bio up (fresh, cache miss, or join a running lookup) ────────
    task = running or await _start_bio_job(request, name)
    # shield: if the browser goes away, this request is cancelled but the
    # lookup carries on and stores its result.
    bio = await asyncio.shield(task)
    summary = {
        "extract": bio.extract,
        "description": bio.description,
        "url": bio.url,
        "thumbnail_url": bio.thumbnail_url,
    }

    fallback_cover = None if bio.thumbnail_url else await _first_cover_url(request, name)
    ctx = _ctx(
        request,
        artist=name,
        summary=summary,
        bio_source=bio.source,
        fallback_cover_url=fallback_cover,
    )
    return _tmpl().TemplateResponse(request=request, name="partials/artist_info.html", context=ctx)


@artists_router.get("/{artist}/discography", response_class=HTMLResponse)
async def artist_discography(
    request: Request,
    artist: str,
    refresh: bool = Query(default=False),
) -> HTMLResponse:
    """HTMX partial: discography list from MusicBrainz → Wikipedia, enriched with Spotify URLs.

    Cached per artist; re-fetched at most once per calendar day unless
    refresh=true is passed (by the reload button).
    """
    name = urllib.parse.unquote(artist)

    # ── Serve from cache if fetched today ───────────────────────────────────
    if not refresh:
        async with _sf(request)() as db:
            row = (
                await db.execute(select(ArtistBio).where(ArtistBio.artist_name == name))
            ).scalar_one_or_none()
        if row and row.disco_data and row.disco_fetched_at:
            today = datetime.now(UTC).date()
            if row.disco_fetched_at.date() >= today:
                discography = _annotate_in_db(row.disco_data, await _in_db_titles(request, name))
                ctx = _ctx(
                    request,
                    artist=name,
                    discography=discography,
                    source_used=row.disco_source or "",
                )
                return _tmpl().TemplateResponse(
                    request=request, name="partials/artist_discography.html", context=ctx
                )

    # ── Fresh fetch: MusicBrainz primary, Wikipedia fallback ────────────────
    discography = []
    source_used: str = ""

    try:
        discography, source_used = await _mb_discography(name)
    except Exception as exc:
        logger.warning("MusicBrainz discography failed for %r: %s", name, exc)

    if not discography:
        try:
            discography, source_used = await _wikipedia_discography(name)
        except Exception as exc:
            logger.warning("Wikipedia discography fallback failed for %r: %s", name, exc)

    # ── Enrich with Spotify URLs ─────────────────────────────────────────────
    if discography:
        credentials = await _read_spotify_credentials(request)
        if credentials:
            try:
                discography, spotify_matched = await _spotify_enrich(discography, name, credentials)
                spotify_note = (
                    f"Spotify ({spotify_matched})" if spotify_matched else "Spotify (no match)"
                )
            except Exception as exc:
                logger.warning("Spotify enrichment failed for %r: %s", name, exc)
                spotify_note = "Spotify (error)"
        else:
            spotify_note = "Spotify (not configured)"
        source_used = f"{source_used} · {spotify_note}" if source_used else spotify_note

    # ── Persist cache ────────────────────────────────────────────────────────
    if discography:
        try:
            async with _sf(request)() as db:
                row = (
                    await db.execute(select(ArtistBio).where(ArtistBio.artist_name == name))
                ).scalar_one_or_none()
                if row is None:
                    row = ArtistBio(artist_name=name)
                    db.add(row)
                row.disco_data = discography
                row.disco_source = source_used
                row.disco_fetched_at = datetime.now(UTC)
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to cache discography for %r: %s", name, exc)

        # Backfill tokens.spotify_url for owned pressings that match
        try:
            await _backfill_token_spotify_urls(request, name, discography)
        except Exception as exc:
            logger.warning("Spotify token backfill failed for %r: %s", name, exc)

    discography = _annotate_in_db(discography, await _in_db_titles(request, name))

    ctx = _ctx(
        request,
        artist=name,
        discography=discography,
        source_used=source_used,
    )
    return _tmpl().TemplateResponse(
        request=request, name="partials/artist_discography.html", context=ctx
    )


# ──────────────────────────────────────────────────────────────────────────────
# Query-param mirrors — safe for artist names containing '/' (e.g. HUNTR/X).
# Uvicorn decodes %2F in path segments before routing, breaking path-param
# routes. Query params are decoded correctly by FastAPI.
# ──────────────────────────────────────────────────────────────────────────────


@artists_router.get("/detail", response_class=HTMLResponse)
async def artist_detail_by_query(request: Request, artist: str = Query("")) -> HTMLResponse:
    return await artist_detail(request, artist)


@artists_router.get("/albums", response_class=HTMLResponse)
async def artist_albums_by_query(request: Request, artist: str = Query("")) -> HTMLResponse:
    return await artist_albums(request, artist)


@artists_router.get("/info", response_class=HTMLResponse)
async def artist_info_by_query(
    request: Request,
    artist: str = Query(""),
    refresh: bool = Query(default=False),
) -> HTMLResponse:
    return await artist_info(request, artist, refresh)


@artists_router.get("/discography", response_class=HTMLResponse)
async def artist_discography_by_query(
    request: Request,
    artist: str = Query(""),
    refresh: bool = Query(default=False),
) -> HTMLResponse:
    return await artist_discography(request, artist, refresh)


# ──────────────────────────────────────────────────────────────────────────────
# Data fetchers for discography
# ──────────────────────────────────────────────────────────────────────────────


async def _first_cover_url(request: Request, artist_name: str) -> str | None:
    """Return a /catalogue/.../images/... URL for the first cover of an artist, or None."""
    async with _sf(request)() as db:
        row = (
            await db.execute(
                select(MediaObject.id, MediaObject.token_id)
                .join(Token, Token.id == MediaObject.token_id)
                .where(Token.deleted_at.is_(None))
                .where(Token.status != TokenStatus.MERGED)
                .where(Token.artist == artist_name)
                .order_by(
                    MediaObject.is_primary_cover.desc().nulls_last(),
                    Token.year.asc().nulls_last(),
                    MediaObject.id.asc(),
                )
                .limit(1)
            )
        ).first()
    if row:
        return f"/catalogue/{row.token_id}/images/{row.id}"
    return None


def _discogs_token() -> str | None:
    """Read the Discogs token from the Docker secret file, if present."""
    import pathlib

    for path in ("/run/secrets/discogs_token", "/run/secrets/discogs_token.txt"):
        p = pathlib.Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or None
    return None


async def _in_db_titles(request: Request, artist_name: str) -> set[str]:
    """Lower-cased titles of the artist's live pressings (Discogs and MusicBrainz differ in case)."""
    async with _sf(request)() as db:
        return set(
            (
                await db.execute(
                    select(func.lower(Token.title))
                    .where(Token.deleted_at.is_(None))
                    .where(Token.status != TokenStatus.MERGED)
                    .where(Token.artist == artist_name)
                    .where(Token.title.isnot(None))
                )
            )
            .scalars()
            .all()
        )


def _annotate_in_db(discography: list[dict[str, Any]], in_db: set[str]) -> list[dict[str, Any]]:
    for album in discography:
        album["in_db"] = (album.get("title") or "").lower() in in_db
    return discography


async def _discogs_discography(
    name: str, token: str | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Search Discogs for the artist then fetch their master releases."""
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        # Search for the artist
        r = await client.get(
            f"{_DISCOGS_BASE}/database/search",
            params={"q": name, "type": "artist", "per_page": 5},
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return [], ""

        artist_id = results[0].get("id")
        if not artist_id:
            return [], ""

        # Fetch their releases (masters only)
        r2 = await client.get(
            f"{_DISCOGS_BASE}/artists/{artist_id}/releases",
            params={"sort": "year", "sort_order": "asc", "per_page": 100, "type": "master"},
        )
        r2.raise_for_status()
        releases = r2.json().get("releases", [])

    albums = []
    seen: set[str] = set()
    for rel in releases:
        if rel.get("role", "").lower() not in ("main", ""):
            continue
        title = rel.get("title", "").strip()
        year = rel.get("year")
        master_id = rel.get("id")
        if not title or title in seen:
            continue
        seen.add(title)
        albums.append(
            {
                "title": title,
                "year": year,
                "type": rel.get("type", ""),
                "source": "discogs",
                "discogs_master_id": master_id,
                "url": f"https://www.discogs.com/master/{master_id}" if master_id else None,
            }
        )

    return albums, "Discogs"


async def _discogs_artist_image(name: str, token: str | None = None) -> str | None:
    """Return a representative image URL for an artist from Discogs, or None."""
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            r = await client.get(
                f"{_DISCOGS_BASE}/database/search",
                params={"q": name, "type": "artist", "per_page": 3},
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            if not results:
                return None
            artist_id = results[0].get("id")
            if not artist_id:
                return None
            r2 = await client.get(f"{_DISCOGS_BASE}/artists/{artist_id}")
            r2.raise_for_status()
            images = r2.json().get("images", [])
            if not images:
                return None
            primary = next(
                (img for img in images if img.get("type") == "primary"),
                images[0],
            )
            url = primary.get("uri150") or primary.get("uri")
            return str(url) if url else None
        except Exception as exc:
            logger.warning("_discogs_artist_image failed for %r: %s", name, exc)
            return None


async def _mb_discography(name: str) -> tuple[list[dict[str, Any]], str]:
    """Search MusicBrainz for the artist then fetch their release groups."""
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _USER_AGENT}) as client:
        r = await client.get(
            f"{_MB_BASE}/artist",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": 5},
        )
        r.raise_for_status()
        artists = r.json().get("artists", [])
        if not artists:
            return [], ""

        mbid = artists[0].get("id", "")
        if not mbid:
            return [], ""

        r2 = await client.get(
            f"{_MB_BASE}/artist/{mbid}",
            params={"inc": "release-groups", "fmt": "json"},
        )
        r2.raise_for_status()
        groups = r2.json().get("release-groups", [])

    albums = []
    seen: set[str] = set()
    for rg in sorted(groups, key=lambda x: x.get("first-release-date") or "9999"):
        rg_type = (rg.get("primary-type") or "").lower()
        if rg_type not in ("album", "ep", "single", ""):
            continue
        title = rg.get("title", "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        date = rg.get("first-release-date", "")
        year: int | None = None
        with contextlib.suppress(ValueError):
            year = int(date[:4]) if date else None
        rg_id = rg.get("id", "")
        albums.append(
            {
                "title": title,
                "year": year,
                "type": rg.get("primary-type", ""),
                "source": "musicbrainz",
                "mbid": rg_id,
                "url": f"https://musicbrainz.org/release-group/{rg_id}" if rg_id else None,
            }
        )

    return albums, "MusicBrainz"


async def _wikipedia_discography(name: str) -> tuple[list[dict[str, Any]], str]:
    """Parse Wikipedia discography for the artist."""
    from mediacat.ingestion.wikipedia import WikipediaConnector

    wiki = WikipediaConnector()
    candidates = await wiki.search_artist(name)
    if not candidates:
        return [], ""

    from mediacat.ingestion.artist_bio import is_meta_title, title_matches_name

    # Only a page named after the artist; a list or song page has no discography
    best = next(
        (
            c
            for c in candidates
            if title_matches_name(c["title"], name) and not is_meta_title(c["title"])
        ),
        None,
    )
    if best is None:
        return [], ""
    albums = await wiki.fetch_discography(best["title"])
    for a in albums:
        a["url"] = (
            f"https://en.wikipedia.org/wiki/{urllib.parse.quote(a['title'].replace(' ', '_'))}"
        )
    return albums, "Wikipedia"


async def _read_spotify_credentials(request: Request) -> tuple[str, str] | None:
    """Return (client_id, client_secret) from SiteSetting, or None if not configured."""
    async with _sf(request)() as db:
        rows = (
            (
                await db.execute(
                    select(SiteSetting).where(
                        SiteSetting.key.in_(["spotify_client_id", "spotify_client_secret"])
                    )
                )
            )
            .scalars()
            .all()
        )
    settings = {row.key: row.value for row in rows}
    client_id = settings.get("spotify_client_id", "").strip()
    client_secret = settings.get("spotify_client_secret", "").strip()
    if client_id and client_secret:
        return client_id, client_secret
    return None


async def _spotify_enrich(
    discography: list[dict[str, Any]],
    artist_name: str,
    credentials: tuple[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """Attach spotify_url to each discography entry where a Spotify match exists.

    Returns (enriched_discography, matched_count).
    Raises on auth / network errors so the caller can capture them with full context.
    """
    from mediacat.ingestion.spotify import SpotifyClient

    client_id, client_secret = credentials
    client = SpotifyClient(client_id, client_secret)

    artist, _ = await client.resolve_artist(artist_name)
    if not artist:
        logger.info("Spotify: artist %r not found", artist_name)
        return discography, 0

    spotify_albums = await client.fetch_artist_albums(artist["id"])
    url_map = {
        a["title"].casefold(): a["spotify_url"] for a in spotify_albums if a.get("spotify_url")
    }

    matched = 0
    for album in discography:
        title = album.get("title", "")
        spotify_url = url_map.get(title.casefold())
        if spotify_url:
            album["spotify_url"] = spotify_url
            matched += 1

    logger.info(
        "Spotify: enriched %d/%d entries for %r (Spotify returned %d albums)",
        matched,
        len(discography),
        artist_name,
        len(spotify_albums),
    )
    return discography, matched


async def _backfill_token_spotify_urls(
    request: Request,
    artist_name: str,
    discography: list[dict[str, Any]],
) -> None:
    """Set tokens.spotify_url for owned pressings that match enriched album titles."""
    url_by_title = {
        a["title"]: a["spotify_url"] for a in discography if a.get("spotify_url") and a.get("title")
    }
    if not url_by_title:
        return

    async with _sf(request)() as db:
        for title, spotify_url in url_by_title.items():
            await db.execute(
                update(Token)
                .where(Token.artist == artist_name)
                .where(func.lower(Token.title) == title.lower())
                .where(Token.spotify_url.is_(None))
                .values(spotify_url=spotify_url)
            )
        await db.commit()

    logger.info(
        "Spotify: backfilled tokens.spotify_url for %d album(s) of %r",
        len(url_by_title),
        artist_name,
    )
