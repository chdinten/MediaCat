"""Unit tests for :mod:`mediacat.ingestion.valuation_confidence`.

All tests are pure (no I/O, no DB, no HTTP).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from mediacat.ingestion.discogs_sales_parser import (
    SalesRow,
    apply_outlier_detection,
    parse_sales_text,
)
from mediacat.ingestion.valuation_confidence import (
    HIGH,
    LOW,
    MEDIUM,
    calculate_confidence,
    compute_statistics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date(2026, 6, 10)


def _row(
    sold_date: date,
    price: str,
    currency: str = "EUR",
    condition: str = "VG+",
    is_outlier: bool = False,
) -> SalesRow:
    return SalesRow(
        sold_date=sold_date,
        sold_price=Decimal(price),
        currency=currency,
        media_condition=condition,
        sleeve_condition=None,
        is_outlier=is_outlier,
    )


def _rows_near(
    count: int,
    price: str = "20.00",
    days_ago: int = 30,
    condition: str = "VG+",
) -> list[SalesRow]:
    """Return *count* non-outlier rows all with the same price and recent dates."""
    return [
        _row(TODAY - timedelta(days=days_ago + i), price, condition=condition) for i in range(count)
    ]


# ===========================================================================
# source_confidence — always HIGH
# ===========================================================================


def test_source_confidence_always_high_with_no_rows() -> None:
    result = calculate_confidence([], today=TODAY)
    assert result.source_confidence == HIGH


def test_source_confidence_always_high_with_rows() -> None:
    rows = _rows_near(15)
    result = calculate_confidence(rows, today=TODAY)
    assert result.source_confidence == HIGH


# ===========================================================================
# valuation_confidence — row-count factor
# ===========================================================================


def test_zero_rows_gives_low() -> None:
    result = calculate_confidence([], today=TODAY)
    assert result.valuation_confidence == LOW


def test_one_row_gives_low() -> None:
    result = calculate_confidence(_rows_near(1), today=TODAY)
    assert result.valuation_confidence == LOW


def test_two_rows_gives_low() -> None:
    result = calculate_confidence(_rows_near(2), today=TODAY)
    assert result.valuation_confidence == LOW


def test_three_rows_recent_gives_medium() -> None:
    rows = _rows_near(3, days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == MEDIUM


def test_nine_rows_recent_gives_medium() -> None:
    rows = _rows_near(9, days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == MEDIUM


def test_ten_rows_recent_gives_high() -> None:
    rows = _rows_near(10, days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == HIGH


def test_twenty_rows_recent_gives_high() -> None:
    rows = _rows_near(20, days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == HIGH


# ===========================================================================
# valuation_confidence — recency factor
# ===========================================================================


def test_newest_date_over_two_years_gives_low() -> None:
    # 10 rows but newest is 731 days ago → recency LOW overrides row-count HIGH
    rows = _rows_near(10, days_ago=731)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == LOW


def test_newest_date_exactly_two_years_gives_low() -> None:
    rows = _rows_near(10, days_ago=730)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == LOW


def test_newest_date_within_two_years_not_within_six_months_gives_medium() -> None:
    # 10 rows, newest is 365 days ago (between 180 and 730)
    rows = _rows_near(10, days_ago=365)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == MEDIUM


def test_newest_date_within_six_months_and_enough_rows_gives_high() -> None:
    rows = _rows_near(10, days_ago=90)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == HIGH


def test_newest_date_exactly_six_months_gives_high() -> None:
    rows = _rows_near(10, days_ago=180)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == HIGH


def test_newest_date_just_past_six_months_gives_medium() -> None:
    rows = _rows_near(10, days_ago=181)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == MEDIUM


# ===========================================================================
# valuation_confidence — minimum of the two factors
# ===========================================================================


def test_high_rows_old_data_gives_low() -> None:
    # 15 rows (count=HIGH) but 800 days old (recency=LOW) → LOW
    rows = _rows_near(15, days_ago=800)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == LOW


def test_recent_data_few_rows_gives_low() -> None:
    # 2 rows (count=LOW) but very recent → LOW (count wins)
    rows = _rows_near(2, days_ago=5)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == LOW


def test_medium_count_medium_recency_gives_medium() -> None:
    rows = _rows_near(5, days_ago=365)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == MEDIUM


# ===========================================================================
# Outlier rows excluded from confidence calculation
# ===========================================================================


def test_outlier_rows_excluded_from_count() -> None:
    # 10 rows total: 9 non-outlier (recent), 1 outlier
    rows = _rows_near(9, days_ago=10)
    rows.append(_row(TODAY - timedelta(days=10), "500.00", is_outlier=True))
    # 9 non-outlier rows → MEDIUM (< 10)
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == MEDIUM


def test_all_rows_outliers_gives_low() -> None:
    rows = [_row(TODAY - timedelta(days=10), "500.00", is_outlier=True)] * 5
    result = calculate_confidence(rows, today=TODAY)
    assert result.valuation_confidence == LOW


def test_outlier_exclusion_noted() -> None:
    rows = _rows_near(9, days_ago=10)
    rows.append(_row(TODAY - timedelta(days=10), "500.00", is_outlier=True))
    result = calculate_confidence(rows, today=TODAY)
    assert any("outlier" in note.lower() for note in result.notes)


# ===========================================================================
# Condition spread note
# ===========================================================================


def test_condition_spread_note_present_when_mixed() -> None:
    rows = _rows_near(5, condition="VG+", days_ago=10) + _rows_near(5, condition="NM", days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert any("condition spread" in note.lower() for note in result.notes)


def test_no_condition_spread_note_when_uniform() -> None:
    rows = _rows_near(10, condition="VG+", days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert not any("condition spread" in note.lower() for note in result.notes)


# ===========================================================================
# Notes structure
# ===========================================================================


def test_notes_always_populated() -> None:
    result = calculate_confidence([], today=TODAY)
    assert len(result.notes) >= 1


def test_notes_include_source_confidence() -> None:
    result = calculate_confidence([], today=TODAY)
    assert any("source_confidence" in note.lower() for note in result.notes)


def test_notes_include_valuation_confidence() -> None:
    rows = _rows_near(5, days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert any("valuation confidence" in note.lower() for note in result.notes)


def test_notes_include_row_count() -> None:
    rows = _rows_near(5, days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert any("row count" in note.lower() for note in result.notes)


def test_notes_include_recency() -> None:
    rows = _rows_near(5, days_ago=10)
    result = calculate_confidence(rows, today=TODAY)
    assert any("newest sale" in note.lower() for note in result.notes)


# ===========================================================================
# compute_statistics
# ===========================================================================


def test_statistics_empty_rows() -> None:
    stats = compute_statistics([])
    assert stats["low"] is None
    assert stats["median"] is None
    assert stats["high"] is None
    assert stats["row_count"] == 0


def test_statistics_single_row() -> None:
    rows = [_row(TODAY, "25.00")]
    stats = compute_statistics(rows)
    assert stats["low"] == Decimal("25.00")
    assert stats["median"] == Decimal("25.00")
    assert stats["high"] == Decimal("25.00")
    assert stats["row_count"] == 1


def test_statistics_odd_count_uses_middle_value() -> None:
    # prices: 10, 20, 30 → median = 20
    rows = [
        _row(TODAY, "30.00"),
        _row(TODAY, "10.00"),
        _row(TODAY, "20.00"),
    ]
    stats = compute_statistics(rows)
    assert stats["low"] == Decimal("10.00")
    assert stats["median"] == Decimal("20.00")
    assert stats["high"] == Decimal("30.00")
    assert stats["row_count"] == 3


def test_statistics_even_count_uses_lower_middle() -> None:
    # prices: 10, 20, 30, 40 → lower-middle = 20
    rows = [
        _row(TODAY, "10.00"),
        _row(TODAY, "20.00"),
        _row(TODAY, "30.00"),
        _row(TODAY, "40.00"),
    ]
    stats = compute_statistics(rows)
    assert stats["median"] == Decimal("20.00")
    assert stats["low"] == Decimal("10.00")
    assert stats["high"] == Decimal("40.00")
    assert stats["row_count"] == 4


def test_statistics_excludes_outlier_rows() -> None:
    rows = [
        _row(TODAY, "20.00"),
        _row(TODAY, "22.00"),
        _row(TODAY, "21.00"),
        _row(TODAY, "500.00", is_outlier=True),  # must be excluded
    ]
    stats = compute_statistics(rows)
    assert stats["high"] == Decimal("22.00")
    assert stats["row_count"] == 3


def test_statistics_all_outliers_returns_none() -> None:
    rows = [_row(TODAY, "500.00", is_outlier=True)] * 3
    stats = compute_statistics(rows)
    assert stats["low"] is None
    assert stats["row_count"] == 0


def test_statistics_integration_with_parser() -> None:
    """End-to-end: parse → outlier detection → statistics."""
    text = (
        "May 14, 2026\tVG+\tVG+\tGermany\t€20.00\n"
        "May 13, 2026\tVG+\tVG+\tGermany\t€22.00\n"
        "May 12, 2026\tVG+\tVG+\tGermany\t€21.00\n"
        "May 11, 2026\tVG+\tVG+\tGermany\t€18.00\n"
        "May 10, 2026\tVG+\tVG+\tGermany\t€19.00\n"
        "May 09, 2026\tVG+\tVG+\tGermany\t€23.00\n"
        "May 08, 2026\tVG+\tVG+\tGermany\t€20.00\n"
        "May 07, 2026\tVG+\tVG+\tGermany\t€500.00\n"  # outlier
    )
    result = parse_sales_text(text)
    result = apply_outlier_detection(result)
    stats = compute_statistics(result.valid_rows)
    # Outlier (€500) should be excluded from high
    assert stats["high"] == Decimal("23.0000")
    assert stats["low"] == Decimal("18.0000")
    assert stats["row_count"] == 7
