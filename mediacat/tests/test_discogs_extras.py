"""Tests for Discogs runout grouping, collection fields and the album sheet PDF."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mediacat.ingestion.discogs import DiscogsConnector
from mediacat.ingestion.discogs_extras import (
    classify_field,
    collection_view,
    format_qty,
    group_runouts,
    is_yes,
    merge_collection,
    side_to_lp,
    track_lp,
    variant_runout_fill,
)

# Pink Floyd — The Division Bell (Discogs release 34394860), abridged
DIVISION_BELL = [
    {"type": "Barcode", "value": "888751843110", "description": "Sticker, scanned"},
    {"type": "Rights Society", "value": "BIEM/SDRM"},
    {
        "type": "Matrix / Runout",
        "value": "2564629328 - A RE3 TML-M ++ BE 49494 - 01 A3",
        "description": "Runout side A, variant 1",
    },
    {
        "type": "Matrix / Runout",
        "value": "2564629328 - B - RE2 TML-M  ¯V¯ BE 49494-01 B2 == U",
        "description": "Runout side B, variant 1",
    },
    {
        "type": "Matrix / Runout",
        "value": "2564629328 - C - RE2 TML-M ¯V¯  3VT BE 49494-02 C2",
        "description": "Runout side C, variant 1",
    },
    {
        "type": "Matrix / Runout",
        "value": "2564629328 - D - RE2 TML-M ¯V¯ R=∇  BE 49494-02 D2 ",
        "description": "Runout side D, variant 1",
    },
    {
        "type": "Matrix / Runout",
        "value": "2564629328 - D - RE 2 TML-M ¯V¯ 6+⊥ BE 49494-02 D2",
        "description": "Runout side D, variant 2",
    },
    {
        "type": "Matrix / Runout",
        "value": "2564629328 - A RE3 TML-M  BE 49494 - 01 A3 6+⊥",
        "description": "Runout side A, variant 2",
    },
]

# Pink Floyd — The Dark Side Of The Moon (Discogs release 370444), abridged;
# other identifiers listed after the runouts, variants out of order.
DARK_SIDE = [
    {
        "type": "Matrix / Runout",
        "value": "05 249 A - 2 #",
        "description": "Runout side A, variant 3",
    },
    {"type": "Matrix / Runout", "value": "05 249 A - 1", "description": "Runout side A, variant 1"},
    {"type": "Matrix / Runout", "value": "05 249 B - 1", "description": "Runout side B, variant 1"},
    {
        "type": "Matrix / Runout",
        "value": "05 249 B - 2 #",
        "description": "Runout side B, variant 3",
    },
    {"type": "Price Code", "value": "D", "description": "Boxed, on sleeve back"},
    {"type": "Rights Society", "value": "GEMA", "description": "Boxed"},
]


# ---------------------------------------------------------------------------
# Runout grouping
# ---------------------------------------------------------------------------


def test_side_to_lp() -> None:
    assert side_to_lp("A") == (1, "A")
    assert side_to_lp("b") == (1, "B")
    assert side_to_lp("C") == (2, "A")
    assert side_to_lp("D") == (2, "B")
    assert side_to_lp("3") == (2, "A")
    assert side_to_lp("") is None


def test_group_runouts_double_lp_variants() -> None:
    g = group_runouts(DIVISION_BELL)
    assert g["runout_count"] == 6
    assert [v["label"] for v in g["variants"]] == ["Variant 1", "Variant 2"]
    v1 = g["variants"][0]
    assert [lp["lp"] for lp in v1["lps"]] == [1, 2]
    assert [s["side"] for s in v1["lps"][1]["sides"]] == ["C", "D"]
    # Runout text is kept verbatim (double spaces, symbols)
    assert (
        v1["lps"][0]["sides"][1]["value"] == "2564629328 - B - RE2 TML-M  ¯V¯ BE 49494-01 B2 == U"
    )
    # Variant 2 sides sorted although Discogs listed D before A
    v2 = g["variants"][1]
    assert [lp["lp"] for lp in v2["lps"]] == [1, 2]
    assert {t["type"] for t in g["other"]} == {"Barcode", "Rights Society"}


def test_group_runouts_orders_variants_numerically() -> None:
    g = group_runouts(DARK_SIDE)
    assert [v["variant"] for v in g["variants"]] == ["1", "3"]
    assert [o["type"] for o in g["other"]] == ["Price Code", "Rights Society"]


def test_group_runouts_unnumbered_and_unsided() -> None:
    g = group_runouts(
        [
            {"type": "Matrix / Runout", "value": "XYZ 1", "description": "Etched"},
            {"type": "Matrix / Runout", "value": "XYZ 2", "description": "Side B, stamped"},
        ]
    )
    (only,) = g["variants"]
    assert only["label"] == "Unnumbered"
    assert only["unsided"] == [{"value": "XYZ 1", "note": "Etched"}]
    assert only["lps"][0]["sides"][0]["note"] == "stamped"


def test_variant_runout_fill_maps_lp2_to_c_d() -> None:
    fill = variant_runout_fill(group_runouts(DIVISION_BELL)["variants"][0])
    assert fill["runout_lp1_a"].startswith("2564629328 - A")
    assert fill["runout_lp2_a"].startswith("2564629328 - C")
    assert fill["runout_lp2_b"].startswith("2564629328 - D")


# ---------------------------------------------------------------------------
# Collection fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Runout / Matrix LP1 - A", "runout_lp1_a"),
        ("Runout / Matrix LP1 - B", "runout_lp1_b"),
        ("Runout / Matrix LP2 - A", "runout_lp2_a"),
        ("Runout/Matrix LP 2 Side B", "runout_lp2_b"),
        ("Wax Side A", "runout_lp1_a"),
        ("Autographed", "autographed"),
        ("Signed by", "signed_by"),
        ("Signed on", "signed_on"),
        ("Price (Signed)", "price_estimate"),
        ("Media Condition", "media_condition"),
        ("Notes", "notes"),
        ("Purchase price", None),
        ("Runout / Matrix LP3 - A", None),
    ],
)
def test_classify_field(name: str, key: str | None) -> None:
    assert classify_field(name) == key


def test_is_yes() -> None:
    assert all(is_yes(v) for v in ("Yes", "ja", " x "))
    assert not any(is_yes(v) for v in ("No", "", None))


FIELDS = [
    {
        "id": 1,
        "name": "Media Condition",
        "type": "dropdown",
        "options": ["Mint (M)"],
        "position": 1,
    },
    {"id": 5, "name": "Runout / Matrix LP1 - A", "type": "textarea", "options": [], "position": 4},
    {"id": 7, "name": "Autographed", "type": "dropdown", "options": ["Yes", "No"], "position": 5},
    {"id": 8, "name": "Signed by", "type": "textarea", "options": [], "position": 6},
]


def test_merge_collection_keeps_unsynced_local_edits() -> None:
    instance = {
        "instance_id": 99,
        "folder_id": 1,
        "notes": [{"field_id": 5, "value": "REMOTE A"}, {"field_id": 7, "value": "Yes"}],
    }
    first = merge_collection(None, username="me", fields=FIELDS, instance=instance)
    assert first["values"]["5"] == "REMOTE A"
    assert first["in_collection"]
    assert first["instance_id"] == 99

    # Local edit not yet written back survives a refresh; untouched fields follow Discogs
    first["values"]["5"] = "LOCAL A"
    instance["notes"] = [{"field_id": 5, "value": "REMOTE A2"}, {"field_id": 7, "value": "No"}]
    second = merge_collection(first, username="me", fields=FIELDS, instance=instance)
    assert second["values"]["5"] == "LOCAL A"
    assert second["discogs_values"]["5"] == "REMOTE A2"
    assert second["values"]["7"] == "No"


def test_collection_view_known_keys_and_pending() -> None:
    coll = merge_collection(
        None,
        username="me",
        fields=FIELDS,
        instance={
            "instance_id": 1,
            "folder_id": 1,
            "notes": [{"field_id": 7, "value": "Yes"}, {"field_id": 8, "value": "Nick Mason"}],
        },
    )
    coll["values"]["5"] = "A-1"
    view = collection_view({"discogs_collection": coll})
    assert view["present"]
    assert view["signed"]
    assert view["signed_by"] == "Nick Mason"
    assert view["known"]["runout_lp1_a"] == "A-1"
    assert view["pending"] == 1
    assert collection_view(None)["present"] is False


def test_track_lp_and_format_qty() -> None:
    assert track_lp({"position": "A3"}) == 1
    assert track_lp({"position": "D1"}) == 2
    assert track_lp({"position": "2-4"}) == 2
    assert track_lp({"position": "", "disc": 3}) == 3
    assert format_qty([{"name": "Vinyl", "qty": "2"}]) == 2
    assert format_qty([{"name": "CD", "qty": "1"}]) is None


# ---------------------------------------------------------------------------
# Connector write-back
# ---------------------------------------------------------------------------


async def test_set_collection_field_posts_json_and_accepts_204() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    conn = DiscogsConnector(name="t", base_url="https://api.discogs.com", max_retries=1)
    conn._client = httpx.AsyncClient(
        base_url="https://api.discogs.com", transport=httpx.MockTransport(handler)
    )
    await conn.set_collection_field(
        "me", folder_id=1, release_id=370444, instance_id=99, field_id=5, value="05 249 A - 1"
    )
    await conn.close()
    assert seen == {
        "method": "POST",
        "path": "/users/me/collection/folders/1/releases/370444/instances/99/fields/5",
        "body": {"value": "05 249 A - 1"},
    }


# ---------------------------------------------------------------------------
# Album sheet
# ---------------------------------------------------------------------------


def _token(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "artist": "Pink Floyd",
        "title": "The Division Bell",
        "year": 2025,
        "country": SimpleNamespace(name="Worldwide"),
        "tracklist": [
            {"position": "A1", "title": "Cluster One"},
            {"position": "B1", "title": "Take It Back"},
            {"position": "C1", "title": "Marooned"},
            {"position": "D1", "title": "High Hopes"},
        ],
        "matrix_runout": "MATRIX A FALLBACK",
        "matrix_runout_b": None,
        "extra": {"discogs": {"format_qty": 2}},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_sheet_data_groups_lps_and_runouts() -> None:
    from mediacat.web.discogs_details import build_sheet_data

    data = build_sheet_data(_token())  # type: ignore[arg-type]
    assert [lp["lp"] for lp in data["lps"]] == [1, 2]
    assert data["lps"][1]["tracks"] == [("C1", "Marooned"), ("D1", "High Hopes")]
    assert data["lps"][0]["runouts"][0] == ("Side A", "MATRIX A FALLBACK")
    assert data["lps"][1]["runouts"][0][0] == "Side C"
    assert data["lp_total"] == 2
    assert not data["signed"]


def test_build_sheet_data_box_set_count() -> None:
    from mediacat.web.discogs_details import build_sheet_data

    data = build_sheet_data(_token(extra={"discogs": {"format_qty": 5}}))  # type: ignore[arg-type]
    assert len(data["lps"]) == 2
    assert data["lp_total"] == 5


def test_album_sheet_pdf_renders_and_truncates() -> None:
    from mediacat.web.print_sheet import (
        MARGIN,
        TRACK_POS_W,
        TRACK_RIGHT,
        _register_fonts,
        build_album_sheet,
        truncate,
    )

    long_title = "Symphony No. 9 in D minor, Op. 125 'Choral': IV. Presto - Allegro assai " * 3
    pdf = build_album_sheet(
        {
            "artist": "Beethoven",
            "title": "Symphonies",
            "year": 1963,
            "country": "Germany",
            "lps": [
                {
                    "lp": 1,
                    "tracks": [("A1", long_title)],
                    "runouts": [("Side A", "2564629328 - D ¯V¯ 6+⊥")],
                },
                {"lp": 2, "tracks": [], "runouts": []},
            ],
            "lp_total": 4,
            "signed": True,
            "signed_by": "Herbert von Karajan",
            "signed_on": "Front",
            "price_estimate": "CHF 450",
        }
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000

    font = _register_fonts()["sans"]
    max_w = TRACK_RIGHT - (MARGIN + TRACK_POS_W)
    short = truncate(long_title, font, 9.5, max_w)
    assert short.endswith("…")
    assert len(short) < len(long_title)


# ---------------------------------------------------------------------------
# Collection form route
# ---------------------------------------------------------------------------


def _collection_app(monkeypatch: pytest.MonkeyPatch, writes: list) -> tuple:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mediacat.web import discogs_details as dd

    coll = merge_collection(
        None,
        username="me",
        fields=FIELDS,
        instance={"instance_id": 99, "folder_id": 1, "notes": [{"field_id": 5, "value": "OLD"}]},
    )
    token = SimpleNamespace(id="t1", discogs_release_id=370444, extra={"discogs_collection": coll})
    saved: dict = {}

    async def fake_load(request, token_id, **kw):
        return token

    async def fake_save(request, token_id, updates):
        saved.update(updates)

    class FakeConn:
        async def set_collection_field(self, username, **kw):
            writes.append((username, kw))

        async def close(self) -> None:
            pass

    async def fake_open(request):
        return FakeConn()

    monkeypatch.setattr(dd, "_load", fake_load)
    monkeypatch.setattr(dd, "_save_extra", fake_save)
    monkeypatch.setattr(dd, "open_collection_connector", fake_open)

    app = FastAPI()

    @app.middleware("http")
    async def role(request, call_next):  # type: ignore[no-untyped-def]
        request.state.user_role = "admin"
        request.state.user_id = "u1"
        return await call_next(request)

    app.include_router(dd.discogs_details_router)
    return TestClient(app), saved


def test_collection_save_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list = []
    client, saved = _collection_app(monkeypatch, writes)
    r = client.post(
        "/catalogue/t1/discogs-collection",
        data={"op": "save", "f_5": "05 249 A - 1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "msg=collection_saved" in r.headers["location"]
    assert saved["discogs_collection"]["values"]["5"] == "05 249 A - 1"
    assert writes == []


def test_collection_save_and_write_back(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list = []
    client, saved = _collection_app(monkeypatch, writes)
    r = client.post(
        "/catalogue/t1/discogs-collection",
        data={"op": "writeback", "f_5": "05 249 A - 1"},
        follow_redirects=False,
    )
    assert "msg=writeback_ok&n=1" in r.headers["location"]
    assert writes == [
        (
            "me",
            {
                "folder_id": 1,
                "release_id": 370444,
                "instance_id": 99,
                "field_id": 5,
                "value": "05 249 A - 1",
            },
        )
    ]
    assert saved["discogs_collection"]["discogs_values"]["5"] == "05 249 A - 1"


def test_price_label_uses_print_month() -> None:
    from datetime import UTC, datetime

    from mediacat.web.print_sheet import price_label

    assert price_label(datetime(2026, 10, 31, tzinfo=UTC)) == "October-2026 - price estimate -"


def test_sheet_data_carries_price_estimate() -> None:
    from mediacat.web.discogs_details import build_sheet_data

    coll = merge_collection(
        None,
        username="me",
        fields=[
            *FIELDS,
            {"id": 13, "name": "Price (Signed)", "type": "textarea", "options": [], "position": 9},
        ],
        instance={
            "instance_id": 1,
            "folder_id": 1,
            "notes": [{"field_id": 13, "value": "CHF 450"}],
        },
    )
    data = build_sheet_data(_token(extra={"discogs_collection": coll}))  # type: ignore[arg-type]
    assert data["price_estimate"] == "CHF 450"


def test_mark_matching_variants_exact_text() -> None:
    from mediacat.ingestion.discogs_extras import mark_matching_variants

    groups = group_runouts(DARK_SIDE)  # variants 1 and 3
    # Repeated spaces and case are ignored; anything else must match exactly
    known = {"runout_lp1_a": "05  249 a - 2 #", "runout_lp1_b": "05 249 B - 2 #"}
    marked = mark_matching_variants(groups, known)
    assert [v["matches"] for v in marked["variants"]] == [False, True]
    assert marked["any_match"]

    assert not mark_matching_variants(group_runouts(DARK_SIDE), {})["any_match"]
    partial = mark_matching_variants(group_runouts(DARK_SIDE), {"runout_lp1_a": "05 249 A - 1"})
    assert [v["matches"] for v in partial["variants"]] == [True, False]


# Japanese pressing 9689531: sides named first, variant in brackets, plus label matrices
DSOTM_JP = [
    {"type": "Matrix / Runout", "value": "SHVL-804A", "description": "Side A label"},
    {"type": "Matrix / Runout", "value": "SHVL-804B", "description": "Side B label"},
    {
        "type": "Matrix / Runout",
        "value": "SHVL - 804 - A 1S 68 4 _ 7 W9W",
        "description": "Side A runout, stamped (variant 1)",
    },
    {
        "type": "Matrix / Runout",
        "value": "SHVL - 804 - B 1S2 41",
        "description": "Side B runout, stamped (variant 1)",
    },
    {
        "type": "Matrix / Runout",
        "value": "SHVL - 804 - A 1S 82 〄 5 _ 3 W",
        "description": "Side A runout, stamped (variant 2)",
    },
    {
        "type": "Matrix / Runout",
        "value": "SHVL - 804 - B 1S2 55 〄",
        "description": "Side B runout, stamped (variant 2)",
    },
    {"type": "Rights Society", "value": "JASRAC", "description": ""},
]


def test_group_runouts_side_first_bracketed_variant_and_labels() -> None:
    groups = group_runouts(DSOTM_JP)
    labels = [v["label"] for v in groups["variants"]]
    assert labels == ["Variant 1", "Variant 2", "Label"]
    v1 = groups["variants"][0]
    assert variant_runout_fill(v1) == {
        "runout_lp1_a": "SHVL - 804 - A 1S 68 4 _ 7 W9W",
        "runout_lp1_b": "SHVL - 804 - B 1S2 41",
    }
    assert v1["lps"][0]["sides"][0]["note"] == "stamped"
    assert [s["value"] for s in groups["variants"][2]["lps"][0]["sides"]] == [
        "SHVL-804A",
        "SHVL-804B",
    ]
    assert groups["runout_count"] == 6
    assert [o["type"] for o in groups["other"]] == ["Rights Society"]


async def test_refresh_rides_out_short_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from mediacat.web import catalogue, discogs_details

    calls: list[int] = []

    async def fake_fetch(release_id: int) -> dict[str, Any]:
        calls.append(release_id)
        if len(calls) < 3:
            req = httpx.Request("GET", "https://api.discogs.com/releases/1")
            raise httpx.HTTPStatusError(
                "429", request=req, response=httpx.Response(429, request=req)
            )
        return {"identifiers": DSOTM_JP}

    monkeypatch.setattr(catalogue, "_fetch_discogs_release", fake_fetch)
    monkeypatch.setattr(discogs_details, "RATE_LIMIT_WAITS", (0.0, 0.0, 0.0))
    data = await discogs_details._fetch_release_patiently(9689531)
    assert len(calls) == 3
    assert data["identifiers"] == DSOTM_JP

    calls.clear()

    async def always_429(release_id: int) -> dict[str, Any]:
        calls.append(release_id)
        req = httpx.Request("GET", "https://api.discogs.com/releases/1")
        raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))

    monkeypatch.setattr(catalogue, "_fetch_discogs_release", always_429)
    with pytest.raises(httpx.HTTPStatusError):
        await discogs_details._fetch_release_patiently(9689531)
    assert len(calls) == len(discogs_details.RATE_LIMIT_WAITS) + 1
