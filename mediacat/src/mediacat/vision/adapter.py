"""Provider-agnostic vision-language model interface.

Every backend implements :class:`VisionBackend` with a ``transcribe``
method that takes image bytes + a prompt and returns structured JSON.
The :class:`HybridVision` adapter handles local-first, API-fallback.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VisionResponse:
    """Structured response from a vision model call."""

    text: str
    """Raw model output (expected to be JSON)."""

    parsed: dict[str, Any]
    """Parsed JSON, or empty dict if parsing failed."""

    provider: str
    model: str
    latency_ms: float = 0.0
    confidence: float = 0.0


class VisionBackend(Protocol):
    """Interface for a vision-language model backend."""

    @property
    def provider_name(self) -> str: ...

    async def transcribe(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
    ) -> VisionResponse: ...


# ---------------------------------------------------------------------------
# Ollama VLM backend
# ---------------------------------------------------------------------------


class OllamaVisionBackend:
    """Local VLM via Ollama (e.g. LLaVA, Qwen2-VL).

    Uses the ``/api/chat`` endpoint with image content.
    """

    def __init__(
        self,
        base_url: str = "http://ollama:11434",
        default_model: str = "llava",
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "ollama_vision"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    async def transcribe(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
    ) -> VisionResponse:
        model_name = model or self._default_model
        b64 = base64.b64encode(image_bytes).decode("ascii")

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt, "images": [b64]})

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.0},
        }

        client = await self._get_client()
        t0 = time.monotonic()
        resp = await client.post(f"{self._base_url}/api/chat", json=payload)
        resp.raise_for_status()
        elapsed_ms = (time.monotonic() - t0) * 1000

        data = resp.json()
        text = data.get("message", {}).get("content", "")
        parsed = _try_parse_json(text)

        return VisionResponse(
            text=text,
            parsed=parsed,
            provider="ollama_vision",
            model=model_name,
            latency_ms=elapsed_ms,
            confidence=parsed.get("confidence", 0.0) if parsed else 0.0,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Anthropic Vision backend
# ---------------------------------------------------------------------------


class AnthropicVisionBackend:
    """Anthropic Messages API with vision (image content blocks)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        api_key_file: str | None = None,
        default_model: str = "claude-sonnet-4-20250514",
        base_url: str = "https://api.anthropic.com",
        timeout: float = 60.0,
    ) -> None:
        from mediacat.llm.api import _resolve_key

        self._api_key = _resolve_key(api_key or None, api_key_file)
        self._default_model = default_model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "anthropic_vision"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        return self._client

    async def transcribe(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
    ) -> VisionResponse:
        model_name = model or self._default_model
        b64 = base64.b64encode(image_bytes).decode("ascii")

        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": 2048,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime_type, "data": b64},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        if system:
            payload["system"] = system

        client = await self._get_client()
        t0 = time.monotonic()
        resp = await client.post(f"{self._base_url}/v1/messages", json=payload)
        resp.raise_for_status()
        elapsed_ms = (time.monotonic() - t0) * 1000

        data = resp.json()
        content = data.get("content", [])
        text = " ".join(b["text"] for b in content if b.get("type") == "text")
        parsed = _try_parse_json(text)

        return VisionResponse(
            text=text,
            parsed=parsed,
            provider="anthropic_vision",
            model=data.get("model", model_name),
            latency_ms=elapsed_ms,
            confidence=parsed.get("confidence", 0.0) if parsed else 0.0,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Hybrid adapter
# ---------------------------------------------------------------------------


class HybridVision:
    """Tries primary VLM, falls back to API on error."""

    def __init__(
        self,
        primary: VisionBackend,
        fallback: VisionBackend | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def transcribe(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        task: str = "general",
    ) -> VisionResponse:
        t0 = time.monotonic()
        try:
            resp = await self._primary.transcribe(
                image_bytes, mime_type, prompt, system=system, model=model
            )
            logger.info(
                "Vision call: provider=%s task=%s latency=%.0fms",
                resp.provider,
                task,
                time.monotonic() - t0,
            )
            return resp
        except Exception as exc:
            logger.warning("Primary VLM (%s) failed: %s", self._primary.provider_name, exc)
            if self._fallback is None:
                raise
            try:
                t1 = time.monotonic()
                resp = await self._fallback.transcribe(
                    image_bytes, mime_type, prompt, system=system, model=model
                )
                logger.info(
                    "Vision fallback: provider=%s task=%s latency=%.0fms",
                    resp.provider,
                    task,
                    (time.monotonic() - t1) * 1000,
                )
                return resp
            except Exception as fb_exc:
                raise fb_exc from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _try_parse_json(text: str) -> dict[str, Any]:
    """Attempt to parse JSON from model output, stripping fences."""
    import json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}
