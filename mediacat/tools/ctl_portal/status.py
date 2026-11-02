"""Read-only stack status for the start page: containers and the docs server."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

DOCS_PID = Path("/tmp/mediacat-docs.pid")  # noqa: S108 - same file mediacat-ctl.sh uses


def _compose_cmd(proj: Path, *args: str) -> list[str]:
    """Same compose files as ``_compose`` in mediacat-ctl.sh."""
    cmd = ["docker", "compose", "--env-file", ".env", "-f", "deploy/docker-compose.yaml"]
    if (proj / "deploy" / "docker-compose.dev.yaml").exists():
        cmd += ["-f", "deploy/docker-compose.dev.yaml"]
    return [*cmd, *args]


def _run(proj: Path, *args: str) -> str:
    out = subprocess.run(  # noqa: S603 - fixed docker compose command
        _compose_cmd(proj, *args),
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return out.stdout


def parse_ps(raw: str) -> list[dict[str, Any]]:
    """Parse ``docker compose ps --format json`` (JSON lines or one array)."""
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        rows = json.loads(raw)
    else:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [r for r in rows if isinstance(r, dict)]


def _state(row: dict[str, Any] | None) -> dict[str, str]:
    if row is None:
        return {"badge": "off", "label": "Not created", "detail": ""}
    state = (row.get("State") or "").lower()
    health = (row.get("Health") or "").lower()
    detail = row.get("Status") or ""
    if state == "running":
        if health == "unhealthy":
            return {"badge": "bad", "label": "Unhealthy", "detail": detail}
        if health == "starting":
            return {"badge": "warn", "label": "Starting…", "detail": detail}
        label = "Healthy" if health == "healthy" else "Running"
        return {"badge": "ok", "label": label, "detail": detail}
    if state in ("restarting", "created", "paused"):
        return {"badge": "warn", "label": state.capitalize(), "detail": detail}
    return {"badge": "off", "label": state.capitalize() or "Stopped", "detail": detail}


def stack_status(proj: Path) -> dict[str, Any]:
    """Return ``{"services": [...], "running": n, "total": n, "error": str}``."""
    try:
        core = [s for s in _run(proj, "config", "--services").split() if s]
        rows = parse_ps(_run(proj, "--profile", "ollama", "ps", "--all", "--format", "json"))
    except FileNotFoundError:
        return {"services": [], "running": 0, "total": 0, "error": "docker not found on PATH"}
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()[-1:] or [str(exc)]
        return {"services": [], "running": 0, "total": 0, "error": msg[0]}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"services": [], "running": 0, "total": 0, "error": str(exc)}

    by_service = {r.get("Service"): r for r in rows}
    names = core + sorted(s for s in by_service if s and s not in core)
    services = [{"name": n, **_state(by_service.get(n))} for n in names]
    running = sum(1 for s in services if s["badge"] == "ok")
    return {"services": services, "running": running, "total": len(services), "error": ""}


def read_env(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader (KEY=VALUE, optional quotes; later lines win)."""
    env: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.split(" #", 1)[0].strip().strip("'\"")
    return env


def public_urls(proj: Path) -> dict[str, str]:
    """Browser URLs of the stack, as Caddy publishes them.

    Caddy listens on ``HTTPS_BIND`` (default ``127.0.0.1:8443``), not on 443,
    so the port must be part of the URL unless it is the scheme's default.
    """
    env = read_env(proj / ".env")
    scheme = env.get("PUBLIC_SCHEME") or "https"
    host = env.get("PUBLIC_HOSTNAME") or "mediacat.localhost"
    bind = env.get("HTTPS_BIND" if scheme == "https" else "HTTP_BIND") or (
        "127.0.0.1:8443" if scheme == "https" else "127.0.0.1:8080"
    )
    port = bind.rsplit(":", 1)[-1]
    suffix = "" if port == {"https": "443", "http": "80"}.get(scheme) else f":{port}"
    return {
        "app": f"{scheme}://{host}{suffix}/",
        "docs": f"{scheme}://docs.{host}{suffix}/",
        "minio": f"{scheme}://minio.{host}{suffix}/",
    }


def docs_running() -> bool:
    try:
        os.kill(int(DOCS_PID.read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True
