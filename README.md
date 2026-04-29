# MediaCat

A cataloguing platform for physical music media — vinyl records and compact discs.

Token-object registry with provenance, multi-source ingestion (Discogs, MusicBrainz),
vision-assisted transcription of labels, obi strips, and runout etchings,
country-aware symbol decoding, human-in-the-loop review, and LLM support for
comparison, anomaly detection, translation (British English), and text generation.

## Key capabilities

- **Catalogue browser** — Artist → Album → Pressing hierarchy with HTMX drill-down; create, edit, archive, merge, soft-delete, and flag pressings for review
- **Direct import** — Create a Token directly from a Discogs or MusicBrainz release ID with a one-click preview-and-import flow; scan a barcode from a cover photo to pre-fill the form
- **Image management** — Drag-and-drop upload on both the detail page and the edit form; assign region labels; delete images; primary cover selection (is_primary_cover flag) for Japanese OBI and similar multi-image cases
- **Structured matrix breakdown** — Vision analysis decomposes runout etchings into 7 typed fields (matrix number, stamper code, SID mastering/mould codes, lacquer cutter, pressing plant, other etchings) with per-field confidence scores and source tracking
- **Inline field correction** — Correct any individual parsed matrix field with a mandatory reason code (vision_misread, physical_inspection, cross_reference, authoritative_source, other) and optional notes; every correction is stored as a new TokenRevision for full audit trail
- **External search** — Discogs and MusicBrainz lookup directly from the catalogue UI
- **Revision history** — Every change is a new `TokenRevision`; nothing is overwritten
- **Vision pipeline** — Ollama (local-first) or Anthropic API transcribes labels, OBI strips, runout etchings; can be triggered interactively per image or automatically on upload/region reassignment
- **Symbol registry** — 26 seeded runout / dead-wax symbols (EMI △, PRS ▽, Porky, etc.) with slug-based indexing
- **Review queue** — All AI proposals land here first; humans approve before any token is updated
- **Dark / light UI** — Netflix-inspired dark theme (default) with persistent toggle
- **Security hardened** — Argon2id passwords, CSRF protection, non-root containers, least-privilege DB roles, strict `script-src 'self'` CSP with no inline event handlers

## Technology stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Web | FastAPI + Jinja2 + HTMX (no JS framework) |
| Database | PostgreSQL 16 (pg_trgm, uuid-ossp) |
| Object storage | MinIO (S3-compatible, SHA-256 dedup) |
| Job queue | Redis 7 (BLMOVE atomic dequeue) |
| Rule engine | Open Policy Agent + Python fallback |
| Vision / LLM | Ollama (local-first, CUDA) — Anthropic API fallback |
| Reverse proxy | Caddy (automatic TLS) |
| Deployment | Docker Compose on WSL2 |

## Quick start (Windows 11 / WSL2)

Full instructions: `MediaCat_Installation_Guide.md`

```powershell
# [PS-Admin] 1. Prepare WSL2 and Ubuntu
.\scripts\wsl2-prepare.ps1
```

```bash
# [WSL-User] 2. From the repository root
./scripts/ubuntu-bootstrap.sh   # install system deps and Docker
make setup                       # Python venv + pre-commit hooks
./scripts/data-init.sh           # create host data tree
./scripts/secrets-init.sh        # generate random secrets
cp .env.example .env && nano .env
cp config/app.example.yaml config/app.yaml
make up                          # start all 8 Docker services
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec app python -m alembic -c alembic/alembic.ini upgrade head
```

Open `https://mediacat.localhost` in your browser.
Default admin credentials are set via `MEDIACAT_DEV_ADMIN_PASSWORD` in `.env`.

## Optional: Ollama / local AI (GPU recommended)

```bash
# Copy the GPU override and enable the Ollama profile
cp .env.example .env   # if not already done
# Set in .env:  MEDIACAT_OLLAMA=1  MEDIACAT_OLLAMA_GPU=1
make ollama-gpu-up     # starts Ollama + Open WebUI, pulls qwen2.5vl:32b + glm-ocr
```

Open WebUI is available at `https://ollama.mediacat.localhost`.

## Design invariants

1. **Data/code separation** — All persistent state lives under `MEDIACAT_DATA_ROOT` on the host; containers are ephemeral.
2. **Secrets never in the image, never in Git** — Sourced from Docker secret files (`/run/secrets/`).
3. **Security by default** — Non-root containers, read-only rootfs, least-privilege DB roles, strict CSP, Argon2id, CSRF.
4. **Advisory-only AI** — Vision and LLM models propose; humans always confirm via the review queue.
5. **Append-only revisions** — Every change creates a new `TokenRevision`; historical records are never overwritten.

## Project layout

```
mediacat/
├── scripts/          Bootstrap & lifecycle scripts
├── config/           Non-secret YAML config (examples checked in; live files host-mounted)
├── deploy/           Dockerfiles, Docker Compose, Caddy, OPA bundles, backup scripts
├── alembic/          Database migrations (0001 initial schema, 0002 symbol registry, 0003 matrix parsed, 0004 primary cover)
├── src/mediacat/     Python application package
│   ├── db/           ORM models, enums, migrations engine
│   ├── web/          FastAPI app, routes, auth, middleware, templates, static assets
│   ├── vision/       VLM adapter, task prompts, candidate matcher
│   ├── llm/          LLM adapter, tasks, safety, Ollama & Anthropic backends
│   ├── ingestion/    Connector base, Discogs, MusicBrainz, Redis queue, drift detector
│   ├── rules/        OPA adapter, local Python fallback
│   └── storage/      MinIO wrapper, image pipeline, OCR, translation
├── tests/            Pytest suite
└── docs/             ADRs, runbooks, MkDocs site
```

Persistent data (outside the repo, default `/srv/mediacat`):

```
${MEDIACAT_DATA_ROOT}/
├── postgres/         PGDATA (uid 70)
├── minio/            Object store data
├── redis/            AOF + RDB (uid 999)
├── secrets/          0700, root-owned — Docker reads secrets from here
├── backups/          pg_dump + mc mirror output
├── logs/             Optional host-mounted logs
└── open-webui/       Open WebUI data (if Ollama enabled)
```

## Common make targets

| Target | Description |
|---|---|
| `make setup` | Create venv, install deps, install pre-commit hooks |
| `make lint` / `make typecheck` / `make test` | Quality gates |
| `make up` / `make down` | Docker Compose lifecycle |
| `make data-init` | Create the host data tree with correct permissions |
| `make ollama-up` / `make ollama-gpu-up` | Start Ollama stack (CPU / CUDA) |
| `make ollama-models` | Show loaded models and GPU status |

## Documentation

- `MediaCat_Installation_Guide.md` — Step-by-step installation with troubleshooting
- `MediaCat_Technical_Reference.md` — Architecture, schema, API, configuration reference
- `docs/architecture.md` — High-level architecture and data flow
- `docs/adr/` — Architecture Decision Records
- `docs/runbooks/` — Operational procedures

## Licence

Proprietary. Dependencies retain their upstream licences.
