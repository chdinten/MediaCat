"""Pure valuation-confidence calculator for Discogs sales-history imports.

No I/O, no database, no HTTP, no framework dependency.

Usage
-----
::

    from mediacat.ingestion.discogs_sales_parser import apply_outlier_detection
    from mediacat.ingestion.valuation_confidence import (
        calculate_confidence,
        compute_statistics,
    )

    result = apply_outlier_detection(parse_result)
    stats   = compute_statistics(result.valid_rows)
    conf    = calculate_confidence(result.valid_rows)

Confidence model
----------------
Two independent factors are computed; the final ``valuation_confidence``
is the minimum of the two factor tiers.

Factor 1 — Row count (non-outlier rows only):
  < 3   → LOW
  3–9   → MEDIUM
  ≥ 10  → HIGH

Factor 2 — Recency (newest_sold_date of non-outlier rows):
  > 2 years ago  → LOW
  6 months–2 yrs → MEDIUM
  ≤ 6 months     → HIGH

``source_confidence`` is always HIGH for user-assisted imports because the
data was directly viewed and submitted by the authenticated user.  It is
not the same as ``valuation_confidence``, which reflects data quality.

Condition spread is recorded as a note but does not reduce confidence by
itself — the user should read the condition breakdown to judge applicability.

Statistics
----------
:func:`compute_statistics` calculates low / median / high from non-outlier
rows only.  All values are :class:`decimal.Decimal`.  Returns None for each
statistic when there are no non-outlier rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mediacat.ingestion.discogs_sales_parser import SalesRow

# ---------------------------------------------------------------------------
# Confidence tier strings
# ---------------------------------------------------------------------------

HIGH: str = "high"
MEDIUM: str = "medium"
LOW: str = "low"

_TIER_RANK: dict[str, int] = {LOW: 0, MEDIUM: 1, HIGH: 2}

# ---------------------------------------------------------------------------
# Thresholds — tunable without schema changes
# ---------------------------------------------------------------------------

_ROW_COUNT_MEDIUM_MIN: int = 3  # < 3  → LOW
_ROW_COUNT_HIGH_MIN: int = 10  # < 10 → MEDIUM,  ≥ 10 → HIGH

_RECENCY_HIGH_DAYS: int = 180  # ≤ 6 months       → HIGH
_RECENCY_LOW_DAYS: int = 730  # > 2 years         → LOW

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ConfidenceResult:
    """Output of :func:`calculate_confidence`."""

    source_confidence: str  # always "high" for user-assisted imports
    valuation_confidence: str  # "high" | "medium" | "low"
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calculate_confidence(
    valid_rows: list[SalesRow],
    today: date | None = None,
) -> ConfidenceResult:
    """Calculate source and valuation confidence from parsed sales rows.

    Parameters
    ----------
    valid_rows:
        All valid rows **including** those flagged as outliers.  Outliers
        are excluded when computing row count and recency; their exclusion
        is recorded in the notes.
    today:
        Override today's date (for deterministic testing).
        Defaults to today (UTC).
    """
    if today is None:
        today = datetime.now(UTC).date()

    source_confidence = HIGH
    notes: list[str] = [f"Source: user-assisted import → source_confidence: {HIGH.upper()}"]

    non_outlier = [r for r in valid_rows if not r.is_outlier]
    outlier_count = len(valid_rows) - len(non_outlier)

    if outlier_count > 0:
        outlier_prices = ", ".join(str(r.sold_price) for r in valid_rows if r.is_outlier)
        notes.append(
            f"Outliers excluded from statistics: {outlier_count} row(s)"
            f" ({outlier_prices}) — IQR × 3 method"
        )

    # ── Factor 1: row count ──────────────────────────────────────────────────
    row_count = len(non_outlier)
    row_tier = _row_count_tier(row_count)
    notes.append(f"Row count (excl. outliers): {row_count} → {row_tier.upper()}")

    # ── Factor 2: recency ────────────────────────────────────────────────────
    newest_date: date | None = max(r.sold_date for r in non_outlier) if non_outlier else None
    recency_tier = _recency_tier(newest_date, today)
    if newest_date is not None:
        age_days = (today - newest_date).days
        notes.append(f"Newest sale: {newest_date} ({age_days} day(s) ago) → {recency_tier.upper()}")
    else:
        notes.append(f"No valid dated rows → recency: {recency_tier.upper()}")

    # ── Factor 3: condition spread (informational) ───────────────────────────
    conditions = sorted({r.media_condition for r in non_outlier})
    if len(conditions) > 1:
        notes.append(f"Condition spread: {', '.join(conditions)} — prices reflect multiple grades")

    # ── Final: minimum of the two factor tiers ───────────────────────────────
    final_tier = _min_tier(row_tier, recency_tier)
    notes.append(
        f"Valuation confidence: {final_tier.upper()}"
        f" (min of row-count tier [{row_tier.upper()}]"
        f" and recency tier [{recency_tier.upper()}])"
    )

    return ConfidenceResult(
        source_confidence=source_confidence,
        valuation_confidence=final_tier,
        notes=notes,
    )


def compute_statistics(
    valid_rows: list[SalesRow],
) -> dict[str, Decimal | int | None]:
    """Return low / median / high sold price, excluding outlier rows.

    All returned values are :class:`decimal.Decimal` or ``None`` when no
    non-outlier rows exist.  The ``median`` key uses the lower-middle value
    for an even number of rows (no interpolation) to keep the result a real
    observed price.

    Keys: ``"low"``, ``"median"``, ``"high"``, ``"row_count"``
    (``row_count`` is an :class:`int`).
    """
    prices = sorted(r.sold_price for r in valid_rows if not r.is_outlier)
    n = len(prices)
    if n == 0:
        return {"low": None, "median": None, "high": None, "row_count": 0}

    mid = n // 2
    # Use lower-middle for even n so the median is an observed price.
    median = prices[mid] if n % 2 == 1 else prices[mid - 1]

    return {
        "low": prices[0],
        "median": median,
        "high": prices[-1],
        "row_count": n,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_count_tier(count: int) -> str:
    if count < _ROW_COUNT_MEDIUM_MIN:
        return LOW
    if count < _ROW_COUNT_HIGH_MIN:
        return MEDIUM
    return HIGH


def _recency_tier(newest: date | None, today: date) -> str:
    if newest is None:
        return LOW
    age_days = (today - newest).days
    if age_days >= _RECENCY_LOW_DAYS:
        return LOW
    if age_days > _RECENCY_HIGH_DAYS:
        return MEDIUM
    return HIGH


def _min_tier(a: str, b: str) -> str:
    return a if _TIER_RANK[a] <= _TIER_RANK[b] else b
