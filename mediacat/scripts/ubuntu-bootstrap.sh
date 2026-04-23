#!/usr/bin/env bash
#
# ubuntu-bootstrap.sh -- prepare a fresh Ubuntu 24.04 LTS (native or WSL2)
# for MediaCat development and runtime.
#
# Idempotent. Safe to re-run.
#
# Installs:
#   - Base tooling (curl, ca-certificates, git, make, build-essential, jq, ...)
#   - Python 3.12 + venv + pipx
#   - Docker Engine + Compose plugin + Buildx (from Docker's official apt repo)
#   - Caddy (optional; only if MEDIACAT_INSTALL_CADDY=1)
#   - Pre-commit, ruff, mypy via pipx (user-level)
#
# Creates the persistent data tree at ${MEDIACAT_DATA_ROOT:-/srv/mediacat}.
#
# Usage:
#   ./scripts/ubuntu-bootstrap.sh

set -Eeuo pipefail
IFS=$'\n\t'

# ---------- Logging helpers ---------------------------------------------------
readonly C_RESET=$'\033[0m'
readonly C_STEP=$'\033[1;36m'
readonly C_OK=$'\033[1;32m'
readonly C_WARN=$'\033[1;33m'
readonly C_ERR=$'\033[1;31m'
log_step() { printf '%s[bootstrap] %s%s\n' "$C_STEP" "$*" "$C_RESET"; }
log_ok()   { printf '%s[bootstrap] %s%s\n' "$C_OK"   "$*" "$C_RESET"; }
log_warn() { printf '%s[bootstrap] %s%s\n' "$C_WARN" "$*" "$C_RESET"; }
log_err()  { printf '%s[bootstrap] %s%s\n' "$C_ERR"  "$*" "$C_RESET" >&2; }
trap 'log_err "Failed at line $LINENO (exit $?)"' ERR

# ---------- Pre-flight --------------------------------------------------------
if [[ "$(id -u)" -eq 0 ]]; then
    log_err "Do not run this as root. Run as your normal user; sudo is invoked as needed."
    exit 1
fi
if ! command -v sudo >/dev/null 2>&1; then
    log_err "sudo is required."; exit 1
fi
if ! command -v lsb_release >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y --no-install-recommends lsb-release
fi
distrib="$(lsb_release -is)"
codename="$(lsb_release -cs)"
if [[ "$distrib" != "Ubuntu" ]]; then
    log_err "This script supports Ubuntu only (detected: $distrib)."; exit 1
fi
log_ok "Detected Ubuntu $codename"

# ---------- Configuration -----------------------------------------------------
MEDIACAT_DATA_ROOT="${MEDIACAT_DATA_ROOT:-/srv/mediacat}"
MEDIACAT_INSTALL_CADDY="${MEDIACAT_INSTALL_CADDY:-0}"
MEDIACAT_PYTHON="${MEDIACAT_PYTHON:-python3.12}"

# ---------- Base packages -----------------------------------------------------
log_step "Installing base apt packages..."
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git make jq unzip xz-utils tar rsync \
    build-essential pkg-config \
    "${MEDIACAT_PYTHON}" "${MEDIACAT_PYTHON}-venv" "${MEDIACAT_PYTHON}-dev" \
    python3-pip pipx \
    software-properties-common apt-transport-https \
    libssl-dev libffi-dev libpq-dev \
    uuid-runtime tzdata

# pipx path
pipx ensurepath >/dev/null
# shellcheck disable=SC1091
[[ -f "$HOME/.profile" ]] && source "$HOME/.profile" || true
export PATH="$HOME/.local/bin:$PATH"

# ---------- Docker Engine (official repo) ------------------------------------
log_step "Installing Docker Engine + Compose plugin..."
if ! command -v docker >/dev/null 2>&1; then
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/ubuntu/gpg" \
        | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    arch="$(dpkg --print-architecture)"
    echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${codename} stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -y
    sudo apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    log_ok "Docker already installed: $(docker --version)"
fi

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
    log_step "Adding $USER to the docker group (log out/in required to take effect)..."
    sudo usermod -aG docker "$USER"
    NEED_RELOGIN=1
fi

# WSL detection: rootless starts only, no systemd guarantees unless wsl.conf set.
if grep -qi microsoft /proc/version 2>/dev/null; then
    log_warn "WSL detected. Ensure /etc/wsl.conf has [boot] systemd=true (wsl2-prepare.ps1 handles this)."
fi

# ---------- Caddy (optional) --------------------------------------------------
if [[ "$MEDIACAT_INSTALL_CADDY" == "1" ]]; then
    log_step "Installing Caddy on host (optional)..."
    sudo apt-get install -y --no-install-recommends debian-keyring debian-archive-keyring
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -y
    sudo apt-get install -y caddy
fi

# ---------- Developer Python tools via pipx ----------------------------------
log_step "Installing developer tools via pipx..."
for pkg in pre-commit ruff mypy bandit pip-audit pdoc mkdocs-material; do
    if pipx list --short 2>/dev/null | awk '{print $1}' | grep -qx "$pkg"; then
        pipx upgrade "$pkg" >/dev/null || true
    else
        pipx install --force "$pkg" >/dev/null
    fi
done

# ---------- Persistent data tree ---------------------------------------------
log_step "Creating persistent data tree at $MEDIACAT_DATA_ROOT ..."
sudo install -d -m 0755 "$MEDIACAT_DATA_ROOT"
for d in postgres minio redis backups logs; do
    sudo install -d -m 0750 "$MEDIACAT_DATA_ROOT/$d"
done
# Secrets dir is root:root 0700.
sudo install -d -m 0700 -o root -g root "$MEDIACAT_DATA_ROOT/secrets"

# Give the invoking user read/traverse on the root only (not secrets).
sudo chown "root:${USER}" "$MEDIACAT_DATA_ROOT"
sudo chmod 0750 "$MEDIACAT_DATA_ROOT"

# ---------- Verify ------------------------------------------------------------
log_step "Versions:"
"${MEDIACAT_PYTHON}" --version || true
pipx --version || true
# docker check may fail until re-login for the docker group; don't trap.
docker --version 2>/dev/null || log_warn "docker not yet usable without re-login"
docker compose version 2>/dev/null || true

log_ok "Bootstrap complete."
if [[ "${NEED_RELOGIN:-0}" == "1" ]]; then
    log_warn "You were added to the 'docker' group. Log out of WSL (exit) and back in, or run: newgrp docker"
fi
echo
echo "Next: make setup"
