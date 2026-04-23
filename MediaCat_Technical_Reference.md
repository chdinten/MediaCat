# MediaCat — Technical Reference & User Guide

**Version 0.1 · April 2026 · CONFIDENTIAL**

> Cataloguing platform for vinyl records and compact discs.
> Python 3.12 · FastAPI · PostgreSQL 16 · MinIO · Redis · OPA

---

## Table of Contents

1. [Project Overview & Architecture](#1-project-overview--architecture)
2. [Installation — Windows WSL2](#2-installation--windows-wsl2)
3. [Database Schema](#3-database-schema)
4. [Vision & OCR Pipeline](#4-vision--ocr-pipeline)
5. [Symbol Registry](#5-symbol-registry)
6. [Ingestion Connectors](#6-ingestion-connectors)
7. [Review Queue](#7-review-queue)
8. [Authentication & Security](#8-authentication--security)
9. [Object Storage (MinIO)](#9-object-storage-minio)
10. [Configuration Reference](#10-configuration-reference)
11. [API Endpoints](#11-api-endpoints)
12. [Alembic Migrations](#12-alembic-migrations)
13. [Make Targets](#13-make-targets)
14. [Open Items & Roadmap](#14-open-items--roadmap)
15. [Usage Guide](#15-usage-guide)

---

## 1 · Project Overview & Architecture

### 1.1 What Is MediaCat?

MediaCat is a hybrid-AI cataloguing platform for physical music media — vinyl records and compact discs. It ingests metadata and cover/runout photography from external sources (Discogs, MusicBrainz, CoverArtArchive), enriches records with OCR and vision-model transcription, and stores everything in an append-only token-object registry with mandatory human review gates. No AI decision is ever applied automatically: **LLMs propose, humans approve**.

### 1.2 Technology Stack

| Layer | Technology | Version / Notes |
|---|---|---|
| Language | Python | 3.12, async/await throughout |
| Web Framework | FastAPI + Jinja2 + HTMX | Server-side rendering, no heavy JS framework |
| Database | PostgreSQL | 16, pg_trgm + uuid-ossp + btree_gist |
| ORM / Migrations | SQLAlchemy 2 + Alembic | Async ORM, deterministic migration names |
| Object Storage | MinIO | S3-compatible, content-hash dedup |
| Job Queue | Redis 7 | BLMOVE atomic dequeue, stale-job reaper |
| Rule Engine | Open Policy Agent (OPA) | Rego policies + Python fallback |
| Vision / LLM | Ollama (local-first) | LLaVA / Qwen2-VL; Anthropic API fallback |
| Reverse Proxy | Caddy | Automatic TLS (Let's Encrypt) |
| Deployment | Docker Compose on WSL2 | 8-service stack; host-mounted data volumes |
| Auth / Security | Argon2id + TOTP + OPA | MFA scaffold; CSP; X-CSRF-Token header |

### 1.3 Architectural Principles

- **Advisory-only AI** — LLMs and vision models propose updates; humans always confirm.
- **Append-only revisions** — Every change creates a new `TokenRevision` row; nothing is overwritten.
- **Content-addressed storage** — Images are keyed by SHA-256 hash in MinIO — duplicates are free.
- **Data / code separation** — All persistent state lives on host-mounted volumes; containers are ephemeral.
- **Least-privilege DB roles** — The app role cannot ALTER, DROP, or write the audit log.
- **Hybrid AI resilience** — Local Ollama handles 99% of traffic; API fallback fires automatically on failure.

### 1.4 Repository Layout

```
mediacat/src/mediacat/       Python package root
  db/                        ORM models, enums, base, engine, symbol helpers
  web/                       FastAPI app, routes, auth, middleware, templates
  vision/                    VLM adapter, task prompts, candidate matcher
  llm/                       LLM adapter, tasks, safety, Ollama & Anthropic backends
  ingestion/                 Connector base, Discogs, MusicBrainz, Redis queue, drift
  rules/                     OPA adapter, local Python fallback
  storage/                   MinIO wrapper, image pipeline, OCR, translation
mediacat/alembic/versions/   Sequential migrations (0001 initial, 0002 symbols)
mediacat/deploy/             Docker Compose, Caddyfile, OPA bundles, backup scripts
mediacat/config/             app.yaml + connectors.yaml (host-mounted)
mediacat/docs/               ADRs, section docs, due-diligence report
mediacat/tests/              Pytest suite (models, vision, ingestion, web, storage…)
```

---

## 2 · Installation — Windows WSL2

### 2.1 Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Windows 11 (or 10 21H2+) | — | WSL2 kernel included |
| WSL2 — Ubuntu 24.04 LTS | 24.04 | `wsl --install -d Ubuntu-24.04` |
| Docker Desktop for Windows | 4.28+ | Enable WSL2 backend; allocate ≥ 8 GB RAM |
| Git | 2.40+ | Pre-installed in Ubuntu 24.04 |
| GNU Make | 4.3+ | `sudo apt-get install make` |
| uv (Python package manager) | 0.4+ | Installed automatically by `make setup` |

### 2.2 Step-by-Step Installation

#### Step 1 — Enable WSL2 & Install Ubuntu

Open PowerShell as Administrator and run:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart when prompted, then set a UNIX username and password.

#### Step 2 — Install Docker Desktop

Download Docker Desktop from docker.com/products/docker-desktop.
In **Settings → Resources → WSL Integration**, enable your Ubuntu distro.

#### Step 3 — Clone the Repository

Inside the Ubuntu terminal:

```bash
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/chdinten/MediaCat.git sounddb
cd sounddb/mediacat
```

#### Step 4 — Bootstrap the System

```bash
make bootstrap
```

This requires sudo — installs build tools, Tesseract OCR, and system dependencies.

#### Step 5 — Create the Data Directory Tree

```bash
make data-init
```

Default root: `~/data/mediacat` (override with `MEDIACAT_DATA_ROOT`).

#### Step 6 — Configure Secrets

```bash
cp .env.example .env
nano .env
```

Create secret files in `~/data/mediacat/secrets/`:

```
postgres_app_password
minio_root_password
redis_password
session_secret
```

Each file should contain only the secret value with no trailing newline.

#### Step 7 — Set Up the Python Environment

```bash
make setup
```

Uses `uv` to create a virtualenv at `.venv/` and installs all packages from `pyproject.toml` including dev extras.

#### Step 8 — Start the Stack

```bash
make up
```

Starts all 8 Docker services: Caddy, PostgreSQL 16, MinIO, Redis 7, OPA, app, worker, backup.

#### Step 9 — Run Migrations

```bash
docker compose exec app alembic upgrade head
```

Creates all tables, enums, indexes, and seeds symbol data.

#### Step 10 — Verify the Installation

```bash
curl http://localhost/healthz
```

Open a browser at `http://localhost` and log in with the dev admin credentials set in `.env`.

### 2.3 Useful Docker Commands

| Command | Description |
|---|---|
| `make up` | Start all services in the background |
| `make down` | Stop and remove containers (data volumes preserved) |
| `make restart` | Restart the stack |
| `make logs` | Tail logs from all services |
| `make ps` | Show container status |
| `make config-check` | Validate docker-compose configuration |
| `docker compose exec app bash` | Open a shell inside the app container |

### 2.4 Running Tests & Quality Gates

| Command | Description |
|---|---|
| `make lint` | Ruff linter — checks code style and imports |
| `make format` | Ruff formatter — auto-fixes formatting |
| `make typecheck` | Mypy strict — full static type checking |
| `make security` | Bandit + pip-audit — security scan |
| `make test` | Full pytest suite with coverage report |
| `make test-fast` | Pytest excluding slow / integration tests |

---

## 3 · Database Schema

PostgreSQL 16 is the primary data store. All tables use UUID primary keys (server-generated via `uuid_generate_v4()`). Timestamps are stored in UTC with timezone. The schema is managed exclusively via Alembic migrations — never modify the database schema by hand.

### 3.1 Entity Relationship Overview

- **Token** — the unit of identity (one per unique physical release)
- **TokenRevision** — append-only history; every change creates a new revision
- **MediaObject** — image stored in MinIO, linked to a token
- **OcrArtifact** — OCR text extracted from a MediaObject
- **Symbol** — canonical graphical runout mark (e.g. EMI triangle)
- **SymbolVariant** — visual variant of a canonical Symbol
- **TokenSymbol** — FK index: which symbols appear in a token's runout
- **ReviewItem** — proposed change awaiting human approval
- **Label / Manufacturer / Country** — reference entities (confirmed by reviewers)
- **IngestionJob** — background connector job tracking
- **AuditLog** — append-only immutable action trail
- **User** — application user with role-based access

### 3.2 Core Table Reference

#### users
Application users — reviewers, admins, service accounts.

`id · username · email · password_hash (Argon2id) · role · is_active · mfa_secret · failed_login_count · locked_until`

#### tokens
Core token object — one row per unique physical release.

`id · barcode · catalog_number · matrix_runout (plain text, side A) · matrix_runout_b · matrix_runout_parts (JSONB) · matrix_runout_b_parts (JSONB) · media_format · status · title · artist · year · country_id · label_id · manufacturer_id · discogs_release_id · musicbrainz_release_id · current_revision_id · extra (JSONB)`

#### token_revisions
Append-only revision log; stores complete attribute snapshot.

`id · token_id · revision_number · source · data (JSONB) · diff (JSONB) · confidence · created_by · ingestion_job_id`

#### media_objects
Images stored in MinIO; keyed by SHA-256 content hash.

`id · token_id · content_hash · bucket · object_key · mime_type · size_bytes · width_px · height_px · region · source_url · metadata (JSONB)`

#### ocr_artifacts
OCR text per image region; includes symbol candidates.

`id · media_object_id · engine · region · raw_text · detected_language · translated_text · confidence · symbol_candidates (JSONB) · metadata (JSONB)`

#### symbols *(added in migration 0002)*
Canonical graphical runout / dead-wax symbol registry.

`id · slug (immutable) · name · category · description · unicode_approx · taxonomy_level (1–5) · region_scope · is_confirmed · metadata (JSONB)`

#### symbol_variants *(added in migration 0002)*
Visual variants of a canonical symbol.

`id · symbol_id · variant_key · description · reference_image_key`

#### token_symbols *(added in migration 0002)*
FK index: symbol ↔ token position for fast joins.

`id · token_id · symbol_id · position · side (a|b)`

#### labels
Record label reference; confirmed by human reviewer.

`id · name · name_normalised · country_id · discogs_id · musicbrainz_id · is_confirmed · metadata (JSONB)`

#### manufacturers
Pressing plant / manufacturer reference.

`id · name · name_normalised · country_id · plant_code · is_confirmed`

#### countries
ISO 3166-1 country seed data.

`id · alpha2 · alpha3 · name · numeric_code`

#### ingestion_jobs
Background connector job tracking.

`id · connector_name · status · payload (JSONB) · result (JSONB) · error_message · attempt_count · started_at · completed_at`

#### review_items
Human-review queue; all AI proposals land here first.

`id · token_id · revision_id · status · reason · priority · details (JSONB) · assigned_to · resolved_at · resolution_comment`

#### audit_log
Immutable append-only action log (BIGINT PK, no updates or deletes).

`id · timestamp · user_id · action · entity_type · entity_id · detail (JSONB) · ip_address · request_id`

### 3.3 Enumerations

| Enum Name | Values |
|---|---|
| `media_format` | `vinyl` · `cd` |
| `token_status` | `draft` · `active` · `merged` · `archived` |
| `revision_source` | `ingestion` · `vision` · `ocr` · `human` · `llm` · `import` |
| `review_status` | `pending` · `in_progress` · `approved` · `rejected` · `deferred` |
| `review_reason` | `low_confidence` · `conflict` · `novel_entity` · `anomaly` · `manual` |
| `ingestion_job_status` | `queued` · `running` · `completed` · `failed` · `cancelled` |
| `ocr_engine` | `tesseract` · `azure` · `aws_textract` · `manual` |
| `image_region` | `label_a` · `label_b` · `obi_front/back/spine` · `runout_a/b` · `matrix` · `cover_front/back` · `sleeve_inner` · `disc_surface` · `other` |
| `user_role` | `admin` · `reviewer` · `viewer` · `service` |
| `symbol_category` | `pressing_plant_mark` · `engineer_mark` · `label_logo` · `cut_type` · `certification` · `other` |

---

## 4 · Vision & OCR Pipeline

### 4.1 Pipeline Overview

| Step | Stage | Detail |
|---|---|---|
| 1 | Image arrives | Uploaded by user or fetched from Discogs / CoverArtArchive |
| 2 | MinIO store | SHA-256 hash computed → deduplicated upload to MinIO bucket |
| 3 | OCR extraction | Tesseract (or cloud: Azure / AWS Textract) per image region |
| 4 | Translation | OCR text translated to British English via local LLM |
| 5 | Vision transcription | HybridVision calls Ollama (LLaVA / Qwen2-VL) with task prompt |
| 6 | JSON parsing | Structured response validated against expected schema |
| 7 | Candidate matching | Trigram search + exact match against token table |
| 8 | Review queue | Results written to review_items — never auto-applied |

### 4.2 Vision Backends

- **OllamaVisionBackend (primary)** — Sends base64-encoded image + prompt to `http://ollama:11434/api/chat`. Models: LLaVA 1.6, Qwen2-VL. Timeout: 120 s. Runs entirely on-premises.
- **AnthropicVisionBackend (fallback)** — Uses the Anthropic Messages API (claude-3-5-sonnet or configured model). Requires `ANTHROPIC_API_KEY`. Invoked only when Ollama fails or confidence is too low.

### 4.3 Prompt Templates

| Function | Region | Key Output Fields |
|---|---|---|
| `label_prompt()` | label_a / label_b | label_name, catalog_number, artist, title, side, speed_rpm, country, year |
| `obi_prompt()` | obi_front / back / spine | japanese_title, romanised_title, english_title, catalog_number, price, obi_type |
| `runout_prompt()` | runout_a / runout_b / matrix | matrix_number, stamper_code, sid_codes, lacquer_cut_info, pressing_plant_hint, symbol_detections |
| `symbol_identification_prompt()` | runout (re-run) | symbols[] with slug_suggestion, unicode_approx, description, application, confidence |

### 4.4 Symbol Detections in Runout Output

When the vision model encounters a non-alphanumeric graphical mark in the dead-wax area, it emits a `symbol_detections` entry rather than embedding the mark as plain text:

```json
{
  "slug_suggestion": "emi-triangle",
  "unicode_approx": "△",
  "description": "Upward triangle stamped into wax, ~3 mm",
  "application": "stamped",
  "confidence": 0.92
}
```

> **Design invariant:** Vision proposals are advisory only. All detections are written to the review queue; they are never applied to the token or symbol tables without explicit human approval.

---

## 5 · Symbol Registry

### 5.1 Purpose

Runout / dead-wax inscriptions on vinyl records mix plain text with graphical symbols — pressed triangles, stamped stars, etched circles — that identify pressing plants, mastering engineers, label certifications, and cutting systems. The symbol registry gives each mark a stable, human-readable slug and enables indexed queries without scanning JSONB arrays.

### 5.2 Taxonomy — Five Levels of Rarity

| Level | Frequency | Description | Examples |
|---|---|---|---|
| 1 | Very common | Core text content handled as plain text | Matrix numbers (XZAL-9067), side codes (A/B), stamper codes (1A), basic plant text (EMI, CBS) |
| 2 | Common graphical symbols — seeded at install | Appears on thousands of records | EMI △ (UK), PRS ▽ (UK), Capitol ☆ (US), Decca ◆/◈/✤ (US), Porky/Pecko marks, Pye Studios △M |
| 3 | Regional / label-specific | Familiar to specialists; import from reference data | Columbia plant codes (Ƨ/T/P/G), Allied (a/Q), Sterling Sound stamp, Masterdisk stamp, Wakefield tulip |
| 4 | Specialist / Vintage | Rare but identifiable to experts | Western Electric □/◇, Lindström £/ℒ, Japanese JIS 〄, Nigerian/Jamaican plant marks |
| 5 | Edge cases | One in a thousand — entered manually | Direct-cut indicators, unique handwritten engineer marks, test-pressing one-offs |

### 5.3 Seeded Symbols (Levels 2–4)

| Slug | Name | Category | Region | Level |
|---|---|---|---|---|
| `emi-triangle` | EMI Pressing Triangle | pressing_plant_mark | UK | 2 |
| `prs-triangle-down` | PRS Downward Triangle ▽ | certification | UK | 2 |
| `pye-triangle` | Pye Studios Engineer Triangle | engineer_mark | UK | 2 |
| `porky-prime-cut` | Porky Prime Cut | engineer_mark | UK | 2 |
| `pecko-duck` | Pecko Duck (alt. Peckham mark) | engineer_mark | UK | 2 |
| `decca-circle` | Decca / London Circle | pressing_plant_mark | UK | 2 |
| `sonic-arts-logo` | Sonic Arts Logo ▭◯▭ | label_logo | UK | 2 |
| `capitol-la-star` | Capitol Los Angeles Star ☆ | pressing_plant_mark | US | 2 |
| `decca-us-gloversville` | MCA/Decca Gloversville ✤ | pressing_plant_mark | US | 2 |
| `decca-us-pinckneyville` | MCA/Decca Pinckneyville ◆ | pressing_plant_mark | US | 2 |
| `decca-us-richmond` | MCA/Decca Richmond ◈ | pressing_plant_mark | US | 2 |
| `sheffield-lab-delta` | Sheffield Lab △#### | pressing_plant_mark | US | 2 |
| `sterling-sound` | Sterling Sound stamp | engineer_mark | US | 3 |
| `masterdisk` | Masterdisk stamp | engineer_mark | US | 3 |
| `columbia-santa-maria` | Columbia Santa Maria (Ƨ) | pressing_plant_mark | US | 3 |
| `columbia-terre-haute` | Columbia Terre Haute (T/CT/CTH) | pressing_plant_mark | US | 3 |
| `columbia-pitman` | Columbia Pitman (P) | pressing_plant_mark | US | 3 |
| `columbia-carrollton` | Columbia Carrollton (G/G1) | pressing_plant_mark | US | 3 |
| `capitol-jacksonville` | Capitol Jacksonville (0/()) | pressing_plant_mark | US | 3 |
| `capitol-winchester` | Capitol Winchester (—◁) | pressing_plant_mark | US | 3 |
| `capitol-scranton-iam` | Capitol Scranton (IAM △) | pressing_plant_mark | US | 3 |
| `allied-record-a` | Allied Record (a/Q) | pressing_plant_mark | US | 3 |
| `wakefield-tulip` | Wakefield Manufacturing tulip | pressing_plant_mark | US | 3 |
| `western-electric-blumlein-square` | WE Blumlein Square □ | cut_type | — | 4 |
| `western-electric-diamond` | WE Diamond ◇ (1C/1D) | cut_type | — | 4 |
| `lindstrom-pound` | Lindström System £/ℒ | cut_type | Europe | 4 |
| `japanese-jis` | Japanese JIS Mark 〄 | certification | Japan | 4 |

### 5.4 Parts Array Format

Once symbols are confirmed, the plain-text `matrix_runout` field is supplemented with a structured parts array stored in `matrix_runout_parts` (JSONB):

```json
[
  {"t": "text", "v": "A1 "},
  {"t": "sym",  "slug": "emi-triangle", "id": "<uuid>"},
  {"t": "text", "v": " XZA 1234-1"}
]
```

### 5.5 Symbol Helpers

#### `render_parts_to_text(parts, *, symbols: dict[str, str]) → str`

Converts a parts array back to a plain-text string using a `slug → display` mapping. Unknown slugs render as `[slug]` so nothing is silently dropped.

#### `extract_symbol_ids(parts) → list[tuple[str, int]]`

Returns `(uuid, position)` pairs for every symbol entry, used to rebuild the `token_symbols` FK index after a parts array update.

---

## 6 · Ingestion Connectors

### 6.1 Architecture

Each connector extends `BaseConnector` and is governed by three independent resilience mechanisms:

- **Token-bucket rate limiter** — Limits outbound request rate per connector (configurable; Discogs default 1 req/s).
- **Circuit breaker** — Opens after N consecutive failures; automatically recovers after a timeout. Prevents hammering a failing upstream during outages.
- **Exponential backoff retry** — Re-attempts failed fetches up to `max_attempts` (default 3–5) with configurable backoff factor.

### 6.2 Available Connectors

| Connector | Source | Operations | Rate Limit |
|---|---|---|---|
| Discogs | discogs.com API | `fetch_release(id)`, `search_releases(query)` | 1 req/s (3600/hr) |
| MusicBrainz | musicbrainz.org API | `fetch_release(id)`, `search_releases(query)` | 1 req/s; User-Agent required |

### 6.3 Redis Job Queue

| Redis Key | Purpose |
|---|---|
| `mediacat:jobs:pending` | FIFO queue of jobs awaiting processing |
| `mediacat:jobs:processing` | In-flight jobs (dequeued but not yet complete) |
| `mediacat:jobs:dead` | Jobs that exhausted all retry attempts |
| `mediacat:jobs:processing_times` | Hash of job_id → dequeue timestamp (staleness detection) |

Jobs use `BLMOVE` for atomic pending → processing transfer, providing exactly-once delivery even if the worker crashes mid-job.

### 6.4 Job Lifecycle

1. Scheduler enqueues `Job(connector, action, payload)`
2. Worker calls `BLMOVE pending → processing`
3. Connector fetches data + image URLs
4. Storage pipeline downloads + deduplicates images in MinIO
5. OCR extracts text per image region
6. Vision model transcribes label / OBI / runout
7. Rule engine decodes matrix codes (OPA or Python fallback)
8. Token created or revision appended
9. Low-confidence or conflicting results → `ReviewItem` queued
10. `LREM` removes job from processing (success)
11. On crash: stale-job reaper re-enqueues after 600 s

---

## 7 · Review Queue

### 7.1 Triggers

| Trigger | Reason Code | Description |
|---|---|---|
| Vision / OCR confidence below threshold | `low_confidence` | Vision model returns confidence < 0.7 |
| Multiple sources disagree | `conflict` | Discogs and MusicBrainz return different values for the same field |
| Unknown label or manufacturer | `novel_entity` | Pipeline proposes an entity not in the reference tables |
| LLM anomaly detection | `anomaly` | Drift detector flags unexpected field values or schema changes |
| Manual raise | `manual` | Reviewer triggers re-review of an existing token |

### 7.2 Review Item States

| State | Description |
|---|---|
| `pending` | Newly queued; waiting for a reviewer to pick up |
| `in_progress` | A reviewer has opened the item |
| `approved` | Revision accepted; token updated; audit logged |
| `rejected` | Revision discarded; reason captured in `resolution_comment` |
| `deferred` | Held for later; may be reassigned or escalated |

### 7.3 Workflow

1. **Browse the queue** — Navigate to `/reviews`. Items are sorted by priority (desc) then age.
2. **Open a review** — Click any item to see the token's current values, the proposed revision, and the diff.
3. **Examine evidence** — View source images, OCR text, and vision model confidence scores.
4. **Approve or Reject** — `POST` to `/reviews/{id}/approve` or `/reviews/{id}/reject` with an optional comment. Both actions are CSRF-protected and logged to `audit_log`.
5. **Token updated** — On approval, the revision is applied to the token's denormalised fields and `current_revision_id` is advanced.

---

## 8 · Authentication & Security

### 8.1 Authentication Flow

1. **Password hashing** — Argon2id (`time_cost=3`, `memory=64 MiB`, `parallelism=2`, `hash_len=32 B`, `salt=16 B`).
2. **Login rate limiting** — In-memory lockout; max 10 failures per username or IP in 15-minute window.
3. **Account lockout** — `User.locked_until` prevents further attempts after threshold reached.
4. **Session creation** — Signed cookie (`itsdangerous.TimestampSigner`); payload = `user_id|role|nonce`; 24 h TTL.
5. **CSRF protection** — Per-session HMAC-SHA256 token validated from `X-CSRF-Token` header on all mutating requests.
6. **MFA (scaffold)** — `User.mfa_secret` stores TOTP secret (encrypted at rest); not yet wired into login flow.

### 8.2 User Roles

| Role | Capabilities |
|---|---|
| `admin` | Full access: user management, review, browse, all admin functions |
| `reviewer` | Approve / reject review items; browse tokens |
| `viewer` | Read-only: browse tokens, view revisions |
| `service` | Machine-to-machine: ingestion jobs, OCR, vision pipeline |

### 8.3 Database Roles (Least Privilege)

| DB Role | Permissions |
|---|---|
| `postgres` | Superuser — used for initial setup only |
| `mediacat_migrator` | Owns schema; runs Alembic; CREATE/DROP/ALTER |
| `mediacat_app` | INSERT/SELECT/UPDATE on app tables; no DDL; no UPDATE/DELETE on audit_log |
| `mediacat_readonly` | SELECT only — for reporting and analytics |

### 8.4 Security Headers

| Header | Value |
|---|---|
| `Content-Security-Policy` | Strict; no inline scripts; form-action restricted |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` |
| `Strict-Transport-Security` | `max-age=63072000` (2 years) |
| `Server` / `X-Powered-By` | Removed — no server fingerprinting |

### 8.5 Network Security

- Caddy reverse proxy terminates TLS; app only binds to `127.0.0.1:8000`.
- Frontend (public) and backend (internal) Docker networks are isolated.
- All secrets injected via Docker secret files (`/run/secrets/`); never in environment variables.
- All parameterised queries via SQLAlchemy — no raw SQL, no injection risk.
- Input validated by Pydantic models at every external boundary.
- Jinja2 templates auto-escape HTML — no XSS via template output.

---

## 9 · Object Storage (MinIO)

### 9.1 Buckets

| Bucket | Contents | Key Pattern |
|---|---|---|
| `media-originals` | Raw images fetched from Discogs, CoverArtArchive, user upload | `<sha256>.<ext>` |
| `ocr-artifacts` | OCR extraction results, parsing artefacts | `<sha256>.json` |
| `backups` | Daily pg_dump snapshots and database archives | `YYYY-MM-DD/<dump>.sql.gz` |

### 9.2 Content-Hash Deduplication

Every image is hashed with SHA-256 before upload. If a file with the same hash already exists in MinIO, no upload occurs — the existing object is referenced. The same album cover fetched from Discogs and uploaded manually by a user occupies exactly one object in storage.

### 9.3 Image Validation

- MIME type whitelist: JPEG, PNG, TIFF, WebP, GIF, BMP only.
- Pillow opens the image to verify it is structurally valid.
- Maximum pixel count enforced (178.9 MP) — prevents decompression bomb attacks.
- Width and height extracted and stored in `media_objects` for layout hints.

---

## 10 · Configuration Reference

### 10.1 app.yaml — Key Sections

| Section | Key Settings |
|---|---|
| `app` | name, environment (dev\|staging\|prod), log_level |
| `server` | host, port (default 8000) |
| `security` | session_secret, cookie_secure, login lockout thresholds |
| `postgres` | host, port, user, database, password_file |
| `object_store` | endpoint, access_key, secret_key_file, default_bucket |
| `redis` | url template, password_file |
| `rule_engine` | backend (opa\|local), opa_url |
| `vision` | backend (hybrid), primary (ollama), ollama_url, fallback (anthropic) |
| `llm` | backend (hybrid), primary (ollama), fallback (anthropic) |
| `feature_flags` | vision_local, llm_local, api_fallback — toggle per environment |

### 10.2 .env — Key Variables

| Variable | Description | Example |
|---|---|---|
| `MEDIACAT_ENV` | Runtime environment | `dev` |
| `MEDIACAT_DATA_ROOT` | Host path for volumes and secrets | `~/data/mediacat` |
| `HTTP_BIND` | Port binding for Caddy HTTP | `0.0.0.0:80` |
| `HTTPS_BIND` | Port binding for Caddy HTTPS | `0.0.0.0:443` |
| `PUBLIC_HOSTNAME` | External hostname (for Let's Encrypt) | `mediacat.example.com` |
| `MEDIACAT_DEV_ADMIN_PASSWORD` | Seed password for dev admin user | *(choose a strong password)* |
| `TZ` | Timezone for containers | `Europe/London` |

### 10.3 Docker Secrets

| Secret File | Used By |
|---|---|
| `postgres_app_password` | PostgreSQL connection string for the app role |
| `minio_root_password` | MinIO admin credentials |
| `redis_password` | Redis AUTH password |
| `session_secret` | HMAC key for session cookie signing |
| `discogs_token` | Discogs API personal access token (optional) |

---

## 11 · API Endpoints

MediaCat uses FastAPI with Jinja2-rendered HTML responses. All mutating endpoints require a valid session and an `X-CSRF-Token` header.

### 11.1 Public (No Auth Required)

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness probe — returns `{"status":"ok"}` |
| `GET` | `/readyz` | Readiness probe — returns `{"status":"ok"}` |
| `GET` | `/login` | Render login form with CSRF token |
| `POST` | `/login` | Authenticate — creates session cookie, redirects to `/` |
| `GET` | `/logout` | Clear session cookie, redirect to `/login` |

### 11.2 Dashboard & Browse (Auth Required)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Main dashboard — pending count, stats |
| `GET` | `/tokens` | Browse tokens; query params: `q`, `media`, `page` |
| `GET` | `/tokens/{token_id}` | Single token detail with revision history |

### 11.3 Review Queue (Reviewer+ Role)

| Method | Path | Description |
|---|---|---|
| `GET` | `/reviews` | List review items; params: `status`, `page` |
| `GET` | `/reviews/{review_id}` | Single review detail with diff view |
| `POST` | `/reviews/{review_id}/approve` | Approve revision; form: `comment` |
| `POST` | `/reviews/{review_id}/reject` | Reject revision; form: `comment` |

### 11.4 User Management (Admin Role)

| Method | Path | Description |
|---|---|---|
| `GET` | `/users` | List all users |
| `GET` | `/users/new` | Render create-user form |
| `POST` | `/users/new` | Create user; form: `username`, `email`, `password`, `role` |

---

## 12 · Alembic Migrations

```bash
# Apply all pending migrations
docker compose exec app alembic upgrade head

# Roll back one migration
docker compose exec app alembic downgrade -1

# Show current revision
docker compose exec app alembic current
```

### Migration 0001 — initial_schema (2026-04-17)

- 9 PostgreSQL ENUM types
- Extensions: `uuid-ossp`, `pg_trgm`, `btree_gist`
- 11 application tables: users, countries, labels, manufacturers, tokens, token_revisions, media_objects, ocr_artifacts, ingestion_jobs, review_items, audit_log
- Trigram GIN indexes on `labels.name_normalised` and `manufacturers.name_normalised`
- Trigram GIN indexes on `tokens.title` and `tokens.artist`
- Partial index on `review_items` WHERE `status = 'pending'`
- `REVOKE UPDATE, DELETE ON audit_log FROM mediacat_app`

### Migration 0002 — symbols (2026-04-22)

- New ENUM: `symbol_category` (6 values)
- New table: `symbols` (slug, name, category, unicode_approx, taxonomy_level, region_scope)
- New table: `symbol_variants` (variant_key, reference_image_key per symbol)
- New table: `token_symbols` (FK index: token ↔ symbol position)
- New columns on `tokens`: `matrix_runout_b` (TEXT), `matrix_runout_parts` (JSONB), `matrix_runout_b_parts` (JSONB)
- New column on `ocr_artifacts`: `symbol_candidates` (JSONB)
- Seed data: 26 confirmed symbols at taxonomy levels 2–4 (UK, US, European, Japanese marks)

---

## 13 · Make Targets

| Target | Category | Description |
|---|---|---|
| `bootstrap` | Setup | Run Ubuntu bootstrap script (sudo required); installs build tools, Tesseract |
| `data-init` | Setup | Create/verify persistent data directory tree on host |
| `venv` | Setup | Create local Python virtualenv at `.venv/` |
| `setup` | Setup | venv + install deps (uv sync) + install pre-commit hooks |
| `deps-sync` | Setup | Re-sync Python dependencies from pyproject.toml |
| `lint` | Quality | Ruff linter |
| `format` | Quality | Ruff formatter + import sorter |
| `typecheck` | Quality | Mypy strict static type checking |
| `security` | Quality | Bandit (code) + pip-audit (dependencies) |
| `test` | Quality | Full pytest suite with coverage |
| `test-fast` | Quality | Pytest excluding slow / integration markers |
| `docs-api` | Docs | Generate API reference with pdoc → `docs/reference/` |
| `docs-build` | Docs | Build MkDocs site to `site/` |
| `docs-serve` | Docs | Live-preview docs on http://127.0.0.1:8800 |
| `up` | Docker | Start all 8 Docker services (background) |
| `down` | Docker | Stop services; data volumes preserved |
| `restart` | Docker | Restart the stack |
| `logs` | Docker | Tail logs from all services |
| `ps` | Docker | `docker compose ps` — show service status |
| `config-check` | Docker | Validate docker-compose configuration |
| `clean` | Cleanup | Remove build artefacts and caches |
| `distclean` | Cleanup | `clean` + remove `.venv/` |

---

## 14 · Open Items & Roadmap

### 14.1 Security Defects (Tracked)

#### DEF-001 [MEDIUM] — Wire sanitise() into translation pipeline

OCR text is passed to the LLM for translation without sanitisation — malicious text in a scanned label could inject instructions into the LLM prompt.

**Fix:** Apply `sanitise()` before truncation, before LLM call. **Effort:** 1 hour.

#### DEF-002 [LOW] — Migrate login rate limiter to Redis

`LoginRateLimiter` is an in-memory dict — state is lost on app restart and not shared across multiple app instances.

**Fix:** Redis backend provides persistence and cluster safety. **Effort:** 0.5 day.

#### DEF-003 [MEDIUM] — REVOKE UPDATE/DELETE on audit_log at DB level

Currently enforced via OPA policy only. A direct DB connection could still delete audit rows.

**Fix:** Execute `REVOKE UPDATE, DELETE ON audit_log FROM mediacat_app;` after migration. **Effort:** 1 hour.

#### DEF-004 [LOW] — detect_is_english false positives

Simple substring-match heuristic may misclassify non-English text.

**Fix:** Replace with a proper language detection library. **Effort:** 0.5 day.

#### DEF-005 [LOW] — BaseHTTPMiddleware body-streaming overhead

Starlette `BaseHTTPMiddleware` buffers the full request/response body.

**Fix:** Migrate to pure ASGI middleware for better performance on large image uploads. **Effort:** 1–2 days.

### 14.2 Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| ADR-0001 | Media scope: vinyl + CD only (v1) | Accepted |
| ADR-0002 | Hybrid AI: local Ollama primary, API fallback | Accepted |
| ADR-0003 | Rule engine: OPA primary, Python local fallback | Accepted |
| ADR-0004 | Data/code separation via host bind mounts | Accepted |
| ADR-0005 | Web stack: FastAPI + Jinja2 + HTMX (no JS framework) | Accepted |

### 14.3 Near-term Roadmap

| Priority | Item |
|---|---|
| P1 | Wire DEF-001 `sanitise()` into translation pipeline |
| P1 | Enforce DEF-003 audit_log REVOKE at DB level |
| P2 | DEF-002 Redis-backed login rate limiter |
| P2 | Phase 2 symbol pipeline: focused re-run with slug hints |
| P2 | LLM comparison / anomaly detection module (`llm/tasks.py`) |
| P3 | MFA TOTP login flow (scaffold already in User model) |
| P3 | CLIP embedding similarity search for symbol Phase 3 |
| P3 | Horizontal scaling: multiple worker containers on same Redis queue |

---

## 15 · Usage Guide

### 15.1 Daily Workflow — Cataloguing a New Record

#### 1. Add the token
Navigate to `/tokens` and click **New Token**. Enter the basic details: media format (vinyl/CD), barcode or catalogue number if known. The token is created in `Draft` status.

#### 2. Upload images
On the token detail page, upload scans for each region: label side A, label side B, runout A, runout B, cover front/back. Use high-resolution scans (≥ 600 DPI) for runout areas to help the vision model detect symbols.

#### 3. Trigger ingestion
Click **Fetch from Discogs** or **Fetch from MusicBrainz** to pull metadata. The system queues a job; the worker fetches, OCRs, and runs vision in the background.

#### 4. Review proposals
Navigate to `/reviews`. Each AI proposal appears as a pending review item. Check the diff (proposed vs current), inspect confidence scores, and approve or reject each field update.

#### 5. Confirm symbols
For runout images, `symbol_detections` appear in the review item details. If a slug suggestion is shown (e.g. `emi-triangle`), verify it visually and approve. If no slug was suggested, look up the symbol in the registry and assign the correct slug before approving.

#### 6. Activate the token
Once satisfied with the data, change the token status from `Draft` to `Active` on the token detail page.

### 15.2 Searching for Tokens

The token browser at `/tokens` supports full-text search across title and artist (backed by PostgreSQL trigram similarity) and filtering by media format. The search is tolerant of minor spelling errors.

- Search is case-insensitive.
- Use the `media=` query parameter to restrict to `vinyl` or `cd`.
- Matrix runout plain text is searchable via the `extra` JSONB field.
- To find all tokens containing a specific symbol: `SELECT * FROM token_symbols JOIN symbols ON token_symbols.symbol_id = symbols.id WHERE symbols.slug = 'emi-triangle';`

### 15.3 Adding a New Symbol Manually

1. **Choose a slug** — Lowercase, hyphenated, descriptive (e.g. `columbia-pitman`). Must be unique and must never change once assigned.
2. **Insert via admin shell:**
   ```sql
   INSERT INTO symbols (slug, name, category, taxonomy_level, region_scope, is_confirmed)
   VALUES ('my-slug', 'My Symbol Name', 'pressing_plant_mark', 3, 'US', false);
   ```
3. **Confirm in review** — Create a `ReviewItem` with `reason=novel_entity`. A reviewer approves and sets `is_confirmed = true`.

### 15.4 Running the Worker Manually

```bash
# Run in the foreground (development)
docker compose exec worker python -m mediacat.worker

# Enqueue a single job from the Python shell
from mediacat.ingestion.queue import enqueue_job, Job
await enqueue_job(Job(connector='discogs', action='fetch_release', payload={'id': 1328315}))
```

### 15.5 Backup & Restore

Automated daily backups run in the backup container and are stored in the MinIO `backups` bucket.

```bash
# Run a manual backup
deploy/scripts/backup.sh
```

To restore from backup: stop the stack, restore the PostgreSQL dump (`psql < backup.sql`), restart, and run `alembic upgrade head` to ensure the schema is current.

---

*MediaCat · Technical Reference · v0.1 · April 2026 · CONFIDENTIAL*

*This document is confidential. Do not distribute outside the project team.*
