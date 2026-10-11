#!/usr/bin/env bash
#
# PuffPAI HomeBot — Founders Edition: one-command install on a Jetson AGX Orin
#
# This is the whole appliance, from a checkout you already have on the box:
# code, venv, dashboard build, env files, Ollama + the models the code pins,
# the systemd units, Node-RED, SearXNG, and optionally Zone 3 and the kiosk.
#
# ── Division of labour (read this before changing anything) ─────────────────
#
#   scripts/provision_orin.sh   OWNS the machine: OS packages, Docker/compose,
#                               the puffpai service account, /opt/puffpai and
#                               /opt/puffpai-dashboard, the /data/puffpai
#                               layout, /etc/puffpai. It is idempotent and it
#                               is CALLED FROM HERE three times, in the order
#                               its own header demands (docs/INSTALL_ORIN.md
#                               §3): once up front, once after Ollama exists
#                               (so the 'ollama' user can own the model dir),
#                               once after the code is deployed (so it can
#                               create the repo-relative writable paths).
#                               None of that is duplicated below.
#
#   scripts/install_ubuntu.sh   the API-ONLY path, and it CLONES from GitHub.
#                               Use it for a dev box or an x86_64 machine.
#                               This script installs from a local checkout and
#                               goes all the way to a working dashboard.
#
#   scripts/provision_kiosk.sh  OWNS the wall-display session. Called with
#                               --with-kiosk; nothing here reimplements it.
#
#   this script                 the delta: code deploy, venv, dashboard build,
#                               env-file seeding, model pulls, unit install,
#                               container bring-up, and a verify pass.
#
# ── Verification status ─────────────────────────────────────────────────────
#
# This has not been run end to end on a fresh Orin. Steps that docs/INSTALL_ORIN.md
# marks UNVERIFIED ON A FRESH BOX print
#     NOTE: this step has not been verified on a fresh box
# when they run. Read --dry-run output first; it prints every command.
#
# Usage:
#     sudo bash scripts/install_orin.sh --dry-run     # print the plan, run none
#     sudo bash scripts/install_orin.sh               # the default install
#     sudo bash scripts/install_orin.sh --with-zone3 --with-kiosk --yes
#
set -euo pipefail

# ── Tunables ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${PUFFPAI_REPO_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

INSTALL_DIR="${PUFFPAI_INSTALL_DIR:-/opt/puffpai}"
DASH_DIR="${PUFFPAI_DASHBOARD_DIR:-/opt/puffpai-dashboard}"
DATA_ROOT="${PUFFPAI_DATA_ROOT:-/data/puffpai}"
# systemd/puffpai-api.service:51 — PUFFPAI_DATA_DIR. provision_orin.sh does NOT
# create this one (it stops at $DATA_ROOT's children), so phase 2 does.
STATE_DIR="${PUFFPAI_STATE_DIR:-${DATA_ROOT}/state}"
ETC_DIR=/etc/puffpai
SVC_USER="${PUFFPAI_USER:-puffpai}"
SVC_GROUP="${PUFFPAI_GROUP:-puffpai}"
API_PORT="${PUFFPAI_API_PORT:-8000}"        # systemd/puffpai-api.service
DASH_PORT="${PUFFPAI_DASHBOARD_PORT:-3003}" # dashboard/package.json "start"

# Floors, same numbers provision_orin.sh:37 and install_ubuntu.sh:62 use.
MIN_FREE_GB="${PUFFPAI_MIN_FREE_GB:-80}"
MIN_FREE_OPT_GB=20          # venv + code only; the models land on $DATA_ROOT

OLLAMA_INSTALL_URL="https://ollama.com/install.sh"

# Download sizes for display only, the same figures as
# scripts/install_ubuntu.sh:53-58. The TAGS themselves are
# not listed here on purpose — they are read out of the code in phase 6, so
# this script cannot drift from what the product actually runs.
model_size_gb() {
    case "$1" in
        gpt-oss:20b)      echo "13.8" ;;
        qwen3:30b-a3b)    echo "18.6" ;;
        qwen2.5vl:7b)     echo "6.0"  ;;
        nomic-embed-text) echo "0.3"  ;;
        *)                echo "?"    ;;
    esac
}

DRY_RUN=0
ASSUME_YES=0
SKIP_MODELS=0
SKIP_VOICE=0
WITH_ZONE3=0
WITH_KIOSK=0

# TEST-ONLY. Set HOMEBOT_INSTALL_SKIP_PREFLIGHT=1 to exercise the dry-run
# printer on a machine that is not a Jetson (a workstation, CI). It disables
# every hardware refusal, so it must NEVER be set on a box you are installing.
SKIP_PREFLIGHT="${HOMEBOT_INSTALL_SKIP_PREFLIGHT:-0}"

# ── Helpers ─────────────────────────────────────────────────────────────────
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; NC=$'\033[0m'

phase() { printf '\n%s== phase: %s ==%s\n' "$BLUE" "$*" "$NC"; }
info()  { printf '   %s\n' "$*"; }
warn()  { printf '%s   WARNING: %s%s\n' "$YELLOW" "$*" "$NC" >&2; }
die()   { printf '%s\nERROR: %s%s\n' "$RED" "$*" "$NC" >&2; exit 1; }

# Printed by every step docs/INSTALL_ORIN.md marks UNVERIFIED ON A FRESH BOX.
unverified() { printf '%s   NOTE: this step has not been verified on a fresh box%s\n' "$YELLOW" "$NC"; }

