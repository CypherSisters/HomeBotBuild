# shellcheck shell=bash disable=SC2034  # values are used by the sourcing scripts
# Shared settings and helpers for build_release.sh and activate_release.sh.
# Sourced, never run. Every value can be overridden from the environment,
# which is how the tiny-data smoke on a laptop points at a scratch folder.

MAPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Where releases live: $PUFFPAI_MAPS_ROOT, else "maps" under the install's
# data dir ($PUFFPAI_DATA_DIR, else install.sh's default). The API reads the
# live build from the same place. Put it on a disk with room (RUNBOOK).
PUFFPAI_DATA_DIR="${PUFFPAI_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/puffpai}"
MAPS_ROOT="${MAPS_ROOT:-${PUFFPAI_MAPS_ROOT:-$PUFFPAI_DATA_DIR/maps}}"

# Pinned engines, by tag AND digest. The default is the multi-arch index
# digest, which pulls the right image on linux/arm64 and linux/amd64. After an
# offline `docker save --platform <arch>` + `docker load` only that arch's
# manifest digest resolves, so set VALHALLA_IMAGE / JRE_IMAGE to it:
#   linux/arm64  valhalla  ghcr.io/valhalla/valhalla:3.9.1@sha256:abd5743085e7ba902cbce443e788ac76830252ae63f8cff2f6f7c63c31b912e4
#   linux/arm64  jre       eclipse-temurin:21.0.12.1_1-jre-noble@sha256:ad32e01f3e9051e9f69ee3f41bc4826b580569a9f738368bb64b6d2a148824ff
#   linux/amd64  read it from the index: docker buildx imagetools inspect <image:tag>
VALHALLA_VERSION="3.9.1"
VALHALLA_IMAGE="${VALHALLA_IMAGE:-ghcr.io/valhalla/valhalla:3.9.1@sha256:4fd114eb1d26b8fc2f43bcb3df234c1b63a0bf13c277405e5943d5361dc575de}"
JRE_IMAGE="${JRE_IMAGE:-eclipse-temurin:21.0.12.1_1-jre-noble@sha256:000fd431958bc81a24abe1e8e5f0f0fd3ae365a594bd50aadb20696805f9408c}"
PHOTON_VERSION="1.3.0"
# sha256 published on the GitHub release asset photon-1.3.0.jar.
PHOTON_JAR_SHA256="a89707c0045e4807b2a1180e132e68e108d998709f48b6c94b98a6e281f571a5"
PHOTON_JAR="${PHOTON_JAR:-$MAPS_ROOT/bin/photon-$PHOTON_VERSION.jar}"

# Containers run as the user running these scripts, so every file they write
# is owned by the install's user.
MAPS_UID_GID="${MAPS_UID_GID:-$(id -u):$(id -g)}"

# Serving ports (loopback only) used by the compose file and activation checks.
VALHALLA_PORT="${VALHALLA_PORT:-8002}"
PHOTON_PORT="${PHOTON_PORT:-2322}"

COMPOSE_FILE="${COMPOSE_FILE:-$MAPS_DIR/docker-compose.maps.yml}"

# Smoke landmarks: two public places, never anyone's home. These match the
# Texas example region; set all the SMOKE_* values for another region.
# Texas State Capitol (Austin) -> downtown Waco, "lat,lon".
SMOKE_FROM="${SMOKE_FROM:-30.2747,-97.7404}"
SMOKE_TO="${SMOKE_TO:-31.5493,-97.1467}"
SMOKE_COSTINGS="${SMOKE_COSTINGS:-auto pedestrian bicycle}"
# Pipe-separated place searches; each must come back with a matching name.
SMOKE_SEARCHES="${SMOKE_SEARCHES:-Alamo|Texas State Capitol}"

# Never fails: after an ssh drop or Ctrl+C the terminal or tee may be gone.
log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2 || true; }
LAST_ERR=""
die() { LAST_ERR="$*"; log "FAIL: $*"; exit 1; }

# One-line status file the deployer reads after a detached run:
#   <STATE> <UTC time> <details>   (STATE: RUNNING, READY, OK, ROLLED_BACK,
#   NOGO, FAILED, INTERRUPTED, ROLLBACK_FAILED). Written atomically.
STATUS_FILE=""
status() {
  if [ -n "$STATUS_FILE" ]; then
    printf '%s %s %s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${2:-}" > "$STATUS_FILE.tmp"
    mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
  fi
}

