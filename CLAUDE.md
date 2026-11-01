# CLAUDE.md — Project context for Claude Code

## What this is
MediaCat — cataloguing platform for vinyl + CD. Python 3.12, FastAPI,
PostgreSQL 16, MinIO, Redis, OPA. Docker Compose deployment on WSL2.

## Key commands
- `make setup` — create venv, install deps, install pre-commit
- `make lint` / `make typecheck` / `make test` — quality gates
- `make up` / `make down` — Docker Compose lifecycle (always uses `--env-file .env`)
- `make ollama-gpu-up` / `make ollama-down` — Ollama + Open WebUI stack
- Migrations: `docker compose --env-file .env -f deploy/docker-compose.yaml exec -e PGHOST=postgres -e PGUSER=mediacat_migrator -e PGDATABASE=mediacat app alembic -c alembic.ini upgrade head` (run from `mediacat/` dir; service is `app` not `worker`)

## Architecture decisions
- Hybrid AI: local Ollama default, API fallback (ADR-0002)
- OPA for rule engine, Python fallback (ADR-0003)
- Data/code separation via host bind mounts (ADR-0004)
- CSRF via X-CSRF-Token header (not form body)
- LLMs are advisory only, never autonomous
- Dark-first UI: CSS custom properties, [data-theme="light"] override, no build step
- Catalogue: Artist→Album→Pressing derived from Token table (no separate tables)
- `--env-file .env` required on all docker compose commands (Compose v2 resolves .env from compose file dir)
- Secret file modes: postgres_password=0600 (root only), others=0644 (app uid 10001 readable)
- WSL2 mirrored networking required for Windows browser access to 127.0.0.1
- Caddy local CA for .localhost HTTPS; cert imported once into Windows cert store
- Browser address is `https://mediacat.localhost:8443/` — Caddy publishes on `HTTPS_BIND` (127.0.0.1:8443); host port 443 belongs to Kasm Workspaces (`kasm_proxy`), so port-less links land on Kasm's login. Always write URLs with `:8443` in docs/UI
- Docs `.docx` files are pandoc output of their `.md` source: edit the `.md`, then `pandoc X.md -o X.docx --reference-doc=<old X.docx>` to keep styles
- Discography: MusicBrainz primary → Wikipedia fallback; Spotify enrichment via `ingestion/spotify.py` (client-credentials, no user auth); credentials stored in SiteSetting (Admin → API Tokens)
- Spotify API: omit `include_groups` and `market` params — Spotify rejects them with misleading "Invalid limit" 400; pagination uses `data["next"]` verbatim
- Discogs collection custom fields (Runout / Matrix LP1–2 A/B, Autographed, Signed by…) and release identifiers live in `Token.extra["discogs_collection"]` / `["discogs"]` (no migration); field names matched by pattern in `ingestion/discogs_extras.py`; write-back uses the collection instance API (`web/discogs_details.py`) — release identifiers themselves cannot be edited via the Discogs API
- `backend` network is `internal: true` (no internet): containers on it must not install packages at start-up — backup sidecar uses the image's BusyBox `crond` (daily pg_dump 02:00 UTC → `/srv/mediacat/backups/postgres`)
- Web control panel `tools/ctl_portal` (`make ctl-web`, 127.0.0.1:8810) runs `scripts/mediacat-ctl.sh --run N`; keep menu numbers/labels in `tools/ctl_portal/actions.py` in sync (enforced by `tests/test_ctl_portal.py`)
- CSP `script-src 'self'` blocks all inline JS including HTMX `hx-on` attributes — wire all event handlers in static `.js` files instead

## Open items
- (none)

## Documentation
- MediaCat_Technical_Reference.md — architecture, schema, API, configuration
- MediaCat_Installation_Guide.md — installation, validation, troubleshooting
