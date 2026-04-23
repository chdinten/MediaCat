"""PostgreSQL-backed enum types for the MediaCat domain.

Each Python enum maps 1:1 to a Postgres ``CREATE TYPE … AS ENUM`` via
SQLAlchemy's :class:`~sqlalchemy.dialects.postgresql.ENUM`.  New members
are added through Alembic migrations (``ALTER TYPE … ADD VALUE``).
"""

from __future__ import annotations

import enum


class MediaFormat(enum.StrEnum):
    """Physical media type.  First generation: vinyl + CD."""

    VINYL = "vinyl"
    CD = "cd"


class TokenStatus(enum.StrEnum):
    """Lifecycle state of a token object."""

    DRAFT = "draft"
    ACTIVE = "active"
    MERGED = "merged"  # duplicate resolved into another token
    ARCHIVED = "archived"


class RevisionSource(enum.StrEnum):
    """How a revision was created."""

    INGESTION = "ingestion"  # automated connector
    VISION = "vision"  # vision-model transcription
    OCR = "ocr"  # OCR pipeline
    HUMAN = "human"  # reviewer via the UI
    LLM = "llm"  # LLM comparison / anomaly pass
    IMPORT = "import"  # bulk import


class ReviewStatus(enum.StrEnum):
    """State of a review-queue item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class ReviewReason(enum.StrEnum):
    """Why an item was queued for human review."""

    LOW_CONFIDENCE = "low_confidence"
    CONFLICT = "conflict"  # multiple sources disagree
    NOVEL_ENTITY = "novel_entity"  # proposed new label / plant / etc.
    ANOMALY = "anomaly"  # LLM flagged
    MANUAL = "manual"  # human-triggered re-review


class IngestionJobStatus(enum.StrEnum):
    """Lifecycle of a background ingestion job."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OcrEngine(enum.StrEnum):
    """Which OCR backend produced the text."""

    TESSERACT = "tesseract"
    AZURE = "azure"
    AWS_TEXTRACT = "aws_textract"
    MANUAL = "manual"


class ImageRegion(enum.StrEnum):
    """Named region of a media image."""

    LABEL_A = "label_a"
    LABEL_B = "label_b"
    OBI_FRONT = "obi_front"
    OBI_BACK = "obi_back"
    OBI_SPINE = "obi_spine"
    RUNOUT_A = "runout_a"
    RUNOUT_B = "runout_b"
    MATRIX = "matrix"
    COVER_FRONT = "cover_front"
    COVER_BACK = "cover_back"
    SLEEVE_INNER = "sleeve_inner"
    DISC_SURFACE = "disc_surface"
    OTHER = "other"


class UserRole(enum.StrEnum):
    """Application-level roles (not DB roles)."""

    ADMIN = "admin"
    REVIEWER = "reviewer"
    VIEWER = "viewer"
    SERVICE = "service"  # machine-to-machine


class SymbolCategory(enum.StrEnum):
    """Classification of a graphical runout / dead-wax symbol.

    Taxonomy levels (1 = very common, 5 = one in a thousand):

    Level 1 — Core text content (always handled as plain text):
      Plain matrix numbers, side designators (A/B), stamper codes,
      pressing-plant text codes (EMI, CBS, PRS…), engineer initials.

    Level 2 — Common graphical symbols (seed data):
      EMI pressing triangle (△), Decca/London circle variants, Porky/Pecko
      text marks (George Peckham), Pye Studios engineer triangles (△M…),
      PRS ▽ prefix codes, US Capitol stars (☆/✲/✹), US Decca/MCA shapes
      (✤=Gloversville, ◆=Pinckneyville, ◈=Richmond), Sheffield Lab △####,
      Sonic Arts logo (▭◯▭).

    Level 3 — Regional / label-specific (import from reference data):
      Monarch MR-in-circle, Columbia plant codes (Ƨ/T/P/G…), Capitol plant
      variants (0/()/-◁/IAM△), Allied Record (a/Q), Sterling Sound stamp,
      Masterdisk stamp, Wakefield tulip, personal cutter marks (BAZZA,
      BilBo, Rasputin, RAZEL / Ray Staff aliases).

    Level 4 — Specialist / Vintage (rare but identifiable):
      Western Electric system marks (Blumlein □, ◇ for 1C/1D), Lindström
      (£/ℒ), Japanese JIS (〄), Nigerian/African plant marks (RMNL/FIS),
      Jamaican studio marks (DSR/RRS/Dynamic Sounds), shellac-era cutting
      systems, Hub-Servall and Research Craft patent marks.

    Level 5 — Edge cases (one in a thousand):
      Direct-cut / direct-to-disc indicators, unique handwritten engineer
      symbols (⋊ Anne-Marie Suenram, spiral @ D+M), test-pressing one-offs,
      extremely obscure regional marks (Druco Music ↽N/N⇀ Belgian).
    """

    PRESSING_PLANT_MARK = "pressing_plant_mark"  # EMI △, Capitol ☆, Decca ◆ …
    ENGINEER_MARK = "engineer_mark"              # PORKY, △M (Malcolm Davies) …
    LABEL_LOGO = "label_logo"                    # small trademark stamped in wax
    CUT_TYPE = "cut_type"                        # lacquer-cut system (WE, Lindström)
    CERTIFICATION = "certification"              # PRS ▽, copyright marks
    OTHER = "other"
