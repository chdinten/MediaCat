"""Connector registry — load config, resolve secrets, instantiate connectors.

The registry reads ``config/connectors.yaml``, validates the entries,
reads auth tokens from Docker secrets, and produces ready-to-use
connector instances.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from mediacat.ingestion.base import BaseConnector
from mediacat.ingestion.discogs import DiscogsConnector
from mediacat.ingestion.musicbrainz import MusicBrainzConnector

logger = logging.getLogger(__name__)

# Maps connector names to their implementation classes.
_CONNECTOR_CLASSES: dict[str, type[BaseConnector]] = {
    "discogs": DiscogsConnector,
    "musicbrainz": MusicBrainzConnector,
}


def register_connector(name: str, cls: type[BaseConnector]) -> None:
    """Register a custom connector class at runtime."""
    _CONNECTOR_CLASSES[name] = cls
    logger.info("Registered connector class: %s → %s", name, cls.__name__)


def load_connectors(
    config_path: str | Path,
    *,
    secrets_root: str | Path = "/run/secrets",
) -> dict[str, BaseConnector]:
    """Load connector config and return instantiated (but not opened) connectors.

    Parameters
    ----------
    config_path
        Path to ``connectors.yaml``.
    secrets_root
        Root directory for Docker secrets.

    Returns
    -------
    dict[str, BaseConnector]
        Mapping of connector name → instance.  Only enabled connectors
        are included.  Call ``await connector.open()`` before use.
    """
    path = Path(config_path)
    if not path.exists():
        logger.warning("Connectors config not found: %s", path)
        return {}

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = raw.get("connectors", [])
    connectors: dict[str, BaseConnector] = {}

    for entry in entries:
        name = entry.get("name", "unknown")
        if not entry.get("enabled", True):
            logger.info("Connector %s is disabled; skipping", name)
            continue

        cls = _CONNECTOR_CLASSES.get(name)
        if cls is None:
            logger.warning("No implementation for connector %s; skipping", name)
            continue

        auth_header = _resolve_auth(entry.get("auth", {}), secrets_root)
        rate = entry.get("rate_limit", {}).get("requests_per_minute", 60)
        user_agent = entry.get("user_agent", "MediaCat/0.1")

        connector = cls(
            name=name,
            base_url=entry["base_url"],
            user_agent=user_agent,
            auth_header=auth_header,
            rate_limit=rate,
        )
        connectors[name] = connector
        logger.info("Loaded connector: %s (%s)", name, entry["base_url"])

    return connectors


def _resolve_auth(
    auth_cfg: dict[str, Any],
    secrets_root: str | Path,
) -> str | None:
    """Build the Authorization header value from config + secret file."""
    scheme = auth_cfg.get("scheme", "none")
    if scheme == "none":
        return None

    secret_file = auth_cfg.get("secret_file")
    if not secret_file:
        logger.warning("Auth scheme '%s' but no secret_file configured", scheme)
        return None

    # Try the configured path first (absolute); fall back to secrets_root.
    sf = Path(secret_file)
    if not sf.exists():
        sf = Path(secrets_root) / sf.name
    if not sf.exists():
        logger.warning("Secret file not found: %s", secret_file)
        return None

    token = sf.read_text(encoding="utf-8").strip()

    if scheme == "token":
        return f"Discogs token={token}"
    if scheme == "bearer":
        return f"Bearer {token}"
    return token
