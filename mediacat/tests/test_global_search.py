"""Top-bar search: artists, album titles and track titles."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi.templating import Jinja2Templates
from sqlalchemy.dialects import postgresql

from mediacat.web import routes
from mediacat.web.app import _WEB_DIR


class _RecordingDb:
    """Records executed statements and answers each with the queued rows."""

    def __init__(self, *results: list[Any]) -> None:
        self.statements: list[Any] = []
        self._results = list(results)

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        rows = self._results.pop(0) if self._results else []
        return SimpleNamespace(all=lambda: rows)


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _hit(**kw: Any) -> dict[str, Any]:
    base = {
        "id": "t1",
        "artist": "Pink Floyd",
        "enc_artist": "Pink%20Floyd",
        "title": "The Dark Side Of The Moon",
        "year": 1973,
        "media_format": "vinyl",
        "cover_url": None,
    }
    return base | kw


def test_like_pattern_matches_wildcards_literally() -> None:
    assert routes._like_pattern("50%_off") == "%50\\%\\_off%"
    assert routes._like_pattern("a\\b") == "%a\\\\b%"


def test_first_hit_prefers_artists_then_albums_then_tracks() -> None:
    artists = [{"enc": "ABBA", "name": "ABBA"}]
    album = _hit(id="a1")
    track = _hit(id="t9", track_title="Money")
    assert routes._first_hit(artists, [album], [])["brief_id"] == ""
    assert routes._first_hit([], [album], [track]) == {
        "artist_enc": "Pink%20Floyd",
        "artist_name": "Pink Floyd",
        "brief_id": "a1",
    }
    assert routes._first_hit([], [], [track])["brief_id"] == "t9"
    assert routes._first_hit([], [], [])["artist_enc"] == ""


async def test_track_search_reads_the_tracklist_json() -> None:
    row = SimpleNamespace(
        id="t1",
        artist="Pink Floyd",
        title="The Dark Side Of The Moon",
        year=1973,
        media_format="vinyl",
        track_title="Money",
        position="B1",
    )
    cover = SimpleNamespace(id="m1", token_id="t1")
    db = _RecordingDb([row], [cover])
    hits = await routes._search_tracks(db, routes._like_pattern("money"))

    sql = _sql(db.statements[0])
    assert "jsonb_array_elements" in sql
    assert "LATERAL" in sql
    assert "jsonb_typeof" in sql  # non-array tracklists cannot break the query
    assert hits == [_hit(cover_url="/catalogue/t1/images/m1", track_title="Money", position="B1")]


async def test_album_search_matches_title_and_catalogue_number_only() -> None:
    db = _RecordingDb([], [])
    await routes._search_pressings(db, "%x%", by_artist=False, by_album=True)
    where = _sql(db.statements[0]).split("WHERE", 1)[1]
    assert "tokens.title ILIKE" in where
    assert "tokens.catalog_number ILIKE" in where
    assert "tokens.artist ILIKE" not in where


def _render_hits(**ctx: Any) -> str:
    env = Jinja2Templates(directory=str(_WEB_DIR / "templates")).env
    base: dict[str, Any] = {
        "q": "money",
        "scope": "all",
        "artists": [],
        "albums": [],
        "tracks": [],
        "albums_more": False,
        "tracks_more": False,
        "first": {"artist_enc": "", "artist_name": "", "brief_id": ""},
    }
    return env.get_template("partials/search_3panel.html").render(base | ctx)


def test_track_hits_open_artist_and_pressing() -> None:
    html = _render_hits(
        scope="tracks",
        tracks=[_hit(track_title="Money", position="B1")],
        first={"artist_enc": "Pink%20Floyd", "artist_name": "Pink Floyd", "brief_id": "t1"},
    )
    assert "Money" in html
    assert 'class="sp-hit-pos">B1<' in html
    assert 'hx-get="/artists/detail?artist=Pink%20Floyd"' in html
    assert 'data-mc-brief="/catalogue/t1/brief"' in html
    assert 'hx-get="/catalogue/t1/brief" hx-trigger="load"' in html  # right panel
    assert "hx-on" not in html  # inline handlers are blocked by the CSP


def test_album_hits_are_grouped_under_artists() -> None:
    html = _render_hits(
        artists=[
            {
                "name": "Pink Floyd",
                "enc": "Pink%20Floyd",
                "pressing_count": 3,
                "year_range": "1973",
                "portrait_url": None,
                "cover_url": None,
            }
        ],
        albums=[_hit(catalog_number="SHVL 804")],
    )
    assert html.index(">Artists <") < html.index(">Albums <")
    assert "SHVL 804" in html


def test_no_hits_suggests_the_track_search() -> None:
    html = _render_hits(scope="albums")
    assert "No albums match" in html
    assert "Tracks" in html
