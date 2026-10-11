"""Tests for DualZoneRAG.health_check() and scripts/chromadb_health.py."""

import pytest
from unittest.mock import MagicMock, patch

from puffpai.knowledge.rag import DualZoneRAG


class TestHealthCheck:
    """DualZoneRAG.health_check() method."""

    def _make_libraries(self, names: list[str]) -> list[dict]:
        """Build a minimal libraries list for testing."""
        return [
            {"collection": f"kiwix_{name}", "library": name, "library_id": name}
            for name in names
        ]

    def test_healthy_collection(self, tmp_path):
        """A collection with enough chunks and successful query is 'healthy'."""
        db_dir = str(tmp_path / "chroma_health")
        rag = DualZoneRAG(db_path=db_dir)

        # Seed a collection with 300+ chunks
        coll = rag.client.get_or_create_collection(name="kiwix_wiki")
        ids = [f"doc_{i}" for i in range(350)]
        docs = [f"Article about topic number {i}" for i in range(350)]
        metas = [{"title": f"Topic {i}"} for i in range(350)]
        coll.add(ids=ids, documents=docs, metadatas=metas)

        libs = self._make_libraries(["wiki"])
        report = rag.health_check(libraries=libs)

        assert len(report) == 1
        assert report[0]['status'] == 'healthy'
        assert report[0]['chunks'] == 350
        assert report[0]['queryable'] is True
        assert report[0]['error'] is None

    def test_empty_collection(self, tmp_path):
        """A collection with 0 chunks is 'empty'."""
        db_dir = str(tmp_path / "chroma_empty")
        rag = DualZoneRAG(db_path=db_dir)

        # Create but don't populate
        rag.client.get_or_create_collection(name="kiwix_empty")
        libs = self._make_libraries(["empty"])
        report = rag.health_check(libraries=libs)

        assert len(report) == 1
        assert report[0]['status'] == 'empty'
        assert report[0]['chunks'] == 0
        assert report[0]['queryable'] is False

    def test_missing_collection_is_error(self, tmp_path):
        """A collection that doesn't exist at all is 'error'."""
        db_dir = str(tmp_path / "chroma_missing")
        rag = DualZoneRAG(db_path=db_dir)

        libs = self._make_libraries(["nonexistent"])
        report = rag.health_check(libraries=libs)

        assert len(report) == 1
        assert report[0]['status'] == 'error'
        assert report[0]['error'] is not None

    def test_partial_collection(self, tmp_path):
        """A collection with < skip_threshold chunks is 'partial'."""
        db_dir = str(tmp_path / "chroma_partial")
        rag = DualZoneRAG(db_path=db_dir)

        coll = rag.client.get_or_create_collection(name="kiwix_small")
        coll.add(
            ids=["doc_0", "doc_1", "doc_2"],
            documents=["Hello world", "Python programming", "Test data"],
            metadatas=[{"title": "A"}, {"title": "B"}, {"title": "C"}],
        )

        libs = self._make_libraries(["small"])
        report = rag.health_check(libraries=libs)

        assert len(report) == 1
        assert report[0]['status'] == 'partial'
        assert report[0]['chunks'] == 3
        assert report[0]['queryable'] is True
