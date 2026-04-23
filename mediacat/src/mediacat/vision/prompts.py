"""Task-specific prompt templates for vision-model transcription.

Each function builds a structured prompt for a specific image region
(label, OBI, runout) and returns the expected JSON schema so the caller
can validate the response.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# System prompt shared across all vision tasks
# ---------------------------------------------------------------------------

VISION_SYSTEM = """\
You are an expert physical-media cataloguing assistant specialising in
vinyl records and CDs. You are precise, factual, and use British English.
When you cannot read a field with confidence, set its value to null and
set the field's confidence to 0.

Always respond with ONLY valid JSON matching the schema described in the
user prompt. No markdown fences, no commentary."""


# ---------------------------------------------------------------------------
# Label transcription
# ---------------------------------------------------------------------------


def label_prompt(media_format: str = "vinyl") -> tuple[str, dict[str, Any]]:
    """Build the prompt for transcribing a record / CD label.

    Returns (prompt_text, expected_schema).
    """
    prompt = f"""\
This image shows the label of a {media_format} release. Extract all
visible text and identify the following fields:

{{
  "label_name": "Record label name (e.g. 'Columbia', 'Deutsche Grammophon')",
  "catalog_number": "Catalog number printed on the label",
  "artist": "Artist or band name",
  "title": "Release title / album name",
  "side": "Side designation if visible (e.g. 'A', 'B', '1', '2')",
  "speed_rpm": "RPM if indicated (33, 45, 78) or null",
  "country": "Country of manufacture if indicated, or null",
  "year": "Year if printed, or null",
  "other_text": "Any other significant text on the label",
  "confidence": 0.0 to 1.0
}}

Respond with ONLY the JSON object."""

    schema = {
        "type": "object",
        "properties": {
            "label_name": {"type": ["string", "null"]},
            "catalog_number": {"type": ["string", "null"]},
            "artist": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "side": {"type": ["string", "null"]},
            "speed_rpm": {"type": ["integer", "null"]},
            "country": {"type": ["string", "null"]},
            "year": {"type": ["integer", "null"]},
            "other_text": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
        },
    }
    return prompt, schema


# ---------------------------------------------------------------------------
# OBI strip transcription
# ---------------------------------------------------------------------------


def obi_prompt() -> tuple[str, dict[str, Any]]:
    """Build the prompt for transcribing a Japanese OBI strip."""
    prompt = """\
This image shows an OBI strip (Japanese promotional sleeve band) from a
vinyl record or CD. OBI strips typically contain Japanese text. Extract:

{
  "japanese_title": "Title in Japanese characters",
  "romanised_title": "Romanised (romaji) title if visible",
  "english_title": "English title if visible, or null",
  "catalog_number": "Catalog number on the OBI",
  "price": "Price (e.g. '¥2800') if visible, or null",
  "label_name": "Label name if visible",
  "obi_type": "Type: 'original', 'reissue', 'promo', or null if unknown",
  "other_text": "Any other significant text",
  "confidence": 0.0 to 1.0
}

Respond with ONLY the JSON object."""

    schema = {
        "type": "object",
        "properties": {
            "japanese_title": {"type": ["string", "null"]},
            "romanised_title": {"type": ["string", "null"]},
            "english_title": {"type": ["string", "null"]},
            "catalog_number": {"type": ["string", "null"]},
            "price": {"type": ["string", "null"]},
            "label_name": {"type": ["string", "null"]},
            "obi_type": {"type": ["string", "null"]},
            "other_text": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
        },
    }
    return prompt, schema


# ---------------------------------------------------------------------------
# Runout / matrix transcription
# ---------------------------------------------------------------------------


def runout_prompt(media_format: str = "vinyl") -> tuple[str, dict[str, Any]]:
    """Build the prompt for transcribing runout etchings / matrix codes.

    Symbol detections are returned in ``symbol_detections`` — a list of
    graphical marks that are NOT plain alphanumeric text.  Each entry carries
    a ``slug`` suggestion (or null when unknown), a Unicode approximation, and
    a per-symbol confidence.  The caller is responsible for matching slugs
    against the symbol registry.
    """
    region = "dead wax / runout area" if media_format == "vinyl" else "inner ring / data surface"

    prompt = f"""\
This image shows the {region} of a {media_format} release. This area
typically contains etched, stamped, or printed codes. Extract:

