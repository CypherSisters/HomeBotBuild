#!/usr/bin/env bash
#
# PuffPAI: Jetson AGX Orin provisioning
#
# Prepares a fresh JetPack 6.2.2 (Ubuntu 22.04, L4T r36.5) box for the
# headless appliance: OS packages, the /data/puffpai layout on the NVMe, and
# the /etc/puffpai secret files the systemd units expect.
#
# Run it MANUALLY, and read it first. It is idempotent — re-running is safe
# and is the intended way to pick up changes.
#
#     sudo ./scripts/provision_orin.sh --dry-run   # print the plan, change nothing
#     sudo ./scripts/provision_orin.sh             # asks before it changes anything
#     sudo ./scripts/provision_orin.sh --yes       # unattended (the installer uses this)
#
# scripts/install_ubuntu.sh calls this script; you do not need to run both.
#
# What it deliberately does NOT do:
#   * install Ollama          — one-line official installer, run it yourself
#                               (it detects the Jetson CUDA runtime); then
#                               apply systemd/ollama.service.d/override.conf
#   * install the systemd units — see systemd/README.md
#   * create the Python venv / build the dashboard — that is the deploy step
#   * partition or format the NVMe — do that once, by hand, before this
#   * touch nftables rules    (Zone 3 / firewall is set up separately)
#
# See systemd/README.md for how the pieces fit together.

set -euo pipefail

DATA_ROOT="${PUFFPAI_DATA_ROOT:-/data/puffpai}"
ETC_DIR="/etc/puffpai"
SVC_USER="${PUFFPAI_USER:-puffpai}"
SVC_GROUP="${PUFFPAI_GROUP:-puffpai}"

MIN_FREE_GB="${PUFFPAI_MIN_FREE_GB:-80}"

log()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   WARNING: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

DRY_RUN=0
ASSUME_YES=0

usage() {
    cat <<'USAGE'
Usage: sudo ./scripts/provision_orin.sh [options]

  --yes, -y       skip the confirmation prompt (unattended / called by the installer)
  --dry-run, -n   print what would change and touch nothing
  -h, --help      this text

Idempotent: re-running is safe and is the intended way to pick up changes.
It never overwrites an existing secret file.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)     ASSUME_YES=1 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help)    usage; exit 0 ;;
        *)            usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        info "dry-run: would require root (not running as root — continuing)"
    else
        echo "This script must run as root:  sudo $0" >&2
        exit 1
    fi
fi

# ── 0. Preflight ────────────────────────────────────────────────────────────
# Refuse early rather than half-provision. Cheap checks only; the detailed
# hardware report is section 1 below.
if [ ! -r /etc/os-release ]; then
    die "/etc/os-release missing — unsupported OS."
fi
# shellcheck disable=SC1091
. /etc/os-release
: "${ID:=unknown}" "${VERSION_ID:=0.0}"
case "${ID}${ID_LIKE:-}" in
    *ubuntu*|*debian*) : ;;
    *) die "unsupported distribution '${ID}'. Ubuntu 22.04 (JetPack 6.2.2) is the target." ;;
esac
if [ "${VERSION_ID%%.*}" -lt 20 ] 2>/dev/null; then
    die "Ubuntu ${VERSION_ID} is too old; 20.04 is the floor, 22.04 is the target."
fi
case "$(uname -m)" in
    x86_64|aarch64) : ;;
    *) die "unsupported architecture '$(uname -m)' (need x86_64 or aarch64)." ;;
