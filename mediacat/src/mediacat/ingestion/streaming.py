"""Streaming-service album links: Spotify, Deezer and Apple Music.

MediaCat stores no streaming logins.  For each album it only finds the
public album page; playback happens in the user's own app or web player.

Lookups per service
-------------------
* **Spotify** — client credentials (SiteSetting ``spotify_client_id`` /
  ``spotify_client_secret``) through :class:`~mediacat.ingestion.spotify.SpotifyClient`.
  Search ``upc:<code>``, then ``album:<title> artist:<artist>``.  No
  ``market`` parameter (Spotify answers it with misleading 400s); the
  storefront is checked against ``available_markets`` instead.
* **Deezer** — public API, no key: ``/album/upc:<code>``, then
  ``/search/album``.  Deezer has no country filter.
* **Apple Music** — the free iTunes Search API, no key:
  ``/lookup?upc=``, then ``/search?entity=album``, both with ``country=``.
  About 20 calls a minute are allowed, hence the 3 s throttle.

Matching
--------
Barcodes first (``Token.barcode`` and the Discogs "Barcode" identifiers),
tried as UPC-12 and EAN-13; a barcode hit still needs the artist to match.
Then a text search, accepted only when the artist matches and the title
matches after :func:`match_title` (case, accents, a leading "The", edition
suffixes such as "(Remastered 2011)" or "- Deluxe Edition"), or nearly so
(:data:`MIN_SCORE`).  Anything less is "not found": no icon is better than
a wrong album.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import unicodedata
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from mediacat.ingestion import spotify as spotify_mod

logger = logging.getLogger(__name__)

SERVICES: tuple[str, ...] = ("spotify", "deezer", "apple")
SERVICE_LABELS: dict[str, str] = {
    "spotify": "Spotify",
    "deezer": "Deezer",
    "apple": "Apple Music",
}
# Services whose API needs an account (credentials in Admin → API Tokens).
NEEDS_CREDENTIALS: frozenset[str] = frozenset({"spotify"})

DEFAULT_COUNTRY = "CH"
MIN_SCORE = 0.88  # below this a text-search hit is not shown
MAX_BARCODES = 3  # distinct barcodes tried per album and service

_DEEZER_API = "https://api.deezer.com"
_ITUNES_API = "https://itunes.apple.com"
_USER_AGENT = "MediaCat/1.0 (+https://mediacat.localhost)"
_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_DISCOGS_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")
_EDITION_WORDS = (
    r"remaster(?:ed)?|remix(?:ed)?|deluxe|expanded|anniversary|edition|bonus|reissue|legacy|"
    r"collector'?s|super|special|mono|stereo|version|mix|explicit|clean"
)
_EDITION_BRACKET_RE = re.compile(
    rf"\s*[\(\[][^\)\]]*\b(?:{_EDITION_WORDS})\b[^\)\]]*[\)\]]", re.IGNORECASE
)
_EDITION_DASH_RE = re.compile(rf"\s+[-–—]\s+[^-–—]*\b(?:{_EDITION_WORDS})\b.*$", re.IGNORECASE)
_RELEASE_TYPE_RE = re.compile(r"\s+[-–—]\s+(?:ep|single)\s*$", re.IGNORECASE)
_LEADING_THE_RE = re.compile(r"^the\s+")
_NON_ALNUM_RE = re.compile(r"[^\w]+")
_SPACE_RE = re.compile(r"\s+")


def _fold(text: str) -> str:
    """Casefold, strip accents and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACE_RE.sub(" ", stripped.casefold()).strip()


def strip_discogs_suffix(name: str) -> str:
    """Drop the Discogs disambiguation suffix: ``"Nirvana (2)"`` → ``"Nirvana"``."""
    return _DISCOGS_SUFFIX_RE.sub("", name.strip()).rstrip("*").strip()


def artist_key(name: str) -> str:
    """Storage key of an artist: folded, Discogs "(N)" suffix dropped."""
    return _fold(strip_discogs_suffix(name or ""))


