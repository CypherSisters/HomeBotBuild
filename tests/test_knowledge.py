"""Tests for the knowledge package (ZIM reader, ingestion, dual-zone RAG)."""

import os
import struct
import pytest
from unittest.mock import MagicMock, patch

from puffpai.knowledge.ingestion import (
    KnowledgeIngestion, ContentSource, TargetZone, IngestedDocument, TextChunk,
)
from puffpai.knowledge.rag import (
    DualZoneRAG, RAGResult, RAGResponse, ResultSource,
)


# ── Ingestion Tests ─────────────────────────────────────────────────────────

class TestPIIDetection:
    def setup_method(self):
        self.ingestion = KnowledgeIngestion()

    def test_detects_email(self):
        assert self.ingestion._detect_pii("Contact me at john@example.com")

    def test_detects_phone(self):
        assert self.ingestion._detect_pii("Call me at 555-123-4567")

    def test_detects_ssn(self):
        assert self.ingestion._detect_pii("My SSN is 123-45-6789")

    def test_detects_credit_card(self):
        assert self.ingestion._detect_pii("Card: 4111 1111 1111 1111")

    def test_detects_self_identification(self):
        assert self.ingestion._detect_pii("My name is John")

    def test_no_pii_in_clean_text(self):
        assert not self.ingestion._detect_pii("Python is a programming language.")

    def test_detects_address(self):
        assert self.ingestion._detect_pii("I live at 123 Main St")


class TestChunking:
    def setup_method(self):
        self.ingestion = KnowledgeIngestion()

    def test_short_text_single_chunk(self):
        chunks = self.ingestion._chunk_text(
            "Short text.", "doc_123", "Title",
            ContentSource.MANUAL_ENTRY, TargetZone.KNOWLEDGE,
        )
        assert len(chunks) == 1
        assert chunks[0].text == "Short text."
        assert chunks[0].metadata['total_chunks'] == 1

    def test_long_text_multiple_chunks(self):
        # Create text longer than CHUNK_SIZE
        long_text = "\n\n".join([f"Paragraph {i}. " * 20 for i in range(20)])
        chunks = self.ingestion._chunk_text(
            long_text, "doc_456", "Long Doc",
            ContentSource.DOCUMENT_UPLOAD, TargetZone.VAULT,
        )
        assert len(chunks) > 1
        for c in chunks:
            assert c.metadata['zone'] == 'puffpai_vault'
            assert c.metadata['source'] == 'document_upload'

    def test_chunk_metadata_has_title(self):
        chunks = self.ingestion._chunk_text(
            "Some content.", "doc_789", "My Title",
            ContentSource.VOICE_DICTATION, TargetZone.VAULT,
        )
        assert chunks[0].metadata['title'] == "My Title"


class TestZoneRouting:
    def setup_method(self):
        self.ingestion = KnowledgeIngestion()

    def test_pii_routes_to_vault(self):
        doc = self.ingestion.ingest_text(
            "My email is john@example.com",
            title="Contact Info",
        )
        assert doc.target_zone == TargetZone.VAULT
        assert doc.has_pii is True

    def test_clean_text_routes_to_knowledge(self):
        doc = self.ingestion.ingest_text(
            "Python is a programming language created by Guido van Rossum.",
            title="Python Info",
        )
        assert doc.target_zone == TargetZone.KNOWLEDGE
        assert doc.has_pii is False

    def test_voice_dictation_routes_to_vault(self):
        doc = self.ingestion.ingest_text(
            "Buy groceries tomorrow.",
            title="Voice Note",
            source=ContentSource.VOICE_DICTATION,
        )
        assert doc.target_zone == TargetZone.VAULT

    def test_document_upload_routes_to_vault(self):
        doc = self.ingestion.ingest_text(
            "Meeting notes from today.",
            title="Meeting Notes",
            source=ContentSource.DOCUMENT_UPLOAD,
        )
        assert doc.target_zone == TargetZone.VAULT

    def test_force_zone_overrides(self):
        doc = self.ingestion.ingest_text(
            "My SSN is 123-45-6789",
            title="Sensitive",
            force_zone=TargetZone.KNOWLEDGE,  # Override vault routing
        )
        assert doc.target_zone == TargetZone.KNOWLEDGE

    def test_zim_pack_routes_to_knowledge(self):
        doc = self.ingestion.ingest_text(
            "General knowledge article about science.",
            title="Science Article",
            source=ContentSource.ZIM_PACK,
            force_zone=TargetZone.KNOWLEDGE,
        )
        assert doc.target_zone == TargetZone.KNOWLEDGE


