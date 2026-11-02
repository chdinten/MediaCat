# MediaCat

A cataloguing platform for physical music media — vinyl records and compact discs.

Token-object registry with provenance, multi-source ingestion (Discogs, MusicBrainz),
vision-assisted transcription of labels, obi strips, and runout etchings,
country-aware symbol decoding, human-in-the-loop review, and LLM support for
comparison, anomaly detection, translation (British English), and text generation.

## Key capabilities

- **Catalogue browser** — Artist → Album → Pressing hierarchy with HTMX drill-down; create, edit, archive, merge, soft-delete, and flag pressings for review
- **Artists tab** — Grid of all artists with portrait images (Wikipedia), pressing counts, and year ranges; click-through to album list, biography (Wikipedia via MusicBrainz or LLM-verified search → Discogs profile if clearly fuller → official homepage AI summary → "not found" statement; looked up in the background with a progress timer), and discography (MusicBrainz primary → Wikipedia fallback, enriched with Spotify album URLs, cached daily); Spotify links appear in the discography list and on the pressing detail page
- **Direct import** — Create a Token directly from a Discogs or MusicBrainz release ID with a one-click preview-and-import flow; scan a barcode from a cover photo to pre-fill the form
- **Bulk collection import** — Import an entire Discogs user collection via the Admin panel; paginated, resumable, skip-existing-by-default, with live progress tracking and per-release error recovery
- **Image management** — Drag-and-drop upload on both the detail page and the edit form; assign region labels; delete images; primary cover selection (is_primary_cover flag) for Japanese OBI and similar multi-image cases
- **Structured matrix breakdown** — Vision analysis decomposes runout etchings into 7 typed fields (matrix number, stamper code, SID mastering/mould codes, lacquer cutter, pressing plant, other etchings) with per-field confidence scores and source tracking
- **Inline field correction** — Correct any individual parsed matrix field with a mandatory reason code (vision_misread, physical_inspection, cross_reference, authoritative_source, other) and optional notes; every correction is stored as a new TokenRevision for full audit trail
- **External search** — Discogs and MusicBrainz lookup directly from the catalogue UI
- **Global search** — Top-bar search with a scope picker: artists and album titles / catalogue numbers together, or artists, albums or song titles (tracks) on their own; album and track hits open the artist and the pressing; Ctrl+K palette with `/album` and `/track`
- **Discogs pressing details** — Release identifiers grouped by pressing variant and LP side; pick a variant, tick matrix codes and copy them into *My copy* (your Discogs collection fields: runout per LP side, autographed, signed by…), with write-back to Discogs; A4 album sheet PDF; image lightbox; banner while a Discogs import runs
- **Revision history** — Every change is a new `TokenRevision`; nothing is overwritten
- **Vision pipeline** — Ollama (local-first) or Anthropic API transcribes labels, OBI strips, runout etchings; can be triggered interactively per image or automatically on upload/region reassignment
- **Symbol registry** — 26 seeded runout / dead-wax symbols (EMI △, PRS ▽, Porky, etc.) with slug-based indexing
- **Review queue** — All AI proposals land here first; humans approve before any token is updated
- **Ratings & genres** — Personal rating (0–10), Discogs community rating/votes, JSONB genre tags, full tracklist storage per token
- **Price history** — Discogs market data snapshots per pressing (lowest price, median, num for sale)
- **Admin panel** — Tabbed interface for user management (create/edit/role/timeout), bulk Discogs collection import, catalogue ZIP backup/restore, and debug info
- **3panel-style layout** — Optional 3-column UI (selectable in Admin → Layout): persistent left panel (artist/search list), wide centre (detail), narrow right (pressing brief); panel widths drag-resizable and memorised per-page in localStorage; global top-bar search switches to artist-panel view; classic single-column layout retained as alternative
- **Late Night Hi-Fi UI** — Dark-first theme with hero dashboard, stats overlay, genre counts, and oldest-year badge; `[data-theme="light"]` override available
- **Security hardened** — Argon2id passwords, Redis-backed login rate limiter, CSRF protection with dedicated secret, trusted-proxy IP validation, RBAC on all mutating routes, non-root containers, least-privilege DB roles, strict `script-src 'self'` CSP with no inline event handlers

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
    exec -e PGHOST=postgres -e PGUSER=mediacat_migrator -e PGDATABASE=mediacat \
    app alembic -c alembic.ini upgrade head
```

Open **`https://mediacat.localhost:8443/`** in your browser. Mind the port: Caddy publishes HTTPS on `HTTPS_BIND` (default `127.0.0.1:8443`), and on this machine port 443 belongs to Kasm Workspaces.
Default admin credentials are set via `MEDIACAT_DEV_ADMIN_PASSWORD` in `.env`.

## Optional: Ollama / local AI (GPU recommended)

```bash
# Copy the GPU override and enable the Ollama profile
cp .env.example .env   # if not already done
# Set in .env:  MEDIACAT_OLLAMA=1  MEDIACAT_OLLAMA_GPU=1
make ollama-gpu-up     # starts Ollama + Open WebUI, pulls qwen2.5vl:32b + glm-ocr
```

Open WebUI is available at `https://ollama.mediacat.localhost:8443`.

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
├── alembic/          Database migrations (0001–0014: schema, symbols, matrix, covers, genres/ratings, contributors, price history, vinyl material, artist bios, countries, site settings, Spotify URL)
├── src/mediacat/     Python application package
│   ├── db/           ORM models, enums, migrations engine
│   ├── web/          FastAPI app, routes, auth, middleware, templates, static assets
│   ├── vision/       VLM adapter, task prompts, candidate matcher
│   ├── llm/          LLM adapter, tasks, safety, Ollama & Anthropic backends
│   ├── ingestion/    Connector base, Discogs, MusicBrainz, Spotify, Wikipedia, Redis queue, drift detector
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
├── backups/          Daily pg_dump (02:00 UTC, 30-day retention)
├── logs/             Optional host-mounted logs
└── open-webui/       Open WebUI data (if Ollama enabled)
```

## Stack manager script

`mediacat/scripts/mediacat-ctl.sh` is an interactive terminal menu for managing the
Docker Compose stack from WSL2 — a convenient alternative to memorising individual
`docker compose` commands.

Copy it to your home directory, set `PROJ_DIR` on line 11 to your local path, make it
executable, and run it:

```bash
cp mediacat/scripts/mediacat-ctl.sh ~/mediacat-ctl.sh
# edit PROJ_DIR="/mnt/c/path/to/your/mediacat" on line 11
chmod +x ~/mediacat-ctl.sh
~/mediacat-ctl.sh
```

The menu covers 41 operations in seven groups: start/stop/restart, image rebuilds,
per-service restarts, database migrations, logs, docs server, and cleanup.
Dangerous options (catalogue wipe, volume removal, full system prune) require an explicit `y` confirmation.
Full option reference: `mediacat/scripts/mediacat-ctl.md`.

**Web control panel.** The same menu is also a web page: run `make ctl-web` in `mediacat/` and
open `http://127.0.0.1:8810/`. It shows container status, has a prominent link to the app, and runs
each action as `mediacat-ctl.sh --run N` with live output, so both always run the same commands.

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