PREFLIGHT_FAILURES=0
PREFLIGHT_LIST=""
# Soft preflight failure: fatal on a real run, reported-and-continued under
# --dry-run so the plan the flag exists to print actually gets printed. Same
# contract as install_ubuntu.sh:95.
fail() {
    if [ "$DRY_RUN" -eq 1 ]; then
        PREFLIGHT_FAILURES=$((PREFLIGHT_FAILURES + 1))
        PREFLIGHT_LIST="${PREFLIGHT_LIST}
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

as_svc() { run sudo -u "$SVC_USER" -H "$@"; }

# ── Secrets ─────────────────────────────────────────────────────────────────
# ONE API key for the whole appliance, minted here and written to every env
# file that needs it. The value is NEVER echoed, never passed on a command
# line (visible in /proc/*/cmdline) and never written to the install log; the
# installer prints only WHERE it lives.

VERIFY_FAILURES=0
VERIFY_LIST=""
verify_fail() {
    VERIFY_FAILURES=$((VERIFY_FAILURES + 1))
    VERIFY_LIST="${VERIFY_LIST}
     - $*"
    printf '%s   VERIFY FAIL: %s%s\n' "$RED" "$*" "$NC" >&2
}

# is_placeholder <value> — true (0) when the value is a must-fill marker
# rather than a credential. Mirrors puffpai.api_server.validate_api_key(), and
# tests/test_release_api_key.py pins the two lists to the same meaning.
is_placeholder() {
    local v="${1:-}"
    [ -n "$v" ] || return 0
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
        change_me|changeme|change-me|chgme) return 0 ;;
        your_api_key_here|your-api-key-here|your_key_here) return 0 ;;
        replace_me|replaceme|placeholder|todo|tbd|xxx|example|sample) return 0 ;;
        default|secret|password|apikey|api_key|api-key|key) return 0 ;;
        none|null|nil|undefined|unset|n/a) return 0 ;;
    esac
    [ "${#v}" -lt 16 ] && return 0
    return 1
}

# env_value <file> <KEY> — the last KEY= value in an env file, or empty.
env_value() {
    [ -r "$1" ] || return 0
    sed -n "s/^[[:space:]]*$2=//p" "$1" | tail -1 | tr -d '\r'
}

mint_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        # /dev/urandom fallback: same 32 bytes, same hex, no openssl.
        od -An -vtx1 -N32 /dev/urandom | tr -d ' \n'
    fi
}

# Resolve the shared API key: reuse a real one already on the box (so re-runs
# and a half-filled set of env files converge on ONE value), otherwise mint.
API_KEY_VALUE=""
API_KEY_ORIGIN=""
resolve_shared_api_key() {
    local f v
    for f in "${ETC_DIR}/api.env" "${ETC_DIR}/dashboard.env" "${INSTALL_DIR}/.env"; do
        v="$(env_value "$f" PUFFPAI_API_KEY)"
        if ! is_placeholder "$v"; then
            API_KEY_VALUE="$v"; API_KEY_ORIGIN="reused from ${f}"; return 0
        fi
    done
    API_KEY_VALUE="$(mint_secret)"
    API_KEY_ORIGIN="newly generated (openssl rand -hex 32)"
}

# Put $API_KEY_VALUE into <file>, replacing a placeholder or appending if the
# key is absent. Never touches a file that already holds a real key. The value
# reaches awk through the environment, not argv, so it never appears in `ps`.
set_api_key_in_file() {
    local f="$1" cur tmp
    cur="$(env_value "$f" PUFFPAI_API_KEY)"
    if ! is_placeholder "$cur"; then
        info "  ${f} — PUFFPAI_API_KEY already set (left untouched)"
        return 0
    fi
    printf '   %s+ set PUFFPAI_API_KEY in %s (value not printed)%s\n' "$GREEN" "$f" "$NC"
    [ "$DRY_RUN" -eq 1 ] && return 0
    if grep -qE '^[[:space:]]*PUFFPAI_API_KEY=' "$f" 2>/dev/null; then
        tmp="$(mktemp)"
        chmod 0600 "$tmp"
        PUFFPAI_NEW_KEY="$API_KEY_VALUE" awk \
            '/^[[:space:]]*PUFFPAI_API_KEY=/ { print "PUFFPAI_API_KEY=" ENVIRON["PUFFPAI_NEW_KEY"]; next } { print }' \
            "$f" > "$tmp"
        cat "$tmp" > "$f"          # preserve the file's mode and owner
        rm -f "$tmp"
    else
        printf 'PUFFPAI_API_KEY=%s\n' "$API_KEY_VALUE" >> "$f"
    fi
}

# The primary LAN IPv4 of this box. `ip route get` consults the routing table
# only — it sends no packet — so this works with the WAN unplugged.
detect_lan_ip() {
    local ip=""
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null \
          | sed -n 's/.*[[:space:]]src[[:space:]]\([0-9.]*\).*/\1/p' | head -1)"
    [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '%s' "$ip"
}

# write_file <path> <mode> <owner:group> — heredoc on stdin. Never clobbers a
# non-empty file: every target here is a secrets file.
write_file() {
    local path="$1" mode="$2" owner="$3" body
    body="$(cat)"
    if [ -s "$path" ]; then
        info "${path} exists and is non-empty — left untouched"
        return 0
    fi
    printf '   %s+ write %s (%s %s)%s\n' "$GREEN" "$path" "$mode" "$owner" "$NC"
    if [ "$DRY_RUN" -eq 0 ]; then
        install -D -m "$mode" /dev/null "$path"
        printf '%s\n' "$body" > "$path"
        chown "$owner" "$path"
    fi
}

usage() {
    cat <<'USAGE'
Usage: sudo bash scripts/install_orin.sh [options]

  --dry-run, -n      print every command that would run, execute none
  --yes, -y          skip confirmations (unattended)
  --with-zone3       also install puffpai-zone3.service (AdGuard DNS; inert
                     until you point the router at this box)
  --with-kiosk       also run scripts/provision_kiosk.sh (wall display)
  --skip-models      do everything except `ollama pull`
  --skip-voice       do not install the user-level voice unit
  --repo-dir PATH    checkout to install from (default: this script's repo)
  -h, --help         this text

Phases: preflight, system, code, dashboard, env, ollama, services, verify.
Every phase is idempotent; re-running is the intended way to pick up changes.
No secret file is ever overwritten.
USAGE
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run|-n)  DRY_RUN=1 ;;
            --yes|-y)      ASSUME_YES=1 ;;
            --with-zone3)  WITH_ZONE3=1 ;;
            --with-kiosk)  WITH_KIOSK=1 ;;
            --skip-models) SKIP_MODELS=1 ;;
            --skip-voice)  SKIP_VOICE=1 ;;
            --repo-dir)    shift; [ $# -gt 0 ] || die "--repo-dir needs a value"
                           REPO_DIR="$1" ;;
            -h|--help)     usage; exit 0 ;;
            *)             usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done
    REPO_DIR="$(cd "$REPO_DIR" 2>/dev/null && pwd)" || die "--repo-dir: no such directory"
}

