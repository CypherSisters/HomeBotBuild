"""Tests for scripts/reindex.py — knowledge base re-indexer."""

import importlib.util
import os
import sys

import pytest
from unittest.mock import MagicMock, patch, call

# Load reindex.py as a module (it lives in scripts/, not a package)
_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "reindex.py",
)
_spec = importlib.util.spec_from_file_location("reindex", _SCRIPT_PATH)
reindex = importlib.util.module_from_spec(_spec)

# Prevent the top-level 'from puffpai.knowledge...' from failing in
# environments where chromadb isn't installed — we mock everything below.
# The sys.path insert in the script ensures puffpai is importable via conftest.
_spec.loader.exec_module(reindex)


class TestCountChunks:
    """count_chunks() helper."""

    def test_returns_count_from_collection(self):
        mock_client = MagicMock()
        mock_coll = MagicMock()
        mock_coll.count.return_value = 42
        mock_client.get_collection.return_value = mock_coll

        assert reindex.count_chunks(mock_client, "my_collection") == 42
        mock_client.get_collection.assert_called_once_with(name="my_collection")

    def test_returns_zero_on_error(self):
        mock_client = MagicMock()
        mock_client.get_collection.side_effect = Exception("not found")

        assert reindex.count_chunks(mock_client, "missing") == 0


class TestMatchZimToLibrary:
    """match_zim_to_library() matching logic."""

    SAMPLE_LIBS = [
        {
            "collection": "kiwix_wikipedia_en_all_mini_2025_06",
            "library": "Wikipedia",
            "library_id": "wikipedia_en_all_mini_2025-06",
        },
        {
            "collection": "kiwix_rationalwiki_en_all_nopic_2025_05",
            "library": "RationalWiki",
            "library_id": "rationalwiki_en_all_nopic_2025-05",
        },
    ]

    def test_matches_by_library_id(self):
        """ZIM filename that matches a library_id returns that library."""
        result = reindex.match_zim_to_library(
            "/mnt/zim/wikipedia_en_all_mini_2025-06.zim",
            self.SAMPLE_LIBS,
        )
        assert result is not None
        assert result["library"] == "Wikipedia"

    def test_matches_with_underscores_for_hyphens(self):
        """Hyphens and underscores are treated as equivalent."""
        result = reindex.match_zim_to_library(
            "F:\\rationalwiki_en_all_nopic_2025_05.zim",
            self.SAMPLE_LIBS,
        )
        assert result is not None
        assert result["library"] == "RationalWiki"

    def test_returns_none_for_no_match(self):
        """Unknown ZIM file returns None."""
        result = reindex.match_zim_to_library(
            "/mnt/zim/totally_unknown_file.zim",
            self.SAMPLE_LIBS,
        )
        assert result is None


class TestLoadLibraries:
    """load_libraries() reads indexed_libraries.json."""

    def test_loads_json_list(self):
        """Returns a list of library dicts from the manifest file."""
        libs = reindex.load_libraries()
        assert isinstance(libs, list)
        assert len(libs) > 0
        # Each entry should have the required keys
        for lib in libs:
            assert "collection" in lib
            assert "library" in lib
            assert "library_id" in lib


class TestReembedCollection:
    """reembed_collection() — the destructive half of the J2 re-index.

    The invariant under test: no failure path may leave the primary
    collection silently truncated.
    """

    @staticmethod
    def _chroma():
        """Real in-memory-ish chroma client, or skip."""
        chromadb = pytest.importorskip("chromadb")
        import tempfile
        return chromadb.PersistentClient(path=tempfile.mkdtemp())

    @staticmethod
    def _seed(client, name="user_uploads", rows=5):
        coll = client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"})
        coll.add(
            ids=[f"i{n}" for n in range(rows)],
            documents=[f"doc {n}" for n in range(rows)],
            metadatas=[{"filename": f"f{n}.txt"} for n in range(rows)],
            embeddings=[[float(n)] * 8 for n in range(rows)],
        )
        return coll

    def test_missing_collection_reports_missing(self):
        client = self._chroma()
        assert reindex.reembed_collection(client, "nope_not_here")[0] == "missing"

    def test_empty_collection_reports_empty(self):
        client = self._chroma()
        client.get_or_create_collection(name="emptycoll")
        assert reindex.reembed_collection(client, "emptycoll")[0] == "empty"

    def test_failure_midway_leaves_original_intact(self):
        """An embed failure part-way through must NOT truncate the original."""
        client = self._chroma()
        self._seed(client, rows=5)

        calls = {"n": 0}

        def flaky(texts, embedding_function=None):
            calls["n"] += 1
            if calls["n"] > 1:          # first batch OK, then blow up
                raise RuntimeError("embedder died")
            return [[0.5] * 8 for _ in texts]

        with patch("puffpai.embeddings.embed_documents", flaky):
            with pytest.raises(RuntimeError):
                reindex.reembed_collection(client, "user_uploads", batch_size=2)

        # Original still complete and untouched.
        survivor = client.get_collection(name="user_uploads")
        assert survivor.count() == 5
        # The partial copy is parked under an obvious name, not merged in.
        names = [c.name for c in client.list_collections()]
        assert "user_uploads" + reindex.STAGING_SUFFIX in names

    def test_retry_after_failure_succeeds_and_clears_staging(self):
        """A stale staging collection must not block or corrupt a retry."""
        client = self._chroma()
        self._seed(client, rows=5)
        # Leave a stale staging collection behind, as an aborted run would.
        client.get_or_create_collection(
            name="user_uploads" + reindex.STAGING_SUFFIX)

        def ok(texts, embedding_function=None):
            return [[0.25] * 8 for _ in texts]

        with patch("puffpai.embeddings.embed_documents", ok):
            status, rebuilt, total = reindex.reembed_collection(
                client, "user_uploads", batch_size=2)

        assert (status, rebuilt, total) == ("ok", 5, 5)
        final = client.get_collection(name="user_uploads")
        assert final.count() == 5
        # Distance space and metadata survive the swap.
        assert (final.metadata or {}).get("hnsw:space") == "cosine"
        assert final.get(ids=["i3"])["metadatas"][0]["filename"] == "f3.txt"
        # Staging cleaned up.
        assert [c.name for c in client.list_collections()] == ["user_uploads"]


class TestReembedConfirmationGate:
    """--reembed refuses to run without --yes, and reports failures."""

    def test_refuses_without_yes(self):
        import types
        args = types.SimpleNamespace(yes=False)
        # Must not even construct a client, let alone touch a collection.
        with patch.object(reindex, "DualZoneRAG") as rag:
            assert reindex.cmd_reembed(args) == 1
            rag.assert_not_called()

    def test_nonzero_exit_when_a_collection_fails(self):
        import types
        args = types.SimpleNamespace(yes=True)
        with patch.object(reindex, "DualZoneRAG"), \
             patch.object(reindex, "reembed_collection",
                          side_effect=RuntimeError("staged 2 of 5")):
            assert reindex.cmd_reembed(args) == 1

    def test_zero_exit_when_all_succeed(self):
        import types
        args = types.SimpleNamespace(yes=True)
        with patch.object(reindex, "DualZoneRAG"), \
             patch.object(reindex, "reembed_collection",
                          return_value=("ok", 5, 5)):
            assert reindex.cmd_reembed(args) == 0
