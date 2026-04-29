#!/bin/sh
# ollama-pull.sh — pull VLM and OCR models into Ollama.
#
# Runs as the ollama-pull one-shot service inside Docker.
# The ollama server is reachable at OLLAMA_HOST (set by compose).
#
# Models:
#   OLLAMA_VLM_MODEL  — default qwen2.5vl:32b  (~21 GB)
#   OLLAMA_OCR_MODEL  — default glm-ocr         (~2.2 GB)
#
# Skips any model that is already present on the server.

set -e

OLLAMA_HOST="${OLLAMA_HOST:-http://ollama:11434}"
VLM_MODEL="${OLLAMA_VLM_MODEL:-qwen2.5vl:32b}"
OCR_MODEL="${OLLAMA_OCR_MODEL:-glm-ocr}"

log() { printf '[ollama-pull] %s\n' "$*"; }

# Wait until the API is ready (ollama healthcheck should guarantee this, but
# be defensive in case of scheduling races).
until ollama list >/dev/null 2>&1; do
    log "Waiting for Ollama at ${OLLAMA_HOST}..."
    sleep 3
done
log "Ollama is ready."

pull_if_missing() {
    model="$1"
    # ollama list output contains model names; grep for an exact prefix match.
    if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "${model}"; then
        log "SKIP  ${model} (already present)"
    else
        log "PULL  ${model} ..."
        ollama pull "${model}"
        log "DONE  ${model}"
    fi
}

pull_if_missing "${VLM_MODEL}"
pull_if_missing "${OCR_MODEL}"

log "All models ready."