esac
# Disk: the models alone are ~39 GB and land under DATA_ROOT.
_probe="$DATA_ROOT"
while [ ! -d "$_probe" ] && [ "$_probe" != "/" ]; do _probe="$(dirname "$_probe")"; done
_free_gb="$(df -BG --output=avail "$_probe" | tail -1 | tr -dc '0-9')"
if [ "${_free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
    die "only ${_free_gb} GB free on ${_probe} (for ${DATA_ROOT}); need ${MIN_FREE_GB} GB
       for ~39 GB of models plus ChromaDB, ZIM packs and logs. Mount the NVMe first,
       or override with PUFFPAI_MIN_FREE_GB= if you know what you are doing."
fi

# ── 0b. Plan + confirmation ─────────────────────────────────────────────────
cat <<PLAN

PuffPAI provisioning — ${ID} ${VERSION_ID} on $(uname -m), ${_free_gb} GB free on ${_probe}

It will, as root:
  * apt-get update and install the OS package set (python3-venv, build-essential,
    tesseract-ocr, poppler-utils, nftables, libportaudio2, alsa-utils, sqlite3, ...)
  * add Docker's apt repo and the compose v2 plugin if compose v2 is missing
    (it never replaces a working engine — that would take JetPack's NVIDIA runtime)
  * create the system account ${SVC_USER}:${SVC_GROUP} and add it to 'audio'
  * create /opt/puffpai, /opt/puffpai-dashboard and the ${DATA_ROOT} layout
  * create ${ETC_DIR}/api.env and dashboard.env EMPTY if absent — existing
    files are never touched
It does NOT install Ollama, the systemd units, the venv, or firewall rules.

PLAN

if [ "$DRY_RUN" -eq 1 ]; then
    info "--dry-run: commands are printed, nothing is executed."
    # Shadow every mutating command with a printer. These definitions exist
    # ONLY in dry-run mode, so a real run is unaffected by them.
    for _c in apt-get install useradd usermod groupadd chown chmod systemctl gpg curl; do
        eval "${_c}() { printf '   + %s %s\n' '${_c}' \"\$*\"; }"
    done
elif [ "$ASSUME_YES" -eq 0 ]; then
    if [ ! -t 0 ]; then
        die "no terminal to confirm on. Pass --yes for an unattended run."
    fi
    read -r -p "Proceed? [y/N] " _reply
    case "$_reply" in
        [Yy]*) : ;;
        *) echo "Cancelled."; exit 1 ;;
    esac
fi

# ── 1. Sanity: is this actually the box we think it is? ─────────────────────
log "Hardware / OS"
if [ -r /etc/nv_tegra_release ]; then
    info "L4T: $(head -n1 /etc/nv_tegra_release)"
else
    info "WARNING: /etc/nv_tegra_release missing — this does not look like a"
    info "         Jetson. Continuing anyway (the script is not Tegra-specific)."
fi
info "RAM: $(free -h | awk '/^Mem:/ {print $2}')  (the model plan assumes 64 GB)"
info "Arch: $(uname -m)"
if ! lsblk -dno NAME | grep -q '^nvme'; then
    info "WARNING: no NVMe device found. The 64 GB eMMC cannot hold the models"
    info "         + ChromaDB + ZIM packs. Mount an NVMe at /data before"
    info "         running the appliance for real."
fi

# ── 2. OS packages ─────────────────────────────────────────────────────────
log "Installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update

# Grouped by why we need them, so this list stays auditable.
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev build-essential \
    git curl ca-certificates gnupg \
    net-tools \
    tesseract-ocr poppler-utils \
    nftables \
    libportaudio2 alsa-utils \
    sqlite3
#   net-tools     -> `arp`. device_sentinel._scan_ip() shells out to `arp -a`
#                    (cross-platform parser, verified — no PowerShell). Ubuntu
#                    22.04 does not install net-tools by default, so the LAN
#                    scan silently returns nothing without this.
#                    Dropping net-tools would need an `ip neigh` parsing
#                    branch in device_sentinel.py.
#   tesseract-ocr -> image/handwriting OCR on uploads. ingestion.py finds it
#                    via shutil.which("tesseract") -> /usr/bin/tesseract.
#                    Without it OCR is skipped, not broken. The Python side
#                    also needs the "ocr" extra (pytesseract + pdf2image) —
#                    the binary alone is not enough.
#   poppler-utils -> pdftoppm, required by pdf2image for OCR of scanned PDFs
#                    (file_processor._ocr_pdf). Without it, image-only PDFs
#                    extract as empty.
#   nftables      -> NftablesFirewall backend (firewall_manager.py). Installed
#                    here; no rules are installed.
#   libportaudio2 -> the `sounddevice` wheel needs it at runtime for mic/spk.
#   alsa-utils    -> `arecord -l` / `aplay -l` for selecting audio devices by
#                    name rather than by index.

# ── 3. Node.js for the dashboard ───────────────────────────────────────────
# package.json requires node ^20.19 || >=22.12. Ubuntu 22.04 ships something
# far older, so this needs NodeSource. Not automated: adding a third-party
# apt repo should be a deliberate, eyes-on decision.
log "Node.js"
if command -v node >/dev/null 2>&1; then
    info "node $(node --version) present"
