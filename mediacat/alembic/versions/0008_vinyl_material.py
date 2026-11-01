"""Add vinyl_material column to tokens

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-02

Schema changes
--------------
* tokens.vinyl_material VARCHAR(100) NULL — stores the vinyl compound ID
  (e.g. "pvc", "virgin_pvc", "carbon_black_vinyl") chosen by the cataloguer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tokens", sa.Column("vinyl_material", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("tokens", "vinyl_material")
