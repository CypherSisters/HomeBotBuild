"""The shipped embedding backend has to work on a base install.

If ``config/puffpai_config.json`` shipped
``embedding_backend: "sentence_transformers"``, which is only in the
``embeddings-legacy`` extra, the first knowledge query on a stranger's base
install would raise ImportError inside ``get_embedding_function()``,
``api_server._get_rag_context`` would swallow it, and RAG would return nothing
with no error anywhere a person could see, while the library counts still said
"ready".

So three things are checked here:

1. the SHIPPED config resolves to a backend this install can actually build,
   with nothing installed that ``requirements.txt`` does not install;
2. a backend that cannot be built says so, in a sentence that names what to do;
3. the wizard writes ``models.embedding_backend`` explicitly, so a build's
   record says which embedder it chose rather than inheriting a later default.

No network: the ONNX model files are either already cached on the machine
running this (the normal case on a dev box) or the availability test is skipped
with the reason printed, because downloading them is exactly the egress this
row put behind a door.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puffpai import embeddings

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "config" / "puffpai_config.json"


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A writable data/config dir, so nothing here touches an installed build."""
    import puffpai.config as config

    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path / "config"))
    config.get_config(_force_reload=True)
    yield tmp_path
    config.get_config(_force_reload=True)


# ── 1. what we ship ─────────────────────────────────────────────────────────

def test_shipped_config_names_the_base_install_backend():
    raw = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    assert raw["models"]["embedding_backend"] == embeddings.BACKEND_CHROMA_DEFAULT


def test_code_default_matches_the_shipped_file():
    """A config with no models section must land on the same backend."""
    from puffpai.config import ModelConfig

    assert ModelConfig().embedding_backend == embeddings.BACKEND_CHROMA_DEFAULT
    assert embeddings.DEFAULT_BACKEND == embeddings.BACKEND_CHROMA_DEFAULT


def test_the_default_backend_needs_no_extra_package():
    """onnxruntime and tokenizers arrive with chromadb, which is a base dep."""
    import importlib.util

    assert importlib.util.find_spec("chromadb") is not None
    for module in ("onnxruntime", "tokenizers"):
        assert importlib.util.find_spec(module) is not None, (
            f"{module} is missing; chromadb declares it, so a base install has it")
    # And the legacy backend's package is NOT assumed by any of this.
    assert embeddings.BACKEND_SENTENCE_TRANSFORMERS != embeddings.DEFAULT_BACKEND


def test_base_install_can_embed_a_string(scratch):
    """The end the user feels: one string in, one vector out, no extras."""
    if not embeddings.onnx_model_present():
        pytest.skip(
            "chroma's ONNX model files are not cached on this machine; "
            f"fetching them is the {embeddings.ONNX_DOWNLOAD_DOOR} door and "
            "this test does not open doors. Expected folder: "
            f"{embeddings.onnx_model_dir()}")
    vectors = embeddings.embed_documents(
        ["the vault holds the household's own documents"],
        embedding_function=embeddings.get_embedding_function())
    assert len(vectors) == 1
    assert len(vectors[0]) == 384  # all-MiniLM-L6-v2, same space as the legacy EF


def test_status_is_available_for_the_shipped_default(scratch):
    if not embeddings.onnx_model_present():
        pytest.skip("ONNX model files not cached here; see the skip above")
    status = embeddings.embedding_status()
    assert status["backend"] == embeddings.BACKEND_CHROMA_DEFAULT
    assert status["available"] is True
    assert status["message"] == ""


# ── 2. a missing backend is visible, never a silent empty ───────────────────

def _set_backend(name: str, monkeypatch):
    from puffpai.config import get_config

    cfg = get_config()
    monkeypatch.setattr(cfg.models, "embedding_backend", name, raising=False)
    return cfg


def test_missing_legacy_backend_says_what_to_install(scratch, monkeypatch):
    import importlib.util

    if importlib.util.find_spec("sentence_transformers") is not None:
        pytest.skip("sentence-transformers IS installed here, so there is no "
                    "missing-backend state to observe")
    _set_backend(embeddings.BACKEND_SENTENCE_TRANSFORMERS, monkeypatch)
    status = embeddings.embedding_status()
    assert status["available"] is False
    assert "embeddings-legacy" in status["message"]


