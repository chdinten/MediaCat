"""MusicBrainz API connector.

Fetches release data from the MusicBrainz Web Service 2 (JSON) and
normalises it into MediaCat's domain vocabulary.

Authentication
--------------
MusicBrainz does not require authentication but enforces a rate limit
of 1 request per second for anonymous users.  A meaningful ``User-Agent``
header is mandatory.
"""

from __future__ import annotations

import logging
from typing import Any

from mediacat.ingestion.base import BaseConnector, FetchResult

logger = logging.getLogger(__name__)


class MusicBrainzConnector(BaseConnector):
    """Connector for the MusicBrainz Web Service 2."""

    async def fetch_release(self, external_id: str) -> FetchResult:
        """Fetch a release by MusicBrainz release MBID."""
        raw = await self._request(
            "GET",
            f"/release/{external_id}",
            params={
                "inc": "labels+artists+recordings+release-groups+genres+tags",
                "fmt": "json",
            },
        )
        return self._normalise(raw)

    async def fetch_artist_releases(self, mbid: str) -> list[dict[str, Any]]:
        """Return release groups for an artist sorted by year."""
        raw = await self._request(
            "GET",
            f"/artist/{mbid}",
            params={"inc": "release-groups", "fmt": "json", "limit": 100},
        )
        groups = raw.get("release-groups", [])
        result: list[dict[str, Any]] = []
        for rg in groups:
            rg_type = (rg.get("primary-type") or "").lower()
            if rg_type not in ("album", "ep", "single", ""):
                continue
            year = _extract_year(rg.get("first-release-date"))
            result.append(
                {
                    "title": rg.get("title", ""),
                    "year": year,
                    "type": rg.get("primary-type", ""),
                    "mbid": rg.get("id", ""),
                }
            )
        result.sort(key=lambda r: r.get("year") or 9999)
        return result

    async def search_artist(self, name: str) -> list[dict[str, Any]]:
        """Search for artists by name; returns top matches with MBID."""
        raw = await self._request(
            "GET",
            "/artist",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": 5},
        )
        return [
            {
                "mbid": a.get("id", ""),
                "name": a.get("name", ""),
                "disambiguation": a.get("disambiguation", ""),
                "type": a.get("type", ""),
                "score": a.get("score", 0),
            }
            for a in raw.get("artists", [])
        ]

    async def resolve_artist(self, name: str) -> dict[str, Any] | None:
        """Return the best-matching raw MB artist dict for *name*, or None.

        Returns the raw MusicBrainz artist object (MBID is at key ``"id"``).
        Uses exact-name match, group-type preference, and score to rank results.
        """
        raw = await self._request(
            "GET",
            "/artist",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": 5},
        )
        artists = raw.get("artists", [])
        if not artists:
            return None

        def rank(a: dict[str, Any]) -> tuple[int, int, int]:
            exact = int(a.get("name", "").lower() == name.lower())
            is_group = int(a.get("type") == "Group")
            score = int(a.get("score", 0))
            return exact, is_group, score

        best: dict[str, Any] = sorted(artists, key=rank, reverse=True)[0]
        return best

    async def get_artist(
        self,
        mbid: str,
        *,
        inc: str = "aliases+url-rels+genres+tags",
    ) -> dict[str, Any]:
        """Fetch a full artist record by MBID, including URL relations."""
        return await self._request(
            "GET",
            f"/artist/{mbid}",
            params={"fmt": "json", "inc": inc},
        )

    async def search_releases(self, query: str, **kwargs: Any) -> list[FetchResult]:
        """Search for releases via the MusicBrainz search API."""
        params: dict[str, Any] = {"query": query, "fmt": "json", "limit": 25, **kwargs}
        raw = await self._request("GET", "/release", params=params)
        results: list[FetchResult] = []
        for item in raw.get("releases", []):
            results.append(self._normalise(item, is_search=True))
        return results

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def _normalise(
        self,
        raw: dict[str, Any],
        *,
        is_search: bool = False,
    ) -> FetchResult:
        """Map MusicBrainz JSON to MediaCat vocabulary."""
        # Artist
        artist_credit = raw.get("artist-credit", [])
        artist = _join_artist_credit(artist_credit)

        # Label / catalog number
        label_info = raw.get("label-info", [])
        label_name = None
        catalog_number = None
        if label_info:
            first = label_info[0]
            label_name = first.get("label", {}).get("name")
            catalog_number = first.get("catalog-number")

        # Country
        country = raw.get("country")

        # Media format
        media_list = raw.get("media", [])
        media_format = _mb_format_to_media(media_list)

        # Barcode
        barcode = raw.get("barcode") or None

        # Release group for year
        rg = raw.get("release-group", {})
        year = _extract_year(raw.get("date") or rg.get("first-release-date"))

        # Genres from tags (MusicBrainz uses the "genres" and "tags" arrays)
        genres_raw = raw.get("genres", []) or raw.get("tags", [])
        genres = [
            g.get("name", "").title()
            for g in sorted(genres_raw, key=lambda x: x.get("count", 0), reverse=True)
            if g.get("name")
        ]

        mbid = raw.get("id", "")
        normalised: dict[str, Any] = {
            "title": raw.get("title", ""),
            "artist": artist,
            "year": year,
            "country": country,
            "media_format": media_format,
            "barcode": barcode,
            "catalog_number": catalog_number,
            "label_name": label_name,
            "genres": genres,
            "tracklist": _extract_tracklist(media_list),
            "musicbrainz_release_url": f"https://musicbrainz.org/release/{mbid}" if mbid else None,
        }

        return FetchResult(
            source="musicbrainz",
            external_id=mbid,
            raw_payload=raw,
            normalised=normalised,
            image_urls=[],  # MusicBrainz doesn't embed images; use CAA
            confidence=0.9 if not is_search else 0.75,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_artist_credit(credit_list: list[dict[str, Any]]) -> str:
    """Join artist-credit array into a single string."""
    parts: list[str] = []
    for entry in credit_list:
        name = entry.get("name") or entry.get("artist", {}).get("name", "")
        if name:
            parts.append(name)
        joinphrase = entry.get("joinphrase", "")
        if joinphrase:
            parts.append(joinphrase)
    return "".join(parts).strip()


def _mb_format_to_media(media_list: list[dict[str, Any]]) -> str:
    """Map MusicBrainz medium format to MediaCat media_format."""
    for medium in media_list:
        fmt = (medium.get("format") or "").lower()
        if "vinyl" in fmt or "lp" in fmt or '12"' in fmt or '7"' in fmt:
            return "vinyl"
        if "cd" in fmt or "compact disc" in fmt:
            return "cd"
    return "vinyl"


def _extract_year(date_str: str | None) -> int | None:
    """Extract the year from a date string like '1983-06-15' or '1983'."""
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, IndexError):
        return None


def _extract_tracklist(media_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten MusicBrainz media list into [{disc, position, title, artists, duration_ms}]."""
    tracks: list[dict[str, Any]] = []
    for disc_idx, medium in enumerate(media_list, start=1):
        disc = medium.get("position") or disc_idx
        for track in medium.get("tracks", medium.get("track-list", [])):
            recording = track.get("recording", {})
            # Artist credits may be on the track or on the recording
            credit = track.get("artist-credit") or recording.get("artist-credit", [])
            track_artists = [
                (c.get("name") or c.get("artist", {}).get("name", "")).strip()
                for c in credit
                if isinstance(c, dict) and (c.get("name") or c.get("artist", {}).get("name"))
            ]
            tracks.append(
                {
                    "disc": disc,
                    "position": track.get("position") or track.get("number"),
                    "title": track.get("title") or recording.get("title", ""),
                    "artists": track_artists,
                    "duration_ms": track.get("length") or recording.get("length"),
                }
            )
    return tracks
