#!/usr/bin/env bash
# data-init.sh -- create and verify the persistent data tree outside the repo.
# Idempotent. Can be run any time; never touches existing data files.
#
# Ownership is set to match each container's runtime user so bind-mounted
# volumes are always writable without resorting to privileged containers:
#
#   postgres   → uid 70   (postgres:alpine)   mode 0700 (Postgres enforces this)
#   redis      → uid 999  (redis:alpine)       mode 0750
#   minio      → uid 0    (runs as root)       mode 0750
#   ollama     → uid 0    (runs as root)       mode 0750
#   open-webui → uid 1000 (node in image)      mode 0750
#   backups, logs, ollama → root               mode 0750
set -Eeuo pipefail

: "${MEDIACAT_DATA_ROOT:=/srv/mediacat}"

say() { printf '[data-init] %s\n' "$*"; }

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Do not run as root." >&2; exit 1
fi

# Root of data tree — traversable by the invoking user.
sudo install -d -m 0750 -o root -g "$USER" "$MEDIACAT_DATA_ROOT"

# Secrets — root-only; group permissions set per-secret by secrets-init.sh.
sudo install -d -m 0700 -o root -g root "$MEDIACAT_DATA_ROOT/secrets"

# PostgreSQL — uid 70 must own the directory; mode 0700 is required by initdb.
sudo install -d -m 0700 -o 70 -g 70 "$MEDIACAT_DATA_ROOT/postgres"

# Redis — uid 999 (redis user in redis:alpine).
sudo install -d -m 0750 -o 999 -g 999 "$MEDIACAT_DATA_ROOT/redis"

# MinIO, Ollama, backups, logs — run as root inside the container.
for d in minio backups logs ollama; do
    sudo install -d -m 0750 -o root -g root "$MEDIACAT_DATA_ROOT/$d"
done

# Open WebUI — node process runs as uid 1000.
sudo install -d -m 0750 -o 1000 -g 1000 "$MEDIACAT_DATA_ROOT/open-webui"

say "Layout:"
sudo find "$MEDIACAT_DATA_ROOT" -maxdepth 1 -mindepth 1 -printf '  %M %u:%g %p\n'
say "Done. Root: $MEDIACAT_DATA_ROOT"