def test_missing_ollama_embedder_says_the_pull_command(scratch, monkeypatch):
    """The upgrade backend, with the model absent: name the command, not 'no results'."""
    _set_backend(embeddings.BACKEND_OLLAMA, monkeypatch)
    from puffpai import brain_health

    monkeypatch.setattr(brain_health, "list_models", lambda *a, **k: ["gpt-oss:20b"])
    status = embeddings.embedding_status()
    assert status["available"] is False
    assert "ollama pull nomic-embed-text" in status["message"]


def test_unreachable_ollama_is_reported_not_swallowed(scratch, monkeypatch):
    _set_backend(embeddings.BACKEND_OLLAMA, monkeypatch)
    from puffpai import brain_health

    monkeypatch.setattr(brain_health, "list_models", lambda *a, **k: None)
    status = embeddings.embedding_status()
    assert status["available"] is False
    assert status["message"]


def test_unknown_backend_names_the_valid_ones(scratch, monkeypatch):
    _set_backend("magic-beans", monkeypatch)
    status = embeddings.embedding_status()
    assert status["available"] is False
    assert embeddings.BACKEND_CHROMA_DEFAULT in status["message"]


def test_missing_model_files_refuse_with_the_door_named(scratch, monkeypatch):
    """Files absent + door shut = a legible refusal, and chroma is never called.

    This is the offline path: a household that never opens the download door
    gets a sentence telling it how to copy the files over, not an empty answer.
    """
    monkeypatch.setattr(embeddings, "onnx_model_present", lambda: False)
    monkeypatch.setattr(embeddings, "_chroma_default_ef",
                        embeddings._chroma_default_ef)  # keep the real one
    from puffpai.security import egress_doors as ed

    monkeypatch.setattr(ed, "egress_allowed", lambda door_id: (False, "shut"))
    with pytest.raises(RuntimeError) as excinfo:
        embeddings.get_embedding_function()
    assert embeddings.ONNX_DOWNLOAD_DOOR in str(excinfo.value)

    status = embeddings.embedding_status()
    assert status["available"] is False
    assert embeddings.ONNX_DOWNLOAD_DOOR in status["message"]


def test_the_download_is_a_shut_door(scratch):
    """Fetching the model files is enumerated egress like everything else."""
    from puffpai.security.egress_doors import door

    d = door(embeddings.ONNX_DOWNLOAD_DOOR)
    assert d.default is False
    assert d.hosts == ("chroma-onnx-models.s3.amazonaws.com",)
    assert d.data_sent and d.settings_hint


# ── 3. the wizard writes the choice down ────────────────────────────────────

def test_wizard_writes_the_backend_explicitly(scratch):
    from puffpai import setup_ops

    path = setup_ops.set_embedding_config(embeddings.BACKEND_CHROMA_DEFAULT)
    written = json.loads(Path(path).read_text(encoding="utf-8"))
    assert written["models"]["embedding_backend"] == embeddings.BACKEND_CHROMA_DEFAULT


def test_wizard_records_the_ollama_upgrade_with_its_model(scratch):
    from puffpai import setup_ops

    path = setup_ops.set_embedding_config(embeddings.BACKEND_OLLAMA,
                                          "nomic-embed-text")
    written = json.loads(Path(path).read_text(encoding="utf-8"))
    assert written["models"]["embedding_backend"] == embeddings.BACKEND_OLLAMA
    assert written["models"]["ollama_embedding_model"] == "nomic-embed-text"


def test_upgrade_is_offered_only_when_the_model_is_already_there(scratch):
    from puffpai import setup_ops

    assert setup_ops.embedding_options([])["upgrade"] == ""
    assert setup_ops.embedding_options(["gpt-oss:20b"])["upgrade"] == ""
    offered = setup_ops.embedding_options(["nomic-embed-text:latest"])
    assert offered["upgrade"] == embeddings.BACKEND_OLLAMA
    assert offered["default"] == embeddings.BACKEND_CHROMA_DEFAULT


# ── 4. knowledge packs: absence is not a limitation ─────────────────────────

def test_shipped_config_ships_no_zim_directory():
    """A stranger's install has no /data/puffpai/knowledge/zim, and saying it
    does made every fresh box report a knowledge folder it did not have."""
    raw = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    assert raw["paths"]["zim_dirs"] == []


def test_no_zim_dirs_means_no_packs_yet_not_an_error(scratch):
    from puffpai.knowledge.discovery import discover_zim_files

    assert discover_zim_files(zim_dirs=[], zim_files=[]) == []
    assert discover_zim_files(zim_dirs=None, zim_files=None) == []


