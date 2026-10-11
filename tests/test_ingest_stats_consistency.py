"""/ingest/stats must agree with itself and with /ingest/list.

Without that contract the endpoints can disagree like this:

    /ingest/stats -> document_count: 12, indexed_document_count: 13,
                     by_store.ingestion: {documents: 0, indexed_documents: 1}
    /ingest/search?q=... returns patient_record.txt
    /ingest/list does NOT list patient_record.txt

Three numbers for two stores, and a document the search index can return that
the list denied existed. The fixture below reproduces exactly that shape: one
document present in the SEARCH INDEX and in neither manifest.

Contract pinned here:
  * every document the index can return appears in /ingest/list;
  * document_count == len(the list);
  * one labelled count per store, and the labelled counts add up.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

#: The knowledge-upload manifest, live shape (reference packs carry no
#: ``uploaded_at``; user uploads do).
MANIFEST = [
    {"library": "wikipedia_en_simple_all_maxi_2025_09",
     "collection": "wikipedia_en_simple", "chunks": 900000},
    {"library": "Strength Program.docx", "collection": "sp",
     "chunks": 44, "uploaded_at": "2026-08-14T10:00:00"},
    {"library": "Stretching Guide.docx", "collection": "sg",
     "chunks": 24, "uploaded_at": "2026-08-14T10:05:00"},
]

#: The user_uploads search index: two chunks of a document NEITHER manifest
#: lists, the situation an orphaned upload such as patient_record.txt is in.
ORPHAN_META = [
    {"file_id": "20260901_120000_patient_record.txt",
     "filename": "patient_record.txt", "classification": "study",
     "indexed_at": "2026-09-01T12:00:00", "chunk_index": 0, "total_chunks": 2,
     "source": "user_upload"},
    {"file_id": "20260901_120000_patient_record.txt",
     "filename": "patient_record.txt", "classification": "study",
     "indexed_at": "2026-09-01T12:00:00", "chunk_index": 1, "total_chunks": 2,
     "source": "user_upload"},
]


class _Collection:
    def __init__(self, metadatas):
        self.metadatas = metadatas

    def count(self):
        return len(self.metadatas)

    def get(self, include=None, **kwargs):
        return {"metadatas": list(self.metadatas)}


@pytest.fixture()
def box(monkeypatch):
    """The live shape: empty ingestion catalog, 2 manifest uploads, 1 orphan."""
    import puffpai.ingestion as ingestion
    monkeypatch.setattr(api, "_load_library_manifest", lambda: list(MANIFEST))
    monkeypatch.setattr(ingestion, "list_ingested_docs", lambda: [])
    monkeypatch.setattr(ingestion, "_get_user_uploads_collection",
                        lambda: _Collection(ORPHAN_META))


def _stats():
    with api.app.test_request_context('/ingest/stats'):
        return api.ingest_stats().get_json()


def _listed():
    with api.app.test_request_context('/ingest/list'):
        return api.ingest_list().get_json()['documents']


def test_a_document_only_the_index_knows_about_is_listed(box):
    names = {d["filename"] for d in _listed()}
    assert "patient_record.txt" in names, (
        "/ingest/search can return it but /ingest/list still denies it")
    assert names == {"Strength Program.docx", "Stretching Guide.docx",
                     "patient_record.txt"}


def test_the_orphan_is_labelled_with_the_store_it_came_from(box):
    orphan = next(d for d in _listed()
                  if d["filename"] == "patient_record.txt")
    assert orphan["store"] == "index_only"
    assert orphan["retrievable"] is True
    assert orphan["chunk_count"] == 2


def test_document_count_equals_the_list_length(box):
    assert _stats()["document_count"] == len(_listed()) == 3


def test_the_two_headline_counts_no_longer_disagree(box):
    stats = _stats()
    assert stats["document_count"] == stats["indexed_document_count"] == 3
    assert stats["fully_indexed"] is True


def test_one_labelled_count_per_store_and_they_add_up(box):
    stats = _stats()
    assert stats["catalog_documents"] == 0
    assert stats["upload_manifest_documents"] == 2
    assert stats["index_only_documents"] == 1
    assert stats["index_documents"] == 1
    assert stats["index_chunks"] == 2
    assert (stats["catalog_documents"] + stats["upload_manifest_documents"]
            + stats["index_only_documents"]) == stats["document_count"]


def test_by_store_matches_the_labelled_counts(box):
    stats = _stats()
    by = stats["by_store"]
    assert by["ingestion"]["documents"] == stats["catalog_documents"]
    assert by["knowledge_upload"]["documents"] == \
        stats["upload_manifest_documents"]
    assert by["index_only"]["documents"] == stats["index_only_documents"]
    # The by_store columns are a PARTITION, so each adds up to its own
    # labelled total. The failure this catches: `ingestion` reporting the
    # WHOLE index and `index_only` reporting the same chunk again, so the
    # indexed column sums to one more than indexed_document_count and the
    # chunk column to one more than chunk_count.
    assert sum(v["documents"] for v in by.values()) == stats["document_count"]
    assert sum(v["chunks"] for v in by.values()) == stats["chunk_count"]
    assert sum(v["indexed_documents"] for v in by.values()) == \
        stats["indexed_document_count"]
    # index_only is by definition the slice of the index no manifest
    # lists, so the ingestion store owns the index MINUS that slice.
    assert by["ingestion"]["chunks"] == \
        stats["index_chunks"] - by["index_only"]["chunks"]
    # The live contradiction: documents 0 while indexed_documents 1.
    assert by["ingestion"]["indexed_documents"] <= \
        by["ingestion"]["documents"] + stats["index_only_documents"]


def test_a_catalogued_document_is_not_double_counted(monkeypatch):
    """The same file in the catalog AND the index is ONE document."""
    import puffpai.ingestion as ingestion
    monkeypatch.setattr(api, "_load_library_manifest", lambda: [])
    monkeypatch.setattr(ingestion, "list_ingested_docs", lambda: [
        {"id": "20260901_120000_patient_record.txt",
         "filename": "patient_record.txt", "classification": "study",
         "indexed_at": "2026-09-01T12:00:00", "word_count": 40}])
    monkeypatch.setattr(ingestion, "_get_user_uploads_collection",
                        lambda: _Collection(ORPHAN_META))
    listed = _listed()
    assert len(listed) == 1
    assert listed[0]["store"] == "ingestion"
    stats = _stats()
    assert stats["document_count"] == 1
    assert stats["catalog_documents"] == 1
    assert stats["index_only_documents"] == 0
    assert stats["indexed_document_count"] == 1


def test_an_unreadable_index_does_not_break_the_list(monkeypatch):
    import puffpai.ingestion as ingestion
    monkeypatch.setattr(api, "_load_library_manifest", lambda: list(MANIFEST))
    monkeypatch.setattr(ingestion, "list_ingested_docs", lambda: [])

    def _boom():
        raise RuntimeError("chroma down")

    monkeypatch.setattr(ingestion, "_get_user_uploads_collection", _boom)
    names = {d["filename"] for d in _listed()}
    assert names == {"Strength Program.docx", "Stretching Guide.docx"}
    assert _stats()["document_count"] == 2
