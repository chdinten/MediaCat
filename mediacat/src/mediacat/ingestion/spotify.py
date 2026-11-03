"""Spotify Web API client — album URL discovery and enrichment.

Scoped to the client-credentials flow (no user login required).  Used
exclusively to attach Spotify album page URLs to discography entries and
cached Token rows.

Credentials are read from SiteSetting at the call-site; the client itself
only stores the resolved client_id / client_secret pair.  Access tokens
are cached module-level, keyed by (client_id, client_secret), and
refreshed automatically 30 seconds before expiry.

Rate limiting
-------------
Spotify's client-credentials tier allows generous concurrency, but we
cap outbound requests conservatively at one every two seconds (≤ 30/min)
via an instance-level monotonic clock check.  Each SpotifyClient instance
maintains its own ``_last_request_at`` timestamp; concurrent FastAPI
requests each hold their own instance, so limits are per-request, which
is safe.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
_API_BASE = "https://api.spotify.com/v1"
_USER_AGENT = "MediaCat/1.0 (+https://mediacat.localhost)"
_TIMEOUT = 20.0
_MIN_INTERVAL = 2.0  # seconds between requests → ≤ 30 req/min

# Module-level token cache: (client_id, client_secret) → (access_token, expires_at_monotonic)
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


class SpotifyClient:
    """Async Spotify Web API client scoped to album URL discovery."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._last_request_at: float = 0.0

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _access_token(self) -> str:
        """Return a valid access token, refreshing from the cache if needed."""
        cache_key = (self._client_id, self._client_secret)
        cached = _token_cache.get(cache_key)
        if cached:
            token, expires_at = cached
            if time.monotonic() < expires_at - 30:
                return token

        raw = f"{self._client_id}:{self._client_secret}".encode()
        auth = base64.b64encode(raw).decode()
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}
        ) as client:
            r = await client.post(
                _ACCOUNTS_URL,
                headers={"Authorization": f"Basic {auth}"},
                data={"grant_type": "client_credentials"},
            )
            r.raise_for_status()
            data: dict[str, Any] = r.json()

        token = str(data["access_token"])
        expires_in = int(data.get("expires_in", 3600))
        _token_cache[cache_key] = (token, time.monotonic() + expires_in)
        logger.debug("Spotify: new access token acquired (expires in %ds)", expires_in)
        return token

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    async def _throttle(self) -> None:
        wait = _MIN_INTERVAL - (time.monotonic() - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._throttle()
        token = await self._access_token()
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}
        ) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", 5))
                logger.warning("Spotify: 429 rate-limited, retrying after %.0fs", retry_after)
                await asyncio.sleep(retry_after)
                # One retry after back-off
                r = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
            if not r.is_success:
                logger.debug(
                    "Spotify: %s %s → %d %s",
                    "GET",
                    r.url,
                    r.status_code,
                    r.text[:200],
                )
            r.raise_for_status()
            result: dict[str, Any] = r.json()
        self._last_request_at = time.monotonic()
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve_artist(self, name: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Return (best_match_or_None, all_candidates) for *name*.

        Prefers exact case-insensitive name match; among ties picks highest
        popularity score.  Callers that only need the artist can do:
            artist, _ = await client.resolve_artist(name)
        """
        data = await self._get(
            f"{_API_BASE}/search",
            params={"q": name, "type": "artist", "limit": 10},
        )
        artists: list[dict[str, Any]] = data.get("artists", {}).get("items", [])
        exact = [a for a in artists if a["name"].casefold() == name.casefold()]
        if not exact:
            logger.info(
                "Spotify: no exact match for %r (candidates: %s)",
                name,
                [a["name"] for a in artists[:3]],
            )
            return None, artists
        return max(exact, key=lambda a: a.get("popularity", 0)), artists

    async def search_albums(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Raw album objects for a search *query* (``upc:…`` or ``album:… artist:…``).

        No ``market`` parameter (see the note on :meth:`fetch_artist_albums`);
        callers check ``available_markets`` themselves.
        """
        data = await self._get(
            f"{_API_BASE}/search",
            params={"q": query, "type": "album", "limit": limit},
        )
        items: list[dict[str, Any]] = data.get("albums", {}).get("items", []) or []
        return [i for i in items if isinstance(i, dict)]

    async def fetch_artist_albums(
        self,
        artist_id: str,
    ) -> list[dict[str, Any]]:
        """Return all albums for *artist_id*.

        Each entry: {title, year, type, spotify_album_id, spotify_url}.
        Paginates through all pages; deduplicates by Spotify album ID.

        Omits include_groups so Spotify returns all release types (album,
        single, compilation, appears_on) — avoids comma-encoding issues with
        httpx.  album_type in each item identifies the type.  Pagination URLs
        from data["next"] are used verbatim.
        """
        albums: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        base_url = f"{_API_BASE}/artists/{artist_id}/albums"
        next_url: str | None = None

        while True:
            if next_url:
                data = await self._get(next_url)
            else:
                data = await self._get(base_url)
            for item in data.get("items", []):
                album_id = item.get("id", "")
                if album_id in seen_ids:
                    continue
                seen_ids.add(album_id)
                albums.append(
                    {
                        "title": item.get("name", ""),
                        "year": (item.get("release_date") or "")[:4] or None,
                        "type": item.get("album_type", ""),
                        "spotify_album_id": album_id,
                        "spotify_url": (item.get("external_urls") or {}).get("spotify", ""),
                    }
                )
            next_url = data.get("next")  # full URL from Spotify, used verbatim
            if not next_url:
                break

        logger.info("Spotify: fetched %d albums for artist_id=%s", len(albums), artist_id)
        return albums

    async def build_album_url_map(self, artist_name: str) -> dict[str, str]:
        """Return {title.casefold(): spotify_url} for all Spotify albums of *artist_name*.

        Returns an empty dict if the artist is not found or Spotify returns no
        albums.
        """
        artist, _ = await self.resolve_artist(artist_name)
        if not artist:
            return {}
        albums = await self.fetch_artist_albums(artist["id"])
        return {a["title"].casefold(): a["spotify_url"] for a in albums if a.get("spotify_url")}
