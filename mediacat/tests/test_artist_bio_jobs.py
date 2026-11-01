"""Artist bio lookups run as server-side tasks that outlive the request."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from mediacat.ingestion.artist_bio import BioResult
from mediacat.web import admin, artists
from mediacat.web.app import _WEB_DIR
from mediacat.web.routes import set_templates


class _FakeDb:
    """Just enough of an AsyncSession for the bio cache write."""

    def __init__(self, saved: list[Any]) -> None:
        self._saved = saved

    async def __aenter__(self) -> _FakeDb:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, row: Any) -> None:
        self._saved.append(row)

    async def commit(self) -> None:
        return None


def _request(saved: list[Any]) -> Request:
    state = SimpleNamespace(db_session_factory=lambda: _FakeDb(saved), llm=None)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/artists/info",
        "query_string": b"",
        "headers": [],
        "app": SimpleNamespace(state=state),
        "state": {},
    }
    return Request(scope)


@pytest.fixture
def slow_lookup(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """resolve_artist_bio that blocks until the test releases it."""
    ctl: dict[str, Any] = {"calls": 0, "started": asyncio.Event(), "release": asyncio.Event()}

    async def slow(name: str, **kw: Any) -> BioResult:
        ctl["calls"] += 1
        ctl["started"].set()
        await ctl["release"].wait()
        return BioResult(extract="A bio.", source="test", found=True, thumbnail_url="x.jpg")

    async def no_token(request: Any) -> str:
        return ""

    monkeypatch.setattr(artists, "resolve_artist_bio", slow)
    monkeypatch.setattr(admin, "_resolve_discogs_token", no_token)
    set_templates(Jinja2Templates(directory=str(_WEB_DIR / "templates")))
    artists._bio_jobs.clear()
    return ctl


async def test_lookup_finishes_and_saves_after_the_request_is_cancelled(
    slow_lookup: dict[str, Any],
) -> None:
    saved: list[Any] = []
    request_task = asyncio.create_task(
        artists.artist_info(_request(saved), "Some%20Artist", refresh=True)
    )
    await slow_lookup["started"].wait()
    job = artists._bio_jobs["Some Artist"]

    request_task.cancel()  # the user navigated away
    with contextlib.suppress(asyncio.CancelledError):
        await request_task
    assert not job.done()  # the lookup is still running

    slow_lookup["release"].set()
    bio = await job
    await asyncio.sleep(0)  # let the done-callback run
    assert bio.extract == "A bio."
    assert [row.extract for row in saved] == ["A bio."]
    assert "Some Artist" not in artists._bio_jobs


async def test_second_request_joins_the_running_lookup(slow_lookup: dict[str, Any]) -> None:
    saved: list[Any] = []
    first = asyncio.create_task(artists.artist_info(_request(saved), "Band", refresh=True))
    await slow_lookup["started"].wait()
    # e.g. the user comes back to the artist (plain load) while it still runs
    second = asyncio.create_task(artists.artist_info(_request(saved), "Band", refresh=False))
    await asyncio.sleep(0)
    slow_lookup["release"].set()
    responses = await asyncio.gather(first, second)
    assert slow_lookup["calls"] == 1
    assert all(b"A bio." in r.body for r in responses)
