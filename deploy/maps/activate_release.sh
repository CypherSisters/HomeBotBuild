#!/usr/bin/env bash
# Make a READY release the live offline-maps data, with automatic rollback.
#
#   activate_release.sh [--detach] <release-id | path to releases/<id>>
#
# --detach runs it in its own session (immune to an ssh drop); read
#   $MAPS_ROOT/activate.status afterwards: OK, ROLLED_BACK, FAILED,
#   INTERRUPTED or ROLLBACK_FAILED (RUNNING while it works).
#
# 1. Refuses unless the release has READY (written only after its smoke passed).
# 2. Repoints `previous` -> the old current and `current` -> the new release,
#    each atomically (symlink to a temp name, then rename over). Links are
#    relative (releases/<id>).
# 3. Recreates puffpai-valhalla and puffpai-photon. Both mount the whole maps
#    folder and open /maps/current/... inside the container when they start,
#    so a fresh start is what makes them serve the new release.
# 4. Health-checks both, plus one drive route and one place search.
# 5. Any failure, or Ctrl+C / kill / hang-up (INT, TERM, HUP) once anything
#    has changed: points `current` back at the old release, recreates, checks
#    again. First-ever activation: removes `current` and the two containers
#    (maps off, clear state).
# 6. Re-running on the release that is already current is safe: it recreates
#    the engines on it and verifies; if that fails it falls back to `previous`.
# 7. Success: prunes releases older than the new one, keeping current and
#    previous (the last two good releases). Newer unactivated builds are kept.
# kill -9 cannot be trapped: re-run this script with the same release id
# (step 6) to bring the engines up on whatever `current` points at and verify.
set -euo pipefail
# shellcheck source=maps_common.sh
. "$(dirname "$0")/maps_common.sh"
maybe_detach activate "$@"

[ $# -eq 1 ] || { echo "usage: $0 [--detach] <release-id|releases/<id>>" >&2; exit 2; }
REL_ID="$(basename "$1")"
REL="$MAPS_ROOT/releases/$REL_ID"
[ -d "$REL" ] || die "no release $REL"
[ -f "$REL/READY" ] || die "release $REL_ID has no READY marker (build did not pass); refusing"
# A missing bind source would make Docker create an empty directory there.
[ -f "$MAPS_ROOT/bin/photon-$PHOTON_VERSION.jar" ] || die "photon jar missing under $MAPS_ROOT/bin (RUNBOOK step 3)"

take_lock
STATUS_FILE="$MAPS_ROOT/activate.status"
mkdir -p "$MAPS_ROOT/logs"
LOG="$MAPS_ROOT/logs/activate-$REL_ID-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG" 9>&-) 2>&1  # 9>&-: tee must not hold the lock
# A dead tee (ssh drop, Ctrl+C) must not kill us with SIGPIPE before the
# traps below can roll back.
trap '' PIPE
status RUNNING "$REL_ID pid $$"

export MAPS_ROOT MAPS_UID_GID VALHALLA_PORT PHOTON_PORT VALHALLA_IMAGE JRE_IMAGE
compose() { docker compose -f "$COMPOSE_FILE" "$@" 9>&-; }

link() {  # $1 link name under MAPS_ROOT, $2 target relative to MAPS_ROOT
  ln -sfn "$2" "$MAPS_ROOT/.$1.tmp"
  mv -Tf "$MAPS_ROOT/.$1.tmp" "$MAPS_ROOT/$1"
}
target_of() { if [ -L "$MAPS_ROOT/$1" ]; then readlink "$MAPS_ROOT/$1"; fi; }

# Remove the two map-engine containers in any state, including the
# "<id>_puffpai-valhalla" / "<id>_puffpai-photon" names compose leaves behind
# when a recreate is interrupted mid-rename. Matches only these two
# services; no other container is ever touched.
clear_engines() {
  local ids
  ids="$(docker ps -a --format '{{.ID}} {{.Names}}'     | awk '$2 ~ /^([0-9a-f]+_)?puffpai-(valhalla|photon)$/ {print $1}')"
  if [ -n "$ids" ]; then
    # shellcheck disable=SC2086  # one id per word
    docker rm -f $ids >/dev/null 2>&1 || true
  fi
}

