"""Unit tests for the background worker's heartbeat (compose healthcheck)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mediacat import worker


async def test_worker_touches_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beat = tmp_path / "heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT", beat)
    monkeypatch.setattr(worker, "_shutdown", asyncio.Event())
    task = asyncio.create_task(worker.run_worker())
    for _ in range(50):
        if beat.exists():
            break
        await asyncio.sleep(0.01)
    worker._shutdown.set()
    await asyncio.wait_for(task, timeout=6)
    assert beat.exists()


def test_heartbeat_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "HEARTBEAT", Path("/nonexistent-dir/heartbeat"))
    worker._beat()  # logs, doesn't raise
