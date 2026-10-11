#!/bin/bash
# Waits for the Gutenberg pack to be verified and moved live, then builds its FTS shard.
# Pass extra library ids as arguments to force-rebuild them too (for example shards
# built before long pages were indexed in chunks).
#
# Optional settings (defaults shown):
#   PUFFPAI_KNOWLEDGE_DIR /data/puffpai/knowledge
#   GUTENBERG_LIBRARY_ID  gutenberg_en_all_2025_11
#   PUFFPAI_VENV_PYTHON   python3   (must be able to import puffpai)
set -u
KNOWLEDGE=${PUFFPAI_KNOWLEDGE_DIR:-/data/puffpai/knowledge}
LIB_ID=${GUTENBERG_LIBRARY_ID:-gutenberg_en_all_2025_11}
PY=${PUFFPAI_VENV_PYTHON:-python3}
LOG=$KNOWLEDGE/gutenberg_fts_build.log
MARK=$KNOWLEDGE/.gutenberg_pipeline
log(){ echo "$(date +%FT%T) $*" >>"$LOG"; }
log "waiting for download marker"
while [ ! -f "$MARK/download" ]; do sleep 120; done
log "build $LIB_ID"
"$PY" -m puffpai.knowledge.fts_build_cli --only "$LIB_ID" >>"$LOG" 2>&1
log "build $LIB_ID rc=$?"
for lib in "$@"; do
  log "force rebuild $lib"
  "$PY" -m puffpai.knowledge.fts_build_cli --force --only "$lib" >>"$LOG" 2>&1
  log "force rebuild $lib rc=$?"
done
log "FTS BUILDS DONE"
