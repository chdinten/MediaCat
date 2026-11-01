"""Fix media_objects unique constraint to be per-token

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-04

Schema changes
--------------
* DROP CONSTRAINT uq_media_bucket_key (bucket, object_key) — this global
  constraint wrongly prevented two tokens from referencing the same MinIO
  object (e.g. identical cover art on different pressings). MinIO already
  deduplicates storage by content hash; the DB constraint only needs to
  prevent the same image appearing twice for the same token.
* ADD CONSTRAINT uq_media_token_content_hash (token_id, content_hash) —
  per-token uniqueness, which is the correct scope.
"""

from __future__ import annotations

from alembic import op


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_media_bucket_key", "media_objects", type_="unique")
    op.create_unique_constraint(
        "uq_media_token_content_hash", "media_objects", ["token_id", "content_hash"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_media_token_content_hash", "media_objects", type_="unique")
    op.create_unique_constraint(
        "uq_media_bucket_key", "media_objects", ["bucket", "object_key"]
    )
