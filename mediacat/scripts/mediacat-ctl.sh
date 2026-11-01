#!/usr/bin/env bash
# =============================================================================
#  mediacat-ctl  —  MediaCat Docker stack management
#
#  Location : ~/mediacat-ctl.sh   (WSL2: /mnt/c/Users/chdin/mediacat-ctl.sh)
#  Usage    : bash ~/mediacat-ctl.sh
#             chmod +x ~/mediacat-ctl.sh  &&  ~/mediacat-ctl.sh
#             mediacat-ctl.sh --run N    run one menu action and exit (no menu)
#
#  --run mode is what the web control panel (tools/ctl_portal) calls, so the
#  menu and the web page always execute the same commands.  Inputs that the
#  menu would prompt for come from the environment instead:
#      CTL_TAIL=N          log lines   (default 20)
#      CTL_SERVICE=name    service for action 31
#      CTL_CONFIRM=yes     required for the dangerous actions 35-37
#  Logs are printed once instead of followed.  Exit status is the action's.
# =============================================================================
set -uo pipefail

BATCH=0
FOLLOW=(-f)
if [[ "${1:-}" == "--run" ]]; then
    BATCH=1
    FOLLOW=()
fi

PROJ_DIR="/mnt/c/MEGA/SoundDB/mediacat"
DEFAULT_TAIL=20
_DOCS_PID="/tmp/mediacat-docs.pid"
_DOCS_LOG="/tmp/mediacat-docs.log"

# ── ANSI colours ──────────────────────────────────────────────────────────────
R=$'\e[1;31m'   # bold red   — selection numbers
B=$'\e[0;34m'   # blue       — item text
G=$'\e[1;32m'   # bold green — frame / headers / footers
D=$'\e[0m'      # reset

# ── Frame geometry ─────────────────────────────────────────────────────────────
#   Full line: │ + W chars + │  =  W+2 total visible cols
#   Column row: │ + H + │ + H + │  →  H+1+H = W
W=79   # inner full width  (between outer │ borders)
H=39   # column width      (39 + 1 separator + 39 = 79 = W)

HR_W=$(printf '─%.0s' $(seq 1 $W))
HR_H=$(printf '─%.0s' $(seq 1 $H))

# ── Border / separator primitives ─────────────────────────────────────────────
top_border() { printf "${G}┌${HR_W}┐${D}\n"; }
bot_border() { printf "${G}└${HR_W}┘${D}\n"; }
full_sep()   { printf "${G}├${HR_W}┤${D}\n"; }
col_open()   { printf "${G}├${HR_H}┬${HR_H}┤${D}\n"; }   # → two columns
col_close()  { printf "${G}├${HR_H}┴${HR_H}┤${D}\n"; }   # ← back to full-width

# Full-width content row  ($1 = plain ASCII text, padded to W visible chars)
full_row() { printf "${G}│${D}%-*s${G}│${D}\n" "$W" "$1"; }

