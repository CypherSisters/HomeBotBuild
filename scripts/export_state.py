#!/usr/bin/env python3
"""PuffPAI state backup / migration export.

Copies everything that constitutes PuffPAI's irreplaceable state into a
dated snapshot directory. Doubles as the hardware-migration tool: run it,
carry the snapshot over, restore.

What it saves (relative to this repo checkout — resolved from __file__, so
the snapshot always covers the checkout this script lives in):
  vault_data/                encrypted vault (AES-256-GCM)
  <chromadb dir>/            ChromaDB knowledge library, LARGE.
                             Directory name is read from
                             config/puffpai_config.json's paths.chromadb_path
                             (or the legacy chromadb.path), so this stays
                             correct if that path is ever renamed.
  src/puffpai/data/          legacy state location. Still copied: a module
                             whose default moved ADOPTS the old file in place
                             rather than migrating it, so this is live state
                             on a box that never set $PUFFPAI_DATA_DIR.
  data/*.json, *.jsonl, *.txt   registries, firewall rules, the consent
                             ledger, consent-request
                             queue, agent event log, scheduled-task queue,
                             daily briefing, caretaker restart ledger and the
                             sentinel baseline/alerts.
                             The walk recurses, so the toolsmith sandbox
                             (data/workspace/, its default location)
                             is traversed too — but the filter applies at
                             every level, so only .json/.jsonl/.txt tool
                             output survives it. Anything else an agent wrote
                             there is NOT in the snapshot.
  chat_history/              conversation history
  config/                    all config JSON
  security_logs/             audit JSONL
  ../puffpai-dashboard/.env.local   dashboard server-side env (tokens)

Usage:
  python export_state.py                    # full snapshot to F:\\PuffPAI_backups\\<date>
  python export_state.py --dest D:\\bak     # custom destination
  python export_state.py --skip-rag        # skip the big ChromaDB (quick nightly)
  python export_state.py --verify <dir>    # sanity-check an existing snapshot

Schedule nightly (elevated prompt; schtasks needs ABSOLUTE paths, so fill in
your own interpreter and your own checkout - both must be spelled out, and
the interpreter is whichever python.exe can import the repo, not a bare
`python`):
  schtasks /Create /TN PuffPAI-StateBackup /SC DAILY /ST 03:30 ^
    /TR "<path-to>\\python.exe <path-to>\\PuffPAI\\scripts\\export_state.py --skip-rag"

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # this checkout's root
ROOT = os.path.dirname(REPO)                                            # ...\PuffPAI
DEFAULT_DEST = r"F:\PuffPAI_backups"


#: Env var naming the writable state root. Mirrors ``config.STATE_DIR_ENV``
#: without importing the app package — this tool must keep working when the
#: app itself is broken, which is exactly when a backup matters most.
STATE_DIR_ENV = "PUFFPAI_DATA_DIR"


def _configured_chromadb_path() -> str:
    """The raw ``paths.chromadb_path`` (or legacy ``chromadb.path``) string."""
    default = "chromadb"
    cfg_path = os.path.join(REPO, "config", "puffpai_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return default
    return (cfg.get("paths", {}).get("chromadb_path")
            or cfg.get("chromadb", {}).get("path")
            or default)


def _chromadb_source(configured: str | None = None,
                     repo: str | None = None) -> str:
    """Absolute path of the ChromaDB store to back up.

    Precedence is the app's own (``config.resolve_state_dir``), so the backup
    reads the directory the service writes:

    1. an ABSOLUTE configured path is honoured verbatim;
    2. a RELATIVE one is joined to ``$PUFFPAI_DATA_DIR`` when that is set -
       the writable data root an appliance runs with;
    3. otherwise it falls back to the repo checkout (dev convenience, which is
       the only case where the store really does live in the tree).

    Joining only ``os.path.basename()`` of the configured path back onto the
    checkout would turn a configured ``/data/puffpai/chromadb`` into
    ``/opt/puffpai/chromadb``, which does not exist, so the RAG leg of the
    backup would silently cover nothing but a "WARN missing" line.
    """
    repo = repo or REPO
    path = configured if configured is not None else _configured_chromadb_path()
    path = os.path.expanduser(str(path or "").strip()) or "chromadb"
    if os.path.isabs(path):
        return os.path.normpath(path)
    data_root = os.path.expanduser(os.environ.get(STATE_DIR_ENV, "").strip())
    if data_root:
        if not os.path.isabs(data_root):
            data_root = os.path.join(repo, data_root)
        return os.path.normpath(os.path.join(data_root, path))
    return os.path.normpath(os.path.join(repo, path))


def _chromadb_dirname(configured: str | None = None) -> str:
    """The name the store is filed under INSIDE the snapshot."""
    path = configured if configured is not None else _configured_chromadb_path()
    return os.path.basename(os.path.normpath(str(path or ""))) or "chromadb"


CHROMADB_DIR = _chromadb_dirname()
CHROMADB_SRC = _chromadb_source()

# (source, dest-subdir, is_dir, optional glob for files-in-dir)
ITEMS: list[tuple[str, str, bool, str | None]] = [
    (os.path.join(REPO, "vault_data"),                 "vault_data",        True,  None),
    (CHROMADB_SRC,                                     CHROMADB_DIR,        True,  None),
    (os.path.join(REPO, "src", "puffpai", "data"),     "src_puffpai_data",  True,  None),
    (os.path.join(REPO, "data"),                       "data",              True,  "*.json|*.jsonl|*.txt"),
    (os.path.join(REPO, "chat_history"),               "chat_history",      True,  None),
    (os.path.join(REPO, "config"),                     "config",            True,  None),
    (os.path.join(REPO, "security_logs"),              "security_logs",     True,  None),
    (os.path.join(ROOT, "puffpai-dashboard", ".env.local"),  "dashboard_env.local", False, None),
]

BIG_ITEMS = {CHROMADB_DIR}  # skipped with --skip-rag


def _copy_dir_filtered(src: str, dst: str, patterns: str | None) -> int:
    """Copy a directory; optional '|'-separated glob filter on filenames."""
    count = 0
    for base, _dirs, files in os.walk(src):
        rel = os.path.relpath(base, src)
        for f in files:
            if patterns and not any(fnmatch.fnmatch(f, p) for p in patterns.split("|")):
                continue
            target_dir = os.path.join(dst, rel) if rel != "." else dst
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(os.path.join(base, f), os.path.join(target_dir, f))
            count += 1
    return count


def export(dest_root: str, skip_rag: bool) -> int:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    dest = os.path.join(dest_root, stamp)
    drive = os.path.splitdrive(dest_root)[0]
    if drive and not os.path.exists(drive + os.sep):
        print(f"FAIL: destination drive {drive} not mounted. Plug in the backup drive"
              f" or pass --dest.")
        return 1
    os.makedirs(dest, exist_ok=True)

    manifest: dict = {"created": stamp, "source_repo": REPO, "items": {}, "skipped": []}
    total = 0
    for src, sub, is_dir, patterns in ITEMS:
        if skip_rag and sub in BIG_ITEMS:
            manifest["skipped"].append(sub)
            print(f"  skip (—skip-rag): {sub}")
            continue
        if not os.path.exists(src):
            manifest["items"][sub] = {"status": "MISSING_SOURCE"}
            print(f"  WARN missing: {src}")
            continue
        target = os.path.join(dest, sub)
        if is_dir:
            n = _copy_dir_filtered(src, target, patterns)
        else:
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            shutil.copy2(src, target)
            n = 1
        manifest["items"][sub] = {"status": "ok", "files": n}
        total += n
        print(f"  {sub}: {n} file(s)")

    with open(os.path.join(dest, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nDONE: {total} files -> {dest}")
    print("Restore: copy each item back to its source path (see MANIFEST.json"
          " + header of this script), then restart the backend.")

    # Retention: keep the 14 newest snapshots.
    snaps = sorted(d for d in os.listdir(dest_root)
                   if os.path.isdir(os.path.join(dest_root, d)))
    for old in snaps[:-14]:
        shutil.rmtree(os.path.join(dest_root, old), ignore_errors=True)
        print(f"  pruned old snapshot: {old}")
    return 0


def verify(snapshot_dir: str) -> int:
    mpath = os.path.join(snapshot_dir, "MANIFEST.json")
    if not os.path.isfile(mpath):
        print(f"FAIL: no MANIFEST.json in {snapshot_dir}")
        return 1
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    rc = 0
    for sub, info in manifest["items"].items():
        if info.get("status") != "ok":
            print(f"  {sub}: {info.get('status')}")
            continue
        path = os.path.join(snapshot_dir, sub)
        exists = os.path.exists(path)
        n = sum(len(fs) for _, _, fs in os.walk(path)) if os.path.isdir(path) else int(exists)
        ok = exists and (n >= info["files"] * 0.99)
        print(f"  {sub}: {'OK' if ok else 'FAIL'} ({n}/{info['files']} files)")
        if not ok:
            rc = 1
    print("VERIFY", "PASSED" if rc == 0 else "FAILED")
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--skip-rag", action="store_true",
                    help="skip the ChromaDB knowledge library (large); use for quick nightlies")
    ap.add_argument("--verify", metavar="SNAPSHOT_DIR",
                    help="verify an existing snapshot instead of exporting")
    args = ap.parse_args()
    sys.exit(verify(args.verify) if args.verify else export(args.dest, args.skip_rag))
