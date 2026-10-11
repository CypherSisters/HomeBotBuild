"""Where the app-facing modules put their runtime state.

Three modules used to pin writable state to hardcoded repo-relative paths:
:mod:`puffpai.ingestion` (uploads + the ingested-doc catalog),
:mod:`puffpai.app_data` (the HTML apps' JSON) and :mod:`puffpai.suggestions`
(the proactive-suggestion queue). On the appliance the code tree is mounted
read-only (``ProtectSystem=strict``, ``ReadWritePaths=/data/puffpai``), so every
one of those writes failed with EROFS. Worse, ingestion did its ``mkdir`` at
MODULE SCOPE, which turned a plain ``import puffpai.ingestion`` into an error
and took every route that imports it down with it.

They now resolve through :func:`puffpai.statedir.resolve_runtime_dir`. What
these tests pin, per module:

* env unset  → byte-identical to the historical in-tree path;
* env set    → the relocated subtree, so a hardened unit actually works;
* override   → the module-level hook still outranks both (existing tests
  monkeypatch those names and must keep passing);
* one real write per module lands in the relocated tree with parents created.

No OCR, no LLM, no ChromaDB, no network — path logic only. Nothing here may
write inside the repo, so any case that actually writes points the env at
``tmp_path`` first; the env-unset cases only ever compute a path.

Run: py -3 -m pytest tests/test_app_state_paths.py -q
"""

import os
from pathlib import Path

import pytest

from puffpai import app_data, suggestions as sug
from puffpai import ingestion as ing

_ENV = "PUFFPAI_DATA_DIR"


@pytest.fixture(autouse=True)
def _fresh_resolution(monkeypatch):
    """Drop every cached/overridden path so each case re-resolves from its env.

    app_data and suggestions memoize the resolved directory; ingestion does
    not, but it has the two override hooks. Clearing all of them keeps the
    cases order-independent.
    """
    monkeypatch.setattr(app_data, "_data_dir_cache", None)
    monkeypatch.setattr(sug, "_data_dir_cache", None)
    monkeypatch.setattr(sug, "_STORE_PATH", None)
    monkeypatch.setattr(ing, "_UPLOAD_DIR", None)
    monkeypatch.setattr(ing, "_DOC_INDEX", None)


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    """Point $PUFFPAI_DATA_DIR at a throwaway absolute directory."""
    root = tmp_path / "state"
    monkeypatch.setenv(_ENV, str(root))
    return root


def _unset_env(monkeypatch, *repo_roots):
    """Unset ``$PUFFPAI_DATA_DIR`` and state the dev-checkout precondition.

    "Env unset keeps the in-tree path" is only true of a checkout that has
    already RUN: ``config._dev_checkout_has_state`` looks for runtime state
    under ``<repo>/data`` and, finding none, sends a fresh clone to the
    per-user directory instead. These tests used to depend on that state being
    there ambiently -- so they passed on a developer's machine, passed after a
    suite that had leaked writes into the checkout, and failed on a clean clone
    where the answer is the honest one. The precondition is now stated rather
    than inherited, which also means no test needs the checkout to be dirty.
    """
    import os as _os

    from puffpai import config as _cfgmod

    monkeypatch.delenv(_ENV, raising=False)
    for root in repo_roots:
        monkeypatch.setitem(_cfgmod._dev_state_cache,
                            _os.path.normpath(str(root)), True)


# ── ingestion ────────────────────────────────────────────────────────────────

class TestIngestionPaths:
    def test_unset_env_keeps_the_historical_in_tree_paths(self, monkeypatch):
        _unset_env(monkeypatch, ing._REPO_ROOT)
        assert ing._upload_dir() == ing._REPO_ROOT / "data" / "uploads"
        assert ing._doc_index() == ing._REPO_ROOT / "data" / "ingested_docs.json"

    def test_env_relocates_uploads_into_their_own_subtree(self, state_root):
        assert ing._upload_dir() == Path(state_root) / "uploads"

    def test_env_relocates_the_doc_catalog_to_the_state_root(self, state_root):
        # No env_subdir: a single file belongs in the root, not in a directory
        # of its own.
        assert ing._doc_index() == Path(state_root) / "ingested_docs.json"

    def test_module_import_creates_no_directory(self, state_root):
        """The import-time mkdir is gone — resolving must stay side-effect free.

        This is the EROFS trap the migration existed to remove: merely
        resolving must not touch the filesystem.
        """
        ing._upload_dir()
        ing._doc_index()
        assert not state_root.exists()

    def test_hook_outranks_the_environment(self, state_root, tmp_path,
                                           monkeypatch):
        forced_dir = tmp_path / "forced_uploads"
        forced_index = tmp_path / "forced" / "docs.json"
        monkeypatch.setattr(ing, "_UPLOAD_DIR", forced_dir)
        monkeypatch.setattr(ing, "_DOC_INDEX", forced_index)
        assert ing._upload_dir() == forced_dir
        assert ing._doc_index() == forced_index

    def test_hook_accepts_a_plain_string(self, state_root, tmp_path,
                                         monkeypatch):
        """Existing callers hand these hooks a Path; a str must work too."""
        monkeypatch.setattr(ing, "_DOC_INDEX", str(tmp_path / "s.json"))
        assert ing._doc_index() == tmp_path / "s.json"

    def test_saving_an_upload_creates_the_relocated_dir(self, state_root):
        """The write site owns directory creation now, and the parents are
        missing on a fresh appliance — so it must create them."""
        assert not state_root.exists()
        path = Path(ing.save_uploaded_file(b"hello", "notes.txt"))

        assert path.parent == state_root / "uploads"
        assert path.read_bytes() == b"hello"

    def test_doc_index_write_lands_in_the_relocated_tree(self, state_root,
                                                         monkeypatch):
        # Stub the vector index — this test is about the JSON catalog's path,
        # and ChromaDB has no business being started for it.
        monkeypatch.setattr(ing, "_add_to_user_uploads_collection",
                            lambda *a, **k: None)
        ing._index_study_doc("f1", "syllabus.pdf", "some extracted text")

        catalog = state_root / "ingested_docs.json"
        assert catalog.is_file()
        assert ing.list_ingested_docs()[0]["filename"] == "syllabus.pdf"


