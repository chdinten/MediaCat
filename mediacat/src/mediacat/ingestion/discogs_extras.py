"""Pure helpers for Discogs identifiers and collection custom fields.

Two kinds of Discogs data are handled here:

* **Release identifiers** ("Barcode and Other Identifiers").  The API returns
  these as a list of ``{"type", "value", "description"}`` dicts.  Matrix /
  runout entries carry their side and variant in the free-text description,
  e.g. ``"Runout side C, variant 2"``.  :func:`group_runouts` groups them by
  variant, then by LP (sides A/B → LP 1, C/D → LP 2, …).  The runout text
  itself is never interpreted — it is shown verbatim for the user to copy.

* **Collection custom fields** (Discogs → Settings → Collection).  These are
  user-defined per-copy fields such as "Runout / Matrix LP1 - A",
  "Autographed" or "Signed by".  Field names are free text, so
  :func:`classify_field` maps them onto well-known keys by pattern.

Storage
-------
Both are kept in ``Token.extra`` (JSONB) so no schema migration is needed::

    extra["discogs"] = {"identifiers": [...], "format_qty": 2, "fetched_at": "…"}
    extra["discogs_collection"] = {
        "username": "…", "instance_id": 123, "folder_id": 1,
        "fields": [{"id", "name", "type", "options", "position"}],
        "values": {"4": "…"},          # current value in MediaCat
        "discogs_values": {"4": "…"},  # last value known to be on Discogs
        "fetched_at": "…",
    }

A field whose ``values`` entry differs from ``discogs_values`` has been edited
in MediaCat and not yet written back.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

MATRIX_TYPE = "matrix / runout"

RUNOUT_KEYS = ("runout_lp1_a", "runout_lp1_b", "runout_lp2_a", "runout_lp2_b")

_SIDE_RE = re.compile(r"\bside\s+([A-Z]{1,2}|\d{1,2})\b", re.IGNORECASE)
_VARIANT_RE = re.compile(r"\bvariant\s*#?\s*(\d+)\b", re.IGNORECASE)
_NOTE_STRIP_RE = re.compile(r"\b(runout|matrix)\b", re.IGNORECASE)
_LP_SIDE_RE = re.compile(
    r"\blp\s*(\d+)\s*[-–—_/:,.]*\s*(?:side\s*)?([ab])\b",
    re.IGNORECASE,
)
_YES = {"yes", "y", "ja", "j", "oui", "si", "sí", "true", "1", "x", "✓", "✔"}


# ---------------------------------------------------------------------------
# Sides and LPs
# ---------------------------------------------------------------------------


def side_to_lp(side: str) -> tuple[int, str] | None:
    """Map a Discogs side label to ``(lp_number, "A"|"B")``.

    ``A``/``B`` → LP 1, ``C``/``D`` → LP 2, …; numeric sides ``1``/``2`` → LP 1,
    ``3``/``4`` → LP 2.  Returns ``None`` for labels that cannot be mapped.
    """
    s = (side or "").strip().upper()
    if len(s) == 1 and "A" <= s <= "Z":
        idx = ord(s) - ord("A")
    elif s.isdigit() and int(s) > 0:
        idx = int(s) - 1
    else:
        return None
    return idx // 2 + 1, "AB"[idx % 2]


def lp_letters(lp: int) -> tuple[str, str]:
    """Return the Discogs side letters for an LP number (1 → A/B, 2 → C/D)."""
    first = chr(ord("A") + (lp - 1) * 2)
    return first, chr(ord(first) + 1)


def track_lp(track: dict[str, Any]) -> int:
    """Return the LP number a tracklist entry belongs to.

    Uses the position prefix when present ("C3" → LP 2, "2-4" → LP 2),
    otherwise the stored ``disc`` number.
    """
    pos = str(track.get("position") or "").strip()
    m = re.match(r"^([A-Za-z])\d*\b", pos)
    if m:
        mapped = side_to_lp(m.group(1))
        if mapped:
            return mapped[0]
    m = re.match(r"^(\d+)[-.]\d+", pos)
    if m:
        return max(int(m.group(1)), 1)
    try:
        return max(int(track.get("disc") or 1), 1)
    except (TypeError, ValueError):
        return 1


def format_qty(formats: list[Any] | None) -> int | None:
    """Total number of vinyl discs in a Discogs ``formats`` list."""
    total = 0
    for fmt in formats or []:
        if not isinstance(fmt, dict):
            continue
        if "vinyl" not in (fmt.get("name") or "").lower():
            continue
        try:
            total += int(fmt.get("qty") or 1)
        except (TypeError, ValueError):
            total += 1
    return total or None


# ---------------------------------------------------------------------------
# Release identifiers → runout variants
# ---------------------------------------------------------------------------


def clean_identifiers(identifiers: list[Any] | None) -> list[dict[str, str]]:
    """Keep only the type/value/description keys of each identifier."""
    out: list[dict[str, str]] = []
    for ident in identifiers or []:
        if not isinstance(ident, dict):
            continue
        value = str(ident.get("value") or "").strip()
        if not value:
            continue
        out.append(
            {
                "type": str(ident.get("type") or "").strip(),
                "value": value,
                "description": str(ident.get("description") or "").strip(),
            }
        )
    return out


def _parse_runout_description(desc: str) -> tuple[str | None, str | None, str]:
    """Return ``(side, variant, note)`` parsed from a runout description."""
    side_m = _SIDE_RE.search(desc)
    var_m = _VARIANT_RE.search(desc)
    note = desc
    for m in (side_m, var_m):
        if m:
            note = note.replace(m.group(0), " ")
    note = _NOTE_STRIP_RE.sub(" ", note)
    note = re.sub(r"[\s,;:()\[\]]+", " ", note).strip(" -")
    return (
        side_m.group(1).upper() if side_m else None,
        var_m.group(1) if var_m else None,
        note,
    )


def _variant_label(variant: str | None, group: dict[str, Any]) -> str:
    """ "Variant N"; entries without a number are "Unnumbered", or "Label"
    when every one of them is described as a label matrix ("Side A label")."""
    if variant is not None:
        return f"Variant {variant}"
    entries = [s for sides in group["lps"].values() for s in sides] + group["unsided"]
    if entries and all("label" in e["note"].lower() for e in entries):
        return "Label"
    return "Unnumbered"


def group_runouts(identifiers: list[Any] | None) -> dict[str, Any]:
    """Group Matrix / Runout identifiers by variant, then LP and side.

    Returns::

        {
          "variants": [
            {"variant": "1", "label": "Variant 1",
             "lps": [{"lp": 1, "sides": [{"side": "A", "lp_side": "A",
                                           "value": "…", "note": ""}]}],
             "unsided": [{"value": "…", "note": "…"}]},
            …
          ],
          "other": [{"type": "Barcode", "value": "…", "description": "…"}],
          "runout_count": 8,
        }

    Variants keep their numeric order; entries without a variant number are
    collected in a trailing "Unnumbered" group.  Within a variant, sides are
    sorted (A, B, C, D …) regardless of the order Discogs listed them in.
    """
    variants: dict[str | None, dict[str, Any]] = {}
    other: list[dict[str, str]] = []
    runout_count = 0

    for ident in clean_identifiers(identifiers):
        if ident["type"].lower() != MATRIX_TYPE:
            other.append(ident)
            continue
        runout_count += 1
        side, variant, note = _parse_runout_description(ident["description"])
        group = variants.setdefault(variant, {"lps": {}, "unsided": []})
        mapped = side_to_lp(side) if side else None
        if mapped is None:
            group["unsided"].append({"value": ident["value"], "note": note or ident["description"]})
            continue
        lp, lp_side = mapped
        group["lps"].setdefault(lp, []).append(
            {"side": side, "lp_side": lp_side, "value": ident["value"], "note": note}
        )

    def _vkey(v: str | None) -> tuple[int, int]:
        return (1, 0) if v is None else (0, int(v))

    out: list[dict[str, Any]] = []
    for v in sorted(variants, key=_vkey):
        g = variants[v]
        lps = [
            {
                "lp": lp,
                "sides": sorted(g["lps"][lp], key=lambda s: (len(s["side"]), s["side"])),
            }
            for lp in sorted(g["lps"])
        ]
        out.append(
            {
                "variant": v,
                "label": _variant_label(v, g),
                "lps": lps,
                "unsided": g["unsided"],
            }
        )
    return {"variants": out, "other": other, "runout_count": runout_count}


def variant_runout_fill(variant: dict[str, Any]) -> dict[str, str]:
    """Map one grouped variant onto the well-known runout field keys."""
    fill: dict[str, str] = {}
    for lp_block in variant.get("lps", []):
        if lp_block["lp"] > 2:
            continue
        for s in lp_block["sides"]:
            key = f"runout_lp{lp_block['lp']}_{s['lp_side'].lower()}"
            fill.setdefault(key, s["value"])
    return fill


def _norm_code(text: str) -> str:
    """Whitespace-collapsed, case-folded runout text for exact comparison."""
    return " ".join(text.split()).casefold()


def mark_matching_variants(groups: dict[str, Any], known: dict[str, str]) -> dict[str, Any]:
    """Flag variants whose codes equal the runout fields filled in on *my copy*.

    Exact text only (ignoring repeated spaces and letter case): every runout
    field the user has filled must equal the variant's code for that side.
    Sets ``variant["matches"]`` and ``groups["any_match"]``; returns *groups*.
    """
    mine = {k: _norm_code(known[k]) for k in RUNOUT_KEYS if known.get(k)}
    any_match = False
    for variant in groups.get("variants", []):
        codes = {k: _norm_code(v) for k, v in variant_runout_fill(variant).items()}
        variant["matches"] = bool(mine) and all(codes.get(k) == v for k, v in mine.items())
        any_match = any_match or variant["matches"]
    groups["any_match"] = any_match
    return groups


# ---------------------------------------------------------------------------
# Collection custom fields
# ---------------------------------------------------------------------------


def classify_field(name: str) -> str | None:  # noqa: PLR0911
    """Map a collection field name onto a well-known key.

    Recognised keys: ``runout_lp{1,2}_{a,b}``, ``autographed``, ``signed_by``,
    ``signed_on``, ``price_estimate``, ``media_condition``, ``sleeve_condition``,
    ``notes``.
    """
    n = " ".join((name or "").lower().split())
    if not n:
        return None
    if re.search(r"runout|run-out|matrix|wax", n):
        m = _LP_SIDE_RE.search(n)
        if m:
            lp, side = int(m.group(1)), m.group(2).lower()
        else:
            sm = re.search(r"\bside\s*([ab])\b|[-–—/]\s*([ab])\s*$", n)
            if not sm:
                return None
            lp, side = 1, (sm.group(1) or sm.group(2))
        return f"runout_lp{lp}_{side}" if lp in (1, 2) else None
    # e.g. "Price (Signed)"; what was paid is not an estimate
    if re.search(r"\bprice\b|\bvalue\b|valuation|estimate", n) and not re.search(
        r"purchase|paid|bought|cost", n
    ):
        return "price_estimate"
    if re.search(r"signed\s*by|signature|signer|autographed\s*by", n):
        return "signed_by"
    if re.search(r"signed\s*on", n):
        return "signed_on"
    if "autograph" in n or n in ("signed", "signed?"):
        return "autographed"
    if n == "media condition":
        return "media_condition"
    if n == "sleeve condition":
        return "sleeve_condition"
    if n == "notes":
        return "notes"
    return None


def is_yes(value: Any) -> bool:
    """True for affirmative dropdown values ("Yes", "Ja", "x", …)."""
    v = str(value or "").strip().lower()
    return v in _YES or v.startswith("yes")


def clean_field_defs(fields: list[Any] | None) -> list[dict[str, Any]]:
    """Normalise the ``/users/{u}/collection/fields`` response list."""
    out: list[dict[str, Any]] = []
    for f in fields or []:
        if not isinstance(f, dict) or f.get("id") is None:
            continue
        out.append(
            {
                "id": int(f["id"]),
                "name": str(f.get("name") or f"Field {f['id']}"),
                "type": str(f.get("type") or "textarea"),
                "options": [str(o) for o in (f.get("options") or [])],
                "position": int(f.get("position") or 0),
            }
        )
    out.sort(key=lambda f: (f["position"], f["id"]))
    return out


def notes_to_values(notes: list[Any] | None) -> dict[str, str]:
    """Convert a collection instance ``notes`` list to ``{field_id: value}``."""
    out: dict[str, str] = {}
    for n in notes or []:
        if isinstance(n, dict) and n.get("field_id") is not None:
            out[str(n["field_id"])] = str(n.get("value") or "")
    return out


def merge_collection(
    existing: dict[str, Any] | None,
    *,
    username: str,
    fields: list[dict[str, Any]],
    instance: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge freshly fetched Discogs collection data into stored state.

    Values edited locally but not yet written back are preserved; all other
    values follow Discogs.
    """
    prev = existing or {}
    prev_values: dict[str, str] = dict(prev.get("values") or {})
    prev_remote: dict[str, str] = dict(prev.get("discogs_values") or {})
    remote = notes_to_values((instance or {}).get("notes"))

    values: dict[str, str] = {}
    for f in fields:
        fid = str(f["id"])
        local = prev_values.get(fid)
        if local is not None and local != prev_remote.get(fid, ""):
            values[fid] = local  # edited in MediaCat, not yet written back
        else:
            values[fid] = remote.get(fid, "")

    return {
        "username": username,
        "in_collection": instance is not None,
        "instance_id": (instance or {}).get("instance_id") or prev.get("instance_id"),
        "folder_id": (instance or {}).get("folder_id") or prev.get("folder_id"),
        "fields": fields,
        "values": values,
        "discogs_values": {str(f["id"]): remote.get(str(f["id"]), "") for f in fields},
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def collection_view(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Build a template-friendly view of the stored collection fields.

    Returns ``{"present", "in_collection", "fields": [...], "known": {...},
    "pending": int, "signed": bool, "signed_by": str, "signed_on": str,
    "price_estimate": str,
    "fetched_at"}`` where
    each field is ``{"id", "name", "type", "options", "value", "key",
    "pending"}`` and ``known`` maps well-known keys to values.
    """
    coll = (extra or {}).get("discogs_collection") or {}
    values = coll.get("values") or {}
    remote = coll.get("discogs_values") or {}
    fields: list[dict[str, Any]] = []
    known: dict[str, str] = {}
    pending = 0
    for f in coll.get("fields") or []:
        fid = str(f.get("id"))
        val = values.get(fid, "")
        key = classify_field(f.get("name", ""))
        is_pending = val != remote.get(fid, "")
        pending += int(is_pending)
        fields.append({**f, "value": val, "key": key, "pending": is_pending})
        if key and val and key not in known:
            known[key] = val
    signed_by = known.get("signed_by", "")
    return {
        "present": bool(coll.get("fields")),
        "in_collection": bool(coll.get("in_collection", bool(coll.get("instance_id")))),
        "fields": fields,
        "known": known,
        "pending": pending,
        "signed": is_yes(known.get("autographed")),
        "signed_by": signed_by,
        "signed_on": known.get("signed_on", ""),
        "price_estimate": known.get("price_estimate", ""),
        "fetched_at": coll.get("fetched_at"),
    }


def release_preview(data: dict[str, Any], *, max_images: int = 8) -> dict[str, Any]:
    """Summarise a Discogs release for the "Add pressing" cards view.

    Images (primary first, https only), the first runout variant, barcodes and
    the free-text format details ("Yellow labels", "Gatefold") that tell
    otherwise identical pressings apart.
    """
    images: list[dict[str, Any]] = []
    for img in data.get("images") or []:
        if not isinstance(img, dict):
            continue
        full = str(img.get("uri") or "")
        thumb = str(img.get("uri150") or full)
        if full.startswith("https://") and thumb.startswith("https://"):
            images.append({"thumb": thumb, "full": full, "primary": img.get("type") == "primary"})
    images.sort(key=lambda i: not i["primary"])  # stable: primary first

    groups = group_runouts(data.get("identifiers"))
    variants = groups["variants"]
    formats = [f for f in data.get("formats") or [] if isinstance(f, dict)]
    return {
        "images": images[:max_images],
        "image_count": len(images),
        "variant": variants[0] if variants else None,
        "variant_count": len(variants),
        "barcodes": [o["value"] for o in groups["other"] if o["type"].lower() == "barcode"][:3],
        "format_text": ", ".join(str(f["text"]) for f in formats if f.get("text")),
        "released": str(data.get("released") or ""),
        "track_count": len([t for t in data.get("tracklist") or [] if isinstance(t, dict)]),
    }


def release_extra(data: dict[str, Any]) -> dict[str, Any]:
    """Build the ``extra["discogs"]`` payload from a Discogs release JSON."""
    return {
        "identifiers": clean_identifiers(data.get("identifiers")),
        "format_qty": format_qty(data.get("formats")),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
