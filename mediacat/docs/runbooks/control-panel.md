# Control Panel and Addresses

## Opening MediaCat

| What | Address |
|---|---|
| **MediaCat app** | `https://mediacat.localhost:8443/` |
| Documentation (this site, when the docs server runs) | `https://docs.mediacat.localhost:8443/` |
| MinIO console | `https://minio.mediacat.localhost:8443/` |
| Open WebUI (Ollama profile) | `https://ollama.mediacat.localhost:8443/` |
| Stack control panel | `http://127.0.0.1:8810/` |

!!! warning "Always include `:8443`"
    Caddy publishes HTTPS on `HTTPS_BIND` (default `127.0.0.1:8443`), not on the
    standard port 443. An address without the port reaches whatever else listens on
    443. On this machine that is **Kasm Workspaces**, which shows its own login page.
    If 443 is free and you prefer short addresses, set `HTTPS_BIND=127.0.0.1:443` in
    `.env` and restart Caddy.

## Web control panel

The terminal menu `scripts/mediacat-ctl.sh` is also available as a web page, built
like the Music Lab portal.

```bash
cd /mnt/c/MEGA/SoundDB/mediacat
make ctl-web            # or: .venv/bin/python -m tools.ctl_portal [--port N]
```

Open `http://127.0.0.1:8810/` in a Windows browser (WSL2 mirrored networking).

- **Media Catalogue card** at the top: the app's address, its health, and an
  **Open MediaCat** button. Addresses are read from `.env`, so they follow
  `PUBLIC_HOSTNAME` and `HTTPS_BIND`.
- **Docker stack card**: every container's state (healthy, running, stopped)
  and the docs server.
- **One card per menu section**, with the same numbers as the terminal menu.
- Clicking an action runs `scripts/mediacat-ctl.sh --run N` as a background job
  and opens a progress page with live output. One action runs at a time;
  **Cancel** stops it.
- Logs (23–31) are printed once with the chosen number of lines, not followed.
- The dangerous cleanup actions (35–37) need a confirmation tick; see
  [Stack cleanup](cleanup-stack.md).
- Loopback only: every action is a POST with a CSRF token, and requests from
  other hosts or origins are refused.

### Run it as a user service

```bash
mkdir -p ~/.config/systemd/user
sed "s#@CHECKOUT@#$PWD#" tools/ctl_portal/mediacat-ctl-web.service.in \
    > ~/.config/systemd/user/mediacat-ctl-web.service
systemctl --user daemon-reload && systemctl --user enable --now mediacat-ctl-web
```

### Non-interactive menu

`mediacat-ctl.sh --run N` runs one action and exits with its status. Inputs the
menu would prompt for come from the environment: `CTL_TAIL` (log lines),
`CTL_SERVICE` (action 31) and `CTL_CONFIRM=yes` (required for 35–37). A test
(`tests/test_ctl_portal.py`) fails if the web page's numbers or labels drift from
the menu.