# ── 5. the download door holds wherever chroma could fetch the model ───────
#
# chroma's ONNX function downloads its model files itself, on its own socket,
# the first time it is CALLED with them missing. So the door has to be asked
# before chroma is handed any text, by every path that hands it some. None of
# these tests reaches the network: the model folder is a tmp dir and chroma's
# download step is replaced by a recorder that raises.

@pytest.fixture
def onnx_cache(tmp_path, monkeypatch):
    """chroma's model folder repointed at an empty tmp dir, its fetch recorded.

    ``onnx_model_dir()`` and chroma's own ``_download_model_if_not_exists``
    both read ``ONNXMiniLM_L6_V2.DOWNLOAD_PATH``, so one patch moves both.
    Returns ``(folder, fetches)``: write files into ``folder`` to make the
    model "present"; ``fetches`` gets one entry each time chroma reaches the
    step that would download.
    """
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
        ONNXMiniLM_L6_V2)

    root = tmp_path / "onnx_models" / "all-MiniLM-L6-v2"
    monkeypatch.setattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", root)
    fetches = []

    def _no_fetch(self):
        fetches.append(str(self.DOWNLOAD_PATH))
        raise RuntimeError("test stub: chroma reached its model download")

    monkeypatch.setattr(ONNXMiniLM_L6_V2, "_download_model_if_not_exists",
                        _no_fetch)
    folder = root / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME
    assert embeddings.onnx_model_dir() == str(folder)
    assert not embeddings.onnx_model_present()
    return folder, fetches


def _make_present(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for name in embeddings._ONNX_FILES:
        (folder / name).write_text("stub", encoding="utf-8")


def _door(monkeypatch, *, open_=False, boom=False):
    """Set the download door's answer; returns the list of door ids asked."""
    from puffpai.security import egress_doors as ed

    asked = []

    def _allowed(door_id):
        asked.append(door_id)
        if boom:
            raise RuntimeError("registry unavailable")
        return bool(open_), "test"

    monkeypatch.setattr(ed, "egress_allowed", _allowed)
    return asked


def test_our_file_check_is_the_one_chroma_uses():
    """If chroma would still download when we say "present", the gate lies.

    chroma checks a list of names under DOWNLOAD_PATH/EXTRACTED_FOLDER_NAME and
    downloads when any is missing; ``onnx_model_present`` must ask about the
    same files in the same folder. Read off chroma's own source, so an upgrade
    that adds a file fails here rather than fetching past the gate.
    """
    import ast
    import inspect
    import textwrap

    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
        ONNXMiniLM_L6_V2)

    source = textwrap.dedent(
        inspect.getsource(ONNXMiniLM_L6_V2._download_model_if_not_exists))
    lists = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Assign)
             and any(getattr(t, "id", "") == "onnx_files" for t in node.targets)]
    assert len(lists) == 1, "chroma's file check moved; re-read it"
    chroma_files = tuple(ast.literal_eval(lists[0].value))
    assert sorted(chroma_files) == sorted(embeddings._ONNX_FILES)
    assert "self.DOWNLOAD_PATH, self.EXTRACTED_FOLDER_NAME" in source


def test_files_present_may_run_without_asking_the_door(onnx_cache,
                                                       monkeypatch):
    folder, _fetches = onnx_cache
    _make_present(folder)
    asked = _door(monkeypatch, open_=False)
    assert embeddings.chroma_default_may_run() is True
    assert asked == [], "nothing to fetch, so there is nothing to ask"


def test_files_absent_and_door_shut_may_not_run(onnx_cache, monkeypatch):
    asked = _door(monkeypatch, open_=False)
    assert embeddings.chroma_default_may_run() is False
    assert asked == [embeddings.ONNX_DOWNLOAD_DOOR]


def test_files_absent_and_door_open_may_run(onnx_cache, monkeypatch):
    _door(monkeypatch, open_=True)
    assert embeddings.chroma_default_may_run() is True


def test_a_door_check_that_raises_counts_as_shut(onnx_cache, monkeypatch):
    _door(monkeypatch, boom=True)
    assert embeddings.chroma_default_may_run() is False
    with pytest.raises(RuntimeError) as excinfo:
        embeddings._chroma_default_ef()
    assert embeddings.ONNX_DOWNLOAD_DOOR in str(excinfo.value)


