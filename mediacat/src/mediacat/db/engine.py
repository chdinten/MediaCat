"""Async engine factory and session management.

Usage
-----
::

    from mediacat.db.engine import get_engine, get_session_factory

    engine = get_engine(dsn)
    session_factory = get_session_factory(engine)

    async with session_factory() as session:
        ...

The ``dsn`` is built at startup from ``config/app.yaml`` plus the
secret file for the password.  See :mod:`mediacat.config` (Section 4+).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def get_engine(
    dsn: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    pool_timeout: int = 30,
    echo: bool = False,
    **kwargs: Any,
) -> AsyncEngine:
    """Create an :class:`AsyncEngine` with connection-pool defaults.

    Parameters
    ----------
    dsn
        Async DSN, e.g. ``postgresql+asyncpg://user:pass@host/db``.
    pool_size, max_overflow, pool_timeout
        Standard SQLAlchemy pool tuning.
    echo
        Log all SQL (disable in production).
    """
    return create_async_engine(
        dsn,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        echo=echo,
        pool_pre_ping=True,
        **kwargs,
    )


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a :class:`async_sessionmaker` bound to *engine*."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transactional_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager that commits on success, rolls back on error.

    Example::

        async with transactional_session(factory) as session:
            session.add(obj)
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
