"""Ollama backend — local LLM inference.

Communicates with an Ollama server over HTTP (default ``http://ollama:11434``).
Uses the ``/api/chat`` endpoint with JSON mode.

Requirements
------------
* Ollama must be running and the desired model pulled.
* For vision tasks, use a VLM model (e.g. ``llava``, ``qwen2-vl``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from mediacat.llm.adapter import LlmResponse

logger = logging.getLogger(__name__)


class OllamaBackend:
    """Local LLM backend via Ollama.

    Parameters
    ----------
    base_url
        Ollama server URL.
    default_model
        Model to use when none is specified per-call.
    timeout
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "http://ollama:11434",
        default_model: str = "llama3.1",
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "ollama"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
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
        """Generate a completion via Ollama's chat API."""
        model_name = model or self._default_model
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        client = await self._get_client()
        t0 = time.monotonic()
        resp = await client.post(f"{self._base_url}/api/chat", json=payload)
        resp.raise_for_status()
        elapsed_ms = (time.monotonic() - t0) * 1000

        data = resp.json()
        text = data.get("message", {}).get("content", "")
        # Ollama returns token counts in eval_count / prompt_eval_count
        input_tokens = data.get("prompt_eval_count", 0)
        output_tokens = data.get("eval_count", 0)

        return LlmResponse(
            text=text,
            provider="ollama",
            model=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
