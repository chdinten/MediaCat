# MediaCat — Technical Reference

**Version 0.3 · April 2026**

> Cataloguing platform for vinyl records and compact discs.
> Python 3.12 · FastAPI · PostgreSQL 16 · MinIO · Redis · OPA · Ollama


## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Web Layer — UI & Routes](#2-web-layer--ui--routes)
3. [Catalogue Feature](#3-catalogue-feature)
4. [Database Schema](#4-database-schema)
5. [Vision & OCR Pipeline](#5-vision--ocr-pipeline)
6. [Symbol Registry](#6-symbol-registry)
7. [Ingestion Connectors](#7-ingestion-connectors)
8. [Review Queue](#8-review-queue)
9. [Authentication & Security](#9-authentication--security)
10. [Object Storage (MinIO)](#10-object-storage-minio)
11. [AI Infrastructure (Ollama)](#11-ai-infrastructure-ollama)
12. [Configuration Reference](#12-configuration-reference)
13. [Deployment — Docker Compose](#13-deployment--docker-compose)
14. [API Endpoints](#14-api-endpoints)
15. [Alembic Migrations](#15-alembic-migrations)
16. [Make Targets](#16-make-targets)
17. [Open Items & Roadmap](#17-open-items--roadmap)
18. [Usage Guide](#18-usage-guide)


## 1 · Architecture Overview

### 1.1 What Is MediaCat?

MediaCat is a hybrid-AI cataloguing platform for physical music media — vinyl records and compact discs. It ingests metadata and imagery from external sources (Discogs, MusicBrainz, CoverArtArchive), enriches records with OCR and vision-model transcription, and stores everything in an append-only token-object registry with mandatory human review gates.

**LLMs propose, humans approve.** No AI decision is ever applied automatically.

### 1.2 Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.12 | Async/await throughout |
| Web framework | FastAPI + Jinja2 + HTMX | Server-side rendering; no JS framework build step |
| Database | PostgreSQL 16 | pg_trgm, uuid-ossp, btree_gist |
| ORM / migrations | SQLAlchemy 2 + Alembic | Async ORM, deterministic migration names |
| Object storage | MinIO | S3-compatible, SHA-256 content-hash dedup |
| Job queue | Redis 7 | BLMOVE atomic dequeue, stale-job reaper |
| Rule engine | Open Policy Agent | Rego policies + Python fallback |
| Vision / LLM | Ollama (local-first) | Qwen2.5-VL, glm-ocr; Anthropic API fallback |
| Reverse proxy | Caddy | Automatic TLS (local CA for dev, Let's Encrypt for prod) |
| Deployment | Docker Compose on WSL2 | 8-service stack; host-mounted data volumes |
| Auth | Argon2id + itsdangerous | Signed session cookies, X-CSRF-Token header |

### 1.3 Technology Stack — Rationale

#### Python 3.12

Python is the dominant language in both data science and the broader AI/ML ecosystem — the communities, libraries, tooling, and hiring pool are unmatched. Version 3.12 adds meaningful performance improvements (sub-interpreter support, improved specialising adaptive interpreter) and eliminates several long-standing rough edges. The async/await model introduced in Python 3.5 has now matured to a point where FastAPI and SQLAlchemy 2 use it natively, making it a natural fit for an I/O-heavy cataloguing workload where most time is spent waiting on database queries, object-store uploads, and external API calls rather than doing CPU work.

**Alternatives considered:** Go (faster, but far smaller AI/ML library ecosystem); Node.js (mature async, but weaker data-science tooling and type safety); Rust (excellent performance, but impractical iteration speed for a domain-heavy application at this stage).


#### FastAPI + Jinja2 + HTMX

**FastAPI** is the fastest-growing Python web framework and has become the de-facto standard for new Python web APIs, overtaking Flask in stars and downloads. Its design around Python type annotations means input validation, documentation generation (OpenAPI / Swagger), and IDE completion come for free — no boilerplate. The async-native foundation slots directly into the async SQLAlchemy and httpx ecosystem.

**Jinja2** is the standard Python template engine (used by Flask, Ansible, Django-adjacent projects). It is stable, well-documented, and auto-escapes HTML by default, eliminating an entire class of XSS vulnerabilities. Server-side HTML rendering avoids shipping a JavaScript build pipeline and its associated complexity, security surface, and maintenance burden.

**HTMX** replaces the traditional single-page-application pattern by adding `hx-get`, `hx-post`, and `hx-target` attributes to ordinary HTML. It enables full AJAX interactivity — the Artist→Album→Pressing drill-down, live search, partial updates — without any JavaScript build step, no npm, no bundler, and no client-side state management framework. The library is ~15 KB minified, stable, and maintained by a single focused team. For a content-heavy cataloguing application the server-side model is a better fit than React or Vue.

**Alternatives considered:** Django (heavier, batteries-included — adds value for admin interfaces but brings ORM coupling and a migration system that conflicts with Alembic); Flask (simpler but requires more wiring for async and validation); React/Vue (excellent for highly interactive UIs, but adds a JS build pipeline and a client-server state split that is unnecessary here).


#### PostgreSQL 16

PostgreSQL is the most feature-complete open-source relational database and has been the top-rated database on the DB-Engines ranking for several consecutive years. For this project three specific capabilities were decisive:

- **`pg_trgm`** — trigram-based fuzzy text search over artist names and titles without a separate search index service (Elasticsearch, Meilisearch, etc.).
- **`JSONB`** — native binary JSON storage with indexing. The `matrix_runout_parts` structured parts array, the `matrix_runout_parsed` breakdown dict, and revision `data` snapshots live in JSONB — no document-database overhead.
- **`uuid-ossp`** — server-side UUID generation. Every row uses a UUID primary key, eliminating integer-sequence leakage and enabling safe distributed insert ordering.

PostgreSQL 16 also adds logical replication improvements and improved `VACUUM` parallelism, useful if the catalogue grows to hundreds of thousands of tokens.

**Alternatives considered:** MySQL / MariaDB (weaker JSONB support, no pg_trgm equivalent); SQLite (insufficient for concurrent multi-container access, no server-side UUID generation); MongoDB (BSON documents are appealing for the revision data, but the rigid SQL schema + JSONB escape hatch gives both structure and flexibility without abandoning ACID guarantees).


#### SQLAlchemy 2 + Alembic

**SQLAlchemy 2** is the reference Python ORM. The 2.0 release (2023) completed the async-native rewrite — async sessions, async engine, and 2.0-style `select()` queries — making it the obvious choice for a FastAPI application. The declarative ORM model with type-annotated columns (`Mapped[str]`, `mapped_column()`) provides IDE completion and mypy static type checking on every model attribute, catching errors at development time rather than at runtime.

**Alembic** is SQLAlchemy's own migration tool, maintained by the same team. The `--autogenerate` feature diffs the ORM models against the live schema and produces migration scripts automatically. The naming convention configuration (`ix_%(column_0_label)s`, etc.) ensures that constraint names are deterministic across environments, preventing the common problem of migration files that work on the developer's machine but fail on the production database.

**Alternatives considered:** Django ORM (tightly coupled to Django); Tortoise ORM (async-native but less mature, smaller community); raw SQL + psycopg3 (maximum control but significant boilerplate for models and schema management).


#### MinIO

MinIO is an S3-compatible object store that can run entirely on-premises in a single Docker container. The S3 API is the de-facto standard for object storage — the same `boto3`/`minio` client code works against MinIO in development and AWS S3 in production with a single endpoint change. MinIO is written in Go, delivers high throughput, and has no external dependencies.

For MediaCat the key property is **content-addressed storage**: every image is keyed by its SHA-256 hash. Storing the same album cover fetched from Discogs and uploaded manually by a user costs exactly one object. This deduplication is free and automatic — no separate dedup service required.

MinIO also ships a clean browser-based admin console on port 9001, making it easy to inspect uploaded images and audit storage usage during development.

**Alternatives considered:** AWS S3 (requires internet access and an AWS account — unsuitable for a fully local stack); local filesystem (no dedup, no content-addressed keys, no S3-compatible API, hard to migrate later); Ceph (far more complex to operate for a small single-node deployment).


#### Redis 7

Redis provides the background job queue and is the obvious choice for this role: it is the most widely deployed in-memory data store, available in every cloud and package repository, and has a battle-tested atomic list primitive (`BLMOVE`) that gives exactly-once job handoff between the queue and the worker.

The job lifecycle uses a two-list pattern (`pending` → `processing`) with `BLMOVE`, which is atomic — if the worker crashes mid-job, the job remains in `processing` and the stale-job reaper re-enqueues it after a timeout. No message is ever silently lost.

Redis 7 adds Redis Functions (Lua replacement) and multi-part AOF, improving durability on unclean shutdown. The AOF (append-only file) persistence mode is enabled so the job queue survives container restarts.

**Alternatives considered:** RabbitMQ (more complex, requires a broker setup, better for fan-out / exchange routing that is not needed here); Celery (adds a Python layer on top of Redis/RabbitMQ — useful for distributed workers but introduces its own state machine complexity); PostgreSQL SKIP LOCKED queue (simpler stack but mixes transactional and queue concerns in the same DB).


#### Open Policy Agent (OPA)

OPA is the CNCF-graduated policy engine used by Kubernetes, Envoy, and dozens of major platforms. It decouples policy logic from application code: rules are written in Rego (a purpose-built declarative language), loaded into a running OPA sidecar, and queried over HTTP. This means access control rules can be updated and tested without redeploying the application.

For MediaCat, OPA governs which user roles can perform which operations (approve/reject reviews, manage users, archive tokens). The Python fallback (`rules/local.py`) implements the same rules in pure Python for environments where the OPA container is not running, ensuring the app is never completely ungated.

**Alternatives considered:** Casbin (Python-native RBAC library — simpler for basic role checks but not externalised or auditable); hand-coded `if role == 'admin'` guards (simple but scattered through the codebase, hard to audit, easy to miss); AWS Cognito / Auth0 (cloud identity services — unnecessary coupling to an external provider for a self-hosted application).


#### Ollama (local-first Vision / LLM)

Ollama is the leading open-source local model server. It provides a unified API compatible with the OpenAI chat format, manages model weights on disk, handles CUDA GPU allocation, and serves multiple models simultaneously. The single `ollama pull <model>` command replaces hours of manual GGUF conversion, quantisation, and server configuration.

**Why local-first?** For a vinyl cataloguing workflow, images of runout etchings and label text are processed repeatedly during development and QA. Sending every image to a cloud API would be slow (network round-trip), costly (per-token billing), and a potential privacy concern (album cover images and handwritten dedications uploaded to a third party). Running the VLM locally on an RTX 4090 (24 GB GDDR6X) provides sub-10-second inference with no per-query cost and no data leaving the machine.

**`qwen2.5vl:32b`** is Qwen's 32-billion-parameter vision-language model. At Q4_K_M quantisation (~21 GB) it fits on a single RTX 4090 with context length 8192. It achieves strong results on OCR and document understanding benchmarks — directly relevant to reading label text and runout inscriptions.

**`glm-ocr`** is a smaller (~2.2 GB) OCR-specialist model optimised for text extraction from document images, used for the initial OCR pass before the VLM sees the image.

**Anthropic API fallback** — when Ollama is unavailable or returns low-confidence results, the hybrid adapter automatically falls back to the Anthropic Messages API (Claude). This provides resilience without requiring the GPU to be permanently on.

**Alternatives considered:** llama.cpp directly (more control, but no model management, no API server, manual CUDA setup); vLLM (higher throughput for batch inference, but heavier to run and requires more configuration); cloud-only (Anthropic / OpenAI / Google Vision API — vendor lock-in, per-query cost, no offline capability).


#### Caddy

Caddy is the only major web server that automatically provisions and renews TLS certificates with zero configuration — both via Let's Encrypt for public domains and via its own local CA for `.localhost` development domains. The `Caddyfile` syntax is an order of magnitude simpler than nginx or Apache configuration.

For local development, Caddy generates a local CA certificate automatically. Importing that certificate into the Windows trust store once gives all browsers a trusted HTTPS connection to `mediacat.localhost` — no self-signed certificate warnings, no manual `mkcert` setup. In production, pointing `PUBLIC_HOSTNAME` to a real domain causes Caddy to obtain a Let's Encrypt certificate automatically with no configuration change.

**Alternatives considered:** nginx (powerful but requires explicit TLS certificate management — `certbot` for Let's Encrypt, manual cert for local dev; no zero-config option); Traefik (dynamic routing, good for multi-service Kubernetes setups, but more complex configuration for a fixed 8-service stack); direct uvicorn on port 80/443 (no TLS termination, no HTTP→HTTPS redirect, no subdomain routing).


#### Docker Compose on WSL2

Docker Compose is the simplest multi-container orchestration tool available and is bundled with Docker Desktop. The compose file is declarative, version-controlled, and reviewable by anyone without Kubernetes knowledge. The `profiles:` feature (used for the Ollama optional stack) allows subsets of services to be started without multiple compose files.

**WSL2** (Windows Subsystem for Linux 2) runs a real Linux kernel inside a lightweight VM, giving Docker full Linux container support on Windows with near-native I/O performance on the `ext4` filesystem. The `networkingMode=mirrored` WSL2 setting (introduced in Windows 11 22H2) eliminates the NAT networking complexity that previously made WSL2 Docker ports unreachable from Windows browsers.

The **host-mounted volume** pattern (rather than Docker named volumes) keeps all persistent data visible on the host filesystem at `MEDIACAT_DATA_ROOT` — it can be inspected, backed up, and restored with standard filesystem tools without any Docker knowledge.

**Alternatives considered:** Kubernetes / k3s (necessary for production multi-node deployments, but dramatically more complex for a single-developer local setup); Docker named volumes (opaque to the host filesystem, harder to backup and inspect); Podman Compose (compatible but less tooling, less documentation).


#### Argon2id + itsdangerous (Auth)

**Argon2id** is the winner of the 2015 Password Hashing Competition and the current OWASP recommendation for password storage. It is memory-hard (resistant to GPU/ASIC brute-force), configurable (time cost, memory cost, parallelism), and available via the `argon2-cffi` Python package which wraps the reference C implementation. The `id` variant combines data-independent (Argon2i) and data-dependent (Argon2d) modes, providing resistance to both side-channel attacks and GPU cracking.

**itsdangerous** (from the Pallets project, same team as Flask and Jinja2) provides cryptographically signed tokens using HMAC-SHA1 or HMAC-SHA256. The session cookie is a signed timestamp token — the signature prevents tampering and the timestamp enforces the 24-hour TTL server-side without a server-side session store. The CSRF token is derived from the session secret using the same signing mechanism, giving per-session CSRF protection with no database round-trip.

**Alternatives considered:** bcrypt (well-established but less resistant to GPU attacks than Argon2id; OWASP now recommends Argon2id first); JWTs for sessions (stateless but revocation requires a blocklist; the signed cookie approach is simpler and equally secure for a server-rendered app); dedicated session middleware (Django sessions, Flask-Session) — unnecessary complexity when itsdangerous provides exactly what is needed.


### 1.4 Architectural Principles

- **Advisory-only AI** — Vision and LLM models propose updates; humans always confirm via the review queue.
- **Append-only revisions** — Every change creates a new `TokenRevision` row; nothing is overwritten.
- **Content-addressed storage** — Images keyed by SHA-256 in MinIO — duplicates are free.
- **Data / code separation** — All persistent state lives on host-mounted volumes; containers are ephemeral.
- **Least-privilege DB roles** — The app role cannot ALTER, DROP, or write the audit log.
- **Hybrid AI resilience** — Local Ollama handles 99% of traffic; API fallback fires automatically on failure.

### 1.5 Repository Layout

```
mediacat/
├── src/mediacat/
│   ├── db/           ORM models, enums, base, engine, symbol helpers
│   ├── web/          FastAPI app, routes, auth, middleware, catalogue, templates, static
│   ├── vision/       VLM adapter, task prompts, candidate matcher
│   ├── llm/          LLM adapter, tasks, safety, Ollama & Anthropic backends
│   ├── ingestion/    Connector base, Discogs, MusicBrainz, Redis queue, drift detector
│   ├── rules/        OPA adapter, local Python fallback
│   └── storage/      MinIO wrapper, image pipeline, OCR, translation
├── alembic/versions/ Sequential migrations (0001 initial, 0002 symbols, 0003 matrix parsed, 0004 primary cover)
├── deploy/           Docker Compose, Caddyfile, OPA bundles, backup scripts
├── config/           app.yaml + connectors.yaml (host-mounted; examples in Git)
└── docs/             ADRs, section docs, due-diligence report
```


## 2 · Web Layer — UI & Routes

### 2.1 Application Factory (`web/app.py`)

The FastAPI application is created by `create_app()` and uses a lifespan context manager (`_lifespan`) that:

1. Reads YAML config and constructs the DB connection string.
2. Creates the SQLAlchemy async engine with connection pool settings from config.
3. Stores `engine` and `session_factory` on `app.state` (shared across all requests).
4. Registers all middleware and routers.
5. Disposes the engine on shutdown.

```python
# DB access pattern in route handlers:
async with request.app.state.db_session_factory() as db:
    result = await db.execute(select(Token).where(...))
```

The catalogue router is imported inside `create_app()` (not at module level) to avoid circular imports:
```python
from mediacat.web.catalogue import catalogue_router  # noqa: PLC0415
app.include_router(catalogue_router)
```

### 2.2 Middleware Stack (outermost first)

| Middleware | Purpose |
|---|---|
| `AccessLogMiddleware` | Structured request/response log line |
| `SecurityHeadersMiddleware` | Injects CSP, HSTS, X-Frame-Options, etc. |
| `SessionMiddleware` | Reads signed cookie → `request.state.session`, `user_id`, `user_role`, `csrf_token` |
| `RequestIdMiddleware` | Generates `X-Request-ID` UUID for log correlation |

### 2.3 UI Theme

The UI uses a **dark-first CSS custom properties** system. Dark mode is the default; light mode is applied by setting `[data-theme="light"]` on the `<html>` element.

- **`static/theme.js`** — Runs synchronously before first paint (no `defer`) to prevent flash of wrong theme. Reads `localStorage['mc-theme']` and applies the `data-theme` attribute immediately.
- **`static/style.css`** — Single stylesheet; all colours are CSS variables. `[data-theme="light"]` block overrides the dark defaults.
- Theme persists across sessions via `localStorage`.

### 2.4 Template Context

All route handlers call `_ctx(request, **extra)` which builds:

```python
{
    "request": request,
    "user": request.state.session,    # dict with user_id, role, username
    "csrf_token": request.state.csrf_token,
    "is_htmx": request.headers.get("hx-request") == "true",
    **extra
}
```

### 2.5 HTMX Integration

HTMX is loaded from `/static/htmx.min.js`. The `<body>` tag includes `hx-headers` with the CSRF token:
```html
<body hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'>
```
All HTMX requests include the CSRF token automatically.


## 3 · Catalogue Feature

### 3.1 Data Model (Derived Hierarchy)

The Artist → Album → Pressing hierarchy is derived from the `tokens` table — no separate artist or album tables. This keeps the data model flat while supporting full CRUD and browse.

```
Token (one per unique physical pressing)
├── artist       → grouped into "Artist" level
├── title        → grouped into "Album" level
└── Individual pressing fields (year, format, label, matrix, etc.)
```

### 3.2 Route Structure (`web/catalogue.py`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/catalogue` | Artist browser — grouped by `artist`, paginated |
| `GET` | `/catalogue/new` | Create-pressing form |
| `POST` | `/catalogue/new` | Create Token + initial TokenRevision (source=human) |
| `GET` | `/catalogue/search` | External search (Discogs / MusicBrainz) — HTMX partial |
| `GET` | `/catalogue/merge` | Merge selection UI |
| `POST` | `/catalogue/merge` | Execute merge — sets loser `status=merged`, adds winner revision |
| `POST` | `/catalogue/scan-cover` | Scan barcode from cover photo (pyzbar); redirects to pre-filled form |
| `GET` | `/catalogue/import/discogs/{release_id}` | Preview Discogs release before import |
| `POST` | `/catalogue/import/discogs/{release_id}` | Create Token directly from Discogs release |
| `GET` | `/catalogue/import/musicbrainz/{release_id}` | Preview MusicBrainz release before import |
| `POST` | `/catalogue/import/musicbrainz/{release_id}` | Create Token directly from MusicBrainz release |
| `GET` | `/catalogue/artists/{artist}/albums` | Albums for artist — HTMX partial |
| `GET` | `/catalogue/artists/{artist}/albums/{title}/pressings` | Pressings for album — HTMX partial |
| `GET` | `/catalogue/{token_id}` | Pressing detail (full page) |
| `GET` | `/catalogue/{token_id}/edit` | Edit form (includes full image gallery + upload zone) |
| `POST` | `/catalogue/{token_id}/edit` | Save — creates new TokenRevision (source=human) |
| `POST` | `/catalogue/{token_id}/archive` | Set `status=archived` |
| `POST` | `/catalogue/{token_id}/delete` | Soft-delete — sets `deleted_at` |
| `POST` | `/catalogue/{token_id}/flag-review` | Manually flag pressing for the review queue |
| `POST` | `/catalogue/{token_id}/refresh-discogs` | Re-fetch images from Discogs for an existing pressing |
| `POST` | `/catalogue/{token_id}/images` | Upload one or more images (JPEG/PNG/TIFF/WebP, up to 30 MB each) |
| `GET` | `/catalogue/{token_id}/images/{image_id}` | Serve image bytes from MinIO |
| `POST` | `/catalogue/{token_id}/images/{image_id}/region` | Reassign image region label; auto-triggers vision analysis if reassigned to a runout region |
| `POST` | `/catalogue/{token_id}/images/{image_id}/analyse` | Run synchronous vision analysis on a runout/label image; saves OcrArtifact |
| `POST` | `/catalogue/{token_id}/images/{image_id}/apply-ocr` | Write latest OcrArtifact into the token's matrix fields + parsed breakdown |
| `POST` | `/catalogue/{token_id}/images/{image_id}/delete` | Hard-delete a MediaObject DB row and MinIO object |
| `POST` | `/catalogue/{token_id}/images/{image_id}/set-cover` | Mark image as primary cover (clears flag on all others for this token) |
| `POST` | `/catalogue/{token_id}/correct-matrix-field` | Apply a human correction to one parsed matrix field with a mandatory reason code + optional notes |

> **Route ordering:** Static path segments (`/new`, `/search`, `/merge`, `/scan-cover`, `/import/...`, `/artists/...`) are resolved before the parametric `/{token_id}` catch-all. This is guaranteed by Starlette's routing which prioritises literal segments over path parameters.

### 3.3 HTMX Drill-down Pattern

The catalogue browse works as a three-level accordion using HTMX:

1. **Artist grid** (`catalogue.html`) — each artist card has `hx-get="/catalogue/artists/{enc}/albums"` targeting a `<div class="album-drawer">` directly below.
2. **Album list** (`partials/album_list.html`) — each album row has `hx-get="/catalogue/artists/{enc_artist}/albums/{enc_title}/pressings"` targeting a `<div class="pressing-drawer">` below.
3. **Pressing list** (`partials/pressing_list.html`) — each pressing links to the full detail page `GET /catalogue/{token_id}`.

Artist and title values in URLs are `urllib.parse.quote()`-encoded and decoded at the route handler.

### 3.4 External Search

`GET /catalogue/search?q=...&source=discogs|musicbrainz` queries the external APIs and returns `partials/search_results.html`. No API key is required for basic Discogs searches (limited rate); a personal access token in `connectors.yaml` raises the rate limit to 3600/hr.

MusicBrainz requires a `User-Agent` header identifying the application.

### 3.5 Create / Edit — Revision Lifecycle

On create:
1. A `Token` row is inserted with `status=active`.
2. A `TokenRevision` row is inserted with `revision_number=1`, `source=human`, and a `data` JSONB snapshot of all fields.
3. `token.current_revision_id` is set to the new revision.

On edit:
1. The token's denormalised fields are updated in-place.
2. A new `TokenRevision` is appended with `revision_number = max(existing) + 1`.
3. `token.current_revision_id` is advanced.

This preserves the full edit history without needing event sourcing.

### 3.6 Merge

Merging marks the loser token `status=merged` (excluded from browse queries). A merge revision is appended to the winner token's history with a comment noting the merged token ID. The loser is never hard-deleted — it can be inspected or unmerged by direct DB query.

### 3.7 Image Management

#### Upload

Images can be uploaded on both the **pressing detail page** (`/catalogue/{id}`) and the **edit form** (`/catalogue/{id}/edit`). The edit form includes the full image gallery and drag-and-drop upload zone, so users no longer have to navigate to a separate page just to upload images.

The upload endpoint (`POST /catalogue/{id}/images`) accepts one or more files per request. Accepted MIME types: `image/jpeg`, `image/png`, `image/tiff`, `image/webp`. Maximum size: 30 MB per file. After storage in MinIO (SHA-256 key), runout and label images are automatically submitted to the vision pipeline as a background task.

#### Drag-and-drop Implementation

The drop zone uses **document-level event delegation**: `dragover` and `drop` listeners are attached to `document` rather than to the zone element directly. On each event, the handler checks whether the drag cursor is over a descendant of the drop zone before activating it. This fixes a failure mode where dragging a file over a child element of the drop zone (e.g., text or an icon inside it) caused the browser to open the file in a new tab instead of triggering the upload handler.

#### Region Labels

Every `MediaObject` has an `image_region` enum value (e.g. `label_a`, `runout_a`, `cover_front`, `obi_front`). The region can be reassigned via `POST /catalogue/{id}/images/{image_id}/region`. Reassigning to any runout region (`runout_a`, `runout_b`, `matrix`) automatically triggers background vision analysis.

#### Delete

`POST /catalogue/{id}/images/{image_id}/delete` hard-deletes the `MediaObject` row and removes the object from MinIO. A confirmation dialog is shown before submission.

#### Primary Cover Selection

`POST /catalogue/{id}/images/{image_id}/set-cover` sets `media_objects.is_primary_cover = TRUE` for the specified image and clears the flag on all other images for the same token. Cover display logic prefers `is_primary_cover = TRUE` over the `region = cover_front` fallback. This enables correct display for Japanese pressings with separate OBI strip and album art images.

Each image card on the detail and edit pages shows a **"Set as cover"** button (or **"★ Primary cover"** if already selected).

### 3.8 Vision Analysis & Matrix Parsing

#### Interactive Analysis

In addition to automatic background analysis triggered on image upload or region reassignment, users can trigger vision analysis manually from any image card using the **"Analyse with AI"** button. This calls `POST /catalogue/{id}/images/{image_id}/analyse` synchronously and returns the structured result immediately.

#### Apply OCR Result

After analysis, the **"Apply to token"** button calls `POST /catalogue/{id}/images/{image_id}/apply-ocr`. This writes the latest `OcrArtifact` for that image into the token's `matrix_runout` / `matrix_runout_b` text fields and into the structured `matrix_runout_parsed` / `matrix_runout_b_parsed` JSONB columns.

#### Structured Breakdown Table

The pressing detail page renders a **Manufacturing & Stamping** section with a breakdown table of the parsed matrix fields. Each field shows:

- The field value (or `—` if not detected)
- A **confidence badge**: green (high ≥ 70%), amber (mid ≥ 40%), red (low < 40%)
- A **source badge**: "AI" (source=`vision`), "✓ human" (source=`human`), "import", or "rule"
- A per-field **Edit** button that opens the correction modal

The display text in the section header (`10AA6305231 1Y 320`) is reconstructed at render time by `_build_full_runout_text(parsed)`, so old records with narrow stored values still display correctly without requiring re-analysis.

#### Field Correction

See [Section 5.5](#55-field-correction-audit-trail) for the full correction workflow.


## 4 · Database Schema

PostgreSQL 16 is the primary data store. All tables use UUID primary keys (server-generated via `uuid_generate_v4()`). Timestamps are UTC with timezone. Schema is managed exclusively via Alembic migrations.

### 4.1 Entity Overview

| Entity | Purpose |
|---|---|
| `Token` | One per unique physical release — unit of identity |
| `TokenRevision` | Append-only history; every change creates a new row |
| `MediaObject` | Image stored in MinIO, linked to a token |
| `OcrArtifact` | OCR text extracted from a MediaObject |
| `Symbol` | Canonical graphical runout mark (e.g. EMI triangle) |
| `SymbolVariant` | Visual variant of a canonical Symbol |
| `TokenSymbol` | FK index: which symbols appear in a token's runout |
| `ReviewItem` | AI proposal awaiting human approval |
| `Label / Manufacturer / Country` | Reference entities confirmed by reviewers |
| `IngestionJob` | Background connector job tracking |
| `AuditLog` | Append-only immutable action trail (BIGINT PK) |
| `User` | Application user with role-based access |

### 4.2 Token Table (key columns)

```
id · barcode · catalog_number
matrix_runout (plain text, side A)
matrix_runout_b (plain text, side B)
matrix_runout_parts (JSONB — structured symbol/text parts array)
matrix_runout_b_parts (JSONB)
matrix_runout_parsed (JSONB — structured 7-field breakdown, side A)   ← migration 0003
matrix_runout_b_parsed (JSONB — structured 7-field breakdown, side B) ← migration 0003
media_format · status · title · artist · year
country_id · label_id · manufacturer_id
discogs_release_id · musicbrainz_release_id
current_revision_id · extra (JSONB)
deleted_at (soft delete — SoftDeleteMixin)
```

**`matrix_runout_parsed` / `matrix_runout_b_parsed` schema:**

Each column is a JSONB dict with the following keys:

| Key | Description |
|---|---|
| `matrix_number` | Primary matrix identifier (e.g. `10AA6305231`) |
| `stamper_code` | Stamper/mother/lacquer code (e.g. `1Y`) |
| `sid_mastering` | SID mastering code (IFPI Lx…) |
| `sid_mould` | SID mould code (IFPI Mx…) |
| `lacquer_cutter` | Lacquer cutter abbreviation or mark |
| `pressing_plant` | Pressing plant identifier |
| `other_etchings` | Remaining etched text not assigned to above fields |

Each value is an object: `{"value": str|null, "confidence": float|null, "source": "vision"|"human"|"import"|"rule"|null}`.

Browse queries always include `.where(Token.deleted_at.is_(None)).where(Token.status != TokenStatus.MERGED)`.

### 4.3 Enumerations

| Enum | Values |
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

### 4.4 MediaObject Table (key columns)

```
id · token_id (FK) · minio_key · sha256 · mime_type
image_region (enum — see 4.3)
is_primary_cover (BOOLEAN NOT NULL DEFAULT FALSE)   ← migration 0004
width · height · file_size
created_at · deleted_at
```

**Cover selection precedence:**

1. The image where `is_primary_cover = TRUE` (if any) — set explicitly by the user via "Set as cover".
2. Fallback: the first image where `region = cover_front`.

This two-level precedence enables Japanese pressings (and similar cases) to nominate a specific image as the catalogue cover independently of region tagging. For example, an OBI strip image tagged `obi_front` can be designated the primary cover while the album art image retains its own `cover_front` region label.


## 5 · Vision & OCR Pipeline

### 5.1 Pipeline Overview

| Step | Stage | Detail |
|---|---|---|
| 1 | Image arrives | Uploaded by user (detail or edit page) or fetched from Discogs / CoverArtArchive |
| 2 | MinIO store | SHA-256 hash computed → deduplicated upload |
| 3 | OCR extraction | Tesseract (or cloud: Azure / AWS Textract) per image region |
| 4 | Translation | OCR text translated to British English via local LLM |
| 5 | Vision transcription | HybridVision calls Ollama (Qwen2.5-VL) with task prompt |
| 6 | JSON parsing | Structured response validated against expected schema |
| 7 | Candidate matching | Trigram search + exact match against token table |
| 8 | Review queue or direct apply | Background path → review_items; interactive path → user clicks Apply |

Vision analysis can be triggered in two ways:
- **Background (automatic):** Fires on image upload (if region is runout/label) or on region reassignment to a runout region.
- **Interactive (user-initiated):** User clicks "Analyse with AI" on any image card → `POST /catalogue/{id}/images/{image_id}/analyse` runs synchronously and returns the result in-page.

### 5.2 Vision Backends

- **OllamaVisionBackend (primary)** — Sends base64-encoded image + prompt to `http://ollama:11434/api/chat`. Timeout: 120 s. Runs entirely on-premises.
- **AnthropicVisionBackend (fallback)** — Uses Anthropic Messages API. Requires `ANTHROPIC_API_KEY`. Invoked only when Ollama fails or confidence is too low.

### 5.3 Prompt Templates

| Function | Region | Key Output Fields |
|---|---|---|
| `label_prompt()` | label_a / label_b | label_name, catalog_number, artist, title, side, speed_rpm, country, year |
| `obi_prompt()` | obi_front / back / spine | japanese_title, romanised_title, english_title, catalog_number, price |
| `runout_prompt()` | runout_a / runout_b | matrix_number, stamper_code, sid_codes, lacquer_cut_info, symbol_detections |

### 5.4 Structured Matrix Breakdown

The `runout_prompt()` returns a structured JSON object that is parsed into the `matrix_runout_parsed` / `matrix_runout_b_parsed` JSONB columns (added in migration 0003). The seven fields are:

| Field | Typical content |
|---|---|
| `matrix_number` | Primary matrix identifier, e.g. `10AA6305231` |
| `stamper_code` | Side-specific stamper/lacquer iteration code, e.g. `1Y` |
| `sid_mastering` | IFPI SID mastering code, e.g. `IFPI L803` |
| `sid_mould` | IFPI SID mould code, e.g. `IFPI M053` |
| `lacquer_cutter` | Lacquer cutter mark or abbreviation, e.g. `Porky` |
| `pressing_plant` | Pressing plant code or name |
| `other_etchings` | Remaining handwritten or stamped text not classified above |

**`_build_full_runout_text(parsed)`** is a helper that reconstructs the full space-separated display string from all non-null field values in dict insertion order. This is used at render time for the detail page header, ensuring that old records with a narrow `matrix_runout` stored value still display the full parsed breakdown correctly without requiring re-analysis.

**Confidence levels** are float values 0.0–1.0 produced by the vision model and stored per field. The UI renders: ≥ 0.70 → green badge ("high"); 0.40–0.69 → amber badge ("mid"); < 0.40 → red badge ("low").

**Source values** indicate how the field was last set:
- `vision` — set by the AI vision pipeline
- `human` — set or corrected by a logged-in user
- `import` — set from a Discogs or MusicBrainz import
- `rule` — set by the OPA/local rule engine
- `null` — not yet populated

### 5.5 Field Correction Audit Trail

Users can correct any individual parsed matrix field without triggering a full re-analysis. The correction endpoint is `POST /catalogue/{id}/correct-matrix-field`.

**Request body:**
```json
{
  "field": "stamper_code",
  "value": "1Y",
  "reason_code": "physical_inspection",
  "notes": "Confirmed under magnification — VLM misread as 1Z"
}
```

**Reason codes (mandatory):**

| Code | Meaning |
|---|---|
| `vision_misread` | The vision model transcribed the text incorrectly |
| `physical_inspection` | Value confirmed by direct examination of the physical disc |
| `cross_reference` | Confirmed by comparison with another copy or pressing |
| `authoritative_source` | From a published reference (Discogs database, collector guide) |
| `other` | Any other reason (requires a note) |

**Audit trail:** Every correction is stored as a new `TokenRevision` row with `source=human` and a `data["correction"]` sub-object containing the field name, old value, new value, reason code, and notes. The token's `matrix_runout_parsed` column is updated in-place and the field's `source` is set to `"human"`. No correction is ever silently discarded.


## 6 · Symbol Registry

### 6.1 Purpose

Runout / dead-wax inscriptions on vinyl records mix plain text with graphical symbols — pressed triangles, stamped stars — that identify pressing plants, mastering engineers, and certifications. The symbol registry gives each mark a stable slug and enables indexed queries.

### 6.2 Taxonomy — Five Levels of Rarity

| Level | Description | Examples |
|---|---|---|
| 1 | Common text content | Matrix numbers, side codes, stamper codes |
| 2 | Common graphical symbols — seeded at install | EMI △, PRS ▽, Capitol ☆, Porky mark |
| 3 | Regional / label-specific | Columbia plant codes, Sterling Sound, Masterdisk |
| 4 | Specialist / Vintage | Western Electric □/◇, Lindström £/ℒ, JIS 〄 |
| 5 | Edge cases — manual entry | Direct-cut indicators, unique handwritten marks |

### 6.3 Seeded Symbols (26 symbols, migration 0002)

26 symbols at levels 2–4 covering UK (EMI, PRS, Pye, Decca), US (Capitol, Columbia, Allied, Sterling, Masterdisk), and global (Western Electric, Lindström, JIS) marks. Full list in `docs/architecture.md`.

### 6.4 Parts Array Format

```json
[
  {"t": "text", "v": "A1 "},
  {"t": "sym",  "slug": "emi-triangle", "id": "<uuid>"},
  {"t": "text", "v": " XZA 1234-1"}
]
```

Stored in `token.matrix_runout_parts` (JSONB). The `token_symbols` table provides a FK index for fast symbol-based queries.


## 7 · Ingestion Connectors

### 7.1 Resilience Mechanisms

Each connector has three independent resilience layers:
- **Token-bucket rate limiter** — per-connector request rate limit
- **Circuit breaker** — opens after N consecutive failures; auto-recovers after timeout
- **Exponential backoff retry** — up to `max_attempts` with configurable backoff

### 7.2 Available Connectors

| Connector | Operations | Rate Limit |
|---|---|---|
| Discogs | `fetch_release(id)`, `search_releases(query)` | 1 req/s (token: 3600/hr) |
| MusicBrainz | `fetch_release(id)`, `search_releases(query)` | 1 req/s; User-Agent required |

### 7.3 Redis Job Queue Keys

| Key | Purpose |
|---|---|
| `mediacat:jobs:pending` | FIFO queue of jobs awaiting processing |
| `mediacat:jobs:processing` | In-flight jobs (dequeued but not yet complete) |
| `mediacat:jobs:dead` | Jobs that exhausted all retry attempts |
| `mediacat:jobs:processing_times` | job_id → dequeue timestamp (staleness detection) |

Jobs use `BLMOVE` for atomic pending → processing transfer.


## 8 · Review Queue

### 8.1 Review Triggers

| Trigger | Reason Code | Description |
|---|---|---|
| Vision / OCR confidence below threshold | `low_confidence` | Confidence < 0.7 |
| Multiple sources disagree | `conflict` | Discogs and MusicBrainz return different values |
| Unknown label or manufacturer | `novel_entity` | Entity not in reference tables |
| LLM anomaly detection | `anomaly` | Drift detector flags unexpected values |
| Manual raise | `manual` | Reviewer triggers re-review via `POST /catalogue/{id}/flag-review` |

### 8.2 Review States

`pending` → `in_progress` → `approved` / `rejected` / `deferred`


## 9 · Authentication & Security

### 9.1 Authentication Flow

1. **Password hashing** — Argon2id (`time_cost=3`, `memory=64 MiB`, `parallelism=2`)
2. **Login rate limiting** — In-memory lockout; 10 failures per username or IP in 15 min (DEF-002: planned Redis migration)
3. **Session creation** — Signed cookie (`itsdangerous.TimestampSigner`); payload = `user_id|role|nonce`; 24 h TTL
4. **CSRF protection** — Per-session HMAC-SHA256 token validated from `X-CSRF-Token` header on all mutating requests
5. **MFA scaffold** — `User.mfa_secret` stores TOTP secret; not yet wired into login flow

### 9.2 User Roles

| Role | Capabilities |
|---|---|
| `admin` | Full access: user management, review, browse, all admin functions |
| `reviewer` | Approve / reject review items; browse tokens |
| `viewer` | Read-only: browse tokens, view revisions |
| `service` | Machine-to-machine: ingestion, OCR, vision |

### 9.3 Database Roles (Least Privilege)

| DB Role | Permissions |
|---|---|
| `postgres` | Superuser — initial setup only |
| `mediacat_migrator` | Owns schema; runs Alembic; CREATE/DROP/ALTER |
| `mediacat_app` | INSERT/SELECT/UPDATE on app tables; no DDL; no UPDATE/DELETE on audit_log |
| `mediacat_readonly` | SELECT only — for reporting |

The `REVOKE UPDATE, DELETE ON audit_log FROM mediacat_app` restriction is enforced at both DB level (in migration 0001) and via an ORM-level guard in `models.py`.

### 9.4 Security Headers (SecurityHeadersMiddleware)

`Content-Security-Policy` · `X-Content-Type-Options: nosniff` · `X-Frame-Options: DENY` · `Referrer-Policy: strict-origin-when-cross-origin` · `Permissions-Policy` · `Strict-Transport-Security` · Server fingerprint removed.

**CSP compliance:** The `script-src 'self'` directive is strictly observed. No inline event handlers (`onclick=`, `onchange=`, etc.) appear in any template. All JavaScript event wiring is contained in external files served from `/static/`:

| File | Purpose |
|---|---|
| `static/theme.js` | Theme toggle; runs synchronously before first paint |
| `static/pressing.js` | Pressing detail and edit: upload zone, image actions, correction modal; loaded on both `pressing_detail.html` and `token_edit.html` |
| `static/forms.js` | Shared form utilities: confirmation dialogs, inline validation |

This means CSP can block all inline script execution without breaking any UI functionality.

### 9.5 Network Security

- Caddy terminates TLS; app binds to `127.0.0.1:8000` only.
- Frontend (public) and backend (internal) Docker networks are isolated.
- All secrets injected via Docker secret files (`/run/secrets/`); never in environment variables.
- Parameterised queries via SQLAlchemy — no raw SQL injection risk.
- Jinja2 auto-escapes HTML — no XSS via template output.

### 9.6 SecretRedactFilter Bug Fix

`logging_filters.py` contains a `SecretRedactFilter` that redacts secret values from log records using regex. A bug existed where the regex was applied to `record.msg` (the format string) even when `record.args` were present. This caused `%s` and other format placeholders to be stripped, resulting in a `TypeError` when the logging machinery later attempted string formatting (e.g. `"value: %s" % (3.14,)` would fail because `%s` had been removed from `record.msg`).

**Fix:** The filter now only redacts `record.msg` when `record.args` is empty or `None`. When `record.args` is populated, redaction is applied to the individual values in `record.args` instead.


## 10 · Object Storage (MinIO)

### 10.1 Buckets

| Bucket | Contents | Key pattern |
|---|---|---|
| `media-originals` | Raw images from Discogs, CoverArtArchive, upload | `<sha256>.<ext>` |
| `ocr-artifacts` | OCR extraction results | `<sha256>.json` |
| `backups` | Daily pg_dump snapshots | `YYYY-MM-DD/<dump>.sql.gz` |

### 10.2 Image Validation

- MIME whitelist: JPEG, PNG, TIFF, WebP, GIF, BMP.
- Pillow verifies structural validity on open.
- Maximum pixel count (178.9 MP) prevents decompression bomb attacks.


## 11 · AI Infrastructure (Ollama)

### 11.1 Overview

Ollama provides local inference for vision (VLM) and text (LLM) tasks. It is optional — the app functions without it using the Anthropic API fallback.

### 11.2 Docker Compose Profiles

Ollama services are in the `ollama` Docker Compose profile. They are not started with `make up`; they require explicit activation:

```bash
make ollama-up       # CPU inference
make ollama-gpu-up   # NVIDIA CUDA GPU inference
make ollama-down     # stop Ollama services
make ollama-models   # list loaded models
```

### 11.3 GPU Configuration (`docker-compose.gpu.yaml`)

```yaml
ollama:
  environment:
    NVIDIA_VISIBLE_DEVICES: all
    NVIDIA_DRIVER_CAPABILITIES: compute,utility
    OLLAMA_NUM_GPU: -1                  # auto-select all GPUs
    OLLAMA_FLASH_ATTENTION: 1           # Flash Attention 2 (Ada/sm_89+)
    OLLAMA_KEEP_ALIVE: 24h
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
```

Tested on NVIDIA RTX 4090 (24 GB GDDR6X, Ada Lovelace, CUDA sm_89). `qwen2.5vl:32b` at Q4_K_M quantisation (~21 GB) loads entirely into VRAM with ~3 GB headroom at 8192-token context.

### 11.4 Model Pull (`scripts/ollama-pull.sh`)

The `ollama-pull` container runs at startup and downloads models if not already present:

```bash
pull_if_missing() {
    local model="$1"
    if ollama list | awk 'NR>1 {print $1}' | grep -qx "${model}"; then
        echo "Model ${model} already present — skipping."
    else
        ollama pull "${model}"
    fi
}
```

> **Key insight:** The `ollama/ollama` image does not contain `curl`. All healthchecks and readiness loops must use `ollama list >/dev/null 2>&1 || exit 1` rather than `curl`.

### 11.5 Network Topology

Ollama and `ollama-pull` are attached to **both** `frontend` and `backend` networks. The `frontend` network provides external internet access (for model registry downloads from `registry.ollama.ai`). The `backend` network allows the app and worker to reach Ollama at `http://ollama:11434`.

> **Key insight:** Containers on `backend` only (with `internal: true`) cannot resolve external DNS and cannot pull models. Adding the `frontend` network to ollama-related services fixes this.

### 11.6 Context Length

`OLLAMA_CONTEXT_LENGTH` (default: 8192) is passed to Ollama at startup. 8192 tokens provides sufficient context for most label/runout transcription tasks while fitting comfortably within 24 GB VRAM on an RTX 4090.

### 11.7 Open WebUI

Open WebUI (`ghcr.io/open-webui/open-webui`) provides a browser-based chat interface for testing models. It proxies through Caddy at `https://ollama.<PUBLIC_HOSTNAME>`.


## 12 · Configuration Reference

### 12.1 `.env` — Key Variables

| Variable | Default | Description |
|---|---|---|
| `MEDIACAT_ENV` | `dev` | Runtime environment (`dev` · `staging` · `prod`) |
| `MEDIACAT_DATA_ROOT` | `/srv/mediacat` | Host path for all volumes and secrets |
| `PUBLIC_HOSTNAME` | `mediacat.localhost` | Browser-visible hostname |
| `PUBLIC_SCHEME` | `https` | `https` for Caddy TLS; `http` for plain |
| `HTTP_BIND` | `127.0.0.1:8080` | Caddy HTTP listener |
| `HTTPS_BIND` | `127.0.0.1:8443` | Caddy HTTPS listener |
| `MEDIACAT_DEV_ADMIN_PASSWORD` | *(blank)* | Seeds dev admin user at startup (MEDIACAT_ENV=dev only) |
| `TZ` | `Europe/London` | Container timezone |
| `MEDIACAT_OLLAMA` | `0` | `1` to enable the Ollama Compose profile |
| `MEDIACAT_OLLAMA_GPU` | `0` | `1` to add the GPU override file |
| `OLLAMA_VLM_MODEL` | `qwen2.5vl:32b` | Vision/language model to pull |
| `OLLAMA_OCR_MODEL` | `glm-ocr` | OCR-specialist model to pull |
| `OLLAMA_CONTEXT_LENGTH` | `8192` | Ollama context window (tokens) |
| `OLLAMA_FLASH_ATTENTION` | `1` | Enable Flash Attention 2 |

> **Critical — `--env-file .env`:** Docker Compose v2 resolves `.env` relative to the compose file's directory (`deploy/`), not the working directory. When running `docker compose` manually from the repo root, always pass `--env-file .env` explicitly. The `make` targets and `scripts/dev-up.sh` handle this automatically.

### 12.2 `config/app.yaml` — Key Sections

| Section | Key Settings |
|---|---|
| `app` | name, environment, log_level |
| `security` | session_secret (overridden by Docker secret), cookie_secure, login lockout thresholds |
| `postgres` | host, port, user, database, password_file (`/run/secrets/postgres_app_password`) |
| `object_store` | endpoint, access_key, secret_key_file (`/run/secrets/minio_root_password`) |
| `redis` | url template, password_file (`/run/secrets/redis_password`) |
| `vision` | backend=hybrid, primary=local_vlm, ollama_url=`http://ollama:11434`, fallback=anthropic |
| `llm` | backend=hybrid, primary=local, fallback=anthropic |

### 12.3 Docker Secrets — Permission Requirements

| Secret File | Mode | Reason |
|---|---|---|
| `postgres_password` | `0600` | Read by PostgreSQL init entrypoint running as root |
| `postgres_app_password` | `0644` | Read by app container (uid 10001); must be world-readable |
| `minio_root_password` | `0644` | Read by app container (uid 10001) |
| `redis_password` | `0644` | Read by app container (uid 10001) |

> **Why:** Docker bind-mount secrets preserve the host file permissions. The app and worker containers run as uid 10001 (non-root). Secret files left at `0600` (root-only) cause `PermissionError` at runtime even though Docker has mounted the file.


## 13 · Deployment — Docker Compose

### 13.1 Compose File Structure

| File | Purpose |
|---|---|
| `deploy/docker-compose.yaml` | Base stack — all 8 services |
| `deploy/docker-compose.dev.yaml` | Dev overrides — debug ports, reload |
| `deploy/docker-compose.gpu.yaml` | NVIDIA GPU reservation for Ollama |

### 13.2 Caddy Configuration

The `Caddyfile` uses environment variable substitution for all site addresses:

```
{$PUBLIC_SCHEME:-https}://{$PUBLIC_HOSTNAME:-mediacat.localhost}
```

This pattern allows switching between HTTP (dev, no cert) and HTTPS (local CA or Let's Encrypt) without editing the file.

**Local development HTTPS:** Caddy generates its own local CA for `.localhost` domains. The root certificate must be imported into the Windows certificate store once:

```powershell
# [PS-Admin] — run after extracting cert from Caddy container:
Import-Certificate -FilePath "$env:USERPROFILE\caddy-root.crt" `
    -CertStoreLocation Cert:\LocalMachine\Root
```

### 13.3 WSL2 Networking

For Windows browsers to reach Docker services running in WSL2, enable mirrored networking in `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

With mirrored networking, ports bound to `127.0.0.1` inside WSL2 are directly accessible from Windows at `127.0.0.1` — no `hosts` file entry needed. Restart WSL (`wsl --shutdown`) after changing this file.

> **Why not NAT mode:** In the default NAT networking mode, Windows and WSL2 have separate IP addresses. Services in WSL2 are not reachable at `127.0.0.1` from Windows — you would need `localhost` port forwarding or the WSL2 IP address. Mirrored mode eliminates this complexity.

### 13.4 Service Health Checks

| Service | Health check |
|---|---|
| `postgres` | `pg_isready -U postgres` |
| `redis` | `redis-cli -a <password> ping` |
| `minio` | `mc ready local` |
| `app` | `curl -f http://127.0.0.1:8000/healthz` |
| `ollama` | `ollama list >/dev/null 2>&1` (no curl in ollama image) |


## 14 · API Endpoints

All mutating endpoints require a valid session cookie and an `X-CSRF-Token` header.

### 14.1 Public (No Auth)

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/readyz` | Readiness probe |
| `GET` | `/login` | Login form |
| `POST` | `/login` | Authenticate — creates session cookie |
| `GET` | `/logout` | Clear session cookie |

### 14.2 Dashboard (Auth Required)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard — stats, pending reviews |

### 14.3 Catalogue (Auth Required)

| Method | Path | Description |
|---|---|---|
| `GET` | `/catalogue` | Artist browser |
| `GET` | `/catalogue/new` | Create-pressing form |
| `POST` | `/catalogue/new` | Create pressing |
| `GET` | `/catalogue/search` | External search (Discogs/MusicBrainz) |
| `GET` | `/catalogue/merge` | Merge UI |
| `POST` | `/catalogue/merge` | Execute merge |
| `POST` | `/catalogue/scan-cover` | Scan barcode from cover photo; redirect to pre-filled form |
| `GET` | `/catalogue/import/discogs/{release_id}` | Preview Discogs release |
| `POST` | `/catalogue/import/discogs/{release_id}` | Create Token from Discogs release |
| `GET` | `/catalogue/import/musicbrainz/{release_id}` | Preview MusicBrainz release |
| `POST` | `/catalogue/import/musicbrainz/{release_id}` | Create Token from MusicBrainz release |
| `GET` | `/catalogue/artists/{artist}/albums` | Albums for artist (HTMX) |
| `GET` | `/catalogue/artists/{artist}/albums/{title}/pressings` | Pressings for album (HTMX) |
| `GET` | `/catalogue/{token_id}` | Pressing detail |
| `GET` | `/catalogue/{token_id}/edit` | Edit form |
| `POST` | `/catalogue/{token_id}/edit` | Save edit |
| `POST` | `/catalogue/{token_id}/archive` | Archive pressing |
| `POST` | `/catalogue/{token_id}/delete` | Soft-delete pressing |
| `POST` | `/catalogue/{token_id}/flag-review` | Flag pressing for review queue |
| `POST` | `/catalogue/{token_id}/refresh-discogs` | Re-fetch images from Discogs |
| `POST` | `/catalogue/{token_id}/images` | Upload one or more images |
| `GET` | `/catalogue/{token_id}/images/{image_id}` | Serve image bytes from MinIO |
| `POST` | `/catalogue/{token_id}/images/{image_id}/region` | Reassign image region label |
| `POST` | `/catalogue/{token_id}/images/{image_id}/analyse` | Run vision analysis synchronously |
| `POST` | `/catalogue/{token_id}/images/{image_id}/apply-ocr` | Apply OcrArtifact to token fields |
| `POST` | `/catalogue/{token_id}/images/{image_id}/delete` | Hard-delete image |
| `POST` | `/catalogue/{token_id}/images/{image_id}/set-cover` | Set as primary cover |
| `POST` | `/catalogue/{token_id}/correct-matrix-field` | Correct a single parsed matrix field |

### 14.4 Review Queue (Reviewer+ Role)

| Method | Path | Description |
|---|---|---|
| `GET` | `/reviews` | List review items |
| `GET` | `/reviews/{id}` | Review detail with diff |
| `POST` | `/reviews/{id}/approve` | Approve revision |
| `POST` | `/reviews/{id}/reject` | Reject revision |

### 14.5 Tokens (Legacy Browse)

| Method | Path | Description |
|---|---|---|
| `GET` | `/tokens` | Token list (search/filter) |
| `GET` | `/tokens/{token_id}` | Token detail |

### 14.6 User Management (Admin Role)

| Method | Path | Description |
|---|---|---|
| `GET` | `/users` | List users |
| `GET` | `/users/new` | Create-user form |
| `POST` | `/users/new` | Create user |
| `POST` | `/users/{user_id}/activate` | Activate user |
| `POST` | `/users/{user_id}/deactivate` | Deactivate user |


## 15 · Alembic Migrations

```bash
# Apply all pending migrations (inside running container)
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec app python -m alembic -c alembic/alembic.ini upgrade head

# Roll back one migration
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec app python -m alembic -c alembic/alembic.ini downgrade -1

# Show current revision
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec app python -m alembic -c alembic/alembic.ini current
```

### Migration 0001 — initial_schema

- 9 PostgreSQL ENUM types; extensions: `uuid-ossp`, `pg_trgm`, `btree_gist`
- 11 application tables
- Trigram GIN indexes on `labels.name_normalised`, `manufacturers.name_normalised`, `tokens.title`, `tokens.artist`
- Partial index on `review_items WHERE status = 'pending'`
- `REVOKE UPDATE, DELETE ON audit_log FROM mediacat_app`

### Migration 0002 — symbols

- `symbol_category` ENUM (6 values)
- Tables: `symbols`, `symbol_variants`, `token_symbols`
- New columns on `tokens`: `matrix_runout_b`, `matrix_runout_parts`, `matrix_runout_b_parts`
- New column on `ocr_artifacts`: `symbol_candidates`
- Seed data: 26 confirmed symbols (levels 2–4)

### Migration 0003 — matrix_parsed

- Two new `JSONB` columns on `tokens`:
  - `matrix_runout_parsed` — structured 7-field breakdown of side A runout etchings
  - `matrix_runout_b_parsed` — same for side B
- Each column stores a dict keyed by: `matrix_number`, `stamper_code`, `sid_mastering`, `sid_mould`, `lacquer_cutter`, `pressing_plant`, `other_etchings`
- Each value: `{"value": str|null, "confidence": float|null, "source": "vision"|"human"|"import"|"rule"|null}`
- Default: `NULL` (populated on first vision analysis or import)

### Migration 0004 — media_primary_cover

- New `BOOLEAN NOT NULL DEFAULT FALSE` column `media_objects.is_primary_cover`
- Enables explicit primary cover designation independent of `image_region`
- Cover selection logic: `is_primary_cover = TRUE` takes precedence over `region = cover_front`
- Supports Japanese OBI and similar multi-image cover scenarios


## 16 · Make Targets

| Target | Category | Description |
|---|---|---|
| `setup` | Setup | venv + install deps + pre-commit hooks |
| `data-init` | Setup | Create/verify host data directory tree |
| `lint` | Quality | Ruff linter |
| `format` | Quality | Ruff formatter |
| `typecheck` | Quality | Mypy strict |
| `security` | Quality | Bandit + pip-audit |
| `test` | Quality | Full pytest suite with coverage |
| `test-fast` | Quality | Pytest excluding slow/integration markers |
| `up` | Docker | Start all 8 services (background) |
| `down` | Docker | Stop; data volumes preserved |
| `restart` | Docker | down + up |
| `logs` | Docker | Tail all service logs |
| `ps` | Docker | Show container status |
| `ollama-up` | Ollama | Start Ollama + Open WebUI (CPU) |
| `ollama-gpu-up` | Ollama | Start Ollama + Open WebUI (NVIDIA GPU) |
| `ollama-down` | Ollama | Stop Ollama services |
| `ollama-models` | Ollama | List loaded models and GPU state |
| `docs-build` | Docs | Build MkDocs site to `site/` |
| `docs-serve` | Docs | Live-preview docs on `http://127.0.0.1:8800` |
| `clean` | Cleanup | Remove build artefacts and caches |
| `distclean` | Cleanup | clean + remove `.venv/` |


## 17 · Open Items & Roadmap

### 17.1 Security Defects (Tracked)

| ID | Severity | Description | Effort | Status |
|---|---|---|---|---|
| DEF-001 | MEDIUM | Wire `sanitise()` into translation pipeline before LLM call (prompt injection risk from scanned labels) | 1 hour | Open |
| DEF-002 | LOW | Migrate `LoginRateLimiter` from in-memory dict to Redis (state lost on restart; not cluster-safe) | 0.5 day | Open |

> **DEF-003 (REVOKE audit_log) — closed.** `REVOKE UPDATE, DELETE ON audit_log FROM mediacat_app` is enforced at DB level in migration 0001 and via an ORM-level guard in `models.py`. No further action required.

### 17.2 Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| ADR-0001 | Media scope: vinyl + CD only (v1) | Accepted |
| ADR-0002 | Hybrid AI: local Ollama primary, API fallback | Accepted |
| ADR-0003 | Rule engine: OPA primary, Python local fallback | Accepted |
| ADR-0004 | Data/code separation via host bind mounts | Accepted |
| ADR-0005 | Web stack: FastAPI + Jinja2 + HTMX (no JS framework) | Accepted |

### 17.3 Near-term Roadmap

| Priority | Item | Status |
|---|---|---|
| P1 | Fix DEF-001 `sanitise()` before LLM calls | Open |
| P1 | Fix DEF-003 audit_log REVOKE at DB level | **Done (v0.3)** |
| P1 | Full image management (upload, delete, primary cover) | **Done (v0.3)** |
| P1 | Structured matrix breakdown with AI analysis and field correction | **Done (v0.3)** |
| P1 | CSP compliance: remove all inline event handlers | **Done (v0.3)** |
| P2 | DEF-002 Redis-backed login rate limiter | Open |
| P2 | Phase 2 symbol pipeline: focused re-run with slug hints from review | Open |
| P2 | LLM comparison / anomaly detection module (`llm/tasks.py`) | Open |
| P3 | MFA TOTP login flow (scaffold already in User model) | Open |
| P3 | CLIP embedding similarity for symbol Phase 3 | Open |
| P3 | Horizontal scaling: multiple worker containers on same Redis queue | Open |
| P3 | Crop tool: drag-select region on image, save as new MediaObject | Open |


## 18 · Usage Guide

### 18.1 Cataloguing a New Record

1. **Navigate to Catalogue** → **Add pressing** (`/catalogue/new`).
2. Fill in artist, title, year, format, catalogue number, label, matrix runout.
3. Optionally search Discogs or MusicBrainz to pre-fill fields — or click a search result's **Import** button to go straight to `GET /catalogue/import/discogs/{id}` for a preview before creating.
4. Alternatively, **scan a cover barcode** (`POST /catalogue/scan-cover`) to auto-fill the form from a photo of the barcode.
5. Submit — the pressing appears in the catalogue immediately.
6. Upload scans on the pressing detail page or directly on the edit form (drag-and-drop or file picker).
7. For runout images, click **Analyse with AI** to extract structured matrix fields.
8. Review the breakdown table; click **Apply to token** to write the parsed fields into the token.
9. Correct any mis-read fields using the per-field edit button (see [18.8](#188-vision-analysis-and-matrix-parsing-workflow)).
10. Review any AI proposals in `/reviews` — approve or reject each field update.
11. Confirm any symbol detections in the runout images.

### 18.2 Browsing the Catalogue

- Navigate to `/catalogue` — artists are grouped alphabetically.
- Click an artist to expand its albums; click an album to expand its pressings.
- Click any pressing to see the full detail page with revision history.
- Use the search bar to filter by artist/title; use the format dropdown for vinyl or CD.
- Use the Discogs/MusicBrainz search panel to look up releases externally.

### 18.3 Editing and Archiving

- On any pressing detail page, click **Edit** to open the form.
- Every save creates a new `TokenRevision` with `source=human` — nothing is lost.
- Click **Archive** to mark a pressing as archived (hidden from browse by default).
- Archived pressings can be found by searching with `status=archived`.
- Click **Delete** on the pressing detail page to soft-delete (sets `deleted_at`; recoverable via DB).
- Click **Flag for review** to manually send a pressing to the review queue.

### 18.4 Merging Duplicate Pressings

1. Navigate to `/catalogue/merge` (or click **Merge into…** from a detail page).
2. Paste or confirm the **winner** token ID (the pressing you want to keep).
3. Paste the **loser** token ID (the duplicate to retire).
4. Add an optional comment explaining why these are duplicates.
5. Click **Merge pressings** — the loser is marked `status=merged` and a merge revision is added to the winner's history.

The loser is never hard-deleted; it remains in the database and can be recovered by directly updating its status if needed.

### 18.5 Symbol Workflow

1. Upload a runout image scan on the pressing detail page.
2. The vision pipeline proposes `symbol_detections` in the review queue.
3. In the review item, verify the `slug_suggestion` against the [symbol registry](#6-symbol-registry).
4. Approve to add the symbol to the token's `matrix_runout_parts` array and index it in `token_symbols`.
5. For new symbols not in the registry, create them via the admin shell (see Appendix), then re-review.

### 18.6 Backup and Restore

```bash
# Manual backup (pg_dump to MinIO backups bucket)
deploy/scripts/backup.sh

# Restore from backup
docker compose ... stop app worker
# restore PostgreSQL dump into the postgres container
docker compose ... start app worker
docker compose ... exec app python -m alembic -c alembic/alembic.ini upgrade head
```

### 18.7 Managing Images

#### Uploading

Images can be uploaded from two places:
- **Detail page** (`/catalogue/{id}`) — the image gallery section includes a drag-and-drop upload zone.
- **Edit form** (`/catalogue/{id}/edit`) — the same gallery and upload zone are also embedded in the edit page, so you do not need to navigate away to upload new scans while editing metadata.

Drag one or more files onto the drop zone, or click the zone to open a file picker. Accepted types: JPEG, PNG, TIFF, WebP. Maximum 30 MB per file. Progress indicators appear per file; the gallery updates automatically when uploads complete.

#### Assigning a Region

Each image has a **region label** (e.g. `label_a`, `runout_a`, `cover_front`, `obi_front`). To change it, use the region dropdown on the image card and submit. If you reassign an image to a runout region (`runout_a`, `runout_b`, `matrix`), the vision pipeline is automatically triggered in the background.

#### Deleting an Image

Click the **Delete** button on an image card. A confirmation dialog appears. On confirmation, the `MediaObject` row is removed from the database and the corresponding object is deleted from MinIO. This action is not reversible.

#### Setting the Primary Cover

Click **Set as cover** on any image card. The selected image is flagged `is_primary_cover = TRUE` and all other images for this token have the flag cleared. The button changes to **★ Primary cover** to indicate the current selection.

Use this to designate a specific image as the catalogue thumbnail — for example, to use the OBI strip front image as the cover for a Japanese pressing rather than the bare album sleeve.

### 18.8 Vision Analysis and Matrix Parsing Workflow

1. Upload a runout scan (side A or side B) and assign region `runout_a` or `runout_b`.
2. Click **Analyse with AI** on the image card. The analysis runs synchronously (~5–15 seconds) and the structured breakdown table appears on the page without a full reload.
3. Review the breakdown table:
   - Each field shows its value, a confidence badge (high / mid / low), and a source badge (AI / human / import).
   - Fields with low confidence (red badge) should be verified against the physical disc.
4. Click **Apply to token** to write the parsed fields into `matrix_runout` (plain text) and `matrix_runout_parsed` (structured JSONB) on the token. A new `TokenRevision` is created with `source=vision`.
5. To correct an individual field:
   a. Click the **Edit** button next to the field.
   b. A modal appears with the current value, a corrected-value input, a mandatory **Reason** dropdown, and an optional **Notes** field.
   c. Select a reason code (e.g. `physical_inspection` — confirmed under magnification).
   d. Enter the correct value and submit.
   e. The field updates in-place; its source badge changes to **✓ human**. A `TokenRevision` is created recording the correction with the full reason code and notes.

The `_build_full_runout_text()` helper reconstructs the display string from all parsed fields at render time, so the header line always shows the complete runout text regardless of which fields have been manually corrected or which were set by an older narrower analysis.


*MediaCat · Technical Reference · v0.3 · April 2026*
