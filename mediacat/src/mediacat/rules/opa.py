"""Open Policy Agent HTTP adapter.

Queries OPA's Data API at ``POST /v1/data/{policy_path}`` with the
media format and raw fields as input, and maps the response to a
:class:`~mediacat.rules.engine.DecodeResult`.

The OPA server runs as a sidecar container with policy bundles mounted
from ``deploy/opa/bundles/``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mediacat.rules.engine import DecodeResult

logger = logging.getLogger(__name__)


class OpaRuleEngine:
    """Rule engine backed by Open Policy Agent over HTTP.

    Parameters
    ----------
    base_url
        OPA server URL, e.g. ``http://opa:8181``.
    policy_path
        Rego package path (dots replaced with slashes), e.g.
        ``mediacat/decode``.
    timeout
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "http://opa:8181",
        policy_path: str = "mediacat/decode",
        timeout: float = 5.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/data/{policy_path}"
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    async def decode(
        self,
        media_format: str,
        fields: dict[str, Any],
    ) -> DecodeResult:
        """Query OPA for country-specific decoding."""
        payload = {
            "input": {
                "media_format": media_format,
                "fields": fields,
            }
        }

        try:
            client = await self._get_client()
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, httpx.TransportError) as exc:
            logger.error("OPA query failed: %s", exc)
            return DecodeResult(
                status="error",
                decoded={},
                warnings=[f"OPA unavailable: {exc}"],
                confidence=0.0,
            )

        # OPA wraps the result under {"result": {...}}
        result = data.get("result", {})

        return DecodeResult(
            status=result.get("status", "unknown"),
            decoded=result.get("decoded", {}),
            warnings=result.get("warnings", []),
            rule_ids=result.get("rule_ids", []),
            confidence=float(result.get("confidence", 0.0)),
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
