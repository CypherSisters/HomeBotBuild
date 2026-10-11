# Gutenberg intake

Kiwix publishes Project Gutenberg as a ZIM (`gutenberg_en_all_YYYY-MM.zim`, about 206 GB).
PuffPAI reads it with its own `zim_reader` and indexes it with its own FTS5 shards,
with no separate ZIM server and no libzim. The pack also carries a full EPUB per book, which these
tools can lift into your Calibre library so the books are browsable and readable
offline in Calibre-web.

- `gutenberg_pipeline.sh`: waits for the download, checks the published sha256, moves the
  pack into the live ZIM directory, extracts the EPUBs, backs up `metadata.db`,
  runs `calibredb add` in batches and tags the books "Project Gutenberg". Marker files
  under `$PUFFPAI_KNOWLEDGE_DIR/.gutenberg_pipeline/` make reruns skip finished steps.
  Set `GUTENBERG_SHA256` and `CALIBRE_LIBRARY` first; the script header lists the
  optional settings.
- `extract_gutenberg_epubs.py`: EPUB extractor on PuffPAI's own reader
  (`<out>/<id//1000>/<id>.epub` plus `manifest.tsv`), resumable.
- `tag_gutenberg.py`: run with `calibre-debug`, adds the umbrella tag by identifier.
- `gutenberg_fts_build.sh`: after the pack is live, builds its search shard. Extra
  library ids given as arguments are force-rebuilt as well.

Calibre's `calibredb` and `calibre-debug` must be on `PATH`.
