#!/usr/bin/env bash
# Build a NEW offline-maps release from local files. Never touches `current`.
#
#   build_release.sh [--detach] <texas-latest.osm.pbf> <photon-dump-usa-1.0-latest.jsonl.zst>
#
# --detach runs it in its own session (immune to an ssh drop; the build takes
#   1 to 2.5 hours on a Jetson AGX Orin); read $MAPS_ROOT/build.status afterwards:
#   READY <id>, NOGO, FAILED or INTERRUPTED (RUNNING while it works).
#
# Makes $MAPS_ROOT/releases/<YYYYMMDD-HHMM>/ with:
#   valhalla/  valhalla.json + tiles.tar + admins.sqlite (routes)
#   photon/    photon_data/ (places, cut to Texas)
#   META.json  data date, build time, source sha256s, engine versions, peaks
#   READY      written last, only if every smoke check passed
# Then activate it with activate_release.sh <release>.
#
# Downloads nothing (the refresh download is a separate Bridge-gated step).
# All containers run with --network none except the smoke servers, which
# publish on loopback alternate ports only. Heavy steps run under nice/ionice
# with hard memory caps so the build can sit next to Puff's resident models.
# On any failure or Ctrl+C / kill / hang-up (INT, TERM, HUP): exit non-zero,
# remove the build and smoke containers, delete the half-built release (keep
# it with MAPS_KEEP_FAILED=1), `current` untouched. kill -9 cannot be
# trapped: the next run removes the leftover containers and any release
# folder without READY before it starts.
set -euo pipefail
# shellcheck source=maps_common.sh
. "$(dirname "$0")/maps_common.sh"
maybe_detach build "$@"

# Valhalla tile build: target peak <= 8 GB next to the models.
BUILD_MEM="${BUILD_MEM:-8g}"
BUILD_CPUS="${BUILD_CPUS:-4}"
VALHALLA_THREADS="${VALHALLA_THREADS:-$BUILD_CPUS}"
# Photon import of the Texas cut.
PHOTON_IMPORT_MEM="${PHOTON_IMPORT_MEM:-4g}"
PHOTON_IMPORT_XMX="${PHOTON_IMPORT_XMX:-3g}"
# Texas is ISO 3166-2 US-TX; every place inside a US state carries it.
PHOTON_REGION_ISO="${PHOTON_REGION_ISO:-US-TX}"
# A cut below this is broken input, not Texas. From the streamed sample the
# USA dump holds roughly 20 million places and Texas about 6% of US map
# data, so a real cut is around a million; 200,000 is a generous floor.
PHOTON_MIN_PLACES="${PHOTON_MIN_PLACES:-200000}"
PHOTON_LANGUAGES="${PHOTON_LANGUAGES:-en,es}"
# Kept for later use (offline store hours, brands for "nearest Target").
PHOTON_EXTRA_TAGS="${PHOTON_EXTRA_TAGS:-opening_hours,brand,operator,phone,website}"
# Serving caps for the smoke servers (same as the compose file).
VALHALLA_MEM_LIMIT="${VALHALLA_MEM_LIMIT:-2g}"
PHOTON_MEM_LIMIT="${PHOTON_MEM_LIMIT:-3g}"
PHOTON_XMX="${PHOTON_XMX:-2g}"
SMOKE_VALHALLA_PORT="${SMOKE_VALHALLA_PORT:-18002}"
SMOKE_PHOTON_PORT="${SMOKE_PHOTON_PORT:-12322}"
# Refuse to start unless this much RAM is free beyond the build cap.
MEM_MARGIN_MB="${MEM_MARGIN_MB:-2048}"
MIN_FREE_GB="${MIN_FREE_GB:-20}"
# Optional: a prebuilt Valhalla timezones.sqlite (valhalla_build_timezones
# downloads, so it is never run here). Without it routes work; answers just
# carry no local-time fields.
VALHALLA_TIMEZONE_DB="${VALHALLA_TIMEZONE_DB:-}"

