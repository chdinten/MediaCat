"""Catalogue browser for the 3panel layout: artist index, albums, pressings, preview.

The selection lives in the URL (``/catalogue?artist=&album=&pressing=`` plus the
filter parameters), so Back, reload, bookmarks and the pressing page's breadcrumb
all reopen the exact view.  Picking an artist or an album pushes a history entry
(``hx-push-url``); selecting another pressing of the same album replaces it
(``hx-replace-url``), so stepping through the pressings adds no Back steps.
``album=`` (empty) is the album without a title.

URL structure
-------------
GET  /catalogue                     full page (``catalogue.catalogue_index`` delegates here)
GET  /catalogue/browse/artist       centre panel: artist header, album covers (HTMX)
GET  /catalogue/browse/album        pressings of one album as a versions table (HTMX)
GET  /catalogue/{token_id}/preview  right panel: one pressing (HTMX)
"""

from __future__ import annotations

import logging
import unicodedata
import urllib.parse
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import ColumnElement, func, literal_column, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from mediacat.db.enums import ImageRegion, MediaFormat, TokenStatus
from mediacat.db.models import ArtistBio, Country, MediaObject, Token
from mediacat.ingestion.discogs_extras import collection_view, display_artist
from mediacat.web.catalogue import _fetch_facets
from mediacat.web.routes import _ctx, _tmpl

logger = logging.getLogger(__name__)

catalogue_browser_router = APIRouter(prefix="/catalogue", tags=["catalogue"])

_FILTER_KEYS = ("q", "fmt", "genres", "year_from", "year_to", "country_q")
_FORMAT_LABELS = {"vinyl": "Vinyl", "cd": "CD"}
_MATCH_LIMIT = 100  # pressings listed when filters are set but no artist is open
_BACK_PATHS = ("/catalogue", "/search")  # pages the pressing page may link back to
# Album key: NULL and "" titles form one "Untitled" album.  A literal (not a bound
# parameter) so the GROUP BY expression is identical to the selected one.
_album_key = func.coalesce(Token.title, literal_column("''"))


def _sf(request: Request) -> Any:
    """The app's async_sessionmaker (set in the lifespan)."""
    return request.app.state.db_session_factory


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        return None


# ── Filters and URLs ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrowseFilters:
    """Catalogue filters as they appear in the query string (empty = not set)."""

    q: str = ""
    fmt: str = ""
    genres: str = ""
    year_from: str = ""
    year_to: str = ""
    country_q: str = ""

    @classmethod
    def clean(
        cls,
        *,
        q: str = "",
        fmt: str = "",
        genres: str = "",
        year_from: str = "",
        year_to: str = "",
        country_q: str = "",
    ) -> BrowseFilters:
        """Trim the raw values and drop the ones the queries would ignore."""
        genre_list = [g.strip() for g in genres.split(",") if g.strip()]
        return cls(
            q=q.strip(),
            fmt=fmt if fmt in _FORMAT_LABELS else "",
            genres=",".join(genre_list),
            year_from=year_from.strip() if year_from.strip().isdigit() else "",
            year_to=year_to.strip() if year_to.strip().isdigit() else "",
            country_q=country_q.strip(),
        )

    @classmethod
    def from_query(cls, query: str) -> BrowseFilters:
        """The filters of a ``/catalogue`` query string."""
        params = dict(urllib.parse.parse_qsl(query))
        return cls.clean(**{key: params.get(key, "") for key in _FILTER_KEYS})

    @property
    def genre_list(self) -> list[str]:
        return [g for g in self.genres.split(",") if g]

    @property
    def active(self) -> bool:
        return bool(self.pairs())

    def pairs(self) -> list[tuple[str, str]]:
        """The filters that are set, as query-string pairs in a fixed order."""
        values = (self.q, self.fmt, self.genres, self.year_from, self.year_to, self.country_q)
        return [(key, value) for key, value in zip(_FILTER_KEYS, values, strict=True) if value]


def browse_url(
    filters: BrowseFilters,
    artist: str | None = None,
    album: str | None = None,
    pressing: str | None = None,
) -> str:
    """The ``/catalogue`` URL that reopens this selection with these filters."""
    pairs: list[tuple[str, str]] = []
    if artist:
        pairs.append(("artist", artist))
        if album is not None:
            pairs.append(("album", album))
            if pressing:
                pairs.append(("pressing", pressing))
    pairs += filters.pairs()
    return "/catalogue?" + urllib.parse.urlencode(pairs) if pairs else "/catalogue"


