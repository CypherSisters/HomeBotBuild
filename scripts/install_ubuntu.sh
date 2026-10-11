#!/usr/bin/env bash
#
# PuffPAI / HomeBot — one-shot installer for Ubuntu (x86_64 and Jetson arm64)
#
# It installs the SAME topology the reference build runs, so the units, paths
# and docs all mean one thing:
#
#     /opt/puffpai        the checkout + venv        (read-only at runtime)
#     /data/puffpai       every writable thing       (models, chromadb, vault)
#     /etc/puffpai        EnvironmentFile secrets    (root:puffpai 0640)
#     puffpai:puffpai     the service account
#     127.0.0.1:8000      the Flask backend (systemd/puffpai-api.service)
#
# The heavy lifting for packages / users / directories is NOT duplicated here:
# this script calls scripts/provision_orin.sh, which owns that and is
# idempotent. This script owns fetching the code, the models, the venv and the
# unit, and it ends by proving the API answers.
#
# Usage (download, read, THEN run — never pipe a URL into sudo bash):
#
#     curl -fsSL -o install_ubuntu.sh \
#       https://raw.githubusercontent.com/CypherSisters/HomeBotBuild/main/scripts/install_ubuntu.sh
#     less install_ubuntu.sh
#     sudo bash install_ubuntu.sh --dry-run     # print every command, run none
#     sudo bash install_ubuntu.sh               # asks before it touches anything
#
# The repository is PUBLIC, so no credential is needed. If you are installing
# from a fork or a private mirror, authenticate one of:
#     gh auth login                       (GitHub CLI)
#     export PUFFPAI_GH_TOKEN=ghp_...     (a PAT with 'repo' read scope)
#
# DO NOT run this on an already-provisioned box (it pulls models, which
# unloads pinned models from VRAM). It is for a fresh machine.
#
set -euo pipefail

# ── Tunables ────────────────────────────────────────────────────────────────
PUFFPAI_REPO_SLUG="${PUFFPAI_REPO_SLUG:-CypherSisters/HomeBotBuild}"
PUFFPAI_BRANCH="${PUFFPAI_BRANCH:-main}"
INSTALL_DIR="${PUFFPAI_INSTALL_DIR:-/opt/puffpai}"
DATA_ROOT="${PUFFPAI_DATA_ROOT:-/data/puffpai}"
SVC_USER="${PUFFPAI_USER:-puffpai}"
SVC_GROUP="${PUFFPAI_GROUP:-puffpai}"
API_PORT="${PUFFPAI_API_PORT:-8000}"

# The models the reference build actually runs, with their real download sizes
# (from the Ollama manifests). Change these and
# you change what the product does — chat quality, routing and vision all move.
#   gpt-oss:20b        13.8 GB   chat / general answering (config.llm.model)
#   qwen3:30b-a3b      18.6 GB   agent classify + plan (agents/engine.py)
#   qwen2.5vl:7b        6.0 GB   vision / OCR-assist (config.llm.vision_model)
#   nomic-embed-text    0.3 GB   embeddings (config.models.ollama_embedding_model)
MODELS=(
    "gpt-oss:20b|13.8"
    "qwen3:30b-a3b|18.6"
    "qwen2.5vl:7b|6.0"
    "nomic-embed-text|0.3"
)
MODELS_TOTAL_GB=39

# Floor: 39 GB of models + ~8 GB venv/deps + headroom for ChromaDB and logs.
MIN_FREE_GB=80
# qwen3:30b-a3b alone is 18.6 GB of weights; below 32 GB of RAM it will not
# co-reside with gpt-oss:20b and the box will thrash.
MIN_RAM_GB=32
HARD_MIN_RAM_GB=16

DRY_RUN=0
ASSUME_YES=0
SKIP_MODELS=0

# ── Small helpers ───────────────────────────────────────────────────────────
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; NC=$'\033[0m'

log()  { printf '\n%s== %s%s\n' "$BLUE" "$*" "$NC"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '%s   WARNING: %s%s\n' "$YELLOW" "$*" "$NC" >&2; }
die()  { printf '%s\nERROR: %s%s\n' "$RED" "$*" "$NC" >&2; exit 1; }

