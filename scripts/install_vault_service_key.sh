#!/usr/bin/env bash
# Install the machine-bound vault service key. ROOT STEP -- run with sudo.
#
# This is the piece that lets the appliance read its Home Assistant
# token after a 03:00 reboot with nobody awake, WITHOUT weakening the PIN gate
# on the household's documents.
#
# What it creates:
#
#   /etc/puffpai/vault_service.key                  0600 root:root
#       64 hex characters from the kernel CSPRNG. The vault wraps its Service
#       Data Key -- which encrypts ONLY the boot secrets, never the Zone-1
#       data key -- under a KEK derived from this file.
#
#   /etc/systemd/system/puffpai-api.service.d/30-vault-service-key.conf
#       LoadCredential=, so systemd copies the key into a 0400 file in the
#       service's own tmpfs at start. puffpai-api reads it there; it never has
#       permission to open /etc/puffpai/vault_service.key itself.
#
#   /etc/puffpai/home_assistant_config.pre-vault.json   0600 root:root
#       The one rollback copy of the pre-migration config.
#       scripts/migrate_ha_token.py REFUSES to scrub without it.
#
# What this protects against: someone who walks off with the NVMe, restores
# your backup somewhere else, or reads the config as any non-root user. They
# get a wrapped key and no way to open it.
# What it does NOT protect against: root on this box. Root can read the key
# file, so root can read the boot secrets. That is not a regression -- root
# could already read the plaintext token this replaces.
#
# Usage:
#   sudo ./install_vault_service_key.sh --dry-run     # show everything, do nothing
#   sudo ./install_vault_service_key.sh               # create key + drop-in + backup
#   sudo ./install_vault_service_key.sh --backup-only  # just the rollback copy
#   sudo ./install_vault_service_key.sh --rotate      # new key (then re-run the migration)
#
# After this, the OWNER runs scripts/migrate_ha_token.py to enroll the wrap.

set -euo pipefail

KEY_FILE=/etc/puffpai/vault_service.key
CRED_NAME=puffpai-vault-service-key
DROPIN_DIR=/etc/systemd/system/puffpai-api.service.d
DROPIN=$DROPIN_DIR/30-vault-service-key.conf
HA_CONFIG=${PUFFPAI_HA_CONFIG:-/opt/puffpai/config/home_assistant_config.json}
BACKUP=/etc/puffpai/home_assistant_config.pre-vault.json

DRY_RUN=0
BACKUP_ONLY=0
ROTATE=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY_RUN=1 ;;
    --backup-only) BACKUP_ONLY=1 ;;
    --rotate)      ROTATE=1 ;;
    -h|--help)     sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n%s\n' "$*"; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  WOULD RUN: %s\n' "$*"
  else
    "$@"
  fi
}

if [ "$(id -u)" != 0 ]; then
  echo "This must run as root (sudo). It writes root-only files under /etc/puffpai." >&2
  exit 1
fi

step "Preconditions"
sd_ver=$(systemctl --version 2>/dev/null | head -1 | awk '{print $2}' || echo 0)
say "systemd version: ${sd_ver:-unknown} (LoadCredential= needs >= 247)"
if [ "${sd_ver:-0}" -lt 247 ] 2>/dev/null; then
  echo "  systemd is too old for LoadCredential=. Stopping." >&2
  exit 1
fi
say "HA config: $HA_CONFIG (exists=$( [ -f "$HA_CONFIG" ] && echo yes || echo no ))"
say "key file : $KEY_FILE (exists=$( [ -f "$KEY_FILE" ] && echo yes || echo no ))"
say "drop-in  : $DROPIN (exists=$( [ -f "$DROPIN" ] && echo yes || echo no ))"
say "backup   : $BACKUP (exists=$( [ -f "$BACKUP" ] && echo yes || echo no ))"

step "1. /etc/puffpai"
if [ -d /etc/puffpai ]; then
  say "already exists; leaving its ownership and mode alone"
else
  run install -d -o root -g root -m 0755 /etc/puffpai
fi

step "2. Rollback copy of the plaintext HA config"
if [ ! -f "$HA_CONFIG" ]; then
  say "no config at $HA_CONFIG -- nothing to back up"