def _partial_url(path: str, filters: BrowseFilters, **params: str) -> str:
    return path + "?" + urllib.parse.urlencode([*params.items(), *filters.pairs()])


def _fold(text: str) -> str:
    """Lower case without accents, for sorting and the type-to-filter box."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def sort_key(name: str) -> str:
    """Index order: "The Cure" files under C; case and accents are ignored."""
    bare = name.strip()
    if bare[:4].lower() == "the " and len(bare) > 4:
        bare = bare[4:].lstrip()
    return _fold(bare)


def index_letter(name: str) -> str:
    """The A–Z group an artist is listed under; digits and anything else go under "#"."""
    first = sort_key(name)[:1].upper()
    return first if "A" <= first <= "Z" else "#"


def _years(first: int | None, last: int | None) -> str:
    if not first:
        return ""
    return str(first) if not last or last == first else f"{first}–{last}"


def _grade(value: str) -> str:
    """Discogs grade in short form: "Very Good Plus (VG+)" → "VG+", "Near Mint (NM or M-)" → "NM"."""
    value = value.strip()
    if value.endswith(")") and "(" in value:
        inner = value[value.rindex("(") + 1 : -1].split(" or ")[0].strip()
        return inner or value
    return value


def _clauses(filters: BrowseFilters) -> list[ColumnElement[bool]]:
    """WHERE clauses for the live pressings that pass the filters."""
    clauses: list[ColumnElement[bool]] = [
        Token.deleted_at.is_(None),
        Token.status != TokenStatus.MERGED,
        Token.artist.isnot(None),
    ]
    if filters.q:
        like = f"%{filters.q}%"
        clauses.append(or_(Token.artist.ilike(like), Token.title.ilike(like)))
    if filters.fmt:
        clauses.append(Token.media_format == MediaFormat(filters.fmt))
    if filters.year_from:
        clauses.append(Token.year >= int(filters.year_from))
    if filters.year_to:
        clauses.append(Token.year <= int(filters.year_to))
    if filters.country_q:
        clauses.append(
            Token.country_id.in_(
                select(Country.id).where(Country.name.ilike(f"%{filters.country_q}%"))
            )
        )
    if filters.genre_list:
        genre = func.jsonb_array_elements_text(Token.genres).table_valued("value").alias("genre")
        wanted = [g.lower() for g in filters.genre_list]
        clauses.append(
            select(genre.c.value)
            .where(func.lower(genre.c.value).in_(wanted))
            .correlate(Token)
            .exists()
        )
    return clauses


# ── Pressing helpers ──────────────────────────────────────────────────────────


def pressing_label(token: Token) -> str:
    """Short name of one pressing, e.g. "UK 1973 · SHVL 804" (needs ``country`` loaded)."""
    place = " ".join(
        part
        for part in (token.country.name if token.country else "", str(token.year or ""))
        if part
    )
    return " · ".join(part for part in (place, token.catalog_number or "") if part) or "Pressing"


def _cover_url(token: Token) -> str | None:
    """Primary cover, else the front-cover photo; never a label or runout shot."""
    cover = next((m for m in token.media_objects if m.is_primary_cover), None) or next(
        (m for m in token.media_objects if m.region == ImageRegion.COVER_FRONT), None
    )
    return f"/catalogue/{token.id}/images/{cover.id}" if cover else None


def _condition(token: Token) -> tuple[str, str]:
    """(media, sleeve) grades from the Discogs collection fields, short form."""
    known = collection_view(token.extra)["known"]
    return _grade(known.get("media_condition", "")), _grade(known.get("sleeve_condition", ""))


def _format_label(token: Token) -> str:
    return _FORMAT_LABELS.get(token.media_format.value, token.media_format.value)


def _completeness(token: Token) -> int:
    """Cover, tracklist, country and matrix: how many of the four are filled in."""
    return (
        int(any(m.is_primary_cover for m in token.media_objects))
        + int(bool(token.tracklist))
        + int(token.country_id is not None)
        + int(bool(token.matrix_runout or token.matrix_runout_b))
    )


def _version_order(token: Token) -> tuple[bool, int, str, str, str]:
    """Versions-table order: year (unknown last), then country and catalogue number."""
    return (
        token.year is None,
        token.year or 0,
        _fold(token.country.name) if token.country else "￿",
        _fold(token.catalog_number or ""),
        str(token.id),
    )


# ── Queries ───────────────────────────────────────────────────────────────────


async def _artist_index(db: Any, filters: BrowseFilters, selected: str) -> list[dict[str, Any]]:
    """Every artist with a pressing that passes the filters, in index order."""
    rows = (
        await db.execute(
            select(
                Token.artist,
                func.count(Token.id).label("cnt"),
                func.min(Token.year).label("yr_min"),
                func.max(Token.year).label("yr_max"),
            )
            .where(*_clauses(filters))
            .group_by(Token.artist)
        )
    ).all()
    entries = [
        {
            "name": r.artist,
            "count": r.cnt,
            "years": _years(r.yr_min, r.yr_max),
            "letter": index_letter(r.artist),
            "fold": _fold(r.artist),
            "sort": sort_key(r.artist),
            "active": r.artist == selected,
            "url": browse_url(filters, r.artist),
            "hx_url": _partial_url("/catalogue/browse/artist", filters, artist=r.artist),
        }
        for r in rows
    ]
    entries.sort(key=lambda e: (e["letter"] != "#", e["sort"], e["name"]))
    return entries


def _index_groups(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for entry in entries:
        if not groups or groups[-1]["letter"] != entry["letter"]:
            anchor = "num" if entry["letter"] == "#" else entry["letter"]
            groups.append({"letter": entry["letter"], "anchor": anchor, "artists": []})
        groups[-1]["artists"].append(entry)
    return groups


def _rail(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    present = {g["letter"] for g in groups}
    letters = ["#", *(chr(c) for c in range(ord("A"), ord("Z") + 1))]
    return [
        {
            "letter": letter,
            "anchor": "num" if letter == "#" else letter,
            "enabled": letter in present,
        }
        for letter in letters
    ]


async def _artist_view(
    db: Any, filters: BrowseFilters, artist: str, album: str | None
) -> dict[str, Any]:
    """Header and album covers of one artist (counts follow the filters)."""
    where = [*_clauses(filters), Token.artist == artist]
    rows = (
        await db.execute(
            select(
                _album_key.label("title"),
                func.count(Token.id).label("cnt"),
                func.min(Token.year).label("yr_min"),
                func.max(Token.year).label("yr_max"),
            )
            .where(*where)
            .group_by(_album_key)
            .order_by(func.min(Token.year).asc().nulls_last(), _album_key.asc())
        )
    ).all()
    covers: dict[str, str] = {}
    if rows:
        cover_rows = (
            await db.execute(
                select(_album_key.label("title"), MediaObject.token_id, MediaObject.id)
                .join(MediaObject, MediaObject.token_id == Token.id)
                .where(*where)
                .where(
                    or_(
                        MediaObject.is_primary_cover.is_(True),
                        MediaObject.region == ImageRegion.COVER_FRONT,
                    )
                )
                .order_by(
                    MediaObject.is_primary_cover.desc().nulls_last(),
                    Token.year.asc().nulls_last(),
                )
            )
        ).all()
        for c in cover_rows:
            covers.setdefault(c.title, f"/catalogue/{c.token_id}/images/{c.id}")
    portrait = (
        await db.execute(
            select(ArtistBio.thumbnail_url)
            .where(ArtistBio.artist_name == artist, ArtistBio.thumbnail_url.isnot(None))
            .limit(1)
        )
    ).scalar_one_or_none()

    albums = [
        {
            "title": r.title,
            "display": r.title or "Untitled",
            "initial": (sort_key(r.title)[:1] or "?").upper(),
            "year": r.yr_min,
            "count": r.cnt,
            "cover_url": covers.get(r.title),
            "active": album is not None and r.title == album,
            "url": browse_url(filters, artist, r.title),
            "hx_url": _partial_url(
                "/catalogue/browse/album", filters, artist=artist, album=r.title
            ),
        }
        for r in rows
    ]
    firsts = [r.yr_min for r in rows if r.yr_min]
    lasts = [r.yr_max for r in rows if r.yr_max]
    return {
        "name": artist,
        "initial": (sort_key(artist)[:1] or "?").upper(),
        "portrait_url": portrait,
        "count": sum(r.cnt for r in rows),
        "album_count": len(rows),
        "years": _years(min(firsts) if firsts else None, max(lasts) if lasts else None),
        "albums": albums,
        "bio_url": "/artists?" + urllib.parse.urlencode({"artist": artist}),
        "all_url": browse_url(BrowseFilters(), artist),
    }


async def _versions(db: Any, filters: BrowseFilters, artist: str, album: str) -> list[Token]:
    """The pressings of one album that pass the filters, in versions-table order."""
    tokens = (
        (
            await db.execute(
                select(Token)
                .options(
                    selectinload(Token.label),
                    selectinload(Token.country),
                    selectinload(Token.media_objects),
                )
                .where(*_clauses(filters), Token.artist == artist, _album_key == album)
            )
        )
        .scalars()
        .all()
    )
    return sorted(tokens, key=_version_order)


def _versions_view(
    tokens: list[Token], filters: BrowseFilters, artist: str, album: str, selected: str
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for token in tokens:
        tid = str(token.id)
        media, sleeve = _condition(token)
        rows.append(
            {
                "id": tid,
                "name": pressing_label(token),
                "year": token.year,
                "country": token.country.name if token.country else "",
                "label": token.label.name if token.label else "",
                "catno": token.catalog_number or "",
                "format": _format_label(token),
                "condition": f"{media or '—'} / {sleeve or '—'}" if media or sleeve else "",
                "completeness": _completeness(token),
                "active": tid == selected,
                "open_url": f"/catalogue/{tid}",
                "preview_url": f"/catalogue/{tid}/preview",
                "state_url": browse_url(filters, artist, album, tid),
            }
        )
    formats = Counter(r["format"] for r in rows)
    return {
        "artist": artist,
        "title": album,
        "display": album or "Untitled",
        "rows": rows,
        "summary": " · ".join(f"{count} {fmt}" for fmt, count in formats.most_common()),
    }


async def _load_pressing(db: Any, token_id: uuid.UUID) -> Token | None:
    token: Token | None = (
        await db.execute(
            select(Token)
            .options(
                selectinload(Token.label),
                selectinload(Token.country),
                selectinload(Token.media_objects),
            )
            .where(Token.id == token_id, Token.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    return token


def _preview_view(token: Token) -> dict[str, Any]:
    media, sleeve = _condition(token)
    collection = collection_view(token.extra)
    return {
        "id": str(token.id),
        "artist": token.artist or "",
        "title": token.title or "Untitled",
        "album": token.title or "",
        "name": pressing_label(token),
        "cover_url": _cover_url(token),
        "format": _format_label(token),
        "country": token.country.name if token.country else "",
        "year": token.year,
        "label": token.label.name if token.label else "",
        "catno": token.catalog_number or "",
        "matrix_a": token.matrix_runout or "",
        "matrix_b": token.matrix_runout_b or "",
        "media": media,
        "sleeve": sleeve,
        "signed": collection["signed"],
        "signed_by": collection["signed_by"],
        "genres": ", ".join(token.genres or []),
        "rating": token.personal_rating,
        "discogs_url": (
            f"https://www.discogs.com/release/{token.discogs_release_id}"
            if token.discogs_release_id
            else ""
        ),
        "open_url": f"/catalogue/{token.id}",
        "edit_url": f"/catalogue/{token.id}/edit",
    }


async def _matches(db: Any, filters: BrowseFilters) -> tuple[list[dict[str, Any]], int]:
    """Pressings that pass the filters, for the centre panel when no artist is open."""
    where = _clauses(filters)
    total: int = (await db.execute(select(func.count(Token.id)).where(*where))).scalar_one()
    tokens = (
        (
            await db.execute(
                select(Token)
                .options(selectinload(Token.country))
                .where(*where)
                .order_by(Token.artist.asc(), _album_key.asc(), Token.year.asc().nulls_last())
                .limit(_MATCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    rows = [
        {
            "artist": t.artist or "",
            "title": t.title or "Untitled",
            "year": t.year,
            "format": _format_label(t),
            "country": t.country.name if t.country else "",
            "catno": t.catalog_number or "",
            "url": browse_url(filters, t.artist, t.title or "", str(t.id)),
        }
        for t in tokens
    ]
    return rows, total


# ── Left panel: filters ───────────────────────────────────────────────────────


async def _facets(
    db: Any, filters: BrowseFilters, artist: str, album: str | None
) -> dict[str, list[dict[str, Any]]]:
    """Format, genre and decade pills; a filter change keeps the open artist and album."""
    raw = await _fetch_facets(
        db, filters.q, filters.fmt, filters.year_from, filters.year_to, filters.country_q
    )

    def url(changed: BrowseFilters) -> str:
        return browse_url(changed, artist or None, album)

    wanted = {g.lower() for g in filters.genre_list}
    genres: list[dict[str, Any]] = []
    for g in raw["genres"]:
        on = g["name"].lower() in wanted
        others = [x for x in filters.genre_list if x.lower() != g["name"].lower()]
        chosen = others if on else [*filters.genre_list, g["name"]]
        genres.append(
            {
                "label": g["name"],
                "count": g["count"],
                "active": on,
                "url": url(replace(filters, genres=",".join(chosen))),
            }
        )
    formats = [
        {
            "label": _FORMAT_LABELS.get(f["name"], f["name"]),
            "count": f["count"],
            "active": filters.fmt == f["name"],
            "url": url(replace(filters, fmt="" if filters.fmt == f["name"] else f["name"])),
        }
        for f in raw["formats"]
    ]
    decades: list[dict[str, Any]] = []
    for d in raw["decades"]:
        start, end = str(d["decade"]), str(d["decade"] + 9)
        on = filters.year_from == start and filters.year_to == end
        decades.append(
            {
                "label": d["label"],
                "count": d["count"],
                "active": on,
                "url": url(
                    replace(filters, year_from="" if on else start, year_to="" if on else end)
                ),
            }
        )
    return {"formats": formats, "genres": genres, "decades": decades}


def _chips(filters: BrowseFilters, artist: str, album: str | None) -> list[dict[str, str]]:
    """The filters in force, each with the URL that removes it."""

    def url(changed: BrowseFilters) -> str:
        return browse_url(changed, artist or None, album)

    chips: list[dict[str, str]] = []
    if filters.q:
        chips.append({"label": f"Search: {filters.q}", "url": url(replace(filters, q=""))})
    if filters.fmt:
        chips.append({"label": _FORMAT_LABELS[filters.fmt], "url": url(replace(filters, fmt=""))})
    for genre in filters.genre_list:
        rest = ",".join(g for g in filters.genre_list if g != genre)
        chips.append({"label": genre, "url": url(replace(filters, genres=rest))})
    if filters.year_from or filters.year_to:
        if filters.year_from and filters.year_to:
            years = f"{filters.year_from}–{filters.year_to}"
        else:
            years = f"From {filters.year_from}" if filters.year_from else f"Until {filters.year_to}"
        chips.append({"label": years, "url": url(replace(filters, year_from="", year_to=""))})
    if filters.country_q:
        chips.append(
            {"label": f"Country: {filters.country_q}", "url": url(replace(filters, country_q=""))}
        )
    return chips


def _hidden(
    filters: BrowseFilters, artist: str, album: str | None, *skip: str
) -> list[tuple[str, str]]:
    """Hidden inputs for the left-panel forms: the selection and the other filters."""
    pairs: list[tuple[str, str]] = []
    if artist:
        pairs.append(("artist", artist))
        if album is not None:
            pairs.append(("album", album))
    pairs += [(k, v) for k, v in filters.pairs() if k not in skip]
    return pairs


# ── Full page ─────────────────────────────────────────────────────────────────


async def browser_page(
    request: Request,
    *,
    filters: BrowseFilters,
    artist: str,
    album: str | None,
    pressing: str,
) -> Response:
    """``/catalogue`` in the 3panel layout, with the selection taken from the URL."""
    pressing_id = _parse_uuid(pressing) if pressing else None
    state: dict[str, Any] = {
        "index": [],
        "facets": {"formats": [], "genres": [], "decades": []},
        "view": None,
        "versions": None,
        "preview": None,
        "matches": None,
        "match_total": 0,
    }
    db_error = False
    try:
        async with _sf(request)() as db:
            if pressing_id and not artist:
                # A link that names only the pressing: send it to the full selection.
                token = await _load_pressing(db, pressing_id)
                if token is not None and token.artist:
                    target = browse_url(filters, token.artist, token.title or "", str(token.id))
                    return RedirectResponse(target, status_code=303)
            state["index"] = await _artist_index(db, filters, artist)
            state["facets"] = await _facets(db, filters, artist, album)
            if artist:
                state.update(await _load_selection(db, filters, artist, album, pressing_id))
            elif filters.active:
                state["matches"], state["match_total"] = await _matches(db, filters)
    except (SQLAlchemyError, OSError) as exc:  # query failed, or Postgres is unreachable
        logger.error("Catalogue browser DB error (migrations not run?): %s", exc)
        db_error = True

    groups = _index_groups(state["index"])
    ctx = _ctx(
        request,
        filters=filters,
        artist=artist,
        album=album,
        db_error=db_error,
        index_groups=groups,
        rail=_rail(groups),
        artist_total=len(state["index"]),
        pressing_total=sum(e["count"] for e in state["index"]),
        facets=state["facets"],
        chips=_chips(filters, artist, album),
        clear_url=browse_url(BrowseFilters(), artist or None, album),
        search_hidden=_hidden(filters, artist, album, "q"),
        range_hidden=_hidden(filters, artist, album, "year_from", "year_to", "country_q"),
        view=state["view"],
        versions=state["versions"],
        preview=state["preview"],
        matches=state["matches"],
        match_total=state["match_total"],
    )
    return _tmpl().TemplateResponse(request=request, name="catalogue_browser.html", context=ctx)


async def _load_selection(
    db: Any,
    filters: BrowseFilters,
    artist: str,
    album: str | None,
    pressing_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Centre and right panel for the open artist, album and pressing."""
    view = await _artist_view(db, filters, artist, album)
    selection: dict[str, Any] = {"view": view, "versions": None, "preview": None}
    if album is None and len(view["albums"]) == 1:
        # One album: open it straight away.  The URL names no album, so a reload
        # opens it the same way.
        album = view["albums"][0]["title"]
        view["albums"][0]["active"] = True
    if album is not None:
        tokens = await _versions(db, filters, artist, album)
        selection["versions"] = _versions_view(
            tokens, filters, artist, album, str(pressing_id or "")
        )
        chosen = next((t for t in tokens if t.id == pressing_id), None)
        if chosen is not None:
            selection["preview"] = _preview_view(chosen)
    return selection