# confirm <prompt> — --yes and --dry-run both answer yes.
confirm() {
    [ "$DRY_RUN" -eq 1 ] && return 0
    [ "$ASSUME_YES" -eq 1 ] && { info "--yes: proceeding"; return 0; }
    [ -t 0 ] || die "no terminal to confirm on. Pass --yes for an unattended run."
    local reply
    read -r -p "   $1 [y/N] " reply
    case "$reply" in [Yy]*) return 0 ;; *) die "Cancelled." ;; esac
}

free_gb() {  # free_gb <path> — GB free on the nearest existing ancestor
    local probe="$1"
    while [ ! -d "$probe" ] && [ "$probe" != "/" ]; do probe="$(dirname "$probe")"; done
    df -BG --output=avail "$probe" 2>/dev/null | tail -1 | tr -dc '0-9'
}

# ── 1. Preflight ────────────────────────────────────────────────────────────
phase_preflight() {
    phase "preflight"

    if [ "$SKIP_PREFLIGHT" = "1" ]; then
        warn "HOMEBOT_INSTALL_SKIP_PREFLIGHT=1 — every hardware check is skipped."
        warn "This override exists to exercise --dry-run off-box. Never set it"
        warn "on a machine you are actually installing onto."
        return 0
    fi

    # Hard refusals. Founders Edition targets ONE machine; a half-install on
    # something else is worse than a clean "no".
    local arch; arch="$(uname -m 2>/dev/null || echo unknown)"
    if [ "$arch" != "aarch64" ]; then
        die "this installer is for a Jetson AGX Orin only.
       Expected: uname -m = aarch64          Found: ${arch}
       On x86_64 or a dev box use scripts/install_ubuntu.sh (API only)."
    fi
    if [ ! -r /etc/nv_tegra_release ]; then
        die "this installer is for a Jetson AGX Orin only.
       Expected: /etc/nv_tegra_release to exist (it is written by JetPack).
       Found:    nothing readable at that path.
       If JetPack is installed and this file is missing, the flash is
       incomplete — reflash before installing (docs/INSTALL_ORIN.md §1)."
    fi
    info "L4T:  $(head -n1 /etc/nv_tegra_release)"   # target: r36.5 / JetPack 6.2.2
    info "Arch: ${arch}"

    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            info "dry-run: would require root (not root — continuing)"
        else
            die "run as root:  sudo bash $0"
        fi
    fi

    # Power mode: reported, never changed. src/puffpai/inference/model_options.py
    # records the planner context was measured on a box pinned at MODE_50W.
    if command -v nvpmodel >/dev/null 2>&1; then
        info "nvpmodel: $(nvpmodel -q 2>/dev/null | tr '\n' ' ' | tr -s ' ')"
        info "          (not changed by this script — see docs/INSTALL_ORIN.md §2)"
    else
        warn "nvpmodel not found — JetPack looks incomplete."
    fi

    # Disk. Models (~39 GB) land on $DATA_ROOT; code + venv on $INSTALL_DIR.
    local f
    f="$(free_gb "$DATA_ROOT")"
    info "free for ${DATA_ROOT}: ${f:-unknown} GB (need ${MIN_FREE_GB})"
    [ "${f:-0}" -ge "$MIN_FREE_GB" ] 2>/dev/null || \
        fail "need ${MIN_FREE_GB} GB free for ${DATA_ROOT} (~39 GB of models + index); found ${f:-unknown}. Mount the NVMe first."
    f="$(free_gb "$INSTALL_DIR")"
    info "free for ${INSTALL_DIR}: ${f:-unknown} GB (need ${MIN_FREE_OPT_GB})"
    [ "${f:-0}" -ge "$MIN_FREE_OPT_GB" ] 2>/dev/null || \
        fail "need ${MIN_FREE_OPT_GB} GB free for ${INSTALL_DIR} (code + venv); found ${f:-unknown}."

    # Docker + compose v2. provision_orin.sh installs them, so this is a
    # report, not a refusal — except that the Node-RED unit hard-requires it.
    if command -v docker >/dev/null 2>&1; then
        info "docker: $(docker --version 2>/dev/null | head -1)"
        if docker compose version >/dev/null 2>&1; then
            info "compose: $(docker compose version --short 2>/dev/null)"
        else
            info "compose v2 missing — provision_orin.sh adds the plugin (phase: system)"
        fi
    else
        info "docker not installed — provision_orin.sh installs it (phase: system)"
    fi

    # Node. provision_orin.sh:195-207 deliberately does NOT install it.
    local engines; engines="$(sed -n 's/.*"node"[[:space:]]*:[[:space:]]*"\(.*\)".*/\1/p' \
        "${REPO_DIR}/dashboard/package.json" | head -1)"
    info "dashboard/package.json engines.node: ${engines:-unreadable}"
    if command -v node >/dev/null 2>&1; then
        local nv maj min
        nv="$(node --version | tr -d 'v')"; maj="${nv%%.*}"; min="${nv#*.}"; min="${min%%.*}"
        info "node: v${nv}"
        if   [ "$maj" -eq 20 ] && [ "$min" -ge 19 ]; then :
        elif [ "$maj" -eq 22 ] && [ "$min" -ge 12 ]; then :
        elif [ "$maj" -gt 22 ]; then :
        else
            fail "node v${nv} does not satisfy ${engines}. Install it first:
       curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
       sudo apt-get install -y nodejs"
        fi
    else
        fail "node is not installed and the dashboard build needs ${engines}.
       provision_orin.sh will not add a third-party apt repo for you:
       curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
       sudo apt-get install -y nodejs"
    fi

    # Python. setup.py pins python_requires=">=3.10".
    local floor; floor="$(sed -n 's/.*python_requires=">=\([0-9.]*\)".*/\1/p' \
        "${REPO_DIR}/setup.py" | head -1)"
    floor="${floor:-3.10}"
    if command -v python3 >/dev/null 2>&1; then
        local pv; pv="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
        info "python3: ${pv} (setup.py floor ${floor})"
        if [ "${pv%%.*}" -lt "${floor%%.*}" ] || \
           { [ "${pv%%.*}" -eq "${floor%%.*}" ] && [ "${pv#*.}" -lt "${floor#*.}" ]; }; then
            fail "python3 ${pv} is below setup.py's floor of ${floor}."
        fi
    else
        fail "python3 not found; setup.py needs >= ${floor}."
    fi

    # The checkout itself.
    [ -f "${REPO_DIR}/start_api.py" ] && [ -d "${REPO_DIR}/src/puffpai" ] || \
        fail "${REPO_DIR} does not look like a HomeBot checkout (start_api.py / src/puffpai missing)."

    if [ "$DRY_RUN" -eq 1 ]; then
        if [ "$PREFLIGHT_FAILURES" -eq 0 ]; then
            info "preflight: all checks passed."
        else
            warn "preflight: ${PREFLIGHT_FAILURES} check(s) would STOP a real run:${PREFLIGHT_LIST}"
            warn "The plan below is what a machine that passes them would run."
        fi
    fi
}

