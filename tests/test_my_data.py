"""The owner's data folder, linked folders, the watcher and vault capture.

No network, temp folders only; every key here is fake (EXAMPLE, 0000).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket

import pytest

from puffpai import ingestion
from puffpai.config import get_config, get_state_dir
from puffpai.knowledge import my_data, reading_helper, upload_search
from puffpai.security.vault import Vault

PW = "correct horse battery"
FAKE_KEY = "ghp_EXAMPLE000000000000000000000000000000"
TEXT = "Our garden plan. The tomatoes go in the north bed in May. " * 5


@pytest.fixture
def quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(reading_helper, "chroma_cache_dir", lambda: str(tmp_path / "c"))
    monkeypatch.setattr(reading_helper, "helper_dir", lambda: str(tmp_path / "o"))
    monkeypatch.setattr(ingestion, "_upload_embedder", None)

    def no_network(*a, **k):
        raise AssertionError("tried to go online")
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(ingestion, "classify_document", lambda text: "note")


@pytest.fixture
def vault(tmp_path):
    v = Vault(vault_dir=str(tmp_path / "vault"))
    v.initialize(PW)
    v.unlock(PW)
    return v


@pytest.fixture
def data(tmp_path):
    out = my_data.set_data_dir(str(tmp_path / "Data folder"))
    assert out["ok"], out
    return tmp_path / "Data folder"


def _digest(folder):
    out = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as fh:
                out[p] = (hashlib.sha256(fh.read()).hexdigest(), os.stat(p).st_mtime)
    return out


def test_data_dir_round_trips_and_downloads_and_uploads_land_there(tmp_path, quiet, vault, data):
    from puffpai.knowledge import find_more
    assert my_data.data_dir() == str(data)
    assert get_config().knowledge.data_dir == str(data)
    assert get_config().knowledge.zim_dir == str(data)
    assert find_more.knowledge_folder() == str(data)
    out = my_data.add_upload(TEXT.encode(), "plan.txt", relative="Garden/plan.txt",
                             auto_apply=False, vault=vault)
    assert out["status"] == "indexed", out
    assert (data / "My files" / "Garden" / "plan.txt").is_file()
    # changing it keeps the old folder and still reads it
    other = my_data.set_data_dir(str(tmp_path / "Second"))
    assert other["ok"] and any("kept as it was" in line for line in other["lines"])
    assert str(data) in my_data.previous_dirs()
    assert (data / "My files" / "Garden" / "plan.txt").is_file()
    assert my_data.scan_once(vault=vault)["removed"] == 0


def test_upload_names_stay_inside_my_files(data):
    target = my_data.upload_target("x.txt", "../../evil/x.txt")
    assert os.path.commonpath([target, str(data / "My files")]) == str(data / "My files")


def test_link_unlink_and_wrong_places(tmp_path, quiet, vault, data):
    mine = tmp_path / "Notes"
    mine.mkdir()
    (mine / "a.md").write_text(TEXT, encoding="utf-8")
    got = my_data.link_folder(str(mine))
    assert got["ok"], got
    fid = got["folder"]["id"]
    assert [r["id"] for r in get_config().knowledge.linked_folders] == [fid]
    assert my_data.link_folder(str(mine)).get("already")
    my_data.scan_once(vault=vault)
    assert upload_search.count(my_data.doc_id_for(str(mine / "a.md"), fid)) > 0
    out = my_data.unlink_folder(fid)
    assert out["ok"] and out["removed_from_index"] == 1
    assert upload_search.count(my_data.doc_id_for(str(mine / "a.md"), fid)) == 0
    assert (mine / "a.md").is_file()
    # the vault's folder, the state folder and anything holding them are refused
    for wrong in (get_state_dir(), os.path.dirname(get_state_dir())):
        os.makedirs(wrong, exist_ok=True)
        refused = my_data.link_folder(wrong)
        assert not refused["ok"], wrong
    from puffpai.security import owner_folders
    os.makedirs(owner_folders.vault_dir(), exist_ok=True)
    assert not my_data.link_folder(owner_folders.vault_dir())["ok"]
    assert not my_data.set_data_dir(get_state_dir())["ok"]


def test_watcher_adds_changes_and_drops(tmp_path, quiet, vault, data):
    mine = data / "My files"
    f = mine / "dropped.txt"
    f.write_text(TEXT, encoding="utf-8")
    doc = my_data.doc_id_for(str(f), "mine")
    assert my_data.scan_once(vault=vault)["added"] == 1
    assert ingestion.search_indexed_docs("tomatoes north bed")
    f.write_text("Peppers want water every second morning. " * 5, encoding="utf-8")
    os.utime(f, (os.stat(f).st_atime, os.stat(f).st_mtime + 5))
    assert my_data.scan_once(vault=vault)["changed"] == 1
    assert ingestion.search_indexed_docs("peppers water")
    # HomeBot's search answers with the nearest document whatever the words,
    # so the old text is checked where it would show: no snippet says it.
    assert not any("tomatoes" in str(h.get("snippet") or "").lower()
                   for h in ingestion.search_indexed_docs("tomatoes north bed"))
    # hidden files, too-big files and unsupported endings are skipped
    (mine / ".secret.txt").write_text(TEXT, encoding="utf-8")
    (mine / "photo.png").write_bytes(b"\x89PNG")
    get_config().knowledge.max_file_mb = 1
    (mine / "big.txt").write_text("x" * (1024 * 1024 + 10), encoding="utf-8")
    counts = my_data.scan_once(vault=vault)
    assert counts["added"] == 0 and counts["too_big"] == 1
    status = my_data.status()
    assert status["folders"][0]["too_big"] == 1
    f.unlink()
    assert my_data.scan_once(vault=vault)["removed"] == 1
    assert upload_search.count(doc) == 0


def test_auto_index_off_stops_the_watcher(quiet, vault, data):
    (data / "My files" / "a.txt").write_text(TEXT, encoding="utf-8")
    get_config().knowledge.auto_index = False
    assert my_data.scan_once(vault=vault).get("off")


def test_locked_vault_plain_upload_is_added_at_once(quiet, vault, data):
    vault.lock()
    out = my_data.add_upload(TEXT.encode(), "garden.txt", auto_apply=False, vault=vault)
    assert out["status"] == "indexed", out
    assert (data / "My files" / "garden.txt").is_file()
    assert ingestion.search_indexed_docs("tomatoes north bed")


def test_deleting_an_upload_deletes_its_file_but_never_a_linked_one(quiet, vault, data, tmp_path):
    out = my_data.add_upload(TEXT.encode(), "plan.txt", auto_apply=False, vault=vault)
    path = data / "My files" / "plan.txt"
    assert path.is_file()
    report = ingestion.delete_ingested_doc_report(out["file_id"])
    assert report["ok"], report
    assert not path.exists()
    mine = tmp_path / "Linked"
    mine.mkdir()
    (mine / "a.md").write_text(TEXT, encoding="utf-8")
    fid = my_data.link_folder(str(mine))["folder"]["id"]
    my_data.scan_once(vault=vault)
    assert ingestion.delete_ingested_doc_report(my_data.doc_id_for(str(mine / "a.md"), fid))["ok"]
    assert (mine / "a.md").is_file()


def test_a_big_scan_writes_its_state_a_handful_of_times(data, monkeypatch):
    mine = data / "My files"
    for n in range(1000):
        (mine / f"f{n:04d}.txt").write_text("x", encoding="utf-8")
    writes = []
    real = my_data._save
    monkeypatch.setattr(my_data, "_save", lambda state: (writes.append(1), real(state)))

    def fake_ingest(path, *, root_id, doc_id, vault=None, upload=False):
        return {"root": root_id, "doc_id": doc_id, "name": os.path.basename(path),
                "status": "indexed"}
    counts = my_data.scan_once(ingest=fake_ingest)
    assert counts["added"] == 1000
    assert len(writes) <= 7, len(writes)
    assert my_data.status()["folders"][0]["indexed"] == 1000


def test_installed_rows(quiet, vault, data, tmp_path):
    from puffpai import installed
    assert "knowledge:my-files" not in {r["id"] for r in installed.listing()}
    (data / "My files" / "a.txt").write_text(TEXT, encoding="utf-8")
    my_data.scan_once(vault=vault)
    (tmp_path / "Linked").mkdir()
    fid = my_data.link_folder(str(tmp_path / "Linked"))["folder"]["id"]
    rows = {r["id"]: r for r in installed.listing()}
    for rid in ("knowledge:my-files", f"knowledge:{fid}"):
        assert rows[rid]["kind"] == "knowledge" and rows[rid]["status"] == "ready"
    assert rows["knowledge:my-files"]["name"] == "My files"
    assert str(tmp_path) not in rows[f"knowledge:{fid}"]["what"]


def test_buildbots_own_sidecar_files_are_never_the_owners_uploads(tmp_path):
    """A book's ``.zim.provenance.json`` is never indexed as "Your upload".
    BuildBot's own files are left out of the watch."""
    from puffpai.knowledge import my_data
    (tmp_path / "notes.md").write_text("my notes", encoding="utf-8")
    (tmp_path / "world.zim.provenance.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Book.provenance.json").write_text("{}", encoding="utf-8")
    apps = tmp_path / "state" / "host_apps" / "bb-x"
    apps.mkdir(parents=True)
    (apps / "README.md").write_text("an app", encoding="utf-8")
    (tmp_path / "bb-local.compose.yml").write_text("x", encoding="utf-8")
    found, cut = my_data._walk(str(tmp_path), data_root=True)  # noqa: SLF001
    names = sorted(os.path.basename(p) for p, _st in found)
    assert names == ["bb-local.compose.yml", "notes.md"] and not cut
