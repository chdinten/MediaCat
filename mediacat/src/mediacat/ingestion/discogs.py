"""Discogs API connector.

Fetches release data from the Discogs database API and normalises it
into MediaCat's domain vocabulary.

Authentication
--------------
Discogs personal access tokens are read from a Docker secret file.
The token is sent as ``Authorization: Discogs token=<value>``.

Rate limiting
-------------
Authenticated Discogs requests allow 60/min.  We default to 55 with
headroom managed by the base class token bucket.
"""

from __future__ import annotations

import logging
from typing import Any

from mediacat.ingestion.base import BaseConnector, FetchResult

logger = logging.getLogger(__name__)


class DiscogsConnector(BaseConnector):
    """Connector for the Discogs database API."""

    async def fetch_release(self, external_id: str) -> FetchResult:
        """Fetch a release by Discogs release ID."""
        raw = await self._request("GET", f"/releases/{external_id}")
        return self._normalise(raw)

    async def fetch_master(self, master_id: str) -> FetchResult:
        """Fetch a master release by Discogs master ID."""
        raw = await self._request("GET", f"/masters/{master_id}")
        return self._normalise(raw, is_master=True)

    async def search_releases(self, query: str, **kwargs: Any) -> list[FetchResult]:
        """Search the Discogs database for releases.

        Parameters
        ----------
        query
            Free-text search string.
        kwargs
            Extra params forwarded to the Discogs search endpoint
            (e.g. ``type="release"``, ``format="Vinyl"``).
        """
        params: dict[str, Any] = {"q": query, "type": "release", **kwargs}
        raw = await self._request("GET", "/database/search", params=params)
        results: list[FetchResult] = []
        for item in raw.get("results", []):
            results.append(self._normalise_search_result(item))
        return results

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def _normalise(
        self,
        raw: dict[str, Any],
        *,
        is_master: bool = False,  # noqa: ARG002
    ) -> FetchResult:
        """Map Discogs JSON to MediaCat vocabulary."""
        # Extract label info
        labels_raw = raw.get("labels", [])
        label_name = labels_raw[0].get("name") if labels_raw else None
        catalog_number = labels_raw[0].get("catno") if labels_raw else None

        # Extract format → media_format
        formats = raw.get("formats", [])
        media_format = _discogs_format_to_media(formats)

        # Extract pressing plant / companies
        companies = raw.get("companies", [])
        manufacturer = _extract_manufacturer(companies)

        # Images
        images = raw.get("images", [])
        image_urls = [img["uri"] for img in images if img.get("uri")]

        normalised: dict[str, Any] = {
            "title": raw.get("title", ""),
            "artist": _join_artists(raw.get("artists", [])),
            "year": raw.get("year"),
            "country": raw.get("country"),
            "media_format": media_format,
            "barcode": _first_identifier(raw, "Barcode"),
            "catalog_number": catalog_number,
            "matrix_runout": _first_identifier(raw, "Matrix / Runout"),
            "label_name": label_name,
            "manufacturer_name": manufacturer,
            "genres": raw.get("genres", []),
            "styles": raw.get("styles", []),
            "tracklist": raw.get("tracklist", []),
            "notes": raw.get("notes", ""),
        }

        ext_id = str(raw.get("id", ""))
        return FetchResult(
            source="discogs",
            external_id=ext_id,
            raw_payload=raw,
            normalised=normalised,
            image_urls=image_urls,
        )

    def _normalise_search_result(self, item: dict[str, Any]) -> FetchResult:
        """Normalise a search result snippet (less data than a full release)."""
        normalised: dict[str, Any] = {
            "title": item.get("title", ""),
            "year": item.get("year"),
            "country": item.get("country"),
            "media_format": item.get("format", [""])[0] if item.get("format") else None,
            "label_name": item.get("label", [""])[0] if item.get("label") else None,
            "catalog_number": item.get("catno"),
        }
        image_urls = [item["cover_image"]] if item.get("cover_image") else []
        return FetchResult(
            source="discogs",
            external_id=str(item.get("id", "")),
            raw_payload=item,
            normalised=normalised,
            image_urls=image_urls,
            confidence=0.8,  # search snippets are lower confidence
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_artists(artists: list[dict[str, Any]]) -> str:
    """Join artist names with their join strings (e.g. ' & ')."""
    parts: list[str] = []
    for a in artists:
        name = a.get("name", "").strip()
        if name:
            parts.append(name)
        join = a.get("join", "").strip()
        if join and join != ",":
            parts.append(join)
    return " ".join(parts)


def _first_identifier(raw: dict[str, Any], id_type: str) -> str | None:
    """Extract the first identifier of *id_type* from the identifiers list."""
    for ident in raw.get("identifiers", []):
        if ident.get("type") == id_type:
            val = ident.get("value")
            return str(val) if val is not None else None
    return None


def _discogs_format_to_media(formats: list[dict[str, Any]]) -> str:
    """Map Discogs format names to MediaCat media_format."""
    for fmt in formats:
        name = (fmt.get("name") or "").lower()
        if "vinyl" in name or "lp" in name or "12" in name or "7" in name or "10" in name:
            return "vinyl"
        if "cd" in name or "compact disc" in name:
            return "cd"
    return "vinyl"  # default assumption


def _extract_manufacturer(companies: list[dict[str, Any]]) -> str | None:
    """Extract pressing plant name from the companies array."""
    for comp in companies:
        role = (comp.get("entity_type_name") or "").lower()
        if "pressed" in role or "manufactured" in role or "plant" in role:
            return comp.get("name")
    return None
