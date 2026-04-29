"""Task-specific LLM prompt templates and response validators.

Each task (comparison, anomaly detection, translation, text generation)
has a dedicated function that builds the prompt, sanitises inputs,
calls the LLM, and validates the response structure.

All untrusted inputs go through :mod:`mediacat.llm.safety` before
reaching the model.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mediacat.llm.adapter import HybridLlm, LlmResponse
from mediacat.llm.safety import build_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

_COMPARISON_SYSTEM = """\
You are a music cataloguing expert. Compare the two revisions of a
physical media token object below. Identify meaningful differences in
metadata (title, artist, label, country, year, pressing plant,
catalog number, matrix/runout codes). Ignore trivial formatting
differences.

Respond with a JSON object:
{
  "has_differences": true/false,
  "differences": [
    {"field": "...", "revision_a": "...", "revision_b": "...", "significance": "high|medium|low"}
  ],
  "summary": "One-sentence summary"
}
Output ONLY valid JSON, no markdown fences or commentary."""

_COMPARISON_USER = """\
Revision A:
{revision_a}

Revision B:
{revision_b}"""


async def compare_revisions(
    llm: HybridLlm,
    revision_a: dict[str, Any],
    revision_b: dict[str, Any],
) -> dict[str, Any]:
    """Compare two token revisions and return structured differences."""
    user_prompt, flags = build_prompt(
        _COMPARISON_USER,
        data_fields={
            "revision_a": json.dumps(revision_a, indent=2, default=str),
            "revision_b": json.dumps(revision_b, indent=2, default=str),
        },
    )
    if flags:
        logger.warning("Injection flags in comparison input: %s", flags)

    resp = await llm.complete(_COMPARISON_SYSTEM, user_prompt, task="comparison")
    return _parse_json_response(
        resp, fallback={"has_differences": False, "differences": [], "summary": ""}
    )


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------

_ANOMALY_SYSTEM = """\
You are a music cataloguing expert. Analyse the token object data below
for anomalies — inconsistencies, implausible combinations, or data that
does not match known patterns for the media format and era.

Respond with a JSON object:
{
  "has_anomalies": true/false,
  "anomalies": [
    {"field": "...", "issue": "...", "severity": "high|medium|low"}
  ],
  "summary": "One-sentence summary"
}
Output ONLY valid JSON, no markdown fences or commentary."""

_ANOMALY_USER = """\
Token data:
{token_data}"""


async def detect_anomalies(
    llm: HybridLlm,
    token_data: dict[str, Any],
) -> dict[str, Any]:
    """Analyse a token for anomalies."""
    user_prompt, flags = build_prompt(
        _ANOMALY_USER,
        data_fields={"token_data": json.dumps(token_data, indent=2, default=str)},
    )
    if flags:
        logger.warning("Injection flags in anomaly input: %s", flags)

    resp = await llm.complete(_ANOMALY_SYSTEM, user_prompt, task="anomaly")
    return _parse_json_response(
        resp, fallback={"has_anomalies": False, "anomalies": [], "summary": ""}
    )


# ---------------------------------------------------------------------------
# Translation (delegated from storage.translation)
# ---------------------------------------------------------------------------

_TRANSLATE_SYSTEM = """\
You are a precise translator. Translate the user-provided text into
British English (en-GB). Preserve the original meaning, formatting, and
any proper nouns. Output ONLY the translated text with no preamble,
explanation, or commentary."""


async def translate_text(
    llm: HybridLlm,
    text: str,
    source_language: str | None = None,
) -> str:
    """Translate text to British English."""
    lang_hint = f" The source language is {source_language}." if source_language else ""
    user_prompt, flags = build_prompt(
        f"Translate the following text to British English.{lang_hint}\n\n{{source_text}}",
        data_fields={"source_text": text},
    )
    if flags:
        logger.warning("Injection flags in translation input: %s", flags)
    resp = await llm.complete(_TRANSLATE_SYSTEM, user_prompt, task="translation")
    return resp.text.strip()


# ---------------------------------------------------------------------------
# Text generation
# ---------------------------------------------------------------------------

_TEXTGEN_SYSTEM = """\
You are a music cataloguing assistant. Generate the requested text based
on the token object data provided. Be factual and concise. Use British
English spelling."""


async def generate_text(
    llm: HybridLlm,
    instruction: str,
    token_data: dict[str, Any],
) -> str:
    """Generate text (description, summary, etc.) for a token."""
    user_prompt, flags = build_prompt(
        "{instruction}\n\nToken data:\n{token_data}",
        data_fields={
            "instruction": instruction,
            "token_data": json.dumps(token_data, indent=2, default=str),
        },
    )
    if flags:
        logger.warning("Injection flags in textgen input: %s", flags)

    resp = await llm.complete(_TEXTGEN_SYSTEM, user_prompt, temperature=0.3, task="text_generation")
    return resp.text.strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json_response(resp: LlmResponse, fallback: dict[str, Any]) -> dict[str, Any]:
    """Parse a JSON response from the LLM, falling back on parse errors."""
    text = resp.text.strip()
    # Strip markdown fences if the model wrapped anyway
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON response; returning fallback")
        return fallback