# ── HTMX fragments ────────────────────────────────────────────────────────────


@catalogue_browser_router.get("/browse/artist", response_class=HTMLResponse)
async def browse_artist(
    request: Request,
    artist: str = Query(""),
    q: str = Query(""),
    fmt: str = Query(""),
    genres: str = Query(""),
    year_from: str = Query(""),
    year_to: str = Query(""),
    country_q: str = Query(""),
) -> HTMLResponse:
    """Centre panel for one artist; also empties the preview (out of band)."""
    if not artist:
        raise HTTPException(status_code=404, detail="Not found")
    filters = BrowseFilters.clean(
        q=q, fmt=fmt, genres=genres, year_from=year_from, year_to=year_to, country_q=country_q
    )
    async with _sf(request)() as db:
        selection = await _load_selection(db, filters, artist, None, None)
    ctx = _ctx(
        request,
        filters=filters,
        view=selection["view"],
        versions=selection["versions"],
        db_error=False,
        oob_center=True,
    )
    return _tmpl().TemplateResponse(
        request=request, name="partials/browse_center.html", context=ctx
    )


@catalogue_browser_router.get("/browse/album", response_class=HTMLResponse)
async def browse_album(
    request: Request,
    artist: str = Query(""),
    album: str = Query(""),
    q: str = Query(""),
    fmt: str = Query(""),
    genres: str = Query(""),
    year_from: str = Query(""),
    year_to: str = Query(""),
    country_q: str = Query(""),
) -> HTMLResponse:
    """Versions table of one album; also empties the preview (out of band)."""
    if not artist:
        raise HTTPException(status_code=404, detail="Not found")
    filters = BrowseFilters.clean(
        q=q, fmt=fmt, genres=genres, year_from=year_from, year_to=year_to, country_q=country_q
    )
    async with _sf(request)() as db:
        tokens = await _versions(db, filters, artist, album)
    ctx = _ctx(
        request, versions=_versions_view(tokens, filters, artist, album, ""), oob_versions=True
    )
    return _tmpl().TemplateResponse(
        request=request, name="partials/browse_versions.html", context=ctx
    )