elif [ -f "$BACKUP" ]; then
  say "already present; NOT overwriting (one rollback, taken before the first"
  say "migration -- overwriting it with a scrubbed config would destroy it)"
else
  run install -o root -g root -m 0600 "$HA_CONFIG" "$BACKUP"
  say "took the rollback copy (root-only, 0600)"
fi

if [ "$BACKUP_ONLY" = 1 ]; then
  step "Done (--backup-only)."
  exit 0
fi

step "3. Machine-bound service key"
if [ -f "$KEY_FILE" ] && [ "$ROTATE" = 0 ]; then
  say "already present; NOT regenerating."
  say "Regenerating would orphan the wrap already in the vault keyring and the"
  say "appliance would silently lose its unattended boot path. Use --rotate,"
  say "then re-run scripts/migrate_ha_token.py to re-enroll."
else
  if [ -f "$KEY_FILE" ]; then
    say "ROTATING. After this you MUST re-run scripts/migrate_ha_token.py or"
    say "the appliance loses its unattended boot path."
    run cp -a "$KEY_FILE" "$KEY_FILE.rotated-$(date +%Y%m%d%H%M%S)"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '  WOULD RUN: umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d " \\n" > %s\n' "$KEY_FILE"
    printf '  WOULD RUN: chown root:root %s && chmod 600 %s\n' "$KEY_FILE" "$KEY_FILE"
  else
    umask 077
    tmp=$(mktemp /etc/puffpai/.vault_service.key.XXXXXX)
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$tmp"
    chown root:root "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$KEY_FILE"
    say "wrote 64 hex chars, 0600 root:root (value not shown)"
  fi
fi

step "4. systemd drop-in (LoadCredential=)"
if [ "$DRY_RUN" = 1 ]; then
  printf '  WOULD WRITE %s containing:\n' "$DROPIN"
  printf '    [Service]\n    LoadCredential=%s:%s\n' "$CRED_NAME" "$KEY_FILE"
else
  install -d -o root -g root -m 0755 "$DROPIN_DIR"
  cat > "$DROPIN" <<EOF
# Machine-bound vault service key.
#
# systemd copies $KEY_FILE into \$CREDENTIALS_DIRECTORY as a 0400 file owned by
# the service user, for the lifetime of the process only. The service therefore
# never needs read access to the root-only file itself.
#
# Installed by scripts/install_vault_service_key.sh. If the key file is
# missing, puffpai-api will FAIL TO START -- remove this drop-in to recover.
[Service]
LoadCredential=$CRED_NAME:$KEY_FILE
EOF
  chmod 0644 "$DROPIN"
  say "wrote $DROPIN"
fi

run systemctl daemon-reload

step "Next"
say "1. Restart the backend so it picks up the credential:"
say "     sudo systemctl restart puffpai-api"
say "2. Confirm the credential arrived (prints a NAME, never the value):"
say "     sudo systemctl show puffpai-api -p LoadCredential"
say "3. The OWNER now runs the migration, with their PIN. It must run AS"
say "   puffpai (it writes the vault) but needs the root-only key, so hand"
say "   it in the same way this unit gets it -- through systemd-run:"
say ""
say "     sudo systemd-run --pty --wait --collect \\"
say "       --uid=puffpai --gid=puffpai \\"
say "       --property=LoadCredential=$CRED_NAME:$KEY_FILE \\"
say "       --setenv=PYTHONPATH=/opt/puffpai/src \\"
say "       /opt/puffpai/venv/bin/python \\"
say "       /opt/puffpai/scripts/migrate_ha_token.py --dry-run"
say ""
say "   then the same command without --dry-run, then with --scrub."
say "   (A plain 'sudo -u puffpai' run works for --status, but cannot read"
say "    the key, so it cannot enroll the wrap. The script says so.)"
say ""
say "To undo everything this script did:"
say "     sudo rm -f $DROPIN && sudo systemctl daemon-reload"
say "     sudo systemctl restart puffpai-api"
say "     sudo shred -u $KEY_FILE     # only after the vault wrap is removed"
