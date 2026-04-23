"""Tests for :mod:`mediacat.logging_filters`."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from mediacat.logging_filters import (
    JsonFormatter,
    RequestIdFilter,
    SecretRedactFilter,
    new_request_id,
    redact,
    request_id_var,
)


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("password=hunter2 and other stuff", "hunter2"),
        ("api_key=sk-ABCDEFGHIJKLMNOP1234", "sk-ABCDEFGHIJKLMNOP1234"),
        ("connecting to https://user:s3cr3t@db.example/foo", "s3cr3t"),
        ("token deadbeefdeadbeefdeadbeefdeadbeef", "deadbeefdeadbeefdeadbeefdeadbeef"),
        (
            "jwt eyJhbGciOi.eyJzdWIiOiIxMjM0NTY3ODkwI.SflKxwRJSMeKKF2QT4fwpMeJf",
            "eyJhbGciOi.eyJzdWIiOiIxMjM0NTY3ODkwI.SflKxwRJSMeKKF2QT4fwpMeJf",
        ),
    ],
)
def test_redact_removes_common_secrets(raw: str, must_not_contain: str) -> None:
    scrubbed = redact(raw)
    assert must_not_contain not in scrubbed
    assert "REDACTED" in scrubbed


def test_redact_preserves_non_secret_text() -> None:
    text = "hello world, nothing to see here"
    assert redact(text) == text


def test_request_id_filter_attaches_context_value() -> None:
    token = request_id_var.set("req-123")
    try:
        record = logging.LogRecord(
            name="x",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hi",
            args=(),
            exc_info=None,
        )
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "req-123"
    finally:
        request_id_var.reset(token)


def test_secret_redact_filter_scrubs_message() -> None:
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="password=hunter2",
        args=(),
        exc_info=None,
    )
    SecretRedactFilter().filter(record)
    assert "hunter2" not in record.getMessage()


def test_json_formatter_roundtrip(capfd: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("mediacat.test.logfmt")
    logger.handlers.clear()
    logger.propagate = False
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    token = request_id_var.set("abc")
    try:
        logger.info("hello %s", "world", extra={"custom_field": 42})
    finally:
        request_id_var.reset(token)

    line = stream.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "abc"
    assert payload["custom_field"] == 42
    assert payload["timestamp"].endswith("Z")


def test_new_request_id_is_unique_and_hex() -> None:
    a = new_request_id()
    b = new_request_id()
    assert a != b
    assert len(a) == 32
    int(a, 16)  # does not raise
