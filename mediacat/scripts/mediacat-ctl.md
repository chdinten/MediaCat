# mediacat-ctl.sh — MediaCat Stack Manager

Interactive terminal menu for managing the MediaCat Docker Compose stack on WSL2.
Covers the full lifecycle: start/stop, image rebuilds, database migrations, log tailing,
and destructive cleanup — all without having to remember individual `docker compose` commands.

---

## First-time setup

1. **Copy the script** to a convenient location (typically your WSL2 home directory):

   ```bash
   cp mediacat/scripts/mediacat-ctl.sh ~/mediacat-ctl.sh
   chmod +x ~/mediacat-ctl.sh
   ```

2. **Edit `PROJ_DIR`** near the top of the script to match where the `mediacat/` folder
   lives on your machine:

   ```bash
   # Line 11 — change this to your own path
   PROJ_DIR="/mnt/c/MEGA/SoundDB/mediacat"
   ```

   The path must be a WSL2 path (i.e. `/mnt/c/...`), not a Windows path.

3. **Run it:**

   ```bash
   ~/mediacat-ctl.sh
   ```

> The script aborts with an error if `PROJ_DIR` does not exist, so an incorrect path is
> caught immediately.

---

## Menu overview

The menu is divided into five sections, each described below.
Enter the number next to an option and press Enter. Press `0` (or `q`) to quit.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│  MediaCat Stack Manager                                                       │
│  /mnt/c/MEGA/SoundDB/mediacat                                                 │
├───────────────────────────────────────────────────────────────────────────────┤
│  -- START / STOP / RESTART                                                    │
├───────────────────────────────────┬───────────────────────────────────────────┤
│ [1] Start stack (dev)             │ [2] Stop stack                            │
│ [3] Restart stack (down + up)     │ [4] Restart: app container                │
│ [5] Restart: worker container     │ [6] Start + Ollama (CPU)                  │
│ [7] Start + Ollama (GPU)          │                                           │
...
```

---

## Section reference

### START / STOP / RESTART  (1 – 7)

| # | Action | Notes |
|---|--------|-------|
| 1 | Start stack (dev) | `docker compose up -d`. Merges `docker-compose.yaml` + `docker-compose.dev.yaml` if present. |
| 2 | Stop stack | `docker compose down --remove-orphans`. Does **not** remove volumes. |
| 3 | Restart stack | Full down then up cycle. Use when multiple containers need a clean restart. |
| 4 | Restart: app | Hot-restart the `app` container only. Fastest way to pick up Python/template changes. |
| 5 | Restart: worker | Hot-restart the `worker` (ARQ background worker) container. |
| 6 | Start + Ollama (CPU) | Includes the `ollama` Compose profile, no GPU overlay. |
| 7 | Start + Ollama (GPU) | Includes `docker-compose.gpu.yaml`; requires NVIDIA Container Toolkit. |

### REBUILD IMAGES  (8 – 13)

| # | Action | Notes |
|---|--------|-------|
| 8  | Rebuild: app + worker | Rebuilds both images without restarting. |
| 9  | Rebuild: app only | |
| 10 | Rebuild: worker only | |
| 11 | Pull latest base images | Refreshes upstream images used in `FROM` directives. |
| 12 | Rebuild + restart: app | Build then `up -d --no-deps app`. Zero-downtime-ish single-service update. |
| 13 | Rebuild + restart: all | Build app + worker then full `up -d`. |

### SERVICE CONTROL  (14 – 20)

| # | Action |
|---|--------|
| 14 | Restart postgres |
| 15 | Restart redis |
| 16 | Restart minio |
| 17 | Restart caddy |
| 18 | Restart opa |
| 19 | Restart ollama |
| 20 | Pull Ollama models | Runs the `ollama-pull` one-shot service to download the configured VLM + OCR models. |

### DATABASE  (21 – 22)

| # | Action | Notes |
|---|--------|-------|
| 21 | Run migrations | `alembic upgrade head` inside the running `app` container. Sets `PGHOST=postgres` so Alembic connects to the Compose network, not localhost. |
| 22 | Migration status | `alembic current` — shows which revision is applied. |

### MONITOR / LOGS  (23 – 31)

| # | Action |
|---|--------|
| 23 | Stack status | `docker compose ps` — running/stopped state for all services. |
| 24 | Logs: all services | Follows combined log output; prompts for number of tail lines (default 20). |
| 25 – 30 | Logs: app / worker / postgres / redis / caddy / minio | Per-service follow; prompts for tail lines. |
| 31 | Logs: custom service + lines | Prompts for both service name and tail line count. |

Log tailing is interactive — press **Ctrl-C** to stop following and return to the "Press Enter" prompt.

### CLEANUP  (32 – 36)

| # | Action | Risk |
|---|--------|------|
| 32 | Prune containers + networks | Safe — only stopped/unused resources. |
| 33 | Prune unused images | Safe — dangling and unreferenced images only. |
| 34 | `make clean` | Removes Python build artefacts (`.venv`, `__pycache__`, etc.). |
| 35 | Down + remove volumes | **Destructive** — deletes named volumes (Caddy TLS certs, etc.). Requires `y` confirmation. |
| 36 | Full `docker system prune` | **Destructive** — removes all unused Docker data system-wide. Requires `y` confirmation. |

Options 35 and 36 display a `WARNING` prompt and require an explicit `y` to proceed.
Any other input cancels and returns to the menu.

---

## Compose file merging

The script automatically merges Compose files in this order when the files are present:

```
docker-compose.yaml          ← always loaded
docker-compose.dev.yaml      ← merged if file exists (dev overrides)
docker-compose.gpu.yaml      ← merged only for options 7 and 19 (Ollama GPU)
```

The Ollama profile (`--profile ollama`) is added automatically for options 6, 7, 19, and 20.

All commands inherit the project `.env` file via `--env-file .env`.

---

## Colour coding

| Colour | Meaning |
|--------|---------|
| Red (bold) | Selection numbers `[N]` |
| Blue | Menu item text |
| Green (bold) | Frame borders, section headers, prompts |

Colour requires a terminal with ANSI escape support (standard in WSL2 + Windows Terminal).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ERROR: project directory not found` | Update `PROJ_DIR` on line 11 to your actual path. |
| Migration fails with connection error | The `app` container must be running. Start the stack first (option 1), then run migrations (option 21). |
| Ollama GPU option starts but no GPU seen | Verify NVIDIA Container Toolkit is installed in WSL2: `docker run --rm --gpus all nvidia/cuda:12-base nvidia-smi` |
| Menu rendering looks garbled | Use a terminal that supports UTF-8 box-drawing characters (Windows Terminal, iTerm2). PuTTY may need its character set set to UTF-8. |