# `<script> --detach <args>`: re-run the script in its own session (setsid +
# nohup), so a dropped ssh connection or a closed terminal cannot interrupt
# it. Prints where the log and the status file are, then returns at once.
maybe_detach() {  # $1 status name (build|activate), then the script's args
  local name="$1"; shift
  if [ "${1:-}" = "--detach" ]; then
    shift
    mkdir -p "$MAPS_ROOT/logs"
    local out pid
    out="$MAPS_ROOT/logs/$name-$(date +%Y%m%d-%H%M%S)-$$.out"
    setsid nohup bash "$(readlink -f "$0")" "$@" > "$out" 2>&1 < /dev/null &
    pid=$!
    # Replace the last run's final state, so nobody mistakes it for this
    # run's result; only when no other maps job holds the lock.
    if ( flock -n 8 ) 8>"$MAPS_ROOT/.maps.lock"; then
      STATUS_FILE="$MAPS_ROOT/$name.status" status STARTING "pid $pid"
    fi
    echo "started in the background (pid $pid)"
    echo "  log:    $out"
    echo "  status: $MAPS_ROOT/$name.status   (cat it; RUNNING until done)"
    exit 0
  fi
}

# Wait until an HTTP URL answers 2xx, or give up after $2 seconds.
wait_http() {
  local url="$1" limit="$2" waited=0
  until curl -fs -o /dev/null --max-time 5 "$url"; do
    waited=$((waited + 3))
    if [ "$waited" -ge "$limit" ]; then
      return 1
    fi
    sleep 3
  done
}

# One route through Valhalla. $1 port, $2 costing. Passes only when Valhalla
# found a route and returned turn-by-turn maneuvers.
check_route() {
  local port="$1" costing="$2" from_lat from_lon to_lat to_lon body out
  from_lat="${SMOKE_FROM%,*}"; from_lon="${SMOKE_FROM#*,}"
  to_lat="${SMOKE_TO%,*}"; to_lon="${SMOKE_TO#*,}"
  body="{\"locations\":[{\"lat\":$from_lat,\"lon\":$from_lon},{\"lat\":$to_lat,\"lon\":$to_lon}],\"costing\":\"$costing\",\"units\":\"miles\"}"
  out="$(curl -fsS --max-time 60 -H 'Content-Type: application/json' \
    --data "$body" "http://127.0.0.1:$port/route")" || { log "route $costing: request failed"; return 1; }
  if printf '%s' "$out" | grep -q '"status_message":"Found route' \
     && printf '%s' "$out" | grep -q '"maneuvers":\[{'; then
    log "route $costing: ok ($(printf '%s' "$out" | grep -o '"summary":{[^}]*}' | tail -n1 | grep -o '"length":[0-9.]*' | head -n1) mi)"
    return 0
  fi
  log "route $costing: no route in answer: $(printf '%s' "$out" | head -c 300)"
  return 1
}

# One place search through Photon. $1 port, $2 query. Passes only when a
# result name contains the query (case-insensitive).
check_search() {
  local port="$1" query="$2" out q
  q="$(printf '%s' "$query" | sed 's/ /%20/g')"
  out="$(curl -fsS --max-time 30 "http://127.0.0.1:$port/api?q=$q&limit=5")" \
    || { log "search '$query': request failed"; return 1; }
  if printf '%s' "$out" | grep -qi "\"name\":\"[^\"]*$query"; then
    log "search '$query': ok"
    return 0
  fi
  log "search '$query': no matching name: $(printf '%s' "$out" | head -c 300)"
  return 1
}

# Run all smoke checks against Valhalla on $1 and Photon on $2.
# $3 = "all" (every costing and search) or "one" (one drive, one search).
run_checks() {
  local vport="$1" pport="$2" scope="$3" c s rc=0
  local costings="$SMOKE_COSTINGS" searches="$SMOKE_SEARCHES"
  if [ "$scope" = "one" ]; then
    costings="${costings%% *}"; searches="${searches%%|*}"
  fi
  for c in $costings; do
    check_route "$vport" "$c" || rc=1
  done
  local IFS='|'
  for s in $searches; do
    check_search "$pport" "$s" || rc=1
  done
  return "$rc"
}

# Take a lock so a build and an activation never overlap.
take_lock() {
  mkdir -p "$MAPS_ROOT"
  exec 9>"$MAPS_ROOT/.maps.lock"
  # Wait a little: after a kill -9, short-lived children (sleep, curl) may
  # hold the lock for a few seconds.
  flock -w "${MAPS_LOCK_WAIT:-20}" 9 || die "another maps build or activation is running ($MAPS_ROOT/.maps.lock)"
}
