"""Catalogue routes (web/catalogue.py) end to end: pressings, revisions, images, browse."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.enums import (
    ImageRegion,
    MediaFormat,
    ReviewReason,
    ReviewStatus,
    RevisionSource,
    TokenStatus,
)
from mediacat.db.models import Country, MediaObject, ReviewItem, Token, TokenRevision, User

from .fakes import FakeObjectStore, png_bytes

_FORM = {
    "artist": "Kate Bush",
    "title": "Hounds of Love",
    "year": "1985",
    "media_format": "vinyl",
    "catalog_number": "KAB 1",
    "barcode": "5099924026714",
    "matrix_runout": "KAB 1 A-1U",
    "matrix_runout_b": "KAB 1 B-1U",
    "country_name": "UK",
    "label_name": "EMI",
    "manufacturer_name": "EMI Records Ltd",
    "discogs_release_id": "123456",
    "comment": "first entry",
}


async def seed_token(db: AsyncSession, **fields: Any) -> Token:
    """Insert a live pressing (defaults: active vinyl) and commit."""
    values: dict[str, Any] = {
        "media_format": MediaFormat.VINYL,
        "status": TokenStatus.ACTIVE,
        "artist": "Artist",
        "title": "Album",
    }
    values.update(fields)
    token = Token(**values)
    db.add(token)
    await db.commit()
    return token


async def revisions(db: AsyncSession, token_id: uuid.UUID) -> list[TokenRevision]:
    rows = await db.execute(
        select(TokenRevision)
        .where(TokenRevision.token_id == token_id)
        .order_by(TokenRevision.revision_number)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def reload(db: AsyncSession, token: Token) -> Token:
    fresh = await db.get(Token, token.id, populate_existing=True)
    assert fresh is not None
    return fresh


def token_id_from(resp: httpx.Response) -> uuid.UUID:
    assert resp.status_code == 303, resp.text
    return uuid.UUID(resp.headers["location"].rsplit("/", 1)[1])


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_pressing_stores_token_and_first_revision(
    admin: httpx.AsyncClient, db: AsyncSession, users: dict[str, User]
) -> None:
    tid = token_id_from(await admin.post("/catalogue/new", data=_FORM))

    token = await db.get(Token, tid)
    assert token is not None
    assert (token.artist, token.title, token.year) == ("Kate Bush", "Hounds of Love", 1985)
    assert token.media_format is MediaFormat.VINYL
    assert token.status is TokenStatus.ACTIVE
    assert token.discogs_release_id == 123456
    uk = (await db.execute(select(Country).where(Country.name == "UK"))).scalar_one()
    assert token.country_id == uk.id

    (rev,) = await revisions(db, tid)
    assert rev.revision_number == 1
    assert rev.source is RevisionSource.HUMAN
    assert rev.created_by == users["admin"].id
    assert rev.comment == "first entry"
    assert rev.data["label_name"] == "EMI"
    assert rev.data["catalog_number"] == "KAB 1"
    assert token.current_revision_id == rev.id


async def test_create_with_unknown_country_adds_it(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    before = (await db.execute(select(func.count(Country.id)))).scalar_one()
    tid = token_id_from(
        await contributor.post("/catalogue/new", data={**_FORM, "country_name": "Freedonia"})
    )
    token = await db.get(Token, tid)
    assert token is not None
    country = await db.get(Country, token.country_id)
    assert country is not None
    assert country.name == "Freedonia"
    assert (await db.execute(select(func.count(Country.id)))).scalar_one() == before + 1


async def test_create_rejects_bad_year_without_writing(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    r = await admin.post("/catalogue/new", data={**_FORM, "year": "19x5"})
    assert r.status_code == 400
    assert "year" in r.text.lower()
    assert (await db.execute(select(func.count(Token.id)))).scalar_one() == 0


async def test_new_pressing_form_renders_for_contributor(contributor: httpx.AsyncClient) -> None:
    r = await contributor.get("/catalogue/new")
    assert r.status_code == 200
    assert 'name="artist"' in r.text


@pytest.mark.parametrize("role", ["viewer"])
async def test_viewer_cannot_create(make_client: Any, db: AsyncSession, role: str) -> None:
    client = await make_client(role)
    assert (await client.get("/catalogue/new")).status_code == 403
    assert (await client.post("/catalogue/new", data=_FORM)).status_code == 403
    assert (await db.execute(select(func.count(Token.id)))).scalar_one() == 0


# ---------------------------------------------------------------------------
# Edit → new revision, history kept
# ---------------------------------------------------------------------------


async def test_edit_adds_revision_and_keeps_old_data(
    admin: httpx.AsyncClient, reviewer: httpx.AsyncClient, db: AsyncSession, users: dict[str, User]
) -> None:
    tid = token_id_from(await admin.post("/catalogue/new", data=_FORM))

    r = await reviewer.post(
        f"/catalogue/{tid}/edit",
        data={**_FORM, "title": "Hounds of Love (Remaster)", "year": "1997", "comment": "fix"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/catalogue/{tid}"

    token = await reload(db, Token(id=tid))
    assert (token.title, token.year) == ("Hounds of Love (Remaster)", 1997)

    first, second = await revisions(db, tid)
    assert first.data["title"] == "Hounds of Love"
    assert first.data["year"] == 1985
    assert second.revision_number == 2
    assert second.data["title"] == "Hounds of Love (Remaster)"
    assert second.created_by == users["reviewer"].id
    assert token.current_revision_id == second.id


async def test_edit_form_shows_current_values(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db, artist="Can", title="Tago Mago", catalog_number="UAS 29211")
    r = await reviewer.get(f"/catalogue/{token.id}/edit")
    assert r.status_code == 200
    assert "Tago Mago" in r.text
    assert "UAS 29211" in r.text


async def test_edit_bad_year_keeps_token_unchanged(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db, year=1971)
    r = await admin.post(f"/catalogue/{token.id}/edit", data={**_FORM, "year": "abc"})
    assert r.status_code == 400
    assert (await reload(db, token)).year == 1971
    assert await revisions(db, token.id) == []


async def test_edit_redirects_back_to_local_page(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    r = await admin.post(f"/catalogue/{token.id}/edit", data={**_FORM, "back": "/catalogue?q=x"})
    assert r.headers["location"] == "/catalogue?q=x"
    r = await admin.post(
        f"/catalogue/{token.id}/edit", data={**_FORM, "back": "https://evil.example/"}
    )
    assert r.headers["location"] == f"/catalogue/{token.id}"


async def test_edit_back_rejects_protocol_relative_url(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    # "//host/path" starts with "/" but browsers treat it as another site.
    token = await seed_token(db)
    for back in ("//evil.example/", "/\\evil.example/"):
        r = await admin.post(f"/catalogue/{token.id}/edit", data={**_FORM, "back": back})
        assert r.headers["location"] == f"/catalogue/{token.id}", back


@pytest.mark.parametrize("role", ["contributor", "viewer"])
async def test_edit_denied_below_reviewer(make_client: Any, db: AsyncSession, role: str) -> None:
    token = await seed_token(db, title="Original")
    client = await make_client(role)
    assert (await client.get(f"/catalogue/{token.id}/edit")).status_code == 403
    r = await client.post(f"/catalogue/{token.id}/edit", data={**_FORM, "title": "Changed"})
    assert r.status_code == 403
    assert (await reload(db, token)).title == "Original"


async def test_edit_unknown_or_deleted_pressing_is_404(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    from datetime import UTC, datetime

    gone = await seed_token(db, deleted_at=datetime.now(UTC))
    for tid in (uuid.uuid4(), gone.id, "not-a-uuid"):
        assert (await admin.post(f"/catalogue/{tid}/edit", data=_FORM)).status_code == 404


# ---------------------------------------------------------------------------
# Archive, flag for review, soft delete
# ---------------------------------------------------------------------------


async def test_archive_sets_status(reviewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(db)
    r = await reviewer.post(f"/catalogue/{token.id}/archive")
    assert r.status_code == 303
    assert (await reload(db, token)).status is TokenStatus.ARCHIVED


async def test_contributor_cannot_archive(contributor: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(db)
    assert (await contributor.post(f"/catalogue/{token.id}/archive")).status_code == 403
    assert (await reload(db, token)).status is TokenStatus.ACTIVE


async def test_flag_for_review_queues_manual_item(
    reviewer: httpx.AsyncClient, db: AsyncSession, users: dict[str, User]
) -> None:
    token = await seed_token(db)
    r = await reviewer.post(f"/catalogue/{token.id}/flag-review")
    assert r.headers["location"].endswith("?msg=review_flagged")
    item = (await db.execute(select(ReviewItem))).scalar_one()
    assert item.token_id == token.id
    assert (item.status, item.reason) == (ReviewStatus.PENDING, ReviewReason.MANUAL)
    assert item.details["requested_by"] == str(users["reviewer"].id)


async def test_soft_delete_hides_pressing_but_keeps_row(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db, artist="Delete Me", title="Gone")
    assert (await admin.get(f"/catalogue/{token.id}")).status_code == 200

    r = await admin.post(f"/catalogue/{token.id}/delete")
    assert r.headers["location"] == "/catalogue"

    row = await reload(db, token)
    assert row.deleted_at is not None  # soft: row still there for recovery
    assert (await admin.get(f"/catalogue/{token.id}")).status_code == 404
    assert "Delete Me" not in (await admin.get("/catalogue")).text
    # A second delete finds nothing live.
    assert (await admin.post(f"/catalogue/{token.id}/delete")).status_code == 404


@pytest.mark.parametrize("role", ["reviewer", "contributor", "viewer"])
async def test_only_admin_can_delete(make_client: Any, db: AsyncSession, role: str) -> None:
    token = await seed_token(db)
    client = await make_client(role)
    assert (await client.post(f"/catalogue/{token.id}/delete")).status_code == 403
    assert (await reload(db, token)).deleted_at is None


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


async def test_merge_marks_loser_and_records_revision_on_winner(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    winner = await seed_token(db, artist="Low", title="Things We Lost in the Fire", year=2001)
    loser = await seed_token(db, artist="Low", title="Things We Lost In The Fire", year=2001)

    form = await reviewer.get(f"/catalogue/merge?winner_id={winner.id}&loser_id={loser.id}")
    assert form.status_code == 200
    assert "Things We Lost In The Fire" in form.text

    r = await reviewer.post(
        "/catalogue/merge",
        data={"winner_id": str(winner.id), "loser_id": str(loser.id), "comment": "dupe"},
    )
    assert r.headers["location"] == f"/catalogue/{winner.id}"

    assert (await reload(db, loser)).status is TokenStatus.MERGED
    assert (await reload(db, winner)).status is TokenStatus.ACTIVE
    (rev,) = await revisions(db, winner.id)
    assert rev.comment == f"Merged from {loser.id}. dupe"

    # The merged duplicate drops out of browsing; the winner stays.
    pressings = await reviewer.get(
        "/catalogue/pressings", params={"artist": "Low", "title": winner.title}
    )
    assert f"/catalogue/{winner.id}" in pressings.text
    albums = await reviewer.get("/catalogue/albums", params={"artist": "Low"})
    assert "Things We Lost In The Fire" not in albums.text


async def test_merge_with_itself_is_refused(reviewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(db)
    r = await reviewer.post(
        "/catalogue/merge", data={"winner_id": str(token.id), "loser_id": str(token.id)}
    )
    assert r.status_code == 400
    assert "Cannot merge a pressing with itself" in r.text
    assert (await reload(db, token)).status is TokenStatus.ACTIVE


async def test_merge_with_itself_is_refused_whatever_the_id_spelling(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Same UUID, different case: still the same pressing.
    token = await seed_token(db)
    r = await reviewer.post(
        "/catalogue/merge",
        data={"winner_id": str(token.id), "loser_id": str(token.id).upper()},
    )
    assert r.status_code == 400
    assert (await reload(db, token)).status is TokenStatus.ACTIVE


async def test_merge_search_lists_candidates_except_other_side(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    a = await seed_token(db, artist="Slowdive", title="Souvlaki")
    b = await seed_token(db, artist="Slowdive", title="Souvlaki (US)")
    await seed_token(db, artist="Slowdive", title="Pygmalion", status=TokenStatus.MERGED)

    r = await reviewer.get(
        "/catalogue/merge/search", params={"q": "slowdive", "other_id": str(a.id)}
    )
    assert f"loser_id={b.id}" in r.text
    assert f"loser_id={a.id}" not in r.text  # a is the other side already
    assert "Pygmalion" not in r.text
    # Too short a query searches nothing.
    assert "Souvlaki" not in (await reviewer.get("/catalogue/merge/search?q=s")).text


async def test_merge_denied_for_contributor(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    a = await seed_token(db)
    b = await seed_token(db)
    r = await contributor.post(
        "/catalogue/merge", data={"winner_id": str(a.id), "loser_id": str(b.id)}
    )
    assert r.status_code == 403
    assert (await reload(db, b)).status is TokenStatus.ACTIVE


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


async def upload(
    client: httpx.AsyncClient, token_id: uuid.UUID, data: bytes, region: str = "cover_front"
) -> httpx.Response:
    return await client.post(
        f"/catalogue/{token_id}/images",
        files={"file": ("scan.png", data, "image/png")},
        data={"region": region},
    )


async def media(db: AsyncSession, token_id: uuid.UUID) -> list[MediaObject]:
    rows = await db.execute(
        select(MediaObject)
        .where(MediaObject.token_id == token_id)
        .order_by(MediaObject.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def test_upload_stores_object_and_media_row(
    contributor: httpx.AsyncClient, db: AsyncSession, object_store: FakeObjectStore
) -> None:
    token = await seed_token(db)
    data = png_bytes(40, 30)
    r = await upload(contributor, token.id, data, region="label_a")
    assert r.headers["location"] == f"/catalogue/{token.id}?uploaded=1"

    (mo,) = await media(db, token.id)
    assert (mo.region, mo.width_px, mo.height_px) == (ImageRegion.LABEL_A, 40, 30)
    assert mo.mime_type == "image/png"
    assert object_store.objects[(mo.bucket, mo.object_key)] == data

    served = await contributor.get(f"/catalogue/{token.id}/images/{mo.id}")
    assert served.status_code == 200
    assert served.content == data
    assert served.headers["content-type"] == "image/png"


async def test_upload_rejects_non_image_types_and_bad_data(
    admin: httpx.AsyncClient, db: AsyncSession, object_store: FakeObjectStore
) -> None:
    token = await seed_token(db)
    r = await admin.post(
        f"/catalogue/{token.id}/images",
        files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert r.status_code == 415
    r = await admin.post(
        f"/catalogue/{token.id}/images",
        files={"file": ("x.png", b"not really a png", "image/png")},
    )
    assert r.status_code == 500
    assert object_store.objects == {}
    assert await media(db, token.id) == []


async def test_upload_unknown_region_falls_back_to_other(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    await upload(admin, token.id, png_bytes(), region="sideways")
    (mo,) = await media(db, token.id)
    assert mo.region is ImageRegion.OTHER


async def test_upload_same_image_twice_does_not_fail(
    admin: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    data = png_bytes()
    assert (await upload(admin, token.id, data)).status_code == 303
    r = await upload(admin, token.id, data)
    assert r.headers["location"] == f"/catalogue/{token.id}?msg=image_exists"
    assert len(await media(db, token.id)) == 1
    page = await admin.get(r.headers["location"])
    assert "already attached" in page.text


async def test_upload_without_object_store_is_503(
    admin: httpx.AsyncClient, db: AsyncSession, app: Any
) -> None:
    token = await seed_token(db)
    app.state.object_store = None
    r = await upload(admin, token.id, png_bytes())
    assert r.status_code == 503
    assert "Image storage is not configured" in r.json()["detail"]
    assert await media(db, token.id) == []


async def test_viewer_cannot_upload(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(db)
    assert (await upload(viewer, token.id, png_bytes())).status_code == 403
    assert await media(db, token.id) == []


async def test_set_cover_moves_primary_flag(reviewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(db)
    await upload(reviewer, token.id, png_bytes(colour="red"))
    await upload(reviewer, token.id, png_bytes(colour="blue"))
    first, second = await media(db, token.id)

    r = await reviewer.post(f"/catalogue/{token.id}/images/{second.id}/set-cover")
    assert r.headers["location"].endswith("?msg=cover_set")
    first, second = await media(db, token.id)
    assert (first.is_primary_cover, second.is_primary_cover) == (False, True)

    await reviewer.post(f"/catalogue/{token.id}/images/{first.id}/set-cover")
    first, second = await media(db, token.id)
    assert (first.is_primary_cover, second.is_primary_cover) == (True, False)

    # The brief panel shows the primary cover.
    brief = await reviewer.get(f"/catalogue/{token.id}/brief")
    assert f"/catalogue/{token.id}/images/{first.id}" in brief.text


async def test_region_reassignment(reviewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(db)
    await upload(reviewer, token.id, png_bytes())
    (mo,) = await media(db, token.id)
    r = await reviewer.post(
        f"/catalogue/{token.id}/images/{mo.id}/region", data={"region": "runout_b"}
    )
    assert r.status_code == 303
    (mo,) = await media(db, token.id)
    assert mo.region is ImageRegion.RUNOUT_B


async def test_delete_image_removes_row_only_for_that_pressing(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    other = await seed_token(db)
    await upload(reviewer, token.id, png_bytes())
    (mo,) = await media(db, token.id)

    # The image id must belong to the pressing in the URL.
    assert (await reviewer.post(f"/catalogue/{other.id}/images/{mo.id}/delete")).status_code == 404
    assert len(await media(db, token.id)) == 1

    r = await reviewer.post(f"/catalogue/{token.id}/images/{mo.id}/delete")
    assert r.headers["location"].endswith("?msg=image_deleted")
    assert await media(db, token.id) == []
    assert (await reviewer.get(f"/catalogue/{token.id}/images/{mo.id}")).status_code == 404


async def test_contributor_cannot_delete_or_set_cover(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    await upload(contributor, token.id, png_bytes())
    (mo,) = await media(db, token.id)
    assert (
        await contributor.post(f"/catalogue/{token.id}/images/{mo.id}/delete")
    ).status_code == 403
    assert (
        await contributor.post(f"/catalogue/{token.id}/images/{mo.id}/set-cover")
    ).status_code == 403
    (mo,) = await media(db, token.id)
    assert mo.is_primary_cover is False


# ---------------------------------------------------------------------------
# Artist → Album → Pressing drill-down
# ---------------------------------------------------------------------------


async def test_drill_down_artist_album_pressing(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    from datetime import UTC, datetime

    uk = await seed_token(db, artist="Talk Talk", title="Spirit of Eden", year=1988)
    us = await seed_token(db, artist="Talk Talk", title="Spirit of Eden", year=1989)
    await seed_token(db, artist="Talk Talk", title="Laughing Stock", year=1991)
    await seed_token(db, artist="Talk Talk", title="Colour of Spring", deleted_at=datetime.now(UTC))
    await seed_token(db, artist="Other", title="Spirit of Eden")

    index = await viewer.get("/catalogue")
    assert index.status_code == 200
    assert "Talk Talk" in index.text
    assert "Other" in index.text

    albums = await viewer.get("/catalogue/albums", params={"artist": "Talk Talk"})
    assert "Spirit of Eden" in albums.text
    assert "Laughing Stock" in albums.text
    assert "Colour of Spring" not in albums.text
    # Path variant gives the same list.
    path_albums = await viewer.get("/catalogue/artists/Talk%20Talk/albums")
    assert "Laughing Stock" in path_albums.text

    pressings = await viewer.get(
        "/catalogue/pressings", params={"artist": "Talk Talk", "title": "Spirit of Eden"}
    )
    assert f"/catalogue/{uk.id}" in pressings.text
    assert f"/catalogue/{us.id}" in pressings.text
    path_pressings = await viewer.get(
        "/catalogue/artists/Talk%20Talk/albums/Spirit%20of%20Eden/pressings"
    )
    assert f"/catalogue/{us.id}" in path_pressings.text

    detail = await viewer.get(f"/catalogue/{uk.id}")
    assert detail.status_code == 200
    assert "Spirit of Eden" in detail.text
    assert "1988" in detail.text


async def test_artist_names_with_slash_work_via_query_routes(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db, artist="AC/DC", title="Powerage")
    albums = await viewer.get("/catalogue/albums", params={"artist": "AC/DC"})
    assert "Powerage" in albums.text
    pressings = await viewer.get(
        "/catalogue/pressings", params={"artist": "AC/DC", "title": "Powerage"}
    )
    assert f"/catalogue/{token.id}" in pressings.text


async def test_catalogue_index_filters(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    await seed_token(db, artist="Vinyl Act", title="LP", year=1975, genres=["Rock"])
    await seed_token(
        db, artist="Disc Act", title="CD", year=1995, media_format=MediaFormat.CD, genres=["Jazz"]
    )

    def names(r: httpx.Response) -> set[str]:
        return {n for n in ("Vinyl Act", "Disc Act") if n in r.text}

    assert names(await viewer.get("/catalogue")) == {"Vinyl Act", "Disc Act"}
    assert names(await viewer.get("/catalogue?fmt=cd")) == {"Disc Act"}
    assert names(await viewer.get("/catalogue?q=vinyl")) == {"Vinyl Act"}
    assert names(await viewer.get("/catalogue?year_from=1990&year_to=1999")) == {"Disc Act"}
    # A genre filter switches to a flat pressing list.
    genre = await viewer.get("/catalogue?genres=jazz")
    assert names(genre) == {"Disc Act"}
    assert "alert-danger" not in genre.text


async def test_artists_partial_paginates(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    for i in range(30):
        db.add(
            Token(
                media_format=MediaFormat.VINYL,
                status=TokenStatus.ACTIVE,
                artist=f"Band {i:02d}",
                title="X",
            )
        )
    await db.commit()
    first = await viewer.get("/catalogue")
    assert "Band 00" in first.text
    assert "Band 29" not in first.text
    page2 = await viewer.get("/catalogue/artists-partial?page=2")
    assert "Band 29" in page2.text
    assert "Band 00" not in page2.text


async def test_peek_and_brief_for_missing_pressing_are_404(viewer: httpx.AsyncClient) -> None:
    for path in ("brief", "peek"):
        assert (await viewer.get(f"/catalogue/{uuid.uuid4()}/{path}")).status_code == 404
        assert (await viewer.get(f"/catalogue/nope/{path}")).status_code == 404


async def test_peek_shows_track_count(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(
        db,
        title="Peekable",
        tracklist=[{"position": "A1", "title": "One"}, {"position": "B1", "title": "Two"}],
    )
    r = await viewer.get(f"/catalogue/{token.id}/peek")
    assert r.status_code == 200
    assert "Peekable" in r.text


# ---------------------------------------------------------------------------
# External sources (Discogs / MusicBrainz) — answered by FakeHttp
# ---------------------------------------------------------------------------

DISCOGS = "https://api.discogs.com"
MB = "https://musicbrainz.org/ws/2"
MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"


def discogs_release(rid: int = 4242, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": rid,
        "title": "Remain in Light",
        "year": 1980,
        "country": "UK",
        "artists": [{"name": "Talking Heads*"}],
        "labels": [{"name": "Sire", "catno": "SRK 6095"}],
        "formats": [{"name": "Vinyl"}],
        "identifiers": [
            {"type": "Barcode", "value": "075992609514"},
            {"type": "Matrix / Runout", "description": "Side A", "value": "SRK-6095-A"},
            {"type": "Matrix / Runout", "description": "Side B", "value": "SRK-6095-B"},
        ],
        "genres": ["Rock"],
        "styles": ["New Wave"],
        "tracklist": [
            {"position": "A1", "title": "Born Under Punches", "duration": "5:46"},
            {"position": "B1", "title": "Once in a Lifetime", "duration": "4:19"},
        ],
        "community": {"rating": {"average": 4.42, "count": 812}},
        "images": [
            {"type": "primary", "uri": "https://i.discogs.com/front.jpg"},
            {"type": "secondary", "uri": "https://i.discogs.com/back.jpg"},
        ],
    }
    data.update(over)
    return data


def mb_release(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": MBID,
        "title": "Remain in Light",
        "date": "1980-10-08",
        "artist-credit": [{"name": "Talking Heads"}],
        "label-info": [{"label": {"name": "Sire"}, "catalog-number": "SRK 6095"}],
        "media": [
            {
                "position": 1,
                "format": '12" Vinyl',
                "tracks": [
                    {
                        "position": "1",
                        "number": "A1",
                        "title": "Born Under Punches",
                        "length": 346000,
                    },
                    {
                        "position": "2",
                        "number": "A2",
                        "title": "Crosseyed and Painless",
                        "length": 285000,
                    },
                ],
            }
        ],
        "genres": [{"name": "new wave", "count": 5}, {"name": "art rock", "count": 3}],
    }
    data.update(over)
    return data


@pytest.fixture
def quick_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Discogs image import waits 0.5 s between images; don't."""
    import asyncio

    real = asyncio.sleep

    async def _sleep(_delay: float, *args: Any, **kwargs: Any) -> Any:
        return await real(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _sleep)


