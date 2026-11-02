# CLAUDE.md — Project context for Claude Code

## What this is
MediaCat — cataloguing platform for vinyl + CD. Python 3.12, FastAPI,
PostgreSQL 16, MinIO, Redis, OPA. Docker Compose deployment on WSL2.

## Key commands
- `make setup` — create venv, install deps, install pre-commit
- `make lint` / `make typecheck` / `make test` — quality gates
- `make up` / `make down` — Docker Compose lifecycle (always uses `--env-file .env`)
- `make ollama-gpu-up` / `make ollama-down` — Ollama + Open WebUI stack
- Migrations: `docker compose --env-file .env -f deploy/docker-compose.yaml exec -e PGHOST=postgres -e PGUSER=mediacat_migrator -e PGDATABASE=mediacat app alembic -c alembic.ini upgrade head` (run from `mediacat/` dir; service is `app` not `worker`)

## Architecture decisions
- Hybrid AI: local Ollama default, API fallback (ADR-0002)
- OPA for rule engine, Python fallback (ADR-0003)
- Data/code separation via host bind mounts (ADR-0004)
- CSRF via X-CSRF-Token header (not form body)
- LLMs are advisory only, never autonomous
- Dark-first UI: CSS custom properties, [data-theme="light"] override, no build step
- Catalogue: Artist→Album→Pressing derived from Token table (no separate tables)
- `--env-file .env` required on all docker compose commands (Compose v2 resolves .env from compose file dir)
- Secret file modes: postgres_password=0600 (root only), others=0644 (app uid 10001 readable)
- WSL2 mirrored networking required for Windows browser access to 127.0.0.1
- Caddy local CA for .localhost HTTPS; root cert imported once into Windows cert store. Leaf certs are 30 days (`(local_tls)` snippet) with a 90-day intermediate: Caddy's default 12 h certs expired while the PC slept, so the browser warned "not secure" after wake-up until the next renewal
- Browser address is `https://mediacat.localhost:8443/` — Caddy publishes on `HTTPS_BIND` (127.0.0.1:8443); host port 443 belongs to Kasm Workspaces (`kasm_proxy`), so port-less links land on Kasm's login. Always write URLs with `:8443` in docs/UI
- Docs `.docx` files are pandoc output of their `.md` source: edit the `.md`, then `pandoc X.md -o X.docx --reference-doc=<old X.docx>` to keep styles
- Discography: MusicBrainz primary → Wikipedia fallback; Spotify enrichment via `ingestion/spotify.py` (client-credentials, no user auth); credentials stored in SiteSetting (Admin → API Tokens)
- Spotify API: omit `include_groups` and `market` params — Spotify rejects them with misleading "Invalid limit" 400; pagination uses `data["next"]` verbatim
- Discogs collection custom fields (Runout / Matrix LP1–2 A/B, Autographed, Signed by…) and release identifiers live in `Token.extra["discogs_collection"]` / `["discogs"]` (no migration); field names matched by pattern in `ingestion/discogs_extras.py`; write-back uses the collection instance API (`web/discogs_details.py`) — release identifiers themselves cannot be edited via the Discogs API
- `backend` network is `internal: true` (no internet): containers on it must not install packages at start-up — backup sidecar uses the image's BusyBox `crond` (daily pg_dump 02:00 UTC → `/srv/mediacat/backups/postgres`)
- Web control panel `tools/ctl_portal` (`make ctl-web`, 127.0.0.1:8810) runs `scripts/mediacat-ctl.sh --run N`; keep menu numbers/labels in `tools/ctl_portal/actions.py` in sync (enforced by `tests/test_ctl_portal.py`)
- CSP `script-src 'self'` blocks all inline JS including HTMX `hx-on` attributes — wire all event handlers in static `.js` files instead; list-row highlighting uses `data-mc-active="row-class"` (handler in `forms.js`)
- Top-bar search (`routes.global_search`, `/search?q=&scope=`): scopes `all` (artists + album titles/cat. no.), `artists`, `albums`, `tracks`; track titles come from `Token.tracklist` JSONB via a lateral `jsonb_array_elements` (no track table). Album/track hits link to `/catalogue/{id}`; a single album/track hit answers `HX-Redirect`
- Close every `<dialog>` with `</dialog>` (enforced by `tests/test_global_search.py`): a stray `</div>` ends the 3panel grid early and the right panel renders full width
- Add pressing cards view: `/catalogue/new/discogs-preview/{id}` (lazy per card, `intersect once`, 1 h in-memory cache, `discogs_extras.release_preview`); Discogs search API returns no images, the release endpoint does
- Static files are served without `Cache-Control`: after a deploy the browser may run old JS/CSS until Ctrl+F5
- CSP lives in `SecurityHeadersMiddleware` (the `security.csp` key in app.yaml is not read); `img-src` must list every external image host (Wikipedia uses both `upload.` and `thumb.wikimedia.org`)
- Artist bios (`ingestion/artist_bio.py`): Wikipedia (MusicBrainz link, trusted; or search with title/music checks + LLM verdict) — used as-is when ≥ 800 chars — else compared with the Discogs artist profile (`choose_bio`: Discogs needs ≥ 2× the length and a real bio, ≥ 150 chars / 2 sentences) → official homepage summarised by the LLM ("AI summary") → cached "Not found" statement; sized 1200–2500 chars; Discogs "(N)" name suffix dropped for MusicBrainz/Wikipedia only
- Bio lookups run as server-side tasks (`web/artists.py` `_bio_jobs`, awaited via `asyncio.shield`): they finish and save even if the browser leaves; concurrent requests join the running task. The "Research in progress" banner is `static/bio_research.js`
- Text LLM is `app.state.llm` (HybridLlm, Ollama `OLLAMA_TEXT_MODEL` / `llm.text_model`, defaults to the vision model); pass untrusted text through `llm.safety.build_prompt`
- Login + dashboard background (`web/background.py`, `partials/page_background.html`, `static/background.js`): SiteSetting `background` (JSON: mode video|image, uploaded image/mp4/webm); uploads checked by content (JPEG/PNG/WebP, MP4 H.264/AV1, WebM VP8/VP9/AV1), stored in MinIO `site/background/<sha256>.<ext>`, served publicly with Range by `/site-media/background/{name}`; built-in defaults `static/media-cat-hero*`; managed in Admin → Layout. Admin page extends `base.html`, which loads only `style.css`
- Quality gates: `make lint`, `ruff format --check`, `make typecheck` (mypy src: 0 errors), bandit (0 findings) all clean; pre-commit runs mypy/bandit from the venv. Bandit reasons go *before* the marker: `# reason  # nosec B105`
- `.bat` files keep CRLF (`/.gitattributes`); everything else LF
- Discogs API calls take their token from `catalogue._discogs_token(request)`: Admin → API Tokens (SiteSetting `discogs_token`), then the app connector, then `DISCOGS_TOKEN`; pass it to `_fetch_discogs_release` / `_discogs_search` / `_import_discogs_images` (Discogs search needs auth)
- Sessions sign the user's timeout into the token (`user_id|role|nonce|lifetime`), enforced server-side; login reads the DB user first (memory bootstrap store only for unknown usernames), enforces `locked_until` and counts failures on the row; `routes._audit()` writes `audit_log` best-effort
- Backup import converts the export's text values back by column type (`information_schema`) and restores `tokens.current_revision_id` after `token_revisions` (`_RESTORE_LATER`)
- Integration tests (`tests/integration/`, `make test-integration`): throwaway `postgres:16-alpine` container per session (never the Compose DB), DB `mediacat_test` built by `alembic upgrade head`, app as `mediacat_app`; per-test reset + seed restore; clients per role via `make_client(role)`; outbound network blocked — stub it with the `fake_http` fixture (`FakeHttp`) or monkeypatch. `make test` needs Docker; `make test-unit` doesn't. Coverage needs `concurrency = ["greenlet", "thread"]` (SQLAlchemy async)

## Open items
- None known. Convention: a bug found but not fixed yet is pinned by a strict `xfail(reason="BUG: …")` test; fixing it makes the test XPASS → remove the marker

## Documentation
- MediaCat_Technical_Reference.md — architecture, schema, API, configuration
- MediaCat_Installation_Guide.md — installation, validation, troubleshooting
