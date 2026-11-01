"""Tests for the web control panel (tools/ctl_portal) and its link to mediacat-ctl.sh."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tools.ctl_portal.actions import BY_NUM, SCRIPT
from tools.ctl_portal.app import create_app

FAKE_STATUS = {
    "services": [{"name": "app", "badge": "ok", "label": "Healthy", "detail": "Up 2 hours"}],
    "running": 1,
    "total": 1,
    "error": "",
}


def _menu_labels() -> dict[int, str]:
    """Parse the ``row2 N "label" M "label"`` lines of draw_menu in the script."""
    src = SCRIPT.read_text(encoding="utf-8")
    labels: dict[int, str] = {}
    for m in re.finditer(r"^\s*row2\s+(.*)$", src, re.MULTILINE):
        for num, label in re.findall(r'(\d+)\s+"([^"]*)"', m.group(1)):
            labels[int(num)] = label
    return labels


def test_actions_match_terminal_menu() -> None:
    menu = _menu_labels()
    assert menu, "draw_menu not found in mediacat-ctl.sh"
    assert {n: a.label for n, a in BY_NUM.items()} == menu


def test_every_action_has_a_case_branch() -> None:
    src = SCRIPT.read_text(encoding="utf-8")
    for num in BY_NUM:
        assert re.search(rf"^\s*{num}\)", src, re.MULTILINE), f"no case branch for {num}"


@pytest.fixture
def fake_script(tmp_path: Path) -> Path:
    script = tmp_path / "ctl.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'echo "action=$2 tail=${CTL_TAIL:-} svc=${CTL_SERVICE:-} confirm=${CTL_CONFIRM:-}"\n'
        'printf "\\e[1;32mcoloured\\e[0m\\n"\n'
        '[[ "$2" == "9" ]] && sleep 30\n'
        '[[ "$2" == "22" ]] && exit 3\n'
        "exit 0\n"
    )
    return script


@pytest.fixture
def client(tmp_path: Path, fake_script: Path) -> TestClient:
    app = create_app(
        script=fake_script,
        proj_dir=tmp_path,
        log_dir=tmp_path / "logs",
        status_fn=lambda: FAKE_STATUS,
    )
    c = TestClient(app)
    c.get("/")  # sets the CSRF cookie
    return c


def _token(c: TestClient) -> str:
    m = re.search(r'name="token" value="([^"]+)"', c.get("/").text)
    assert m
    return m.group(1)


def _wait(c: TestClient, url: str) -> dict:
    jid = url.rsplit("/", 1)[-1]
    for _ in range(100):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    return j


def test_home_lists_sections_and_status(client: TestClient) -> None:
    html = client.get("/").text
    assert "Start / Stop / Restart" in html
    assert "[37]" in html
    assert "1 of 1 running" in html


def test_foreign_host_refused(client: TestClient) -> None:
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403


def test_run_requires_csrf(client: TestClient) -> None:
    assert client.post("/run", data={"action": 23, "token": "wrong"}).status_code == 403


def test_run_refuses_cross_origin(client: TestClient) -> None:
    r = client.post(
        "/run",
        data={"action": 23, "token": _token(client)},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_danger_needs_confirmation(client: TestClient) -> None:
    tok = _token(client)
    assert client.post("/run", data={"action": 36, "token": tok}).status_code == 400
    r = client.post(
        "/run", data={"action": 36, "token": tok, "confirm": "yes"}, follow_redirects=False
    )
    job = _wait(client, r.headers["location"])
    assert "confirm=yes" in job["log"][0]


def test_unknown_action_and_service_rejected(client: TestClient) -> None:
    tok = _token(client)
    assert client.post("/run", data={"action": 0, "token": tok}).status_code == 400
    r = client.post("/run", data={"action": 31, "token": tok, "service": "app;rm"})
    assert r.status_code == 400


def test_logs_job_passes_inputs_and_strips_ansi(client: TestClient) -> None:
    r = client.post(
        "/run",
        data={"action": 31, "token": _token(client), "tail": 7, "service": "worker"},
        follow_redirects=False,
    )
    job = _wait(client, r.headers["location"])
    assert job["status"] == "done"
    assert job["log"][0] == "action=31 tail=7 svc=worker confirm="
    assert job["log"][1] == "coloured"
    assert job["title"] == "Logs: worker"


def test_failed_exit_code_reported(client: TestClient) -> None:
    r = client.post("/run", data={"action": 22, "token": _token(client)}, follow_redirects=False)
    job = _wait(client, r.headers["location"])
    assert job["status"] == "failed"
    assert job["exit_code"] == 3


def test_one_job_at_a_time_and_cancel(client: TestClient) -> None:
    tok = _token(client)
    first = client.post("/run", data={"action": 9, "token": tok}, follow_redirects=False)
    second = client.post("/run", data={"action": 23, "token": tok}, follow_redirects=False)
    assert first.headers["location"] == second.headers["location"]
    jid = first.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/job/{jid}/cancel", data={"token": tok})
    assert _wait(client, first.headers["location"])["status"] == "cancelled"


@pytest.mark.parametrize(
    ("env", "app_url"),
    [
        ("", "https://mediacat.localhost:8443/"),
        (
            "PUBLIC_HOSTNAME=mediacat.localhost\nHTTPS_BIND=127.0.0.1:8443\n",
            "https://mediacat.localhost:8443/",
        ),
        ("HTTPS_BIND=0.0.0.0:443\nPUBLIC_HOSTNAME='cat.example'\n", "https://cat.example/"),
        ("PUBLIC_SCHEME=http\nHTTP_BIND=127.0.0.1:8080\n", "http://mediacat.localhost:8080/"),
    ],
)
def test_public_urls_include_caddy_port(tmp_path: Path, env: str, app_url: str) -> None:
    from tools.ctl_portal.status import public_urls

    (tmp_path / ".env").write_text(env)
    urls = public_urls(tmp_path)
    assert urls["app"] == app_url
    assert urls["docs"] == app_url.replace("://", "://docs.", 1)


def test_home_shows_app_card_first(client: TestClient) -> None:
    html = client.get("/").text
    hero = html.index('class="card app-hero"')
    assert hero < html.index('class="card status"')
    assert 'href="https://mediacat.localhost:8443/"' in html
    assert "App healthy" in html
