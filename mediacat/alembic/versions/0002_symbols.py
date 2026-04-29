"""graphical symbol support

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-22

Adds the symbol registry (symbols, symbol_variants, token_symbols) and
extends tokens / ocr_artifacts with structured runout columns.

Schema changes
--------------
* New enum:  symbol_category
* New table: symbols
* New table: symbol_variants
* New table: token_symbols
* tokens:     + matrix_runout_b (TEXT)
              + matrix_runout_parts (JSONB)
              + matrix_runout_b_parts (JSONB)
* ocr_artifacts: + symbol_candidates (JSONB)

Seed data
---------
Level-1 and Level-2 symbols are seeded at the bottom of upgrade().
Slugs are immutable once inserted — treat them as stable public identifiers.
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Seed data — Levels 1–2 symbols
# ---------------------------------------------------------------------------
# Each dict: slug, name, category, unicode_approx, taxonomy_level,
#            region_scope, description
_SEED_SYMBOLS = [
    # ── Level 2: EMI / UK pressing marks ────────────────────────────────────
    {
        "slug": "emi-triangle",
        "name": "EMI Pressing Triangle",
        "category": "pressing_plant_mark",
        "unicode_approx": "△",
        "taxonomy_level": 2,
        "region_scope": "UK",
        "description": (
            "Upward-pointing triangle stamped into the dead wax by EMI's UK pressing plants "
            "(Hayes, Swindon). Ubiquitous on UK pressings 1960s–1980s."
        ),
    },
    {
        "slug": "prs-triangle-down",
        "name": "PRS Downward Triangle",
        "category": "certification",
        "unicode_approx": "▽",
        "taxonomy_level": 2,
        "region_scope": "UK",
        "description": (
            "Downward-pointing triangle used as a prefix by the Performing Right Society "
            "on licensed UK pressings, typically followed by a numeric code (e.g. ▽420A)."
        ),
    },
    {
        "slug": "pye-triangle",
        "name": "Pye Studios Engineer Triangle",
        "category": "engineer_mark",
        "unicode_approx": "△",
        "taxonomy_level": 2,
        "region_scope": "UK",
        "description": (
            "Triangle used at Pye Studios followed by an engineer initial "
            "(e.g. △M = Malcolm Davies, △D = Denis Preston assistant)."
        ),
    },
    {
        "slug": "porky-prime-cut",
        "name": "Porky Prime Cut",
        "category": "engineer_mark",
        "unicode_approx": None,
        "taxonomy_level": 2,
        "region_scope": "UK",
        "description": (
            "Hand-etched text mark by mastering engineer George Peckham at Utopia / Apple. "
            "Variants include 'PORKY', 'A PORKY PRIME CUT', 'PECKO DUCK'."
        ),
    },
    {
        "slug": "pecko-duck",
        "name": "Pecko Duck",
        "category": "engineer_mark",
        "unicode_approx": None,
        "taxonomy_level": 2,
        "region_scope": "UK",
        "description": "Alternate hand-etched mark by George Peckham (same person as PORKY).",
    },
    {
        "slug": "decca-circle",
        "name": "Decca / London Circle",
        "category": "pressing_plant_mark",
        "unicode_approx": "○",
        "taxonomy_level": 2,
        "region_scope": "UK",
        "description": (
            "Circle or oval stamped by Decca's Pressing Plant (New Malden, UK). "
            "Several size and style variants exist across decades."
        ),
    },
    {
        "slug": "sonic-arts-logo",
        "name": "Sonic Arts Logo",
        "category": "label_logo",
        "unicode_approx": "▭◯▭",
        "taxonomy_level": 2,
        "region_scope": "UK",
        "description": "Rectangular–circle–rectangular glyph used by Sonic Arts studios.",
    },
    # ── Level 2: US pressing plant marks ────────────────────────────────────
    {
        "slug": "capitol-la-star",
        "name": "Capitol Los Angeles Star",
        "category": "pressing_plant_mark",
        "unicode_approx": "☆",
        "taxonomy_level": 2,
        "region_scope": "US",
        "description": (
            "Star symbol used at Capitol Records' Los Angeles pressing plant. "
            "Variants include ✲ (six-point) and ✹ (eight-point burst)."
        ),
    },
    {
        "slug": "decca-us-gloversville",
        "name": "US Decca / MCA — Gloversville Plant",
        "category": "pressing_plant_mark",
        "unicode_approx": "✤",
        "taxonomy_level": 2,
        "region_scope": "US",
        "description": "Four-pointed decorative star used at the Gloversville, NY MCA/Decca plant.",
    },
    {
        "slug": "decca-us-pinckneyville",
        "name": "US Decca / MCA — Pinckneyville Plant",
        "category": "pressing_plant_mark",
        "unicode_approx": "◆",
        "taxonomy_level": 2,
        "region_scope": "US",
        "description": "Solid diamond used at the Pinckneyville, IL MCA/Decca plant.",
    },
    {
        "slug": "decca-us-richmond",
        "name": "US Decca / MCA — Richmond Plant",
        "category": "pressing_plant_mark",
        "unicode_approx": "◈",
        "taxonomy_level": 2,
        "region_scope": "US",
        "description": "Diamond-with-dot used at the Richmond, IN MCA/Decca plant.",
    },
    {
        "slug": "sheffield-lab-delta",
        "name": "Sheffield Lab Delta + Number",
        "category": "pressing_plant_mark",
        "unicode_approx": "△",
        "taxonomy_level": 2,
        "region_scope": "US",
        "description": (
            "Delta (triangle) followed by a four-digit number used by Sheffield Lab "
            "direct-to-disc and audiophile pressings."
        ),
    },
    # ── Level 3: US regional / label ────────────────────────────────────────
    {
        "slug": "sterling-sound",
        "name": "Sterling Sound Stamp",
        "category": "engineer_mark",
        "unicode_approx": None,
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": (
            "Text stamp 'STERLING' etched or stamped into the dead wax by "
            "Sterling Sound mastering studio, NYC."
        ),
    },
    {
        "slug": "masterdisk",
        "name": "Masterdisk Stamp",
        "category": "engineer_mark",
        "unicode_approx": None,
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": (
            "Text stamp 'MASTERDISK' by Masterdisk Corporation mastering studio, NYC."
        ),
    },
    {
        "slug": "columbia-santa-maria",
        "name": "Columbia Santa Maria Plant (Ƨ)",
        "category": "pressing_plant_mark",
        "unicode_approx": "Ƨ",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": "Reversed S (Ƨ) indicating Columbia's Santa Maria, CA pressing plant.",
    },
    {
        "slug": "columbia-terre-haute",
        "name": "Columbia Terre Haute Plant (T / CT / CTH)",
        "category": "pressing_plant_mark",
        "unicode_approx": "T",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": (
            "Letter T, CT, or CTH in the dead wax indicating Columbia's "
            "Terre Haute, IN pressing plant."
        ),
    },
    {
        "slug": "columbia-pitman",
        "name": "Columbia Pitman Plant (P)",
        "category": "pressing_plant_mark",
        "unicode_approx": "P",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": "Letter P indicating Columbia's Pitman, NJ pressing plant.",
    },
    {
        "slug": "columbia-carrollton",
        "name": "Columbia Carrollton Plant (G / G1)",
        "category": "pressing_plant_mark",
        "unicode_approx": "G",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": "Letter G or G1 indicating Columbia's Carrollton, GA pressing plant.",
    },
    {
        "slug": "capitol-jacksonville",
        "name": "Capitol Jacksonville Plant (0 / ())",
        "category": "pressing_plant_mark",
        "unicode_approx": "0",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": "Zero or parentheses indicating Capitol's Jacksonville, IL pressing plant.",
    },
    {
        "slug": "capitol-winchester",
        "name": "Capitol Winchester Plant (—◁)",
        "category": "pressing_plant_mark",
        "unicode_approx": "—◁",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": (
            "Dash + left-pointing triangle indicating Capitol's Winchester, VA plant."
        ),
    },
    {
        "slug": "capitol-scranton-iam",
        "name": "Capitol Scranton Plant (IAM △)",
        "category": "pressing_plant_mark",
        "unicode_approx": "△",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": "IAM triangle indicating Capitol's Scranton, PA pressing plant.",
    },
    {
        "slug": "allied-record-a",
        "name": "Allied Record Company (a / Q)",
        "category": "pressing_plant_mark",
        "unicode_approx": "a",
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": "Lowercase 'a' or circular 'Q' mark by Allied Record Company, LA.",
    },
    {
        "slug": "wakefield-tulip",
        "name": "Wakefield Manufacturing Tulip",
        "category": "pressing_plant_mark",
        "unicode_approx": None,
        "taxonomy_level": 3,
        "region_scope": "US",
        "description": (
            "Stylised tulip or 'W over M' mark used by Wakefield Manufacturing, "
            "a contract pressing plant."
        ),
    },
    # ── Level 4: Western Electric / vintage cut-type ─────────────────────────
    {
        "slug": "western-electric-blumlein-square",
        "name": "Western Electric Blumlein Square",
        "category": "cut_type",
        "unicode_approx": "□",
        "taxonomy_level": 4,
        "region_scope": None,
        "description": (
            "Square symbol on pre-war and early post-war pressings indicating "
            "Blumlein / Western Electric lateral-cut system."
        ),
    },
    {
        "slug": "western-electric-diamond",
        "name": "Western Electric Diamond (1C / 1D)",
        "category": "cut_type",
        "unicode_approx": "◇",
        "taxonomy_level": 4,
        "region_scope": None,
        "description": (
            "Diamond shape on early electrical recordings indicating WE system "
            "variants 1C or 1D."
        ),
    },
    {
        "slug": "lindstrom-pound",
        "name": "Lindström System Mark (£ / ℒ)",
        "category": "cut_type",
        "unicode_approx": "£",
        "taxonomy_level": 4,
        "region_scope": "Europe",
        "description": (
            "Pound-sign or script L used on Lindström-system (Beka, Odeon, Parlophone) "
            "shellac-era pressings."
        ),
    },
    {
        "slug": "japanese-jis",
        "name": "Japanese JIS Standard Mark (〄)",
        "category": "certification",
        "unicode_approx": "〄",
        "taxonomy_level": 4,
        "region_scope": "Japan",
        "description": (
            "JIS (Japanese Industrial Standard) certification mark found on some "
            "Japanese pressings, particularly audiophile releases."
        ),
    },
]


def upgrade() -> None:
    """Add symbol support: enum, tables, new columns, seed data."""

    # ── New enum ────────────────────────────────────────────────────────────
    sa.Enum(
        "pressing_plant_mark", "engineer_mark", "label_logo",
        "cut_type", "certification", "other",
        name="symbol_category",
    ).create(op.get_bind(), checkfirst=True)  # type: ignore[arg-type]

    # ── symbols ─────────────────────────────────────────────────────────────
    op.create_table(
        "symbols",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("category", sa.Enum(
            "pressing_plant_mark", "engineer_mark", "label_logo",
            "cut_type", "certification", "other",
            name="symbol_category", create_type=False,
        ), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("unicode_approx", sa.String(20), nullable=True),
        sa.Column("taxonomy_level", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("region_scope", sa.String(100), nullable=True),
        sa.Column("reference_image_key", sa.String(500), nullable=True),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_symbols"),
        sa.UniqueConstraint("slug", name="uq_symbols_slug"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_symbols_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"],
                                name="fk_symbols_updated_by_users", ondelete="SET NULL"),
    )
    op.create_index("ix_symbols_slug", "symbols", ["slug"], unique=True)
    op.create_index("ix_symbols_category_level", "symbols", ["category", "taxonomy_level"])

    # ── symbol_variants ─────────────────────────────────────────────────────
    op.create_table(
        "symbol_variants",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("symbol_id", sa.Uuid(), nullable=False),
        sa.Column("variant_key", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reference_image_key", sa.String(500), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_symbol_variants"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"],
                                name="fk_symbol_variants_symbol_id_symbols", ondelete="CASCADE"),
        sa.UniqueConstraint("symbol_id", "variant_key", name="uq_symbol_variant_key"),
    )
    op.create_index("ix_symbol_variants_symbol_id", "symbol_variants", ["symbol_id"])

    # ── token_symbols (FK index) ────────────────────────────────────────────
    op.create_table(
        "token_symbols",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("symbol_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(1), nullable=False, server_default=sa.text("'a'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_token_symbols"),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"],
                                name="fk_token_symbols_token_id_tokens", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"],
                                name="fk_token_symbols_symbol_id_symbols", ondelete="CASCADE"),
        sa.UniqueConstraint(
            "token_id", "symbol_id", "position", "side",
            name="uq_token_symbol_pos",
        ),
    )
    op.create_index("ix_token_symbols_token_id", "token_symbols", ["token_id"])
    op.create_index("ix_token_symbols_symbol_id", "token_symbols", ["symbol_id"])

    # ── New columns on tokens ───────────────────────────────────────────────
    op.add_column("tokens", sa.Column("matrix_runout_b", sa.Text(), nullable=True))
    op.add_column("tokens", sa.Column("matrix_runout_parts", JSONB(), nullable=True))
    op.add_column("tokens", sa.Column("matrix_runout_b_parts", JSONB(), nullable=True))

    # ── New column on ocr_artifacts ─────────────────────────────────────────
    op.add_column("ocr_artifacts", sa.Column("symbol_candidates", JSONB(), nullable=True))

    # ── Seed Level 1–2 symbols ──────────────────────────────────────────────
    # Use raw SQL with an explicit ::symbol_category cast.  op.bulk_insert with
    # sa.String() causes asyncpg to annotate the parameter as $N::varchar, which
    # PostgreSQL rejects for a user-defined enum column without an implicit cast.
    for s in _SEED_SYMBOLS:
        op.execute(
            sa.text(
                "INSERT INTO symbols "
                "(id, slug, name, category, description, "
                "unicode_approx, taxonomy_level, region_scope, is_confirmed) "
                "VALUES (CAST(:sym_id AS uuid), :slug, :name, "
                "CAST(:category AS symbol_category), "
                ":description, :unicode_approx, :taxonomy_level, :region_scope, :is_confirmed)"
            ).bindparams(
                sym_id=str(uuid.uuid4()),
                slug=s["slug"],
                name=s["name"],
                category=s["category"],
                description=s.get("description"),
                unicode_approx=s.get("unicode_approx"),
                taxonomy_level=s["taxonomy_level"],
                region_scope=s.get("region_scope"),
                is_confirmed=True,
            )
        )


def downgrade() -> None:
    """Remove all symbol-related schema additions."""
    op.drop_column("ocr_artifacts", "symbol_candidates")
    op.drop_column("tokens", "matrix_runout_b_parts")
    op.drop_column("tokens", "matrix_runout_parts")
    op.drop_column("tokens", "matrix_runout_b")
    op.drop_table("token_symbols")
    op.drop_table("symbol_variants")
    op.drop_table("symbols")
    sa.Enum(name="symbol_category").drop(op.get_bind(), checkfirst=True)  # type: ignore[arg-type]