@catalogue_browser_router.get("/{token_id}/preview", response_class=HTMLResponse)
async def pressing_preview(request: Request, token_id: str) -> HTMLResponse:
    """Right-panel preview of one pressing."""
    tid = _parse_uuid(token_id)
    if tid is None:
        raise HTTPException(status_code=404, detail="Not found")
    async with _sf(request)() as db:
        token = await _load_pressing(db, tid)
    if token is None:
        raise HTTPException(status_code=404, detail="Not found")
    ctx = _ctx(request, preview=_preview_view(token))
    return _tmpl().TemplateResponse(
        request=request, name="partials/pressing_preview.html", context=ctx
    )


# ── Pressing page: breadcrumb, back link, previous / next ─────────────────────


def safe_back(value: str) -> str | None:
    """A local catalogue or search URL to return to, or None.

    Anything with a scheme or host, a protocol-relative ``//`` start or a
    backslash is refused, so the back link never leads off the site.
    """
    value = value.strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value or len(value) > 2000:
        return None
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc or parts.path not in _BACK_PATHS:
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _referer_back(request: Request) -> str | None:
    """The page this one was opened from, when that is a catalogue or search page here."""
    ref = urllib.parse.urlsplit(request.headers.get("referer", ""))
    if not ref.netloc or ref.netloc != request.url.netloc:
        return None
    return safe_back(ref.path + (f"?{ref.query}" if ref.query else ""))


