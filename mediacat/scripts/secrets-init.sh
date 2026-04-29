#!/usr/bin/env bash
# secrets-init.sh — generate initial secret files for the dev stack.
#
# Writes random passwords into ${MEDIACAT_DATA_ROOT}/secrets/.
# NEVER overwrites existing files — safe to re-run.
# For production, replace with Vault / SOPS / cloud KMS workflow.
#
# Permission strategy:
#   postgres_password     — 0600 root:root  (only read by postgres entrypoint as root)
#   all others            — 0644 root:root  (readable by any container user; access
#                                            is controlled by Docker's secrets: list,
#                                            not by host filesystem permissions)
set -Eeuo pipefail

: "${MEDIACAT_DATA_ROOT:=/srv/mediacat}"
SECRETS_DIR="${MEDIACAT_DATA_ROOT}/secrets"

say() { printf '[secrets-init] %s\n' "$*"; }

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Do not run as root. sudo is used internally." >&2; exit 1
fi

# Ensure directory exists with correct perms
sudo install -d -m 0700 -o root -g root "$SECRETS_DIR"

# gen_secret NAME [MODE]
# Generates a random password and writes it to SECRETS_DIR/NAME.
# MODE defaults to 0644 (readable by any container user).
gen_secret() {
    local name="$1"
    local mode="${2:-0644}"
    local file="${SECRETS_DIR}/${name}"
    if sudo test -f "$file"; then
        say "SKIP  $name (already exists)"
    else
        local pw
        pw="$(openssl rand -base64 32 | tr -d '\n=')"
        echo -n "$pw" | sudo tee "$file" >/dev/null
        say "WROTE $name ($(echo -n "$pw" | wc -c) chars)"
    fi
    sudo chown root:root "$file"
    sudo chmod "$mode" "$file"
}

# postgres_password: only the postgres entrypoint reads this (starts as root).
gen_secret postgres_password     0600

# All other secrets are read by the app / worker (uid 10001) or redis (uid 999).
# Docker's secrets: list controls which containers receive each file.
gen_secret postgres_app_password 0644
gen_secret minio_root_password   0644
gen_secret redis_password        0644

say ""
say "Secrets directory:"
sudo ls -la "$SECRETS_DIR"
say ""
say "Done. These are random dev passwords."
say "For production, replace with secrets from your KMS / Vault."