# ── 2. System: users, packages, directories ─────────────────────────────────
# Delegated wholesale to provision_orin.sh — see the header. The only thing
# added here is $STATE_DIR, which the API unit names and the provisioner does
# not create.
phase_system() {
    phase "system (provision_orin.sh pass 1 of 3)"
    local p="${REPO_DIR}/scripts/provision_orin.sh"
    [ -f "$p" ] || die "missing ${p} — the checkout is incomplete."
    # Printed, not executed, under --dry-run: the contract is "touch nothing",
    # and the provisioner has its own --dry-run for reading its plan.
    run bash "$p" --yes
    [ "$DRY_RUN" -eq 1 ] && info "(read its own plan with: sudo bash ${p} --dry-run)"
    info "PUFFPAI_DATA_DIR (systemd/puffpai-api.service:51) — not created above:"
    run install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0750 "$STATE_DIR"
}

# ── 3. Code + venv ──────────────────────────────────────────────────────────
phase_code() {
    phase "code"
    # No --delete: venv/, the seeded JSON state files and the runtime-writable
    # directories under /opt/puffpai must survive a redeploy.
    run rsync -a \
        --exclude '.git' --exclude 'dashboard/' --exclude 'tests/' \
        --exclude 'venv/' --exclude 'node_modules/' \
        --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' \
        "${REPO_DIR}/" "${INSTALL_DIR}/"
    run chown -R "${SVC_USER}:${SVC_GROUP}" "$INSTALL_DIR"

    as_svc python3 -m venv "${INSTALL_DIR}/venv"
    as_svc "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip wheel
    as_svc "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
    info "Optional extras (setup.py extras_require), not installed by default:"
    info "  [embeddings-legacy]  pulls PyTorch — on Jetson install torch from"
    info "                       NVIDIA's wheel index FIRST (INSTALL_ORIN.md §5)"
    info "  [ocr]                image + scanned-PDF uploads"

    phase "system (provision_orin.sh pass 2 of 3 — repo-relative writable paths)"
    # With the code in place it creates src/security_logs/, security_logs/,
    # data/, data/library_sources/, app_data/ and seeds the untracked JSON
    # state files (provision_orin.sh:313-354). The security audit trail FAILS
    # OPEN if src/security_logs/ is not writable, so this pass is not optional.
    run bash "${INSTALL_DIR}/scripts/provision_orin.sh" --yes
}

# ── 4. Dashboard ────────────────────────────────────────────────────────────
phase_dashboard() {
    phase "dashboard"
    # UNVERIFIED ON A FRESH BOX (docs/INSTALL_ORIN.md §7): provision_orin.sh
    # creates /opt/puffpai-dashboard empty and only this phase populates it.
    unverified
    # --delete IS right here: /opt/puffpai-dashboard is a build output
    # directory, not state. .next/ and node_modules/ are excluded so an
    # incremental redeploy does not throw the whole build away.
    run rsync -a --delete --exclude node_modules --exclude .next \
        "${REPO_DIR}/dashboard/" "${DASH_DIR}/"

    # Symlink workaround: `data` is a plain directory in this tree. If a
    # deployment ever ships it as a symlink, rsync -a copies the link itself,
    # and a link pointing into the read-only /opt (or nowhere) fails the build
    # in a way that is hard to read. Detect it and say so rather than guess.
    if [ -L "${REPO_DIR}/dashboard/data" ]; then
        info "dashboard/data is a symlink -> $(readlink "${REPO_DIR}/dashboard/data")"
        if [ -e "${REPO_DIR}/dashboard/data" ]; then
            run rsync -a --copy-links "${REPO_DIR}/dashboard/data/" "${DASH_DIR}/data/"
            info "copied its contents instead of the link (INSTALL_ORIN.md §7)"
        else
            warn "dashboard/data is a DANGLING symlink; the build will fail. Fix it first."
        fi
    fi

    run chown -R "${SVC_USER}:${SVC_GROUP}" "$DASH_DIR"
    if [ ! -e "${DASH_DIR}/.env.production" ] && [ -e "${REPO_DIR}/dashboard/.env.production.example" ]; then
        as_svc cp "${DASH_DIR}/.env.production.example" "${DASH_DIR}/.env.production"
        info "seeded .env.production (NEXT_PUBLIC_* only — inlined into the bundle, never secrets)"
    else
        info ".env.production left as-is"
    fi

    # HOME must be writable or npm dies creating $HOME/.npm; the unit sets the
    # same two variables (systemd/puffpai-dashboard.service:31-32).
    local npmenv="sudo -u ${SVC_USER} env HOME=${DATA_ROOT}/home npm_config_cache=${DATA_ROOT}/home/.npm"
    run_sh "cd '${DASH_DIR}' && ${npmenv} npm ci"
    # `next build` reports usage to telemetry.nextjs.org unless told not to.
    run_sh "cd '${DASH_DIR}' && ${npmenv} NEXT_TELEMETRY_DISABLED=1 npm run build"
    info "The build is a DEPLOY step, not a boot step: the unit's prestart hook"
    info "(dashboard/scripts/ensure-fresh-build.js) refuses to serve a .next"
    info "older than the source. Rebuild after every code change."
}

