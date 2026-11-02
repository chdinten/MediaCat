"""Add pressing: Discogs release preview cards (images + first runout variant)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from mediacat.ingestion.discogs_extras import release_preview
from mediacat.web import admin, discogs_details
from mediacat.web.app import _WEB_DIR
from mediacat.web.routes import set_templates

RELEASE: dict[str, Any] = {
    "images": [
        {
            "type": "secondary",
            "uri": "https://i.discogs.com/b.jpg",
            "uri150": "https://i.discogs.com/b150.jpg",
        },
        {
            "type": "primary",
            "uri": "https://i.discogs.com/a.jpg",
            "uri150": "https://i.discogs.com/a150.jpg",
        },
        {"type": "secondary", "uri": "http://insecure.example/c.jpg", "uri150": ""},
    ],
    "identifiers": [
        {"type": "Barcode", "value": "5 099969 945717"},
        {"type": "Matrix / Runout", "value": "YEX 715-1", "description": "Side A, variant 1"},
        {"type": "Matrix / Runout", "value": "YEX 716-1", "description": "Side B, variant 1"},
        {"type": "Matrix / Runout", "value": "YEX 715-2", "description": "Side A, variant 2"},
    ],
    "formats": [{"name": "Vinyl", "text": "Yellow labels"}, {"name": "Vinyl"}],
    "released": "1969-01-17",
    "tracklist": [{"title": "Yellow Submarine"}, {"title": "Only A Northern Song"}],
}


def test_release_preview_summarises_images_and_first_variant() -> None:
    p = release_preview(RELEASE)
    assert [i["full"] for i in p["images"]] == [
        "https://i.discogs.com/a.jpg",
        "https://i.discogs.com/b.jpg",
    ]
    assert p["image_count"] == 2  # the http:// image is dropped
    assert p["variant_count"] == 2
    sides = [(s["side"], s["value"]) for lp in p["variant"]["lps"] for s in lp["sides"]]
    assert sides == [("A", "YEX 715-1"), ("B", "YEX 716-1")]
    assert p["barcodes"] == ["5 099969 945717"]
    assert p["format_text"] == "Yellow labels"
    assert (p["released"], p["track_count"]) == ("1969-01-17", 2)


def test_release_preview_handles_an_empty_release() -> None:
    p = release_preview({})
    assert p["images"] == []
    assert p["variant"] is None
    assert p["barcodes"] == []


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace()),
        "state": {},
    }
    req = Request(scope)
    req.state.user_role = "admin"
    req.state.session = {}
    req.state.csrf_token = ""
    return req


@pytest.fixture
def preview_env(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake_fetch(release_id: int, token: str) -> dict[str, Any]:
        calls.append(release_id)
        if release_id == 429:
            raise httpx.HTTPStatusError(
                "rate", request=httpx.Request("GET", "x"), response=httpx.Response(429)
            )
        return RELEASE

    async def no_token(request: Any) -> str:
        return ""

    monkeypatch.setattr(discogs_details, "_fetch_release_as", fake_fetch)
    monkeypatch.setattr(admin, "_resolve_discogs_token", no_token)
    discogs_details._PREVIEW_CACHE.clear()
    set_templates(Jinja2Templates(directory=str(_WEB_DIR / "templates")))
    return calls


async def test_preview_renders_lightbox_images_and_runouts(preview_env: list[int]) -> None:
    html = (await discogs_details.discogs_release_preview(_request(), 7)).body.decode()
    assert 'class="dc-preview-images"' in html
    assert 'class="image-thumb-link" href="https://i.discogs.com/a.jpg"' in html
    assert "YEX 716-1" in html
    assert "1 of 2 variants" in html
    await discogs_details.discogs_release_preview(_request(), 7)
    assert preview_env == [7]  # second request served from the cache


async def test_preview_reports_the_rate_limit(preview_env: list[int]) -> None:
    html = (await discogs_details.discogs_release_preview(_request(), 429)).body.decode()
    assert "rate limit" in html
    assert "Try again" in html
    assert 429 not in discogs_details._PREVIEW_CACHE  # errors are not cached
