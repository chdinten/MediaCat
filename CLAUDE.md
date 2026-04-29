# CLAUDE.md — Project context for Claude Code

## What this is
MediaCat — cataloguing platform for vinyl + CD. Python 3.12, FastAPI,
PostgreSQL 16, MinIO, Redis, OPA. Docker Compose deployment on WSL2.

## Key commands
- `make setup` — create venv, install deps, install pre-commit
- `make lint` / `make typecheck` / `make test` — quality gates
- `make up` / `make down` — Docker Compose lifecycle (always uses `--env-file .env`)
- `make ollama-gpu-up` / `make ollama-down` — Ollama + Open WebUI stack

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

## Open items
- (none)

## Documentation
- MediaCat_Technical_Reference.md — architecture, schema, API, configuration
- MediaCat_Installation_Guide.md — installation, validation, troubleshooting
