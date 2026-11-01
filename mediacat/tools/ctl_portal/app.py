"""MediaCat control panel: the mediacat-ctl.sh menu as a web page.

Loopback only.  Mutations are POST with a per-process CSRF token (cookie +
form field) plus an Origin/Referer check, and every request must carry a
loopback Host header (DNS-rebinding guard).  Actions run as background jobs
with a progress page, one at a time — the same pattern as the Music Lab portal.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tools.ctl_portal.actions import BY_NUM, PROJ_DIR, SCRIPT, SECTIONS, SERVICES
from tools.ctl_portal.jobs import JobRunner
from tools.ctl_portal.status import docs_running, public_urls, stack_status

HERE = Path(__file__).parent
DEFAULT_PORT = 8810
LOG_DIR = Path.home() / ".local" / "state" / "mediacat-ctl"


def create_app(  # noqa: PLR0915 - route closures share runner/csrf state
    *,
    port: int = DEFAULT_PORT,
    script: Path = SCRIPT,
    proj_dir: Path = PROJ_DIR,
    log_dir: Path = LOG_DIR,
    status_fn: Any = None,
) -> FastAPI:
    runner = JobRunner(script, proj_dir, log_dir)
    get_status = status_fn or (lambda: stack_status(proj_dir))
    csrf = secrets.token_urlsafe(24)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}
    urls = public_urls(proj_dir)

    app = FastAPI(title="MediaCat Control", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")

    @app.middleware("http")
    async def loopback_host_only(request: Request, call_next: Any) -> Any:
        if request.headers.get("host", "") not in allowed:
            return PlainTextResponse("host not allowed", status_code=403)
        return await call_next(request)

    def check(request: Request, token: str) -> None:
        if not secrets.compare_digest(token, csrf) or request.cookies.get("mc_ctl_csrf") != csrf:
            raise HTTPException(403, "stale form: reload the page and try again")
        src = request.headers.get("origin") or request.headers.get("referer") or ""
        if src and urlparse(src).netloc not in allowed:
            raise HTTPException(403, "cross-origin request refused")

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        r = tpl.TemplateResponse(
            request,
            name,
            {"csrf": csrf, "busy": runner.active(), "proj_dir": str(proj_dir), "urls": urls, **ctx},
        )
        r.set_cookie("mc_ctl_csrf", csrf, httponly=True, samesite="strict")
        return r

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        recent = [j.as_dict(tail=0) for j in reversed(list(runner.jobs.values()))][:8]
        st = get_status()
        app_svc = next((s for s in st["services"] if s["name"] == "app"), None)
        return page(
            request,
            "home.html",
            st=st,
            app_svc=app_svc,
            docs=docs_running(),
            sections=SECTIONS,
            services=SERVICES,
            recent=recent,
        )

    @app.post("/run")
    def run(
        request: Request,
        action: int = Form(...),
        token: str = Form(...),
        tail: int = Form(50),
        service: str = Form("app"),
        confirm: str = Form(""),
    ) -> RedirectResponse:
        check(request, token)
        act = BY_NUM.get(action)
        if act is None:
            raise HTTPException(400, f"unknown action {action}")
        env: dict[str, str] = {}
        if act.kind in ("logs", "logs_custom"):
            env["CTL_TAIL"] = str(min(max(tail, 1), 5000))
        if act.kind == "logs_custom":
            if service not in SERVICES:
                raise HTTPException(400, "unknown service")
            env["CTL_SERVICE"] = service
        if act.kind == "danger":
            if confirm != "yes":
                raise HTTPException(400, "tick the confirmation box to run this action")
            env["CTL_CONFIRM"] = "yes"
        title = act.label if act.kind != "logs_custom" else f"Logs: {service}"
        job = runner.start(act.num, title, env)
        return RedirectResponse(f"/job/{job.id}", status_code=303)

    @app.get("/job/{jid}", response_class=HTMLResponse)
    def job_page(request: Request, jid: str) -> HTMLResponse:
        job = runner.get(jid)
        if not job:
            raise HTTPException(404, "job not found (the control panel was restarted?)")
        return page(request, "job.html", job=job.as_dict())

    @app.post("/job/{jid}/cancel")
    def job_cancel(request: Request, jid: str, token: str = Form(...)) -> RedirectResponse:
        check(request, token)
        job = runner.get(jid)
        if job:
            runner.cancel(job)
        return RedirectResponse(f"/job/{jid}", status_code=303)

    @app.get("/api/jobs/{jid}")
    def job_api(jid: str) -> dict[str, object]:
        job = runner.get(jid)
        if not job:
            raise HTTPException(404)
        return job.as_dict()

    @app.get("/api/status")
    def status_api() -> JSONResponse:
        return JSONResponse({**get_status(), "docs": docs_running()})

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    return app
