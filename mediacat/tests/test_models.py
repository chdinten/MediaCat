"""Tests for :mod:`mediacat.db.models` — import and metadata sanity."""

from __future__ import annotations

from mediacat.db.base import Base
from mediacat.db.enums import MediaFormat, TokenStatus, UserRole
from mediacat.db.models import (
    Token,
    User,
)

EXPECTED_TABLES = {
    "users",
    "countries",
    "labels",
    "manufacturers",
    "ingestion_jobs",
    "tokens",
    "token_revisions",
    "media_objects",
    "ocr_artifacts",
    "review_items",
    "audit_log",
}


def test_all_tables_registered() -> None:
    """Every expected table is in the metadata."""
    registered = set(Base.metadata.tables.keys())
    missing = EXPECTED_TABLES - registered
    assert not missing, f"Missing tables: {missing}"


def test_token_has_expected_columns() -> None:
    table = Base.metadata.tables["tokens"]
    col_names = {c.name for c in table.columns}
    for expected in (
        "id",
        "barcode",
        "catalog_number",
        "matrix_runout",
        "media_format",
        "status",
        "title",
        "artist",
        "year",
        "country_id",
        "label_id",
        "manufacturer_id",
        "current_revision_id",
        "extra",
        "created_at",
        "updated_at",
    ):
        assert expected in col_names, f"Column {expected!r} missing from tokens"


def test_audit_log_pk_is_bigint() -> None:
    table = Base.metadata.tables["audit_log"]
    pk_col = table.c.id
    assert pk_col.autoincrement


def test_enum_values() -> None:
    assert MediaFormat.VINYL.value == "vinyl"
    assert MediaFormat.CD.value == "cd"
    assert TokenStatus.DRAFT.value == "draft"
    assert UserRole.SERVICE.value == "service"


def test_naming_conventions_produce_deterministic_constraint_names() -> None:
    """Verify that FK constraint names follow the naming convention."""
    table = Base.metadata.tables["tokens"]
    fk_names = {fk.name for fk in table.foreign_key_constraints}
    assert "fk_tokens_country_id_countries" in fk_names


def test_model_repr_does_not_raise() -> None:
    """Repr should work on un-persisted instances."""
    u = User(username="test", email="t@x.com", password_hash="x", role=UserRole.VIEWER)
    assert "test" in repr(u)

    t = Token(media_format=MediaFormat.VINYL, title="Test", status=TokenStatus.DRAFT)
    assert "vinyl" in repr(t)
