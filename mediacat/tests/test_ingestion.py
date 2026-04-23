"""Tests for :mod:`mediacat.ingestion`."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mediacat.ingestion.base import (
    CircuitBreaker,
    ConnectorStatus,
    FetchResult,
    TokenBucketRateLimiter,
)
from mediacat.ingestion.discogs import (
    DiscogsConnector,
    _discogs_format_to_media,
    _extract_manufacturer,
    _first_identifier,
    _join_artists,
)
from mediacat.ingestion.drift import (
    detect_drift,
    extract_schema,
    save_snapshot,
)
from mediacat.ingestion.musicbrainz import (
    _extract_year,
    _join_artist_credit,
    _mb_format_to_media,
)
from mediacat.ingestion.queue import Job

# ===========================================================================
# Base — rate limiter
# ===========================================================================


@pytest.mark.asyncio
async def test_rate_limiter_allows_burst() -> None:
    rl = TokenBucketRateLimiter(rate=10, period=1.0)
    for _ in range(10):
        await rl.acquire()  # should not block significantly


# ===========================================================================
# Base — circuit breaker
# ===========================================================================


def test_circuit_breaker_stays_closed_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=1.0)
    cb.record_success()
    assert not cb.is_open


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_seconds=60.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open


def test_circuit_breaker_resets_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_seconds=60.0)
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert not cb.is_open  # only 1 consecutive failure


# ===========================================================================
# Discogs helpers
# ===========================================================================


def test_join_artists_single() -> None:
    artists = [{"name": "Pink Floyd", "join": ""}]
    assert _join_artists(artists) == "Pink Floyd"


def test_join_artists_multiple() -> None:
    artists = [
        {"name": "Simon", "join": " & "},
        {"name": "Garfunkel", "join": ""},
    ]
    assert _join_artists(artists) == "Simon & Garfunkel"


def test_first_identifier_found() -> None:
    raw = {"identifiers": [{"type": "Barcode", "value": "123"}, {"type": "Matrix", "value": "M1"}]}
    assert _first_identifier(raw, "Barcode") == "123"


def test_first_identifier_missing() -> None:
    assert _first_identifier({"identifiers": []}, "Barcode") is None


def test_discogs_format_vinyl() -> None:
    assert _discogs_format_to_media([{"name": "LP"}]) == "vinyl"
    assert _discogs_format_to_media([{"name": '12"'}]) == "vinyl"


def test_discogs_format_cd() -> None:
    assert _discogs_format_to_media([{"name": "CD"}]) == "cd"


def test_extract_manufacturer_found() -> None:
    companies = [{"entity_type_name": "Pressed By", "name": "Sony DADC"}]
    assert _extract_manufacturer(companies) == "Sony DADC"


def test_extract_manufacturer_missing() -> None:
    assert _extract_manufacturer([]) is None


# ===========================================================================
# MusicBrainz helpers
# ===========================================================================


def test_mb_join_artist_credit() -> None:
    credit_list = [
        {"name": "John", "joinphrase": " & "},
        {"name": "Paul", "joinphrase": ""},
    ]
    assert _join_artist_credit(credit_list) == "John & Paul"


def test_mb_format_cd() -> None:
    assert _mb_format_to_media([{"format": "CD"}]) == "cd"


def test_mb_format_vinyl() -> None:
    assert _mb_format_to_media([{"format": '12" Vinyl'}]) == "vinyl"


def test_extract_year_full_date() -> None:
    assert _extract_year("1983-06-15") == 1983


def test_extract_year_year_only() -> None:
    assert _extract_year("2021") == 2021


def test_extract_year_none() -> None:
    assert _extract_year(None) is None


def test_extract_year_invalid() -> None:
    assert _extract_year("unknown") is None


# ===========================================================================
# Drift detection
# ===========================================================================


def test_extract_schema_flat() -> None:
    payload = {"title": "Test", "year": 2021, "labels": [{"name": "EMI"}]}
    schema = extract_schema(payload)
    assert schema["title"] == "str"
    assert schema["year"] == "int"
    assert schema["labels"] == "list"
    assert schema["labels.0"] == "dict"
    assert schema["labels.0.name"] == "str"


def test_detect_drift_no_snapshot_creates_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snap_path = Path(tmp) / "test.json"
        payload = {"title": "Test", "id": 1}
        report = detect_drift("test_connector", payload, snap_path)
        assert not report.has_drift
        assert snap_path.exists()


def test_detect_drift_detects_added_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snap_path = Path(tmp) / "test.json"
        # Create initial snapshot
        save_snapshot({"title": "str"}, snap_path)
        # New payload has an extra key
        payload = {"title": "Test", "year": 2021}
        report = detect_drift("test_connector", payload, snap_path)
        assert report.has_drift
        assert "year" in report.added_keys


def test_detect_drift_detects_removed_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snap_path = Path(tmp) / "test.json"
        save_snapshot({"title": "str", "year": "int"}, snap_path)
        payload = {"title": "Test"}
        report = detect_drift("test_connector", payload, snap_path)
        assert report.has_drift
        assert "year" in report.removed_keys


def test_detect_drift_detects_type_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snap_path = Path(tmp) / "test.json"
        save_snapshot({"year": "int"}, snap_path)
        payload = {"year": "2021"}  # was int, now str
        report = detect_drift("test_connector", payload, snap_path)
        assert report.has_drift
        assert any("year" in tc for tc in report.type_changes)


# ===========================================================================
# Job queue
# ===========================================================================


def test_job_serialisation_roundtrip() -> None:
    job = Job(connector="discogs", action="fetch_release", payload={"id": "123"})
    raw = job.to_json()
    restored = Job.from_json(raw)
    assert restored.connector == "discogs"
    assert restored.action == "fetch_release"
    assert restored.payload == {"id": "123"}
    assert restored.job_id == job.job_id


def test_job_defaults() -> None:
    job = Job(connector="test", action="test")
    assert len(job.job_id) == 32
    assert job.attempt == 0
    assert job.max_attempts == 5


# ===========================================================================
# Connector instantiation (no network)
# ===========================================================================


def test_discogs_connector_instantiation() -> None:
    c = DiscogsConnector(
        name="discogs",
        base_url="https://api.discogs.com",
        auth_header="Discogs token=fake",
        rate_limit=55,
    )
    assert c.name == "discogs"
    assert c.status == ConnectorStatus.HEALTHY


def test_fetch_result_defaults() -> None:
    r = FetchResult(
        source="test",
        external_id="1",
        raw_payload={},
        normalised={},
    )
    assert r.confidence == 1.0
    assert r.image_urls == []
