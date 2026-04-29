#!/usr/bin/env bash
# dev-up.sh -- bring the local development stack up.
# Wraps `docker compose` with sane defaults and config validation.
# The actual compose files land in Section 2 under deploy/.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
: "${MEDIACAT_ENV:=dev}"

if [[ ! -f .env ]]; then
    echo "ERROR: .env missing. Copy .env.example to .env and edit." >&2
    exit 1
fi
if [[ ! -f config/app.yaml ]]; then
    echo "ERROR: config/app.yaml missing. Copy config/app.example.yaml and edit." >&2
    exit 1
fi

COMPOSE_FILES=(--env-file .env -f deploy/docker-compose.yaml)
if [[ "$MEDIACAT_ENV" == "dev" && -f deploy/docker-compose.dev.yaml ]]; then
    COMPOSE_FILES+=(-f deploy/docker-compose.dev.yaml)
fi

PROFILES=()
if [[ "${MEDIACAT_OLLAMA:-0}" == "1" ]]; then
    PROFILES+=(--profile ollama)
    if [[ "${MEDIACAT_OLLAMA_GPU:-0}" == "1" ]]; then
        COMPOSE_FILES+=(-f deploy/docker-compose.gpu.yaml)
    fi
fi

docker compose "${COMPOSE_FILES[@]}" config -q
exec docker compose "${COMPOSE_FILES[@]}" "${PROFILES[@]}" up -d --remove-orphans "$@"