# ── 5. Environment files ────────────────────────────────────────────────────
# Every EnvironmentFile the units name, plus the compose .env. Created ONLY if
# absent or empty — provision_orin.sh:408-415 creates api.env and dashboard.env
# empty, and an empty file carries no secret to lose.
phase_env() {
    phase "env files"
    run install -d -o root -g "$SVC_GROUP" -m 0750 "$ETC_DIR"

    # ONE API key for the whole box, resolved before anything is written, so
    # every file below carries the SAME value. The key is never printed.
    resolve_shared_api_key
    info "PUFFPAI_API_KEY: ${API_KEY_ORIGIN}. Written to the three files listed"
    info "at the end of this phase; its value is printed nowhere."

    LAN_IP="$(detect_lan_ip)"
    if [ -z "$LAN_IP" ]; then
        warn "could not detect this box's LAN IPv4. docker-compose.media.yml and
       docker-compose.spotify.yml REQUIRE PUFFPAI_BIND_IP and refuse to
       start without it — add it to ${INSTALL_DIR}/.env by hand."
    fi

    # systemd/puffpai-api.service:65 + docs/CONFIG.md:18
    write_file "${ETC_DIR}/api.env" 0640 "root:${SVC_GROUP}" <<ENVFILE
# /etc/puffpai/api.env — EnvironmentFile for puffpai-api.service (0640 root:puffpai)
# Resolution order for the API key is vault -> OS keyring -> this file.
# GENERATED by scripts/install_orin.sh and UNIQUE TO THIS BOX. The backend
# refuses to start if it is ever replaced by a placeholder or a short value
# (src/puffpai/api_server.py, validate_api_key).
PUFFPAI_API_KEY=${API_KEY_VALUE}
# Home Assistant long-lived token — YOURS to paste in; we cannot mint it.
# HA: profile -> Security -> Long-lived access tokens.
# The intended home for it is the Vault; see
# systemd/puffpai-api.service.d/20-ha-token-failclosed.conf.
HA_TOKEN=CHANGE_ME
ENVFILE

    # systemd/puffpai-dashboard.service:37 + docs/CONFIG.md:115-128
    write_file "${ETC_DIR}/dashboard.env" 0640 "root:${SVC_GROUP}" <<ENVFILE
# /etc/puffpai/dashboard.env — EnvironmentFile for puffpai-dashboard.service
# Server-side only; nothing here is inlined into the browser bundle.
PUFFPAI_API_URL=http://127.0.0.1:${API_PORT}
# GENERATED — the SAME value as /etc/puffpai/api.env. They must match.
PUFFPAI_API_KEY=${API_KEY_VALUE}
# Home Assistant — yours to fill in (base URL + long-lived token).
HA_BASE_URL=CHANGE_ME
HA_ACCESS_TOKEN=CHANGE_ME
# Pairing gate: off | knowledge | all.
#
# A FRESH BOX MUST START AT 'knowledge'. Under 'all' every route is gated, and
# the kiosk browser on this appliance is just another unpaired client — there
# is deliberately no localhost bypass (dashboard/app/_lib/server/pairing.ts,
# "staged enforcement"). Seeding 'all' here 401s the dashboard before any
# device can pair, which locks the owner out of their own box on first boot.
#
# Pair the first device, THEN tighten:
#   sudo sed -i 's/^PUFF_PAIRING_ENFORCE=.*/PUFF_PAIRING_ENFORCE=all/' \\
#       /etc/puffpai/dashboard.env
#   sudo systemctl restart puffpai-dashboard
PUFF_PAIRING_ENFORCE=knowledge
# This box's LAN address; the media proxy stays off until it is set.
#JELLYFIN_LAN_HOST=
# This box's LAN address; the books proxy stays off until it is set.
#CALIBRE_LAN_HOST=
ENVFILE

    # docs/CONFIG.md:20 — compose reads /opt/puffpai/.env directly for the
    # Node-RED flow secrets; systemd/puffpai-nodered.service:32-34 wants 0600.
    write_file "${INSTALL_DIR}/.env" 0600 "${SVC_USER}:${SVC_GROUP}" <<ENVFILE
# /opt/puffpai/.env — read by the compose files. Git-ignored, 0600.
# GENERATED — PUFFPAI_API_KEY is the SAME value as /etc/puffpai/api.env.
PUFFPAI_API_KEY=${API_KEY_VALUE}
# Third-party tokens: yours to paste in. We cannot mint them.
HA_TOKEN=CHANGE_ME
JELLYFIN_API_KEY=CHANGE_ME
# This box's LAN IPv4, detected at install time. docker-compose.media.yml and
# docker-compose.spotify.yml REQUIRE it and refuse to start rather than guess:
# it is the address Jellyfin/Calibre publish on and the interface librespot
# advertises over zeroconf. Update it if this box's address changes.
PUFFPAI_BIND_IP=${LAN_IP}
# Where the media library is mounted (docker-compose.media.yml).
#PUFFPAI_MEDIA_DIR=${DATA_ROOT}/media
ENVFILE

    local hacfg="${INSTALL_DIR}/config/home_assistant_config.json"
    if [ ! -e "$hacfg" ] && [ -e "${hacfg}.example" ]; then
        run cp "${hacfg}.example" "$hacfg"
        run chown "${SVC_USER}:${SVC_GROUP}" "$hacfg"
        info "seeded home_assistant_config.json from the example"
    else
        info "home_assistant_config.json left as-is"
    fi

    # Re-run safety: write_file leaves a non-empty file alone, so a box
    # installed by an OLDER version of this script still holds
    # PUFFPAI_API_KEY=CHANGE_ME. Repair all three to the one shared value.
    set_api_key_in_file "${ETC_DIR}/api.env"
    set_api_key_in_file "${ETC_DIR}/dashboard.env"
    set_api_key_in_file "${INSTALL_DIR}/.env"

    printf '\n   %sSECRETS%s\n' "$GREEN" "$NC"
    info "  PUFFPAI_API_KEY was generated for THIS box and written to:"
    info "    ${ETC_DIR}/api.env          0640 root:${SVC_GROUP}"
    info "    ${ETC_DIR}/dashboard.env    0640 root:${SVC_GROUP}"
    info "    ${INSTALL_DIR}/.env         0600 ${SVC_USER}:${SVC_GROUP}"
    info "  Its value is NOT printed. To read it on the box:"
    info "    sudo grep '^PUFFPAI_API_KEY=' ${ETC_DIR}/api.env"
    info "  PUFFPAI_BIND_IP=${LAN_IP:-<undetected>}  <- confirm this is this"
    info "    box LAN address; the media/Spotify stacks publish on it."

    printf '\n   %sFILL THESE IN BEFORE FIRST REAL USE (third-party, we cannot mint them)%s\n' "$YELLOW" "$NC"
    info "  ${ETC_DIR}/api.env         HA_TOKEN"
    info "  ${ETC_DIR}/dashboard.env   HA_BASE_URL, HA_ACCESS_TOKEN"
    info "  ${INSTALL_DIR}/.env        HA_TOKEN, JELLYFIN_API_KEY"
    info "  then: systemctl restart puffpai-api puffpai-dashboard"
    info "  Storage paths need NO hand-editing: the units set"
    info "     PUFFPAI_DATA_DIR and the loader re-roots every relative"
    info "     paths.* entry and chromadb.path under it"
    info "     (src/puffpai/config.py, anchor_relative_state_paths)."

    printf '\n   %sPAIRING: this box starts at PUFF_PAIRING_ENFORCE=knowledge%s\n' "$YELLOW" "$NC"
    info "  Pair your first device from the dashboard, then tighten the gate:"
    info "    sudo sed -i 's/^PUFF_PAIRING_ENFORCE=.*/PUFF_PAIRING_ENFORCE=all/' ${ETC_DIR}/dashboard.env"
    info "    sudo systemctl restart puffpai-dashboard"
}

