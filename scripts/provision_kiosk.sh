#!/usr/bin/env bash
#
# PuffPAI — kiosk mode provisioning (wall-display appliance face)
#
# Replaces the full GNOME desktop with a minimal auto-login kiosk session:
# openbox + Chromium full-screen on the PuffPAI dashboard, scheduled night
# blanking, browser auto-restart. Motivation and design: docs/KIOSK_MODE.md
# (short version: a browser left open under GNOME can pin the GPU at 99% and
# trip over-current throttling).
#
# Run it MANUALLY, and read it first. It is idempotent — re-running is safe
# and is the intended way to pick up changes.
#
#     sudo ./scripts/provision_kiosk.sh            # install / update
#     sudo ./scripts/provision_kiosk.sh --revert   # back to normal GNOME login
#
# Takes effect on the next GDM start:  sudo systemctl restart gdm3
# (kills any desktop session currently on the monitor) — or just reboot.
#
# What it deliberately does NOT do:
#   * touch the PuffPAI services, Ollama, or the firewall
#   * uninstall GNOME — the full desktop stays available (see KIOSK_MODE.md
#     "Getting a desktop back")
#   * remove packages or files on --revert — it only turns auto-login off
#     and points the session back at Ubuntu

set -euo pipefail

SVC_USER="${PUFFPAI_USER:-puffpai}"
KIOSK_DIR=/opt/puffpai-kiosk
XSESSIONS_DIR=/usr/share/xsessions
GDM_CONF=/etc/gdm3/custom.conf
ACCOUNTS_FILE="/var/lib/AccountsService/users/${SVC_USER}"
ETC_DIR=/etc/puffpai
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "This script must run as root:  sudo $0" >&2
    exit 1
fi

# file section key value — set key=value inside [section], preserving the
# rest of the file. Only matches uncommented keys, so distro comment blocks
# are left alone.
ensure_ini_key() {
    local file=$1 section=$2 key=$3 value=$4
    [ -f "$file" ] || touch "$file"
    [ -f "${file}.pre-kiosk" ] || cp "$file" "${file}.pre-kiosk"
    grep -q "^\[$section\]" "$file" || printf '\n[%s]\n' "$section" >> "$file"
    if grep -Eq "^[[:space:]]*$key[[:space:]]*=" "$file"; then
        sed -i -E "s|^[[:space:]]*$key[[:space:]]*=.*|$key=$value|" "$file"
    else
        sed -i "/^\[$section\]/a $key=$value" "$file"
    fi
}

if [ "${1:-}" = "--revert" ]; then
    log "Reverting to the normal GNOME login"
    ensure_ini_key "$GDM_CONF" daemon AutomaticLoginEnable false
    ensure_ini_key "$ACCOUNTS_FILE" User XSession ubuntu
    ensure_ini_key "$ACCOUNTS_FILE" User Session ubuntu
    info "Done. Apply with:  sudo systemctl restart gdm3   (or reboot)"
    info "Kiosk files in $KIOSK_DIR and $XSESSIONS_DIR are left installed."
    exit 0
fi

# ── 1. Sanity ───────────────────────────────────────────────────────────────
log "Sanity checks"
id "$SVC_USER" >/dev/null 2>&1 || { echo "user $SVC_USER missing" >&2; exit 1; }
[ -d "$XSESSIONS_DIR" ] || { echo "$XSESSIONS_DIR missing — is GDM/X installed?" >&2; exit 1; }
if ! command -v chromium >/dev/null && [ ! -x /snap/bin/chromium ]; then
    info "WARNING: chromium not found — install it first (snap install chromium)"
fi

# ── 2. Packages ─────────────────────────────────────────────────────────────
log "Installing packages (openbox, unclutter)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq openbox unclutter

# ── 3. Kiosk files ──────────────────────────────────────────────────────────
log "Installing kiosk session"
install -d "$KIOSK_DIR"
install -m 0755 "$REPO_ROOT/kiosk/kiosk-session.sh" "$KIOSK_DIR/kiosk-session.sh"
install -m 0644 "$REPO_ROOT/kiosk/puffpai-kiosk.desktop" "$XSESSIONS_DIR/puffpai-kiosk.desktop"
install -d "$ETC_DIR"
if [ ! -f "$ETC_DIR/kiosk.env" ]; then
    install -m 0644 "$REPO_ROOT/kiosk/kiosk.env.example" "$ETC_DIR/kiosk.env"
    info "wrote $ETC_DIR/kiosk.env (defaults; edit to taste)"
else
    info "$ETC_DIR/kiosk.env exists — leaving it alone"
fi

# CRLF from a Windows checkout would break the shebang; normalize in place.
sed -i 's/\r$//' "$KIOSK_DIR/kiosk-session.sh" "$XSESSIONS_DIR/puffpai-kiosk.desktop"

bash -n "$KIOSK_DIR/kiosk-session.sh"
info "session script parses clean"

# ── 4. GDM auto-login into the kiosk session ────────────────────────────────
log "Configuring GDM auto-login ($SVC_USER -> PuffPAI Kiosk)"
ensure_ini_key "$GDM_CONF" daemon AutomaticLoginEnable true
ensure_ini_key "$GDM_CONF" daemon AutomaticLogin "$SVC_USER"
ensure_ini_key "$GDM_CONF" daemon WaylandEnable false
ensure_ini_key "$ACCOUNTS_FILE" User XSession puffpai-kiosk
ensure_ini_key "$ACCOUNTS_FILE" User Session puffpai-kiosk
ensure_ini_key "$ACCOUNTS_FILE" User SystemAccount false

log "Done"
info "Apply with:  sudo systemctl restart gdm3   (kills the current desktop)"
info "or reboot. Revert any time:  sudo $0 --revert"