# ── app_data ─────────────────────────────────────────────────────────────────

class TestAppDataPaths:
    def test_unset_env_keeps_the_historical_in_tree_dir(self, monkeypatch):
        # Path only — constructing AppData() would makedirs the real
        # <repo>/app_data, and no test may write into the repo tree.
        _unset_env(monkeypatch, app_data._REPO_ROOT)
        assert app_data._data_dir() == os.path.join(app_data._REPO_ROOT,
                                                    "app_data")

    def test_env_relocates_into_an_app_data_subtree(self, state_root):
        assert app_data._data_dir() == str(state_root / "app_data")

    def test_resolution_is_cached(self, state_root, tmp_path, monkeypatch):
        first = app_data._data_dir()
        monkeypatch.setenv(_ENV, str(tmp_path / "moved"))
        assert app_data._data_dir() == first, "cached after first use"

    def test_every_app_file_lands_under_the_relocated_dir(self, state_root):
        app = app_data.AppData()

        expected = state_root / "app_data"
        assert set(app.VALID_APPS) == set(app._files)
        for name, path in app._files.items():
            assert Path(path).parent == expected, name
            assert Path(path).name == f"{name}.json"

    def test_a_save_creates_the_relocated_dir_and_writes_there(self,
                                                               state_root):
        assert not state_root.exists()
        app = app_data.AppData()
        app.alarm_create_timer("pasta", minutes=8)

        landed = state_root / "app_data" / "alarmflow.json"
        assert landed.is_file()
        assert app.get_data("alarmflow")["items"][0]["desc"] == "pasta"

    def test_applications_dir_is_code_and_never_follows_the_env(self,
                                                                state_root):
        """Applications/ holds shipped HTML — code, not state. A hardened unit
        mounts it read-only on purpose, so it must NOT move with the env."""
        assert str(state_root) not in app_data._APPS_DIR


# ── suggestions ──────────────────────────────────────────────────────────────

class TestSuggestionsPaths:
    def test_unset_env_keeps_the_historical_in_tree_path(self, monkeypatch):
        _unset_env(monkeypatch, sug._REPO_ROOT)
        assert sug._store_path() == os.path.join(
            sug._REPO_ROOT, "app_data", "suggestions.json")

    def test_env_relocates_alongside_the_app_data(self, state_root):
        assert sug._store_path() == str(
            state_root / "app_data" / "suggestions.json")

    def test_it_shares_the_app_data_subtree(self, state_root):
        """The queue is *about* the app data; they must not separate."""
        assert (os.path.dirname(sug._store_path())
                == app_data._data_dir())

    def test_hook_outranks_the_environment(self, state_root, tmp_path,
                                           monkeypatch):
        forced = str(tmp_path / "forced" / "suggestions.json")
        monkeypatch.setattr(sug, "_STORE_PATH", forced)
        assert sug._store_path() == forced

    def test_hook_is_a_plain_none_global_that_undo_restores(self, state_root,
                                                            tmp_path,
                                                            monkeypatch):
        """The regression guard for the shim this replaced.

        A __getattr__-synthesized name made monkeypatch's teardown PIN the
        synthesized path into the globals, which then outranked the env for the
        rest of the process and sent writes back into the repo tree.
        """
        assert vars(sug)["_STORE_PATH"] is None, "must be a real global, not a shim"

        with monkeypatch.context() as m:
            m.setattr(sug, "_STORE_PATH", str(tmp_path / "x.json"))
        # After teardown the env must be back in charge.
        assert vars(sug)["_STORE_PATH"] is None
        assert sug._store_path() == str(
            state_root / "app_data" / "suggestions.json")

    def test_hook_accepts_a_path_object(self, state_root, tmp_path,
                                        monkeypatch):
        monkeypatch.setattr(sug, "_STORE_PATH", tmp_path / "p.json")
        assert sug._store_path() == str(tmp_path / "p.json")

    def test_a_store_write_creates_the_relocated_dir(self, state_root):
        assert not state_root.exists()
        sug.add({"id": "s1", "status": "pending", "doc_name": "flyer.pdf",
                 "created_at": "2026-08-12T00:00:00+00:00",
                 "expires_at": "2099-01-01T00:00:00+00:00"})

        landed = state_root / "app_data" / "suggestions.json"
        assert landed.is_file()
        assert [s["id"] for s in sug.list_pending()] == ["s1"]


# ── the whole point ──────────────────────────────────────────────────────────

def test_no_module_resolves_into_the_repo_when_the_env_is_set(state_root):
    """With the env set, nothing may still point inside the code tree.

    This is the EROFS regression guard: one stale hardcoded path is enough to
    fail a write on the appliance.
    """
    repo = str(ing._REPO_ROOT)
    resolved = [
        str(ing._upload_dir()),
        str(ing._doc_index()),
        app_data._data_dir(),
        sug._store_path(),
    ]
    for path in resolved:
        assert not path.startswith(repo), path
