"""Streaming-service links for albums (Spotify, Deezer, Apple Music).

URL structure
-------------
GET  /streaming/icons?artist=&album=      HTMX partial: service icons of one album (lazy)
GET  /streaming/edit?artist=&album=&back= "Edit links" page (any logged-in user)
POST /streaming/edit                      save manual links / "not on this service"
POST /admin/streaming/settings            services on/off, storefront country (admin)
POST /admin/streaming/match               start the "Match all albums" job (admin)
POST /admin/streaming/match/stop          stop it (admin)
GET  /admin/streaming/match/status        HTMX partial: job progress (admin)

Album pages render an empty slot (``partials/streaming_slot.html``) that
loads ``/streaming/icons`` when it scrolls into view, so lookups never block
a page.  A lookup is a task of its own that saves its result (it finishes if
the browser leaves); the icons request waits for it a few seconds and
otherwise answers "still looking", which polls again.

Which icons show: services switched on in Admin → Streaming whose API needs
no account (Deezer, Apple Music), plus those that need one (Spotify) once
their credentials are in Admin → API Tokens — and only for albums that
service has.  Results live in ``streaming_links``; ``tokens.spotify_url`` is
kept in step with the Spotify row.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from mediacat.db.enums import TokenStatus
from mediacat.db.models import SiteSetting, StreamingLink, Token, User
from mediacat.ingestion.streaming import (
    DEFAULT_COUNTRY,
    FINDERS,
    NEEDS_CREDENTIALS,
    SERVICE_LABELS,
    SERVICES,
    AlbumQuery,
    Match,
    album_key,
    artist_key,
    collect_barcodes,
    parse_service_url,
)
from mediacat.web.routes import _audit, _ctx, _require_role, _tmpl

logger = logging.getLogger(__name__)

streaming_router = APIRouter(tags=["streaming"])

SETTING_KEY = "streaming"
CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {
    "spotify": ("spotify_client_id", "spotify_client_secret"),
}
NOT_FOUND_TTL = timedelta(days=30)  # a cached miss is looked up again after this
ERROR_COOLDOWN_S = 600.0  # a failed lookup (network, quota) is not retried before this
WAIT_S = 6.0  # how long the icons request waits for a running lookup
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def _sf(request: Request) -> Any:
    """The app's async_sessionmaker (set in the lifespan)."""
    return request.app.state.db_session_factory


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class StreamingConfig:
    """Admin settings plus the credentials of the services that need them."""

    enabled: dict[str, bool]
    country: str
    credentials: dict[str, str] = field(default_factory=dict)

    def configured(self, service: str) -> bool:
        keys = CREDENTIAL_KEYS.get(service, ())
        return all(self.credentials.get(k) for k in keys)

    @property
    def services(self) -> list[str]:
        """Services that show icons: switched on and, if they need an account, configured."""
        return [s for s in SERVICES if self.enabled.get(s) and self.configured(s)]


def default_setting() -> dict[str, Any]:
    return {"services": dict.fromkeys(SERVICES, True), "country": DEFAULT_COUNTRY}


def _normalise(raw: Any) -> dict[str, Any]:
    setting = default_setting()
    if not isinstance(raw, dict):
        return setting
    services = raw.get("services")
    if isinstance(services, dict):
        for s in SERVICES:
            if isinstance(services.get(s), bool):
                setting["services"][s] = services[s]
    country = str(raw.get("country") or "").upper()
    if _COUNTRY_RE.match(country):
        setting["country"] = country
    return setting


