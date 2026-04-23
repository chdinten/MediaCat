"""Prompt injection mitigations for LLM inputs.

All user-supplied text (OCR output, connector data, review comments)
passes through :func:`sanitise` before being included in any LLM prompt.

Mitigations
-----------
1. **Length limiting** — truncate inputs to a configurable maximum.
2. **Delimiter enforcement** — wrap untrusted text in XML-style tags
   so the model can distinguish instruction from data.
3. **Instruction-leak detection** — scan for common injection patterns
   and flag them (without blocking, since legitimate text can contain
   imperative sentences).

This module does **not** modify the text semantically; it only applies
structural safeguards.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Maximum characters for untrusted input (configurable at call site)
DEFAULT_MAX_CHARS = 10_000

# Patterns that suggest prompt injection attempts
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+a", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
    re.compile(r"<\s*/?\s*system\s*>", re.IGNORECASE),
    re.compile(r"IGNORE\s+ABOVE", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"forget\s+everything", re.IGNORECASE),
]


@dataclass(frozen=True, slots=True)
class SanitisedInput:
    """Result of sanitising untrusted text for LLM consumption."""

    text: str
    """Sanitised text, truncated and wrapped."""

    was_truncated: bool
    """True if the input was longer than max_chars."""

    injection_flags: list[str]
    """Patterns that matched (informational, not blocking)."""


def sanitise(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    tag: str = "user_data",
) -> SanitisedInput:
    """Sanitise untrusted text for inclusion in an LLM prompt.

    Parameters
    ----------
    text
        Raw untrusted text.
    max_chars
        Maximum allowed characters (excess is truncated).
    tag
        XML-style tag name to wrap the text in.

    Returns
    -------
    SanitisedInput
        Wrapped and optionally truncated text with injection flags.
    """
    # Truncate
    was_truncated = len(text) > max_chars
    truncated = text[:max_chars] if was_truncated else text
    if was_truncated:
        logger.debug("Input truncated from %d to %d chars", len(text), max_chars)

    # Scan for injection patterns
    flags: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(truncated):
            flags.append(pattern.pattern)
    if flags:
        logger.warning("Potential prompt injection detected: %s", flags)

    # Wrap in delimiter tags
    wrapped = f"<{tag}>\n{truncated}\n</{tag}>"

    return SanitisedInput(
        text=wrapped,
        was_truncated=was_truncated,
        injection_flags=flags,
    )


def build_prompt(
    template: str,
    *,
    data_fields: dict[str, str] | None = None,
    max_chars_per_field: int = DEFAULT_MAX_CHARS,
) -> tuple[str, list[str]]:
    """Build a prompt from a template with sanitised data fields.

    Parameters
    ----------
    template
        Prompt template with ``{field_name}`` placeholders.
    data_fields
        Untrusted data to insert (each field is sanitised).
    max_chars_per_field
        Per-field character limit.

    Returns
    -------
    tuple[str, list[str]]
        The assembled prompt and any injection flags found.
    """
    all_flags: list[str] = []
    replacements: dict[str, str] = {}

    for field_name, raw_value in (data_fields or {}).items():
        sanitised = sanitise(raw_value, max_chars=max_chars_per_field, tag=field_name)
        replacements[field_name] = sanitised.text
        all_flags.extend(sanitised.injection_flags)

    prompt = template.format(**replacements) if replacements else template
    return prompt, all_flags
