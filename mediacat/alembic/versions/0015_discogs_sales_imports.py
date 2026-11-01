"""Discogs sales history: user-assisted browser import table

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-10

Schema changes
--------------
* CREATE TABLE discogs_sales_imports
  Stores per-pressing sales-history data captured via user-assisted
  copy-and-paste from the Discogs sales-history page.  The application
  never fetches the Discogs page itself; all data is submitted explicitly
  by the authenticated user.

Design notes
------------
* All monetary values use NUMERIC(12, 4) — not FLOAT — to avoid
  floating-point representation errors in currency arithmetic.
* source_confidence / valuation_confidence are VARCHAR(20) enums in
  application code: "high" | "medium" | "low".  Not a PG enum so that
  the vocabulary can expand without a DDL migration.
* raw_import_text is nullable.  It is stored only when the user explicitly
  consents via the per-import checkbox (raw_storage_consented = TRUE).
* parsed_sales_rows JSONB: list of row objects.  sold_price is stored as
  a numeric string (e.g. "24.0000") to avoid JSON float precision loss.
* parser_diagnostics JSONB records row counts, discard reasons, and the
  parser version so old imports can be reinterpreted if the parser improves.
* outlier rows are kept in parsed_sales_rows with is_outlier=true and
  excluded from the aggregate low/median/high columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discogs_sales_imports",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        # ── provenance ────────────────────────────────────────────────────────
        sa.Column(
            "token_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "imported_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # copied from Token at import time so the import is self-contained
        sa.Column("discogs_release_id", sa.Integer(), nullable=True),
        # user-supplied URL; the server never fetches it
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(50),
            nullable=False,
            server_default="manual_discogs_browser_import",
        ),
        # ── raw text (optional, consent-gated) ───────────────────────────────
        sa.Column("raw_import_text", sa.Text(), nullable=True),
        sa.Column(
            "raw_storage_consented",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        # ── parser output ────────────────────────────────────────────────────
        # Always stored; allows re-running improved parsers against old imports.
        sa.Column("parser_diagnostics", postgresql.JSONB(), nullable=True),
        # List of validated row objects; sold_price stored as numeric string.
        sa.Column("parsed_sales_rows", postgresql.JSONB(), nullable=True),
        # ── aggregates (NUMERIC, not FLOAT) ───────────────────────────────────
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("low_sold_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("median_sold_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("high_sold_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("newest_sold_date", sa.Date(), nullable=True),
        sa.Column("oldest_sold_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        # number of rows flagged as outliers (present in parsed_sales_rows
        # with is_outlier=true but excluded from the aggregate columns above)
        sa.Column("outlier_count", sa.Integer(), nullable=True),
        # ── confidence ───────────────────────────────────────────────────────
        # source_confidence: always "high" for this import source
        sa.Column(
            "source_confidence",
            sa.String(20),
            nullable=False,
            server_default="high",
        ),
        # valuation_confidence: "high" | "medium" | "low" — derived from
        # row count, data recency, and outlier ratio; NOT automatically "high"
        # just because the user confirmed the import.
        sa.Column(
            "valuation_confidence",
            sa.String(20),
            nullable=False,
            server_default="low",
        ),
        # Human-readable notes explaining how valuation_confidence was calculated.
        sa.Column("valuation_confidence_notes", postgresql.JSONB(), nullable=True),
    )

    op.create_index(
        "ix_discogs_sales_imports_token_id",
        "discogs_sales_imports",
        ["token_id"],
    )
    # Composite index for "most recent import per token" queries
    op.create_index(
        "ix_discogs_sales_imports_token_captured",
        "discogs_sales_imports",
        ["token_id", sa.text("captured_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_discogs_sales_imports_token_captured", "discogs_sales_imports")
    op.drop_index("ix_discogs_sales_imports_token_id", "discogs_sales_imports")
    op.drop_table("discogs_sales_imports")