def album_key(title: str) -> str:
    """Storage key of an album title (folded only: editions stay distinct albums)."""
    return _fold(title or "")


def _loose(text: str) -> str:
    text = _fold(text).replace("&", " and ")
    text = _NON_ALNUM_RE.sub(" ", text).replace("_", " ")
    text = _SPACE_RE.sub(" ", text).strip()
    return _LEADING_THE_RE.sub("", text)


def search_title(title: str) -> str:
    """The title without edition suffixes, as sent to a search API."""
    text = _EDITION_BRACKET_RE.sub("", title or "")
    text = _EDITION_DASH_RE.sub("", text)
    return _RELEASE_TYPE_RE.sub("", text).strip() or (title or "").strip()


def match_title(title: str) -> str:
    """Comparison form of an album title (edition suffixes and punctuation dropped)."""
    return _loose(search_title(title))


def match_artist(name: str) -> str:
    """Comparison form of an artist name."""
    return _loose(strip_discogs_suffix(name or ""))


def barcode_variants(raw: str) -> list[str]:
    """Digits of a barcode as UPC-12 and EAN-13 (``0`` + UPC), or ``[]`` if it isn't one."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12:
        return [digits, "0" + digits]
    if len(digits) == 13:
        return [digits, digits[1:]] if digits.startswith("0") else [digits]
    if len(digits) in (8, 14):
        return [digits]
    return []


def collect_barcodes(barcode: str | None, extra: dict[str, Any] | None) -> list[str]:
    """Barcodes of one pressing: the ``barcode`` column, then Discogs "Barcode" identifiers."""
    found: list[str] = []
    if barcode:
        found.append(barcode)
    discogs = (extra or {}).get("discogs")
    idents = discogs.get("identifiers") if isinstance(discogs, dict) else None
    for ident in idents or []:
        if isinstance(ident, dict) and str(ident.get("type", "")).casefold() == "barcode":
            found.append(str(ident.get("value") or ""))
    out: list[str] = []
    for code in found:
        digits = re.sub(r"\D", "", code)
        if barcode_variants(digits) and digits not in out:
            out.append(digits)
    return out


# ---------------------------------------------------------------------------
# Candidates and scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlbumQuery:
    """One album to look up (artist and title as catalogued)."""

    artist: str
    title: str
    year: int | None = None
    barcodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    """One album returned by a service."""

    external_id: str
    url: str
    title: str
    artists: tuple[str, ...]
    year: int | None = None
    markets: tuple[str, ...] | None = None  # Spotify only; None = unknown


@dataclass(frozen=True)
class Match:
    """A confident match: the album page to link to."""

    url: str
    external_id: str
    method: str  # "barcode" | "search"
    score: float


def artist_matches(query_artist: str, artists: Iterable[str]) -> bool:
    want = match_artist(query_artist)
    return bool(want) and any(match_artist(a) == want for a in artists)


def title_score(query_title: str, candidate_title: str) -> float:
    """1.0 same title, 0.97 same after edition suffixes are dropped, else 0.95 × similarity."""
    if album_key(query_title) == album_key(candidate_title):
        return 1.0
    want, got = match_title(query_title), match_title(candidate_title)
    if not want or not got:
        return 0.0
    if want == got:
        return 0.97
    return round(0.95 * difflib.SequenceMatcher(None, want, got).ratio(), 4)


def best_match(query: AlbumQuery, candidates: Iterable[Candidate], country: str) -> Match | None:
    """The best text-search candidate, or None when none is confident."""
    ranked: list[tuple[float, int, int, Candidate]] = []
    for cand in candidates:
        if cand.markets is not None and country not in cand.markets:
            continue
        if not artist_matches(query.artist, cand.artists):
            continue
        score = title_score(query.title, cand.title)
        if score < MIN_SCORE:
            continue
        year_gap = abs(cand.year - query.year) if cand.year and query.year else 99
        ranked.append((score, -year_gap, -len(cand.title), cand))
    if not ranked:
        return None
    score, _gap, _len, cand = max(ranked, key=lambda r: r[:3])
    return Match(url=cand.url, external_id=cand.external_id, method="search", score=score)


def barcode_match(query: AlbumQuery, candidates: Iterable[Candidate], country: str) -> Match | None:
    """A barcode hit counts when the artist matches (barcodes get mistyped)."""
    for cand in candidates:
        if cand.markets is not None and country not in cand.markets:
            continue
        if artist_matches(query.artist, cand.artists):
            return Match(url=cand.url, external_id=cand.external_id, method="barcode", score=1.0)
    return None


def _year(value: Any) -> int | None:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_spotify_albums(items: Iterable[Any]) -> list[Candidate]:
    out: list[Candidate] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        markets = item.get("available_markets")
        out.append(
            Candidate(
                external_id=str(item["id"]),
                url=f"https://open.spotify.com/album/{item['id']}",
                title=str(item.get("name") or ""),
                artists=tuple(
                    str(a.get("name") or "")
                    for a in item.get("artists") or []
                    if isinstance(a, dict)
                ),
                year=_year(item.get("release_date")),
                markets=tuple(markets) if isinstance(markets, list) else None,
            )
        )
    return out


def _deezer_candidate(item: dict[str, Any]) -> Candidate | None:
    if not item.get("id"):
        return None
    artist = item.get("artist")
    names = [str(artist.get("name") or "")] if isinstance(artist, dict) else []
    for contributor in item.get("contributors") or []:
        if isinstance(contributor, dict) and contributor.get("name"):
            names.append(str(contributor["name"]))
    return Candidate(
        external_id=str(item["id"]),
        url=f"https://www.deezer.com/album/{item['id']}",
        title=str(item.get("title") or ""),
        artists=tuple(names),
        year=_year(item.get("release_date")),
    )


def parse_deezer_album(data: Any) -> list[Candidate]:
    """``/album/upc:<code>``: one album, or ``{"error": …}`` when the code is unknown."""
    if not isinstance(data, dict) or "error" in data:
        return []
    cand = _deezer_candidate(data)
    return [cand] if cand else []


def parse_deezer_search(data: Any) -> list[Candidate]:
    items = data.get("data") if isinstance(data, dict) else None
    return [c for c in (_deezer_candidate(i) for i in items or [] if isinstance(i, dict)) if c]


def parse_itunes(data: Any) -> list[Candidate]:
    """iTunes ``lookup`` / ``search`` results (albums only; the query string is dropped)."""
    out: list[Candidate] = []
    results = data.get("results") if isinstance(data, dict) else None
    for item in results or []:
        if not isinstance(item, dict) or item.get("wrapperType") != "collection":
            continue
        url = str(item.get("collectionViewUrl") or "").split("?", 1)[0]
        if not item.get("collectionId") or not url:
            continue
        out.append(
            Candidate(
                external_id=str(item["collectionId"]),
                url=url.replace("https://itunes.apple.com/", "https://music.apple.com/", 1),
                title=str(item.get("collectionName") or ""),
                artists=(str(item.get("artistName") or ""),),
                year=_year(item.get("releaseDate")),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Pasted links (manual corrections)
# ---------------------------------------------------------------------------

_SPOTIFY_URL_RE = re.compile(
    r"^(?:https://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?album/|spotify:album:)"
    r"([A-Za-z0-9]{22})(?:[/?#].*)?$"
)
_DEEZER_URL_RE = re.compile(
    r"^https://(?:www\.)?deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?album/(\d+)(?:[/?#].*)?$"
)
_APPLE_URL_RE = re.compile(
    r"^https://(?:music|itunes)\.apple\.com/([a-z]{2})/album/(?:([^/?#]+)/)?(?:id)?(\d+)(?:[/?#].*)?$"
)


def parse_service_url(service: str, url: str) -> tuple[str, str] | None:
    """``(canonical_url, external_id)`` of a pasted album link, or None if it isn't one."""
    url = (url or "").strip()
    if service == "spotify" and (m := _SPOTIFY_URL_RE.match(url)):
        return f"https://open.spotify.com/album/{m.group(1)}", m.group(1)
    if service == "deezer" and (m := _DEEZER_URL_RE.match(url)):
        return f"https://www.deezer.com/album/{m.group(1)}", m.group(1)
    if service == "apple" and (m := _APPLE_URL_RE.match(url)):
        country, slug, album_id = m.groups()
        slug_part = f"{urllib.parse.quote(slug, safe='%-')}/" if slug else ""
        return f"https://music.apple.com/{country}/album/{slug_part}{album_id}", album_id
    return None


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