def serve_release(fake_http: Any, data: dict[str, Any], images: bool = True) -> dict[str, bytes]:
    from .fakes import noisy_jpeg

    fake_http.add(f"{DISCOGS}/releases/{data['id']}", json=data)
    blobs: dict[str, bytes] = {}
    if images:
        for i, img in enumerate(data.get("images") or []):
            blobs[img["uri"]] = noisy_jpeg(seed=i)
            fake_http.add(
                img["uri"], content=blobs[img["uri"]], headers={"content-type": "image/jpeg"}
            )
    return blobs


async def test_import_from_discogs_creates_pressing_with_images(
    reviewer: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: Any,
    object_store: FakeObjectStore,
    quick_sleep: None,
) -> None:
    data = discogs_release()
    blobs = serve_release(fake_http, data)

    preview = await reviewer.get("/catalogue/import/discogs/4242")
    assert preview.status_code == 200
    assert "Remain in Light" in preview.text
    assert "SRK 6095" in preview.text

    tid = token_id_from(await reviewer.post("/catalogue/import/discogs/4242"))
    token = await reload(db, Token(id=tid))
    assert (token.artist, token.title, token.year) == ("Talking Heads", "Remain in Light", 1980)
    assert (token.matrix_runout, token.matrix_runout_b) == ("SRK-6095-A", "SRK-6095-B")
    assert token.barcode == "075992609514"
    assert token.genres == ["Rock", "New Wave"]
    assert token.extra is not None
    assert "discogs" in token.extra  # identifiers kept for the runout panel
    (rev,) = await revisions(db, tid)
    assert rev.source is RevisionSource.IMPORT
    assert token.current_revision_id == rev.id

    front, back = sorted(await media(db, tid), key=lambda m: m.source_url or "")[::-1]
    assert (front.region, front.is_primary_cover) == (ImageRegion.COVER_FRONT, True)
    assert (back.region, back.is_primary_cover) == (ImageRegion.COVER_BACK, False)
    assert object_store.objects[(front.bucket, front.object_key)] == blobs[front.source_url or ""]


