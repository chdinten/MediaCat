"""Typed configuration loader.

Reads ``config/app.yaml`` and resolves secrets from Docker secret files.
Provides a singleton :func:`get_config` that caches the parsed result.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote as _url_quote

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = "config/app.yaml"
_DEFAULT_CONNECTORS_PATH = "config/connectors.yaml"


def _read_secret(path: str | Path) -> str:
    """Read a Docker secret file, stripping trailing newlines."""
    p = Path(path)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    logger.warning("Secret file not found: %s", p)
    return ""


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """Load and cache the application configuration.

    Resolution order for the config path:
    1. ``MEDIACAT_CONFIG_PATH`` env var
    2. ``config/app.yaml`` relative to CWD

    Secrets are resolved from the paths specified in the YAML
    (e.g. ``/run/secrets/postgres_app_password``).
    """
    config_path = os.environ.get("MEDIACAT_CONFIG_PATH", _DEFAULT_CONFIG_PATH)
    p = Path(config_path)
    if not p.exists():
        logger.warning("Config file not found: %s — using defaults", p)
        return _defaults()

    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # Resolve secrets into the config dict
    _resolve_secrets(raw)

    return raw


def _resolve_secrets(cfg: dict[str, Any]) -> None:
    """Read secret files and inject values into the config dict."""
    # Postgres password
    pg = cfg.get("postgres", {})
    pw_file = pg.get("password_file", "/run/secrets/postgres_app_password")
    pg["password"] = pg.get("password") or _read_secret(pw_file)

    # MinIO secret key
    obj = cfg.get("object_store", {})
    sk_file = obj.get("secret_key_file", "/run/secrets/minio_root_password")
    obj["secret_key"] = obj.get("secret_key") or _read_secret(sk_file)

    # Redis password
    redis = cfg.get("redis", {})
    rp_file = redis.get("password_file", "/run/secrets/redis_password")
    redis["password"] = redis.get("password") or _read_secret(rp_file)

    # Session secret (generate if missing — dev only)
    sec = cfg.setdefault("security", {})
    session_file = sec.get("session_secret_file", "/run/secrets/session_secret")
    sec["session_secret"] = sec.get("session_secret") or _read_secret(session_file)
    if not sec["session_secret"]:
        import secrets as _secrets

        sec["session_secret"] = _secrets.token_hex(32)
        logger.warning("No session secret found — generated ephemeral key (dev only)")

    # CSRF secret — separate from session secret for domain separation.
    # Falls back to a derived value if no dedicated secret is configured.
    csrf_file = sec.get("csrf_secret_file", "/run/secrets/csrf_secret")
    sec["csrf_secret"] = sec.get("csrf_secret") or _read_secret(csrf_file)
    if not sec["csrf_secret"]:
        sec["csrf_secret"] = sec["session_secret"] + ":csrf"
        logger.info(
            "No csrf_secret configured — derived from session_secret (add csrf_secret for full separation)"
        )


def get_db_dsn(cfg: dict[str, Any] | None = None) -> str:
    """Build the async Postgres DSN from config."""
    cfg = cfg or get_config()
    pg = cfg.get("postgres", {})
    host = pg.get("host", "localhost")
    port = pg.get("port", 5432)
    user = pg.get("user", "mediacat_app")
    password = pg.get("password", "")
    database = pg.get("database", "mediacat")
    return f"postgresql+asyncpg://{_url_quote(user, safe='')}:{_url_quote(password, safe='')}@{host}:{port}/{database}"


def _defaults() -> dict[str, Any]:
    """Minimal defaults when no config file is found."""
    import secrets as _secrets

    return {
        "app": {"name": "mediacat", "environment": "dev", "log_level": "INFO"},
        "server": {
            "host": "0.0.0.0",
            "port": 8000,
            "trusted_proxies": ["127.0.0.1/32", "::1/128", "172.16.0.0/12", "10.0.0.0/8"],
        },
        "security": {"session_secret": _secrets.token_hex(32)},
        "postgres": {
            "host": "localhost",
            "port": 5432,
            "user": "mediacat_app",
            "password": "",
            "database": "mediacat",
        },
        "object_store": {"endpoint": "http://localhost:9000"},
        "redis": {"url": "redis://localhost:6379/0"},
        "rule_engine": {"backend": "local"},
    }
