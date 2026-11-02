"""Pure parser for Discogs sales-history copy-and-paste text.

No I/O, no database, no HTTP, no framework dependency.

Usage
-----
Accepts the raw text a user copies from the Discogs sales-history page
(https://www.discogs.com/sell/history/{release_id}) and pasted into a
textarea.  Returns a :class:`ParseResult` with structured rows.

Two-step call pattern::

    result = parse_sales_text(raw_text, expected_release_id=12345678)
    if result.has_hard_error:
        # show error message, block confirm button
        ...
    result = apply_outlier_detection(result)
    # show preview of result.valid_rows and result.discarded_rows

Browser copy format
-------------------
Chrome and Firefox copy HTML table content as tab-separated values when
the user selects table cells and copies.  Columns vary; the parser uses
per-cell classification rather than fixed column positions.

Recognised columns (any order):
  Date         — sold date
  Condition    — media grade (Mint, VG+, …)
  Sleeve       — sleeve grade or "None"
  Price        — price with currency symbol or code prefix/suffix
  Ships From   — country name (ignored)
  Seller       — username (ignored)

Monetary values
---------------
Prices are returned as :class:`decimal.Decimal` and serialised to JSON as
numeric strings (e.g. "24.0000") to avoid floating-point representation loss.

Outlier detection
-----------------
:func:`apply_outlier_detection` uses the IQR × 3 fence and requires ≥ 5
non-outlier rows.  Outlier rows are kept in ``valid_rows`` with
``is_outlier=True`` and excluded from aggregate statistics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

PARSER_VERSION: Final[str] = "1.0"

MAX_ROWS: Final[int] = 500
MAX_TEXT_LENGTH: Final[int] = 200_000
MIN_SALE_DATE: Final[date] = date(1990, 1, 1)
MAX_PRICE: Final[Decimal] = Decimal("99999")

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_ALLOWED_CURRENCIES: frozenset[str] = frozenset(
    {
        "EUR",
        "USD",
        "GBP",
        "JPY",
        "AUD",
        "CAD",
        "CHF",
        "SEK",
        "NOK",
        "DKK",
        "NZD",
        "HUF",
        "CZK",
        "PLN",
        "BRL",
    }
)

# Longest symbols must appear before "$" so "AU$" beats "$".
_CURRENCY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("AU$", "AUD"),
    ("NZ$", "NZD"),
    ("CA$", "CAD"),
    ("C$", "CAD"),
    ("A$", "AUD"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("$", "USD"),
)

# Maps lower-cased Discogs grade strings to canonical short forms.
# None means "no sleeve / not applicable" — still a valid row.
_CONDITION_MAP: dict[str, str | None] = {
    "mint (m)": "M",
    "near mint (nm or m-)": "NM",
    "near mint (nm)": "NM",
    "near mint": "NM",
    "very good plus (vg+)": "VG+",
    "very good plus": "VG+",
    "very good (vg)": "VG",
    "very good": "VG",
    "good plus (g+)": "G+",
    "good plus": "G+",
    "good (g)": "G",
    "good": "G",
    "fair (f)": "F",
    "fair": "F",
    "poor (p)": "P",
    "poor": "P",
    "mint": "M",
    "m": "M",
    "nm or m-": "NM",
    "nm": "NM",
    "m-": "NM",
    "vg+": "VG+",
    "vg": "VG",
    "g+": "G+",
    "g": "G",
    "f": "F",
    "p": "P",
    # Sleeve-absent markers — recognised as "no sleeve" (valid)
    "none": None,
    "n/a": None,
    "-": None,
    "": None,
}

# Ordered from most-specific to least-specific to avoid partial matches.
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",  # 2026-05-14
    "%b %d, %Y",  # May 14, 2026
    "%B %d, %Y",  # May 14, 2026 (full month name)
    "%d %b %Y",  # 14 May 2026
    "%d %B %Y",  # 14 May 2026 (full month name)
    "%d/%m/%Y",  # 14/05/2026  (European slash — mm/dd ambiguity documented)
    "%Y/%m/%d",  # 2026/05/14
)

# Words that, when 2+ appear in a line, mark it as a header row.
_HEADER_KEYWORDS: frozenset[str] = frozenset(
    {
        "date",
        "condition",
        "media condition",
        "sleeve",
        "price",
        "seller",
        "ships from",
        "ship from",
        "country",
        "sold",
        "format",
    }
)

# Regex to detect a Discogs release ID in the pasted text.
_RELEASE_ID_RE: re.Pattern[str] = re.compile(
    r"discogs\.com/(?:release|sell/history)/(\d+)", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

# Sentinel returned by _parse_condition when the cell text is not a grade.
_UNRECOGNISED = object()


@dataclass
class SalesRow:
    """A single validated sold-transaction row."""

    sold_date: date
    sold_price: Decimal
    currency: str
    media_condition: str
    sleeve_condition: str | None  # None = no sleeve listed
    is_outlier: bool = False
    raw_row: str = ""


@dataclass
class DiscardedRow:
    """A row that could not be fully parsed, with the reason."""

    row_index: int
    raw_row: str
    reason: str


@dataclass
class ParseResult:
    """Output of :func:`parse_sales_text`."""

    valid_rows: list[SalesRow] = field(default_factory=list)
    discarded_rows: list[DiscardedRow] = field(default_factory=list)

    # Hard-reject flags — if any of these are set the import must be blocked.
    empty_input: bool = False
    input_too_large: bool = False
    too_many_rows: bool = False
    # Set when >1 distinct currency code is found across valid rows.
    mixed_currencies: list[str] | None = None
    # Set when a release ID is found in the pasted text and does not match
    # expected_release_id.  Value is the found (mismatched) ID.
    release_id_mismatch: int | None = None

    # Set by apply_outlier_detection(); 0 until then.
    outlier_count: int = 0

    @property
    def has_hard_error(self) -> bool:
        """True if the import must be blocked before the user can confirm."""
        return (
            self.empty_input
            or self.input_too_large
            or self.too_many_rows
            or self.mixed_currencies is not None
            or self.release_id_mismatch is not None
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_sales_text(  # noqa: PLR0912 - one branch per input format / validation rule
    raw_text: str,
    expected_release_id: int | None = None,
) -> ParseResult:
    """Parse raw clipboard text from the Discogs sales-history page.

    Parameters
    ----------
    raw_text:
        Text pasted by the user.  Typically tab-separated values copied from
        a browser table.
    expected_release_id:
        ``Token.discogs_release_id``.  If a release ID is found in the text
        and does not match, ``ParseResult.release_id_mismatch`` is set and
        the import must be blocked.

    Returns
    -------
    :class:`ParseResult` with ``valid_rows`` and ``discarded_rows``.
    Outlier detection has **not** been applied yet; call
    :func:`apply_outlier_detection` before computing statistics.
    """
    result = ParseResult()

    if not raw_text or not raw_text.strip():
        result.empty_input = True
        return result

    if len(raw_text) > MAX_TEXT_LENGTH:
        result.input_too_large = True
        return result

    # Release ID mismatch check — scan the full text before parsing rows.
    if expected_release_id is not None:
        for m in _RELEASE_ID_RE.finditer(raw_text):
            found_id = int(m.group(1))
            if found_id != expected_release_id:
                result.release_id_mismatch = found_id
                return result
            break  # only check first occurrence

    row_index = 0
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_header_line(stripped):
            continue

        cells = _split_cells(stripped)
        if len(cells) < 2:
            continue

        row_index += 1
        parsed = _parse_row(cells, stripped, row_index)
        if isinstance(parsed, SalesRow):
            result.valid_rows.append(parsed)
        else:
            result.discarded_rows.append(parsed)

    if len(result.valid_rows) > MAX_ROWS:
        result.too_many_rows = True
        result.valid_rows = []
        return result

    # Mixed-currency check.
    currencies_found = {r.currency for r in result.valid_rows}
    if len(currencies_found) > 1:
        result.mixed_currencies = sorted(currencies_found)

    return result


def apply_outlier_detection(result: ParseResult) -> ParseResult:
    """Flag outlier rows in-place using the IQR × 3 fence.

    Requires ≥ 5 valid rows; skipped silently for smaller sets.
    Outliers are marked ``is_outlier=True`` in ``valid_rows`` but are
    not removed.  Updates ``result.outlier_count``.  Returns the same
    object for chaining.
    """
    non_outlier = result.valid_rows
    if len(non_outlier) < 5:
        result.outlier_count = 0
        return result

    prices = sorted(r.sold_price for r in non_outlier)
    n = len(prices)
    q1 = prices[n // 4]
    q3 = prices[(3 * n) // 4]
    iqr = q3 - q1
    lower = q1 - 3 * iqr
    upper = q3 + 3 * iqr

    count = 0
    for row in result.valid_rows:
        if row.sold_price < lower or row.sold_price > upper:
            row.is_outlier = True
            count += 1

    result.outlier_count = count
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_header_line(line: str) -> bool:
    """Return True if ≥ 2 header keywords appear in the cells of this line."""
    cells = _split_cells(line.lower())
    matches = sum(1 for c in cells if c.strip() in _HEADER_KEYWORDS)
    return matches >= 2


def _split_cells(line: str) -> list[str]:
    """Split a line by tab; fall back to runs of 2+ spaces."""
    if "\t" in line:
        return line.split("\t")
    parts = re.split(r"  +", line)
    return parts if len(parts) > 1 else [line]


def _parse_row(cells: list[str], raw: str, idx: int) -> SalesRow | DiscardedRow:
    """Try to extract a :class:`SalesRow` from the list of cells.

    Each cell is tried in order against date, price, then condition
    classifiers.  The first successful match for each field wins;
    subsequent cells of the same type (e.g. a second date) are ignored.
    """
    sold_date: date | None = None
    sold_price: Decimal | None = None
    currency: str | None = None
    media_condition: str | None = None
    sleeve_condition: str | None = None
    sleeve_found = False  # distinguishes "not yet seen" from "None/absent"

    for cell in cells:
        cell_stripped = cell.strip()
        if not cell_stripped:
            continue

        if sold_date is None:
            d = _parse_date(cell_stripped)
            if d is not None:
                sold_date = d
                continue

        if sold_price is None:
            pc = _parse_price_and_currency(cell_stripped)
            if pc is not None:
                sold_price, currency = pc
                continue

        # Condition classifier: media first, then sleeve.
        cond_result = _parse_condition(cell_stripped)
        if cond_result is not _UNRECOGNISED:
            canonical: str | None = cond_result  # type: ignore[assignment]
            if media_condition is None:
                media_condition = canonical
            elif not sleeve_found:
                sleeve_condition = canonical
                sleeve_found = True

    if sold_date is None:
        return DiscardedRow(idx, raw, "Missing or unparseable date")
    if sold_price is None or currency is None:
        return DiscardedRow(idx, raw, "Missing or unparseable price")
    if media_condition is None:
        return DiscardedRow(idx, raw, "Missing or unrecognised media condition")

    return SalesRow(
        sold_date=sold_date,
        sold_price=sold_price,
        currency=currency,
        media_condition=media_condition,
        sleeve_condition=sleeve_condition if sleeve_found else None,
        raw_row=raw,
    )


def _parse_date(text: str) -> date | None:
    """Return a :class:`date` if *text* matches a known format, else None.

    Rejects dates before 1990 and dates in the future.
    Note: slash format assumes dd/mm/yyyy (European).  The US mm/dd/yyyy
    format is not supported in this version.
    """
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
        if parsed < MIN_SALE_DATE:
            return None
        if parsed > datetime.now(UTC).date():
            return None
        return parsed
    return None


def _parse_price_and_currency(text: str) -> tuple[Decimal, str] | None:  # noqa: PLR0912 - one branch per currency notation
    """Extract ``(price, iso_currency_code)`` from a cell such as '€24.50'.

    Returns None if no recognised currency is present or the numeric part
    cannot be parsed as a positive Decimal ≤ 99,999.

    Decimal separator normalisation
    --------------------------------
    * Both separators present (``1,234.56`` or ``1.234,56``): the
      character that appears last is treated as the decimal separator.
    * Comma only (``24,50``): if exactly 2 digits follow the last comma,
      treat it as a decimal comma; otherwise as a thousands separator.
    * Period only or no separator: standard.
    """
    text = text.strip()
    currency: str | None = None
    numeric_part = text

    # 1. Try symbol prefix/suffix (longest first to avoid "$" eating "AU$").
    for symbol, code in _CURRENCY_SYMBOLS:
        sym_upper = symbol.upper()
        text_upper = text.upper()
        if text_upper.startswith(sym_upper):
            currency = code
            numeric_part = text[len(symbol) :].strip()
            break
        if text_upper.endswith(sym_upper):
            currency = code
            numeric_part = text[: -len(symbol)].strip()
            break

    # 2. Try 3-char ISO code prefix/suffix.
    if currency is None:
        text_upper = text.upper()
        for code in _ALLOWED_CURRENCIES:
            if text_upper.startswith(code):
                currency = code
                numeric_part = text[len(code) :].strip()
                break
            if text_upper.endswith(code):
                currency = code
                numeric_part = text[: -len(code)].strip()
                break

    if currency is None:
        return None

    # Strip non-breaking spaces and regular spaces used as thousands separators.
    numeric_part = numeric_part.replace("\xa0", "").replace(" ", "")
    if not numeric_part:
        return None

    # Normalise decimal separator.
    if "," in numeric_part and "." in numeric_part:
        last_dot = numeric_part.rfind(".")
        last_comma = numeric_part.rfind(",")
        if last_dot > last_comma:
            # Standard: 1,234.56
            numeric_part = numeric_part.replace(",", "")
        else:
            # European: 1.234,56
            numeric_part = numeric_part.replace(".", "").replace(",", ".")
    elif "," in numeric_part:
        comma_pos = numeric_part.rfind(",")
        after_comma = numeric_part[comma_pos + 1 :]
        if len(after_comma) == 2 and after_comma.isdigit():
            # 24,50 → decimal comma
            numeric_part = numeric_part.replace(",", ".")
        else:
            # 1,234 → thousands separator
            numeric_part = numeric_part.replace(",", "")

    try:
        price = Decimal(numeric_part).quantize(Decimal("0.0001"))
    except InvalidOperation:
        return None

    if price <= 0 or price > MAX_PRICE:
        return None

    return price, currency


def _parse_condition(text: str) -> str | None | object:
    """Return canonical condition string, None for 'no sleeve', or _UNRECOGNISED.

    Caller must check ``result is not _UNRECOGNISED`` before using the value.
    """
    key = text.strip().lower()
    if key in _CONDITION_MAP:
        return _CONDITION_MAP[key]
    return _UNRECOGNISED
