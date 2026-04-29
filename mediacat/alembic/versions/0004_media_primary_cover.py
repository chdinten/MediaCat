"""Add is_primary_cover to media_objects

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-29

Schema changes
--------------
* media_objects.is_primary_cover  BOOLEAN NOT NULL DEFAULT FALSE

Allows one image per pressing to be designated as the primary cover —
independent of the region label.  This handles the Japanese OBI case where
the preferred album art is not the cover_front image.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_objects",
        sa.Column(
            "is_primary_cover",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("media_objects", "is_primary_cover")
