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

    async def fetch_marketplace_stats(self, release_id: str) -> dict[str, Any]:
        """Fetch marketplace stats (lowest price, num for sale) for a release.

        Returns keys: lowest_price, currency, num_for_sale, has_data.
        median_price is always None — the Discogs /marketplace/stats endpoint
        does not return a median price despite what older code assumed.
        """
        try:
            raw = await self._request("GET", f"/marketplace/stats/{release_id}")
            lowest = raw.get("lowest_price") or {}
            median = raw.get("median") or raw.get("median_price") or {}
            return {
                "lowest_price": lowest.get("value"),
                "median_price": median.get("value")
                if isinstance(median, dict)
                else (median if isinstance(median, (int, float)) else None),
                "currency": lowest.get("currency")
                or (median.get("currency") if isinstance(median, dict) else None),
                "num_for_sale": raw.get("num_for_sale"),
                "has_data": bool(raw),
            }
        except Exception as exc:
            logger.warning("fetch_marketplace_stats failed for release_id=%s: %s", release_id, exc)
            return {"has_data": False}

    async def fetch_price_suggestions(self, release_id: str) -> dict[str, Any]:
        """Fetch condition-graded price suggestions for a release.

        Discogs returns a dict keyed by condition name, e.g.:
          {"Very Good Plus (VG+)": {"currency": "USD", "value": 13.5}, ...}

        Returns empty dict if the endpoint is unavailable or returns no data.
        Requires a valid Discogs auth token.
        """
        try:
            raw = await self._request("GET", f"/marketplace/price_suggestions/{release_id}")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                logger.warning(
                    "fetch_price_suggestions: 403 Forbidden for release_id=%s — "
                    "Discogs requires a seller account for price suggestions",
                    release_id,
                )
            else:
                logger.warning(
                    "fetch_price_suggestions failed for release_id=%s (status=%s): %s",
                    release_id,
                    status,
                    exc,
                )
            return {}

        logger.debug(
            "price_suggestions raw keys for release_id=%s: %s",
            release_id,
            list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
        )

        if not isinstance(raw, dict) or not raw:
            logger.info("price_suggestions returned no data for release_id=%s", release_id)
            return {}

        condition_prices = {
            k: v.get("value") for k, v in raw.items() if isinstance(v, dict) and v.get("value")
        }
        currency = next(
            (v.get("currency") for v in raw.values() if isinstance(v, dict) and v.get("currency")),
            None,
        )
        logger.info(
            "price_suggestions for release_id=%s: %d conditions, currency=%s",
            release_id,
            len(condition_prices),
            currency,
        )
        return {"condition_prices": condition_prices, "currency": currency}

    async def fetch_user_collection(
        self, username: str, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """Fetch a page of the user's Discogs collection.

        Returns dict with keys: releases (list), pages (int), items (int).
        Each release has: release_id (str), title (str), artist (str), year (int|None),
        instance_id, folder_id and notes (custom collection field values).
        """
        try:
            raw = await self._request(
                "GET",
                f"/users/{username}/collection/folders/0/releases",
                params={
                    "page": page,
                    "per_page": per_page,
                    "sort": "added",
                    "sort_order": "desc",
                },
            )
            pagination = raw.get("pagination", {})
            releases = []
            for item in raw.get("releases", []):
                info = item.get("basic_information", {})
                artists = info.get("artists", [])
                artist_str = artists[0].get("name", "") if artists else ""
                releases.append(
                    {
                        "release_id": str(info.get("id", "")),
                        "title": info.get("title", ""),
                        "artist": artist_str,
                        "year": info.get("year"),
                        # Collection instance data — custom field values live in notes
                        "instance_id": item.get("instance_id"),
                        "folder_id": item.get("folder_id"),
                        "notes": item.get("notes") or [],
                    }
                )
            return {
                "releases": releases,
                "pages": pagination.get("pages", 1),
                "items": pagination.get("items", 0),
            }
        except Exception as exc:
            logger.warning("fetch_user_collection failed for %s page %d: %s", username, page, exc)
            return {"releases": [], "pages": 0, "items": 0, "error": str(exc)}

    # ------------------------------------------------------------------
    # Collection custom fields (require an authenticated personal token)
    # ------------------------------------------------------------------

    async def fetch_identity(self) -> str:
        """Return the username the configured token belongs to."""
        raw = await self._request("GET", "/oauth/identity")
        return str(raw.get("username") or "")

    async def fetch_collection_fields(self, username: str) -> list[dict[str, Any]]:
        """Return the user's collection field definitions (built-in + custom).

        Each field has ``id``, ``name``, ``type`` ("dropdown" | "textarea"),
        ``position`` and, for dropdowns, ``options``.
        """
        raw = await self._request("GET", f"/users/{username}/collection/fields")
        return list(raw.get("fields") or [])

    async def fetch_collection_instances(
        self, username: str, release_id: str | int
    ) -> list[dict[str, Any]]:
        """Return every copy of *release_id* in the user's collection.

        Each instance has ``instance_id``, ``folder_id`` and ``notes``
        (``[{field_id, value}]``).  Empty list when the release is not owned.
        """
        try:
            raw = await self._request("GET", f"/users/{username}/collection/releases/{release_id}")
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return []
            raise
        return list(raw.get("releases") or [])

    async def set_collection_field(
        self,
        username: str,
        *,
        folder_id: int,
        release_id: str | int,
        instance_id: int,
        field_id: int,
        value: str,
    ) -> None:
        """Write one custom field value on a collection instance."""
        await self._request(
            "POST",
            f"/users/{username}/collection/folders/{folder_id}/releases/{release_id}"
            f"/instances/{instance_id}/fields/{field_id}",
            json={"value": value},
        )

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

        # Community rating
        community = raw.get("community", {})
        rating_obj = community.get("rating", {})
        community_rating = rating_obj.get("average")
        community_votes = rating_obj.get("count")

        # Combined genres + styles as flat list; deduplicated, order preserved
        _seen: set[str] = set()
        genres: list[str] = []
        for g in list(raw.get("genres", [])) + list(raw.get("styles", [])):
            if g not in _seen:
                _seen.add(g)
                genres.append(g)

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
            "genres": genres,
            "tracklist": _extract_tracklist(raw.get("tracklist", [])),
            "notes": raw.get("notes", ""),
            "discogs_community_rating": community_rating,
            "discogs_community_votes": community_votes,
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


def _extract_tracklist(raw_tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise Discogs tracklist into [{disc, position, title, artists, duration_ms}].

    Discogs tracklist entries with type_ == "heading" are disc/side markers; we
    use them to increment the disc counter.  Position strings like "A1", "B2",
    "1-1", "2-3" are kept verbatim so the UI can display them as-is.
    """
    tracks: list[dict[str, Any]] = []
    disc = 1
    # Discogs multi-LP sets use side headings ("Side A", "Side B", …) or
    # explicit disc headings ("Disc 1", …).  We simply count heading entries
    # to derive the disc/LP number (every 2 sides = 1 LP).
    sides_seen = 0
    for entry in raw_tracks:
        t_type = (entry.get("type_") or "").lower()
        if t_type in ("heading", "index"):
            sides_seen += 1
            if sides_seen % 2 == 1 and sides_seen > 1:
                disc += 1
            continue
        position = entry.get("position", "")
        title = entry.get("title", "")
        duration = entry.get("duration", "")
        duration_ms = _duration_to_ms(duration)
        # Extra artists on a track (Discogs "extraartists" field)
        extra_artists = entry.get("extraartists", [])
        track_artists = [a.get("name", "") for a in extra_artists if a.get("name")]
        tracks.append(
            {
                "disc": disc,
                "position": position,
                "title": title,
                "artists": track_artists,
                "duration_ms": duration_ms,
            }
        )
    return tracks


def _duration_to_ms(duration: str) -> int | None:
    """Convert "3:47" or "1:02:15" to milliseconds, return None on parse failure."""
    if not duration:
        return None
    try:
        parts = [int(p) for p in duration.split(":")]
        if len(parts) == 2:
            return (parts[0] * 60 + parts[1]) * 1000
        if len(parts) == 3:
            return (parts[0] * 3600 + parts[1] * 60 + parts[2]) * 1000
    except (ValueError, AttributeError):
        pass
    return None


def _extract_manufacturer(companies: list[dict[str, Any]]) -> str | None:
    """Extract pressing plant name from the companies array."""
    for comp in companies:
        role = (comp.get("entity_type_name") or "").lower()
        if "pressed" in role or "manufactured" in role or "plant" in role:
            return comp.get("name")
    return None
