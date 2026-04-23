"""Helpers for working with structured matrix/runout parts arrays.

A *parts array* is the authoritative representation of a runout inscription
once graphical symbols have been resolved.  Each element is one of:

  {"t": "text", "v": "<plain text fragment>"}
  {"t": "sym",  "slug": "<symbol-slug>", "id": "<symbol-uuid>"}

These arrays live in ``Token.matrix_runout_parts`` and
``Token.matrix_runout_b_parts``.  The helpers here translate between the
structured form and the plain-text fallback stored in ``matrix_runout``.
"""

from __future__ import annotations


def render_parts_to_text(parts: list[dict], *, symbols: dict[str, str]) -> str:
    """Convert a parts array to a plain-text string.

    ``symbols`` maps slug → display text (e.g. ``{"emi-triangle": "△"}``).
    Unknown slugs fall back to ``[<slug>]`` so nothing is silently dropped.

    Parameters
    ----------
    parts:
        Parsed parts array from ``matrix_runout_parts``.
    symbols:
        Lookup from symbol slug to its Unicode approximation or display label.
    """
    fragments: list[str] = []
    for part in parts:
        kind = part.get("t")
        if kind == "text":
            fragments.append(part.get("v", ""))
        elif kind == "sym":
            slug = part.get("slug", "")
            fragments.append(symbols.get(slug, f"[{slug}]"))
    return "".join(fragments)


def extract_symbol_ids(parts: list[dict]) -> list[tuple[str, int]]:
    """Return (symbol_id, position) pairs for every symbol in a parts array.

    Parameters
    ----------
    parts:
        Parsed parts array from ``matrix_runout_parts``.

    Returns
    -------
    list of (uuid_str, 0-based-position) for entries where ``t == "sym"``.
    """
    result: list[tuple[str, int]] = []
    for idx, part in enumerate(parts):
        if part.get("t") == "sym" and "id" in part:
            result.append((part["id"], idx))
    return result