{{
  "matrix_number": "Primary matrix / master number",
  "stamper_code": "Stamper identification if visible, or null",
  "sid_codes": [
    {{"type": "mastering or mould", "code": "IFPI code if present"}}
  ],
  "lacquer_cut_info": "Lacquer cutting info (e.g. 'Sterling', 'Masterdisk') or null",
  "pressing_plant_hint": "Any text suggesting the pressing plant, or null",
  "side": "Side designation if visible (e.g. 'A', 'B') or null",
  "other_etchings": "Any other readable text in the runout",
  "symbol_detections": [
    {{
      "slug_suggestion": "e.g. 'emi-triangle', or null if unknown",
      "unicode_approx": "Best Unicode approximation of the symbol, e.g. '△'",
      "description": "Brief description: shape, position, and how it was applied",
      "application": "etched, stamped, or printed",
      "confidence": 0.0 to 1.0
    }}
  ],
  "confidence": 0.0 to 1.0
}}

IMPORTANT — graphical symbols:
Report any non-alphanumeric graphical marks (triangles, stars, circles,
diamonds, decorative stamps) as entries in symbol_detections rather than
embedding them as text in other fields.  Common examples:
  △ (upward triangle) — often an EMI pressing-plant mark on UK vinyl
  ▽ (downward triangle) — PRS certification prefix on UK vinyl
  ☆ ✲ ✹ — Capitol pressing-plant stars on US vinyl
  ◆ ◈ ✤ — MCA/Decca US plant diamonds and stars
  ○ — Decca UK circle
If a symbol appears to be a text character incidentally (e.g. the letter O
used as a zero), record it as text, not a symbol detection.

Note: runout text is often hard to read. If uncertain, include your best
guess and set confidence lower. Distinguish between etched (hand-written),
stamped (machine), and printed text if possible.

Respond with ONLY the JSON object."""

    schema = {
        "type": "object",
        "properties": {
            "matrix_number": {"type": ["string", "null"]},
            "stamper_code": {"type": ["string", "null"]},
            "sid_codes": {"type": "array"},
            "lacquer_cut_info": {"type": ["string", "null"]},
            "pressing_plant_hint": {"type": ["string", "null"]},
            "side": {"type": ["string", "null"]},
            "other_etchings": {"type": ["string", "null"]},
            "symbol_detections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slug_suggestion": {"type": ["string", "null"]},
                        "unicode_approx": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                        "application": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                },
            },
            "confidence": {"type": "number"},
        },
    }
    return prompt, schema


def symbol_identification_prompt(
    known_slugs: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a focused prompt for identifying graphical symbols in a runout image.

    Used in Phase 2 of the symbol pipeline when a runout image is reprocessed
    specifically for symbol identification (e.g. after manual flagging or when
    the main runout pass returned low-confidence symbol detections).

    Parameters
    ----------
    known_slugs:
        Optional list of candidate symbol slugs to guide the model.  When
        provided they are listed as hints; the model may still suggest others.
    """
    hints = ""
    if known_slugs:
        hint_list = ", ".join(f'"{s}"' for s in known_slugs)
        hints = (
            f"\n\nHINTS — the following slugs are candidates based on metadata; "
            f"verify whether any appear in the image: {hint_list}"
        )

    prompt = f"""\
This image shows a vinyl record dead wax / runout area. Your task is to
identify and describe ALL graphical symbols — non-alphanumeric marks that
are stamped, etched, or printed into the wax.{hints}

For each symbol found, return one entry:

{{
  "slug_suggestion": "slug from the known list if matched, else null",
  "unicode_approx": "Best Unicode approximation (e.g. '△', '☆', '◆')",
  "description": "Shape, size relative to surrounding text, application method",
  "application": "etched | stamped | printed",
  "position_in_text": "Description of where in the runout string this symbol sits",
  "confidence": 0.0 to 1.0
}}

Respond with ONLY a JSON object:
{{
  "symbols": [ ...entries... ],
  "overall_confidence": 0.0 to 1.0
}}

If no graphical symbols are visible, return {{"symbols": [], "overall_confidence": 1.0}}."""

    schema = {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slug_suggestion": {"type": ["string", "null"]},
                        "unicode_approx": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                        "application": {"type": ["string", "null"]},
                        "position_in_text": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                },
            },
            "overall_confidence": {"type": "number"},
        },
    }
    return prompt, schema


# ---------------------------------------------------------------------------
# Prompt selector
# ---------------------------------------------------------------------------


def get_prompt_for_region(
    region: str,
    media_format: str = "vinyl",
) -> tuple[str, str, dict[str, Any]]:
    """Select the right prompt template for an image region.

    Returns (system_prompt, user_prompt, expected_schema).
    """
    if region.startswith("label"):
        user, schema = label_prompt(media_format)
    elif region.startswith("obi"):
        user, schema = obi_prompt()
    elif region in ("runout_a", "runout_b", "matrix"):
        user, schema = runout_prompt(media_format)
    else:
        # Generic fallback
        user = (
            "Describe all visible text and markings in this image of a "
            f"{media_format} release. Respond as JSON with keys: "
            '"text", "notable_features", "confidence".'
        )
        schema = {"type": "object"}

    return VISION_SYSTEM, user, schema