class Throttle:
    """At most one call per *interval* seconds across the whole process."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock: asyncio.Lock | None = None
        self._last = 0.0

    async def wait(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


THROTTLES: dict[str, Throttle] = {
    "spotify": Throttle(1.0),
    "deezer": Throttle(0.15),  # quota: 50 calls per 5 s
    "apple": Throttle(3.1),  # quota: about 20 calls per minute
}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def _get_json(service: str, url: str, params: dict[str, Any] | None = None) -> Any:
    await THROTTLES[service].wait()
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _codes(query: AlbumQuery) -> list[str]:
    codes: list[str] = []
    for barcode in query.barcodes[:MAX_BARCODES]:
        for code in barcode_variants(barcode):
            if code not in codes:
                codes.append(code)
    return codes


async def find_spotify(
    query: AlbumQuery, country: str, credentials: dict[str, str]
) -> Match | None:
    client_id = credentials.get("spotify_client_id", "")
    client_secret = credentials.get("spotify_client_secret", "")
    if not client_id or not client_secret:
        return None
    client = spotify_mod.SpotifyClient(client_id, client_secret)

    async def search(q: str) -> list[Candidate]:
        await THROTTLES["spotify"].wait()
        return parse_spotify_albums(await client.search_albums(q))

    for code in _codes(query):
        if match := barcode_match(query, await search(f"upc:{code}"), country):
            return match
    title = search_title(query.title)
    artist = strip_discogs_suffix(query.artist)
    return best_match(query, await search(f"album:{title} artist:{artist}"), country)


async def find_deezer(
    query: AlbumQuery, country: str, _credentials: dict[str, str]
) -> Match | None:
    for code in _codes(query):
        data = await _get_json("deezer", f"{_DEEZER_API}/album/upc:{code}")
        if match := barcode_match(query, parse_deezer_album(data), country):
            return match
    q = f'artist:"{strip_discogs_suffix(query.artist)}" album:"{search_title(query.title)}"'
    data = await _get_json("deezer", f"{_DEEZER_API}/search/album", {"q": q, "limit": 25})
    return best_match(query, parse_deezer_search(data), country)


async def find_apple(query: AlbumQuery, country: str, _credentials: dict[str, str]) -> Match | None:
    for code in _codes(query):
        data = await _get_json(
            "apple",
            f"{_ITUNES_API}/lookup",
            {"upc": code, "entity": "album", "country": country},
        )
        if match := barcode_match(query, parse_itunes(data), country):
            return match
    term = f"{strip_discogs_suffix(query.artist)} {search_title(query.title)}"
    data = await _get_json(
        "apple",
        f"{_ITUNES_API}/search",
        {"term": term, "entity": "album", "media": "music", "country": country, "limit": 50},
    )
    return best_match(query, parse_itunes(data), country)


Finder = Callable[[AlbumQuery, str, dict[str, str]], Awaitable[Match | None]]

FINDERS: dict[str, Finder] = {
    "spotify": find_spotify,
    "deezer": find_deezer,
    "apple": find_apple,
}
