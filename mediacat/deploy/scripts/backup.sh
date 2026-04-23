#!/bin/sh
# backup.sh — run by the backup sidecar on a cron schedule.
# Performs:
#   1. pg_dump (custom format, compressed) to /backups/postgres/
#   2. Prune backups older than 30 days.
#
# MinIO mirror is handled separately if mc is available.
set -eu

BACKUP_DIR="/backups/postgres"
DATE="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_FILE="${BACKUP_DIR}/mediacat_${DATE}.dump"

mkdir -p "$BACKUP_DIR"

echo "[backup] Starting pg_dump at $DATE"
pg_dump \
    --format=custom \
    --compress=6 \
    --no-owner \
    --no-acl \
    --file="$DUMP_FILE"

# Verify the dump is non-empty
if [ ! -s "$DUMP_FILE" ]; then
    echo "[backup] ERROR: dump file is empty" >&2
    rm -f "$DUMP_FILE"
    exit 1
fi

SIZE="$(du -h "$DUMP_FILE" | cut -f1)"
echo "[backup] pg_dump complete: $DUMP_FILE ($SIZE)"

# Prune dumps older than 30 days
find "$BACKUP_DIR" -name 'mediacat_*.dump' -mtime +30 -delete
REMAINING="$(find "$BACKUP_DIR" -name 'mediacat_*.dump' | wc -l)"
echo "[backup] Retained $REMAINING dump(s) after pruning."