async def test_refresh_from_discogs_skips_images_already_stored(
    admin: httpx.AsyncClient, db: AsyncSession, fake_http: Any, quick_sleep: None
) -> None:
    data = discogs_release()
    serve_release(fake_http, data)
    token = await seed_token(db, discogs_release_id=4242)

    r = await admin.post(f"/catalogue/{token.id}/refresh-discogs")
    assert r.headers["location"] == f"/catalogue/{token.id}?refreshed=2"
    r = await admin.post(f"/catalogue/{token.id}/refresh-discogs")
    assert r.headers["location"] == f"/catalogue/{token.id}?refreshed=0"
    assert len(await media(db, token.id)) == 2
    # The second run did not download the images again.
    assert fake_http.urls().count("https://i.discogs.com/front.jpg") == 1


async def test_refresh_from_discogs_error_paths(
    admin: httpx.AsyncClient, db: AsyncSession, fake_http: Any, app: Any
) -> None:
    no_id = await seed_token(db)
    assert (await admin.post(f"/catalogue/{no_id.id}/refresh-discogs")).status_code == 400

    token = await seed_token(db, discogs_release_id=9)
    fake_http.add(f"{DISCOGS}/releases/9", status=500, json={"message": "boom"})
    assert (await admin.post(f"/catalogue/{token.id}/refresh-discogs")).status_code == 502

    no_images = await seed_token(db, discogs_release_id=10)
    fake_http.add(f"{DISCOGS}/releases/10", json=discogs_release(10, images=[]))
    r = await admin.post(f"/catalogue/{no_images.id}/refresh-discogs")
    assert r.headers["location"].endswith("?msg=no_discogs_images")

    app.state.object_store = None
    assert (await admin.post(f"/catalogue/{token.id}/refresh-discogs")).status_code == 503


