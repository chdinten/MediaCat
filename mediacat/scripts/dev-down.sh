#!/usr/bin/env bash
# dev-down.sh -- stop the dev stack (preserves volumes = data is safe).
set -Eeuo pipefail
cd "$(dirname "$0")/.."
COMPOSE_FILES=(--env-file .env -f deploy/docker-compose.yaml)
[[ -f deploy/docker-compose.dev.yaml ]] && COMPOSE_FILES+=(-f deploy/docker-compose.dev.yaml)
exec docker compose "${COMPOSE_FILES[@]}" down --remove-orphans "$@"
