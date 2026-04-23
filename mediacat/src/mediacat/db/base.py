"""Declarative base and reusable column mixins.

Design notes
------------
* All primary keys are ``UUID`` — no auto-increment leakage.
* ``created_at`` / ``updated_at`` are always UTC, set server-side.
* ``AuditMixin`` adds ``created_by`` / ``updated_by`` for user attribution.
* ``SoftDeleteMixin`` adds ``deleted_at`` for logical deletion.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, Uuid, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Naming conventions make Alembic auto-generated migration names deterministic.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Project-wide declarative base.

    All models inherit from this.  Metadata naming conventions are set
    so Alembic can produce stable constraint names.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Column mixins
# ---------------------------------------------------------------------------


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` (UTC, server-side)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UUIDPrimaryKeyMixin:
    """Adds a ``id`` UUID primary key with a server-side default."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
        default=uuid.uuid4,
    )


class AuditMixin:
    """Adds ``created_by`` and ``updated_by`` FK columns referencing ``users.id``."""

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class SoftDeleteMixin:
    """Adds ``deleted_at`` for logical deletion."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
