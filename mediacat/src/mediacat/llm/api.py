"""Remote API backend — Anthropic Messages API.

Uses ``httpx`` directly (no SDK dependency) so the adapter stays thin
and the dependency surface is minimal.  OpenAI-compatible endpoints can
be supported by swapping the URL and payload shape.

Credentials
-----------
API keys are read from Docker secrets, never from config files.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from mediacat.llm.adapter import LlmResponse

logger = logging.getLogger(__name__)


class AnthropicBackend:
    """Anthropic Messages API backend.

    Parameters
    ----------
    api_key
        Anthropic API key.  Prefer ``api_key_file`` in production.
    api_key_file
        Path to file containing the API key (Docker secret).
    default_model
        Model to use when none is specified per-call.
    base_url
        API base URL.
    timeout
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_file: str | None = None,
        default_model: str = "claude-sonnet-4-20250514",
        base_url: str = "https://api.anthropic.com",
        timeout: float = 60.0,
    ) -> None:
        self._api_key = _resolve_key(api_key, api_key_file)
        self._default_model = default_model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "anthropic"

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

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> LlmResponse:
        """Generate a completion via the Anthropic Messages API."""
        model_name = model or self._default_model
        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

        client = await self._get_client()
        t0 = time.monotonic()
        resp = await client.post(f"{self._base_url}/v1/messages", json=payload)
        resp.raise_for_status()
        elapsed_ms = (time.monotonic() - t0) * 1000

        data = resp.json()

        # Extract text from content blocks
        content = data.get("content", [])
        text_parts = [block["text"] for block in content if block.get("type") == "text"]
        text = "\n".join(text_parts)

        usage = data.get("usage", {})

        return LlmResponse(
            text=text,
            provider="anthropic",
            model=data.get("model", model_name),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=elapsed_ms,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


class OpenAICompatibleBackend:
    """OpenAI-compatible chat completions backend.

    Works with OpenAI, Azure OpenAI, vLLM, and any endpoint that
    implements the ``/v1/chat/completions`` interface.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_file: str | None = None,
        default_model: str = "gpt-4o",
        base_url: str = "https://api.openai.com",
        timeout: float = 60.0,
    ) -> None:
        self._api_key = _resolve_key(api_key, api_key_file)
        self._default_model = default_model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "openai"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> LlmResponse:
        """Generate a completion via the OpenAI chat completions API."""
        model_name = model or self._default_model
        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        client = await self._get_client()
        t0 = time.monotonic()
        resp = await client.post(f"{self._base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        elapsed_ms = (time.monotonic() - t0) * 1000

        data = resp.json()
        choices = data.get("choices", [])
        text = choices[0]["message"]["content"] if choices else ""
        usage = data.get("usage", {})

        return LlmResponse(
            text=text,
            provider="openai",
            model=data.get("model", model_name),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=elapsed_ms,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


def _resolve_key(api_key: str | None, api_key_file: str | None) -> str:
    """Resolve API key from direct value or file."""
    if api_key:
        return api_key
    if api_key_file:
        p = Path(api_key_file)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    msg = "No API key provided (set api_key or api_key_file)"
    raise ValueError(msg)