class TestIngestionWithChromaDB:
    def test_stores_in_vault_collection(self):
        mock_vault = MagicMock()
        mock_knowledge = MagicMock()
        ingestion = KnowledgeIngestion(mock_vault, mock_knowledge)

        doc = ingestion.ingest_text(
            "My phone is 555-123-4567",
            title="Personal",
        )

        mock_vault.add.assert_called_once()
        mock_knowledge.add.assert_not_called()
        assert doc.target_zone == TargetZone.VAULT

    def test_stores_in_knowledge_collection(self):
        mock_vault = MagicMock()
        mock_knowledge = MagicMock()
        ingestion = KnowledgeIngestion(mock_vault, mock_knowledge)

        doc = ingestion.ingest_text(
            "The Python programming language was created in 1991.",
            title="Python Facts",
        )

        mock_knowledge.add.assert_called_once()
        mock_vault.add.assert_not_called()
        assert doc.target_zone == TargetZone.KNOWLEDGE


class TestDocId:
    def test_deterministic(self):
        ingestion = KnowledgeIngestion()
        id1 = ingestion._generate_doc_id("same text")
        id2 = ingestion._generate_doc_id("same text")
        assert id1 == id2

    def test_different_for_different_text(self):
        ingestion = KnowledgeIngestion()
        id1 = ingestion._generate_doc_id("text one")
        id2 = ingestion._generate_doc_id("text two")
        assert id1 != id2


# ── Dual-Zone RAG Tests ─────────────────────────────────────────────────────

class TestRAGResult:
    def test_vault_provenance_label(self):
        r = RAGResult(text="test", source=ResultSource.VAULT)
        assert r.provenance_label == "from your vault"

    def test_knowledge_provenance_label(self):
        r = RAGResult(text="test", source=ResultSource.KNOWLEDGE)
        assert r.provenance_label == "from knowledge base"


class TestRAGResponse:
    def test_context_string_includes_provenance(self):
        response = RAGResponse(
            query="test",
            results=[
                RAGResult(text="Personal data", source=ResultSource.VAULT,
                          title="My Notes", score=0.1),
                RAGResult(text="General info", source=ResultSource.KNOWLEDGE,
                          title="Wiki", score=0.2),
            ],
            vault_count=1,
            knowledge_count=1,
        )
        ctx = response.context_string()
        assert "From Your Vault" in ctx
        assert "From Knowledge Base" in ctx
        assert "My Notes" in ctx
        assert "Wiki" in ctx

    def test_vault_results_only(self):
        response = RAGResponse(
            query="test",
            results=[
                RAGResult(text="vault", source=ResultSource.VAULT, score=0.1),
                RAGResult(text="kb", source=ResultSource.KNOWLEDGE, score=0.2),
            ],
            vault_count=1,
            knowledge_count=1,
        )
        vault = response.vault_results_only()
        assert len(vault) == 1
        assert vault[0].text == "vault"


class TestDualZoneRAG:
    def _make_mock_collection(self, documents, distances=None, metadatas=None):
        """Create a mock ChromaDB collection."""
        mock = MagicMock()
        mock.query.return_value = {
            'documents': [documents],
            'distances': [distances or [0.1 * i for i in range(len(documents))]],
            'metadatas': [metadatas or [{'title': f'Doc {i}'} for i in range(len(documents))]],
        }
        return mock

    def test_unauthenticated_skips_vault(self):
        vault = self._make_mock_collection(["secret"])
        knowledge = self._make_mock_collection(["public info"])

        rag = DualZoneRAG(vault, knowledge)
        response = rag.query("test", authenticated=False)

        vault.query.assert_not_called()
        knowledge.query.assert_called_once()
        assert response.vault_count == 0
        assert response.knowledge_count == 1

    def test_authenticated_queries_both(self):
        vault = self._make_mock_collection(["personal"])
        knowledge = self._make_mock_collection(["general"])

        rag = DualZoneRAG(vault, knowledge)
        response = rag.query("test", authenticated=True)

        vault.query.assert_called_once()
        knowledge.query.assert_called_once()
        assert response.vault_count == 1
        assert response.knowledge_count == 1
        assert len(response.results) == 2

    def test_vault_only_mode(self):
        vault = self._make_mock_collection(["personal"])
        knowledge = self._make_mock_collection(["general"])

        rag = DualZoneRAG(vault, knowledge)
        response = rag.query("test", authenticated=True, vault_only=True)

        vault.query.assert_called_once()
        knowledge.query.assert_not_called()

    def test_knowledge_only_mode(self):
        vault = self._make_mock_collection(["personal"])
        knowledge = self._make_mock_collection(["general"])

        rag = DualZoneRAG(vault, knowledge)
        response = rag.query("test", knowledge_only=True)

        knowledge.query.assert_called_once()
        vault.query.assert_not_called()

    def test_results_sorted_by_score(self):
        vault = self._make_mock_collection(
            ["vault doc"], distances=[0.5])
        knowledge = self._make_mock_collection(
            ["kb doc"], distances=[0.1])

        rag = DualZoneRAG(vault, knowledge)
        response = rag.query("test", authenticated=True)

        # Knowledge result (0.1) should be before vault (0.5)
        assert response.results[0].source == ResultSource.KNOWLEDGE
        assert response.results[1].source == ResultSource.VAULT

    def test_handles_empty_collection(self):
        empty = MagicMock()
        empty.query.return_value = {'documents': [[]], 'distances': [[]], 'metadatas': [[]]}

        rag = DualZoneRAG(None, empty)
        response = rag.query("test")
        assert len(response.results) == 0

    def test_handles_collection_error(self):
        failing = MagicMock()
        failing.query.side_effect = Exception("DB error")

        rag = DualZoneRAG(None, failing)
        response = rag.query("test")
        assert len(response.results) == 0


