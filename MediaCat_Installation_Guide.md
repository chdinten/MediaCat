# MediaCat — Installation & Operations Guide

This guide covers installation on **Windows 11 with WSL2**, stack validation, testing, and troubleshooting. Every command specifies exactly which shell to run it in.


## Shell legend

| Symbol | Meaning |
|--------|---------|
| **[PS-Admin]** | PowerShell opened **Run as Administrator** |
| **[PS-User]** | Normal PowerShell (no elevation) |
| **[WSL-User]** | Ubuntu terminal (`wsl` or the Ubuntu app) — your normal user, never root |
| **[WSL-sudo]** | A command inside WSL-User prefixed with `sudo` |

> **Never open a WSL shell as root.** The scripts explicitly reject `uid=0`.

Throughout this guide, `<REPO-ROOT>` means the directory containing `Makefile`, `deploy/`, and `src/`. In WSL this is typically `/mnt/c/<your-path>/mediacat`; in Windows it is the corresponding `C:\...\mediacat` path.




## Phase 1 — Windows prerequisites

### 1.1 — Enable WSL2 and install Ubuntu

**[PS-Admin]:**

```powershell
cd <REPO-ROOT>
.\scripts\wsl2-prepare.ps1
```

What it does: enables WSL and Virtual Machine Platform features, sets WSL default version to 2, installs Ubuntu 24.04, writes `/etc/wsl.conf` to enable systemd, then shuts WSL down.

If Windows prompts for a **reboot** — reboot, then re-run the script (it is idempotent). The script prints `Done.` on success.

### 1.2 — First-run Ubuntu setup

Launch **Ubuntu 24.04** from the Start menu (or `wsl -d Ubuntu-24.04`). You will be prompted to create a UNIX username and password. Do this now.

Verify WSL version — **[PS-User]:**
```powershell
wsl --list --verbose
```
`Ubuntu-24.04` must show `VERSION 2`. If it shows `1`: `wsl --set-version Ubuntu-24.04 2`.

### 1.3 — WSL2 mirrored networking (required for Windows browser access)

For the app to be reachable from a Windows browser at `127.0.0.1`, WSL2 must use mirrored networking.

**[PS-User]** — edit (or create) `%USERPROFILE%\.wslconfig`:
```ini
[wsl2]
networkingMode=mirrored
```

Then restart WSL: `wsl --shutdown` and reopen the Ubuntu terminal.




## Phase 2 — Ubuntu system bootstrap

All commands from here are in the **[WSL-User]** shell unless stated otherwise.

### 2.1 — Run the system bootstrap

```bash
cd <REPO-ROOT>
./scripts/ubuntu-bootstrap.sh
```

This installs: Python 3.12, Docker Engine + Compose plugin, build tools, and developer tools (ruff, mypy, bandit, pre-commit, mkdocs). Calls `sudo` internally; you will be prompted for your Linux password once.

When complete you will see `[bootstrap] Bootstrap complete.`

If Docker group membership was just added, you must **exit and reopen** the WSL shell before Docker works without `sudo`.

### 2.2 — Verify bootstrap

```bash
python3.12 --version          # 3.12.x
docker --version              # 24.x or later
docker compose version        # v2.x
ls /srv/mediacat/             # postgres  minio  redis  backups  logs  secrets
```




## Phase 3 — Data directory and secrets

### 3.1 — Create host data tree

```bash
cd <REPO-ROOT>
./scripts/data-init.sh
```

Creates the host directory tree under `MEDIACAT_DATA_ROOT` (default `/srv/mediacat`) with the correct ownership and permissions for each service:

| Directory | Owner | Mode | Used by |
|-----------|-------|------|---------|
| `postgres/` | uid 70 | `0700` | PostgreSQL (Alpine runs as uid 70) |
| `redis/` | uid 999 | `0750` | Redis |
| `minio/` | root | `0750` | MinIO |
| `backups/` | root | `0750` | Backup sidecar |
| `logs/` | root | `0750` | Log aggregation |
| `secrets/` | root | `0700` | Docker secret files |
| `open-webui/` | uid 1000 | `0750` | Open WebUI (Ollama UI) |

> **Why these permissions matter:** The postgres container runs as uid 70 (not root) and requires exclusive ownership of its data directory. Incorrect ownership causes `permission denied` on first start — see Troubleshooting.

### 3.2 — Generate random secrets

```bash
./scripts/secrets-init.sh
```

Writes random passwords into `/srv/mediacat/secrets/`:

