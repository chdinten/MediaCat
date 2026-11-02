"""Background jobs: run ``mediacat-ctl.sh --run N`` and keep its output for the job page.

Same model as the Music Lab portal: one job at a time, the page polls
``/api/jobs/{id}``, and output is also written to a log file.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_LINES = 2000


@dataclass
class Job:
    id: str
    title: str
    num: int
    status: str = "running"  # running | done | failed | cancelled
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    proc: subprocess.Popen[str] | None = None

    def as_dict(self, tail: int = 400) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "num": self.num,
            "status": self.status,
            "exit_code": self.exit_code,
            "log": self.lines[-tail:],
            "truncated": max(len(self.lines) - tail, 0),
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
        }


class JobRunner:
    def __init__(self, script: Path, cwd: Path, logdir: Path) -> None:
        self.script = script
        self.cwd = cwd
        self.logdir = logdir
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def active(self) -> Job | None:
        return next((j for j in reversed(self.jobs.values()) if j.status == "running"), None)

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def start(self, num: int, title: str, env: dict[str, str] | None = None) -> Job:
        """Start action *num*; returns the running job (or the one already running)."""
        with self._lock:
            busy = self.active()
            if busy:
                return busy
            jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            job = Job(jid, title, num)
            self.jobs[jid] = job
            # Keep the most recent 50 jobs in memory
            for old in list(self.jobs)[:-50]:
                del self.jobs[old]

        run_env = {**os.environ, "TERM": "dumb", **(env or {})}
        # bash from PATH, like the terminal menu; fixed script, numeric argument
        job.proc = subprocess.Popen(  # noqa: S603
            ["bash", str(self.script), "--run", str(num)],  # noqa: S607
            cwd=self.cwd,
            env=run_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group, so Cancel stops children too
        )
        threading.Thread(target=self._pump, args=(job,), name=f"job-{jid}", daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        self.logdir.mkdir(parents=True, exist_ok=True)
        if job.proc is None or job.proc.stdout is None:
            return
        with (self.logdir / f"job-{job.id}.log").open("w", encoding="utf-8") as log:
            log.write(f"# [{job.num}] {job.title}\n")
            for raw in job.proc.stdout:
                line = _ANSI.sub("", raw.rstrip("\n")).replace("\r", "")
                job.lines.append(line)
                if len(job.lines) > MAX_LINES:
                    del job.lines[: len(job.lines) - MAX_LINES]
                log.write(line + "\n")
                log.flush()
        job.proc.stdout.close()
        job.exit_code = job.proc.wait()
        job.finished = time.time()
        if job.status != "cancelled":
            job.status = "done" if job.exit_code == 0 else "failed"

    def cancel(self, job: Job) -> None:
        if job.status != "running" or job.proc is None:
            return
        job.status = "cancelled"
        job.lines.append("— cancelled from the control panel —")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(job.proc.pid, signal.SIGTERM)
