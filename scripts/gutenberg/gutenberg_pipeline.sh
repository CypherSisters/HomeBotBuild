#!/bin/bash
# Gutenberg intake: download -> sha256 -> live ZIM dir -> EPUB extract -> Calibre import -> tag.
# Idempotent-ish: rerun after a failure and finished steps are skipped by marker files.
#
# Every location is an environment setting. Required:
#   GUTENBERG_SHA256      the sha256 Kiwix publishes for the pack you downloaded
#   CALIBRE_LIBRARY       your Calibre library directory (the one holding metadata.db)
# Optional (defaults shown):
#   PUFFPAI_KNOWLEDGE_DIR /data/puffpai/knowledge
#   GUTENBERG_ZIM_NAME    gutenberg_en_all_2025-11.zim
#   PUFFPAI_VENV_PYTHON   python3
#   CALIBRE_WEB_CONTAINER (empty: Calibre-web is not stopped and started)
set -u
: "${GUTENBERG_SHA256:?set GUTENBERG_SHA256 to the published sha256 of the pack}"
: "${CALIBRE_LIBRARY:?set CALIBRE_LIBRARY to your Calibre library directory}"
KNOWLEDGE=${PUFFPAI_KNOWLEDGE_DIR:-/data/puffpai/knowledge}
ZIM_NAME=${GUTENBERG_ZIM_NAME:-gutenberg_en_all_2025-11.zim}
PY=${PUFFPAI_VENV_PYTHON:-python3}
CW=${CALIBRE_WEB_CONTAINER:-}
HERE=$(cd "$(dirname "$0")" && pwd)
LOG=$KNOWLEDGE/gutenberg_pipeline.log
INCOMING=$KNOWLEDGE/incoming/$ZIM_NAME
LIVE=$KNOWLEDGE/zim/$ZIM_NAME
SHA=$GUTENBERG_SHA256
LIB=$CALIBRE_LIBRARY
STAGE=$KNOWLEDGE/gutenberg_epub
MARK=$KNOWLEDGE/.gutenberg_pipeline
mkdir -p "$MARK"
log(){ echo "$(date +%FT%T) $*" >>"$LOG"; }
step_done(){ [ -f "$MARK/$1" ]; }
mark(){ touch "$MARK/$1"; log "step $1 done"; }

log "pipeline start pid $$"
if ! step_done download; then
  while pgrep -f "wget.*gutenberg_en_all" >/dev/null; do sleep 60; done
  [ -f "$INCOMING" ] || { log "FAIL: no incoming file"; exit 1; }
  log "download finished: $(stat -c %s "$INCOMING") bytes"
  got=$(sha256sum "$INCOMING" | cut -d' ' -f1)
  [ "$got" = "$SHA" ] || { log "FAIL: sha256 mismatch got=$got"; exit 1; }
  log "sha256 OK"
  mv "$INCOMING" "$LIVE" || { log "FAIL: move"; exit 1; }
  mark download
fi
if ! step_done extract; then
  log "extracting EPUBs to $STAGE"
  "$PY" "$HERE/extract_gutenberg_epubs.py" "$LIVE" "$STAGE" >>"$LOG" 2>&1 \
    || { log "FAIL: extract"; exit 1; }
  mark extract
fi
if ! step_done import; then
  n=$(find "$STAGE" -name '*.epub' | wc -l)
  log "importing $n EPUBs into Calibre library: $LIB"
  cp "$LIB/metadata.db" "$LIB/metadata.db.pre-gutenberg-$(date +%F)" || { log "FAIL: backup"; exit 1; }
  [ -n "$CW" ] && docker stop "$CW" >>"$LOG" 2>&1
  find "$STAGE" -name '*.epub' | sort | xargs -d '\n' -n 250 calibredb add --with-library "$LIB" >>"$LOG" 2>&1
  log "calibredb add finished (rc=$?)"
  mark import
fi
if ! step_done tag; then
  calibre-debug "$HERE/tag_gutenberg.py" -- "$LIB" >>"$LOG" 2>&1 || log "WARN: tag pass failed"
  mark tag
fi
[ -n "$CW" ] && docker start "$CW" >>"$LOG" 2>&1
log "books now: $(calibredb list --with-library "$LIB" -f id 2>/dev/null | tail -n +2 | wc -l)"
log "PIPELINE DONE"
