"""Logging utilities shared across all MediaCat processes.

Provides three pieces that the ``logging.yaml`` dictConfig references:

* :class:`RequestIdFilter`      — attaches a per-request correlation id
* :class:`SecretRedactFilter`   — scrubs obvious credentials from log records
* :class:`JsonFormatter`        — stdlib-only JSON line formatter

Design notes
------------
* Zero non-stdlib dependencies so this module can be imported during very
  early bootstrap (before the full dependency tree is installed).
* The redactor is *defensive, not forensic*. It catches common patterns
  (``Authorization: Bearer ...``, ``password=...``, URLs with userinfo,
  long hex/base64 blobs) so an accidental ``logger.info(request.headers)``
  does not leak. Real secret hygiene is owned by the caller.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Request-id context
# ---------------------------------------------------------------------------

#: Context variable bound by the web/worker middleware to correlate logs.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mediacat_request_id", default="-"
)


def new_request_id() -> str:
    """Generate a fresh request id (UUID4 hex, 32 chars)."""
    return uuid.uuid4().hex


class RequestIdFilter(logging.Filter):
    """Attach the current :data:`request_id_var` value to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_REDACTED = "***REDACTED***"

# Compiled once at import time for speed; patterns are intentionally broad.
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer xyz  /  Authorization: Basic xyz
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic|token)\s+\S+"), r"\1\2 " + _REDACTED),
    # key=value pairs where the key looks sensitive
    (
        re.compile(
            r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?key|token|"
            r"client[_-]?secret|private[_-]?key)"
            r"(\s*[:=]\s*)([^\s,;'\"]+)"
        ),
        r"\1\2" + _REDACTED,
    ),
    # URLs with userinfo: scheme://user:pass@host
    (re.compile(r"([a-z][a-z0-9+.\-]*://)([^:/\s@]+):([^@/\s]+)@"), r"\1\2:" + _REDACTED + "@"),
    # Long contiguous hex / base64 blobs (cards, JWTs, etc.)
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), _REDACTED),
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\b"),
        _REDACTED,
    ),
)


def redact(text: str) -> str:
    """Apply the redaction patterns to ``text``."""
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class SecretRedactFilter(logging.Filter):
    """Scrub obvious secrets from ``record.msg`` and formatted ``args``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        except Exception:  # pragma: no cover - defensive: never break logging
            return True
        return True


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

# LogRecord attributes we never want to auto-copy into "extras".
_STD_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "asctime",
        "taskName",
        "request_id",
    }
)


class JsonFormatter(logging.Formatter):
    """Minimal JSON-line formatter with configurable key mapping.

    Parameters
    ----------
    fmt_keys
        Mapping of *output* JSON key -> LogRecord attribute name. If omitted,
        a sensible default is used. Unknown attributes are skipped silently.
    """

    def __init__(
        self,
        fmt_keys: dict[str, str] | None = None,
        datefmt: str | None = None,
    ) -> None:
        super().__init__(datefmt=datefmt)
        self.fmt_keys = fmt_keys or {
            "timestamp": "asctime",
            "level": "levelname",
            "logger": "name",
            "message": "message",
            "request_id": "request_id",
        }

    def formatTime(  # noqa: N802 - stdlib override
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,  # noqa: ARG002
    ) -> str:
        dt = datetime.fromtimestamp(record.created, tz=UTC)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict[str, Any] = {}
        for out_key, attr in self.fmt_keys.items():
            value = self.formatTime(record) if attr == "asctime" else getattr(record, attr, None)
            if value is not None:
                payload[out_key] = value
        # Attach any user-supplied `extra={...}` fields.
        for key, value in record.__dict__.items():
            if key in _STD_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False, default=str)