# ── Cell builder ──────────────────────────────────────────────────────────────
# Outputs exactly H visible characters: coloured [N] + blue text + space padding
cell() {
    local num="$1" text="$2"
    if [[ -z "$num" ]]; then
        printf "%${H}s" ""
        return
    fi
    local label="[$num]"
    local pad=$(( H - ${#label} - 1 - ${#text} ))
    (( pad < 0 )) && pad=0
    printf "${R}%s${D} ${B}%s${D}%*s" "$label" "$text" "$pad" ""
}

# Two-column item row; pass "" for num/text to leave a cell empty
row2() { printf "${G}│${D}$(cell "$1" "$2")${G}│${D}$(cell "$3" "$4")${G}│${D}\n"; }

# Section divider: closes column block → prints green header → opens new block
section() {
    col_close
    printf "${G}│  %-*s│${D}\n" $(( W - 2 )) "-- $1"
    col_open
}

# ── Compose helpers ───────────────────────────────────────────────────────────
_compose() {
    local files=(docker compose --env-file .env -f deploy/docker-compose.yaml)
    [[ -f "$PROJ_DIR/deploy/docker-compose.dev.yaml" ]] &&
        files+=(-f deploy/docker-compose.dev.yaml)
    ( cd "$PROJ_DIR" && "${files[@]}" "$@" )
}

_compose_ollama() {
    local gpu="$1"; shift
    local files=(docker compose --env-file .env -f deploy/docker-compose.yaml)
    [[ -f "$PROJ_DIR/deploy/docker-compose.dev.yaml" ]] &&
        files+=(-f deploy/docker-compose.dev.yaml)
    [[ "$gpu" == "1" && -f "$PROJ_DIR/deploy/docker-compose.gpu.yaml" ]] &&
        files+=(-f deploy/docker-compose.gpu.yaml)
    ( cd "$PROJ_DIR" && "${files[@]}" --profile ollama "$@" )
}

# ── Input helpers ─────────────────────────────────────────────────────────────
# Prompts go to stderr: these helpers run inside $(...), which captures stdout.
ask_tail() {
    local n
    if (( BATCH )); then
        n="${CTL_TAIL:-$DEFAULT_TAIL}"
    else
        printf "  Lines to tail [%d]: " "$DEFAULT_TAIL" >&2
        read -r n
    fi
    n="${n:-$DEFAULT_TAIL}"
    [[ "$n" =~ ^[0-9]+$ ]] || n=$DEFAULT_TAIL
    printf "%s" "$n"
}

SERVICES="app|worker|caddy|postgres|redis|minio|opa|ollama"

ask_service() {
    local s
    if (( BATCH )); then
        s="${CTL_SERVICE:-app}"
    else
        printf "  Service (%s) [app]: " "$SERVICES" >&2
        read -r s
    fi
    s="${s:-app}"
    [[ "|$SERVICES|" == *"|$s|"* ]] || s=app
    printf "%s" "$s"
}

confirm_dangerous() {
    if (( BATCH )); then
        printf "  WARNING: %s\n" "$1"
        [[ "${CTL_CONFIRM:-}" == "yes" ]] && { echo "  Confirmed by the control panel."; return 0; }
        echo "  Refused: confirmation missing."
        return 1
    fi
    printf "  ${R}WARNING:${D} %s\n  Continue? [y/N]: " "$1"
    local yn; read -r yn
    [[ "$yn" =~ ^[Yy]$ ]]
}

# ── Public URL (Caddy publishes on HTTPS_BIND, default 8443 — not 443) ───────
_env_get() {   # last KEY=value in .env, quotes stripped
    grep -E "^$1=" "$PROJ_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"
}

_public_url() {   # $1 = optional subdomain prefix, e.g. "docs."
    local scheme host bind port
    scheme=$(_env_get PUBLIC_SCHEME); scheme=${scheme:-https}
    host=$(_env_get PUBLIC_HOSTNAME); host=${host:-mediacat.localhost}
    if [[ "$scheme" == "https" ]]; then
        bind=$(_env_get HTTPS_BIND); bind=${bind:-127.0.0.1:8443}
    else
        bind=$(_env_get HTTP_BIND); bind=${bind:-127.0.0.1:8080}
    fi
    port=${bind##*:}
    if [[ "$scheme:$port" == "https:443" || "$scheme:$port" == "http:80" ]]; then
        printf '%s://%s%s/' "$scheme" "${1:-}" "$host"
    else
        printf '%s://%s%s:%s/' "$scheme" "${1:-}" "$host" "$port"
    fi
}

# ── Docs server helpers ───────────────────────────────────────────────────────
_docs_is_running() {
    [[ -f "$_DOCS_PID" ]] && kill -0 "$(cat "$_DOCS_PID")" 2>/dev/null
}

_docs_start() {
    if _docs_is_running; then
        echo "  Docs server already running (PID $(cat "$_DOCS_PID")) — http://127.0.0.1:8800"
        return
    fi
    echo "  Generating API reference (pdoc)..."
    if ! ( cd "$PROJ_DIR" && make docs-api ) >/dev/null 2>&1; then
        echo "  Warning: docs-api failed (run 'make setup' to install doc deps) — starting server anyway"
    fi
    ( cd "$PROJ_DIR" && nohup .venv/bin/mkdocs serve -a 0.0.0.0:8800 -f docs/mkdocs.yml \
        >"$_DOCS_LOG" 2>&1 & echo $! >"$_DOCS_PID" )
    sleep 2
    if _docs_is_running; then
        echo "  Docs server started (PID $(cat "$_DOCS_PID"))"
        echo "  Direct:    http://127.0.0.1:8800"
        echo "  Via Caddy: $(_public_url docs.)"
    else
        echo "  Docs server failed to start — see $_DOCS_LOG"
    fi
}

_docs_stop() {
    if _docs_is_running; then
        local pid; pid=$(cat "$_DOCS_PID")
        kill "$pid" 2>/dev/null && echo "  Docs server stopped (PID $pid)" || echo "  Could not stop PID $pid"
    else
        echo "  Docs server not running"
    fi
    rm -f "$_DOCS_PID"
}

# ── Draw menu ─────────────────────────────────────────────────────────────────
draw_menu() {
    clear
    top_border
    full_row "  MediaCat Stack Manager   --   app: $(_public_url)"
    full_row "  ${PROJ_DIR}"
    full_sep

    # First section: no preceding column block, so skip col_close
    full_row "  -- START / STOP / RESTART"
    col_open
    row2  1 "Start stack (dev)"             2 "Stop stack"
    row2  3 "Restart stack (down + up)"     4 "Restart: app container"
    row2  5 "Restart: worker container"     6 "Start + Ollama (CPU)"
    row2  7 "Start + Ollama (GPU)"         "" ""

    section "REBUILD IMAGES"
    row2  8 "Rebuild: app + worker"         9 "Rebuild: app only"
    row2 10 "Rebuild: worker only"         11 "Pull latest base images"
    row2 12 "Rebuild + restart: app"       13 "Rebuild + restart: all"

    section "SERVICE CONTROL"
    row2 14 "Restart: postgres"            15 "Restart: redis"
    row2 16 "Restart: minio"              17 "Restart: caddy"
    row2 18 "Restart: opa"                19 "Restart: ollama"
    row2 20 "Pull Ollama models"           "" ""

    section "DATABASE"
    row2 21 "Run migrations (upgrade)"     22 "Migration status (current)"

    section "MONITOR / LOGS"
    row2 23 "Stack status (ps)"            24 "Logs: all services"
    row2 25 "Logs: app"                    26 "Logs: worker"
    row2 27 "Logs: postgres"              28 "Logs: redis"
    row2 29 "Logs: caddy"                 30 "Logs: minio"
    row2 31 "Logs: custom service + lines" "" ""

    section "DOCS SERVER"
    row2 38 "Start docs server"           39 "Stop docs server"
    row2 40 "Rebuild API reference"       41 "Docs server status"

    section "CLEANUP"
    row2 32 "Prune: containers + networks" 33 "Prune: unused images"
    row2 34 "Make clean (build artefacts)" 35 "Wipe catalogue data (usr ok) [!]"
    row2 36 "Down + remove volumes [!!]"   37 "Full docker system prune [!!!]"

    col_close
    full_sep
    full_row "  CLEANUP NOTES  --  see docs/runbooks/cleanup-stack.md for full details"
    full_sep
    full_row "   [!]  35 -- Catalogue rows only. Users, schema, MinIO and certs intact."
    full_row "  [!!]  36 -- Destroys PostgreSQL DB + MinIO objects + Caddy TLS cert."
    full_row "              Afterwards: run migrations [21] and re-import the Caddy cert."
    full_row " [!!!]  37 -- Nuclear. Removes ALL unused Docker data system-wide."
    full_sep
    printf "${G}│${D}  ${R}[0]${D} ${B}Exit${D}%*s${G}│${D}\n" $(( W - 10 )) ""
    bot_border
}

# ── Action dispatcher ─────────────────────────────────────────────────────────
do_action() {
    local choice="$1"
    local tail svc

    case "$choice" in

        # ── START / STOP / RESTART ─────────────────────────────────────────
        1)  echo "  Starting MediaCat stack (dev)..."
            _compose up -d --remove-orphans
            _docs_start ;;

        2)  echo "  Stopping MediaCat stack..."
            _docs_stop
            _compose down --remove-orphans ;;

        3)  echo "  Restarting stack (down then up)..."
            _docs_stop
            _compose down --remove-orphans
            _compose up -d --remove-orphans
            _docs_start ;;

        4)  echo "  Restarting app container..."
            _compose restart app ;;

        5)  echo "  Restarting worker container..."
            _compose restart worker ;;

        6)  echo "  Starting stack + Ollama (CPU)..."
            _compose_ollama 0 up -d --remove-orphans ;;

        7)  echo "  Starting stack + Ollama (GPU)..."
            _compose_ollama 1 up -d --remove-orphans ;;

        # ── REBUILD ────────────────────────────────────────────────────────
        8)  echo "  Rebuilding app + worker images..."
            _compose build app worker ;;

        9)  echo "  Rebuilding app image..."
            _compose build app ;;

        10) echo "  Rebuilding worker image..."
            _compose build worker ;;

        11) echo "  Pulling latest base images..."
            _compose pull ;;

        12) echo "  Rebuilding app image then restarting..."
            _compose build app
            _compose up -d --no-deps app ;;

        13) echo "  Rebuilding all images then restarting..."
            _compose build app worker
            _compose up -d --remove-orphans ;;

        # ── SERVICE CONTROL ────────────────────────────────────────────────
        14) echo "  Restarting postgres...";  _compose restart postgres ;;
        15) echo "  Restarting redis...";     _compose restart redis ;;
        16) echo "  Restarting minio...";     _compose restart minio ;;
        17) echo "  Restarting caddy...";     _compose restart caddy ;;
        18) echo "  Restarting opa...";       _compose restart opa ;;
        19) echo "  Restarting ollama...";    _compose_ollama 0 restart ollama ;;

        20) echo "  Pulling Ollama models (VLM + OCR)..."
            _compose_ollama 0 run --rm ollama-pull ;;

        # ── DATABASE ───────────────────────────────────────────────────────
        21) echo "  Running database migrations..."
            _compose exec \
                -e PGHOST=postgres \
                -e PGUSER=mediacat_migrator \
                -e PGDATABASE=mediacat \
                app python -m alembic -c alembic.ini upgrade head ;;

        22) echo "  Migration status..."
            _compose exec \
                -e PGHOST=postgres \
                -e PGUSER=mediacat_migrator \
                -e PGDATABASE=mediacat \
                app python -m alembic -c alembic.ini current ;;

        # ── MONITOR / LOGS ─────────────────────────────────────────────────
        23) _compose ps ;;

        24) tail=$(ask_tail); _compose logs "${FOLLOW[@]}" --tail="$tail" ;;
        25) tail=$(ask_tail); _compose logs "${FOLLOW[@]}" --tail="$tail" app ;;
        26) tail=$(ask_tail); _compose logs "${FOLLOW[@]}" --tail="$tail" worker ;;
        27) tail=$(ask_tail); _compose logs "${FOLLOW[@]}" --tail="$tail" postgres ;;
        28) tail=$(ask_tail); _compose logs "${FOLLOW[@]}" --tail="$tail" redis ;;
        29) tail=$(ask_tail); _compose logs "${FOLLOW[@]}" --tail="$tail" caddy ;;
        30) tail=$(ask_tail); _compose logs "${FOLLOW[@]}" --tail="$tail" minio ;;

        31) svc=$(ask_service)
            tail=$(ask_tail)
            _compose logs "${FOLLOW[@]}" --tail="$tail" "$svc" ;;

        # ── CLEANUP ────────────────────────────────────────────────────────

        # Safe: removes stopped containers and orphaned networks — no data lost.
        32) echo "  Pruning stopped containers and unused networks..."
            docker container prune -f
            docker network prune -f ;;

        # Safe: removes Docker image layers that are no longer referenced — no data lost.
        33) echo "  Pruning unused Docker images..."
            docker image prune -f ;;

        # Safe: removes Python __pycache__, .pyc files, and build artefacts — no data lost.
        34) ( cd "$PROJ_DIR" && make clean ) ;;

        # [!] CATALOGUE DATA WIPE — keeps users, schema, and infrastructure intact.
        # Truncates: tokens, revisions, images, price history, symbols, ocr, review items,
        # and import jobs.  Labels, manufacturers, countries, and user accounts are preserved.
        # Use this to reset test data without tearing down the stack.
        35) confirm_dangerous "Truncates all catalogue data (tokens, images, price history, import jobs). Users and schema are preserved." || \
                { echo "  Cancelled."; return 1; }
            echo "  Truncating catalogue tables..."
            _compose exec postgres psql -U mediacat_app -d mediacat -c "
                TRUNCATE tokens, token_revisions, media_objects,
                         token_price_history, token_symbols,
                         ocr_artifacts, review_items,
                         collection_import_jobs
                CASCADE;
            " && echo "  Done — catalogue data cleared." ;;

        # [!!] VOLUME WIPE — destroys the PostgreSQL database, MinIO objects, and Caddy TLS
        # certs.  The stack must be rebuilt and migrations re-run from scratch afterwards.
        # Caddy will issue a new TLS cert; re-import it into the Windows cert store if needed.
        36) confirm_dangerous "Removes ALL Docker named volumes: PostgreSQL DB, MinIO objects, Caddy TLS certs. Stack must be rebuilt afterwards." || \
                { echo "  Cancelled."; return 1; }
            _compose down --volumes --remove-orphans ;;

        # [!!!] NUCLEAR — removes every unused Docker resource system-wide: images, volumes,
        # networks, and build cache.  Affects ALL projects on this machine, not just MediaCat.
        # Only use this to reclaim disk space when nothing else is running.
        37) confirm_dangerous "Removes ALL unused Docker data system-wide (images, volumes, networks). Affects every Docker project on this machine." || \
                { echo "  Cancelled."; return 1; }
            docker system prune -af --volumes ;;

        # ── DOCS SERVER ────────────────────────────────────────────────
        38) _docs_start ;;

        39) _docs_stop ;;

        40) echo "  Rebuilding API reference (pdoc)..."
            ( cd "$PROJ_DIR" && make docs-api ) \
                && echo "  Done — API reference regenerated." \
                || echo "  Failed — ensure 'make setup' has been run." ;;

        41) if _docs_is_running; then
                echo "  Docs server: RUNNING (PID $(cat "$_DOCS_PID"))"
                echo "  Direct:    http://127.0.0.1:8800"
                echo "  Via Caddy: $(_public_url docs.)"
            else
                echo "  Docs server: STOPPED"
            fi ;;

        0|q|Q) (( BATCH )) && { echo "  Exit is a menu-only option."; return 2; }
               echo "  Bye."; exit 0 ;;

        "")  ;;    # bare Enter — redraw

        *)   printf "  Unknown option: %s\n" "$choice"; return 2 ;;

    esac
}

# ── Main loop ─────────────────────────────────────────────────────────────────
[[ -d "$PROJ_DIR" ]] || {
    printf "ERROR: project directory not found: %s\n" "$PROJ_DIR" >&2
    exit 1
}

if (( BATCH )); then
    [[ "${2:-}" =~ ^[0-9]+$ ]] || { echo "Usage: $0 --run N" >&2; exit 2; }
    do_action "$2" 2>&1
    exit $?
fi

while true; do
    draw_menu
    printf "\n  ${G}Enter selection:${D} "
    read -r choice || { echo; exit 0; }   # graceful Ctrl-D exit
    echo ""

    do_action "${choice}" 2>&1 || true    # errors shown, menu loop continues

    echo ""
    printf "  ${G}Press Enter to return to menu...${D}"
    read -r || exit 0
done
