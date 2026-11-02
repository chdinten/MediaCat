"""Unit tests for :mod:`mediacat.ingestion.discogs_sales_parser`.

All tests are pure (no I/O, no DB, no HTTP).  They import only the parser
module and the standard library.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from mediacat.ingestion.discogs_sales_parser import (
    MAX_ROWS,
    ParseResult,
    apply_outlier_detection,
    parse_sales_text,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Realistic Chrome TSV copied from Discogs sales-history page.
_CHROME_TSV_EUR = (
    "Date\tCondition\tSleeve\tShips From\tPrice\n"
    "May 14, 2026\tVery Good Plus (VG+)\tVery Good Plus (VG+)\tGermany\t€24.00\n"
    "Apr 02, 2026\tNear Mint (NM or M-)\tVG+\tUK\t€31.00\n"
    "Mar 15, 2026\tVery Good (VG)\tVG\tFrance\t€18.50\n"
    "Feb 01, 2026\tVG+\tNone\tGermany\t€22.00\n"
    "Jan 10, 2026\tNear Mint (NM or M-)\tNM\tNetherlands\t€35.00\n"
)

_CHROME_TSV_USD = (
    "Date\tCondition\tSleeve\tShips From\tPrice\nMay 01, 2026\tVG+\tVG+\tUSA\t$18.00\n"
)

TODAY = date(2026, 6, 10)  # fixed for deterministic tests


def _parse(text: str, release_id: int | None = None) -> ParseResult:
    return parse_sales_text(text, expected_release_id=release_id)


# ===========================================================================
# Hard-reject: input shape
# ===========================================================================


def test_empty_string_sets_empty_input() -> None:
    r = _parse("")
    assert r.empty_input is True
    assert r.valid_rows == []


def test_whitespace_only_sets_empty_input() -> None:
    r = _parse("   \n\t\n  ")
    assert r.empty_input is True


def test_text_exceeding_max_length_sets_flag() -> None:
    r = _parse("x" * 200_001)
    assert r.input_too_large is True
    assert r.valid_rows == []


def test_text_at_exact_max_length_is_accepted() -> None:
    # 200 000 chars — all whitespace except one non-matching line → no valid rows,
    # but no input_too_large flag.
    r = _parse("a" * 200_000)
    assert r.input_too_large is False


def test_too_many_rows_flag() -> None:
    # Build MAX_ROWS + 1 valid lines.
    line = "May 14, 2026\tVG+\tVG+\tGermany\t€20.00"
    text = "\n".join([line] * (MAX_ROWS + 1))
    r = _parse(text)
    assert r.too_many_rows is True
    assert r.valid_rows == []


def test_exactly_max_rows_accepted() -> None:
    line = "May 14, 2026\tVG+\tVG+\tGermany\t€20.00"
    text = "\n".join([line] * MAX_ROWS)
    r = _parse(text)
    assert r.too_many_rows is False
    assert len(r.valid_rows) == MAX_ROWS


# ===========================================================================
# Hard-reject: release ID mismatch
# ===========================================================================


def test_release_id_in_url_matches_expected() -> None:
    text = (
        "https://www.discogs.com/sell/history/12345678\nMay 14, 2026\tVG+\tVG+\tGermany\t€24.00\n"
    )
    r = _parse(text, release_id=12345678)
    assert r.release_id_mismatch is None
    assert len(r.valid_rows) == 1


def test_release_id_mismatch_blocks_parse() -> None:
    text = (
        "https://www.discogs.com/sell/history/99999999\nMay 14, 2026\tVG+\tVG+\tGermany\t€24.00\n"
    )
    r = _parse(text, release_id=12345678)
    assert r.release_id_mismatch == 99999999
    assert r.valid_rows == []
    assert r.has_hard_error is True


def test_no_release_id_check_when_expected_none() -> None:
    text = (
        "https://www.discogs.com/sell/history/99999999\nMay 14, 2026\tVG+\tVG+\tGermany\t€24.00\n"
    )
    r = _parse(text, release_id=None)
    assert r.release_id_mismatch is None


# ===========================================================================
# Hard-reject: mixed currencies
# ===========================================================================


def test_mixed_currencies_sets_flag() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€24.00\nMay 01, 2026\tVG+\tVG\tUSA\t$18.00\n"
    r = _parse(text)
    assert r.mixed_currencies is not None
    assert sorted(r.mixed_currencies) == ["EUR", "USD"]
    assert r.has_hard_error is True


def test_single_currency_no_flag() -> None:
    r = _parse(_CHROME_TSV_EUR)
    assert r.mixed_currencies is None


# ===========================================================================
# Valid row parsing — basic
# ===========================================================================


def test_chrome_tsv_eur_all_rows_valid() -> None:
    r = _parse(_CHROME_TSV_EUR)
    assert not r.has_hard_error
    assert len(r.valid_rows) == 5
    assert len(r.discarded_rows) == 0


def test_header_line_is_skipped() -> None:
    text = "Date\tCondition\tSleeve\tShips From\tPrice\nMay 14, 2026\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 1


def test_empty_lines_ignored() -> None:
    text = "\nMay 14, 2026\tVG+\tVG+\tGermany\t€24.00\n\nMay 01, 2026\tNM\tNM\tUK\t€30.00\n\n"
    r = _parse(text)
    assert len(r.valid_rows) == 2


def test_parsed_row_fields() -> None:
    text = "May 14, 2026\tVery Good Plus (VG+)\tVG\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 1
    row = r.valid_rows[0]
    assert row.sold_date == date(2026, 5, 14)
    assert row.sold_price == Decimal("24.0000")
    assert row.currency == "EUR"
    assert row.media_condition == "VG+"
    assert row.sleeve_condition == "VG"
    assert row.is_outlier is False


def test_sleeve_none_is_valid_and_null() -> None:
    text = "May 14, 2026\tVG+\tNone\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 1
    assert r.valid_rows[0].sleeve_condition is None


def test_sleeve_absent_is_valid_and_null() -> None:
    # Only 4 cells — no sleeve column at all.
    text = "May 14, 2026\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 1
    assert r.valid_rows[0].sleeve_condition is None


def test_condition_canonical_forms() -> None:
    cases = [
        ("Mint (M)", "M"),
        ("Near Mint (NM or M-)", "NM"),
        ("Very Good Plus (VG+)", "VG+"),
        ("Very Good (VG)", "VG"),
        ("Good Plus (G+)", "G+"),
        ("Good (G)", "G"),
        ("Fair (F)", "F"),
        ("Poor (P)", "P"),
        ("NM", "NM"),
        ("vg+", "VG+"),
        ("M-", "NM"),
    ]
    for raw_cond, expected in cases:
        text = f"May 14, 2026\t{raw_cond}\tVG+\tGermany\t€24.00\n"
        r = _parse(text)
        assert len(r.valid_rows) == 1, f"Failed for condition: {raw_cond!r}"
        assert r.valid_rows[0].media_condition == expected, (
            f"Expected {expected!r} for {raw_cond!r}, got {r.valid_rows[0].media_condition!r}"
        )


# ===========================================================================
# Date parsing
# ===========================================================================


def test_iso_date_format() -> None:
    text = "2026-05-14\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_date == date(2026, 5, 14)


def test_us_month_name_format() -> None:
    text = "January 03, 2025\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_date == date(2025, 1, 3)


def test_european_day_month_name_format() -> None:
    text = "14 May 2026\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_date == date(2026, 5, 14)


def test_future_date_row_discarded() -> None:
    text = "Dec 31, 2099\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert len(r.discarded_rows) == 1
    assert "date" in r.discarded_rows[0].reason.lower()


def test_pre_1990_date_row_discarded() -> None:
    text = "Jan 01, 1989\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert len(r.discarded_rows) == 1


def test_exactly_min_date_accepted() -> None:
    text = "Jan 01, 1990\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 1


# ===========================================================================
# Price and currency parsing
# ===========================================================================


def test_euro_symbol_prefix() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_price == Decimal("24.0000")
    assert r.valid_rows[0].currency == "EUR"


def test_dollar_symbol_prefix() -> None:
    text = "May 14, 2026\tVG+\tVG+\tUSA\t$18.50\n"
    r = _parse(text)
    assert r.valid_rows[0].currency == "USD"
    assert r.valid_rows[0].sold_price == Decimal("18.5000")


def test_gbp_symbol_prefix() -> None:
    text = "May 14, 2026\tVG+\tVG+\tUK\t£15.00\n"
    r = _parse(text)
    assert r.valid_rows[0].currency == "GBP"


def test_aud_symbol_prefix() -> None:
    text = "May 14, 2026\tVG+\tVG+\tAustralia\tAU$30.00\n"
    r = _parse(text)
    assert r.valid_rows[0].currency == "AUD"


def test_eur_iso_code_suffix() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t24.00 EUR\n"
    r = _parse(text)
    assert r.valid_rows[0].currency == "EUR"
    assert r.valid_rows[0].sold_price == Decimal("24.0000")


def test_comma_decimal_separator_european() -> None:
    # 24,50 EUR — comma is decimal separator (2 digits after)
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€24,50\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_price == Decimal("24.5000")


def test_european_thousands_and_decimal() -> None:
    # 1.234,56 EUR — period=thousands, comma=decimal
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€1.234,56\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_price == Decimal("1234.5600")


def test_standard_thousands_and_decimal() -> None:
    # 1,234.56 USD — comma=thousands, period=decimal
    text = "May 14, 2026\tVG+\tVG+\tUSA\t$1,234.56\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_price == Decimal("1234.5600")


def test_non_breaking_space_as_thousands_separator() -> None:
    text = "May 14, 2026\tVG+\tVG+\tFrance\t€1\xa0234,00\n"
    r = _parse(text)
    assert r.valid_rows[0].sold_price == Decimal("1234.0000")


def test_zero_price_row_discarded() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€0.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert "price" in r.discarded_rows[0].reason.lower()


def test_price_above_max_row_discarded() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€100000.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0


def test_non_numeric_price_row_discarded() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€N/A\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert "price" in r.discarded_rows[0].reason.lower()


def test_unrecognised_currency_row_discarded() -> None:
    text = "May 14, 2026\tVG+\tVG+\tMars\t♦24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert "price" in r.discarded_rows[0].reason.lower()


# ===========================================================================
# Row-level discard reasons
# ===========================================================================


def test_missing_date_row_discarded() -> None:
    text = "VG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert "date" in r.discarded_rows[0].reason.lower()


def test_missing_condition_row_discarded() -> None:
    text = "May 14, 2026\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert "condition" in r.discarded_rows[0].reason.lower()


def test_unrecognised_condition_row_discarded() -> None:
    # Row with only unrecognised condition-like cells — no valid grade anywhere.
    text = "May 14, 2026\tExcellent\tGermany\t€24.00\n"
    r = _parse(text)
    assert len(r.valid_rows) == 0
    assert "condition" in r.discarded_rows[0].reason.lower()


def test_country_name_ignored_not_treated_as_condition() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€24.00\n"
    r = _parse(text)
    # "Germany" must not end up as sleeve_condition
    assert r.valid_rows[0].sleeve_condition == "VG+"


def test_discarded_row_carries_original_line() -> None:
    line = "VG+\tVG+\tGermany\t€24.00"
    r = _parse(line)
    assert r.discarded_rows[0].raw_row == line


def test_mixed_valid_and_invalid_rows() -> None:
    text = (
        "May 14, 2026\tVG+\tVG+\tGermany\t€24.00\n"  # valid
        "VG+\tVG+\tGermany\t€24.00\n"  # no date
        "May 01, 2026\tNM\tNM\tUK\t€30.00\n"  # valid
    )
    r = _parse(text)
    assert len(r.valid_rows) == 2
    assert len(r.discarded_rows) == 1


# ===========================================================================
# has_hard_error property
# ===========================================================================


def test_clean_parse_has_no_hard_error() -> None:
    r = _parse(_CHROME_TSV_EUR)
    assert r.has_hard_error is False


def test_empty_input_has_hard_error() -> None:
    assert _parse("").has_hard_error is True


def test_mixed_currencies_has_hard_error() -> None:
    text = "May 14, 2026\tVG+\tVG+\tGermany\t€24.00\nMay 01, 2026\tVG+\tVG\tUSA\t$18.00\n"
    assert _parse(text).has_hard_error is True


# ===========================================================================
# Outlier detection
# ===========================================================================


def test_outlier_detection_not_applied_below_five_rows() -> None:
    # 4 rows with one obvious outlier — should NOT be flagged.
    text = (
        "May 14, 2026\tVG+\tVG+\tGermany\t€20.00\n"
        "May 13, 2026\tVG+\tVG+\tGermany\t€21.00\n"
        "May 12, 2026\tVG+\tVG+\tGermany\t€19.00\n"
        "May 11, 2026\tVG+\tVG+\tGermany\t€500.00\n"
    )
    r = _parse(text)
    r = apply_outlier_detection(r)
    assert r.outlier_count == 0
    assert all(not row.is_outlier for row in r.valid_rows)


def test_outlier_detection_flags_extreme_price() -> None:
    # 8 rows near €20 + 1 at €500 — should flag the outlier.
    lines = [f"May {14 - i:02d}, 2026\tVG+\tVG+\tGermany\t€20.00" for i in range(8)]
    lines.append("Jan 01, 2026\tVG+\tVG+\tGermany\t€500.00")
    text = "\n".join(lines)
    r = _parse(text)
    r = apply_outlier_detection(r)
    assert r.outlier_count == 1
    outliers = [row for row in r.valid_rows if row.is_outlier]
    assert len(outliers) == 1
    assert outliers[0].sold_price == Decimal("500.0000")


def test_non_outlier_rows_remain_after_detection() -> None:
    lines = [f"May {14 - i:02d}, 2026\tVG+\tVG+\tGermany\t€20.00" for i in range(8)]
    lines.append("Jan 01, 2026\tVG+\tVG+\tGermany\t€500.00")
    text = "\n".join(lines)
    r = _parse(text)
    r = apply_outlier_detection(r)
    non_outliers = [row for row in r.valid_rows if not row.is_outlier]
    assert len(non_outliers) == 8
    assert all(row.sold_price == Decimal("20.0000") for row in non_outliers)


def test_all_same_prices_no_outliers() -> None:
    lines = [f"May {14 - i:02d}, 2026\tVG+\tVG+\tGermany\t€25.00" for i in range(6)]
    text = "\n".join(lines)
    r = _parse(text)
    r = apply_outlier_detection(r)
    assert r.outlier_count == 0


def test_five_rows_no_extreme_no_outliers() -> None:
    text = (
        "May 14, 2026\tVG+\tVG+\tGermany\t€18.00\n"
        "May 13, 2026\tVG+\tVG+\tGermany\t€20.00\n"
        "May 12, 2026\tVG+\tVG+\tGermany\t€22.00\n"
        "May 11, 2026\tVG+\tVG+\tGermany\t€25.00\n"
        "May 10, 2026\tVG+\tVG+\tGermany\t€24.00\n"
    )
    r = _parse(text)
    r = apply_outlier_detection(r)
    assert r.outlier_count == 0


# ===========================================================================
# Fallback: space-separated (non-tab) copy
# ===========================================================================


def test_two_space_separated_cells() -> None:
    # Some copy formats produce runs of spaces rather than tabs.
    text = "May 14, 2026  VG+  VG+  Germany  €24.00"
    r = _parse(text)
    assert len(r.valid_rows) == 1
    assert r.valid_rows[0].currency == "EUR"