recreate_and_check() {
  log "recreating engines on $(target_of current)"
  clear_engines
  if ! compose up -d --force-recreate --no-deps valhalla photon; then
    log "compose up failed; clearing the engine containers and retrying once"
    clear_engines
    compose up -d --force-recreate --no-deps valhalla photon || { log "compose up failed again"; return 1; }
  fi
  log "waiting for health"
  wait_http "http://127.0.0.1:$VALHALLA_PORT/status" 180 || { log "valhalla not healthy"; return 1; }
  wait_http "http://127.0.0.1:$PHOTON_PORT/status" 300 || { log "photon not healthy"; return 1; }
  run_checks "$VALHALLA_PORT" "$PHOTON_PORT" one
}

OLD_CURRENT="$(target_of current)"
OLD_PREVIOUS="$(target_of previous)"
NEW="releases/$REL_ID"
MODE=switch
[ "$OLD_CURRENT" = "$NEW" ] && MODE=verify
TOUCHED=0

# Put things back. Returns 0 when the maps serve a good release again.
rollback() {
  # Never interrupt a rollback halfway: further Ctrl+C / kill / hang-up wait.
  trap '' INT TERM HUP
  if [ "$MODE" = switch ] && [ -n "$OLD_CURRENT" ]; then
    link current "$OLD_CURRENT"
    if [ -n "$OLD_PREVIOUS" ]; then link previous "$OLD_PREVIOUS"; else rm -f "$MAPS_ROOT/previous"; fi
  elif [ "$MODE" = verify ] && [ -n "$OLD_PREVIOUS" ] && [ "$OLD_PREVIOUS" != "$NEW" ]; then
    link current "$OLD_PREVIOUS"
  elif [ "$MODE" = switch ]; then
    rm -f "$MAPS_ROOT/current"
    compose rm -sf valhalla photon || true
    log "first activation did not finish: maps are off, nothing is served"
    status FAILED "$REL_ID: first activation did not finish; maps off"
    return 1
  else
    log "current release $REL_ID is unhealthy and there is no other good release to fall back to"
    status FAILED "$REL_ID: current release unhealthy and there is no other good release to fall back to"
    return 1
  fi
  if recreate_and_check; then
    log "rolled back to $(target_of current); maps are serving the last good data"
    status ROLLED_BACK "$REL_ID -> $(target_of current)"
    return 0
  fi
  log "ROLLBACK ALSO FAILED: maps are down. See RUNBOOK.md 'Roll back by hand'."
  status ROLLBACK_FAILED "$REL_ID -> $(target_of current)"
  return 1
}

# shellcheck disable=SC2317,SC2329  # called through the traps below
on_signal() {
  trap '' INT TERM HUP PIPE
  exec >>"$LOG" 2>&1   # the terminal or tee may be gone (ssh drop, Ctrl+C)
  set +e
  log "interrupted by SIG$1"
  if [ "$TOUCHED" = 0 ]; then
    status INTERRUPTED "$REL_ID: stopped before changing anything"
  elif [ "$MODE" = verify ]; then
    # Nothing was switched; just make sure the engines are running on current.
    clear_engines
    compose up -d --no-deps valhalla photon
    status INTERRUPTED "$REL_ID: verify interrupted; engines started on current, not checked"
  else
    log "rolling back"
    rollback
  fi
  exit 130
}
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

if [ "$MODE" = verify ]; then
  log "$REL_ID is already current: recreating the engines on it and verifying"
  TOUCHED=1
  if recreate_and_check; then
    status OK "$REL_ID (already current, verified)"
    exit 0
  fi
  log "verify of $REL_ID FAILED; falling back"
  if rollback; then exit 1; else exit 3; fi
fi

log "activating $REL_ID (map data from $(grep -o '"data_date": "[^"]*"' "$REL/META.json" | cut -d'"' -f4)); current was ${OLD_CURRENT:-none}"
TOUCHED=1
if [ -n "$OLD_CURRENT" ]; then link previous "$OLD_CURRENT"; fi
link current "$NEW"

if recreate_and_check; then
  log "activated $REL_ID"
  status OK "$REL_ID"
  # Prune: releases older than the new one that are neither current nor previous.
  keep_prev="$(target_of previous)"
  for d in "$MAPS_ROOT"/releases/*/; do
    d="${d%/}"; id="$(basename "$d")"
    rel="releases/$id"
    if [ "$rel" != "$NEW" ] && [ "$rel" != "$keep_prev" ] && [[ "$id" < "$REL_ID" ]]; then
      log "pruning old release $id"
      rm -rf "$d"
    fi
  done
  exit 0
fi

log "activation of $REL_ID FAILED; rolling back"
if rollback; then exit 1; else exit 3; fi
