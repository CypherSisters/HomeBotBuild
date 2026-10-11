#!/usr/bin/env bash
# Map data refresh. Runs as the install's user.
#
#   refresh.sh run       the nightly job (cron). Does nothing unless an update
#                        is due (about every 28 days; a failed run retries
#                        on the next nights, at most 5 in a row), and nothing
#                        while the "Map data updates" Bridge connection is off.
#   refresh.sh run-now   start one update now, in the background. Refuses
#                        (exit 3) while "Map data updates" is off.
#   refresh.sh status    print the last result as JSON.
#
# Every download goes through the "Map data updates" connection
# (security/egress_doors.py, door map_data_updates): two hosts only,
# download.geofabrik.de and download1.graphhopper.com. The build and the
# swap are build_release.sh and activate_release.sh, unchanged, so a failure
# anywhere leaves the live maps as they were.
#
# Status: $PUFFPAI_MAPS_ROOT/refresh.status (JSON). Logs:
# $PUFFPAI_MAPS_ROOT/logs/refresh-*.log (the job) and build-*.log /
# activate-*.log (the scripts).
set -euo pipefail

# The checkout this script sits in (deploy/maps/ -> the repo root).
PUFFPAI_HOME="${PUFFPAI_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
# The same data dir as the API service, so the job reads the SAME switch
# (the owner overlay under it) and writes the SAME Bridge ledger. Cron passes
# it on the crontab line; the fallback is install.sh's default.
export PUFFPAI_DATA_DIR="${PUFFPAI_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/puffpai}"
export PUFFPAI_MAPS_ROOT="${PUFFPAI_MAPS_ROOT:-$PUFFPAI_DATA_DIR/maps}"
export PUFFPAI_MAPS_DEPLOY_DIR="${PUFFPAI_MAPS_DEPLOY_DIR:-$PUFFPAI_HOME/deploy/maps}"
export PYTHONPATH="$PUFFPAI_HOME/src"
# Installed, the job always reads the API service's real env files (the
# appliance file and the per-user install's file in the data dir; none
# readable = the connection is treated as forced shut). The override is for
# dev checkouts and tests only, so it never survives into this wrapper.
unset PUFFPAI_MAPS_SERVICE_ENV_FILE
export PYTHONUNBUFFERED=1
# cron's PATH is minimal; the build needs docker, zstd, flock, curl.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

cd "$PUFFPAI_HOME"
exec "$PUFFPAI_HOME/venv/bin/python" -m puffpai.maps_refresh "$@"
