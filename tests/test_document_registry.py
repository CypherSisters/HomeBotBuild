"""Tests for puffpai.knowledge.registry — document metadata tracker."""

import json
import os
import pytest
from unittest.mock import MagicMock

from puffpai.knowledge.registry import DocumentRegistry
from puffpai.knowledge.ingestion import (
    IngestedDocument, ContentSource, TargetZone,
)


def _make_doc(doc_id="doc_abc123", title="Test Doc", chunk_count=5,
              source=ContentSource.DOCUMENT_UPLOAD,
              zone=TargetZone.VAULT, has_pii=False,
              file_origin="/tmp/test.txt"):
    """Create a fake IngestedDocument for testing."""
    return IngestedDocument(
        doc_id=doc_id,
        title=title,
        source=source,
        target_zone=zone,
        chunk_count=chunk_count,
        has_pii=has_pii,
        ingested_at="2026-03-03T12:00:00",
        file_origin=file_origin,
    )


class TestDocumentRegistry:
    """DocumentRegistry CRUD operations."""

    def test_register_and_list(self, tmp_path):
        """Register a document and retrieve it in list."""
        reg = DocumentRegistry(str(tmp_path / "registry.json"))
        doc = _make_doc()
        reg.register(doc)

        docs = reg.list_documents()
        assert len(docs) == 1
        assert docs[0]['doc_id'] == "doc_abc123"
        assert docs[0]['title'] == "Test Doc"
        assert docs[0]['chunk_count'] == 5

    def test_persists_to_disk(self, tmp_path):
        """Registry file is created on disk after register."""
        path = str(tmp_path / "registry.json")
        reg = DocumentRegistry(path)
        reg.register(_make_doc())

        assert os.path.exists(path)
        with open(path, 'r') as f:
            data = json.load(f)
        assert len(data) == 1

    def test_loads_from_existing_file(self, tmp_path):
        """A new DocumentRegistry instance loads existing data."""
        path = str(tmp_path / "registry.json")
        reg1 = DocumentRegistry(path)
        reg1.register(_make_doc())

        reg2 = DocumentRegistry(path)
        assert reg2.count == 1
        assert reg2.list_documents()[0]['doc_id'] == "doc_abc123"

    def test_search_by_title(self, tmp_path):
        """search() finds documents by title substring."""
        reg = DocumentRegistry(str(tmp_path / "registry.json"))
        reg.register(_make_doc(doc_id="d1", title="Python Programming"))
        reg.register(_make_doc(doc_id="d2", title="Medical Notes"))
        reg.register(_make_doc(doc_id="d3", title="Python Tutorial"))

        results = reg.search("python")
        assert len(results) == 2
        titles = {r['title'] for r in results}
        assert "Python Programming" in titles
        assert "Python Tutorial" in titles

    def test_delete_removes_document(self, tmp_path):
        """delete() removes a document and persists the change."""
        reg = DocumentRegistry(str(tmp_path / "registry.json"))
        reg.register(_make_doc(doc_id="d1", title="First"))
        reg.register(_make_doc(doc_id="d2", title="Second"))

        assert reg.count == 2
        deleted = reg.delete("d1")
        assert deleted is True
        assert reg.count == 1
        assert reg.get("d1") is None
        assert reg.get("d2") is not None

    def test_delete_nonexistent_returns_false(self, tmp_path):
        """delete() returns False for unknown doc_id."""
        reg = DocumentRegistry(str(tmp_path / "registry.json"))
        assert reg.delete("nonexistent") is False

    def test_filter_by_zone(self, tmp_path):
        """list_documents(zone=...) filters by zone."""
        reg = DocumentRegistry(str(tmp_path / "registry.json"))
        reg.register(_make_doc(doc_id="v1", zone=TargetZone.VAULT))
        reg.register(_make_doc(doc_id="k1", zone=TargetZone.KNOWLEDGE))

        vault_docs = reg.list_documents(zone="puffpai_vault")
        assert len(vault_docs) == 1
        assert vault_docs[0]['doc_id'] == "v1"

        kb_docs = reg.list_documents(zone="puffpai_knowledge")
        assert len(kb_docs) == 1
        assert kb_docs[0]['doc_id'] == "k1"

    def test_total_chunks(self, tmp_path):
        """total_chunks property sums all chunk counts."""
        reg = DocumentRegistry(str(tmp_path / "registry.json"))
        reg.register(_make_doc(doc_id="d1", chunk_count=10))
        reg.register(_make_doc(doc_id="d2", chunk_count=25))

        assert reg.total_chunks == 35
