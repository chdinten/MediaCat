"""ASGI middleware stack — security headers, request-id, session, CSRF, access logging.

Applied in :func:`mediacat.web.app.create_app` via ``app.add_middleware``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from mediacat.logging_filters import new_request_id, request_id_var

logger = logging.getLogger(__name__)

# Only accept request IDs that are safe to embed in structured log output.
_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,64}$")

# Public paths that don't require authentication
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/login",
        "/static",
        "/api/docs",
        "/api/openapi.json",
    }
)


def _is_public(path: str) -> bool:
    """Check if a path is publicly accessible without auth."""
    for pub in _PUBLIC_PATHS:
        if path == pub or path.startswith(pub + "/"):
            return True
    return False


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        rid_header = request.headers.get("x-request-id", "")
        rid = rid_header if _REQUEST_ID_RE.match(rid_header) else new_request_id()
        request.state.request_id = rid
        token = request_id_var.set(rid)
        try:
            response: Response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_var.reset(token)


class SessionMiddleware(BaseHTTPMiddleware):
    """Read session cookie, populate request.state.session and CSRF token.

    This middleware bridges the auth.SessionManager and auth.CsrfProtection
    into the request lifecycle. It:
    1. Reads the session cookie and validates it.
    2. Populates ``request.state.session`` with user info.
    3. Generates a CSRF token for the session and sets ``request.state.csrf_token``.
    4. Redirects unauthenticated requests to /login (except public paths).
    5. Validates CSRF tokens on POST/PUT/DELETE requests.
    """

    def __init__(self, app: Any, *, session_manager: Any, csrf_protection: Any) -> None:
        super().__init__(app)
        self._session_mgr = session_manager
        self._csrf = csrf_protection

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        # Read and validate session cookie
        cookie_value = request.cookies.get(self._session_mgr.cookie_name, "")
        session = self._session_mgr.validate_session(cookie_value) if cookie_value else None

        request.state.session = session or {}
        request.state.user_id = session["user_id"] if session else None
        request.state.user_role = session["role"] if session else None

        # Generate CSRF token (tied to the cookie value or a nonce)
        csrf_seed = cookie_value or "anonymous"
        request.state.csrf_token = self._csrf.generate_token(csrf_seed)

        # Auth guard: redirect unauthenticated users to login
        if not session and not _is_public(request.url.path):
            return RedirectResponse(url="/login", status_code=303)

        # CSRF validation on mutating requests.
        # CSRF token is read from the X-CSRF-Token header ONLY (never
        # from the form body) to avoid the Starlette BaseHTTPMiddleware
        # body-consumption bug: reading request.form() here would prevent
        # downstream FastAPI Form() dependencies from working.
        if request.method in ("POST", "PUT", "DELETE") and session:
            csrf_token = request.headers.get("x-csrf-token", "")

            if not self._csrf.validate_token(csrf_token, csrf_seed):
                from fastapi.responses import JSONResponse

                logger.warning("CSRF validation failed for %s %s", request.method, request.url.path)
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)

        response: Response = await call_next(request)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security response headers."""

    def __init__(self, app: Any, *, csp: str = "") -> None:
        super().__init__(app)
        self._csp = csp or (
            "default-src 'self'; "
            "img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'; "
            "base-uri 'self'"
        )

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = self._csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        for hdr in ("server", "x-powered-by"):
            if hdr in response.headers:
                del response.headers[hdr]
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status, and latency."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        t0 = time.monotonic()
        response: Response = await call_next(request)
        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            "%s %s -> %d (%.0fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response