# fail <message> - a PREFLIGHT refusal.
#
# On a real run this IS `die`: a box that cannot host the install must be told
# so before a single package is touched. On --dry-run it REPORTS and continues.
# The flag's contract is "print every command that would run, execute none",
# and free disk / a distro check / GitHub credentials are properties of the
# real install, not of printing a plan. Otherwise an operator evaluating the
# installer on an already-provisioned appliance would get
#   ERROR: need at least 80 GB free for /opt/puffpai ... found 20 GB
# and exit 1, so the plan the flag exists to print would never be printed.
# Nothing is hidden: each failed check is reported where it happens, counted,
# and listed again at the end of preflight.
PREFLIGHT_FAILURES=0
PREFLIGHT_FAILED_CHECKS=""
fail() {
    if [ "$DRY_RUN" -eq 1 ]; then
        PREFLIGHT_FAILURES=$((PREFLIGHT_FAILURES + 1))
        PREFLIGHT_FAILED_CHECKS="${PREFLIGHT_FAILED_CHECKS}
     - $*"
        printf '%s   PREFLIGHT FAIL: %s%s\n' "$RED" "$*" "$NC" >&2
        return 0
    fi
    die "$@"
}

# run <cmd...> — echo it, then execute unless --dry-run.
run() {
    printf '   %s+ %s%s\n' "$GREEN" "$*" "$NC"
    if [ "$DRY_RUN" -eq 0 ]; then
        "$@"
    fi
}

# run_sh <string> — same, for the few places that genuinely need a shell.
run_sh() {
    printf '   %s+ %s%s\n' "$GREEN" "$1" "$NC"
    if [ "$DRY_RUN" -eq 0 ]; then
        bash -c "$1"
    fi
}

# run_secret <display> <string> — run a shell string whose text contains a
# credential. Only <display> is ever printed, so the token never reaches the
# console, the journal, or a piped install log.
run_secret() {
    printf '   %s+ %s%s\n' "$GREEN" "$1" "$NC"
    if [ "$DRY_RUN" -eq 0 ]; then
        bash -c "$2"
    fi
}

as_user() { run sudo -u "$SVC_USER" -H "$@"; }

usage() {
    cat <<'USAGE'
Usage: sudo bash install_ubuntu.sh [options]

  --yes            skip the interactive confirmation (for unattended runs)
  --dry-run        print every command that would run, execute none
  --skip-models    do everything except `ollama pull` (models already present)
  --branch NAME    branch to check out (default: main)
  -h, --help       this text

Environment:
  PUFFPAI_GH_TOKEN   GitHub PAT, only needed for a fork or private mirror
  PUFFPAI_INSTALL_DIR, PUFFPAI_DATA_ROOT, PUFFPAI_USER, PUFFPAI_API_PORT
USAGE
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y)      ASSUME_YES=1 ;;
            --dry-run|-n)  DRY_RUN=1 ;;
            --skip-models) SKIP_MODELS=1 ;;
            --branch)      shift; [ $# -gt 0 ] || die "--branch needs a value"
                           PUFFPAI_BRANCH="$1" ;;
            -h|--help)     usage; exit 0 ;;
            *)             usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done
}

# ── Preflight ───────────────────────────────────────────────────────────────
# Everything that can make the install fail halfway is checked HERE, before a
# single package is touched. A refusal now is cheap; a half-installed box is
# not.
GIT_AUTH_MODE=""

