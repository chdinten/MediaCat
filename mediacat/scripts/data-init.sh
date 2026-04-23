#!/usr/bin/env bash
# data-init.sh -- create and verify the persistent data tree outside the repo.
# Idempotent. Can be run any time; never touches existing data.
set -Eeuo pipefail

: "${MEDIACAT_DATA_ROOT:=/srv/mediacat}"

say() { printf '[data-init] %s\n' "$*"; }

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Do not run as root." >&2; exit 1
fi

sudo install -d -m 0750 -o "root" -g "$USER" "$MEDIACAT_DATA_ROOT"
for d in postgres minio redis backups logs; do
    sudo install -d -m 0750 -o "root" -g "$USER" "$MEDIACAT_DATA_ROOT/$d"
done
sudo install -d -m 0700 -o root -g root "$MEDIACAT_DATA_ROOT/secrets"

say "Layout:"
sudo find "$MEDIACAT_DATA_ROOT" -maxdepth 1 -mindepth 1 -printf '  %M %u:%g %p\n'
say "Done. Root: $MEDIACAT_DATA_ROOT"
