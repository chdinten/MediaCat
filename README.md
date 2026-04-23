# MediaCat

A cataloging platform for physical music media (vinyl + CD, first generation).
Token-object registry with provenance, multi-source ingestion, vision-assisted
transcription of labels / obi / runout etchings, country-aware decoding,
human-in-the-loop review, and LLM support for comparison, anomaly detection,
translation (to British English), and text generation.

## Design invariants

1. **Data/code separation.** All persistent data (PostgreSQL, MinIO, Redis AOF,
   backups, logs) lives under `${MEDIACAT_DATA_ROOT}` on the host, never inside
   the repo or container image. Code upgrades are a rebuild + restart; data
   volumes are untouched.
2. **Secrets never in the image, never in Git.** Secrets are supplied via
   environment or Docker secrets, sourced from a host-only `secrets/` tree.
3. **Security-by-default.** Non-root containers, read-only rootfs where
   possible, principle-of-least-privilege DB roles, strict CSP, rate limiting,
   Argon2id password hashing. See `docs/architecture.md` and the Section 10
   hardening report.
4. **Hybrid AI.** Vision and LLM calls default to a local backend (Ollama /
   local VLM) with a provider-agnostic adapter that can fall back to an API.
5. **LLM-as-advisor, not code-writer.** LLMs never author executable client
   code. Schema-drift detection is an advisory signal for a human merge.

## Project layout

```
mediacat/
├── scripts/          Bootstrap & lifecycle scripts (WSL, Ubuntu, dev-up)
├── config/           Non-secret YAML config (examples only in Git)
├── deploy/           Dockerfiles, docker-compose, reverse-proxy config  (Section 2)
├── alembic/          DB migrations                                      (Section 3)
├── src/mediacat/     Python application package                         (Section 4+)
├── tests/            Pytest suite
├── docs/             MkDocs site, ADRs, runbooks, diagrams
└── tools/            Developer utilities
```

Persistent-data layout (outside the repo, typically `/srv/mediacat/`):

```
${MEDIACAT_DATA_ROOT}/
├── postgres/         PGDATA
├── minio/            Object store data
├── redis/            AOF + RDB
├── secrets/          0700, root-owned; docker secrets read here
├── backups/          pg_dump + mc mirror output
└── logs/             Optional host-mounted logs
```

## Bootstrap on Windows 11 / WSL2

From an **elevated PowerShell** on the Windows host:

```powershell
# 1. Prepare WSL2 and install Ubuntu 24.04
./scripts/wsl2-prepare.ps1
```

Then reopen the Ubuntu shell and from inside WSL, at the repo root:

```bash
# 2. Install Docker, Python 3.12, build tooling, create data dirs
./scripts/ubuntu-bootstrap.sh

# 3. Python dev environment + git hooks
make setup

# 4. Copy and edit configuration
cp .env.example .env
cp config/app.example.yaml config/app.yaml
cp config/connectors.example.yaml config/connectors.yaml
$EDITOR .env config/app.yaml config/connectors.yaml

# 5. (Section 2 onwards) bring the stack up
make up
```

## Common targets

| Target           | Purpose                                           |
|------------------|---------------------------------------------------|
| `make setup`     | Create venv, install deps, install pre-commit     |
| `make lint`      | Ruff lint                                         |
| `make format`    | Ruff format                                       |
| `make typecheck` | Mypy strict                                       |
| `make security`  | Bandit + pip-audit                                |
| `make test`      | Pytest with coverage                              |
| `make docs`      | Build MkDocs + pdoc site into `site/`             |
| `make docs-serve`| Live-preview docs on http://127.0.0.1:8800       |
| `make up/down`   | Docker Compose lifecycle (Section 2+)             |
| `make data-init` | Create the host data tree with correct perms     |

## Documentation

Full docs are generated from source: `make docs`, open `site/index.html`.
Key entry points:

- `docs/architecture.md` — high-level architecture and data flow
- `docs/adr/` — Architecture Decision Records
- `docs/runbooks/` — operational procedures
- `docs/reference/` — auto-generated API reference (pdoc)

## Licence

TBD. Dependencies retain their upstream licences; see `docs/third-party.md`
(generated in Section 11).
