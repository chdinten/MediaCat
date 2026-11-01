"""Add spotify_url to tokens table

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-20

Schema changes
--------------
* tokens.spotify_url VARCHAR(500) — cached Spotify album page URL for the
  release, populated lazily during discography enrichment.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tokens",
        sa.Column("spotify_url", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tokens", "spotify_url")