| File | Mode | Read by |
|------|------|---------|
| `postgres_password` | `0600` | PostgreSQL init (root) |
| `postgres_app_password` | `0644` | App container (uid 10001) |
| `minio_root_password` | `0644` | App container (uid 10001) |
| `redis_password` | `0644` | App container (uid 10001) |

> **Why two permission modes:** PostgreSQL's init entrypoint runs as root and can read `0600`. The app runs as uid 10001 and needs at least world-readable (`0644`) secrets. Files left at `0600` cause `PermissionError` in the app container at startup.



## Phase 4 — Configuration

### 4.1 — Copy and edit the environment file

```bash
cd <REPO-ROOT>
cp .env.example .env
nano .env       # or any editor
```

Key variables to review:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIACAT_DATA_ROOT` | `/srv/mediacat` | Host path for all volumes and secrets |
| `TZ` | `Europe/London` | Container timezone |
| `PUBLIC_HOSTNAME` | `mediacat.localhost` | Browser-visible hostname |
| `PUBLIC_SCHEME` | `https` | `https` for Caddy local CA; `http` for plain HTTP |
| `HTTP_BIND` | `127.0.0.1:8080` | Caddy HTTP bind address |
| `HTTPS_BIND` | `127.0.0.1:8443` | Caddy HTTPS bind address |
| `MEDIACAT_DEV_ADMIN_PASSWORD` | *(blank)* | Sets the dev admin password at startup |
| `MEDIACAT_OLLAMA` | `0` | Set to `1` to enable Ollama profile |
| `MEDIACAT_OLLAMA_GPU` | `0` | Set to `1` to add NVIDIA GPU override |

> **Important:** All `docker compose` commands must include `--env-file .env` when run from the repo root. Compose v2 resolves the `.env` file relative to the **compose file's location** (`deploy/`), not the working directory. The `make` targets and `scripts/dev-up.sh` handle this automatically.

### 4.2 — Application config

```bash
cp config/app.example.yaml config/app.yaml
cp config/connectors.example.yaml config/connectors.yaml
```

For a local dev run the defaults work without changes. Key settings:
- `vision.primary: local_vlm` — uses Ollama if enabled; change to `api_vlm` if using Anthropic API only
- `llm.primary: local` — uses Ollama; change to `api` for Anthropic API only
- `postgres.host: postgres` — resolves to the Docker service name (do not change)



## Phase 5 — Python development environment

```bash
cd <REPO-ROOT>
make setup
```

Creates `.venv/`, installs all packages from `pyproject.toml` (including dev extras), and installs pre-commit hooks. Takes 2–4 minutes on first run.



## Phase 6 — Build and start the stack

### 6.1 — Start all services

```bash
make up
```

This is equivalent to:
```bash
docker compose --env-file .env -f deploy/docker-compose.yaml up -d --remove-orphans
```

Services started (dependency order):
1. `postgres` — PostgreSQL 16
2. `redis` — Redis 7
3. `minio` — Object store
4. `opa` — Open Policy Agent
5. `app` — FastAPI web server (port 8000 internally, via Caddy on 8080/8443)
6. `worker` — Background job processor
7. `caddy` — Reverse proxy (exposed on `127.0.0.1:8080` / `127.0.0.1:8443`)
8. `backup` — Daily pg_dump sidecar

### 6.2 — Check all services are healthy

```bash
make ps
```

Wait until all services show `(healthy)` or `Up`. PostgreSQL, Redis, and MinIO have health checks; `app` takes ~10 seconds to pass `/healthz`.

If a service is stuck in `(starting)` after 60 seconds:
```bash
docker compose --env-file .env -f deploy/docker-compose.yaml logs app --tail=50
```



## Phase 7 — Database migrations

```bash
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec app python -m alembic -c alembic/alembic.ini upgrade head
```

Expected output:
```
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_base, base schema
INFO  [alembic.runtime.migration] Running upgrade 0001_base -> 0002_symbols, symbol registry
```

Verify:
```bash
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec postgres psql -U postgres -d mediacat -c "\dt"
```

You should see tables: `tokens`, `symbols`, `symbol_variants`, `token_symbols`, `users`, etc.



## Phase 8 — HTTPS in the browser (local CA)

Caddy automatically generates a local certificate authority for `.localhost` domains. To trust it in Windows browsers:

**[WSL-User]** — Export the root certificate:
```bash
docker compose --env-file .env -f deploy/docker-compose.yaml \
    cp caddy:/data/caddy/pki/authorities/local/root.crt ~/caddy-root.crt