async def test_discogs_preview_of_missing_release_is_404(
    reviewer: httpx.AsyncClient, fake_http: Any
) -> None:
    fake_http.add(f"{DISCOGS}/releases/1", status=404, json={"message": "Release not found."})
    assert (await reviewer.get("/catalogue/import/discogs/1")).status_code == 404


async def test_import_from_musicbrainz(
    reviewer: httpx.AsyncClient, db: AsyncSession, fake_http: Any
) -> None:
    fake_http.add(f"{MB}/release/{MBID}", json=mb_release())
    preview = await reviewer.get(f"/catalogue/import/musicbrainz/{MBID}")
    assert "Remain in Light" in preview.text

    tid = token_id_from(await reviewer.post(f"/catalogue/import/musicbrainz/{MBID}"))
    token = await reload(db, Token(id=tid))
    assert token.musicbrainz_release_id == MBID
    assert (token.artist, token.year, token.catalog_number) == ("Talking Heads", 1980, "SRK 6095")
    (rev,) = await revisions(db, tid)
    assert rev.comment == f"Imported from MusicBrainz release {MBID}"

    assert (await reviewer.get("/catalogue/import/musicbrainz/not-a-uuid")).status_code == 400


async def test_contributor_cannot_import(contributor: httpx.AsyncClient, db: AsyncSession) -> None:
    assert (await contributor.post("/catalogue/import/discogs/4242")).status_code == 403
    assert (await contributor.post(f"/catalogue/import/musicbrainz/{MBID}")).status_code == 403
    assert (await db.execute(select(func.count(Token.id)))).scalar_one() == 0


