"""Add discography cache columns to artist_bios

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-02

Schema changes
--------------
* artist_bios.disco_data      JSONB NULL — cached discography list
* artist_bios.disco_source    VARCHAR(100) NULL — source used (Discogs/MB/Wikipedia)
* artist_bios.disco_fetched_at TIMESTAMPTZ NULL — when the cache was last filled
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artist_bios", sa.Column("disco_data", JSONB(), nullable=True))
    op.add_column("artist_bios", sa.Column("disco_source", sa.String(100), nullable=True))
    op.add_column("artist_bios", sa.Column("disco_fetched_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("artist_bios", "disco_fetched_at")
    op.drop_column("artist_bios", "disco_source")
    op.drop_column("artist_bios", "disco_data")
