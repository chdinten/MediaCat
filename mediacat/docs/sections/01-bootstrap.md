# Section 1 — Bootstrap & scaffolding

This section delivers the repository skeleton, the host-level bootstrap
scripts, the Python toolchain, and the documentation pipeline. No
application features are implemented yet; Section 2 brings up the Docker
stack, Section 3 the database, and so on.

## What this section produces

- `scripts/wsl2-prepare.ps1` — idempotent Windows 11 preparation (WSL2
  feature enablement, Ubuntu 24.04 install, `/etc/wsl.conf` with
  `systemd=true`).
- `scripts/ubuntu-bootstrap.sh` — idempotent Ubuntu preparation (base
  packages, Python 3.12, Docker CE from the official apt repo, developer
  CLIs via pipx, creation of `${MEDIACAT_DATA_ROOT}`).
- `scripts/data-init.sh`, `scripts/dev-up.sh`, `scripts/dev-down.sh`.
- `pyproject.toml` with strict ruff/mypy/pytest/coverage/bandit config.
- `Makefile` exposing every developer workflow.
- `.env.example`, `config/app.example.yaml`, `config/connectors.example.yaml`,
  `config/logging.yaml`.
- `.pre-commit-config.yaml` with gitleaks, ruff, mypy, bandit, yamllint,
  shellcheck, hadolint.
- `docs/` skeleton (MkDocs-Material + Mermaid + pdoc).
- `src/mediacat/logging_filters.py` — the only non-trivial Python shipped
  in this section (request-id correlation, secret redaction, JSON
  formatter).

## Data / code separation

All persistent state lives under `${MEDIACAT_DATA_ROOT}` (default
`/srv/mediacat`), which is *outside* the repository and is mounted into
containers as bind volumes:

```
/srv/mediacat/
├── postgres/      0750 root:$USER   PGDATA
├── minio/         0750 root:$USER   object store data
├── redis/         0750 root:$USER   AOF + RDB
├── backups/       0750 root:$USER   pg_dump + mc mirror output
├── logs/          0750 root:$USER   optional host-mounted logs
└── secrets/       0700 root:root    Docker secrets source
```

Upgrading the application is a rebuild + restart. No data is ever in an
image layer.

## How to run it

```bash
# On Windows 11 (elevated PowerShell)
./scripts/wsl2-prepare.ps1

# Inside WSL Ubuntu
./scripts/ubuntu-bootstrap.sh
make setup
cp .env.example .env
cp config/app.example.yaml config/app.yaml
cp config/connectors.example.yaml config/connectors.yaml
make lint typecheck security test
make docs
```

## Security properties already enforced

- Repo-level secret scanning via gitleaks pre-commit hook.
- `.gitignore` + `.dockerignore` block real `.env`, real `config/*.yaml`,
  and `secrets/`.
- Docker group membership warned, not silently applied.
- Logging filters redact common credential patterns before formatting.
- Bandit runs on every commit.
- Host data tree has restrictive modes from the moment it is created.

## Deferred to later sections

| Item                                  | Section |
|---------------------------------------|---------|
| Dockerfiles, docker-compose, TLS      | 2       |
| Database schema, RLS, migrations      | 3       |
| Object storage + OCR pipeline         | 4       |
| Connector framework + drift advisor   | 5       |
| Rule engine adapter                   | 6       |
| LLM adapters (local + API)            | 7       |
| Vision adapters                       | 8       |
| Web UI (Jinja + HTMX)                 | 9       |
| ASVS review and threat model          | 10      |
| Full documentation polish             | 11      |
