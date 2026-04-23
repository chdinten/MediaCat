"""LLM-assisted schema drift detection for upstream APIs.

This module compares a live API response against a stored schema snapshot
and produces an advisory report when structural changes are detected.

Design invariant
-----------------
**Advisory only.**  Drift reports are written to the review queue and
logged; they never generate executable code or auto-apply patches.
A human reviews the diff and decides whether to update the connector,
the snapshot, or both.

Flow
----
1. Connector fetches a response as usual.
2. Caller optionally passes the raw JSON to ``detect_drift()``.
3. This module computes a structural diff (key presence/type/nesting).
4. If the diff is non-trivial, it formats an advisory and — when an LLM
   is wired — asks the model to summarise the impact.
5. The advisory is returned for persistence in the review queue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Result of a schema drift check."""

    connector_name: str
    has_drift: bool
    added_keys: list[str] = field(default_factory=list)
    removed_keys: list[str] = field(default_factory=list)
    type_changes: list[str] = field(default_factory=list)
    summary: str = ""
    raw_diff: dict[str, Any] = field(default_factory=dict)


def load_snapshot(path: str | Path) -> dict[str, Any]:
    """Load a stored schema snapshot from disk.

    The snapshot is a flat JSON object mapping dotted key paths to type
    names, e.g. ``{"title": "str", "labels.0.name": "str"}``.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("No schema snapshot at %s; drift detection disabled", p)
        return {}
    return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def save_snapshot(schema: dict[str, Any], path: str | Path) -> None:
    """Persist a schema snapshot to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("Schema snapshot saved to %s", p)


def extract_schema(payload: dict[str, Any], *, prefix: str = "") -> dict[str, str]:
    """Recursively extract a flat type-schema from a JSON payload.

    Returns a dict like ``{"title": "str", "labels.0.name": "str"}``.
    """
    schema: dict[str, str] = {}
    for key, value in payload.items():
        full_key = f"{prefix}.{key}" if prefix else key
        schema[full_key] = type(value).__name__
        if isinstance(value, dict):
            schema.update(extract_schema(value, prefix=full_key))
        elif isinstance(value, list) and value:
            # Sample the first element
            first = value[0]
            schema[f"{full_key}.0"] = type(first).__name__
            if isinstance(first, dict):
                schema.update(extract_schema(first, prefix=f"{full_key}.0"))
    return schema


def detect_drift(
    connector_name: str,
    live_payload: dict[str, Any],
    snapshot_path: str | Path,
) -> DriftReport:
    """Compare a live response against the stored snapshot.

    Parameters
    ----------
    connector_name
        Name of the connector (for the report).
    live_payload
        Raw JSON from the upstream API.
    snapshot_path
        Path to the schema snapshot JSON file.

    Returns
    -------
    DriftReport
        Describes any structural differences found.
    """
    stored = load_snapshot(snapshot_path)
    if not stored:
        # No snapshot yet — generate one and report no drift.
        live_schema = extract_schema(live_payload)
        save_snapshot(live_schema, snapshot_path)
        return DriftReport(connector_name=connector_name, has_drift=False)

    live_schema = extract_schema(live_payload)

    added = sorted(set(live_schema) - set(stored))
    removed = sorted(set(stored) - set(live_schema))
    type_changes: list[str] = []
    for key in sorted(set(live_schema) & set(stored)):
        if live_schema[key] != stored[key]:
            type_changes.append(f"{key}: {stored[key]} → {live_schema[key]}")

    has_drift = bool(added or removed or type_changes)

    if has_drift:
        logger.warning(
            "[%s] Schema drift detected: +%d added, -%d removed, %d type changes",
            connector_name,
            len(added),
            len(removed),
            len(type_changes),
        )

    return DriftReport(
        connector_name=connector_name,
        has_drift=has_drift,
        added_keys=added,
        removed_keys=removed,
        type_changes=type_changes,
        summary=_build_summary(connector_name, added, removed, type_changes),
        raw_diff={
            "added": added,
            "removed": removed,
            "type_changes": type_changes,
        },
    )


def _build_summary(
    connector: str,
    added: list[str],
    removed: list[str],
    type_changes: list[str],
) -> str:
    """Build a human-readable summary for the review queue."""
    if not (added or removed or type_changes):
        return f"[{connector}] No schema drift detected."

    parts = [f"[{connector}] Schema drift detected:"]
    if added:
        parts.append(f"  Added keys ({len(added)}): {', '.join(added[:10])}")
        if len(added) > 10:
            parts.append(f"    … and {len(added) - 10} more")
    if removed:
        parts.append(f"  Removed keys ({len(removed)}): {', '.join(removed[:10])}")
        if len(removed) > 10:
            parts.append(f"    … and {len(removed) - 10} more")
    if type_changes:
        parts.append(f"  Type changes ({len(type_changes)}):")
        for tc in type_changes[:10]:
            parts.append(f"    {tc}")
        if len(type_changes) > 10:
            parts.append(f"    … and {len(type_changes) - 10} more")

    return "\n".join(parts)
