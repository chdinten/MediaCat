# Backup and Restore

## What runs

The `backup` sidecar runs `deploy/scripts/backup.sh` every day at **02:00 UTC**
with the image's BusyBox `crond`.

- Each run writes a compressed `pg_dump` (custom format) to
  `${MEDIACAT_DATA_ROOT}/backups/postgres/mediacat_<UTC timestamp>.dump`.
- Dumps older than 30 days are deleted.
- Output goes to `docker compose logs backup`.

The sidecar installs nothing at start-up. The `backend` network is `internal: true`
(no internet), so any `apk add` there fails. That is why an earlier version, which
installed `supercronic`, restarted endlessly and never made a backup.

!!! note "Not covered"
    MinIO objects (cover and runout images) are not backed up, and the dumps sit on
    the same disk as the database. Copy `backups/` to another location for
    off-host safety.

## Commands

```bash
cd /mnt/c/MEGA/SoundDB/mediacat
C="docker compose --env-file .env -f deploy/docker-compose.yaml -f deploy/docker-compose.dev.yaml"

# Is the scheduler running?
$C logs --tail 5 backup          # "[backup] crond started; daily pg_dump at 02:00 UTC"

# Manual backup
$C exec backup /scripts/backup.sh

# List dumps
$C exec backup ls -lh /backups/postgres
```

## Restore

Restoring replaces the current database contents with the dump.

```bash
$C stop app worker
$C exec backup sh -c 'PGPASSWORD="$(cat "$PGPASSWORD_FILE")" pg_restore --clean --if-exists \
    --no-owner --role=mediacat_migrator -d mediacat /backups/postgres/mediacat_YYYYMMDDTHHMMSSZ.dump'
$C exec backup sh -c 'PGPASSWORD="$(cat "$PGPASSWORD_FILE")" psql -d mediacat \
    -c "REVOKE UPDATE, DELETE ON TABLE audit_log FROM mediacat_app"'
$C start app worker
```

- `--role=mediacat_migrator` makes the restored tables belong to the migrator
  role, so the database's default privileges give `mediacat_app` its access
  again (dumps are taken with `--no-owner --no-acl`).
- The `REVOKE` re-applies the append-only rule on `audit_log`; the app checks it
  at start-up (`DEF-003 OK` in the app log).
- Tested against a scratch database: two consecutive restores, no errors,
  permissions correct.