usage() { echo "usage: $0 [--detach] <region.osm.pbf> <photon-dump.jsonl.zst|.jsonl>" >&2; exit 2; }
[ $# -eq 2 ] || usage

to_mb() {  # 8g -> 8192, 4096m -> 4096
  case "$1" in
    *g|*G) echo $(( ${1%[gG]} * 1024 )) ;;
    *m|*M) echo "${1%[mM]}" ;;
    *) die "memory size must end in g or m: $1" ;;
  esac
}

# ---- Cleanup and signal handling (set up before anything is written).
TAG_PREFIX="puffpai-maps-build-"
REL_ID=""; REL=""; WORK=""; LOG=""; TAG=""
SAMPLER=""; CHILD=""
FINAL_STATE=NOGO
cleanup() {
  local rc=$?
  trap '' INT TERM HUP PIPE
  set +e
  if [ -n "$SAMPLER" ]; then kill "$SAMPLER" 2>/dev/null; fi
  if [ -n "$CHILD" ]; then kill "$CHILD" 2>/dev/null; fi
  if [ -n "$TAG" ]; then
    docker rm -f "$TAG-valhalla" "$TAG-photon" "$TAG-smoke-valhalla" "$TAG-smoke-photon" >/dev/null 2>&1
    # A docker CLI stopped mid-create can still finish creating its
    # container: wait for it to exit, then remove once more.
    if [ -n "$CHILD" ]; then wait "$CHILD" 2>/dev/null; fi
    docker rm -f "$TAG-valhalla" "$TAG-photon" >/dev/null 2>&1
  fi
  if [ "$rc" -ne 0 ]; then
    # Ctrl+C can reach a child (docker, curl) first; it exits 129/130/143
    # and set -e ends the script before our own INT trap runs.
    case "$rc" in
      129|130|143) if [ "$FINAL_STATE" = FAILED ]; then FINAL_STATE=INTERRUPTED; LAST_ERR="${LAST_ERR:-a step was stopped by a signal}"; fi ;;
    esac
    if [ -n "$REL" ] && [ -d "$REL" ]; then
      if [ "${MAPS_KEEP_FAILED:-0}" = 1 ]; then
        rm -rf "$WORK"; touch "$REL/FAILED"
        log "build $FINAL_STATE (exit $rc); kept $REL for inspection; current untouched"
      else
        rm -rf "$REL"
        log "build $FINAL_STATE (exit $rc); removed $REL; current untouched"
      fi
    fi
    status "$FINAL_STATE" "${REL_ID:-} exit $rc ${LAST_ERR}"
  fi
  exit "$rc"
}
on_signal() {
  trap '' INT TERM HUP PIPE
  if [ -n "$LOG" ]; then exec >>"$LOG" 2>&1; fi   # the terminal or tee may be gone
  FINAL_STATE=INTERRUPTED
  LAST_ERR="interrupted by SIG$1"
  log "interrupted by SIG$1; cleaning up"
  exit 130   # runs cleanup
}

# Long host steps run in the background and are waited for, so a signal is
# handled at once instead of after the step ends.
bg_wait() {
  "$@" 9>&- &
  CHILD=$!
  wait "$CHILD"
  CHILD=""
}

# ---- GO / NO-GO. Refuse unless every check passes; nothing is written before this.
# 1. No other maps build or activation is running (lock file).
take_lock
STATUS_FILE="$MAPS_ROOT/build.status"
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP
# Leftovers of a run that was killed with kill -9 (we hold the lock, so
# nothing else can be building): its containers, and release folders that
# never got READY (folders kept on purpose with MAPS_KEEP_FAILED stay).
stale="$(docker ps -aq --filter "name=$TAG_PREFIX")"
if [ -n "$stale" ]; then
  log "removing leftover build containers: $(docker ps -a --filter "name=$TAG_PREFIX" --format '{{.Names}}' | tr '\n' ' ')"
  # shellcheck disable=SC2086  # one id per word
  docker rm -f $stale >/dev/null