async def load_config(sf: Any) -> StreamingConfig:
    """Settings and credentials in one query (defaults on any error)."""
    keys = [SETTING_KEY, *(k for ks in CREDENTIAL_KEYS.values() for k in ks)]
    stored: dict[str, str] = {}
    try:
        async with sf() as db:
            rows = (
                await db.execute(
                    select(SiteSetting.key, SiteSetting.value).where(SiteSetting.key.in_(keys))
                )
            ).all()
        stored = {r.key: r.value for r in rows}
    except Exception as exc:
        logger.warning("streaming settings unavailable: %s", exc)
    raw: Any = None
    with contextlib.suppress(ValueError):
        raw = json.loads(stored.get(SETTING_KEY) or "null")
    setting = _normalise(raw)
    credentials = {k: v.strip() for k, v in stored.items() if k != SETTING_KEY and v.strip()}
    return StreamingConfig(setting["services"], setting["country"], credentials)


async def _save_setting(request: Request, setting: dict[str, Any]) -> None:
    value = json.dumps(setting)
    async with _sf(request)() as db:
        await db.execute(
            insert(SiteSetting)
            .values(key=SETTING_KEY, value=value, description="Streaming-service links")
            .on_conflict_do_update(
                index_elements=["key"], set_={"value": value, "updated_at": datetime.now(UTC)}
            )
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _album_where(artist: str, album: str) -> list[Any]:
    """Live pressings of one album (case-insensitive: discography titles differ in case)."""
    return [
        Token.deleted_at.is_(None),
        Token.status != TokenStatus.MERGED,
        func.lower(Token.artist) == artist.lower(),
        func.lower(func.coalesce(Token.title, "")) == album.lower(),
    ]


async def _links(db: Any, artist: str, album: str) -> dict[str, StreamingLink]:
    rows = (
        (
            await db.execute(
                select(StreamingLink).where(
                    StreamingLink.artist_key == artist_key(artist),
                    StreamingLink.album_key == album_key(album),
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.service: r for r in rows}


@dataclass
class _AlbumFacts:
    year: int | None
    barcodes: tuple[str, ...]
    legacy_spotify: str | None


async def _album_facts(db: Any, artist: str, album: str) -> _AlbumFacts:
    rows = (
        await db.execute(
            select(Token.year, Token.barcode, Token.extra, Token.spotify_url).where(
                *_album_where(artist, album)
            )
        )
    ).all()
    barcodes: list[str] = []
    for r in rows:
        for code in collect_barcodes(r.barcode, r.extra):
            if code not in barcodes:
                barcodes.append(code)
    years = [r.year for r in rows if r.year]
    legacy = next((r.spotify_url for r in rows if r.spotify_url), None)
    return _AlbumFacts(min(years) if years else None, tuple(barcodes), legacy)


def _values(artist: str, album: str, service: str) -> dict[str, Any]:
    return {
        "artist_key": artist_key(artist),
        "album_key": album_key(album),
        "service": service,
        "artist_name": artist[:500],
        "album_title": album[:500],
    }


async def _sync_token_spotify(db: Any, artist: str, album: str, url: str | None) -> None:
    """Keep ``tokens.spotify_url`` in step with the album's Spotify row."""
    await db.execute(update(Token).where(*_album_where(artist, album)).values(spotify_url=url))


async def save_auto(db: Any, artist: str, album: str, service: str, match: Match | None) -> None:
    """Store an auto-match result; a manual row is never overwritten."""
    values = {
        "url": match.url if match else None,
        "external_id": match.external_id if match else None,
        "match_method": match.method if match else None,
        "match_score": match.score if match else None,
        "status": "found" if match else "not_found",
        "checked_at": datetime.now(UTC),
        "updated_by": None,
    }
    table = StreamingLink.__table__
    result = await db.execute(
        insert(StreamingLink)
        .values(**_values(artist, album, service), **values)
        .on_conflict_do_update(
            constraint="uq_streaming_links_album",
            set_={**values, "updated_at": datetime.now(UTC)},
            where=table.c.status != "manual",
        )
    )
    if service == "spotify" and match and result.rowcount:
        await _sync_token_spotify(db, artist, album, match.url)


async def seed_spotify(db: Any, artist: str, album: str, url: str) -> int:
    """Record a Spotify URL found elsewhere (discography enrichment) if the album has none yet.

    A URL already on one of the album's pressings (``tokens.spotify_url``) wins.
    """
    legacy = (await _album_facts(db, artist, album)).legacy_spotify
    parsed = parse_service_url("spotify", legacy or "") or parse_service_url("spotify", url)
    if parsed is None:
        return 0
    result = await db.execute(
        insert(StreamingLink)
        .values(
            **_values(artist, album, "spotify"),
            url=parsed[0],
            external_id=parsed[1],
            match_method="search",
            status="found",
        )
        .on_conflict_do_nothing(constraint="uq_streaming_links_album")
    )
    if not result.rowcount:
        return 0
    await _sync_token_spotify(db, artist, album, parsed[0])
    return 1


async def save_manual(
    db: Any,
    artist: str,
    album: str,
    service: str,
    url: str | None,
    external_id: str | None,
    user_id: uuid.UUID | None,
) -> None:
    """A user's correction: a link, or (``url`` None) "not on this service"."""
    values = {
        "url": url,
        "external_id": external_id,
        "match_method": "manual",
        "match_score": None,
        "status": "manual",
        "checked_at": datetime.now(UTC),
        "updated_by": user_id,
    }
    await db.execute(
        insert(StreamingLink)
        .values(**_values(artist, album, service), **values)
        .on_conflict_do_update(
            constraint="uq_streaming_links_album",
            set_={**values, "updated_at": datetime.now(UTC)},
        )
    )
    if service == "spotify":
        await _sync_token_spotify(db, artist, album, url)


async def clear_manual(db: Any, artist: str, album: str, service: str) -> None:
    """Back to automatic matching: drop the manual row (looked up on the next view)."""
    result = await db.execute(
        delete(StreamingLink).where(
            StreamingLink.artist_key == artist_key(artist),
            StreamingLink.album_key == album_key(album),
            StreamingLink.service == service,
            StreamingLink.status == "manual",
        )
    )
    if service == "spotify" and result.rowcount:
        # Else the hand-set link would come back as a "found" legacy URL.
        await _sync_token_spotify(db, artist, album, None)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

# Lookups in progress, by (artist_key, album_key): a second request for the
# same album joins the running task instead of starting another.
_lookup_tasks: dict[tuple[str, str], asyncio.Task[dict[str, str]]] = {}
# (artist_key, album_key, service) → monotonic time of the last failed lookup.
_failed_at: dict[tuple[str, str, str], float] = {}


def _cooling_down(artist: str, album: str, service: str) -> bool:
    failed = _failed_at.get((artist_key(artist), album_key(album), service))
    return failed is not None and time.monotonic() - failed < ERROR_COOLDOWN_S


def needs_lookup(link: StreamingLink | None, *, recheck: bool = False) -> bool:
    """No row yet, or a "not found" that is old enough (or *recheck*)."""
    if link is None:
        return True
    if link.status != "not_found":
        return False
    if recheck:
        return True
    checked = link.checked_at
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return datetime.now(UTC) - checked > NOT_FOUND_TTL


def _pending(
    links: dict[str, StreamingLink], config: StreamingConfig, artist: str, album: str
) -> list[str]:
    return [
        s
        for s in config.services
        if needs_lookup(links.get(s)) and not _cooling_down(artist, album, s)
    ]


async def resolve_album(
    sf: Any, artist: str, album: str, config: StreamingConfig, *, recheck: bool = False
) -> dict[str, str]:
    """Look the album up on every service that needs it and store the results.

    Returns ``{service: "found" | "not_found" | "error"}`` for the services
    looked up (empty when everything was cached).
    """
    async with sf() as db:
        links = await _links(db, artist, album)
        facts = await _album_facts(db, artist, album)
    todo = [
        s
        for s in config.services
        if needs_lookup(links.get(s), recheck=recheck) and not _cooling_down(artist, album, s)
    ]
    if not todo or not artist.strip() or not album.strip():
        return {}
    query = AlbumQuery(artist=artist, title=album, year=facts.year, barcodes=facts.barcodes)

    async def one(service: str) -> tuple[str, Match | None]:
        if service == "spotify" and facts.legacy_spotify and links.get(service) is None:
            # Found earlier by the discography enrichment (tokens.spotify_url).
            parsed = parse_service_url("spotify", facts.legacy_spotify)
            if parsed:
                return service, Match(parsed[0], parsed[1], "search", 1.0)
        return service, await FINDERS[service](query, config.country, config.credentials)

    outcome: dict[str, str] = {}
    results = await asyncio.gather(*(one(s) for s in todo), return_exceptions=True)
    async with sf() as db:
        for service, result in zip(todo, results, strict=True):
            if isinstance(result, BaseException):
                _failed_at[(artist_key(artist), album_key(album), service)] = time.monotonic()
                logger.warning(
                    "Streaming lookup %s failed for %r / %r: %s", service, artist, album, result
                )
                outcome[service] = "error"
                continue
            match = result[1]
            await save_auto(db, artist, album, service, match)
            outcome[service] = "found" if match else "not_found"
        await db.commit()
    logger.info("Streaming lookup %r / %r: %s", artist, album, outcome)
    return outcome


def _lookup_done(key: tuple[str, str], task: asyncio.Task[dict[str, str]]) -> None:
    if _lookup_tasks.get(key) is task:
        del _lookup_tasks[key]
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Streaming lookup %s failed: %s", key, task.exception())


def start_lookup(
    sf: Any, artist: str, album: str, config: StreamingConfig, *, recheck: bool = False
) -> asyncio.Task[dict[str, str]]:
    """The running lookup for this album, or a new one."""
    key = (artist_key(artist), album_key(album))
    task = _lookup_tasks.get(key)
    if task is None:
        task = asyncio.create_task(
            resolve_album(sf, artist, album, config, recheck=recheck),
            name=f"streaming:{key[0][:30]}/{key[1][:30]}",
        )
        _lookup_tasks[key] = task
        task.add_done_callback(lambda t: _lookup_done(key, t))
    return task


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def icons_url(artist: str, album: str) -> str:
    return "/streaming/icons?" + urllib.parse.urlencode({"artist": artist, "album": album})


def safe_back(value: str | None) -> str:
    """A same-site path (with query) to return to after editing; ``/`` otherwise."""
    if not value:
        return "/"
    parts = urllib.parse.urlsplit(value)
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path + (f"?{parts.query}" if parts.query else "")


def link_rows(links: dict[str, StreamingLink], services: list[str]) -> list[dict[str, str]]:
    """Icons to show, in service order: found or manual rows that have a URL."""
    return [
        {"service": s, "label": SERVICE_LABELS[s], "url": links[s].url or ""}
        for s in services
        if s in links and links[s].status in ("found", "manual") and links[s].url
    ]


@streaming_router.get("/streaming/icons", response_class=HTMLResponse)
async def streaming_icons(
    request: Request, artist: str = Query(""), album: str = Query("")
) -> HTMLResponse:
    """HTMX partial: the album's service icons (looks up what isn't cached yet)."""
    config = await load_config(_sf(request))
    services = config.services
    pending = False
    if services and artist.strip() and album.strip():
        async with _sf(request)() as db:
            links = await _links(db, artist, album)
        if _pending(links, config, artist, album):
            task = start_lookup(_sf(request), artist, album, config)
            # shield: a browser that goes away cancels this request, not the lookup.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=WAIT_S)
            pending = not task.done()
            async with _sf(request)() as db:
                links = await _links(db, artist, album)
    else:
        links = {}
    back = safe_back(request.headers.get("hx-current-url"))
    ctx = _ctx(
        request,
        services=services,
        links=link_rows(links, services),
        pending=pending,
        icons_url=icons_url(artist, album),
        edit_url="/streaming/edit?"
        + urllib.parse.urlencode({"artist": artist, "album": album, "back": back}),
        album=album,
        show=bool(services and artist.strip() and album.strip()),
    )
    return _tmpl().TemplateResponse(
        request=request, name="partials/streaming_icons.html", context=ctx
    )


# ---------------------------------------------------------------------------
# Edit links (any logged-in user)
# ---------------------------------------------------------------------------


def _edit_rows(
    links: dict[str, StreamingLink], config: StreamingConfig, form: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    rows = []
    for s in config.services:
        link = links.get(s)
        if link is None:
            state, mode = "Not looked up yet", "auto"
        elif link.status == "found":
            state, mode = f"Found automatically ({link.match_method})", "auto"
        elif link.status == "not_found":
            state, mode = "Not found automatically", "auto"
        elif link.url:
            state, mode = "Set by hand", "url"
        else:
            state, mode = "Marked as not on this service", "none"
        if form is not None:
            mode = form.get(f"mode_{s}", mode)
        rows.append(
            {
                "service": s,
                "label": SERVICE_LABELS[s],
                "state": state,
                "mode": mode,
                "url": (form or {}).get(f"url_{s}", link.url if link and link.url else ""),
                "current": link.url if link and link.status in ("found", "manual") else "",
            }
        )
    return rows


async def _db_user_id(request: Request) -> uuid.UUID | None:
    """The logged-in user's id if it has a DB row (bootstrap users don't)."""
    try:
        uid = uuid.UUID(str(getattr(request.state, "user_id", "") or ""))
    except ValueError:
        return None
    async with _sf(request)() as db:
        found = (await db.execute(select(User.id).where(User.id == uid))).scalar_one_or_none()
    return uid if found is not None else None


def _album_ref(artist: str, album: str) -> str:
    return hashlib.sha256(f"{artist_key(artist)}\x1f{album_key(album)}".encode()).hexdigest()[:32]


@streaming_router.get("/streaming/edit", response_class=HTMLResponse)
async def streaming_edit_page(
    request: Request,
    artist: str = Query(""),
    album: str = Query(""),
    back: str = Query("/"),
) -> HTMLResponse:
    if not artist.strip() or not album.strip():
        raise HTTPException(status_code=400, detail="Artist and album are required")
    config = await load_config(_sf(request))
    async with _sf(request)() as db:
        links = await _links(db, artist, album)
    ctx = _ctx(
        request,
        artist=artist,
        album=album,
        back=safe_back(back),
        rows=_edit_rows(links, config),
        errors={},
        disabled=[SERVICE_LABELS[s] for s in SERVICES if s not in config.services],
    )
    return _tmpl().TemplateResponse(request=request, name="streaming_edit.html", context=ctx)


@streaming_router.post("/streaming/edit", response_model=None)
async def streaming_edit_save(request: Request) -> HTMLResponse | RedirectResponse:
    """Per service: ``auto`` (drop a manual entry), ``url`` (paste a link) or ``none``."""
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    artist, album = form.get("artist", ""), form.get("album", "")
    back = safe_back(form.get("back"))
    if not artist.strip() or not album.strip():
        raise HTTPException(status_code=400, detail="Artist and album are required")
    config = await load_config(_sf(request))

    errors: dict[str, str] = {}
    changes: dict[str, tuple[str, str | None, str | None]] = {}
    for s in config.services:
        mode = form.get(f"mode_{s}", "auto")
        if mode == "url":
            parsed = parse_service_url(s, form.get(f"url_{s}", ""))
            if parsed is None:
                errors[s] = f"That is not a {SERVICE_LABELS[s]} album link."
                continue
            changes[s] = ("url", parsed[0], parsed[1])
        elif mode == "none":
            changes[s] = ("none", None, None)
        else:
            changes[s] = ("auto", None, None)

    if errors:
        async with _sf(request)() as db:
            links = await _links(db, artist, album)
        ctx = _ctx(
            request,
            artist=artist,
            album=album,
            back=back,
            rows=_edit_rows(links, config, form),
            errors=errors,
            disabled=[SERVICE_LABELS[s] for s in SERVICES if s not in config.services],
        )
        return _tmpl().TemplateResponse(
            request=request, name="streaming_edit.html", context=ctx, status_code=400
        )

    user_id = await _db_user_id(request)
    async with _sf(request)() as db:
        for s, (mode, url, external_id) in changes.items():
            if mode == "auto":
                await clear_manual(db, artist, album, s)
            else:
                await save_manual(db, artist, album, s, url, external_id, user_id)
        await db.commit()
    await _audit(
        request,
        "streaming_links.edit",
        "album",
        _album_ref(artist, album),
        detail={
            "artist": artist,
            "album": album,
            "links": {s: url if mode == "url" else mode for s, (mode, url, _id) in changes.items()},
        },
    )
    return RedirectResponse(url=back, status_code=303)


# ---------------------------------------------------------------------------
# Admin: settings and the "Match all albums" job
# ---------------------------------------------------------------------------


@dataclass
class MatchJob:
    """Progress of the "Match all albums" job (one per process)."""

    total: int = 0
    done: int = 0
    looked_up: int = 0
    found: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SERVICES, 0))
    not_found: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SERVICES, 0))
    errors: int = 0
    current: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stopped: bool = False
    failed: str = ""
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


_job: MatchJob | None = None


async def _all_albums(sf: Any) -> list[tuple[str, str]]:
    async with sf() as db:
        rows = (
            await db.execute(
                select(Token.artist, Token.title)
                .where(
                    Token.deleted_at.is_(None),
                    Token.status != TokenStatus.MERGED,
                    Token.artist.isnot(None),
                    Token.title.isnot(None),
                    Token.title != "",
                )
                .group_by(Token.artist, Token.title)
                .order_by(Token.artist, Token.title)
            )
        ).all()
    seen: set[tuple[str, str]] = set()
    albums: list[tuple[str, str]] = []
    for r in rows:
        key = (artist_key(r.artist), album_key(r.title))
        if key not in seen and key[0] and key[1]:
            seen.add(key)
            albums.append((r.artist, r.title))
    return albums


async def _run_match_job(sf: Any, job: MatchJob, config: StreamingConfig, recheck: bool) -> None:
    try:
        albums = await _all_albums(sf)
        job.total = len(albums)
        for artist, album in albums:
            if job.stop.is_set():
                job.stopped = True
                break
            job.current = f"{artist} — {album}"
            outcome = await asyncio.shield(start_lookup(sf, artist, album, config, recheck=recheck))
            if outcome:
                job.looked_up += 1
            for service, result in outcome.items():
                if result == "found":
                    job.found[service] += 1
                elif result == "not_found":
                    job.not_found[service] += 1
                else:
                    job.errors += 1
            job.done += 1
    except Exception as exc:
        logger.exception("Streaming match job failed")
        job.failed = str(exc)[:200]
    finally:
        job.current = ""
        job.finished_at = datetime.now(UTC)
        logger.info(
            "Streaming match job: %d/%d albums, %d looked up, found %s, errors %d",
            job.done,
            job.total,
            job.looked_up,
            job.found,
            job.errors,
        )


async def _cache_counts(sf: Any) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {
        s: {"found": 0, "not_found": 0, "manual": 0} for s in SERVICES
    }
    try:
        async with sf() as db:
            rows = (
                await db.execute(
                    select(StreamingLink.service, StreamingLink.status, func.count()).group_by(
                        StreamingLink.service, StreamingLink.status
                    )
                )
            ).all()
        for service, status, n in rows:
            if service in counts and status in counts[service]:
                counts[service][status] = n
    except Exception as exc:
        logger.warning("streaming cache counts unavailable: %s", exc)
    return counts


async def admin_view(request: Request) -> dict[str, Any]:
    """Template data for Admin → Streaming."""
    config = await load_config(_sf(request))
    counts = await _cache_counts(_sf(request))
    services = [
        {
            "key": s,
            "label": SERVICE_LABELS[s],
            "enabled": config.enabled.get(s, False),
            "needs_account": s in NEEDS_CREDENTIALS,
            "configured": config.configured(s),
            "counts": counts[s],
        }
        for s in SERVICES
    ]
    return {"services": services, "country": config.country, "job": _job_view()}


def _job_view() -> dict[str, Any]:
    job = _job
    if job is None:
        return {"exists": False, "running": False}
    pct = int(job.done * 100 / job.total) if job.total else (100 if job.finished_at else 0)
    return {
        "exists": True,
        "running": job.running,
        "total": job.total,
        "done": job.done,
        "pct": pct,
        "looked_up": job.looked_up,
        "found": job.found,
        "not_found": job.not_found,
        "errors": job.errors,
        "current": job.current,
        "stopped": job.stopped,
        "failed": job.failed,
        "started_at": job.started_at.strftime("%Y-%m-%d %H:%M") if job.started_at else "",
        "finished_at": job.finished_at.strftime("%Y-%m-%d %H:%M") if job.finished_at else "",
        "labels": SERVICE_LABELS,
    }


def _admin_redirect(**params: str) -> RedirectResponse:
    query = "".join(f"&{k}={v}" for k, v in params.items())
    return RedirectResponse(url="/admin?tab=streaming" + query, status_code=303)


@streaming_router.post("/admin/streaming/settings", response_model=None)
async def streaming_settings_save(request: Request) -> RedirectResponse:
    _require_role(request, "admin")
    form = await request.form()
    country = str(form.get("country") or "").strip().upper()
    if not _COUNTRY_RE.match(country):
        return _admin_redirect(st_error="country")
    setting = {
        "services": {s: form.get(f"service_{s}") == "on" for s in SERVICES},
        "country": country,
    }
    try:
        await _save_setting(request, setting)
    except Exception:
        logger.exception("streaming settings: saving failed")
        return _admin_redirect(st_error="db")
    await _audit(request, "streaming.settings", "site_setting", SETTING_KEY, detail=setting)
    return _admin_redirect(st_msg="saved")


def _job_partial(request: Request) -> HTMLResponse:
    return _tmpl().TemplateResponse(
        request=request,
        name="partials/streaming_job.html",
        context=_ctx(request, job=_job_view()),
    )


@streaming_router.post("/admin/streaming/match", response_class=HTMLResponse)
async def streaming_match_start(request: Request) -> HTMLResponse:
    """Start "Match all albums" (no-op while one runs); answers the progress partial."""
    global _job  # noqa: PLW0603 - one job per process
    _require_role(request, "admin")
    if _job is None or not _job.running:
        form = await request.form()
        recheck = form.get("recheck") == "on"
        config = await load_config(_sf(request))
        job = MatchJob(started_at=datetime.now(UTC))
        job.task = asyncio.create_task(
            _run_match_job(_sf(request), job, config, recheck), name="streaming-match-all"
        )
        _job = job
        await _audit(
            request, "streaming.match_all", "site_setting", SETTING_KEY, detail={"recheck": recheck}
        )
    return _job_partial(request)


@streaming_router.post("/admin/streaming/match/stop", response_class=HTMLResponse)
async def streaming_match_stop(request: Request) -> HTMLResponse:
    _require_role(request, "admin")
    if _job is not None and _job.running:
        _job.stop.set()
    return _job_partial(request)


@streaming_router.get("/admin/streaming/match/status", response_class=HTMLResponse)
async def streaming_match_status(request: Request) -> HTMLResponse:
    _require_role(request, "admin")
    return _job_partial(request)
