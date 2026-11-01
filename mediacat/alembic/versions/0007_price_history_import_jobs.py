"""price history and collection import jobs

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-30

Schema changes
--------------
* import_job_status enum: pending, running, completed, failed, paused
* token_price_history table
* collection_import_jobs table
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create import_job_status enum (idempotent via DO block)
    op.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE import_job_status AS ENUM (
                'pending', 'running', 'completed', 'failed', 'paused'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    # Create token_price_history table
    op.create_table(
        "token_price_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column(
            "token_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(50), nullable=False, server_default="discogs"),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lowest_price", sa.Float(), nullable=True),
        sa.Column("num_for_sale", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("condition_prices", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_token_price_history_token_id", "token_price_history", ["token_id"])
    op.create_index("ix_token_price_history_fetched_at", "token_price_history", ["fetched_at"])

    # Create collection_import_jobs table
    op.create_table(
        "collection_import_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
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
        sa.Column("discogs_username", sa.String(200), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "running", "completed", "failed", "paused",
                name="import_job_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("total_releases", sa.Integer(), nullable=True),
        sa.Column(
            "imported_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "skipped_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "failed_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "last_page", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("collection_import_jobs")
    op.drop_table("token_price_history")
    op.execute(sa.text("DROP TYPE IF EXISTS import_job_status"))
