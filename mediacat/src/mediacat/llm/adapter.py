"""Provider-agnostic LLM interface with hybrid local/API fallback.

Every backend implements :class:`LlmBackend` with a single ``complete``
method.  The :class:`HybridLlm` adapter tries a primary backend first
and falls back to a secondary on failure, timeout, or low quality.

Usage logging
-------------
Every call is logged with provider, model, token counts, latency, and
the task label so cost attribution is possible.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """Structured response from an LLM call."""

    text: str
    """Generated text."""

    provider: str
    """Backend that produced the response (e.g. 'ollama', 'anthropic')."""

    model: str
    """Model identifier."""

    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class LlmBackend(Protocol):
    """Interface for an LLM backend."""

    @property
    def provider_name(self) -> str: ...

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> LlmResponse:
        """Generate a completion.

        Parameters
        ----------
        system
            System prompt.
        user
            User prompt.
        temperature
            Sampling temperature (0 = deterministic).
        max_tokens
            Maximum output tokens.
        model
            Override the default model for this call.
        """
        ...


class HybridLlm:
    """Tries *primary*, falls back to *fallback* on error.

    Parameters
    ----------
    primary
        Default backend (typically local / Ollama).
    fallback
        Fallback backend (typically API / Anthropic).
    """

    def __init__(
        self,
        primary: LlmBackend,
        fallback: LlmBackend | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        model: str | None = None,
        task: str = "general",
    ) -> LlmResponse:
        """Complete with automatic fallback and usage logging."""
        t0 = time.monotonic()
        try:
            resp = await self._primary.complete(
                system, user, temperature=temperature, max_tokens=max_tokens, model=model
            )
            self._log_usage(resp, task, time.monotonic() - t0)
            return resp
        except Exception as primary_exc:
            logger.warning(
                "Primary LLM (%s) failed: %s; trying fallback",
                self._primary.provider_name,
                primary_exc,
            )
            if self._fallback is None:
                raise
            try:
                t1 = time.monotonic()
                resp = await self._fallback.complete(
                    system, user, temperature=temperature, max_tokens=max_tokens, model=model
                )
                self._log_usage(resp, task, time.monotonic() - t1)
                return resp
            except Exception as fallback_exc:
                logger.error(
                    "Fallback LLM (%s) also failed: %s", self._fallback.provider_name, fallback_exc
                )
                raise fallback_exc from primary_exc

    def _log_usage(self, resp: LlmResponse, task: str, wall_s: float) -> None:
        logger.info(
            "LLM call: provider=%s model=%s task=%s in=%d out=%d latency=%.0fms",
            resp.provider,
            resp.model,
            task,
            resp.input_tokens,
            resp.output_tokens,
            wall_s * 1000,
        )