async def test_new_form_prefills_from_discogs_id_and_flags_duplicates(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: Any
) -> None:
    fake_http.add(f"{DISCOGS}/releases/4242", json=discogs_release())
    r = await contributor.get("/catalogue/new?q=4242")  # a short number is a release id
    assert r.status_code == 200
    assert "Remain in Light" in r.text

    # Two live rows with that id (a merged duplicate keeps it): still one warning, no crash.
    first = await seed_token(db, discogs_release_id=4242, title="Remain in Light")
    await seed_token(db, discogs_release_id=4242, status=TokenStatus.MERGED)
    r = await contributor.get("/catalogue/new?discogs_id=4242")
    assert r.status_code == 200
    assert f"/catalogue/{first.id}" in r.text


async def test_new_form_text_search_marks_releases_already_catalogued(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: Any
) -> None:
    await seed_token(db, discogs_release_id=111)
    fake_http.add(
        f"{DISCOGS}/database/search",
        json={
            "results": [
                {"id": 111, "title": "Talking Heads - Remain in Light", "catno": "SRK 6095"},
                {"id": 222, "title": "Talking Heads - Fear of Music", "catno": ["SRK 6076"]},
            ]
        },
    )
    r = await contributor.get("/catalogue/new?q=talking+heads")
    assert "Fear of Music" in r.text
    assert "Remain in Light" in r.text
    (req,) = fake_http.requests
    assert req.url.params["q"] == "talking heads"

    partial = await contributor.get("/catalogue/search?q=talking+heads")
    assert "Fear of Music" in partial.text