preflight() {
    log "Preflight"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "--dry-run: every check below REPORTS pass or fail and nothing"
        info "           exits; only a real run refuses to install."
    fi

    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            info "dry-run: would require root (not running as root - continuing)"
        else
            die "run as root:  sudo bash $0"
        fi
    fi

    # -- OS ------------------------------------------------------------------
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
    else
        fail "/etc/os-release missing - unsupported OS."
    fi
    : "${VERSION_ID:=0.0}" "${ID:=unknown}"
    case "${ID:-}${ID_LIKE:-}" in
        *ubuntu*|*debian*) : ;;
        *) fail "unsupported distribution '${ID:-?}'. Ubuntu 22.04 is the tested target." ;;
    esac
    local major minor
    major="${VERSION_ID%%.*}"; minor="${VERSION_ID#*.}"
    if [ "${major:-0}" -lt 20 ] 2>/dev/null; then
        fail "Ubuntu ${VERSION_ID} is too old; 20.04 is the floor, 22.04 is tested."
    fi
    info "OS: ${PRETTY_NAME:-$ID $VERSION_ID}"
    [ "${major}.${minor}" = "22.04" ] || \
        warn "only Ubuntu 22.04 is tested end-to-end; ${VERSION_ID} may need fixes."

    # -- Arch ----------------------------------------------------------------
    local arch; arch="$(uname -m 2>/dev/null || echo unknown)"
    case "$arch" in
        x86_64|aarch64) info "Arch: $arch" ;;
        *) fail "unsupported architecture '$arch' (need x86_64 or aarch64)." ;;
    esac

    # -- Jetson / power profile ----------------------------------------------
    if [ -r /etc/nv_tegra_release ]; then
        info "Jetson: $(head -n1 /etc/nv_tegra_release)"
        if command -v nvpmodel >/dev/null 2>&1; then
            info "nvpmodel: $(nvpmodel -q 2>/dev/null | tr '\n' ' ' | tr -s ' ')"
        else
            warn "nvpmodel not found - JetPack looks incomplete. Reflash before continuing."
        fi
    else
        info "Not a Jetson (no /etc/nv_tegra_release). Supported, but GPU"
        info "acceleration depends on your own CUDA/driver setup."
    fi

    # -- RAM -----------------------------------------------------------------
    # `free` is absent on some shells a reader may evaluate this from; an
    # unreadable value is reported as unknown rather than crashing the plan.
    local ram_gb; ram_gb="$(free -g 2>/dev/null | awk '/^Mem:/ {print $2}' || true)"
    case "$ram_gb" in ''|*[!0-9]*) ram_gb="" ;; esac
    if [ -z "$ram_gb" ]; then
        info "RAM: unknown (could not read 'free')"
        fail "could not read total RAM; ${MIN_RAM_GB} GB is the recommended floor."
    else
        info "RAM: ${ram_gb} GB"
        if [ "$ram_gb" -lt "$HARD_MIN_RAM_GB" ]; then
            fail "${ram_gb} GB RAM is below the ${HARD_MIN_RAM_GB} GB hard floor."
        elif [ "$ram_gb" -lt "$MIN_RAM_GB" ]; then
            warn "${ram_gb} GB RAM. qwen3:30b-a3b is 18.6 GB of weights; below"
            warn "${MIN_RAM_GB} GB it will not co-reside with gpt-oss:20b and every"
            warn "answer will be slow. The reference build has 64 GB."
        fi
    fi

    # -- Disk ----------------------------------------------------------------
    # Models land under $DATA_ROOT; the venv under $INSTALL_DIR. They can be
    # different filesystems, so check the nearest existing ancestor of each.
    local d free_gb
    for d in "$DATA_ROOT" "$INSTALL_DIR"; do
        local probe="$d"
        while [ ! -d "$probe" ] && [ "$probe" != "/" ]; do probe="$(dirname "$probe")"; done
        free_gb="$(df -BG --output=avail "$probe" 2>/dev/null | tail -1 | tr -dc '0-9' || true)"
        if [ -z "$free_gb" ]; then
            info "Free on $probe (for $d): unknown (could not read 'df')"
            fail "could not read free space on $probe; need ${MIN_FREE_GB} GB for $d."
            continue
        fi
        info "Free on $probe (for $d): ${free_gb} GB"
        if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
            fail "need at least ${MIN_FREE_GB} GB free for $d (${MODELS_TOTAL_GB} GB of models + venv + index); found ${free_gb} GB."
        fi
    done

    # -- Tools ---------------------------------------------------------------
    local missing=()
    for c in curl git; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
    if [ ${#missing[@]} -gt 0 ]; then
        info "will apt-install: ${missing[*]}"
    fi
    if command -v docker >/dev/null 2>&1; then
        info "docker: $(docker --version 2>/dev/null | head -1)"
    else
        info "docker: not installed (provision_orin.sh installs it - Node-RED,"
        info "        SearXNG and the media services need it)."
    fi

    # -- Repo access ---------------------------------------------------------
    # Decide the auth mode NOW, not 20 minutes in. The repo is public, so the
    # fallback is an anonymous clone, not a failure.
    if [ -d "${INSTALL_DIR}/.git" ]; then
        GIT_AUTH_MODE="existing"
        info "Repo: ${INSTALL_DIR} is already a checkout - will fetch + reset."
    elif [ -n "${PUFFPAI_GH_TOKEN:-}" ]; then
        GIT_AUTH_MODE="token"
        info "Repo auth: PUFFPAI_GH_TOKEN (the token is stripped from the remote"
        info "           after cloning; it is never written to .git/config.)"
    elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        GIT_AUTH_MODE="gh"
        info "Repo auth: GitHub CLI (gh auth status OK)."
    else
        # ${PUFFPAI_REPO_SLUG} is public, so an unauthenticated clone is the
        # normal path. A credential is only needed for a fork or a private
        # mirror, and the two branches above pick it up when one is present.
        GIT_AUTH_MODE="public"
        info "Repo auth: none needed - cloning ${PUFFPAI_REPO_SLUG} anonymously."
    fi

    # -- Verdict -------------------------------------------------------------
    # Only reachable on --dry-run; a real run has already exited on the first
    # failure. This is what makes the mode honest: the plan below is printed,
    # AND the reader is told exactly which checks this machine would fail.
    if [ "$DRY_RUN" -eq 1 ]; then
        if [ "$PREFLIGHT_FAILURES" -eq 0 ]; then
            info "Preflight: all checks passed."
        else
            warn "Preflight: ${PREFLIGHT_FAILURES} check(s) would STOP a real run:${PREFLIGHT_FAILED_CHECKS}"
            warn "The plan below is what a machine that passes them would run."
        fi
    fi
}

# ── The plan, printed before anything happens ───────────────────────────────
show_plan() {
    local m
    cat <<PLAN

${BLUE}PuffPAI / HomeBot installer${NC}
  branch          ${PUFFPAI_BRANCH}  (of ${PUFFPAI_REPO_SLUG})
  code            ${INSTALL_DIR}
  writable data   ${DATA_ROOT}
  service account ${SVC_USER}:${SVC_GROUP}
  backend         systemd puffpai-api.service on 127.0.0.1:${API_PORT}

This will, in this order:
  1. apt-get update + install git/curl if missing
  2. clone ${PUFFPAI_REPO_SLUG}@${PUFFPAI_BRANCH} into ${INSTALL_DIR}
  3. run scripts/provision_orin.sh  (OS packages, docker, the ${SVC_USER}
     account, ${DATA_ROOT} layout, /etc/puffpai env files — idempotent)
  4. install Ollama + the model-directory override, re-run provision_orin.sh
  5. pull ${#MODELS[@]} models, ~${MODELS_TOTAL_GB} GB total:
PLAN
    for m in "${MODELS[@]}"; do
        printf '        %-20s %5s GB\n' "${m%%|*}" "${m##*|}"
    done
    cat <<PLAN
     On a 100 Mbit link that is roughly 60-90 minutes. On 1 Gbit, 10-15.
  6. python3 -m venv + pip install -r requirements.txt
  7. seed config + /etc/puffpai/api.env (never overwrites existing secrets)
  8. install and enable systemd/puffpai-api.service
  9. smoke-check http://127.0.0.1:${API_PORT}/health and print next steps

Total time on a fresh box: 45-120 minutes, nearly all of it step 5.
Re-running is safe: every step is idempotent and no secret is overwritten.

PLAN
}

confirm() {
    if [ "$DRY_RUN" -eq 1 ]; then
        info "--dry-run: printing commands only, nothing will be executed."
        return 0
    fi
    if [ "$ASSUME_YES" -eq 1 ]; then
        info "--yes given, proceeding without prompting."
        return 0
    fi
    if [ ! -t 0 ]; then
        die "no terminal to confirm on (are you piping this into bash?).
       Download the script and run it, or pass --yes for an unattended run."
    fi
    local reply
    read -r -p "Proceed? [y/N] " reply
    case "$reply" in
        [Yy]*) return 0 ;;
        *) echo "Cancelled."; exit 1 ;;
    esac
}

