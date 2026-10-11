#!/usr/bin/env python3
"""
PuffPAI Knowledge Base Re-Indexer
==================================
Rebuilds ChromaDB collections from ZIM files on disk.

Uses the modern puffpai.knowledge package:
  - DualZoneRAG for ChromaDB client access
  - KnowledgeIngestion for ZIM -> embedding pipeline
  - discover_from_config() for ZIM file discovery

Run from the project root:
    python scripts/reindex.py

Options:
    --force     Re-index even libraries that already look complete
    --library   Index one specific library by name, e.g.:
                  python scripts/reindex.py --library Wikipedia
    --list      List all discovered ZIM files and exit
    --status    Show chunk counts for all indexed collections and exit
    --reembed   Re-embed the user-content collections in place (see below)

SCOPE: read before switching the embedding model
------------------------------------------------
The ZIM path above only rebuilds collections listed in the manifest
(indexed_libraries.json). Three collections hold user content with no ZIM
behind them and are NOT covered by it:

    user_uploads, puffpai_vault, puffpai_knowledge

Re-running the ZIM re-index would leave those three in the OLD vector space.
After an embedder switch that means writes fail on a dimension mismatch
while reads silently search stale vectors. `--reembed` handles them: their text is already stored
in ChromaDB, so each is rebuilt in place from its own documents using the
currently configured embedding function.

A full re-index after an embedder switch is therefore BOTH:

    python scripts/reindex.py --force            # ZIM libraries
    python scripts/reindex.py --reembed --yes    # user content

--reembed is destructive, so it refuses to run without --yes.

How --reembed avoids losing data
--------------------------------
Each collection is rebuilt into a staging collection named
`<name>__reembed_tmp`, whose count is verified against the source BEFORE the
original is touched. Only then is the original deleted and the staging
collection renamed into its place. Consequences of a failure at each step:

    during the copy / count mismatch -> original UNTOUCHED, partial copy left
                                        in <name>__reembed_tmp (safe to drop)
    after delete, before rename      -> all data is in <name>__reembed_tmp;
                                        rename it manually to recover

There is no path that leaves the primary collection silently truncated, and a
stale staging collection from an earlier aborted run is dropped on the next
attempt. Any collection that does not end up with a matching count is
reported as PARTIAL/FAILED and the process exits non-zero. Back up the
ChromaDB directory anyway.

Memory: the copy is paged (limit/offset), so peak memory is one batch, not a
whole collection. The three collections involved hold user content and are
small next to the ZIM chunks, but paging keeps it bounded regardless.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import sys
import os
import json
import time
import argparse
import logging

# Make src/ importable regardless of CWD
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from puffpai.knowledge.rag import DualZoneRAG
from puffpai.knowledge.ingestion import KnowledgeIngestion
from puffpai.knowledge.discovery import discover_from_config, get_zim_metadata

LIBRARIES_PATH = os.path.join(ROOT, 'src', 'puffpai', 'indexed_libraries.json')

# A collection with this many or more chunks is considered "complete" and will
# be skipped unless --force is given.
SKIP_THRESHOLD = 300

# Articles to index per library
ARTICLES_TO_INDEX = 500

logger = logging.getLogger("reindex")


def count_chunks(client, collection_name: str) -> int:
    """Return the number of documents in a collection, or 0 on error."""
    try:
        coll = client.get_collection(name=collection_name)
        return coll.count()
    except Exception:
        return 0


def load_libraries() -> list:
    """Load the indexed libraries manifest."""
    with open(LIBRARIES_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def match_zim_to_library(zim_path: str, libraries: list) -> dict | None:
    """Try to match a ZIM file to an indexed_libraries.json entry.

    Matches by checking if the library_id (normalized) appears in the
    ZIM filename (also normalized).

    Args:
        zim_path: Path to a ZIM file.
        libraries: List of library dicts from indexed_libraries.json.

    Returns:
        The matching library dict, or None if no match found.
    """
    basename = os.path.basename(zim_path).lower()
    # Strip extension(s): e.g., "file.zim" or "file.zim.part"
    name_part = basename.split('.zim')[0] if '.zim' in basename else basename
    # Normalize: hyphens and underscores are equivalent
    name_norm = name_part.replace('-', '_').replace(' ', '_')

    for lib in libraries:
        lib_id_norm = lib['library_id'].lower().replace('-', '_')
        if lib_id_norm == name_norm or lib_id_norm in name_norm:
            return lib
    return None


def cmd_list():
    """List all discovered ZIM files."""
    zim_paths = discover_from_config()
    if not zim_paths:
        print("No ZIM files found. Check paths.zim_dirs in puffpai_config.json")
        return

    libraries = load_libraries()
    print(f"Discovered {len(zim_paths)} ZIM file(s):\n")
    for path in zim_paths:
        try:
            meta = get_zim_metadata(path)
            size_mb = meta['file_size_bytes'] / (1024 * 1024)
            matched = match_zim_to_library(path, libraries)
            status = f"-> {matched['collection']}" if matched else "(no collection match)"
            print(f"  {meta['filename']}")
            print(f"    Entries: {meta['entry_count']:,}  Size: {size_mb:.1f} MB")
            print(f"    Match: {status}")
        except Exception as e:
            print(f"  {os.path.basename(path)}  (error: {e})")
        print()


def cmd_status():
    """Show chunk counts for all indexed collections."""
    libraries = load_libraries()
    rag = DualZoneRAG()
    client = rag.client

    print(f"\n  {'Collection':<55} {'Chunks':>8}  Status")
    print("  " + "-" * 78)

    healthy = broken = empty = 0
    for lib in libraries:
        name = lib['collection']
        chunks = count_chunks(client, name)
        if chunks >= SKIP_THRESHOLD:
            status = "healthy"
            healthy += 1
        elif chunks > 0:
            status = "partial"
            broken += 1
        else:
            status = "empty"
            empty += 1
        # Truncate long names
        display_name = name[:52] + "..." if len(name) > 55 else name
        print(f"  {display_name:<55} {chunks:>6}  {status}")

    print("  " + "-" * 78)
    print(f"  Total: {len(libraries)} | Healthy: {healthy} "
          f"| Partial: {broken} | Empty: {empty}\n")


# Collections holding user content rather than ZIM libraries. The manifest
# does not list them and there is no source file to re-ingest, so the ZIM
# path cannot rebuild them — but their text lives in ChromaDB already.
NON_ZIM_COLLECTIONS = ("user_uploads", "puffpai_vault", "puffpai_knowledge")


STAGING_SUFFIX = "__reembed_tmp"


def reembed_collection(client, name: str, batch_size: int = 200) -> tuple:
    """Rebuild one collection with the configured embedding function.

    Returns (status, rebuilt, total) where status is 'ok', 'missing' or
    'empty'. Raises RuntimeError on any failure, and the message says where
    the data is: either the original is untouched, or it is all in the
    staging collection. See the module docstring for the full state table.
    """
    from chromadb.errors import NotFoundError
    from puffpai.embeddings import embed_documents, get_or_create_collection

    try:
        src = client.get_collection(name=name)
    except NotFoundError:
        return ('missing', 0, 0)

    total = src.count()
    if not total:
        return ('empty', 0, 0)

    # Preserve the distance space: the relevance math in _get_rag_context
    # branches on it, so silently switching l2 -> cosine would skew scoring.
    space = (getattr(src, 'metadata', None) or {}).get('hnsw:space', 'l2')

    # Drop a stale staging collection left by an earlier aborted run.
    staging_name = name + STAGING_SUFFIX
    try:
        client.delete_collection(name=staging_name)
    except Exception:
        pass
    staging = get_or_create_collection(
        client, staging_name, metadata={'hnsw:space': space})

    # Paged copy — peak memory is one batch, not the whole collection. The
    # source is not modified until the swap below, so paging is stable.
    offset = 0
    while offset < total:
        page = src.get(limit=batch_size, offset=offset,
                       include=['documents', 'metadatas'])
        ids = page.get('ids') or []
        if not ids:
            break
        docs = page.get('documents') or []
        metas = page.get('metadatas') or []
        staging.add(
            ids=ids,
            documents=docs,
            metadatas=[m if m else {} for m in metas] if metas else None,
            embeddings=embed_documents(docs),
        )
        offset += len(ids)

    staged = staging.count()
    if staged != total:
        raise RuntimeError(
            f"staged {staged} of {total} chunks — ORIGINAL '{name}' IS INTACT. "
            f"The partial copy is in '{staging_name}' and is safe to delete.")

    # Swap. Everything above this line is non-destructive.
    client.delete_collection(name=name)
    try:
        staging.modify(name=name)
    except Exception as exc:
        raise RuntimeError(
            f"rebuilt {staged} chunks but could not rename '{staging_name}' "
            f"to '{name}': {exc}. THE DATA IS SAFE IN '{staging_name}' — "
            f"rename it manually to recover.") from exc

    return ('ok', staged, total)


def cmd_reembed(args):
    """Re-embed the user-content collections. Returns a process exit code."""
    from puffpai.config import get_config
    cfg = get_config()
    backend = cfg.models.embedding_backend
    model = (cfg.models.ollama_embedding_model if backend == 'ollama'
             else cfg.models.embedding_model)

    print()
    print("=" * 60)
    print("Re-embedding user-content collections")
    print(f"  backend : {backend}")
    print(f"  model   : {model}")
    print(f"  targets : {', '.join(NON_ZIM_COLLECTIONS)}")
    print("=" * 60)

    if not args.yes:
        print()
        print("  REFUSING TO RUN — this rewrites the collections above with")
        print(f"  {model!r} and deletes their current contents on success.")
        print()
        print("  Each collection is staged and count-verified before its")
        print("  original is removed, so a failure cannot silently truncate")
        print("  one — but back up the ChromaDB directory anyway.")
        print()
        print("  Re-run with --yes to proceed:")
        print("      python scripts/reindex.py --reembed --yes")
        print("=" * 60)
        return 1

    client = DualZoneRAG().client
    total = 0
    failed = []
    for name in NON_ZIM_COLLECTIONS:
        print(f"  {name} ...", end=" ", flush=True)
        try:
            status, rebuilt, source_total = reembed_collection(client, name)
        except Exception as e:
            print(f"FAILED\n      {e}")
            logger.exception("Re-embed failed for %s", name)
            failed.append(name)
            continue
        if status == 'missing':
            print("not present, skipped")
        elif status == 'empty':
            print("empty, skipped")
        elif rebuilt != source_total:
            # Defensive: reembed_collection raises on mismatch, so reaching
            # here means its own verification was bypassed somehow.
            print(f"PARTIAL — {rebuilt} of {source_total}")
            failed.append(name)
        else:
            print(f"rebuilt {rebuilt} of {source_total} chunks")
            total += rebuilt

    print()
    print(f"  Total re-embedded: {total} chunks")
    if failed:
        print(f"  FAILED/PARTIAL   : {len(failed)} ({', '.join(failed)})")
        print()
        print("  The re-index is INCOMPLETE. Collections listed above are")
        print("  still in the old vector space (or staged under")
        print(f"  '<name>{STAGING_SUFFIX}') — resolve before serving queries.")
    print("=" * 60)
    return 1 if failed else 0


def cmd_reindex(args):
    """Main re-indexing logic."""
    libraries = load_libraries()

    if args.library:
        libraries = [lib for lib in libraries
                     if lib['library'].lower() == args.library.lower()]
        if not libraries:
            print(f"ERROR: No library found with name '{args.library}'")
            print("Available libraries:")
            for lib in load_libraries():
                print(f"  - {lib['library']}")
            sys.exit(1)

    print("=" * 60)
    print("PuffPAI Knowledge Base Re-Indexer")
    print("=" * 60)
    print(f"Libraries to process : {len(libraries)}")
    print(f"Articles per library : {ARTICLES_TO_INDEX}")
    print(f"Force re-index       : {args.force}")
    print()

    rag = DualZoneRAG()
    client = rag.client

    # Discover available ZIM files and build a mapping
    zim_paths = discover_from_config()
    zim_map: dict[str, str] = {}
    for zp in zim_paths:
        matched = match_zim_to_library(zp, libraries)
        if matched:
            zim_map[matched['library_id']] = zp

    if zim_paths:
        print(f"ZIM files discovered : {len(zim_paths)}")
        print(f"ZIM files matched    : {len(zim_map)}")
    else:
        print("WARNING: No ZIM files discovered. Check paths.zim_dirs in config.")
    print()

    total = len(libraries)
    skipped = 0
    success = 0
    no_zim = 0
    failed = []

    for i, lib_info in enumerate(libraries, 1):
        library_id = lib_info['library_id']
        library_name = lib_info['library']
        collection_name = lib_info['collection']

        print(f"[{i:>2}/{total}] {library_name}")

        # --- Skip check -------------------------------------------------------
        existing_chunks = count_chunks(client, collection_name)
        if existing_chunks >= SKIP_THRESHOLD and not args.force:
            print(f"         Skipping -- already has {existing_chunks} chunks "
                  f"(use --force to re-index)")
            skipped += 1
            continue

        # --- Find the ZIM file ------------------------------------------------
        zim_path = zim_map.get(library_id)
        if not zim_path:
            print(f"         No ZIM file found for '{library_id}' -- skipping")
            no_zim += 1
            continue

        if existing_chunks > 0:
            print(f"         Existing chunks: {existing_chunks} -- deleting and rebuilding")

        # --- Delete existing collection ---------------------------------------
        try:
            client.delete_collection(name=collection_name)
        except Exception:
            pass  # Collection didn't exist yet -- that's fine

        # --- Re-index via KnowledgeIngestion ----------------------------------
        try:
            t0 = time.time()
            # Pin the fresh collection to the configured embedding function.
            # The collection was just deleted above, so this is always a
            # create — no persisted-config conflict to worry about. This is
            # how a legacy collection stops persisting an empty (= chroma
            # default) config and starts naming ours. KnowledgeIngestion also
            # passes explicit embeddings=, so the vectors would be right
            # either way; this makes the stored config honest too.
            from puffpai.embeddings import get_or_create_collection
            collection = get_or_create_collection(client, collection_name)
            ingestion = KnowledgeIngestion(knowledge_collection=collection)
            results = ingestion.ingest_zim(
                zim_path, max_articles=ARTICLES_TO_INDEX)
            elapsed = time.time() - t0

            new_count = count_chunks(client, collection_name)
            print(f"         Done in {elapsed:.1f}s -- {new_count} chunks "
                  f"({len(results)} articles)")
            success += 1

        except Exception as e:
            print(f"         ERROR: {e}")
            logger.exception("Failed to index %s", library_name)
            failed.append(library_name)

    # --- Summary --------------------------------------------------------------
    print()
    print("=" * 60)
    print("Re-indexing complete")
    print(f"  Success  : {success}")
    print(f"  Skipped  : {skipped}")
    print(f"  No ZIM   : {no_zim}")
    print(f"  Failed   : {len(failed)}")
    if failed:
        print("  Failed libraries:")
        for name in failed:
            print(f"    - {name}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="PuffPAI Knowledge Base Re-Indexer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--force', action='store_true',
                        help='Re-index even if the collection already looks complete')
    parser.add_argument('--library', type=str, default=None,
                        help='Only re-index this library (by name)')
    parser.add_argument('--list', action='store_true',
                        help='List all discovered ZIM files and exit')
    parser.add_argument('--status', action='store_true',
                        help='Show chunk counts for all collections and exit')
    parser.add_argument('--reembed', action='store_true',
                        help='Re-embed the user-content collections in place '
                             '(user_uploads, puffpai_vault, puffpai_knowledge) '
                             '- the other half of a full re-index')
    parser.add_argument('--yes', action='store_true',
                        help='Confirm the destructive --reembed run. Without '
                             'it, --reembed prints what it would do and exits '
                             'non-zero (no interactive prompt, so it stays '
                             'scriptable)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')
    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format='%(name)s: %(message)s')

    if args.list:
        cmd_list()
    elif args.status:
        cmd_status()
    elif args.reembed:
        # Propagate failure: a partially re-embedded store must not look like
        # a clean run to whatever is driving the re-index.
        return cmd_reembed(args)
    else:
        cmd_reindex(args)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
