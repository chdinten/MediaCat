"""SQLAlchemy 2.0 ORM models for MediaCat.

Tables
------
* **users** — application users (reviewers, admins).
* **tokens** — the core "token object" registry, one per unique release.
* **token_revisions** — append-only revision history per token.
* **media_objects** — images / page captures stored in MinIO.
* **ocr_artifacts** — OCR text extracted from media_objects.
* **labels** — record label reference table.
* **manufacturers** — manufacturer / pressing-plant reference table.
* **countries** — ISO 3166-1 country reference (seeds in migration).
* **ingestion_jobs** — background connector job tracking.
* **review_items** — human-review queue.
* **audit_log** — append-only action log for compliance / debugging.

Relationships
-------------
Tokens have many revisions. Each revision has provenance (source, user,
job). Media objects and OCR artifacts are linked to token revisions.
Labels and manufacturers are reference entities proposed by the pipeline
and confirmed by reviewers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mediacat.db.base import (
    AuditMixin,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from mediacat.db.enums import (
    ImageRegion,
    IngestionJobStatus,
    MediaFormat,
    OcrEngine,
    ReviewReason,
    ReviewStatus,
    RevisionSource,
    SymbolCategory,
    TokenStatus,
    UserRole,
)

# ---------------------------------------------------------------------------
# PostgreSQL enum type instances (shared across columns)
# ---------------------------------------------------------------------------
# values_callable ensures SQLAlchemy sends e.value ("merged") not e.name ("MERGED")
_ev = lambda e: [m.value for m in e]  # noqa: E731
_media_format_enum = ENUM(MediaFormat, name="media_format", create_type=False, values_callable=_ev)
_token_status_enum = ENUM(TokenStatus, name="token_status", create_type=False, values_callable=_ev)
_revision_source_enum = ENUM(
    RevisionSource, name="revision_source", create_type=False, values_callable=_ev
)
_review_status_enum = ENUM(
    ReviewStatus, name="review_status", create_type=False, values_callable=_ev
)
_review_reason_enum = ENUM(
    ReviewReason, name="review_reason", create_type=False, values_callable=_ev
)
_job_status_enum = ENUM(
    IngestionJobStatus, name="ingestion_job_status", create_type=False, values_callable=_ev
)
_ocr_engine_enum = ENUM(OcrEngine, name="ocr_engine", create_type=False, values_callable=_ev)
_image_region_enum = ENUM(ImageRegion, name="image_region", create_type=False, values_callable=_ev)
_user_role_enum = ENUM(UserRole, name="user_role", create_type=False, values_callable=_ev)
_symbol_category_enum = ENUM(
    SymbolCategory, name="symbol_category", create_type=False, values_callable=_ev
)


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════


class User(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Application user — reviewer, admin, or service account."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(_user_role_enum, nullable=False, default=UserRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mfa_secret: Mapped[str | None] = mapped_column(Text)  # TOTP secret, encrypted at rest
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    revisions: Mapped[list[TokenRevision]] = relationship(
        back_populates="author",
        foreign_keys="TokenRevision.created_by",
    )
    review_assignments: Mapped[list[ReviewItem]] = relationship(
        back_populates="assignee",
        foreign_keys="ReviewItem.assigned_to",
    )

    def __repr__(self) -> str:
        return f"<User {self.username!r} role={self.role.value}>"


# ═══════════════════════════════════════════════════════════════════════════
# Reference tables
# ═══════════════════════════════════════════════════════════════════════════


class Country(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """ISO 3166-1 country reference.  Seeded by migration."""

    __tablename__ = "countries"

    alpha2: Mapped[str] = mapped_column(String(2), unique=True, nullable=False)
    alpha3: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    numeric_code: Mapped[str | None] = mapped_column(String(3))

    def __repr__(self) -> str:
        return f"<Country {self.alpha2}>"


class Label(UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, SoftDeleteMixin, Base):
    """Record label reference entity.

    New entries are proposed by the pipeline and confirmed by a reviewer
    before they become canonical.
    """

    __tablename__ = "labels"

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    name_normalised: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="SET NULL")
    )
    discogs_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    musicbrainz_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    country: Mapped[Country | None] = relationship()

    __table_args__ = (
        Index(
            "ix_labels_trgm_name",
            "name_normalised",
            postgresql_using="gin",
            postgresql_ops={"name_normalised": "gin_trgm_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<Label {self.name!r}>"


class Manufacturer(UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, SoftDeleteMixin, Base):
    """Pressing plant / manufacturer reference entity."""

    __tablename__ = "manufacturers"

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    name_normalised: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="SET NULL")
    )
    plant_code: Mapped[str | None] = mapped_column(String(50))
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    country: Mapped[Country | None] = relationship()

    __table_args__ = (
        Index(
            "ix_manufacturers_trgm_name",
            "name_normalised",
            postgresql_using="gin",
            postgresql_ops={"name_normalised": "gin_trgm_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<Manufacturer {self.name!r}>"


# ═══════════════════════════════════════════════════════════════════════════
# Symbol registry
# ═══════════════════════════════════════════════════════════════════════════


class Symbol(UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, SoftDeleteMixin, Base):
    """Canonical entry for a graphical runout / dead-wax symbol.

    A symbol is identified by its immutable *slug* (e.g. ``emi-triangle``).
    Once confirmed, the slug is stored verbatim in JSONB parts arrays on every
    token that references this symbol — it must never change.

    Taxonomy levels stored in ``taxonomy_level``:
      1 = very common (EMI △, Capitol ☆ …)
      2 = common graphical (Porky, Decca shapes, PRS ▽ …)
      3 = regional/label-specific (Columbia plants, Allied, cutters …)
      4 = specialist/vintage (Western Electric, Lindström, JIS …)
      5 = one in a thousand (direct-cut marks, handwritten one-offs …)
    """

    __tablename__ = "symbols"

    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[SymbolCategory] = mapped_column(_symbol_category_enum, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    unicode_approx: Mapped[str | None] = mapped_column(String(20))
    taxonomy_level: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    region_scope: Mapped[str | None] = mapped_column(String(100))
    reference_image_key: Mapped[str | None] = mapped_column(String(500))
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    variants: Mapped[list[SymbolVariant]] = relationship(back_populates="symbol")
    token_symbols: Mapped[list[TokenSymbol]] = relationship(back_populates="symbol")

    __table_args__ = (Index("ix_symbols_category_level", "category", "taxonomy_level"),)

    def __repr__(self) -> str:
        return f"<Symbol {self.slug!r} {self.category.value}>"


class SymbolVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A known visual variant of a canonical :class:`Symbol`.

    Different pressings or plants may use slightly different drawings of the
    same symbol.  Variants share the parent slug but carry their own reference
    image and notes.
    """

    __tablename__ = "symbol_variants"

    symbol_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_key: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    reference_image_key: Mapped[str | None] = mapped_column(String(500))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    symbol: Mapped[Symbol] = relationship(back_populates="variants")

    __table_args__ = (UniqueConstraint("symbol_id", "variant_key", name="uq_symbol_variant_key"),)

    def __repr__(self) -> str:
        return f"<SymbolVariant {self.variant_key!r} of symbol={self.symbol_id}>"