# ── Steps ───────────────────────────────────────────────────────────────────
step_base_packages() {
    log "[1/9] Base packages"
    run env DEBIAN_FRONTEND=noninteractive apt-get update
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git curl ca-certificates
}

step_clone() {
    log "[2/9] Code -> ${INSTALL_DIR} (branch ${PUFFPAI_BRANCH})"
    run install -d -m 0755 "$(dirname "$INSTALL_DIR")"

    if [ "$GIT_AUTH_MODE" = "existing" ]; then
        run git -C "$INSTALL_DIR" fetch --prune origin
        run git -C "$INSTALL_DIR" checkout "$PUFFPAI_BRANCH"
        run git -C "$INSTALL_DIR" reset --hard "origin/${PUFFPAI_BRANCH}"
    else
        # Refuse to clone into a non-empty directory rather than half-merging.
        if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
            die "${INSTALL_DIR} exists, is not empty, and is not a git checkout.
       Move it aside (mv ${INSTALL_DIR} ${INSTALL_DIR}.bak-\$(date +%F)) and re-run."
        fi
        case "$GIT_AUTH_MODE" in
            gh)
                run gh repo clone "$PUFFPAI_REPO_SLUG" "$INSTALL_DIR" -- \
                    --branch "$PUFFPAI_BRANCH"
                ;;
            public)
                run git clone --branch "$PUFFPAI_BRANCH"                     "https://github.com/${PUFFPAI_REPO_SLUG}.git" "$INSTALL_DIR"
                ;;
            token)
                # The token is used once, for the clone, then removed from the
                # remote URL so it never persists in .git/config.
                run_secret \
                    "git clone --branch '${PUFFPAI_BRANCH}' 'https://x-access-token:<redacted>@github.com/${PUFFPAI_REPO_SLUG}.git' '${INSTALL_DIR}'" \
                    "git clone --branch '${PUFFPAI_BRANCH}' 'https://x-access-token:${PUFFPAI_GH_TOKEN}@github.com/${PUFFPAI_REPO_SLUG}.git' '${INSTALL_DIR}'"
                run git -C "$INSTALL_DIR" remote set-url origin \
                    "https://github.com/${PUFFPAI_REPO_SLUG}.git"
                ;;
        esac
    fi

    # Prove we got a real tree, not an empty dir or an error page.
    if [ "$DRY_RUN" -eq 0 ]; then
        [ -f "${INSTALL_DIR}/start_api.py" ] && [ -d "${INSTALL_DIR}/src/puffpai" ] || \
            die "${INSTALL_DIR} does not look like a PuffPAI checkout (start_api.py / src/puffpai missing)."
        info "HEAD: $(git -C "$INSTALL_DIR" rev-parse --short HEAD) on $(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)"
    fi
}

