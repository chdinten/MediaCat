from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

import httpx

from mediacat.ingestion.musicbrainz import MusicBrainzConnector

logger = logging.getLogger(__name__)

_API_BASE = "https://en.wikipedia.org/api/rest_v1"
_SEARCH_BASE = "https://en.wikipedia.org/w/api.php"
_WIKIDATA_BASE = "https://www.wikidata.org/wiki/Special:EntityData"
_USER_AGENT = "MediaCat/0.1 (+https://example.invalid/mediacat)"
_TIMEOUT = 10.0

_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json",
}


class WikipediaConnector:
    """Thin async wrapper around the Wikipedia REST + MediaWiki action APIs."""

    async def search_artist(self, name: str) -> list[dict[str, Any]]:
        """Search Wikipedia for an artist page; return top candidates.

        Each result:
        {
            title,
            description,
            snippet,
            url,
            thumbnail_url
        }

        Note:
        Wikipedia search snippets are not proper descriptions. They are exposed
        separately as "snippet". Use fetch_artist_summary() to get a real
        page description and extract.
        """
        params = {
            "action": "query",
            "list": "search",
            "srsearch": name,
            "srnamespace": "0",
            "srlimit": "5",
            "format": "json",
            "origin": "*",
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            try:
                r = await client.get(_SEARCH_BASE, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                logger.warning("Wikipedia search failed for %r: %s", name, exc)
                return []

        results: list[dict[str, Any]] = []

        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            encoded_title = urllib.parse.quote(title.replace(" ", "_"), safe="/:")

            results.append(
                {
                    "title": title,
                    "description": "",
                    "snippet": _strip_html(item.get("snippet", "")),
                    "url": f"https://en.wikipedia.org/wiki/{encoded_title}",
                    "thumbnail_url": None,
                }
            )

        return results

    async def fetch_artist_summary(self, page_title: str) -> dict[str, Any]:
        """Fetch the REST summary for a Wikipedia page.

        Returns:
        {
            title,
            description,
            extract,
            url,
            thumbnail_url,
            original_image_url
        }
        """
        encoded = urllib.parse.quote(page_title.replace(" ", "_"), safe="")

        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            try:
                r = await client.get(f"{_API_BASE}/page/summary/{encoded}")
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                logger.warning("Wikipedia summary fetch failed for %r: %s", page_title, exc)
                return {}

        thumbnail = data.get("thumbnail", {}) or {}
        original = data.get("originalimage", {}) or {}

        thumbnail_url = thumbnail.get("source") or original.get("source")

        # If the REST summary carries no image, try the MediaWiki pageimages API —
        # it often succeeds where the REST endpoint does not (e.g. infobox-only images).
        if not thumbnail_url:
            thumbnail_url = await self._fetch_page_image(page_title)

        return {
            "title": data.get("title", page_title),
            "type": data.get("type", ""),  # "standard" | "disambiguation" | …
            "description": data.get("description", ""),
            "extract": data.get("extract", ""),
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
            "thumbnail_url": thumbnail_url,
            "original_image_url": original.get("source"),
        }

    async def _fetch_page_image(self, page_title: str) -> str | None:
        """Return a lead image URL via the MediaWiki pageimages API, or None."""
        params: dict[str, str | int] = {
            "action": "query",
            "titles": page_title,
            "prop": "pageimages",
            "pithumbsize": 500,
            "piprop": "thumbnail",
            "format": "json",
            "origin": "*",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            try:
                r = await client.get(_SEARCH_BASE, params=params)
                r.raise_for_status()
                pages = r.json().get("query", {}).get("pages", {})
            except Exception as exc:
                logger.warning("Wikipedia pageimages failed for %r: %s", page_title, exc)
                return None
        for page in pages.values():
            src = (page.get("thumbnail") or {}).get("source")
            if src:
                logger.info("pageimages found image for %r: %s", page_title, src)
                return str(src)
        logger.info(
            "pageimages returned no image for %r (pages=%s)", page_title, list(pages.keys())
        )
        return None

    async def fetch_discography(self, page_title: str) -> list[dict[str, Any]]:
        """Parse studio album list from the Wikipedia discography section."""
        params = {
            "action": "query",
            "titles": page_title,
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
            "origin": "*",
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            try:
                r = await client.get(_SEARCH_BASE, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                logger.warning("Wikipedia wikitext fetch failed for %r: %s", page_title, exc)
                return []

        pages = data.get("query", {}).get("pages", {})
        wikitext = ""

        for page in pages.values():
            slots = page.get("revisions", [{}])[0].get("slots", {})
            wikitext = slots.get("main", {}).get("*", "")
            break

        return _parse_discography_wikitext(wikitext)

    async def fetch_plain_paragraphs(self, page_title: str) -> list[str]:
        """Prose paragraphs of the whole article as plain text, lead section first."""
        from mediacat.ingestion.artist_bio import split_paragraphs  # noqa: PLC0415 - cycle

        params = {
            "action": "query",
            "titles": page_title,
            "prop": "extracts",
            "explaintext": "1",
            "exsectionformat": "plain",
            "redirects": "1",
            "format": "json",
            "origin": "*",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            try:
                r = await client.get(_SEARCH_BASE, params=params)
                r.raise_for_status()
                pages = r.json().get("query", {}).get("pages", {})
            except Exception as exc:
                logger.warning("Wikipedia extract fetch failed for %r: %s", page_title, exc)
                return []
        for page in pages.values():
            return split_paragraphs(page.get("extract", ""))
        return []

    async def fetch_artist_image_url(self, page_title: str) -> str | None:
        """Return the first summary image URL for an artist page, or None."""
        summary = await self.fetch_artist_summary(page_title)
        return summary.get("thumbnail_url") or summary.get("original_image_url")

    async def musicbrainz_links(self, name: str) -> dict[str, Any]:
        """Resolve *name* on MusicBrainz and collect the links a bio lookup needs.

        Returns ``{"mbid", "wp_title", "homepages", "discogs_url"}``; keys are
        missing / empty when MusicBrainz has no such data.  The English
        Wikipedia title comes from a direct link or, failing that, from the
        Wikidata sitelink.
        """
        mb = MusicBrainzConnector(
            name="musicbrainz",
            base_url="https://musicbrainz.org/ws/2",
            user_agent=_USER_AGENT,
            rate_limit=50,
        )
        try:
            async with mb:
                best_artist = await mb.resolve_artist(name)
                if not best_artist:
                    logger.info("MB: no artists found for %r", name)
                    return {}
                mbid: str = best_artist["id"]
                logger.info(
                    "MB: resolved %r → %r, type=%r, score=%r, mbid=%s",
                    name,
                    best_artist.get("name"),
                    best_artist.get("type"),
                    best_artist.get("score"),
                    mbid,
                )
                detail = await mb.get_artist(mbid)
                relations = detail.get("relations", [])
        except Exception as exc:
            logger.info("MB lookup failed for %r: %s", name, exc)
            return {}

        links: dict[str, Any] = {"mbid": mbid, "wp_title": None, "homepages": []}
        wikidata_id: str | None = None
        for rel in relations:
            resource = rel.get("url", {}).get("resource", "")
            kind = rel.get("type", "")
            wp = re.search(r"en\.wikipedia\.org/wiki/([^#?]+)", resource)
            wd = re.search(r"wikidata\.org/wiki/(Q\d+)", resource)
            if wp and not links["wp_title"]:
                links["wp_title"] = urllib.parse.unquote(wp.group(1).replace("_", " "))
            elif wd and not wikidata_id:
                wikidata_id = wd.group(1)
            elif kind == "official homepage" and resource not in links["homepages"]:
                links["homepages"].append(resource)
            elif kind == "discogs":
                links["discogs_url"] = resource

        if not links["wp_title"] and wikidata_id:
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as wd_client:
                    r3 = await wd_client.get(f"{_WIKIDATA_BASE}/{wikidata_id}.json")
                    r3.raise_for_status()
                    entity = r3.json().get("entities", {}).get(wikidata_id, {})
                    links["wp_title"] = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
            except Exception as exc:
                logger.info("Wikidata fetch failed for %s: %s", wikidata_id, exc)
        logger.info(
            "MB links for %r: wikipedia=%r homepages=%d",
            name,
            links["wp_title"],
            len(links["homepages"]),
        )
        return links

    async def fetch_artist_summary_via_musicbrainz(self, name: str) -> dict[str, Any]:
        """REST summary of the Wikipedia page MusicBrainz links for *name* ({} if none)."""
        title = (await self.musicbrainz_links(name)).get("wp_title")
        return await self.fetch_artist_summary(title) if title else {}


def _strip_html(text: str) -> str:
    """Remove HTML tags from a snippet."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_discography_wikitext(wikitext: str) -> list[dict[str, Any]]:
    """Very lightweight wikitext discography parser."""
    albums: list[dict[str, Any]] = []

    row_re = re.compile(
        r"\|\|?\s*(?:'{2,3})?\[\[([^\|\]]+?)(?:\|[^\]]+)?\]\](?:'{2,3})?" r".*?(\b(19|20)\d{2}\b)",
        re.DOTALL,
    )

    for match in row_re.finditer(wikitext):
        title = match.group(1).strip()
        year_str = match.group(2)

        try:
            year = int(year_str)
        except ValueError:
            year = None

        if title and year:
            albums.append(
                {
                    "title": title,
                    "year": year,
                    "source": "wikipedia",
                }
            )

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []

    for album in sorted(albums, key=lambda x: x.get("year") or 9999):
        if album["title"] not in seen:
            seen.add(album["title"])
            unique.append(album)

    return unique