```

**[PS-Admin]** — Import into the Windows certificate store:
```powershell
Import-Certificate -FilePath "$env:USERPROFILE\caddy-root.crt" `
    -CertStoreLocation Cert:\LocalMachine\Root
```

After this, all Windows browsers trust `https://mediacat.localhost` permanently (no more certificate warnings). The cert only needs to be imported once.

> **Hosts file not needed** — With `networkingMode=mirrored` in WSL2, `127.0.0.1` in WSL maps directly to `127.0.0.1` in Windows. No `hosts` file entry is required.



## Phase 9 — Verification checklist

Run all of these from **[WSL-User]**:

```bash
# 1. All containers healthy
make ps

# 2. App liveness probe
curl -s http://127.0.0.1:8080/healthz
# Expected: {"status":"ok"}

# 3. PostgreSQL — verify symbol seed data
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec postgres psql -U postgres -d mediacat -c "SELECT count(*) FROM symbols;"
# Expected: 26

# 4. MinIO health
curl -s http://127.0.0.1:9000/minio/health/live
# Expected: HTTP 200

# 5. Redis PING
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec redis redis-cli -a "$(sudo cat /srv/mediacat/secrets/redis_password)" ping
# Expected: PONG

# 6. OPA rule engine
curl -s http://127.0.0.1:8181/health
# Expected: {}

# 7. Python quality gates (from dev env)
make lint
make typecheck
make test
```



## Phase 10 — Ollama / local AI (optional)

Requires an NVIDIA GPU with ≥ 12 GB VRAM for `qwen2.5vl:32b` (Q4_K_M, ~21 GB). Tested on RTX 4090 (24 GB GDDR6X).

### 10.1 — Enable Ollama in .env

```bash
# Add/set in .env:
MEDIACAT_OLLAMA=1
MEDIACAT_OLLAMA_GPU=1             # omit for CPU-only
OLLAMA_VLM_MODEL=qwen2.5vl:32b   # ~21 GB Q4_K_M
OLLAMA_OCR_MODEL=glm-ocr          # ~2.2 GB
OLLAMA_CONTEXT_LENGTH=8192        # fits within 24 GB VRAM
OLLAMA_FLASH_ATTENTION=1          # Flash Attention 2 (Ada/sm_89+)
```

### 10.2 — Start Ollama stack

```bash
make ollama-gpu-up
# or for CPU:
make ollama-up
```

This starts Ollama, Open WebUI, and `ollama-pull` (which downloads the models on first run). Model download progress can be watched:
```bash
docker compose --env-file .env -f deploy/docker-compose.yaml \
    -f deploy/docker-compose.gpu.yaml --profile ollama logs -f ollama-pull
```

### 10.3 — Verify GPU is in use

```bash
make ollama-models
# Should list qwen2.5vl:32b and glm-ocr with GPU memory usage
```

Open WebUI is available at `https://ollama.mediacat.localhost`.

> **Healthcheck note:** The `ollama/ollama` image does not include `curl`. The healthcheck uses `ollama list >/dev/null 2>&1` instead.


## Daily operations

### Stack lifecycle

```bash
make up        # start all services (background)
make down      # stop containers; data volumes are preserved
make restart   # down + up
make logs      # tail all service logs
make ps        # show container status
```

### Rebuild after code changes

```bash
docker compose --env-file .env -f deploy/docker-compose.yaml \
    up -d --build app worker
```

### Run migrations after schema changes

```bash
docker compose --env-file .env -f deploy/docker-compose.yaml \
    exec app python -m alembic -c alembic/alembic.ini upgrade head
```

### Open an app shell

```bash
docker compose --env-file .env -f deploy/docker-compose.yaml exec app bash
```


## Quality gates

| Command | Description |
|---------|-------------|
| `make lint` | Ruff linter — code style and imports |
| `make format` | Ruff formatter — auto-fix formatting |
| `make typecheck` | Mypy strict — full static type checking |
| `make security` | Bandit + pip-audit — security scan |
| `make test` | Full pytest suite with coverage report |
| `make test-fast` | Pytest excluding slow / integration tests |


## Troubleshooting

### `docker: permission denied`
You were not yet in the `docker` group when this shell was opened.
```bash
exit   # exit WSL, then reopen Ubuntu
# or:
newgrp docker
```