step_provision() {
    log "[3/9] provision_orin.sh (packages, ${SVC_USER} account, ${DATA_ROOT})"
    local p="${INSTALL_DIR}/scripts/provision_orin.sh"
    if [ "$DRY_RUN" -eq 0 ] && [ ! -x "$p" ] && [ ! -f "$p" ]; then
        die "missing ${p} — the checkout is incomplete."
    fi
    run bash "$p" --yes
}

step_ollama() {
    log "[4/9] Ollama"
    if command -v ollama >/dev/null 2>&1; then
        info "ollama present: $(ollama --version 2>/dev/null | head -1)"
    else
        run_sh "curl -fsSL https://ollama.com/install.sh -o /tmp/ollama-install.sh"
        if [ "$DRY_RUN" -eq 0 ]; then
            [ -s /tmp/ollama-install.sh ] || \
                die "the Ollama installer downloaded as an empty file — check your network/DNS and re-run."
        fi
        run sh /tmp/ollama-install.sh
        run rm -f /tmp/ollama-install.sh
    fi

    # Model directory + bind address override, so pulls land on $DATA_ROOT
    # (the NVMe) instead of /usr/share/ollama, and 11434 stays on loopback.
    local ovr="${INSTALL_DIR}/systemd/ollama.service.d/override.conf"
    if [ -f "$ovr" ]; then
        run install -D -m 0644 "$ovr" /etc/systemd/system/ollama.service.d/override.conf
        run systemctl daemon-reload
        run systemctl enable ollama
        run systemctl restart ollama
    else
        warn "no ollama override in the checkout — models will land in the default directory."
    fi

    # provision_orin.sh hands $DATA_ROOT/models/ollama to the 'ollama' user,
    # which only exists after the installer ran. Re-run so the pull can write.
    info "re-running provision_orin.sh so the 'ollama' user owns the model dir"
    run bash "${INSTALL_DIR}/scripts/provision_orin.sh" --yes
}

step_models() {
    log "[5/9] Models (~${MODELS_TOTAL_GB} GB)"
    if [ "$SKIP_MODELS" -eq 1 ]; then
        info "--skip-models: not pulling anything."
        return 0
    fi
    # Give ollama a moment after the restart above.
    run_sh "for i in \$(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 2; done"
    local entry name
    for entry in "${MODELS[@]}"; do
        name="${entry%%|*}"
        if [ "$DRY_RUN" -eq 0 ] && ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$name"; then
            info "$name already present, skipping"
            continue
        fi
        info "pulling ${name} (${entry##*|} GB)"
        run ollama pull "$name"
    done
}

