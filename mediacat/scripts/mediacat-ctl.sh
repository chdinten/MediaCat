#!/usr/bin/env bash
# =============================================================================
#  mediacat-ctl  —  MediaCat Docker stack management
#
#  Location : ~/mediacat-ctl.sh   (WSL2: /mnt/c/Users/chdin/mediacat-ctl.sh)
#  Usage    : bash ~/mediacat-ctl.sh
#             chmod +x ~/mediacat-ctl.sh  &&  ~/mediacat-ctl.sh
# =============================================================================
set -uo pipefail

PROJ_DIR="/mnt/c/MEGA/SoundDB/mediacat"
DEFAULT_TAIL=20

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
ask_tail() {
    local n
    printf "  Lines to tail [%d]: " "$DEFAULT_TAIL"
    read -r n
    n="${n:-$DEFAULT_TAIL}"
    [[ "$n" =~ ^[0-9]+$ ]] || n=$DEFAULT_TAIL
    printf "%s" "$n"
}

ask_service() {
    printf "  Service (app|worker|caddy|postgres|redis|minio|opa|ollama) [app]: "
    local s; read -r s
    printf "%s" "${s:-app}"
}

confirm_dangerous() {
    printf "  ${R}WARNING:${D} %s\n  Continue? [y/N]: " "$1"
    local yn; read -r yn
    [[ "$yn" =~ ^[Yy]$ ]]
}

# ── Draw menu ─────────────────────────────────────────────────────────────────
draw_menu() {
    clear
    top_border
    full_row "  MediaCat Stack Manager"
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

    section "CLEANUP"
    row2 32 "Prune: containers + networks" 33 "Prune: unused images"
    row2 34 "Make clean (build artefacts)" 35 "Down + remove volumes [!!]"
    row2 36 "Full docker system prune [!!]" "" ""

    col_close
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
            _compose up -d --remove-orphans ;;

        2)  echo "  Stopping MediaCat stack..."
            _compose down --remove-orphans ;;

        3)  echo "  Restarting stack (down then up)..."
            _compose down --remove-orphans
            _compose up -d --remove-orphans ;;

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

        24) tail=$(ask_tail); _compose logs -f --tail="$tail" ;;
        25) tail=$(ask_tail); _compose logs -f --tail="$tail" app ;;
        26) tail=$(ask_tail); _compose logs -f --tail="$tail" worker ;;
        27) tail=$(ask_tail); _compose logs -f --tail="$tail" postgres ;;
        28) tail=$(ask_tail); _compose logs -f --tail="$tail" redis ;;
        29) tail=$(ask_tail); _compose logs -f --tail="$tail" caddy ;;
        30) tail=$(ask_tail); _compose logs -f --tail="$tail" minio ;;

        31) svc=$(ask_service)
            tail=$(ask_tail)
            _compose logs -f --tail="$tail" "$svc" ;;

        # ── CLEANUP ────────────────────────────────────────────────────────
        32) echo "  Pruning stopped containers and unused networks..."
            docker container prune -f
            docker network prune -f ;;

        33) echo "  Pruning unused Docker images..."
            docker image prune -f ;;

        34) ( cd "$PROJ_DIR" && make clean ) ;;

        35) confirm_dangerous "Removes Docker named volumes (Caddy TLS certs, etc.)." || \
                { echo "  Cancelled."; return; }
            _compose down --volumes --remove-orphans ;;

        36) confirm_dangerous "Removes ALL unused Docker data system-wide." || \
                { echo "  Cancelled."; return; }
            docker system prune -af --volumes ;;

        0|q|Q) echo "  Bye."; exit 0 ;;

        "")  ;;    # bare Enter — redraw

        *)   printf "  Unknown option: %s\n" "$choice" ;;

    esac
}

# ── Main loop ─────────────────────────────────────────────────────────────────
[[ -d "$PROJ_DIR" ]] || {
    printf "ERROR: project directory not found: %s\n" "$PROJ_DIR" >&2
    exit 1
}

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