### PostgreSQL `permission denied` on data directory
The postgres container (uid 70) cannot read its data dir. Fix:
```bash
sudo chown -R 70:70 /srv/mediacat/postgres
sudo chmod 700 /srv/mediacat/postgres
```

### `secrets_xxx file not found` / `PermissionError` on secret file
The app container (uid 10001) cannot read a secret file. Check modes:
```bash
ls -la /srv/mediacat/secrets/
# postgres_app_password, minio_root_password, redis_password must be 0644 or world-readable
sudo chmod 0644 /srv/mediacat/secrets/postgres_app_password
sudo chmod 0644 /srv/mediacat/secrets/minio_root_password
sudo chmod 0644 /srv/mediacat/secrets/redis_password
```

### Docker Compose does not read `.env`
Compose v2 looks for `.env` in the directory of the compose file (`deploy/`), not the current working directory. Always pass `--env-file .env` explicitly when running `docker compose` from the repo root. The `make` targets and `scripts/dev-up.sh` do this automatically.

### Browser refuses connection / ERR_CONNECTION_REFUSED
1. Verify Caddy is running: `make ps` — look for `caddy (healthy)`.
2. Check the bind address: `grep HTTP_BIND .env` should show `127.0.0.1:8080` (not `0.0.0.0:8080`).
3. Verify WSL2 mirrored networking is enabled: check `%USERPROFILE%\.wslconfig` for `networkingMode=mirrored`.
4. Test TCP connectivity from PowerShell: `Test-NetConnection 127.0.0.1 -Port 8080`.

### Browser switches to HTTPS even when using `http://`
Browsers (especially Chromium-based) have HSTS preloading for `.localhost`. Either:
- Use the Caddy local CA approach (Phase 8) to trust the HTTPS certificate, or
- Access via `http://127.0.0.1:8080` directly (bypasses hostname-based HSTS).

### `config/app.yaml missing`
```bash
cp config/app.example.yaml config/app.yaml
```

### App starts but login fails / no admin user
Set `MEDIACAT_DEV_ADMIN_PASSWORD` in `.env` and restart the app:
```bash
# In .env:
MEDIACAT_DEV_ADMIN_PASSWORD=your-chosen-password
# Then:
docker compose --env-file .env -f deploy/docker-compose.yaml restart app
```
The app seeds the dev admin at startup when `MEDIACAT_ENV=dev` and `MEDIACAT_DEV_ADMIN_PASSWORD` is non-empty.

### Ollama healthcheck fails / container exits
The `ollama/ollama` image has no `curl` — do not use curl-based healthchecks. The project uses:
```yaml
test: ollama list >/dev/null 2>&1 || exit 1
```
If the container still fails, check GPU drivers: `nvidia-smi` should show the GPU.

### Ollama model pull container waits forever
The `ollama-pull` container waits for Ollama to be ready using `until ollama list >/dev/null 2>&1; do sleep 3; done`. If Ollama is unhealthy (GPU driver issue, OOM), the pull container will spin indefinitely. Check Ollama logs:
```bash
docker compose --env-file .env -f deploy/docker-compose.yaml \
    -f deploy/docker-compose.gpu.yaml --profile ollama logs ollama
```

### Open WebUI not accessible
Open WebUI proxies through Caddy at `https://ollama.mediacat.localhost`. Ensure:
1. The Ollama profile is running (`make ollama-gpu-up` or `make ollama-up`).
2. The Caddy local CA certificate is imported into Windows (Phase 8).
3. `MEDIACAT_OLLAMA=1` is set in `.env`.


## Quick-reference: shell-by-command

| Command | Shell | Why |
|---------|-------|-----|
| `.\scripts\wsl2-prepare.ps1` | **[PS-Admin]** | Windows feature enablement |
| `wsl --list --verbose` | **[PS-User]** | WSL management |
| Edit `%USERPROFILE%\.wslconfig` | **[PS-User]** | WSL2 network config |
| `Import-Certificate ...` | **[PS-Admin]** | System cert store |
| `./scripts/ubuntu-bootstrap.sh` | **[WSL-User]** | Uses sudo internally |
| `./scripts/data-init.sh` | **[WSL-User]** | Uses sudo internally |
| `./scripts/secrets-init.sh` | **[WSL-User]** | Uses sudo internally |
| `make setup` | **[WSL-User]** | Pure Python, no elevation |
| `make up` / `make down` | **[WSL-User]** | Docker via group membership |
| `docker compose exec ...` | **[WSL-User]** | Docker via group membership |