# ── 6. Ollama + models ──────────────────────────────────────────────────────
# The tags are READ OUT OF THE CODE, never hardcoded here: change the model the
# product runs and this installer follows automatically.
#   config/puffpai_config.json    ollama.model, ollama.vision_model
#   src/puffpai/agents/engine.py  DEFAULT_CLASSIFY_MODEL, DEFAULT_PLAN_MODEL
#   src/puffpai/config.py         ollama_embedding_model
# ollama.code_model ships as the empty string (off by default) and is
# deliberately NOT pulled — it is an on-demand model, never pinned resident.
pinned_models() {
    {
        sed -n 's/.*"\(model\|vision_model\)"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\2/p' \
            "${REPO_DIR}/config/puffpai_config.json"
        sed -n 's/^DEFAULT_\(CLASSIFY\|PLAN\)_MODEL[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\2/p' \
            "${REPO_DIR}/src/puffpai/agents/engine.py"
        sed -n 's/^[[:space:]]*ollama_embedding_model:[[:space:]]*str[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${REPO_DIR}/src/puffpai/config.py"
    } | grep -v '^$' | awk '!seen[$0]++'
}

phase_ollama() {
    phase "ollama"
    if command -v ollama >/dev/null 2>&1; then
        info "ollama present: $(ollama --version 2>/dev/null | head -1)"
    else
        info "Ollama is not installed. The official installer will be downloaded from:"
        info "    ${OLLAMA_INSTALL_URL}"
        info "It detects the Jetson CUDA runtime, and creates its own 'ollama'"
        info "system user and systemd unit — we do not ship one."
        confirm "Download and run it?"
        run_sh "curl -fsSL ${OLLAMA_INSTALL_URL} -o /tmp/ollama-install.sh"
        if [ "$DRY_RUN" -eq 0 ]; then
            [ -s /tmp/ollama-install.sh ] || die "the Ollama installer downloaded empty — check DNS/network."
        fi
        run sh /tmp/ollama-install.sh
        run rm -f /tmp/ollama-install.sh
    fi

    # OLLAMA_MODELS -> the NVMe; OLLAMA_HOST -> 127.0.0.1:11434. Ollama has
    # no authentication, and this appliance ships with no host packet filter,
    # so the bind IS the access control. Everything that needs Ollama runs on
    # this host. See systemd/ollama.service.d/20-lan-ollama.conf.example for
    # the opt-in, and what it costs.
    run install -D -m 0644 "${REPO_DIR}/systemd/ollama.service.d/override.conf" \
        /etc/systemd/system/ollama.service.d/override.conf
    run systemctl daemon-reload
    run systemctl enable ollama
    run systemctl restart ollama

    phase "system (provision_orin.sh pass 3 of 3 — hand models/ollama to the ollama user)"
    # MUST happen before any pull: models/ollama cannot be chowned to a user
    # that did not exist yet, and pulling first fails with EACCES
    # (provision_orin.sh:413-427).
    run bash "${INSTALL_DIR}/scripts/provision_orin.sh" --yes

    if [ "$SKIP_MODELS" -eq 1 ]; then
        info "--skip-models: not pulling anything."
        return 0
    fi
    local tags tag
    tags="$(pinned_models)"
    [ -n "$tags" ] || die "could not read any model tag out of the checkout.
       Expected tags in config/puffpai_config.json, src/puffpai/agents/engine.py
       and src/puffpai/config.py. Refusing to invent one."
    info "tags pinned by this checkout (~39 GB total):"
    for tag in $tags; do
        printf '        %-20s %5s GB\n' "$tag" "$(model_size_gb "$tag")"
    done
    info "On 100 Mbit that is 60-90 minutes; on 1 Gbit, 10-15."
    run_sh "for i in \$(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 2; done"
    for tag in $tags; do
        if [ "$DRY_RUN" -eq 0 ] && ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$tag"; then
            info "${tag} already present, skipping"
            continue
        fi
        run ollama pull "$tag"
    done
}

