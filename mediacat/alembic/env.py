"""Alembic async migration environment.

Imports all models so ``target_metadata`` is fully populated, enabling
``--autogenerate``.  Reads the real DSN from config + secret files.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Ensure src/ is importable when running `alembic` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Import the declarative base (carries the MetaData) and all models so
# that autogenerate can see every table.
from mediacat.db.base import Base  # noqa: E402
from mediacat.db.models import (  # noqa: E402, F401  — side-effect imports
    AuditLog,
    Country,
    IngestionJob,
    Label,
    Manufacturer,
    MediaObject,
    OcrArtifact,
    ReviewItem,
    Token,
    TokenRevision,
    User,
)

# Alembic Config object
config = context.config

# Standard logging setup from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_dsn() -> str:
    """Build the async DSN from env vars or config.

    Priority:
    1. ``DATABASE_URL`` env var (CI / testing).
    2. Assemble from individual ``PGHOST``, ``PGPORT``, etc. env vars.
    3. Fall back to the placeholder in alembic.ini (will fail at connect).
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        # Ensure async driver
        return url.replace("postgresql://", "postgresql+asyncpg://")

    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "mediacat_migrator")
    dbname = os.environ.get("PGDATABASE", "mediacat")

    # Read password from secret file or env var.
    pw_file = os.environ.get("PGPASSFILE", "/run/secrets/postgres_app_password")
    pw = os.environ.get("PGPASSWORD", "")
    if not pw and Path(pw_file).is_file():
        pw = Path(pw_file).read_text().strip()

    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{dbname}"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    context.configure(
        url=_get_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    """Execute migrations inside a synchronous callback."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode — connect to a live database."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _get_dsn()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