async def test_new_form_barcode_and_musicbrainz_searches(
    contributor: httpx.AsyncClient, fake_http: Any
) -> None:
    fake_http.add(
        f"{DISCOGS}/database/search",
        json={"results": [{"id": 5, "title": "Kraftwerk -- Computer World", "label": ["EMI"]}]},
    )
    r = await contributor.get("/catalogue/new?q=0724352200127")
    assert "Computer World" in r.text
    assert fake_http.requests[0].url.params["barcode"] == "0724352200127"

    fake_http.add(
        f"{MB}/release/",
        json={
            "releases": [
                {
                    "id": MBID,
                    "title": "Trans-Europe Express",
                    "artist-credit": [{"name": "Kraftwerk"}],
                }
            ]
        },
    )
    r = await contributor.get("/catalogue/new?q=kraftwerk&source=musicbrainz")
    assert "Trans-Europe Express" in r.text


async def test_new_form_reports_rate_limit_and_unreachable_api(
    contributor: httpx.AsyncClient, fake_http: Any, network_guard: Any
) -> None:
    fake_http.add(f"{DISCOGS}/database/search", status=429, json={})
    r = await contributor.get("/catalogue/new?q=anything")
    assert r.status_code == 200
    assert "rate limit" in r.text

    network_guard.allow_blocked()  # MusicBrainz is "down"
    r = await contributor.get(f"/catalogue/new?q={MBID}")
    assert "Could not reach MusicBrainz" in r.text


# ---------------------------------------------------------------------------
# Genres, ratings, tracklist, price, vinyl material
# ---------------------------------------------------------------------------