# ── 7. Services: units, containers, optional extras ─────────────────────────
phase_services() {
    phase "services"

    local u
    for u in puffpai-api.service puffpai-dashboard.service puffpai-nodered.service; do
        run install -D -m 0644 "${REPO_DIR}/systemd/${u}" "/etc/systemd/system/${u}"
    done
    # Pulse owns the hardware mixer, and the API runs with ProtectHome=true,
    # which hides /run/user and therefore the Pulse socket. This drop-in binds
    # it back in. The OTHER drop-in (20-ha-token-failclosed.conf) is
    # deliberately NOT installed: it must wait until the HA token has been
    # migrated into the Vault and verified — its own comments carry the
    # ordered procedure.
    run install -D -m 0644 "${REPO_DIR}/systemd/puffpai-api.service.d/10-pulse-volume.conf" \
        /etc/systemd/system/puffpai-api.service.d/10-pulse-volume.conf
    run systemctl daemon-reload
    run systemctl enable --now puffpai-api puffpai-dashboard puffpai-nodered
    info "Node-RED editor: 127.0.0.1:1880 — import nodered_flows/flows.json"
    info "through it once (nodered_flows/README.md); nothing does it for you."

    # SearXNG — UNVERIFIED ON A FRESH BOX (docs/INSTALL_ORIN.md §9): there is
    # no script for it; these are the compose file's own header instructions.
    phase "services: searxng"
    unverified
    run install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0750 "${DATA_ROOT}/searxng/config"
    local sx="${DATA_ROOT}/searxng/config/settings.yml"
    if [ -e "$sx" ]; then
        info "${sx} exists — left untouched (it holds the secret key)"
    else
        run cp "${REPO_DIR}/searxng/config/settings.yml.example" "$sx"
        # Generated on the box, never printed: the file is a secret from here on.
        run_sh "sed -i \"s|^\\([[:space:]]*secret_key:\\).*|\\1 \\\"\$(openssl rand -hex 32)\\\"|\" '${sx}'"
        run chown "${SVC_USER}:${SVC_GROUP}" "$sx"
        run chmod 0640 "$sx"
        info "settings.yml created with a generated secret_key (not printed, never commit it)"
    fi
    run docker compose -f "${INSTALL_DIR}/searxng/docker-compose.searxng.yml" up -d
    info "published on 127.0.0.1:8888 only — nothing on the LAN can reach it"

    # Voice — UNVERIFIED ON A FRESH BOX (docs/INSTALL_ORIN.md §10): a USER unit,
    # because it needs the PulseAudio session. It cannot be installed for
    # 'puffpai', which is a nologin system account with no session; it belongs
    # to the human who is logged in at the box.
    phase "services: voice"
    if [ "$SKIP_VOICE" -eq 1 ]; then
        info "--skip-voice: not installing the user unit."
    elif [ ! -f "${REPO_DIR}/systemd/user/puffpai-voice.service" ]; then
        info "this checkout carries no user voice unit — voice install is MANUAL."
    else
        unverified
        local vuser="${SUDO_USER:-}"
        if [ -z "$vuser" ] || [ "$vuser" = "root" ]; then
            warn "cannot tell which desktop user owns the audio session (SUDO_USER unset)."
            info "Install it by hand as that user (docs/INSTALL_ORIN.md §10):"
            info "  mkdir -p ~/.config/systemd/user"
            info "  cp -r ${INSTALL_DIR}/systemd/user/puffpai-voice.service* ~/.config/systemd/user/"
            info "  systemctl --user daemon-reload && systemctl --user enable --now puffpai-voice"
            info "  sudo loginctl enable-linger <that user>"
        else
            local vhome; vhome="$(getent passwd "$vuser" | cut -d: -f6)"
            run install -d -o "$vuser" -m 0755 "${vhome}/.config/systemd/user"
            run cp -r "${REPO_DIR}/systemd/user/puffpai-voice.service" \
                      "${REPO_DIR}/systemd/user/puffpai-voice.service.d" \
                      "${vhome}/.config/systemd/user/"
            run chown -R "$vuser" "${vhome}/.config/systemd/user"
            run loginctl enable-linger "$vuser"
            run sudo -u "$vuser" XDG_RUNTIME_DIR="/run/user/$(id -u "$vuser" 2>/dev/null || echo 1000)" \
                systemctl --user daemon-reload
            run sudo -u "$vuser" XDG_RUNTIME_DIR="/run/user/$(id -u "$vuser" 2>/dev/null || echo 1000)" \
                systemctl --user enable --now puffpai-voice
            info "installed for user '${vuser}'; the mic is picked by name with PUFF_CAPTURE_DEVICE"
        fi
    fi

    # Zone 3 — opt-in, and inert until the router's DNS points here.
    if [ "$WITH_ZONE3" -eq 1 ]; then
        phase "services: zone3"
        run install -D -m 0644 "${REPO_DIR}/systemd/puffpai-zone3.service" \
            /etc/systemd/system/puffpai-zone3.service
        run systemctl daemon-reload
        run systemctl enable --now puffpai-zone3
        info "puffpai-zone3.service runs as root: 'docker compose' needs the"
        info "      docker socket, and socket access is root-equivalent either way."
        info "      Granting it to the 'puffpai' account instead would make a bug"
        info "      in the network-facing backend a root compromise, so the unit"
        info "      keeps the privilege in one oneshot. Do NOT add puffpai to the"
        info "      docker group."
        # The venv interpreter, not system python3: the bootstrap imports
        # bcrypt (requirements.txt) to hash the AdGuard admin password, and
        # system python3 does not have it.
        run "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/scripts/zone3_bootstrap_adguard.py"
        info "Nothing on the network changes until the router's DNS points here,"
        info "and protection ships OFF. Then set zone3.enabled in the config."
    else
        info "Zone 3 not installed (pass --with-zone3)."
    fi

    # Kiosk — opt-in; provision_kiosk.sh owns it end to end.
    if [ "$WITH_KIOSK" -eq 1 ]; then
        phase "services: kiosk"
        run bash "${INSTALL_DIR}/scripts/provision_kiosk.sh"
        info "Takes effect on the next GDM start: sudo systemctl restart gdm3"
        info "Read docs/KIOSK_MODE.md first — and use ?puffMode=kiosk on the URL."
    else
        info "Kiosk not installed (pass --with-kiosk)."
    fi
}

# ── 8. Verify ───────────────────────────────────────────────────────────────
phase_verify() {
    phase "verify"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "+ curl -fsS http://127.0.0.1:${API_PORT}/health   (skipped in --dry-run)"
        info "+ keyed GET /vault/mfa-status                     (skipped in --dry-run)"
        info "+ curl -fsS http://127.0.0.1:${DASH_PORT}/        (skipped in --dry-run)"
        info "+ systemctl is-active puffpai-api puffpai-dashboard puffpai-nodered ollama"
        info "+ placeholder scan of ${ETC_DIR}/*.env and ${INSTALL_DIR}/.env"
        return 0
    fi

    # /health is deliberately PUBLIC (api_server._PUBLIC_ENDPOINTS), so the
    # only success code is 200. A 401 here would mean the gate had grown over
    # the liveness probe, which is a failure, not a pass.
    local i code=""
    for i in $(seq 1 45); do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
                "http://127.0.0.1:${API_PORT}/health" || true)"
        [ "$code" = "200" ] && { info "GET :${API_PORT}/health -> 200 — backend up"; break; }
        sleep 2
    done
    if [ "$code" != "200" ]; then
        verify_fail "the backend never answered 200 on :${API_PORT}/health (last: '${code:-none}').
       journalctl -u puffpai-api -n 100 --no-pager"
    fi

    # A KEYED call that actually touches storage. /health answers from memory
    # and stays 200 on a box whose Vault directory is read-only — exactly the
    # failure this phase exists to catch. /vault/mfa-status constructs the
    # Vault against the directory under PUFFPAI_DATA_DIR and is behind the API
    # key, so one call proves three things: the key in api.env is the key the
    # service resolved, the gate is live, and the state root is reachable.
    local api_key vbody vcode
    api_key="$(env_value "${ETC_DIR}/api.env" PUFFPAI_API_KEY)"
    if is_placeholder "$api_key"; then
        verify_fail "PUFFPAI_API_KEY in ${ETC_DIR}/api.env is still a placeholder.
       The backend refuses to start with one. Re-run this installer."
    else
        # The key reaches curl through a header file on stdin, never argv: a
        # command line is world-readable in /proc for as long as it runs.
        vbody="$(printf 'X-API-Key: %s' "$api_key"                  | curl -s -w '
