#!/usr/bin/env python3
"""
PuffPAI ChromaDB Collection Health Check
==========================================
Test-queries every collection listed in indexed_libraries.json and reports
which ones are healthy, partially indexed, empty, or broken (HNSW errors).

Run from the project root:
    python scripts/chromadb_health.py

Options:
    --json      Output as JSON instead of a human-readable table
    --repair    Delete broken collections so they can be re-indexed
    --verbose   Show detailed error messages

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import sys
import os
import json
import argparse
import logging

# Make src/ importable regardless of CWD
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from puffpai.knowledge.rag import DualZoneRAG

logger = logging.getLogger("chromadb_health")


def print_table(report: list) -> None:
    """Pretty-print the health report as a table."""
    healthy = sum(1 for r in report if r['status'] == 'healthy')
    partial = sum(1 for r in report if r['status'] == 'partial')
    empty = sum(1 for r in report if r['status'] == 'empty')
    errors = sum(1 for r in report if r['status'] == 'error')

    STATUS_ICONS = {
        'healthy': 'OK',
        'partial': 'PARTIAL',
        'empty': 'EMPTY',
        'error': 'ERROR',
    }

    print()
    print("PuffPAI ChromaDB Health Check")
    print("=" * 80)
    print(f"  {'Library':<35} {'Collection':<30} {'Chunks':>6}  Status")
    print("  " + "-" * 78)

    for r in report:
        lib_name = r['library'][:32] + "..." if len(r['library']) > 35 else r['library']
        coll_short = r['name'][:27] + "..." if len(r['name']) > 30 else r['name']
        status = STATUS_ICONS.get(r['status'], r['status'])
        print(f"  {lib_name:<35} {coll_short:<30} {r['chunks']:>6}  {status}")
        if r['error']:
            # Show truncated error on next line
            err_msg = r['error'][:70]
            print(f"  {'':35} -> {err_msg}")

    print("  " + "-" * 78)
    print(f"  Total: {len(report)} | Healthy: {healthy} | "
          f"Partial: {partial} | Empty: {empty} | Error: {errors}")
    print()


def repair_broken(report: list) -> int:
    """Delete broken collections so they can be re-indexed.

    Returns number of collections deleted.
    """
    broken = [r for r in report if r['status'] == 'error']
    if not broken:
        print("No broken collections to repair.")
        return 0

    rag = DualZoneRAG()
    client = rag.client
    deleted = 0

    for r in broken:
        try:
            client.delete_collection(name=r['name'])
            print(f"  Deleted: {r['name']}")
            deleted += 1
        except Exception as e:
            print(f"  Failed to delete {r['name']}: {e}")

    print(f"\nDeleted {deleted} broken collection(s). "
          f"Run 'python scripts/reindex.py' to rebuild them.")
    return deleted


def main():
    parser = argparse.ArgumentParser(
        description="PuffPAI ChromaDB Collection Health Check",
    )
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON instead of table')
    parser.add_argument('--repair', action='store_true',
                        help='Delete broken collections for re-indexing')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format='%(name)s: %(message)s')

    rag = DualZoneRAG()
    report = rag.health_check()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_table(report)

    if args.repair:
        repair_broken(report)


if __name__ == "__main__":
    main()