def _select_pressing(url: str, artist: str, album: str, pressing: str) -> str:
    """Point a browser URL for this album at ``pressing``; any other URL is kept as is."""
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    params = dict(pairs)
    if (
        parts.path != "/catalogue"
        or params.get("artist") != artist
        or params.get("album") != album
        or params.get("pressing") == pressing
    ):
        return url
    out: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "pressing":
            continue
        out.append((key, value))
        if key == "album":
            out.append(("pressing", pressing))
    return "/catalogue?" + urllib.parse.urlencode(out)


async def detail_navigation(request: Request, token: Token) -> dict[str, Any]:
    """Breadcrumb, back link and previous / next pressing of the album for the pressing page.

    The back link returns to the page the pressing was opened from (the
    ``back`` parameter, else the Referer, if it is a catalogue or search page),
    with this pressing selected; otherwise to the album in the browser.
    """
    artist = token.artist or ""
    album = token.title or ""
    tid = str(token.id)
    back = safe_back(request.query_params.get("back", "")) or _referer_back(request)
    if back:
        back = _select_pressing(back, artist, album, tid)
    else:
        back = browse_url(BrowseFilters(), artist, album, tid) if artist else "/catalogue"
    back_parts = urllib.parse.urlsplit(back)
    back_params = dict(urllib.parse.parse_qsl(back_parts.query, keep_blank_values=True))
    if back_parts.path == "/search":
        back_label = "Back to search results"
    elif back_params.get("artist") and "album" in back_params:
        back_artist = display_artist(back_params["artist"])
        back_label = f"Back to {back_artist} · {back_params['album'] or 'Untitled'}"
    elif back_params.get("artist"):
        back_label = f"Back to {display_artist(back_params['artist'])}"
    else:
        back_label = "Back to the catalogue"
    nav: dict[str, Any] = {
        "label": pressing_label(token),
        "artist_url": browse_url(BrowseFilters(), artist) if artist else "",
        "album_url": browse_url(BrowseFilters(), artist, album) if artist else "",
        "back_url": back,
        "back_label": back_label,
        "prev": None,
        "next": None,
        "position": 0,
        "total": 0,
    }
    if not artist:
        return nav

    # Step through the pressings the way the versions table listed them: with
    # the filters of the browser view we came from, unless they exclude this one.
    filters = (
        BrowseFilters.from_query(back_parts.query)
        if back_parts.path == "/catalogue"
        else BrowseFilters()
    )
    try:
        async with _sf(request)() as db:
            siblings = await _versions(db, filters, artist, album)
            if filters.active and token.id not in {s.id for s in siblings}:
                siblings = await _versions(db, BrowseFilters(), artist, album)
    except (SQLAlchemyError, OSError) as exc:  # query failed, or Postgres is unreachable
        logger.warning("Pressing navigation skipped: %s", exc)
        return nav
    ids = [s.id for s in siblings]
    if token.id not in ids:
        return nav
    i = ids.index(token.id)
    back_param = urllib.parse.quote(back, safe="")

    def link(sibling: Token) -> dict[str, str]:
        return {
            "url": f"/catalogue/{sibling.id}?back={back_param}",
            "label": pressing_label(sibling),
        }

    nav["position"] = i + 1
    nav["total"] = len(ids)
    nav["prev"] = link(siblings[i - 1]) if i > 0 else None
    nav["next"] = link(siblings[i + 1]) if i + 1 < len(ids) else None
    return nav