# ── ChromaDB client property (backwards-compat) ───────────────────────────

class TestDualZoneRAGClient:
    """Verify the .client property provides a lazy chromadb.PersistentClient."""

    def test_client_with_explicit_db_path(self, tmp_path):
        """client property creates PersistentClient at the given db_path."""
        db_dir = str(tmp_path / "test_chroma")
        rag = DualZoneRAG(db_path=db_dir)
        client = rag.client
        # Should be a chromadb client
        assert hasattr(client, 'get_collection')
        assert hasattr(client, 'get_or_create_collection')
        assert hasattr(client, 'list_collections')

    def test_client_is_cached(self, tmp_path):
        """Accessing .client twice returns the same object (lazy singleton)."""
        db_dir = str(tmp_path / "test_chroma2")
        rag = DualZoneRAG(db_path=db_dir)
        client1 = rag.client
        client2 = rag.client
        assert client1 is client2

    def test_client_reads_config_when_no_db_path(self):
        """When db_path is None, client reads from puffpai config."""
        mock_client = MagicMock()
        with patch('chromadb.PersistentClient', return_value=mock_client) as mock_pc:
            rag = DualZoneRAG()  # no db_path
            _ = rag.client

            # Should have called PersistentClient with a path from config
            mock_pc.assert_called_once()
            call_args = mock_pc.call_args
            path_arg = call_args.kwargs.get('path') or call_args[1].get('path', call_args[0][0] if call_args[0] else None)
            assert path_arg is not None
            assert isinstance(path_arg, str)

    def test_client_get_or_create_collection_works(self, tmp_path):
        """Can create and query a collection through the client property."""
        db_dir = str(tmp_path / "test_chroma3")
        rag = DualZoneRAG(db_path=db_dir)
        collection = rag.client.get_or_create_collection(name="test_lib")
        collection.add(
            ids=["doc1"],
            documents=["The quick brown fox"],
            metadatas=[{"title": "Test"}],
        )
        results = collection.query(query_texts=["fox"], n_results=1)
        assert len(results['documents'][0]) == 1
        assert "fox" in results['documents'][0][0]


# ── Retired RAG modules stay retired ──────────────────────────────────────

class TestOldRAGRetired:
    """Verify old kiwix_rag.py and langchain_rag.py are retired."""

    def test_no_kiwix_rag_imports_in_source(self):
        """No module in src/puffpai/ should import from kiwix_rag."""
        import glob
        src_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai",
        )
        violations = []
        for py_file in glob.glob(os.path.join(src_dir, "**", "*.py"), recursive=True):
            if "_deprecated_" in py_file:
                continue
            with open(py_file, "r", encoding="utf-8") as f:
                content = f.read()
            if "from kiwix_rag import" in content or "import kiwix_rag" in content:
                violations.append(os.path.basename(py_file))
        assert violations == [], f"Files still import kiwix_rag: {violations}"

    def test_no_langchain_rag_imports_in_source(self):
        """No module in src/puffpai/ should import from langchain_rag."""
        import glob
        src_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai",
        )
        violations = []
        for py_file in glob.glob(os.path.join(src_dir, "**", "*.py"), recursive=True):
            if "_deprecated_" in py_file:
                continue
            with open(py_file, "r", encoding="utf-8") as f:
                content = f.read()
            if "from langchain_rag import" in content or "import langchain_rag" in content:
                violations.append(os.path.basename(py_file))
        assert violations == [], f"Files still import langchain_rag: {violations}"

    def test_init_exports_dual_zone_rag(self):
        """puffpai.__init__ should export DualZoneRAG."""
        import puffpai
        assert "DualZoneRAG" in puffpai.__all__

    def test_init_does_not_export_kiwix_rag(self):
        """puffpai.__init__ should not export KiwixRAG or LangChainRAG."""
        import puffpai
        assert "KiwixRAG" not in puffpai.__all__
        assert "LangChainRAG" not in puffpai.__all__

    def test_assistant_tools_uses_dual_zone_rag(self):
        """assistant_tools.py search_knowledge_base uses DualZoneRAG."""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "puffpai", "assistant_tools.py",
        )
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "DualZoneRAG" in source
        assert "from kiwix_rag import" not in source
