"""Abstract connector base class with retry, circuit breaker, and rate limiting.

Every ingestion connector inherits from :class:`BaseConnector` and implements
two methods:

* ``fetch_release(external_id)`` — fetch a single release by its source ID.
* ``search_releases(query, **kwargs)`` — search for releases by text query.

The base class provides:

* **Rate limiting** — token-bucket per connector, configured in YAML.
* **Retry with exponential backoff** — configurable max attempts and delays.
* **Circuit breaker** — opens after N consecutive failures, auto-recovers.
* **Request accounting** — every HTTP call is logged with timing.

Security
--------
* Credentials are never logged.  The ``Authorization`` header is built
  from a secret file path, not from an inline value.
* User-Agent is always set (required by Discogs and MusicBrainz).
* All outbound requests use ``httpx.AsyncClient`` with timeouts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class ConnectorStatus(StrEnum):
    """Runtime health state of a connector."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # retries happening
    OPEN = "open"  # circuit breaker tripped


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Normalised result from a connector fetch or search."""

    source: str
    """Connector name (e.g. 'discogs')."""

    external_id: str
    """ID in the source system."""

    raw_payload: dict[str, Any]
    """Unmodified JSON from the upstream API."""

    normalised: dict[str, Any]
    """Fields mapped to MediaCat's domain vocabulary."""

    image_urls: list[str] = field(default_factory=list)
    """Direct URLs to cover / label / obi images."""

    confidence: float = 1.0
    """Connector's self-assessed confidence (0-1)."""


# ---------------------------------------------------------------------------
# Rate limiter (token bucket)
# ---------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Simple async token-bucket rate limiter.

    Parameters
    ----------
    rate
        Maximum requests per period.
    period
        Period in seconds (default 60 = per minute).
    """

    def __init__(self, rate: int, period: float = 60.0) -> None:
        self._rate = rate
        self._period = period
        self._tokens = float(rate)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * (self._rate / self._period))
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * (self._period / self._rate)
                logger.debug("Rate limiter: waiting %.2fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Simple circuit breaker with automatic recovery.

    Parameters
    ----------
    failure_threshold
        Consecutive failures before opening.
    recovery_seconds
        Time to wait before allowing a probe request.
    """

    def __init__(self, failure_threshold: int = 5, recovery_seconds: float = 60.0) -> None:
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return not (time.monotonic() - self._opened_at >= self._recovery)

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()
            logger.warning(
                "Circuit breaker OPEN after %d failures (recovery in %.0fs)",
                self._failures,
                self._recovery,
            )


class CircuitBreakerOpenError(Exception):
    """Raised when a request is rejected by an open circuit breaker."""


# ---------------------------------------------------------------------------
# Abstract base connector
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """Abstract base for all ingestion connectors.

    Subclasses implement :meth:`fetch_release` and :meth:`search_releases`.
    The base provides :meth:`_request` with rate limiting, retry, and
    circuit breaker.

    Parameters
    ----------
    name
        Connector name (matches ``connectors.yaml`` entry).
    base_url
        API root URL.
    user_agent
        ``User-Agent`` header value.
    auth_header
        Pre-built ``Authorization`` header value (from secret file).
    rate_limit
        Requests per minute.
    timeout
        Per-request timeout in seconds.
    max_retries
        Maximum retry attempts.
    base_delay
        Initial backoff delay in seconds.
    max_delay
        Cap on backoff delay.
    cb_threshold
        Circuit breaker failure threshold.
    cb_recovery
        Circuit breaker recovery time in seconds.
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        user_agent: str = "MediaCat/0.1",
        auth_header: str | None = None,
        rate_limit: int = 60,
        timeout: float = 30.0,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        cb_threshold: int = 5,
        cb_recovery: float = 60.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._auth_header = auth_header
        self._timeout = timeout
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._rate_limiter = TokenBucketRateLimiter(rate_limit)
        self._circuit_breaker = CircuitBreaker(cb_threshold, cb_recovery)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Create the HTTP client.  Call once before use."""
        headers: dict[str, str] = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> BaseConnector:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Protected HTTP helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue an HTTP request with rate limiting, retry, and circuit breaker.

        Returns the parsed JSON body.

        Raises
        ------
        CircuitBreakerOpenError
            If the circuit breaker is open.
        httpx.HTTPStatusError
            If all retries are exhausted.
        """
        if self._circuit_breaker.is_open:
            raise CircuitBreakerOpenError(f"[{self.name}] circuit breaker is open")
        if not self._client:
            msg = f"[{self.name}] client not open; call .open() first"
            raise RuntimeError(msg)

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            await self._rate_limiter.acquire()
            t0 = time.monotonic()
            try:
                resp = await self._client.request(method, path, params=params)
                elapsed = time.monotonic() - t0
                logger.debug(
                    "[%s] %s %s → %d (%.2fs)",
                    self.name,
                    method,
                    path,
                    resp.status_code,
                    elapsed,
                )

                if resp.status_code == 429:
                    # Rate-limited by upstream; back off harder
                    retry_after = float(resp.headers.get("Retry-After", self._base_delay * attempt))
                    logger.warning(
                        "[%s] 429 rate-limited, retry after %.1fs", self.name, retry_after
                    )
                    await asyncio.sleep(min(retry_after, self._max_delay))
                    continue

                resp.raise_for_status()
                self._circuit_breaker.record_success()
                return resp.json()  # type: ignore[no-any-return]

            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                elapsed = time.monotonic() - t0
                logger.warning(
                    "[%s] Attempt %d/%d failed (%.2fs): %s",
                    self.name,
                    attempt,
                    self._max_retries,
                    elapsed,
                    exc,
                )
                self._circuit_breaker.record_failure()
                if attempt < self._max_retries:
                    delay = min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)
                    await asyncio.sleep(delay)

        # All retries exhausted
        if last_exc is not None:
            raise last_exc
        msg = f"[{self.name}] all retries exhausted with no recorded exception"
        raise RuntimeError(msg)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> ConnectorStatus:
        if self._circuit_breaker.is_open:
            return ConnectorStatus.OPEN
        if self._circuit_breaker._failures > 0:
            return ConnectorStatus.DEGRADED
        return ConnectorStatus.HEALTHY

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_release(self, external_id: str) -> FetchResult:
        """Fetch a single release by its source-system ID."""
        ...

    @abstractmethod
    async def search_releases(self, query: str, **kwargs: Any) -> list[FetchResult]:
        """Search for releases matching *query*."""
        ...