def test_the_built_function_asks_again_on_every_call(onnx_cache, monkeypatch):
    """Built while the files were here, called after they went: no fetch.

    chroma's DefaultEmbeddingFunction makes a fresh ONNX function per call and
    that one downloads whatever is missing, so a check made only at build time
    would be a check made once, for good.
    """
    from chromadb.api.types import DefaultEmbeddingFunction

    folder, fetches = onnx_cache
    _make_present(folder)
    _door(monkeypatch, open_=False)
    ef = embeddings._chroma_default_ef()
    # Persists exactly what chroma's own default does.
    assert isinstance(ef, DefaultEmbeddingFunction)
    assert ef.name() == DefaultEmbeddingFunction.name()
    assert ef.get_config() == DefaultEmbeddingFunction().get_config()

    for name in embeddings._ONNX_FILES:
        (folder / name).unlink()
    with pytest.raises(RuntimeError) as excinfo:
        ef(["is anything left to fetch"])
    assert embeddings.ONNX_DOWNLOAD_DOOR in str(excinfo.value)
    assert fetches == [], "chroma must not reach its download behind a shut door"

    # With the door opened, the same function hands over to chroma.
    _door(monkeypatch, open_=True)
    with pytest.raises(RuntimeError, match="test stub"):
        ef(["now it may"])
    assert len(fetches) == 1


class _Rag:
    def __init__(self, client):
        self.client = client


@pytest.fixture
def rag_fallback(onnx_cache, tmp_path, monkeypatch):
    """``_get_rag_context`` over a REAL chroma collection, embedder dead.

    The collection persisted chroma's default function (what every legacy
    collection resolves to), its one chunk was written with explicit vectors,
    and the configured embedder raises, so the per-collection ``query_texts=``
    fallback is the only thing that could hand chroma text.
    """
    import chromadb

    import puffpai.api_server as api

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    coll = client.create_collection(name="legacy_moon_library")
    coll.add(ids=["c1"], documents=["the moon landing was in 1969"],
             embeddings=[[0.1] * 384])

    (tmp_path / "indexed_libraries.json").write_text(json.dumps([
        {"collection": "legacy_moon_library", "library": "Legacy"}]),
        encoding="utf-8")
    monkeypatch.setattr(api, "_SRC_DIR", str(tmp_path))
    monkeypatch.setattr(api, "_get_dual_zone_rag", lambda: _Rag(client))

    def _dead_embedder():
        def _embed(_texts):
            raise RuntimeError("embedding backend is down")
        return _embed

    monkeypatch.setattr(api, "_get_query_embedder", _dead_embedder)
    monkeypatch.setattr(api, "_embedding_status", lambda *a, **k: {})
    monkeypatch.setattr(api, "_get_zim_fts_results", lambda q, max_hits=5: [])
    monkeypatch.setattr("puffpai.ingestion.query_user_uploads",
                        lambda q, n_results=5: [], raising=False)
    folder, fetches = onnx_cache
    return api, folder, fetches


def test_rag_fallback_never_fetches_behind_a_shut_door(rag_fallback,
                                                       monkeypatch, caplog):
    """The commonest trigger: files absent, door shut, so the embedder failed."""
    api, _folder, fetches = rag_fallback
    asked = _door(monkeypatch, open_=False)
    with caplog.at_level("WARNING"):
        api._get_rag_context("when was the moon landing")
    assert fetches == [], "the fallback handed chroma text behind a shut door"
    assert asked == [embeddings.ONNX_DOWNLOAD_DOOR], "asked once, not per collection"
    assert any(embeddings.ONNX_DOWNLOAD_DOOR in r.getMessage()
               for r in caplog.records), "the skip says which door"


def test_rag_fallback_fails_closed_when_the_door_check_raises(rag_fallback,
                                                              monkeypatch):
    api, _folder, fetches = rag_fallback
    _door(monkeypatch, boom=True)
    api._get_rag_context("when was the moon landing")
    assert fetches == []


def test_rag_fallback_still_runs_when_the_files_are_here(rag_fallback,
                                                         monkeypatch):
    """Nothing to fetch, so the degraded path is offline and keeps working."""
    api, folder, fetches = rag_fallback
    _make_present(folder)
    _door(monkeypatch, open_=False)
    api._get_rag_context("when was the moon landing")
    assert len(fetches) == 1, "the fallback should have reached chroma"


def test_rag_fallback_may_run_with_the_door_open(rag_fallback, monkeypatch):
    api, _folder, fetches = rag_fallback
    _door(monkeypatch, open_=True)
    api._get_rag_context("when was the moon landing")
    assert len(fetches) == 1
