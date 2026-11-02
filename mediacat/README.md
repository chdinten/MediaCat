# MediaCat

A cataloguing platform for physical music media — vinyl records and compact discs.

Token-object registry with provenance, multi-source ingestion (Discogs, MusicBrainz),
vision-assisted transcription of labels, obi strips, and runout etchings,
country-aware symbol decoding, human-in-the-loop review, and LLM support for
comparison, anomaly detection, translation (British English), and text generation.

## Key capabilities

- **Catalogue browser** — Artist → Album → Pressing hierarchy with HTMX drill-down; create, edit, archive, merge, soft-delete, and flag pressings for review
- **Artists tab** — Grid of all artists with portrait images (Wikipedia), pressing counts, and year ranges; click-through to album list, biography (Wikipedia via MusicBrainz or LLM-verified search → Discogs profile if clearly fuller → official homepage AI summary → "not found" statement; looked up in the background with a progress timer), and discography (MusicBrainz primary → Wikipedia fallback, enriched with Spotify album URLs, cached daily); Spotify links appear in the discography list and on the pressing detail page
- **Direct import** — Create a Token from a Discogs or MusicBrainz release ID with a one-click preview-and-import flow; scan a barcode to pre-fill
- **Bulk collection import** — Import an entire Discogs user collection via the Admin panel; resumable, skip-existing-by-default, with live progress tracking
- **External search** — Discogs and MusicBrainz lookup directly from the catalogue UI
- **Global search** — Top-bar search with a scope picker: artists and album titles / catalogue numbers together, or artists, albums or song titles (tracks) on their own; album and track hits open the album page (a single hit opens it directly); Ctrl+K palette with `/album` and `/track`
- **Discogs pressing details** — Release identifiers grouped by pressing variant and LP side; pick a variant, tick matrix codes and copy them into *My copy* (your Discogs collection fields: runout per LP side, autographed, signed by…), with write-back to Discogs; A4 album sheet PDF; image lightbox; banner while a Discogs import runs
- **Revision history** — Every change is a new `TokenRevision`; nothing is overwritten
- **Vision pipeline** — Ollama (local-first) or Anthropic API transcribes labels, OBI strips, runout etchings; matrix breakdown into 7 typed fields with confidence scores
- **Symbol registry** — 26 seeded runout / dead-wax symbols (EMI △, PRS ▽, Porky, etc.) with slug-based indexing
- **Review queue** — All AI proposals land here first; humans approve before any token is updated
- **Ratings & genres** — Personal rating (0–10), Discogs community rating/votes, genre tags, tracklist, price history
- **Admin panel** — Tabbed: user management, bulk collection import, catalogue ZIP backup/restore
- **3panel-style layout** — Optional 3-column UI (Admin → Layout): left panel scrolls artists or search results, centre shows artist detail (bio + collection + discography), right shows a pressing brief on album click; panels are drag-resizable with per-page localStorage persistence; classic layout retained as alternative
- **Late Night Hi-Fi UI** — Dark-first theme with hero dashboard, genre/year stats; light theme toggle
- **Security hardened** — Argon2id passwords, Redis-backed rate limiter, CSRF with dedicated secret, trusted-proxy IP validation, RBAC on all mutating routes, non-root containers, least-privilege DB roles, strict CSP

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

`scripts/mediacat-ctl.sh` is an interactive terminal menu for managing the
Docker Compose stack from WSL2. Copy it to your home directory, set `PROJ_DIR`
on line 11, make it executable, and run it:

```bash
cp scripts/mediacat-ctl.sh ~/mediacat-ctl.sh
chmod +x ~/mediacat-ctl.sh
~/mediacat-ctl.sh
```

The menu covers start/stop/restart, image rebuilds, per-service restarts,
database migrations, logs, the docs server, and destructive cleanup (with confirmation).
Full option reference: `scripts/mediacat-ctl.md`.

**Windows launcher.** Double-click `scripts\mediacat-start.bat`. It checks that WSL is
running (and starts it, kept alive in the background, if not), waits for Docker, says
whether MediaCat is up, prints the start command and the browser address, and offers
to run both. Edit `DISTRO` / `PROJ_DIR` / `APP_URL` at the top if they change.

**Web control panel.** The same menu is also a web page: run `make ctl-web` and open
`http://127.0.0.1:8810/`. It shows container status, has a prominent link to the app, and
runs each action as `mediacat-ctl.sh --run N` with live output, so both always run the
same commands.

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
