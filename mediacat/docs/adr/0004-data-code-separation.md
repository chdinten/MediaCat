# ADR-0004 — Strict data/code separation via host bind mounts

- **Status**: Accepted
- **Date**: 2026-04-16

## Context

The operator must be able to upgrade application code (rebuild + restart)
without any risk to the database or object store. Named Docker volumes
work, but their lifecycle is implicit and `docker compose down -v`
destroys them silently; backups and migration between hosts are
awkward. Bind mounts to a known host path make ownership explicit.

## Decision

All persistent data lives under `${MEDIACAT_DATA_ROOT}` (default
`/srv/mediacat`) on the host, bind-mounted into containers:

```
${MEDIACAT_DATA_ROOT}/
├── postgres/   -> /var/lib/postgresql/data
├── minio/      -> /data
├── redis/      -> /data
├── backups/    -> /backups (backup sidecar only)
├── logs/       -> /logs   (optional)
└── secrets/    -> Docker secrets source (0700, root:root)
```

Modes are `0750` on data directories (root:user so the invoking user can
ls but not write), `0700` on secrets. Container UIDs are fixed (see the
Dockerfile base in Section 2).

## Consequences

- `docker compose down` never loses data. Only manual `rm -rf` does.
- Off-host backup is a filesystem operation (restic, rsync, ZFS send).
- On-disk permissions must match container UIDs; the backup sidecar
  runs as the same UID as Postgres to read PGDATA cleanly.
- Migration between hosts is an `rsync` of the data root plus pointing
  the new stack at it.

## Alternatives considered

- **Named Docker volumes** — rejected; too easy to destroy, opaque
  backup path.
- **External managed Postgres** — would remove one bind-mount concern
  but we run single-host on purpose for v1.
