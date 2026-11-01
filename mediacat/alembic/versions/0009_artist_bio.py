"""Add artist_bios table for cached Wikipedia biographies

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-02

Schema changes
--------------
* artist_bios — keyed by artist_name; stores Wikipedia extract, URL,
  thumbnail, source tag, and fetch timestamp.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artist_bios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artist_name", sa.String(500), nullable=False),
        sa.Column("extract", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("wiki_url", sa.String(500), nullable=True),
        sa.Column("thumbnail_url", sa.String(500), nullable=True),
        sa.Column("bio_source", sa.String(100), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artist_name", name="uq_artist_bios_name"),
    )
    op.create_index("ix_artist_bios_artist_name", "artist_bios", ["artist_name"])


def downgrade() -> None:
    op.drop_index("ix_artist_bios_artist_name", table_name="artist_bios")
    op.drop_table("artist_bios")