fi
for d in "$MAPS_ROOT"/releases/*/; do
  [ -d "$d" ] || continue
  if [ ! -f "$d/READY" ] && [ ! -f "$d/FAILED" ]; then
    log "removing unfinished release folder $(basename "$d")"
    rm -rf "$d"
  fi
done
# 2. RAM: MemAvailable must cover the build cap plus a margin, so the build
#    fits next to Puff's resident models (unified memory: the GPU shares it).
if [ "${MAPS_SKIP_MEM_CHECK:-0}" != 1 ] && [ -r /proc/meminfo ]; then
  avail_mb=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
  need_mb=$(( $(to_mb "$BUILD_MEM") + MEM_MARGIN_MB ))
  [ "$avail_mb" -ge "$need_mb" ] \
    || die "NO-GO: only ${avail_mb} MB RAM available, build needs ${need_mb} MB (cap $BUILD_MEM + margin ${MEM_MARGIN_MB} MB); run when the models are idle"
  log "go: RAM ${avail_mb} MB available >= ${need_mb} MB"
fi
# 3. Disk: enough free space under MAPS_ROOT.
free_gb=$(df -P -BG "$MAPS_ROOT" | awk 'NR==2 {gsub("G","",$4); print $4}')
[ "$free_gb" -ge "$MIN_FREE_GB" ] || die "NO-GO: only ${free_gb} GB free under $MAPS_ROOT, need ${MIN_FREE_GB} GB"
log "go: ${free_gb} GB free under $MAPS_ROOT"
# 4. Inputs: both source files readable, zstd present, Photon jar verified.
PBF="$(readlink -f "$1")"
DUMP="$(readlink -f "$2")"
[ -r "$PBF" ] || die "NO-GO: cannot read $1"
[ -r "$DUMP" ] || die "NO-GO: cannot read $2"
case "$DUMP" in
  *.zst) command -v zstd >/dev/null || die "NO-GO: zstd is needed to read $DUMP"; DECOMP=(zstd -dc --) ;;
  *.jsonl|*.json) DECOMP=(cat --) ;;
  *) die "NO-GO: photon source must be a .jsonl.zst or .jsonl dump" ;;
esac
[ -r "$PHOTON_JAR" ] || die "NO-GO: photon jar missing: $PHOTON_JAR (RUNBOOK step 3)"
[ "$(sha256sum "$PHOTON_JAR" | cut -d' ' -f1)" = "$PHOTON_JAR_SHA256" ] \
  || die "NO-GO: photon jar sha256 does not match the 1.3.0 release"
REL_ID="${MAPS_RELEASE_ID:-$(date +%Y%m%d-%H%M)}"
[ -e "$MAPS_ROOT/releases/$REL_ID" ] && die "NO-GO: release $REL_ID already exists"
log "GO"
# ---- end of go / no-go
FINAL_STATE=FAILED

LOW=(nice -n 19)
if command -v ionice >/dev/null; then LOW+=(ionice -c 3); fi

REL="$MAPS_ROOT/releases/$REL_ID"
WORK="$REL/.work"
TAG="$TAG_PREFIX$REL_ID"
mkdir -p "$REL/valhalla" "$REL/photon/tmp" "$WORK" "$MAPS_ROOT/logs"
LOG="$MAPS_ROOT/logs/build-$REL_ID.log"
# 9>&- everywhere below: child processes must not inherit the lock, or a
# failed build would keep holding it for a moment after it exits.
exec > >(tee -a "$LOG" 9>&-) 2>&1
# A dead tee (ssh drop, Ctrl+C) must not kill us with SIGPIPE before the
# traps can clean up.
trap '' PIPE
status RUNNING "$REL_ID pid $$"

# Record the highest memory use `docker stats` sees for a container, every 2 s.
start_sampler() {  # $1 container, $2 file
  ( while sleep 2; do
      docker stats --no-stream --format '{{.MemUsage}}' "$1" 2>/dev/null | awk '{print $1}' >> "$2" || true
    done ) 9>&- &
  SAMPLER=$!
}
PEAK=0
stop_sampler() {  # sets PEAK (MB); called directly, never in $(...), so SAMPLER clears
  kill "$SAMPLER" 2>/dev/null || true; SAMPLER=""
  PEAK=$(awk '{ v=$1; u=v; gsub(/[0-9.]/,"",u); gsub(/[^0-9.]/,"",v);
         m = (u=="GiB") ? v*1024 : (u=="MiB") ? v : (u=="KiB") ? v/1024 : v/1048576;
         if (m>p) p=m } END { printf "%d", p }' "$1" 2>/dev/null || echo 0)
}

# --sig-proxy=false: signals stop this script (whose traps remove the
# containers by name), never the container directly, so a detached build
# ignores a hang-up end to end.
run_job() {  # name mem cpus [docker args...] image cmd...
  local name="$1" mem="$2" cpus="$3"; shift 3
  bg_wait docker run --rm --sig-proxy=false --name "$name" --user "$MAPS_UID_GID" --network none \
    --memory "$mem" --memory-swap "$mem" --cpus "$cpus" --cpu-shares 256 \
    -e HOME=/tmp "$@"
}

T0=$(date +%s)
log "release $REL_ID: building from $(basename "$PBF") and $(basename "$DUMP")"
# shellcheck disable=SC2016  # expanded by the inner sh
bg_wait sh -c 'sha256sum "$1" | cut -d" " -f1 > "$2"' _ "$PBF" "$WORK/pbf.sha256"
# shellcheck disable=SC2016  # expanded by the inner sh
bg_wait sh -c 'sha256sum "$1" | cut -d" " -f1 > "$2"' _ "$DUMP" "$WORK/dump.sha256"
PBF_SHA=$(cat "$WORK/pbf.sha256")
DUMP_SHA=$(cat "$WORK/dump.sha256")

# 1. Valhalla routing tiles.
if [ -n "$VALHALLA_TIMEZONE_DB" ]; then
  cp "$VALHALLA_TIMEZONE_DB" "$REL/valhalla/timezones.sqlite"
fi
log "valhalla: tiles (cap $BUILD_MEM, $VALHALLA_THREADS threads)"
start_sampler "$TAG-valhalla" "$WORK/peak-valhalla"
# shellcheck disable=SC2016  # $T and $LOW expand inside the container
run_job "$TAG-valhalla" "$BUILD_MEM" "$BUILD_CPUS" \
  -v "$PBF:/in/source.osm.pbf:ro" -v "$REL/valhalla:/data/valhalla" \
  -v "$WORK:/work" -v "$MAPS_DIR:/scripts:ro" -w /data/valhalla -e T="$VALHALLA_THREADS" \
  "$VALHALLA_IMAGE" bash -euo pipefail -c '
    LOW="nice -n 19 ionice -c 3"
    python3 -I /scripts/pbf_header.py /in/source.osm.pbf > /work/pbf_header.json
    # Relative paths: valhalla_service is always started from inside the
    # release folder it serves, so these names resolve to that release.
    valhalla_build_config \
      --mjolnir-tile-dir ./tiles \
      --mjolnir-tile-extract ./tiles.tar \
      --mjolnir-traffic-extract "" \
      --mjolnir-admin ./admins.sqlite \
      --mjolnir-timezone ./timezones.sqlite \
      --mjolnir-concurrency "$T" \
      --mjolnir-max-cache-size 268435456 \
      --additional-data-elevation "" > /data/valhalla/valhalla.json
    $LOW valhalla_build_admins -c valhalla.json /in/source.osm.pbf
    $LOW valhalla_build_tiles -c valhalla.json /in/source.osm.pbf
    $LOW valhalla_build_extract -c valhalla.json -v
    rm -rf tiles'
stop_sampler "$WORK/peak-valhalla"; PEAK_VALHALLA=$PEAK
[ -s "$REL/valhalla/tiles.tar" ] || die "valhalla produced no tiles.tar"
DATA_TIMESTAMP=$(grep -o '"replication_timestamp": "[^"]*"' "$WORK/pbf_header.json" | cut -d'"' -f4 || true)
[ -n "$DATA_TIMESTAMP" ] || die "no replication timestamp in the .pbf header; cannot date the map data"
# What the screen and prints show: "Map data from YYYY-MM-DD".
DATA_DATE="${DATA_TIMESTAMP%%T*}"
log "valhalla: done, map data from $DATA_DATE ($DATA_TIMESTAMP), peak ${PEAK_VALHALLA} MB"

# 2. Photon places, cut to the region, then imported.
log "photon: cutting $(basename "$DUMP") to $PHOTON_REGION_ISO"
# shellcheck disable=SC2016  # expanded by the inner sh
bg_wait bash -o pipefail -c '"${@:4}" "$1" | grep -F -e "\"type\":\"NominatimDumpFile\"" -e "\"type\":\"CountryInfo\"" -e "\"$2\"" > "$3"' \
  _ "$DUMP" "$PHOTON_REGION_ISO" "$WORK/region.jsonl" "${LOW[@]}" "${DECOMP[@]}"
head -n1 "$WORK/region.jsonl" | grep -q '"type":"NominatimDumpFile"' || die "dump has no Photon header line"
PLACES=$(grep -c '"type":"Place"' "$WORK/region.jsonl" || true)
[ "$PLACES" -ge "$PHOTON_MIN_PLACES" ] \
  || die "only $PLACES places tagged $PHOTON_REGION_ISO in the dump (minimum $PHOTON_MIN_PLACES); wrong or truncated file?"
PHOTON_DATA_DATE=$(head -n1 "$WORK/region.jsonl" | grep -o '"data_timestamp":"[^"]*"' | cut -d'"' -f4 || true)
log "photon: $PLACES places, dump dated ${PHOTON_DATA_DATE:-unknown}; importing (cap $PHOTON_IMPORT_MEM)"
start_sampler "$TAG-photon" "$WORK/peak-photon"
run_job "$TAG-photon" "$PHOTON_IMPORT_MEM" "$BUILD_CPUS" \
  -v "$PHOTON_JAR:/opt/photon/photon.jar:ro" -v "$WORK/region.jsonl:/in/region.jsonl:ro" \
  -v "$REL/photon:/photon" \
  "$JRE_IMAGE" nice -n 19 ionice -c 3 java "-Xmx$PHOTON_IMPORT_XMX" -XX:+ExitOnOutOfMemoryError \
    -Djava.io.tmpdir=/photon/tmp -jar /opt/photon/photon.jar import \
    -data-dir /photon -import-file /in/region.jsonl \
    -languages "$PHOTON_LANGUAGES" -extra-tags "$PHOTON_EXTRA_TAGS" -j "$BUILD_CPUS"
stop_sampler "$WORK/peak-photon"; PEAK_PHOTON_IMPORT=$PEAK
[ -d "$REL/photon/photon_data" ] || die "photon import produced no photon_data"
log "photon: import done, peak ${PEAK_PHOTON_IMPORT} MB"

# 3. Smoke checks against the NEW data, on throwaway loopback servers.
log "smoke: starting throwaway servers on 127.0.0.1:$SMOKE_VALHALLA_PORT and :$SMOKE_PHOTON_PORT"
docker run -d --init --name "$TAG-smoke-valhalla" --user "$MAPS_UID_GID" \
  --memory "$VALHALLA_MEM_LIMIT" --memory-swap "$VALHALLA_MEM_LIMIT" --cpus 2 \
  -p "127.0.0.1:$SMOKE_VALHALLA_PORT:8002" -v "$REL/valhalla:/data/valhalla:ro" -w /data/valhalla \
  "$VALHALLA_IMAGE" valhalla_service valhalla.json 2 >/dev/null
docker run -d --name "$TAG-smoke-photon" --user "$MAPS_UID_GID" \
  --memory "$PHOTON_MEM_LIMIT" --memory-swap "$PHOTON_MEM_LIMIT" --cpus 2 \
  -p "127.0.0.1:$SMOKE_PHOTON_PORT:2322" \
  -v "$PHOTON_JAR:/opt/photon/photon.jar:ro" -v "$REL/photon:/photon" \
  "$JRE_IMAGE" java "-Xmx$PHOTON_XMX" -XX:MaxDirectMemorySize=256m -XX:+ExitOnOutOfMemoryError \
    -Djava.io.tmpdir=/photon/tmp -jar /opt/photon/photon.jar serve \
    -data-dir /photon -listen-ip 0.0.0.0 -listen-port 2322 >/dev/null
wait_http "http://127.0.0.1:$SMOKE_VALHALLA_PORT/status" 180 || die "smoke valhalla never became healthy"
wait_http "http://127.0.0.1:$SMOKE_PHOTON_PORT/status" 300 || die "smoke photon never became healthy"
run_checks "$SMOKE_VALHALLA_PORT" "$SMOKE_PHOTON_PORT" all || die "smoke checks failed"
SERVE_VALHALLA=$(docker stats --no-stream --format '{{.MemUsage}}' "$TAG-smoke-valhalla" | awk '{print $1}')
SERVE_PHOTON=$(docker stats --no-stream --format '{{.MemUsage}}' "$TAG-smoke-photon" | awk '{print $1}')
# Clean stop: the smoke Photon serves from this release's own index.
docker stop -t 30 "$TAG-smoke-valhalla" "$TAG-smoke-photon" >/dev/null
docker rm "$TAG-smoke-valhalla" "$TAG-smoke-photon" >/dev/null

# 4. META.json, then READY.
SECS=$(( $(date +%s) - T0 ))
cat > "$REL/META.json" <<EOF
{
  "release": "$REL_ID",
  "data_date": "$DATA_DATE",
  "data_timestamp": "$DATA_TIMESTAMP",
  "data_date_source": "osm.pbf header osmosis_replication_timestamp",
  "places_data_date": "${PHOTON_DATA_DATE}",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "build_seconds": $SECS,
  "source_pbf": {"file": "$(basename "$PBF")", "sha256": "$PBF_SHA", "bytes": $(stat -c %s "$PBF")},
  "source_places": {"file": "$(basename "$DUMP")", "sha256": "$DUMP_SHA", "bytes": $(stat -c %s "$DUMP"), "region": "$PHOTON_REGION_ISO", "places": $PLACES},
  "engines": {
    "valhalla": "$VALHALLA_VERSION", "valhalla_image": "$VALHALLA_IMAGE",
    "photon": "$PHOTON_VERSION", "photon_jar_sha256": "$PHOTON_JAR_SHA256", "jre_image": "$JRE_IMAGE"
  },
  "timezones_db": $( [ -n "$VALHALLA_TIMEZONE_DB" ] && echo true || echo false ),
  "sizes_mb": {"valhalla": $(du -sm "$REL/valhalla" | cut -f1), "photon": $(du -sm "$REL/photon" | cut -f1)},
  "peak_mem_mb": {"valhalla_build": $PEAK_VALHALLA, "photon_import": $PEAK_PHOTON_IMPORT},
  "smoke_serving_mem": {"valhalla": "$SERVE_VALHALLA", "photon": "$SERVE_PHOTON"}
}
EOF
rm -rf "$WORK"
echo "$REL_ID" > "$REL/READY"
status READY "$REL_ID"
log "release $REL_ID READY in ${SECS}s ($REL). Activate with: activate_release.sh $REL_ID"
