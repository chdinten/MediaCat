"""Streaming-service album links

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-03

Schema changes
--------------
* CREATE TABLE streaming_links
  One row per album and streaming service (spotify | deezer | apple): the
  album page URL, or a cached "not found", or a user's manual correction.
  Albums have no table of their own (they are derived from tokens), so the
  row is keyed by the normalised artist + album title + service.

Design notes
------------
* status / match_method are VARCHAR, not PG enums, so services and methods
  can be added without DDL.
* tokens.spotify_url stays: the app keeps it filled from this table, and old
  backups (which contain the column) still restore.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "streaming_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("artist_key", sa.String(500), nullable=False),
        sa.Column("album_key", sa.String(500), nullable=False),
        sa.Column("service", sa.String(20), nullable=False),
        sa.Column("artist_name", sa.String(500), nullable=True),
        sa.Column("album_title", sa.String(500), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("external_id", sa.String(100), nullable=True),
        # barcode | search | manual
        sa.Column("match_method", sa.String(20), nullable=True),
        sa.Column("match_score", sa.Float(), nullable=True),
        # found | not_found | manual
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("artist_key", "album_key", "service", name="uq_streaming_links_album"),
    )


def downgrade() -> None:
    op.drop_table("streaming_links")
