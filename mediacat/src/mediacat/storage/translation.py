"""Language detection and translation to British English.

This module sits between the OCR pipeline and the token-revision store.
It receives raw OCR text, detects the source language, and translates to
``en-GB`` when the source is not English.

The actual LLM call is delegated to :mod:`mediacat.llm` (Section 7).
This module defines the interface and a simple pass-through for English
text so that upstream callers do not need to know about the LLM layer.

Security
--------
* Input text is length-limited before being sent to any model.
* Untrusted text is wrapped in XML-style delimiter tags via :func:`sanitise`
  (DEF-001) before insertion into the prompt, preventing injection of
  instructions from OCR'd content.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from mediacat.llm.safety import sanitise

logger = logging.getLogger(__name__)

# Maximum characters sent to the LLM for translation.
MAX_TRANSLATION_CHARS = 10_000

# Quick heuristic: if ≥80% of characters are Basic Latin + common
# punctuation we guess English and skip translation.
_LATIN_RE = re.compile(r"[\x20-\x7E]")

# BCP-47 code for British English
TARGET_LANGUAGE = "en-GB"


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Output of a translation request."""

    source_language: str | None
    """Detected or declared source language (BCP-47)."""

    source_text: str
    """Original text."""

    translated_text: str
    """Text in ``en-GB``.  Equals *source_text* when no translation needed."""

    was_translated: bool
    """``True`` if translation was actually performed."""


class TranslationBackend(Protocol):
    """Interface for a translation backend (LLM or dedicated API)."""

    async def translate(
        self,
        text: str,
        source_language: str | None = None,
    ) -> TranslationResult: ...


# ---------------------------------------------------------------------------
# Heuristic language detection
# ---------------------------------------------------------------------------


def detect_is_english(text: str, *, threshold: float = 0.80) -> bool:
    """Cheap heuristic: is the text predominantly Basic Latin?

    This is *not* a real language detector — it just avoids sending
    obvious English text to the LLM for translation.  False negatives
    (German text with mostly ASCII) are fine: the LLM will return the
    text unchanged or with minimal changes.
    """
    if not text:
        return True
    latin_count = len(_LATIN_RE.findall(text))
    return (latin_count / len(text)) >= threshold


# ---------------------------------------------------------------------------
# Pass-through backend (used when LLM is not yet wired)
# ---------------------------------------------------------------------------


class PassthroughTranslator:
    """Returns text unchanged.  Placeholder until the LLM adapter lands."""

    async def translate(
        self,
        text: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        is_eng = detect_is_english(text)
        return TranslationResult(
            source_language=source_language or ("en" if is_eng else None),
            source_text=text,
            translated_text=text,
            was_translated=False,
        )


# ---------------------------------------------------------------------------
# LLM-backed translator (delegates to mediacat.llm in Section 7)
# ---------------------------------------------------------------------------


class LlmTranslator:
    """Translates text via the LLM adapter.

    Parameters
    ----------
    llm_call
        An async callable with signature
        ``(system: str, user: str) -> str`` that invokes the LLM.
        Provided by :mod:`mediacat.llm` at wiring time.
    """

    def __init__(self, llm_call: LlmCallable) -> None:
        self._llm_call = llm_call

    async def translate(
        self,
        text: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        if not text.strip():
            return TranslationResult(
                source_language=source_language,
                source_text=text,
                translated_text=text,
                was_translated=False,
            )

        # Skip if clearly English
        if detect_is_english(text) and (source_language or "").startswith("en"):
            return TranslationResult(
                source_language=source_language or "en",
                source_text=text,
                translated_text=text,
                was_translated=False,
            )

        # Truncate to limit, then sanitise against prompt injection (DEF-001)
        truncated = text[:MAX_TRANSLATION_CHARS]
        sanitised = sanitise(truncated, max_chars=MAX_TRANSLATION_CHARS, tag="source_text")

        system_prompt = (
            "You are a precise translator. Translate the user-provided text "
            "into British English (en-GB). Preserve the original meaning, "
            "formatting, and any proper nouns. Output ONLY the translated "
            "text with no preamble, explanation, or commentary."
        )
        lang_hint = f" The source language is {source_language}." if source_language else ""
        user_prompt = f"Translate the following text to British English.{lang_hint}\n\n{sanitised.text}"

        try:
            translated = await self._llm_call(system_prompt, user_prompt)
            return TranslationResult(
                source_language=source_language,
                source_text=text,
                translated_text=translated.strip(),
                was_translated=True,
            )
        except Exception:
            logger.exception("Translation failed; returning original text")
            return TranslationResult(
                source_language=source_language,
                source_text=text,
                translated_text=text,
                was_translated=False,
            )


# Type alias for the LLM callable injected into LlmTranslator.
from collections.abc import Callable, Coroutine  # noqa: E402
from typing import Any  # noqa: E402

LlmCallable = Callable[[str, str], Coroutine[Any, Any, str]]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_translator(
    backend: str = "passthrough",
    *,
    llm_call: LlmCallable | None = None,
) -> TranslationBackend:
    """Instantiate the configured translation backend.

    Parameters
    ----------
    backend
        ``"passthrough"`` (default, no-op) or ``"llm"``.
    llm_call
        Required when *backend* is ``"llm"``.
    """
    if backend == "llm":
        if llm_call is None:
            msg = "llm_call is required when backend='llm'"
            raise ValueError(msg)
        return LlmTranslator(llm_call)
    return PassthroughTranslator()