%{http_code}' --max-time 10 -H @-                         "http://127.0.0.1:${API_PORT}/vault/mfa-status" || true)"
        vcode="$(printf '%s' "$vbody" | tail -1)"
        case "$vcode" in
            200)
                # 200 is not enough: _get_vault() swallows an unwritable vault
                # directory and answers vault_available=false with a 200, which
                # is exactly the read-only-/opt failure this phase exists to
                # catch. The BODY is the assertion.
                if printf '%s' "$vbody" | grep -q '"vault_available": *true'; then
                    info "GET :${API_PORT}/vault/mfa-status -> 200, vault_available=true"
                    info "   keyed call OK and the writable state root is reachable"
                else
                    verify_fail "/vault/mfa-status answered 200 but vault_available=false:
       the backend cannot open its vault directory. Check PUFFPAI_DATA_DIR in
       systemd/puffpai-api.service and that ${DATA_ROOT} is writable by
       ${SVC_USER}. journalctl -u puffpai-api -n 50 --no-pager | grep -i vault"
                fi
                ;;
            401|403)
                verify_fail "/vault/mfa-status -> ${vcode}: the key in ${ETC_DIR}/api.env is
       NOT the key puffpai-api resolved (vault -> OS keyring -> this file).
       journalctl -u puffpai-api -n 50 --no-pager"
                ;;
            *)
                verify_fail "/vault/mfa-status -> '${vcode:-none}': no keyed answer from the
       backend. journalctl -u puffpai-api -n 50 --no-pager"
                ;;
        esac
    fi

    local dash_up=0
    for i in $(seq 1 30); do
        if curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:${DASH_PORT}/"; then
            info "GET :${DASH_PORT}/ -> OK — dashboard up"; dash_up=1; break
        fi
        sleep 2
    done
    if [ "$dash_up" -eq 0 ]; then
        verify_fail "the dashboard never answered on :${DASH_PORT}.
       A .next older than the source is a clean unit failure — rebuild.
       journalctl -u puffpai-dashboard -n 100 --no-pager"
    fi

    # Third-party credentials we cannot mint. Loud, by name, and non-zero at
    # the end: a box shipped with HA_TOKEN=CHANGE_ME silently has no Home
    # Assistant control, and the owner finds out by asking for the lights.
    local f k
    for f in "${ETC_DIR}/api.env" "${ETC_DIR}/dashboard.env" "${INSTALL_DIR}/.env"; do
        [ -r "$f" ] || continue
        for k in HA_TOKEN HA_ACCESS_TOKEN HA_BASE_URL JELLYFIN_API_KEY; do
            grep -qE "^[[:space:]]*${k}=" "$f" 2>/dev/null || continue
            if is_placeholder "$(env_value "$f" "$k")"; then
                verify_fail "${f}: ${k} is still a placeholder — paste the real value in."
            fi
        done
    done

    local u units="ollama puffpai-api puffpai-dashboard puffpai-nodered"
    [ "$WITH_ZONE3" -eq 1 ] && units="${units} puffpai-zone3"
    for u in $units; do
        printf '   %-24s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || echo unknown)"
    done

    cat <<NEXT

${GREEN}HomeBot is installed.${NC}

  Dashboard   http://$(hostname -I 2>/dev/null | awk '{print $1}'):${DASH_PORT}
  Wall panel  same URL + ?puffMode=kiosk   (engages the motion governor)
  Backend     port ${API_PORT}, API-key gated on every route but /health.
              It binds 0.0.0.0 so an off-box Home Assistant can reach it; the
              API key is the only thing in front of it, and this box has no
              host packet filter. If nothing off-box needs it, set
              PUFFPAI_API_BIND=127.0.0.1 in ${ETC_DIR}/api.env.
  Pairing     PUFF_PAIRING_ENFORCE=knowledge. Pair the first device with the
              code the dashboard shows, then set it to 'all' and restart
              puffpai-dashboard.

Before you call it done, run the acceptance list printed at the end of
scripts/provision_orin.sh (the "Next steps" block, item "Acceptance check"):
a few questions you know the answers to, voice round trip, vault decrypt,
Home Assistant control, and the WAN-unplugged test.

Logs: journalctl -u puffpai-api -f
NEXT

    if [ "$VERIFY_FAILURES" -gt 0 ]; then
        printf '%s\n   %d VERIFY FAILURE(S) — this box is NOT ready:%s%s\n' \
            "$RED" "$VERIFY_FAILURES" "$VERIFY_LIST" "$NC" >&2
        return 1
    fi
}

# ── main ────────────────────────────────────────────────────────────────────
# Everything lives in main(), invoked on the LAST line, so a truncated download
# executes nothing at all rather than half an installer.
main() {
    parse_args "$@"
    printf '%sPuffPAI HomeBot — Founders Edition installer (Jetson AGX Orin)%s\n' "$BLUE" "$NC"
    printf '   repo %s -> %s\n' "$REPO_DIR" "$INSTALL_DIR"
    [ "$DRY_RUN" -eq 1 ] && info "--dry-run: every command is printed, none is executed."
    phase_preflight
    phase_system
    phase_code
    phase_dashboard
    phase_env
    phase_ollama
    phase_services
    # Non-zero when verify found something: a fresh box that is missing a
    # required credential or cannot write its state has NOT installed
    # successfully, and an automated caller must be able to see that.
    phase_verify
}

main "$@"
