"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-17

Creates all PostgreSQL enum types and tables for the MediaCat domain.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------- Enum definitions --------------------------------------------------

ENUMS = [
    ("media_format", ["vinyl", "cd"]),
    ("token_status", ["draft", "active", "merged", "archived"]),
    ("revision_source", ["ingestion", "vision", "ocr", "human", "llm", "import"]),
    ("review_status", ["pending", "in_progress", "approved", "rejected", "deferred"]),
    ("review_reason", ["low_confidence", "conflict", "novel_entity", "anomaly", "manual"]),
    ("ingestion_job_status", ["queued", "running", "completed", "failed", "cancelled"]),
    ("ocr_engine", ["tesseract", "azure", "aws_textract", "manual"]),
    (
        "image_region",
        [
            "label_a", "label_b", "obi_front", "obi_back", "obi_spine",
            "runout_a", "runout_b", "matrix", "cover_front", "cover_back",
            "sleeve_inner", "disc_surface", "other",
        ],
    ),
    ("user_role", ["admin", "reviewer", "viewer", "service"]),
]


def upgrade() -> None:
    """Create all enum types and tables."""

    # ---- Enum types ----------------------------------------------------------
    # PostgreSQL has no CREATE TYPE IF NOT EXISTS. Use a DO block that swallows
    # duplicate_object so this step is idempotent on re-runs.
    for name, values in ENUMS:
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(sa.text(
            f"DO $$ BEGIN "
            f"CREATE TYPE {name} AS ENUM ({values_sql}); "
            f"EXCEPTION WHEN duplicate_object THEN NULL; "
            f"END $$"
        ))

    # ---- Extensions (also in initdb but safe to repeat) ----------------------
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "btree_gist"')

    # ---- users ---------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("username", sa.String(150), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Enum(name="user_role", create_type=False), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mfa_secret", sa.Text(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # ---- countries -----------------------------------------------------------
    op.create_table(
        "countries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("alpha2", sa.String(2), nullable=False),
        sa.Column("alpha3", sa.String(3), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("numeric_code", sa.String(3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_countries"),
        sa.UniqueConstraint("alpha2", name="uq_countries_alpha2"),
        sa.UniqueConstraint("alpha3", name="uq_countries_alpha3"),
    )

    # ---- labels --------------------------------------------------------------
    op.create_table(
        "labels",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("name_normalised", sa.String(500), nullable=False),
        sa.Column("country_id", sa.Uuid(), nullable=True),
        sa.Column("discogs_id", sa.Integer(), nullable=True),
        sa.Column("musicbrainz_id", sa.String(36), nullable=True),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_labels"),
        sa.ForeignKeyConstraint(["country_id"], ["countries.id"], name="fk_labels_country_id_countries",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_labels_created_by_users",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_labels_updated_by_users",
                                ondelete="SET NULL"),
        sa.UniqueConstraint("discogs_id", name="uq_labels_discogs_id"),
        sa.UniqueConstraint("musicbrainz_id", name="uq_labels_musicbrainz_id"),
    )
    op.create_index("ix_labels_name_normalised", "labels", ["name_normalised"])
    op.create_index(
        "ix_labels_trgm_name", "labels", ["name_normalised"],
        postgresql_using="gin", postgresql_ops={"name_normalised": "gin_trgm_ops"},
    )

    # ---- manufacturers -------------------------------------------------------
    op.create_table(
        "manufacturers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("name_normalised", sa.String(500), nullable=False),
        sa.Column("country_id", sa.Uuid(), nullable=True),
        sa.Column("plant_code", sa.String(50), nullable=True),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_manufacturers"),
        sa.ForeignKeyConstraint(["country_id"], ["countries.id"],
                                name="fk_manufacturers_country_id_countries", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_manufacturers_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"],
                                name="fk_manufacturers_updated_by_users", ondelete="SET NULL"),
    )
    op.create_index("ix_manufacturers_name_normalised", "manufacturers", ["name_normalised"])
    op.create_index(
        "ix_manufacturers_trgm_name", "manufacturers", ["name_normalised"],
        postgresql_using="gin", postgresql_ops={"name_normalised": "gin_trgm_ops"},
    )

    # ---- ingestion_jobs (created before tokens for FK from revisions) --------
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("connector_name", sa.String(100), nullable=False),
        sa.Column("status", sa.Enum(name="ingestion_job_status", create_type=False), nullable=False),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_jobs"),
    )
    op.create_index("ix_jobs_status_created", "ingestion_jobs", ["status", "created_at"])

    # ---- tokens --------------------------------------------------------------
    op.create_table(
        "tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("barcode", sa.String(50), nullable=True),
        sa.Column("catalog_number", sa.String(100), nullable=True),
        sa.Column("matrix_runout", sa.String(500), nullable=True),
        sa.Column("media_format", sa.Enum(name="media_format", create_type=False), nullable=False),
        sa.Column("status", sa.Enum(name="token_status", create_type=False), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("artist", sa.String(500), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("country_id", sa.Uuid(), nullable=True),
        sa.Column("label_id", sa.Uuid(), nullable=True),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=True),
        sa.Column("discogs_release_id", sa.Integer(), nullable=True),
        sa.Column("discogs_master_id", sa.Integer(), nullable=True),
        sa.Column("musicbrainz_release_id", sa.String(36), nullable=True),
        sa.Column("current_revision_id", sa.Uuid(), nullable=True),
        sa.Column("extra", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_tokens"),
        sa.ForeignKeyConstraint(["country_id"], ["countries.id"],
                                name="fk_tokens_country_id_countries", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["label_id"], ["labels.id"],
                                name="fk_tokens_label_id_labels", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["manufacturer_id"], ["manufacturers.id"],
                                name="fk_tokens_manufacturer_id_manufacturers", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_tokens_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"],
                                name="fk_tokens_updated_by_users", ondelete="SET NULL"),
    )
    op.create_index("ix_tokens_barcode", "tokens", ["barcode"])
    op.create_index("ix_tokens_catalog_number", "tokens", ["catalog_number"])
    op.create_index("ix_tokens_discogs_release_id", "tokens", ["discogs_release_id"])
    op.create_index("ix_tokens_musicbrainz_release_id", "tokens", ["musicbrainz_release_id"])
    op.create_index(
        "ix_tokens_title_trgm", "tokens", ["title"],
        postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_tokens_artist_trgm", "tokens", ["artist"],
        postgresql_using="gin", postgresql_ops={"artist": "gin_trgm_ops"},
    )

    # ---- token_revisions -----------------------------------------------------
    op.create_table(
        "token_revisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("source", sa.Enum(name="revision_source", create_type=False), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("diff", JSONB(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("ingestion_job_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_token_revisions"),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"],
                                name="fk_token_revisions_token_id_tokens", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_token_revisions_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ingestion_job_id"], ["ingestion_jobs.id"],
                                name="fk_token_revisions_ingestion_job_id_ingestion_jobs",
                                ondelete="SET NULL"),
        sa.UniqueConstraint("token_id", "revision_number", name="uq_token_revision_number"),
    )
    op.create_index("ix_token_revisions_token_id", "token_revisions", ["token_id"])

    # Now add the deferred FK from tokens.current_revision_id -> token_revisions.id
    op.create_foreign_key(
        "fk_tokens_current_revision_id_token_revisions",
        "tokens", "token_revisions",
        ["current_revision_id"], ["id"],
        ondelete="SET NULL",
    )

    # ---- media_objects -------------------------------------------------------
    op.create_table(
        "media_objects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(100), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column("region", sa.Enum(name="image_region", create_type=False), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_media_objects"),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"],
                                name="fk_media_objects_token_id_tokens", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_media_objects_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"],
                                name="fk_media_objects_updated_by_users", ondelete="SET NULL"),
        sa.UniqueConstraint("bucket", "object_key", name="uq_media_bucket_key"),
    )
    op.create_index("ix_media_objects_token_id", "media_objects", ["token_id"])
    op.create_index("ix_media_objects_content_hash", "media_objects", ["content_hash"])

    # ---- ocr_artifacts -------------------------------------------------------
    op.create_table(
        "ocr_artifacts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("media_object_id", sa.Uuid(), nullable=False),
        sa.Column("engine", sa.Enum(name="ocr_engine", create_type=False), nullable=False),
        sa.Column("region", sa.Enum(name="image_region", create_type=False), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("detected_language", sa.String(10), nullable=True),
        sa.Column("translated_text", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ocr_artifacts"),
        sa.ForeignKeyConstraint(["media_object_id"], ["media_objects.id"],
                                name="fk_ocr_artifacts_media_object_id_media_objects",
                                ondelete="CASCADE"),
    )
    op.create_index("ix_ocr_artifacts_media_object_id", "ocr_artifacts", ["media_object_id"])

    # ---- review_items --------------------------------------------------------
    op.create_table(
        "review_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Enum(name="review_status", create_type=False), nullable=False),
        sa.Column("reason", sa.Enum(name="review_reason", create_type=False), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("details", JSONB(), nullable=True),
        sa.Column("assigned_to", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_review_items"),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"],
                                name="fk_review_items_token_id_tokens", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], ["token_revisions.id"],
                                name="fk_review_items_revision_id_token_revisions",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"],
                                name="fk_review_items_assigned_to_users", ondelete="SET NULL"),
    )
    op.create_index("ix_review_items_token_id", "review_items", ["token_id"])
    op.create_index(
        "ix_review_pending", "review_items", ["status", "priority", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # ---- audit_log -----------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("request_id", sa.String(36), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_audit_log_user_id_users", ondelete="SET NULL"),
    )
    op.create_index("ix_audit_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_timestamp", "audit_log", ["timestamp"])
    op.create_index("ix_audit_user", "audit_log", ["user_id"])

    # DEF-003: audit_log must be append-only for the app role.
    # Default privileges gave mediacat_app UPDATE + DELETE; revoke them so
    # neither the app nor a compromised session can erase or alter audit rows.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mediacat_app') THEN
                REVOKE UPDATE, DELETE ON TABLE audit_log FROM mediacat_app;
            END IF;
        END $$
    """)


def downgrade() -> None:
    """Drop all tables and enum types."""
    # Restore privileges before dropping so a re-upgrade starts from a clean state.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mediacat_app') THEN
                GRANT UPDATE, DELETE ON TABLE audit_log TO mediacat_app;
            END IF;
        END $$
    """)
    op.drop_table("audit_log")
    op.drop_table("review_items")
    op.drop_table("ocr_artifacts")
    op.drop_table("media_objects")
    op.drop_constraint("fk_tokens_current_revision_id_token_revisions", "tokens", type_="foreignkey")
    op.drop_table("token_revisions")
    op.drop_table("tokens")
    op.drop_table("ingestion_jobs")
    op.drop_table("manufacturers")
    op.drop_table("labels")
    op.drop_table("countries")
    op.drop_table("users")

    for name, _ in reversed(ENUMS):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)  # type: ignore[arg-type]