# ═══════════════════════════════════════════════════════════════════════════
# Token objects & revisions
# ═══════════════════════════════════════════════════════════════════════════


class Token(UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, SoftDeleteMixin, Base):
    """Core token object — one per unique physical release.

    Tokens are the unit of identity.  Every piece of information about a
    release is attached as a *revision* (append-only).  The token row
    itself carries only denormalised "current" pointers for fast lookup.
    """

    __tablename__ = "tokens"

    # Natural-key candidates (all nullable; some releases lack barcodes etc.)
    barcode: Mapped[str | None] = mapped_column(String(50), index=True)
    catalog_number: Mapped[str | None] = mapped_column(String(100), index=True)
    matrix_runout: Mapped[str | None] = mapped_column(String(500))

    # Side-B plain text (side A stored in matrix_runout for back-compat)
    matrix_runout_b: Mapped[str | None] = mapped_column(Text)
    # Structured parts arrays — authoritative form once symbols are resolved.
    # Each element is {"t":"text","v":"…"} or {"t":"sym","slug":"…","id":"uuid"}.
    matrix_runout_parts: Mapped[list[dict] | None] = mapped_column(JSONB)
    matrix_runout_b_parts: Mapped[list[dict] | None] = mapped_column(JSONB)
    # Structured parsed breakdown — each key maps to {"value", "confidence", "source"}.
    # source: "vision" | "human" | "import" | "rule" | null
    matrix_runout_parsed: Mapped[dict | None] = mapped_column(JSONB)
    matrix_runout_b_parsed: Mapped[dict | None] = mapped_column(JSONB)

    media_format: Mapped[MediaFormat] = mapped_column(_media_format_enum, nullable=False)
    status: Mapped[TokenStatus] = mapped_column(
        _token_status_enum, nullable=False, default=TokenStatus.DRAFT
    )

    # Denormalised "current" fields (updated when a new revision is approved).
    title: Mapped[str | None] = mapped_column(String(500))
    artist: Mapped[str | None] = mapped_column(String(500))
    year: Mapped[int | None] = mapped_column(Integer)
    country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="SET NULL")
    )
    label_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("labels.id", ondelete="SET NULL")
    )
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("manufacturers.id", ondelete="SET NULL")
    )

    # External IDs
    discogs_release_id: Mapped[int | None] = mapped_column(Integer, index=True)
    discogs_master_id: Mapped[int | None] = mapped_column(Integer)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(36), index=True)

    # Current revision pointer (denormalised for quick access)
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("token_revisions.id", ondelete="SET NULL", use_alter=True)
    )

    # Full-text + JSONB blob for flexible search
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Relationships
    country: Mapped[Country | None] = relationship()
    label: Mapped[Label | None] = relationship()
    manufacturer: Mapped[Manufacturer | None] = relationship()
    revisions: Mapped[list[TokenRevision]] = relationship(
        back_populates="token",
        foreign_keys="TokenRevision.token_id",
        order_by="TokenRevision.revision_number",
    )
    media_objects: Mapped[list[MediaObject]] = relationship(back_populates="token")
    review_items: Mapped[list[ReviewItem]] = relationship(back_populates="token")
    token_symbols: Mapped[list[TokenSymbol]] = relationship(back_populates="token")

    __table_args__ = (
        Index(
            "ix_tokens_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_tokens_artist_trgm",
            "artist",
            postgresql_using="gin",
            postgresql_ops={"artist": "gin_trgm_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<Token {self.id} {self.media_format.value} {self.title!r}>"


class TokenRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only revision of a token's attribute snapshot.

    Every change — whether from ingestion, OCR, vision, or human review —
    creates a new revision.  The ``data`` JSONB column holds the complete
    attribute set at that point in time, making diffs trivial.
    """

    __tablename__ = "token_revisions"

    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[RevisionSource] = mapped_column(_revision_source_enum, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # delta from prev revision
    comment: Mapped[str | None] = mapped_column(Text)

    # Provenance
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ingestion_jobs.id", ondelete="SET NULL")
    )
    confidence: Mapped[float | None] = mapped_column(Float)

    # Relationships
    token: Mapped[Token] = relationship(back_populates="revisions", foreign_keys=[token_id])
    author: Mapped[User | None] = relationship(
        back_populates="revisions", foreign_keys=[created_by]
    )

    __table_args__ = (
        UniqueConstraint("token_id", "revision_number", name="uq_token_revision_number"),
    )

    def __repr__(self) -> str:
        return f"<TokenRevision token={self.token_id} rev={self.revision_number}>"


# ═══════════════════════════════════════════════════════════════════════════
# Media objects & OCR
# ═══════════════════════════════════════════════════════════════════════════


class MediaObject(UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, Base):
    """An image or page capture stored in MinIO.

    The ``content_hash`` (SHA-256 of the raw bytes) is used as the
    object-store key for deduplication.
    """

    __tablename__ = "media_objects"

    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    region: Mapped[ImageRegion | None] = mapped_column(_image_region_enum)
    is_primary_cover: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    source_url: Mapped[str | None] = mapped_column(Text)  # where it was fetched from
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    token: Mapped[Token] = relationship(back_populates="media_objects")
    ocr_artifacts: Mapped[list[OcrArtifact]] = relationship(back_populates="media_object")

    __table_args__ = (UniqueConstraint("bucket", "object_key", name="uq_media_bucket_key"),)

    def __repr__(self) -> str:
        return f"<MediaObject {self.content_hash[:12]}… {self.mime_type}>"


class OcrArtifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """OCR text extracted from a :class:`MediaObject`.

    Stores the raw text in the original detected language and the
    translated text in British English.
    """

    __tablename__ = "ocr_artifacts"

    media_object_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("media_objects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    engine: Mapped[OcrEngine] = mapped_column(_ocr_engine_enum, nullable=False)
    region: Mapped[ImageRegion | None] = mapped_column(_image_region_enum)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    detected_language: Mapped[str | None] = mapped_column(String(10))  # BCP-47
    translated_text: Mapped[str | None] = mapped_column(Text)  # en-GB
    confidence: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    # Vision-proposed symbol matches: [{"slug":"…","confidence":0.9,"bbox":[…]}]
    symbol_candidates: Mapped[list[dict] | None] = mapped_column(JSONB)

    media_object: Mapped[MediaObject] = relationship(back_populates="ocr_artifacts")

    def __repr__(self) -> str:
        return f"<OcrArtifact engine={self.engine.value} lang={self.detected_language}>"


# ═══════════════════════════════════════════════════════════════════════════
# Ingestion jobs
# ═══════════════════════════════════════════════════════════════════════════


class IngestionJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A background ingestion task tracked for observability and retry."""

    __tablename__ = "ingestion_jobs"

    connector_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[IngestionJobStatus] = mapped_column(
        _job_status_enum, nullable=False, default=IngestionJobStatus.QUEUED
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    def __repr__(self) -> str:
        return f"<IngestionJob {self.connector_name} {self.status.value}>"


# ═══════════════════════════════════════════════════════════════════════════
# Review queue
# ═══════════════════════════════════════════════════════════════════════════


class ReviewItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An item flagged for human review.

    Created automatically when the pipeline's confidence is low or when
    sources conflict; can also be raised manually by a reviewer.
    """

    __tablename__ = "review_items"

    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("token_revisions.id", ondelete="SET NULL")
    )
    status: Mapped[ReviewStatus] = mapped_column(
        _review_status_enum, nullable=False, default=ReviewStatus.PENDING
    )
    reason: Mapped[ReviewReason] = mapped_column(_review_reason_enum, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_comment: Mapped[str | None] = mapped_column(Text)

    token: Mapped[Token] = relationship(back_populates="review_items")
    assignee: Mapped[User | None] = relationship(
        back_populates="review_assignments", foreign_keys=[assigned_to]
    )

    __table_args__ = (
        Index(
            "ix_review_pending",
            "status",
            "priority",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<ReviewItem token={self.token_id} {self.status.value}>"


# ═══════════════════════════════════════════════════════════════════════════
# Audit log
# ═══════════════════════════════════════════════════════════════════════════


class AuditLog(Base):
    """Append-only action log.

    No UUIDPrimaryKeyMixin — uses a BIGINT PK for write performance.
    No ``updated_at`` — rows are never updated.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(45))  # IPv6 max
    request_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_timestamp", "timestamp"),
        Index("ix_audit_user", "user_id"),
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity_type}:{self.entity_id}>"


# DEF-003: ORM-level guard — audit_log rows are immutable.
# The DB already REVOKEs UPDATE/DELETE from mediacat_app (migration 0001).
# These listeners provide defence in depth at the application layer so any
# accidental UPDATE or DELETE is caught immediately in Python, not at the
# database permission check.
@event.listens_for(AuditLog, "before_update")
def _audit_log_no_update(_mapper: object, _connection: object, target: AuditLog) -> None:
    raise RuntimeError(
        f"AuditLog rows are immutable — UPDATE attempted on id={target.id!r}. "
        "Write a new entry instead."
    )


@event.listens_for(AuditLog, "before_delete")
def _audit_log_no_delete(_mapper: object, _connection: object, target: AuditLog) -> None:
    raise RuntimeError(
        f"AuditLog rows are immutable — DELETE attempted on id={target.id!r}. "
        "Audit rows must never be removed."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Symbol ↔ Token index
# ═══════════════════════════════════════════════════════════════════════════


class TokenSymbol(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Fast FK index: which symbols appear in a token's runout, and where.

    Derived from ``Token.matrix_runout_parts`` / ``matrix_runout_b_parts``.
    Allows indexed joins without scanning JSONB arrays on every query.
    ``side`` is ``"a"`` or ``"b"``; ``position`` is 0-based within that array.
    """

    __tablename__ = "token_symbols"

    token_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(1), nullable=False, default="a")

    token: Mapped[Token] = relationship(back_populates="token_symbols")
    symbol: Mapped[Symbol] = relationship(back_populates="token_symbols")

    __table_args__ = (
        UniqueConstraint(
            "token_id",
            "symbol_id",
            "position",
            "side",
            name="uq_token_symbol_pos",
        ),
        Index("ix_token_symbols_symbol_id", "symbol_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<TokenSymbol token={self.token_id} symbol={self.symbol_id}"
            f" side={self.side} pos={self.position}>"
        )
