# Section 3 — Database schema & migrations

## What this section produces

- `src/mediacat/db/enums.py` — 9 `StrEnum` types mapped to Postgres enums:
  `MediaFormat`, `TokenStatus`, `RevisionSource`, `ReviewStatus`,
  `ReviewReason`, `IngestionJobStatus`, `OcrEngine`, `ImageRegion`, `UserRole`.
- `src/mediacat/db/base.py` — `DeclarativeBase` with naming conventions,
  `UUIDPrimaryKeyMixin`, `TimestampMixin`, `AuditMixin`, `SoftDeleteMixin`.
- `src/mediacat/db/models.py` — 11 ORM models (see table list below).
- `src/mediacat/db/engine.py` — async engine factory, session maker,
  `transactional_session` context manager.
- `alembic/` — Alembic configuration with async env.py, migration template,
  initial migration `0001_initial_schema.py`.
- `deploy/initdb/01-roles.sql` — least-privilege DB roles (`mediacat_migrator`,
  `mediacat_app`, `mediacat_readonly`), default privileges, required
  extensions (`uuid-ossp`, `pg_trgm`, `btree_gist`).

## Tables

| Table               | Purpose                                      | PK type  |
|---------------------|----------------------------------------------|----------|
| `users`             | Application users                            | UUID     |
| `countries`         | ISO 3166-1 reference (seeded)                | UUID     |
| `labels`            | Record label reference (confirmed by review) | UUID     |
| `manufacturers`     | Pressing plant reference                     | UUID     |
| `tokens`            | Core token-object registry                   | UUID     |
| `token_revisions`   | Append-only revision history per token       | UUID     |
| `media_objects`     | Images stored in MinIO                       | UUID     |
| `ocr_artifacts`     | OCR text per media_object                    | UUID     |
| `ingestion_jobs`    | Background job tracking                      | UUID     |
| `review_items`      | Human review queue                           | UUID     |
| `audit_log`         | Append-only action log                       | BIGINT   |

## Key design choices

- **UUID PKs everywhere** (except audit_log) — no auto-increment leakage,
  safe for distributed generation.
- **Trigram indexes** (GIN, `pg_trgm`) on `tokens.title`, `tokens.artist`,
  `labels.name_normalised`, `manufacturers.name_normalised` for fuzzy search
  and candidate matching (Section 8).
- **JSONB** on `token_revisions.data` — the full attribute snapshot at each
  revision. Diffs are stored alongside for fast comparison in the review UI.
- **Partial index** on `review_items` filtering `status = 'pending'` — the
  most common query path.
- **Three DB roles** with default privileges so the app role can never run
  DDL and the readonly role can never mutate data.

## Running migrations

```bash
# Inside the app or migrator container:
alembic upgrade head

# Generate a new migration after model changes:
alembic revision --autogenerate -m "describe change"
```
