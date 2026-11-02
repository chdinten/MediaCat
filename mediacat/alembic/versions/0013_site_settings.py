"""Site settings table for admin-managed API credentials and config

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-20

Schema changes
--------------
* CREATE TABLE site_settings (key VARCHAR PK, value TEXT, description TEXT,
  updated_at TIMESTAMPTZ) — admin-managed key/value store for API tokens
  and other site-wide configuration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text, nullable=False, server_default=""),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("site_settings")
