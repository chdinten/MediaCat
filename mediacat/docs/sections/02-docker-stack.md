# Section 2 — Docker Compose stack & reverse proxy

## What this section produces

- `deploy/Dockerfile` — multi-stage, non-root (UID 10001), read-only
  rootfs, Tini as PID 1, Tesseract + language packs pre-installed.
- `deploy/docker-compose.yaml` — full stack: Caddy, PostgreSQL 16,
  MinIO, Redis 7, OPA, app, worker, backup sidecar. Two Docker networks
  (`frontend` public, `backend` internal-only).
- `deploy/docker-compose.dev.yaml` — dev overlay: source-mount live
  reload, relaxed read-only, exposed debug ports.
- `deploy/Caddyfile` — reverse proxy with security headers, upstream
  health checks, JSON logging.
- `deploy/initdb/01-roles.sql` — least-privilege DB roles
  (migrator / app / readonly), default privileges, required extensions.
- `deploy/scripts/backup.sh` — pg_dump cron, 30-day retention.
- `deploy/opa/bundles/mediacat/` — skeleton Rego policy + test.
- `scripts/secrets-init.sh` — generates random dev secrets.

## Network segmentation

```
┌─────────────────────────────────┐
│  frontend network               │
│  Caddy ←→ App                   │
└────────────┬────────────────────┘
             │ app is on both networks
┌────────────┴────────────────────┐
│  backend network (internal)     │
│  App, Worker, Postgres, MinIO,  │
│  Redis, OPA, Backup             │
└─────────────────────────────────┘
```

The `backend` network has `internal: true` — containers on it cannot
reach the internet. Only the `app` service straddles both networks.

## Security properties

- All containers drop all Linux capabilities (`cap_drop: [ALL]`) and
  re-add only what is needed (e.g. `NET_BIND_SERVICE` for Caddy).
- App and worker run as non-root UID 10001 with read-only rootfs.
- Redis renames dangerous commands (`FLUSHDB`, `FLUSHALL`, `DEBUG`).
- Postgres superuser password and app password are separate secrets.
- Caddy strips `Server` and `X-Powered-By` headers, adds HSTS,
  X-Content-Type-Options, X-Frame-Options, Referrer-Policy.

## First-time setup

```bash
./scripts/secrets-init.sh        # generate dev secrets
make data-init                   # create host data tree
make up                          # bring the stack up
```
