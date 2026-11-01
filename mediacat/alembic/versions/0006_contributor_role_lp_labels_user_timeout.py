"""contributor role, multi-LP label regions, user session timeout

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-30

Schema changes
--------------
* user_role enum:   add 'contributor'
* image_region enum: add 'label_c' … 'label_h'  (LP 2-4 label shots)
* users.session_timeout_seconds  INTEGER NOT NULL DEFAULT 86400
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add contributor to user_role enum (ADD VALUE is safe; no table rewrite)
    op.execute(sa.text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'contributor'"))

    # Add LP2-4 label regions to image_region enum
    for val in ("label_c", "label_d", "label_e", "label_f", "label_g", "label_h"):
        op.execute(sa.text(f"ALTER TYPE image_region ADD VALUE IF NOT EXISTS '{val}'"))

    # session_timeout_seconds on users (seconds until cookie expires, default 24 h)
    op.add_column(
        "users",
        sa.Column(
            "session_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default="86400",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "session_timeout_seconds")
    # PostgreSQL does not support removing enum values; downgrade is a no-op for enums.