else
    info "node NOT installed. The dashboard needs >= 20.19. Install via"
    info "NodeSource, then re-check:"
    info "    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -"
    info "    sudo apt-get install -y nodejs"
fi

# ── 3b. Docker Engine + compose v2 ─────────────────────────────────────────
# puffpai-nodered.service needs `docker compose` (v2, the plugin subcommand).
# Ubuntu 22.04's docker.io package ships neither the plugin nor a current
# engine, so use Docker's official apt repo. arm64 is a first-class arch there.
#
# CAREFUL: JetPack images ship docker.io preconfigured with the NVIDIA
# container runtime. Installing docker-ce on top makes apt REMOVE docker.io,
# which can take that GPU integration with it. So the branch below only ever
# adds the compose plugin when an engine is already present — it never
# replaces a working engine.
log "Docker Engine + compose plugin"
_have_engine() { command -v docker >/dev/null 2>&1; }
_have_compose() { docker compose version >/dev/null 2>&1; }

_add_docker_repo() {
    install -d -m 0755 /etc/apt/keyrings
    if [ ! -e /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
    fi
    local arch codename
    arch="$(dpkg --print-architecture)"
    codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
    local line="deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${codename} stable"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "+ would write /etc/apt/sources.list.d/docker.list: ${line}"
    else
        echo "$line" > /etc/apt/sources.list.d/docker.list
    fi
    apt-get update
}

if _have_engine && _have_compose; then
    info "docker $(docker --version | awk '{print $3}' | tr -d ,) with compose v2 present"
elif _have_engine; then
    # Engine present (almost certainly JetPack's docker.io) but no compose v2.
    # docker-compose-plugin is just a CLI plugin binary — it installs happily
    # alongside docker.io and does NOT pull docker-ce.
    info "engine present without compose v2 — adding the plugin only"
    _add_docker_repo
    apt-get install -y --no-install-recommends docker-compose-plugin
    if _have_compose; then
        info "compose v2 added, existing engine left untouched"
    else
        info "WARNING: compose v2 still unavailable. Install the plugin binary"
        info "         manually into /usr/local/lib/docker/cli-plugins/ rather"
        info "         than installing docker-ce, which would remove docker.io"
        info "         and possibly the NVIDIA container runtime with it."
    fi
else
    info "no docker engine found — installing docker-ce + compose v2"
    _add_docker_repo
    apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    info "installed docker + compose v2"
fi

# ── 4. Service account ─────────────────────────────────────────────────────
log "Service account: ${SVC_USER}:${SVC_GROUP}"
if ! getent group "$SVC_GROUP" >/dev/null; then
    groupadd --system "$SVC_GROUP"
    info "created group $SVC_GROUP"
fi
if ! getent passwd "$SVC_USER" >/dev/null; then
    useradd --system --gid "$SVC_GROUP" --home-dir /opt/puffpai \
            --shell /usr/sbin/nologin "$SVC_USER"
    info "created user $SVC_USER"
fi
# Mic/speaker access for the voice pipeline.
usermod -aG audio "$SVC_USER"
# Deliberately NOT added to the docker group: puffpai-nodered.service has no
# User=, so it runs as root like most compose units, and membership would buy
# nothing today. Docker group membership is root-equivalent (you can bind-mount
# / into a container), so it is not something to grant "just in case".

# ── 4b. Application directories ────────────────────────────────────────────
# The units use WorkingDirectory=/opt/puffpai and /opt/puffpai-dashboard, and
# the service user's home is /opt/puffpai. Create them now so the checkout and
# the venv have somewhere to land — the deploy step fills them.
#
# The dashboard is not a separate repository: its source lives INSIDE the
# checkout at /opt/puffpai/dashboard. /opt/puffpai-dashboard
# stays a separate directory on purpose: puffpai-dashboard.service needs a
# writable .next/ and /opt/puffpai is read-only under ProtectSystem=strict. The
# deploy step copies dashboard/ across (see step 4 below); this script does not
# do it, because it must not touch a build that is already serving.
log "Application directories"
for appdir in /opt/puffpai /opt/puffpai-dashboard; do
    install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0755 "$appdir"
    info "$appdir ready"
done

# Repo-relative runtime writers. Seven modules write next to the code instead
# of under /data (no config hook), so
# puffpai-api.service punches these specific paths through its read-only /opt
# via ReadWritePaths. They must exist and be owned by the service user.
# security_logs/ holds the SECURITY AUDIT TRAIL, which FAILS OPEN: if it is
# not writable, entries go to stderr and are lost. See systemd/README.md.
#
# This needs the code to be in place, so it only runs once /opt/puffpai is an
# actual checkout. Re-run the script after deploying — that is the intended
# workflow, and it is why everything here is idempotent.
if [ -d /opt/puffpai/src/puffpai ]; then
    # NOTE the two security_logs at different depths — this is not a typo.
    # audit_log.py resolves the repo root from src/puffpai/security/ and lands
    # in src/; dashboard_registry.py and dashboard_widgets.py resolve from
    # src/puffpai/ and land at the root. src/security_logs is the SECURITY
    # AUDIT TRAIL and is the more important of the two.
    for d in src/security_logs security_logs data data/library_sources app_data; do
        install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0750 "/opt/puffpai/${d}"
    done

    # Seed the untracked JSON state files. Neither is in git, so on a fresh
    # checkout they are absent, the file-level ReadWritePaths entry matches
    # nothing, and the first write hits EROFS on the read-only /opt.
    # Both readers tolerate a missing OR malformed file by falling back to an
    # empty list, so "[]" is a safe starting value:
    #   blocked_devices.json      firewall_manager._load_blocked_list catches
    #                             FileNotFoundError/JSONDecodeError -> []
    #   indexed_libraries.json    api_server catches the same -> []
    for f in blocked_devices.json src/puffpai/indexed_libraries.json; do
        if [ ! -e "/opt/puffpai/${f}" ]; then
            if [ "$DRY_RUN" -eq 1 ]; then
                info "+ would seed ${f} with []"
            else
                echo '[]' > "/opt/puffpai/${f}"
                info "seeded ${f} with []"
            fi
        fi
        chown "$SVC_USER:$SVC_GROUP" "/opt/puffpai/${f}"
        chmod 0640 "/opt/puffpai/${f}"
    done
    # emergency_inventory.json is tracked, so the checkout provides it. It is
    # deliberately NOT seeded: emergency_prep.py json.load()s the file whenever
    # it exists, with no exception handling, so an empty placeholder would be
    # worse than no file at all.
    [ -e /opt/puffpai/emergency_inventory.json ] && \
        chown "$SVC_USER:$SVC_GROUP" /opt/puffpai/emergency_inventory.json
    info "repo-relative writable paths ready"
else
    info "NOTE: /opt/puffpai is not a checkout yet. Deploy the code, then"
    info "      re-run this script to create security_logs/, data/, app_data/"
    info "      and fix ownership — the API unit needs them to start clean."
fi

# ── 5. /data/puffpai layout ────────────────────────────────────────────────
# Everything writable lives here, rooted at /data/puffpai, on the NVMe —
# the systemd units mount the rest of the filesystem read-only.
log "Data layout under ${DATA_ROOT}"
install -d -o root -g root -m 0755 "$(dirname "$DATA_ROOT")"
install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0750 "$DATA_ROOT"

for d in \
    models models/voice models/llm \
    knowledge network config logs chat_history app_data chromadb \
    home
do
    install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0750 "${DATA_ROOT}/${d}"
done
# home/ is HOME for puffpai-dashboard.service: systemd would otherwise set
# HOME=/opt/puffpai, which is read-only under ProtectSystem=strict, and npm
# fails creating $HOME/.npm.

# models/ollama is OLLAMA_MODELS (see systemd/ollama.service.d/override.conf).
# The official installer runs ollama as its own "ollama" user, NOT as puffpai,
# so this one directory must be owned by ollama or `ollama pull` fails with
# EACCES. Group stays puffpai + setgid so the backend can still read the blobs
# and new files inherit the group.
install -d -m 2770 "${DATA_ROOT}/models/ollama"
if getent passwd ollama >/dev/null; then
    chown "ollama:$SVC_GROUP" "${DATA_ROOT}/models/ollama"
    # Owning the leaf is useless without traverse (x) on the 0750 parents:
    # the installer creates 'ollama' outside the puffpai group, and without
    # this ollama.service crash-loops on "mkdir: permission denied" trying
    # to walk /data/puffpai -> models. Group membership is the fix that
    # keeps the deliberate 0750 design intact without widening the parent
    # directories' mode.
    usermod -aG "$SVC_GROUP" ollama
    info "models/ollama owned by ollama:${SVC_GROUP} (2770); ollama added to ${SVC_GROUP}"
else
    chown "root:$SVC_GROUP" "${DATA_ROOT}/models/ollama"
    info "NOTE: no 'ollama' user yet — models/ollama left root:${SVC_GROUP}."
    info "      Install Ollama, then RE-RUN this script to hand it over."
fi

# Zone 1: vault is 0700, owner only — never group-readable.
install -d -o "$SVC_USER" -g "$SVC_GROUP" -m 0700 "${DATA_ROOT}/vault"
info "layout ready (vault 0700, everything else 0750)"

# ── 6. /etc/puffpai secret files ───────────────────────────────────────────
# EnvironmentFile targets for the units. 0640 root:puffpai — root-owned so the
# service cannot rewrite its own secrets, group-readable so it can read them.
# Created empty if absent and NEVER overwritten, so re-running cannot wipe
# live secrets.
log "Environment files in ${ETC_DIR}"
install -d -o root -g "$SVC_GROUP" -m 0750 "$ETC_DIR"
for f in api.env dashboard.env; do
    if [ -e "${ETC_DIR}/${f}" ]; then
        info "${f} exists — left untouched"
    else
        install -o root -g "$SVC_GROUP" -m 0640 /dev/null "${ETC_DIR}/${f}"
        info "created empty ${f}"
    fi
done

# ── 7. Next steps ──────────────────────────────────────────────────────────
log "Provisioning complete. Remaining steps, in order:"
cat <<'NEXT'
   1. Ollama — INSTALL, RE-RUN THIS SCRIPT, *THEN* PULL. The pull writes into
      /data/puffpai/models/ollama, which cannot be handed to the "ollama" user
      until that user exists. Pulling before the re-run fails with EACCES.

        a) curl -fsSL https://ollama.com/install.sh | sh
        b) sudo install -D -m 0644 systemd/ollama.service.d/override.conf \
                /etc/systemd/system/ollama.service.d/override.conf
           sudo systemctl daemon-reload && sudo systemctl restart ollama
        c) sudo ./scripts/provision_orin.sh      # <- chowns the model dir
        d) the four models the build actually runs (~39 GB total):
             ollama pull gpt-oss:20b        # 13.8 GB  chat / general answering
             ollama pull qwen3:30b-a3b      # 18.6 GB  agent classify + plan
             ollama pull qwen2.5vl:7b       #  6.0 GB  vision
             ollama pull nomic-embed-text   #  0.3 GB  embeddings
           (scripts/install_ubuntu.sh does steps a-d for you.)

   2. Code + venv:
        /opt/puffpai            repo checkout (the dir exists and is empty),
        python3 -m venv venv && venv/bin/pip install -r requirements.txt
        # Embeddings: the default backend still needs the legacy extra —
        #   venv/bin/pip install -e ".[embeddings-legacy]"
        # and on Jetson, torch must come from NVIDIA's wheel index FIRST.
        # The torch-free path is models.embedding_backend="ollama", which
        # is only correct after a full re-index. See src/puffpai/embeddings.py.
        # OCR (image + scanned-PDF uploads):
        #   venv/bin/pip install -e ".[ocr]"
      THEN RE-RUN THIS SCRIPT — with the code present it creates
      src/security_logs/, security_logs/, data/ and app_data/ writable by
      puffpai, and seeds the untracked JSON state files.

   3. Config: storage paths need no editing. The units set PUFFPAI_DATA_DIR
      and the loader re-roots every relative paths.* entry and chromadb.path
      under it (src/puffpai/config.py, anchor_relative_state_paths).
      Fill in the THIRD-PARTY values in /etc/puffpai/api.env and
      dashboard.env (HA_TOKEN, HA_BASE_URL, HA_ACCESS_TOKEN); PUFFPAI_API_KEY
      is generated for the box by scripts/install_orin.sh.

   4. Dashboard: it is IN the checkout, so copy, do not clone:
        rsync -a --delete --exclude node_modules --exclude .next               /opt/puffpai/dashboard/ /opt/puffpai-dashboard/
        chown -R puffpai:puffpai /opt/puffpai-dashboard
        cd /opt/puffpai-dashboard && npm ci && npm run build
      (HOME must be writable: HOME=/data/puffpai/home, as the unit sets.)

   5. The systemd units — see systemd/README.md.

   6. Acceptance check before relying on it: a few questions you know the
      answers to, voice round-trip, vault decrypt, HA control, WAN-unplugged
      test.
NEXT
