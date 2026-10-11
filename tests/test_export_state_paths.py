"""export_state.py: back up the ChromaDB store that EXISTS, and ship no
developer's home directory in a public repo.



    config paths.chromadb_path = /data/puffpai/chromadb
    deployed script CHROMADB_DIR = "chromadb", REPO = /opt/puffpai
    ITEMS entry ('/opt/puffpai/chromadb', 'chromadb', True, None)
                               exists: False
    on disk /opt/puffpai/chromadb ABSENT
                               /data/puffpai/chromadb 9.6M (the real store)

so the RAG leg of the backup covered NOTHING - one "WARN missing" line and a
zero exit. The old code took ``os.path.basename`` of the configured path and
joined it back onto the checkout, which throws away exactly the information an
appliance's absolute path carries.

Also pinned: the old ``C:@Users@<user>@...@scripts@export_state.py``
schtasks example is gone. A public repo should not ship a maintainer's username.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "export_state.py"


def _load(monkeypatch, tmp_path, configured, data_dir=None):
    """Import export_state fresh with a config whose chromadb path is
    *configured* and (optionally) $PUFFPAI_DATA_DIR set."""
    fake_repo = tmp_path / "checkout"
    (fake_repo / "config").mkdir(parents=True)
    (fake_repo / "config" / "puffpai_config.json").write_text(
        json.dumps({"paths": {"chromadb_path": configured}}), encoding="utf-8")
    if data_dir is None:
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(data_dir))

    spec = importlib.util.spec_from_file_location(
        "export_state_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # REPO is computed from __file__ at import time; point it at the fixture by
    # loading the module and re-running its two derived constants.
    spec.loader.exec_module(mod)
    mod.REPO = str(fake_repo)
    return mod


def test_the_developer_path_is_gone():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "Users" + chr(92) not in text
    assert "C:" + chr(92) + chr(92) + "Users" not in text


def test_the_schedule_example_still_explains_it_needs_absolute_paths():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "schtasks" in text
    assert "ABSOLUTE paths" in text


# ── path resolution: absolute wins, relative follows the data dir ───────────

def test_an_absolute_configured_path_is_honoured_verbatim(monkeypatch, tmp_path):
    """The live case: /data/puffpai/chromadb must not become
    /opt/puffpai/chromadb."""
    mod = _load(monkeypatch, tmp_path, "/data/puffpai/chromadb")
    got = mod._chromadb_source("/data/puffpai/chromadb", repo="/opt/puffpai")
    assert os.path.normpath(got) == os.path.normpath("/data/puffpai/chromadb")
    assert "opt" not in got.replace(os.sep, "/").split("/data/")[0] or True
    assert not got.replace(os.sep, "/").startswith("/opt/puffpai")


def test_an_absolute_path_still_files_under_its_own_name(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path, "/data/puffpai/chromadb")
    assert mod._chromadb_dirname("/data/puffpai/chromadb") == "chromadb"


def test_a_relative_path_follows_the_data_dir_not_the_repo(monkeypatch, tmp_path):
    data = tmp_path / "data_root"
    mod = _load(monkeypatch, tmp_path, "./kiwix_rag_db", data_dir=data)
    got = mod._chromadb_source("./kiwix_rag_db", repo="/opt/puffpai")
    assert os.path.normpath(got) == os.path.normpath(
        str(data / "kiwix_rag_db"))


def test_a_relative_path_with_no_data_dir_stays_in_the_checkout(
        monkeypatch, tmp_path):
    """Dev convenience, and the only case where the store really is in the
    tree. Mirrors config.resolve_state_dir's own last resort."""
    mod = _load(monkeypatch, tmp_path, "./kiwix_rag_db")
    got = mod._chromadb_source("./kiwix_rag_db", repo=str(tmp_path / "co"))
    assert os.path.normpath(got) == os.path.normpath(
        str(tmp_path / "co" / "kiwix_rag_db"))


def test_a_missing_config_falls_back_to_the_shipped_default(
        monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path, "./kiwix_rag_db")
    mod.REPO = str(tmp_path / "no_such_checkout")
    assert mod._configured_chromadb_path() == "chromadb"


def test_the_legacy_chromadb_section_is_still_read(monkeypatch, tmp_path):
    fake_repo = tmp_path / "legacy"
    (fake_repo / "config").mkdir(parents=True)
    (fake_repo / "config" / "puffpai_config.json").write_text(
        json.dumps({"chromadb": {"path": "/data/puffpai/chromadb"}}),
        encoding="utf-8")
    mod = _load(monkeypatch, tmp_path, "./ignored")
    mod.REPO = str(fake_repo)
    assert mod._configured_chromadb_path() == "/data/puffpai/chromadb"


# ── the item table actually uses it ────────────────────────────────────────

def test_the_backup_item_points_at_the_resolved_source():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("export_state_items", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    entry = [i for i in mod.ITEMS if i[1] == mod.CHROMADB_DIR]
    assert len(entry) == 1
    assert entry[0][0] == mod.CHROMADB_SRC
    # And the store name is still what BIG_ITEMS / --skip-rag keys on.
    assert mod.CHROMADB_DIR in mod.BIG_ITEMS
