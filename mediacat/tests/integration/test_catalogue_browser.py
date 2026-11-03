"""Catalogue browser in the 3panel layout (web/catalogue_browser.py), end to end.

The selection lives in the URL, so every view here is reached by a plain GET:
``/catalogue?artist=&album=&pressing=`` renders all three panels on the server,
and the HTMX fragments carry the push / replace URLs for the history.
"""

from __future__ import annotations

import re
import urllib.parse
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.enums import MediaFormat, TokenStatus
from mediacat.db.models import Country, Label, MediaObject, Token

from .conftest import ClientFactory
from .test_catalogue import seed_token
from .test_routes import _BrokenSessionFactory

_EDEN = "/catalogue?artist=Talk+Talk&amp;album=Spirit+of+Eden"  # as it appears in the HTML

_GRADED = {
    "discogs_collection": {
        "fields": [{"id": 1, "name": "Media Condition"}, {"id": 2, "name": "Sleeve Condition"}],
        "values": {"1": "Very Good Plus (VG+)", "2": "Very Good (VG)"},
    }
}


@pytest.fixture
def browser(viewer: httpx.AsyncClient) -> httpx.AsyncClient:
    viewer.cookies.set("mc_layout", "3panel")
    return viewer


async def seed_eden(db: AsyncSession) -> dict[str, Token]:
    """Talk Talk: three live pressings of Spirit of Eden, plus albums that must not show up."""
    atlantis = Country(alpha2="XA", alpha3="XAT", name="Atlantis")
    borduria = Country(alpha2="XB", alpha3="XBD", name="Borduria")
    label = Label(name="Parlophone", name_normalised="parlophone")
    db.add_all([atlantis, borduria, label])
    await db.flush()
    a = await seed_token(
        db,
        artist="Talk Talk",
        title="Spirit of Eden",
        year=1988,
        country_id=atlantis.id,
        label_id=label.id,
        catalog_number="PCSD 105",
        extra=_GRADED,
    )
    db.add(
        MediaObject(
            token_id=a.id,
            content_hash="0" * 64,
            bucket="media",
            object_key="covers/eden.png",
            mime_type="image/png",
            size_bytes=1,
            is_primary_cover=True,
        )
    )
    b = await seed_token(
        db,
        artist="Talk Talk",
        title="Spirit of Eden",
        year=1988,
        country_id=borduria.id,
        catalog_number="1-25774",
    )
    c = await seed_token(
        db,
        artist="Talk Talk",
        title="Spirit of Eden",
        media_format=MediaFormat.CD,
        catalog_number="CDP 123",
    )
    await seed_token(db, artist="Talk Talk", title="Laughing Stock", year=1991)
    await seed_token(db, artist="Talk Talk", title="Colour of Spring", deleted_at=datetime.now(UTC))
    await seed_token(
        db,
        artist="Talk Talk",
        title="Spirit of Eden",
        status=TokenStatus.MERGED,
        catalog_number="GONE 1",
    )
    return {"a": a, "b": b, "c": c}


def _row(html: str, catno: str) -> str:
    """The versions-table row (<tr>…</tr>) holding this catalogue number."""
    at = html.index(catno)
    return html[html.rindex("<tr", 0, at) : html.index("</tr>", at)]


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------


