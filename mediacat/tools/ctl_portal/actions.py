"""The control panel's action list: the ``mediacat-ctl.sh`` menu, as data.

Numbers and labels match the terminal menu exactly (a test checks this against
``draw_menu`` in the script), and every action runs as
``mediacat-ctl.sh --run N``, so the menu and the web page execute the same
commands.  The portal adds only presentation: sections, colours, and which
actions need a confirmation tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJ_DIR = Path(__file__).resolve().parents[2]
SCRIPT = PROJ_DIR / "scripts" / "mediacat-ctl.sh"

SERVICES = ("app", "worker", "caddy", "postgres", "redis", "minio", "opa", "ollama")


@dataclass(frozen=True)
class Action:
    num: int
    label: str
    kind: str = "plain"  # plain | logs | logs_custom | danger
    danger: str = ""  # "!", "!!", "!!!" for the cleanup actions
    warning: str = ""  # shown next to the confirmation tick


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    blurb: str
    actions: tuple[Action, ...] = field(default_factory=tuple)


SECTIONS: tuple[Section, ...] = (
    Section(
        "lifecycle",
        "Start / Stop / Restart",
        "Bring the Docker stack up or down. Starting also starts the docs server.",
        (
            Action(1, "Start stack (dev)"),
            Action(2, "Stop stack"),
            Action(3, "Restart stack (down + up)"),
            Action(4, "Restart: app container"),
            Action(5, "Restart: worker container"),
            Action(6, "Start + Ollama (CPU)"),
            Action(7, "Start + Ollama (GPU)"),
        ),
    ),
    Section(
        "build",
        "Rebuild images",
        "Rebuild after code or dependency changes; the source is baked into the image.",
        (
            Action(8, "Rebuild: app + worker"),
            Action(9, "Rebuild: app only"),
            Action(10, "Rebuild: worker only"),
            Action(11, "Pull latest base images"),
            Action(12, "Rebuild + restart: app"),
            Action(13, "Rebuild + restart: all"),
        ),
    ),
    Section(
        "services",
        "Service control",
        "Restart a single infrastructure container.",
        (
            Action(14, "Restart: postgres"),
            Action(15, "Restart: redis"),
            Action(16, "Restart: minio"),
            Action(17, "Restart: caddy"),
            Action(18, "Restart: opa"),
            Action(19, "Restart: ollama"),
            Action(20, "Pull Ollama models"),
        ),
    ),
    Section(
        "database",
        "Database",
        "Alembic migrations, run inside the app container as mediacat_migrator.",
        (
            Action(21, "Run migrations (upgrade)"),
            Action(22, "Migration status (current)"),
        ),
    ),
    Section(
        "monitor",
        "Monitor / Logs",
        "Logs are shown once (last N lines), not followed. Use the terminal menu to follow live.",
        (
            Action(23, "Stack status (ps)"),
            Action(24, "Logs: all services", "logs"),
            Action(25, "Logs: app", "logs"),
            Action(26, "Logs: worker", "logs"),
            Action(27, "Logs: postgres", "logs"),
            Action(28, "Logs: redis", "logs"),
            Action(29, "Logs: caddy", "logs"),
            Action(30, "Logs: minio", "logs"),
            Action(31, "Logs: custom service + lines", "logs_custom"),
        ),
    ),
    Section(
        "docs",
        "Docs server",
        "MkDocs on 127.0.0.1:8800, also through Caddy (Docs link at the top).",
        (
            Action(38, "Start docs server"),
            Action(39, "Stop docs server"),
            Action(40, "Rebuild API reference"),
            Action(41, "Docs server status"),
        ),
    ),
    Section(
        "cleanup",
        "Cleanup",
        "See docs/runbooks/cleanup-stack.md for full details.",
        (
            Action(32, "Prune: containers + networks"),
            Action(33, "Prune: unused images"),
            Action(34, "Make clean (build artefacts)"),
            Action(
                35,
                "Wipe catalogue data (usr ok) [!]",
                "danger",
                "!",
                "Catalogue rows only. Users, schema, MinIO and certs stay intact.",
            ),
            Action(
                36,
                "Down + remove volumes [!!]",
                "danger",
                "!!",
                "Destroys the PostgreSQL DB, MinIO objects and Caddy TLS cert. "
                "Afterwards run migrations [21] and re-import the Caddy cert.",
            ),
            Action(
                37,
                "Full docker system prune [!!!]",
                "danger",
                "!!!",
                "Nuclear. Removes ALL unused Docker data system-wide, "
                "for every project on this machine.",
            ),
        ),
    ),
)

BY_NUM: dict[int, Action] = {a.num: a for s in SECTIONS for a in s.actions}
