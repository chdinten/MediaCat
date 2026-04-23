"""FastAPI application factory.

Usage::

    uvicorn mediacat.web.app:create_app --factory

The factory wires middleware, templates, static files, routers, and
the auth/session/CSRF components.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediacat import __version__
from mediacat.config import get_config
from mediacat.web.auth import CsrfProtection, LoginRateLimiter, SessionManager
from mediacat.web.middleware import (
    AccessLogMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    SessionMiddleware,
)
from mediacat.web.routes import (
    auth_router,
    dashboard_router,
    health_router,
    review_router,
    set_templates,
    token_router,
    user_router,
)

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
    """Application lifespan — startup and shutdown hooks."""
    logger.info("MediaCat %s starting", __version__)
    yield
    logger.info("MediaCat shutting down")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    cfg = get_config()
    security_cfg = cfg.get("security", {})
    session_secret = security_cfg.get("session_secret", "CHANGE-ME-IN-PRODUCTION")

    app = FastAPI(
        title="MediaCat",
        version=__version__,
        description="Cataloging platform for physical music media.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # ---- Auth components ----
    cookie_secure: bool = security_cfg.get("cookie_secure", False)
    session_manager = SessionManager(
        session_secret,
        max_age_seconds=86400,
        cookie_secure=cookie_secure,
    )
    csrf_protection = CsrfProtection(session_secret)
    rate_limiter = LoginRateLimiter(
        max_attempts=security_cfg.get("login", {}).get("lockout_threshold", 10),
        window_seconds=security_cfg.get("login", {}).get("lockout_window_seconds", 900),
    )

    # Store on app.state so routes can access them
    app.state.session_manager = session_manager
    app.state.csrf_protection = csrf_protection
    app.state.login_rate_limiter = rate_limiter

    # ---- Middleware (outermost first) ----
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        SessionMiddleware,
        session_manager=session_manager,
        csrf_protection=csrf_protection,
    )
    app.add_middleware(RequestIdMiddleware)

    # ---- Templates ----
    templates_dir = _WEB_DIR / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    set_templates(templates)

    # ---- Static files ----
    static_dir = _WEB_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ---- Routers ----
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(review_router)
    app.include_router(token_router)
    app.include_router(user_router)

    return app
