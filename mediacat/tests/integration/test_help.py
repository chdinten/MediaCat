"""Integration tests for the in-app help page (/help)."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import ClientFactory
from .test_catalogue import seed_token


@pytest.mark.parametrize("role", ["admin", "reviewer", "contributor", "viewer"])
@pytest.mark.parametrize("layout", ["classic", "3panel"])
async def test_help_page_for_every_role_and_layout(
    make_client: ClientFactory, role: str, layout: str
) -> None:
    client = await make_client(role)
    client.cookies.set("mc_layout", layout)
    r = await client.get(
        "/help", headers={"host": "mediacat.localhost:8443", "x-forwarded-proto": "https"}
    )
    assert r.status_code == 200
    html = r.text
    assert '<details class="help-entry" id="artist-number"' in html
    assert 'href="/help" class="active">Help</a>' in html
    assert 'href="https://docs.mediacat.localhost:8443/"' in html  # developer docs, same host
    assert "Start docs server" in html
    assert '<script src="/static/help.js" defer></script>' in html


async def test_help_query_prefills_the_search_box(viewer: httpx.AsyncClient) -> None:
    html = (await viewer.get("/help", params={"q": 'brackets"><b>'})).text
    assert 'value="brackets&#34;&gt;&lt;b&gt;"' in html


async def test_help_needs_a_login(anon: httpx.AsyncClient) -> None:
    r = await anon.get("/help")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_help_link_in_every_layout(make_client: ClientFactory) -> None:
    client = await make_client("viewer")
    for layout in ("classic", "3panel"):
        client.cookies.set("mc_layout", layout)
        assert 'href="/help"' in (await client.get("/")).text


async def test_artist_number_hint_only_for_numbered_names(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    saga = await seed_token(db, artist="Saga (3)", title="Worlds Apart")
    plain = await seed_token(db, artist="Talk Talk", title="Spirit of Eden")
    hint = 'href="/help#artist-number"'
    assert hint in (await viewer.get(f"/catalogue/{saga.id}")).text
    assert hint in (await viewer.get("/artists/detail", params={"artist": "Saga (3)"})).text
    assert hint not in (await viewer.get(f"/catalogue/{plain.id}")).text
    assert hint not in (await viewer.get("/artists/detail", params={"artist": "Talk Talk"})).text