async def test_index_lists_every_artist_a_to_z(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    for i in range(30):
        db.add(
            Token(
                media_format=MediaFormat.VINYL,
                status=TokenStatus.ACTIVE,
                artist=f"Band {i:02d}",
                title="X",
            )
        )
    for name in ("Can", "The Cure", "Depeche Mode", "10cc"):
        db.add(
            Token(media_format=MediaFormat.VINYL, status=TokenStatus.ACTIVE, artist=name, title="X")
        )
    await db.commit()

    r = await browser.get("/catalogue")
    assert r.status_code == 200
    html = r.text
    # The whole index, not a first page: the browser has no infinite scroll.
    assert ">Band 00<" in html
    assert ">Band 29<" in html
    # "The Cure" files under C; digits go under "#", before A.
    assert html.index(">Can<") < html.index(">The Cure<") < html.index(">Depeche Mode<")
    assert html.index('id="mc-letter-num"') < html.index(">10cc<") < html.index('id="mc-letter-B"')
    assert 'data-letter="C"' in html
    assert 'data-letter="Z"' not in html  # unused letters are not buttons
    assert "Pick an artist on the left" in html
    # htmx history snapshots cover the panels only, not the top bar.
    assert 'id="sp-layout-root" hx-history-elt' in html


async def test_url_opens_the_artist_album_and_pressing(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await seed_eden(db)
    a, b = t["a"], t["b"]

    r = await browser.get(
        "/catalogue",
        params={"artist": "Talk Talk", "album": "Spirit of Eden", "pressing": str(a.id)},
    )
    assert r.status_code == 200
    html = r.text
    assert "<title>Talk Talk — Catalogue — MediaCat</title>" in html

    # Left: the artist is the current entry of the index.
    assert re.search(
        r'mc-artist mc-artist--active"[^>]*>\s*<a class="mc-artist-link" href="/catalogue\?artist=Talk\+Talk"',
        html,
    )
    # Centre: header, the album covers (live albums only) and the versions table.
    assert 'id="mc-artist-name">Talk Talk<' in html
    assert 'href="/artists?artist=Talk+Talk"' in html  # biography and discography
    assert "mc-album mc-album--active" in html
    assert 'hx-push-url="/catalogue?artist=Talk+Talk&amp;album=Laughing+Stock"' in html
    assert "Colour of Spring" not in html
    assert "GONE 1" not in html
    assert html.index("PCSD 105") < html.index("1-25774") < html.index("CDP 123")
    assert f'src="/catalogue/{a.id}/images/' in html  # album cover
    assert html.count("mc-version mc-version--active") == 1
    assert "mc-version--active" in _row(html, "PCSD 105")
    assert "VG+ / VG" in _row(html, "PCSD 105")
    # Picking another pressing of the album replaces the URL rather than pushing.
    assert f'hx-replace-url="{_EDEN}&amp;pressing={b.id}"' in html
    # Right: the preview of the selected pressing.
    assert 'class="mc-preview"' in html
    assert f'<a class="btn btn-primary btn-sm" href="/catalogue/{a.id}">Open pressing</a>' in html
    assert "Parlophone" in html


async def test_artist_fragment_pushes_album_urls_and_empties_the_preview(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    await seed_eden(db)
    r = await browser.get(
        "/catalogue/browse/artist", params={"artist": "Talk Talk"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    html = r.text
    assert "<html" not in html
    assert f'hx-push-url="{_EDEN}"' in html
    assert "Laughing Stock" in html
    assert "Colour of Spring" not in html
    assert "Pick an album to list its pressings." in html  # two albums: none opened yet
    assert 'id="sp-right" hx-swap-oob="innerHTML"' in html
    assert (await browser.get("/catalogue/browse/artist")).status_code == 404


async def test_an_artist_with_one_album_opens_it(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    await seed_token(db, artist="Solo", title="Only One", catalog_number="ONE 1")
    html = (await browser.get("/catalogue/browse/artist", params={"artist": "Solo"})).text
    assert "mc-album mc-album--active" in html
    assert "ONE 1" in html
    assert 'hx-replace-url="/catalogue?artist=Solo&amp;album=Only+One&amp;pressing=' in html


async def test_album_fragment_lists_pressings_in_versions_order(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await seed_eden(db)
    r = await browser.get(
        "/catalogue/browse/album", params={"artist": "Talk Talk", "album": "Spirit of Eden"}
    )
    assert r.status_code == 200
    html = r.text
    # Year first (unknown last), then country.
    assert html.index("PCSD 105") < html.index("1-25774") < html.index("CDP 123")
    assert "2 Vinyl · 1 CD" in html
    assert "mc-version--active" not in html
    assert f'hx-replace-url="{_EDEN}&amp;pressing={t["c"].id}"' in html
    assert f'hx-get="/catalogue/{t["c"].id}/preview"' in html
    assert 'id="sp-right" hx-swap-oob="innerHTML"' in html


async def test_filters_narrow_albums_and_pressings_and_keep_the_selection(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    await seed_eden(db)
    html = (
        await browser.get(
            "/catalogue", params={"artist": "Talk Talk", "album": "Spirit of Eden", "fmt": "cd"}
        )
    ).text
    assert "CDP 123" in html
    assert "PCSD 105" not in html
    assert "Laughing Stock" not in html  # a vinyl-only album
    assert "filtered" in html
    # The chip removes the filter, the pills switch it, both keep artist and album.
    assert f'<a class="mc-chip" href="{_EDEN}"' in html
    assert f'href="{_EDEN}&amp;fmt=vinyl"' in html
    # So do the search box and the range form (hidden inputs).
    assert '<input type="hidden" name="artist" value="Talk Talk">' in html
    assert '<input type="hidden" name="fmt" value="cd">' in html


async def test_filters_without_an_artist_list_the_matching_pressings(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    await seed_token(db, artist="Vinyl Act", title="LP", genres=["Rock"])
    jazz = await seed_token(
        db, artist="Disc Act", title="Blue", genres=["Jazz"], media_format=MediaFormat.CD
    )
    r = await browser.get("/catalogue?genres=jazz")
    assert r.status_code == 200
    html = r.text
    assert ">Disc Act<" in html
    assert "Vinyl Act" not in html
    assert "Pressings matching the filters" in html
    assert (
        f'href="/catalogue?artist=Disc+Act&amp;album=Blue&amp;pressing={jazz.id}&amp;genres=jazz"'
        in html
    )


async def test_a_link_to_a_pressing_alone_opens_its_artist_and_album(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await seed_eden(db)
    r = await browser.get(f"/catalogue?pressing={t['b'].id}&fmt=vinyl")
    assert r.status_code == 303
    assert r.headers["location"] == (
        f"/catalogue?artist=Talk+Talk&album=Spirit+of+Eden&pressing={t['b'].id}&fmt=vinyl"
    )


async def test_the_album_without_a_title_is_reachable(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    await seed_token(db, artist="Nameless", title=None, catalog_number="NN 1")
    await seed_token(db, artist="Nameless", title="Named", catalog_number="NN 2")
    html = (await browser.get("/catalogue?artist=Nameless&album=")).text
    assert ">Untitled<" in html
    assert "NN 1" in html
    assert "NN 2" not in html


async def test_preview_fragment(browser: httpx.AsyncClient, db: AsyncSession) -> None:
    t = await seed_eden(db)
    r = await browser.get(f"/catalogue/{t['a'].id}/preview")
    assert r.status_code == 200
    assert "PCSD 105" in r.text
    assert "Parlophone" in r.text
    assert "VG+ / VG" in r.text
    assert (await browser.get(f"/catalogue/{uuid.uuid4()}/preview")).status_code == 404
    assert (await browser.get("/catalogue/nope/preview")).status_code == 404


async def test_browser_renders_when_the_database_is_down(
    app: FastAPI, make_client: ClientFactory
) -> None:
    client = await make_client("viewer")
    client.cookies.set("mc_layout", "3panel")
    app.state.db_session_factory = _BrokenSessionFactory()
    r = await client.get("/catalogue?artist=Talk+Talk&album=Spirit+of+Eden")
    assert r.status_code == 200
    assert "The catalogue could not be loaded." in r.text


async def test_artists_page_opens_the_artist_it_is_asked_for(
    browser: httpx.AsyncClient, db: AsyncSession
) -> None:
    await seed_token(db, artist="Aardvark", title="A")
    await seed_token(db, artist="Talk Talk", title="Spirit of Eden")
    init = re.compile(r'id="sp-artist-detail-init"\s+hx-get="/artists/detail\?artist=([^"]+)"')
    named = init.search((await browser.get("/artists", params={"artist": "Talk Talk"})).text)
    assert named is not None
    assert named.group(1) == "Talk%20Talk"
    first = init.search((await browser.get("/artists")).text)
    assert first is not None
    assert first.group(1) == "Aardvark"


# ---------------------------------------------------------------------------
# Pressing page: breadcrumb, back link, previous / next
# ---------------------------------------------------------------------------


async def test_pressing_page_links_up_the_hierarchy_and_to_its_neighbours(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await seed_eden(db)
    html = (await viewer.get(f"/catalogue/{t['b'].id}")).text
    assert '<a href="/catalogue?artist=Talk+Talk">Talk Talk</a>' in html
    assert f'<a href="{_EDEN}">Spirit of Eden</a>' in html
    assert '<span aria-current="page">Borduria 1988 · 1-25774</span>' in html
    # Opened directly: back to the album in the browser, with this pressing selected.
    assert (
        f'href="{_EDEN}&amp;pressing={t["b"].id}" class="detail-back" data-mc-back>'
        "← Back to Talk Talk · Spirit of Eden"
    ) in html
    assert "2 of 3" in html
    assert re.search(rf'href="/catalogue/{t["a"].id}\?back=[^"]+" rel="prev"', html)
    assert re.search(rf'href="/catalogue/{t["c"].id}\?back=[^"]+" rel="next"', html)


async def test_back_link_returns_to_the_view_the_pressing_was_opened_from(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await seed_eden(db)
    page = f"/catalogue/{t['b'].id}"
    came_from = (
        f"http://testserver/catalogue?artist=Talk+Talk&album=Spirit+of+Eden"
        f"&pressing={t['a'].id}&fmt=vinyl"
    )
    html = (await viewer.get(page, headers={"Referer": came_from})).text
    # Same view and filters, pointed at this pressing.
    assert f'href="{_EDEN}&amp;pressing={t["b"].id}&amp;fmt=vinyl" class="detail-back"' in html
    assert "2 of 2" in html  # with fmt=vinyl the CD is not a neighbour

    html = (await viewer.get(page, headers={"Referer": "http://testserver/search?q=talk"})).text
    assert 'href="/search?q=talk" class="detail-back" data-mc-back>← Back to search results' in html

    for ref in ("https://evil.example/catalogue?artist=X", "http://testserver/admin"):
        html = (await viewer.get(page, headers={"Referer": ref})).text
        assert f'href="{_EDEN}&amp;pressing={t["b"].id}" class="detail-back"' in html, ref


async def test_back_parameter_is_kept_through_previous_and_next(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    t = await seed_eden(db)
    back = "/catalogue?artist=Talk+Talk&album=Spirit+of+Eden&q=talk"
    html = (await viewer.get(f"/catalogue/{t['c'].id}", params={"back": back})).text
    assert f'href="{_EDEN}&amp;pressing={t["c"].id}&amp;q=talk" class="detail-back"' in html
    prev = re.search(r'href="(/catalogue/[^"]+)" rel="prev"', html)
    assert prev is not None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(prev.group(1).replace("&amp;", "&")).query)
    assert query["back"] == [
        f"/catalogue?artist=Talk+Talk&album=Spirit+of+Eden&pressing={t['c'].id}&q=talk"
    ]

    for bad in ("//evil.example/", "https://evil.example/", "/\\evil.example/", "/admin"):
        html = (await viewer.get(f"/catalogue/{t['c'].id}", params={"back": bad})).text
        assert "evil.example" not in html, bad
        assert f'href="{_EDEN}&amp;pressing={t["c"].id}" class="detail-back"' in html, bad