async def test_save_genres_sanitises_and_dedupes(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    r = await contributor.post(
        f"/catalogue/{token.id}/genres", data={"genres": "Rock,  rock , Post\x07 Punk,,Jazz"}
    )
    assert r.headers["location"].endswith("?msg=genres_saved")
    assert (await reload(db, token)).genres == ["Rock", "Post Punk", "Jazz"]

    await contributor.post(f"/catalogue/{token.id}/genres", data={"genres": ""})
    assert (await reload(db, token)).genres is None


async def test_import_genres_merges_discogs_then_falls_back_to_musicbrainz(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: Any
) -> None:
    fake_http.add(f"{DISCOGS}/releases/4242", json=discogs_release())
    token = await seed_token(db, discogs_release_id=4242, genres=["Funk"])
    r = await contributor.post(f"/catalogue/{token.id}/import-genres")
    assert r.headers["location"].endswith("?msg=genres_imported")
    assert (await reload(db, token)).genres == ["Funk", "Rock", "New Wave"]

    fake_http.add(f"{MB}/release/{MBID}", json=mb_release())
    mb_only = await seed_token(db, musicbrainz_release_id=MBID)
    await contributor.post(f"/catalogue/{mb_only.id}/import-genres")
    assert (await reload(db, mb_only)).genres == ["New Wave", "Art Rock"]

    nothing = await seed_token(db)
    r = await contributor.post(f"/catalogue/{nothing.id}/import-genres")
    assert r.headers["location"].endswith("?msg=no_genres_found")


@pytest.mark.parametrize(
    ("raw", "stored"), [("7.3", 7.5), ("10", 10.0), ("11", None), ("-1", None), ("x", None)]
)
async def test_rating_snaps_to_half_points(
    contributor: httpx.AsyncClient, db: AsyncSession, raw: str, stored: float | None
) -> None:
    token = await seed_token(db, personal_rating=5.0)
    await contributor.post(f"/catalogue/{token.id}/rating", data={"rating": raw})
    assert (await reload(db, token)).personal_rating == stored


async def test_import_discogs_community_rating(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: Any
) -> None:
    fake_http.add(f"{DISCOGS}/releases/4242", json=discogs_release())
    token = await seed_token(db, discogs_release_id=4242)
    r = await contributor.post(f"/catalogue/{token.id}/import-discogs-rating")
    assert r.headers["location"].endswith("?msg=discogs_rating_imported")
    row = await reload(db, token)
    assert (row.discogs_community_rating, row.discogs_community_votes) == (4.42, 812)

    fake_http.add(f"{DISCOGS}/releases/7", json=discogs_release(7, community={}))
    unrated = await seed_token(db, discogs_release_id=7)
    r = await contributor.post(f"/catalogue/{unrated.id}/import-discogs-rating")
    assert r.headers["location"].endswith("?msg=no_discogs_rating")

    no_id = await seed_token(db)
    r = await contributor.post(f"/catalogue/{no_id.id}/import-discogs-rating")
    assert r.headers["location"].endswith("?msg=no_discogs_id")


async def test_import_tracks_prefers_discogs_in_auto_mode(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: Any
) -> None:
    fake_http.add(f"{DISCOGS}/releases/4242", json=discogs_release())
    fake_http.add(f"{MB}/release/{MBID}", json=mb_release())
    token = await seed_token(db, discogs_release_id=4242, musicbrainz_release_id=MBID)

    r = await contributor.post(f"/catalogue/{token.id}/import-tracks", data={"source": "auto"})
    assert r.headers["location"].endswith("?msg=tracks_imported")
    row = await reload(db, token)
    assert [t["title"] for t in row.tracklist or []] == ["Born Under Punches", "Once in a Lifetime"]
    assert row.musicbrainz_release_url is None

    # Asking for MusicBrainz explicitly replaces it and records the MB link.
    await contributor.post(f"/catalogue/{token.id}/import-tracks", data={"source": "musicbrainz"})
    row = await reload(db, token)
    assert [t["title"] for t in row.tracklist or []] == [
        "Born Under Punches",
        "Crosseyed and Painless",
    ]
    assert row.musicbrainz_release_url == f"https://musicbrainz.org/release/{MBID}"


async def test_import_tracks_falls_back_to_musicbrainz(
    contributor: httpx.AsyncClient, db: AsyncSession, fake_http: Any
) -> None:
    fake_http.add(f"{DISCOGS}/releases/4242", json=discogs_release(tracklist=[]))
    fake_http.add(f"{MB}/release/{MBID}", json=mb_release())
    token = await seed_token(db, discogs_release_id=4242, musicbrainz_release_id=MBID)
    await contributor.post(f"/catalogue/{token.id}/import-tracks")
    assert len((await reload(db, token)).tracklist or []) == 2

    bare = await seed_token(db)
    r = await contributor.post(f"/catalogue/{bare.id}/import-tracks")
    assert r.headers["location"].endswith("?msg=no_tracks_found")


class StubDiscogsConnector:
    """Marketplace calls of DiscogsConnector with canned numbers."""

    _client = object()  # "already open": the handler must not open/close it

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_marketplace_stats(self, release_id: str) -> dict[str, Any]:
        self.calls.append(release_id)
        return {"lowest_price": 12.5, "num_for_sale": 7, "currency": "EUR", "median_price": 20.0}

    async def fetch_price_suggestions(self, release_id: str) -> dict[str, Any]:
        return {"currency": "EUR", "condition_prices": {"Near Mint (NM or M-)": 25.0}}


async def test_fetch_price_stores_snapshot(
    contributor: httpx.AsyncClient, db: AsyncSession, app: Any
) -> None:
    from mediacat.db.models import TokenPriceHistory

    connector = StubDiscogsConnector()
    app.state.connectors = {"discogs": connector}
    token = await seed_token(db, discogs_release_id=4242)

    r = await contributor.post(f"/catalogue/{token.id}/fetch-price")
    assert r.headers["location"].endswith("?msg=price_fetched")
    snap = (await db.execute(select(TokenPriceHistory))).scalar_one()
    assert (snap.token_id, snap.lowest_price, snap.num_for_sale, snap.currency) == (
        token.id,
        12.5,
        7,
        "EUR",
    )
    assert snap.condition_prices == {"Near Mint (NM or M-)": 25.0, "_median": 20.0}
    assert connector.calls == ["4242"]


async def test_vinyl_material(contributor: httpx.AsyncClient, db: AsyncSession) -> None:
    token = await seed_token(db)
    r = await contributor.post(
        f"/catalogue/{token.id}/vinyl-material", data={"vinyl_material": " Virgin vinyl "}
    )
    assert r.headers["location"].endswith("?msg=vinyl_material_saved")
    assert (await reload(db, token)).vinyl_material == "Virgin vinyl"


@pytest.mark.parametrize(
    "path", ["genres", "rating", "import-genres", "import-tracks", "fetch-price", "vinyl-material"]
)
async def test_viewer_cannot_change_pressing_data(
    viewer: httpx.AsyncClient, db: AsyncSession, path: str
) -> None:
    token = await seed_token(db, genres=["Rock"], personal_rating=8.0)
    assert (await viewer.post(f"/catalogue/{token.id}/{path}", data={})).status_code == 403
    row = await reload(db, token)
    assert (row.genres, row.personal_rating) == (["Rock"], 8.0)


async def test_sync_discogs_updates_everything(
    contributor: httpx.AsyncClient,
    db: AsyncSession,
    fake_http: Any,
    app: Any,
    quick_sleep: None,
) -> None:
    from mediacat.db.models import TokenPriceHistory

    serve_release(fake_http, discogs_release())
    app.state.connectors = {"discogs": StubDiscogsConnector()}
    token = await seed_token(db, discogs_release_id=4242, genres=["Funk"])

    confirm = await contributor.get(f"/catalogue/{token.id}/sync-discogs")
    assert confirm.status_code == 200

    r = await contributor.post(f"/catalogue/{token.id}/sync-discogs")
    assert r.headers["location"].endswith("?msg=sync_complete")
    row = await reload(db, token)
    assert row.genres == ["Funk", "Rock", "New Wave"]
    assert row.discogs_community_rating == 4.42
    assert [t["position"] for t in row.tracklist or []] == ["A1", "B1"]
    country = await db.get(Country, row.country_id)
    assert country is not None
    assert country.name == "UK"
    assert len(await media(db, token.id)) == 2
    assert (await db.execute(select(func.count()).select_from(TokenPriceHistory))).scalar_one() == 1
    assert "discogs" in (row.extra or {})


# ---------------------------------------------------------------------------
# Matrix / runout: human correction, vision analysis, apply OCR
# ---------------------------------------------------------------------------


async def test_correct_matrix_field_records_reason_in_revision(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(
        db,
        matrix_runout="KAB 1 A-1U",
        matrix_runout_parsed={
            "matrix_number": {"value": "KAB 1 A-1U", "confidence": 0.6, "source": "vision"}
        },
    )
    r = await reviewer.post(
        f"/catalogue/{token.id}/correct-matrix-field",
        data={
            "side": "a",
            "field": "matrix_number",
            "value": "KAB 1 A-2U",
            "reason_code": "physical_inspection",
            "reason_text": "checked under a lamp",
        },
    )
    assert r.headers["location"].endswith("?msg=field_corrected")
    row = await reload(db, token)
    assert row.matrix_runout == "KAB 1 A-2U"
    assert row.matrix_runout_parsed == {
        "matrix_number": {"value": "KAB 1 A-2U", "confidence": 1.0, "source": "human"}
    }
    (rev,) = await revisions(db, token.id)
    assert rev.data["correction"]["old_value"] == "KAB 1 A-1U"
    assert rev.data["correction"]["reason_code"] == "physical_inspection"
    assert rev.comment is not None
    assert "'KAB 1 A-1U' → 'KAB 1 A-2U'" in rev.comment


async def test_correct_matrix_field_rejects_unknown_field(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    r = await reviewer.post(
        f"/catalogue/{token.id}/correct-matrix-field", data={"field": "__class__", "value": "x"}
    )
    assert r.status_code == 422
    assert await revisions(db, token.id) == []


@dataclass
class StubVisionBackend:
    """Vision backend returning one canned runout transcription."""

    parsed: dict[str, Any]
    calls: list[tuple[str, str]] = dc_field(default_factory=list)

    @property
    def provider_name(self) -> str:
        return "stub"

    async def transcribe(
        self, image_bytes: bytes, mime_type: str, prompt: str, **kwargs: Any
    ) -> Any:
        from mediacat.vision.adapter import VisionResponse

        self.calls.append((mime_type, kwargs.get("system", "")))
        return VisionResponse(
            text="raw model text", parsed=self.parsed, provider="stub", model="m", confidence=0.8
        )


async def test_analyse_runout_image_then_apply_to_pressing(
    reviewer: httpx.AsyncClient, db: AsyncSession, app: Any
) -> None:
    from mediacat.db.models import OcrArtifact
    from mediacat.vision.adapter import HybridVision

    backend = StubVisionBackend(
        parsed={
            "matrix_number": "SRK-6095-B",
            "stamper_code": "1Y",
            "sid_codes": [{"code": "IFPI L042", "type": "mastering"}],
        }
    )
    app.state.vision = HybridVision(backend)
    token = await seed_token(db)
    await upload(reviewer, token.id, png_bytes(), region="runout_b")
    (mo,) = await media(db, token.id)

    # Nothing to apply before an analysis exists.
    assert (
        await reviewer.post(f"/catalogue/{token.id}/images/{mo.id}/apply-ocr")
    ).status_code == 422

    r = await reviewer.post(f"/catalogue/{token.id}/images/{mo.id}/analyse")
    assert r.headers["location"].endswith("?msg=analysed")
    artifact = (await db.execute(select(OcrArtifact))).scalar_one()
    assert artifact.media_object_id == mo.id
    assert artifact.metadata_ is not None
    assert artifact.metadata_["matrix_number"] == "SRK-6095-B"
    assert len(backend.calls) == 1

    r = await reviewer.post(f"/catalogue/{token.id}/images/{mo.id}/apply-ocr")
    assert r.headers["location"].endswith("?msg=ocr_applied")
    row = await reload(db, token)
    assert row.matrix_runout is None  # side B image
    assert row.matrix_runout_b == "SRK-6095-B 1Y IFPI L042"
    assert row.matrix_runout_b_parsed is not None
    assert row.matrix_runout_b_parsed["sid_mastering"]["value"] == "IFPI L042"
    (rev,) = await revisions(db, token.id)
    assert rev.source is RevisionSource.VISION

    detail = await reviewer.get(f"/catalogue/{token.id}")
    assert "SRK-6095-B" in detail.text


async def test_analyse_needs_a_vision_backend(
    reviewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    token = await seed_token(db)
    await upload(reviewer, token.id, png_bytes(), region="runout_a")
    (mo,) = await media(db, token.id)
    assert (await reviewer.post(f"/catalogue/{token.id}/images/{mo.id}/analyse")).status_code == 503


# ---------------------------------------------------------------------------
# Sales history (pasted Discogs table)
# ---------------------------------------------------------------------------

_SALES_TEXT = """\
Date\tCondition\tSleeve\tPrice
2024-03-01\tNear Mint (NM or M-)\tVery Good Plus (VG+)\t€25.00
2024-02-11\tVery Good Plus (VG+)\tVery Good Plus (VG+)\t€18.00
2024-01-20\tVery Good (VG)\tGood (G)\t€9.50
"""


async def test_sales_history_preview_then_confirm_stores_import(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    from mediacat.db.models import DiscogsSalesImport

    token = await seed_token(db, discogs_release_id=4242)
    preview = await contributor.post(
        f"/catalogue/{token.id}/import-sales-history",
        data={"raw_text": _SALES_TEXT, "source_url": "https://evil.example/x"},
    )
    assert preview.status_code == 200
    assert "URL must start with" in preview.text
    assert (
        await db.execute(select(func.count()).select_from(DiscogsSalesImport))
    ).scalar_one() == 0

    r = await contributor.post(
        f"/catalogue/{token.id}/import-sales-history/confirm",
        data={"raw_text": _SALES_TEXT, "source_url": "https://www.discogs.com/sell/history/4242"},
    )
    assert r.headers["location"].endswith("?msg=sales_imported")
    imp = (await db.execute(select(DiscogsSalesImport))).scalar_one()
    assert imp.token_id == token.id
    assert imp.source_url == "https://www.discogs.com/sell/history/4242"
    assert (imp.row_count, imp.currency) == (3, "EUR")
    assert imp.raw_import_text is None  # raw text only kept with consent

    page = await contributor.get(f"/catalogue/{token.id}")
    assert page.status_code == 200


async def test_sales_history_confirm_rejects_unparseable_text(
    contributor: httpx.AsyncClient, db: AsyncSession
) -> None:
    from mediacat.db.models import DiscogsSalesImport

    token = await seed_token(db)
    r = await contributor.post(
        f"/catalogue/{token.id}/import-sales-history/confirm", data={"raw_text": "  "}
    )
    assert r.headers["location"].endswith("?msg=sales_import_parse_error")
    assert (
        await db.execute(select(func.count()).select_from(DiscogsSalesImport))
    ).scalar_one() == 0


# ---------------------------------------------------------------------------
# Small endpoints: autocomplete, facets, detail rendering
# ---------------------------------------------------------------------------


async def test_country_autocomplete(viewer: httpx.AsyncClient) -> None:
    r = await viewer.get("/catalogue/autocomplete/countries", params={"country_q": "germ"})
    assert r.status_code == 200
    assert "Germany" in r.text
    assert "France" not in r.text


@pytest.mark.xfail(
    strict=True,
    reason="BUG: catalogue_facets calls _require_role(request) with no roles, so every user gets 403",
)
async def test_facets_endpoint_counts(viewer: httpx.AsyncClient, db: AsyncSession) -> None:
    await seed_token(db, artist="A", year=1971, genres=["Rock", "Prog"])
    await seed_token(db, artist="B", year=1975, genres=["Rock"], media_format=MediaFormat.CD)
    r = await viewer.get("/catalogue/facets")
    assert r.status_code == 200
    body = r.json()
    genres = {g["name"]: g["count"] for g in body["genres"]}
    assert genres == {"Rock": 2, "Prog": 1}
    assert {f["name"]: f["count"] for f in body["formats"]} == {"vinyl": 1, "cd": 1}
    assert [d["decade"] for d in body["decades"]] == [1970]
    # The format filter narrows genres but not the format counts.
    cd = (await viewer.get("/catalogue/facets?fmt=cd")).json()
    assert {g["name"] for g in cd["genres"]} == {"Rock"}
    assert len(cd["formats"]) == 2


async def test_pressing_detail_renders_genres_and_collection(
    viewer: httpx.AsyncClient, db: AsyncSession
) -> None:
    # The handler also groups the tracklist by side (tracklist_discs), but
    # pressing_detail.html has never rendered it.
    token = await seed_token(
        db,
        title="Sided",
        genres=["Krautrock"],
        tracklist=[
            {"position": "A1", "title": "Hallogallo"},
            {"position": "A2", "title": "Sonderangebot"},
            {"position": "B1", "title": "Weissensee"},
        ],
        extra={"discogs_collection": {"known": True}},
    )
    r = await viewer.get(f"/catalogue/{token.id}")
    assert r.status_code == 200
    assert "Sided" in r.text
    assert "Krautrock" in r.text