step_venv() {
    log "[6/9] Python environment"
    as_user python3 -m venv "${INSTALL_DIR}/venv"
    as_user "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip wheel
    as_user "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
    info "OCR extras (image + scanned-PDF uploads) are optional:"
    info "  sudo -u ${SVC_USER} ${INSTALL_DIR}/venv/bin/pip install pytesseract pdf2image"
}

step_config() {
    log "[7/9] Configuration"
    local cfg="${INSTALL_DIR}/config/home_assistant_config.json"
    if [ ! -e "$cfg" ] && [ -e "${cfg}.example" ]; then
        run cp "${cfg}.example" "$cfg"
        run chown "${SVC_USER}:${SVC_GROUP}" "$cfg"
        info "seeded home_assistant_config.json from the example"
    else
        info "home_assistant_config.json left as-is"
    fi
    info "Paths: config/puffpai_config.json ships RELATIVE paths. Every paths.*"
    info "entry and chromadb.path must point under ${DATA_ROOT} or the backend"
    info "will fail on the read-only /opt. See docs/INSTALLATION.md."
    info "Secrets: /etc/puffpai/api.env was created empty by provision_orin.sh"
    info "and is NEVER overwritten. Fill it in before first real use."
}

step_service() {
    log "[8/9] systemd unit"
    local unit="${INSTALL_DIR}/systemd/puffpai-api.service"
    if [ "$DRY_RUN" -eq 0 ] && [ ! -f "$unit" ]; then
        die "missing ${unit}"
    fi
    run install -m 0644 "$unit" /etc/systemd/system/puffpai-api.service
    run systemctl daemon-reload
    run systemctl enable puffpai-api
    run systemctl restart puffpai-api
    info "Dashboard (:3003) and Node-RED are separate, optional units —"
    info "see ${INSTALL_DIR}/systemd/README.md."
}

step_smoke() {
    log "[9/9] Smoke check"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "+ curl http://127.0.0.1:${API_PORT}/health  (skipped in --dry-run)"
        return 0
    fi
    local i code=""
    for i in $(seq 1 45); do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
                 "http://127.0.0.1:${API_PORT}/health" || true)"
        case "$code" in
            200)      info "GET /health -> 200. The backend is up."; return 0 ;;
            401|403)  info "GET /health -> ${code}: the backend is up and gated"
                      info "(the API key policy covers /health on this build)."
                      return 0 ;;
        esac
        sleep 2
    done
    warn "the API never answered on 127.0.0.1:${API_PORT} (last status: '${code:-none}')."
    warn "Diagnose with:"
    warn "  systemctl status puffpai-api"
    warn "  journalctl -u puffpai-api -n 100 --no-pager"
    return 1
}

print_next_steps() {
    cat <<NEXT

${GREEN}PuffPAI backend installed.${NC}

Next, in order:
  1. Secrets:   sudoedit /etc/puffpai/api.env      (API key, HA token, ...)
                sudo systemctl restart puffpai-api
  2. Paths:     point paths.* and chromadb.path in
                ${INSTALL_DIR}/config/puffpai_config.json at ${DATA_ROOT}
  3. Dashboard: optional Next.js UI on :3003 — ${INSTALL_DIR}/systemd/README.md
  4. Knowledge: ZIM packs and the index live under ${DATA_ROOT}/knowledge;
                build the index with  scripts/reindex.py

Logs:      journalctl -u puffpai-api -f
Health:    curl http://127.0.0.1:${API_PORT}/health
Re-run:    sudo bash ${INSTALL_DIR}/scripts/install_ubuntu.sh   (idempotent)

Docs:      ${INSTALL_DIR}/docs/INSTALLATION.md

NEXT
}

# ── main ────────────────────────────────────────────────────────────────────
# Everything lives in main(), invoked on the LAST line. A truncated download
# (a 404 page, a dropped connection) therefore executes nothing at all instead
# of running half an installer and exiting 0.
main() {
    parse_args "$@"
    printf '%s\n' "PuffPAI / HomeBot installer — branch ${PUFFPAI_BRANCH}"
    printf '%s\n' "This is the API-ONLY path (no dashboard, containers, voice or kiosk)."
    printf '%s\n' "On a Jetson Orin, use scripts/install_orin.sh instead — it installs all of them."
    preflight
    show_plan
    confirm
    step_base_packages
    step_clone
    step_provision
    step_ollama
    step_models
    step_venv
    step_config
    step_service
    step_smoke
    print_next_steps
}

main "$@"
