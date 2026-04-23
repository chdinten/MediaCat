# CLAUDE.md — Project context for Claude Code

## What this is
MediaCat — cataloguing platform for vinyl + CD. Python 3.12, FastAPI,
PostgreSQL 16, MinIO, Redis, OPA. Docker Compose deployment on WSL2.

## Key commands
- `make setup` — create venv, install deps, install pre-commit
- `make lint` / `make typecheck` / `make test` — quality gates
- `make up` / `make down` — Docker Compose lifecycle

## Architecture decisions
- Hybrid AI: local Ollama default, API fallback (ADR-0002)
- OPA for rule engine, Python fallback (ADR-0003)
- Data/code separation via host bind mounts (ADR-0004)
- CSRF via X-CSRF-Token header (not form body — see FIX-004)
- LLMs are advisory only, never autonomous

## Open items from due diligence audit
- DEF-001: Wire sanitise() into translation pipeline
- DEF-002: Migrate login rate limiter to Redis
- DEF-003: REVOKE UPDATE/DELETE on audit_log for app role
- Sections 10-11 docs need final polish
