"""FastAPI application factory.

Usage::

    uvicorn mediacat.web.app:create_app --factory

The factory wires middleware, templates, static files, routers, and
the auth/session/CSRF components.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediacat import __version__
from mediacat.config import get_config, get_db_dsn
from mediacat.db.engine import get_engine, get_session_factory
from mediacat.web.auth import (
    CsrfProtection,
    LoginRateLimiter,
    RedisLoginRateLimiter,
    SessionManager,
)
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
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — startup and shutdown hooks."""
    logger.info("MediaCat %s starting", __version__)

    # DB engine — wired here so the pool is shared across requests.
    cfg = get_config()
    pg_cfg = cfg.get("postgres", {})
    dsn = get_db_dsn(cfg)
    engine = get_engine(
        dsn,
        pool_size=pg_cfg.get("pool", {}).get("min_size", 2),
        max_overflow=pg_cfg.get("pool", {}).get("max_size", 10)
        - pg_cfg.get("pool", {}).get("min_size", 2),
        pool_timeout=pg_cfg.get("pool", {}).get("timeout", 30),
    )
    app.state.db_engine = engine
    app.state.db_session_factory = get_session_factory(engine)
    logger.info("DB pool created: %s", pg_cfg.get("host", "localhost"))

    # Sync in-memory bootstrap users to the DB so FK on token_revisions.created_by resolves.
    if os.environ.get("MEDIACAT_ENV", "dev") == "dev":
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from mediacat.db.enums import UserRole
            from mediacat.db.models import User
            from mediacat.web.routes import _users_store

            if _users_store:
                async with app.state.db_session_factory() as _db:
                    for uname, udata in _users_store.items():
                        uid = uuid.UUID(udata["id"])
                        stmt = (
                            pg_insert(User)
                            .values(
                                id=uid,
                                username=uname,
                                email=udata.get("email", f"{uname}@localhost"),
                                password_hash=udata["password_hash"],
                                role=UserRole(udata.get("role", "admin")),
                                is_active=bool(udata.get("is_active", True)),
                            )
                            .on_conflict_do_update(
                                index_elements=["id"],
                                set_={"password_hash": udata["password_hash"], "is_active": True},
                            )
                        )
                        await _db.execute(stmt)
                    await _db.commit()
                logger.info("Dev bootstrap users synced to DB: %s", list(_users_store.keys()))
        except Exception as exc:
            logger.warning("Dev user DB sync skipped: %s", exc)

    # Object store (MinIO) — optional; image uploads disabled if unavailable
    obj_cfg = cfg.get("object_store", {})
    raw_endpoint = obj_cfg.get("endpoint", "minio:9000")
    endpoint = raw_endpoint.replace("http://", "").replace("https://", "").rstrip("/")
    access_key = obj_cfg.get("access_key", "mediacat")
    secret_key = obj_cfg.get("secret_key", "")
    if secret_key:
        try:
            from mediacat.storage.object_store import ObjectStore

            store = ObjectStore(endpoint, access_key, secret_key)
            await store.ensure_bucket()
            app.state.object_store = store
            logger.info("Object store connected: %s", endpoint)
        except Exception as exc:
            logger.warning("Object store unavailable (%s) — image uploads disabled", exc)
            app.state.object_store = None
    else:
        logger.info("No MinIO secret key — object store disabled (image uploads unavailable)")
        app.state.object_store = None

    # DEF-003: verify audit_log privilege revocation is in effect
    try:
        from sqlalchemy import text as _text

        async with app.state.db_session_factory() as _db:
            row = await _db.execute(
                _text(
                    "SELECT has_table_privilege('mediacat_app', 'audit_log', 'UPDATE') AS can_update,"
                    "       has_table_privilege('mediacat_app', 'audit_log', 'DELETE') AS can_delete"
                )
            )
            priv = row.mappings().one()
        if priv["can_update"] or priv["can_delete"]:
            logger.error(
                "DEF-003 VIOLATION: mediacat_app still has UPDATE=%s DELETE=%s on audit_log — "
                "run 'make db-migrate' to apply the REVOKE",
                priv["can_update"],
                priv["can_delete"],
            )
        else:
            logger.info("DEF-003 OK: audit_log is append-only for mediacat_app")
    except Exception as exc:
        logger.warning("DEF-003 privilege check skipped: %s", exc)

    # Vision backend — Ollama (local) with optional Anthropic fallback
    try:
        from mediacat.vision.adapter import (
            AnthropicVisionBackend,
            HybridVision,
            OllamaVisionBackend,
        )

        llm_cfg = cfg.get("llm", {})
        ollama_url = os.environ.get("OLLAMA_HOST") or llm_cfg.get(
            "ollama_url", "http://ollama:11434"
        )
        vlm_model = os.environ.get("OLLAMA_VLM_MODEL") or llm_cfg.get("vlm_model", "qwen2.5vl:32b")
        primary = OllamaVisionBackend(base_url=ollama_url, default_model=vlm_model)
        anthropic_key_file = cfg.get("anthropic", {}).get("api_key_file") or os.environ.get(
            "ANTHROPIC_API_KEY_FILE"
        )
        fallback = (
            AnthropicVisionBackend(api_key_file=anthropic_key_file) if anthropic_key_file else None
        )
        app.state.vision = HybridVision(primary, fallback)
        logger.info(
            "Vision backend: Ollama(%s, model=%s) fallback=%s",
            ollama_url,
            vlm_model,
            "anthropic" if fallback else "none",
        )
    except Exception as exc:
        logger.warning("Vision backend unavailable: %s — image analysis disabled", exc)
        app.state.vision = None

    # Redis login rate limiter — replaces the in-memory default set in create_app()
    redis_cfg = cfg.get("redis", {})
    redis_url = redis_cfg.get("url", "redis://redis:6379/0")
    redis_pw = redis_cfg.get("password") or None
    login_cfg = cfg.get("security", {}).get("login", {})
    rl_max = login_cfg.get("lockout_threshold", 10)
    rl_window = login_cfg.get("lockout_window_seconds", 900)
    redis_client = None
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(redis_url, password=redis_pw, decode_responses=False)
        await redis_client.ping()
        app.state.login_rate_limiter = RedisLoginRateLimiter(
            redis_client, max_attempts=rl_max, window_seconds=rl_window
        )
        logger.info(
            "Login rate limiter: Redis (%s, max=%d, window=%ds)", redis_url, rl_max, rl_window
        )
    except Exception as exc:
        logger.warning("Redis unavailable (%s) — login rate limiter using in-memory fallback", exc)

    yield

    await engine.dispose()
    if redis_client is not None:
        await redis_client.aclose()
    logger.info("MediaCat shutting down")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    cfg = get_config()
    security_cfg = cfg.get("security", {})
    session_secret = security_cfg.get("session_secret", "CHANGE-ME-IN-PRODUCTION")
    csrf_secret = security_cfg.get("csrf_secret") or (session_secret + ":csrf")

    # Parse trusted proxy CIDRs for X-Forwarded-For validation (SEC-005).
    server_cfg = cfg.get("server", {})
    _proxy_cidrs: list[str] = server_cfg.get(
        "trusted_proxies", ["127.0.0.1/32", "::1/128", "172.16.0.0/12", "10.0.0.0/8"]
    )
    trusted_proxy_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in _proxy_cidrs:
        try:
            trusted_proxy_networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("Invalid trusted_proxy CIDR %r — skipping", cidr)

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
    csrf_protection = CsrfProtection(csrf_secret)
    rate_limiter = LoginRateLimiter(
        max_attempts=security_cfg.get("login", {}).get("lockout_threshold", 10),
        window_seconds=security_cfg.get("login", {}).get("lockout_window_seconds", 900),
    )

    # Store on app.state so routes can access them
    app.state.session_manager = session_manager
    app.state.csrf_protection = csrf_protection
    app.state.login_rate_limiter = rate_limiter
    app.state.trusted_proxy_networks = trusted_proxy_networks
    logger.info("Trusted proxy networks: %s", [str(n) for n in trusted_proxy_networks])

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

    # Catalogue router imported here to avoid circular imports at module level.
    from mediacat.web.catalogue import catalogue_router

    app.include_router(catalogue_router)

    return app
