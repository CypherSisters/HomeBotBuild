"""/ingest/list and /ingest/stats must agree with what RAG retrieves.

The failure to prevent: ``GET /ingest/list`` -> ``{"documents":[]}`` and
``GET /ingest/stats`` -> ``{"chunk_count":1,"document_count":0,"indexed_document_
count":1}`` in the same session where RAG cites about ten real "Your upload: X"
filenames.

There are TWO upload stores. ``puffpai.ingestion`` keeps a JSON catalog, but
the documents RAG actually cites live in the knowledge-upload manifest
(``indexed_libraries.json``), one collection per document, each stamped
``uploaded_at``. That is the store ``_get_rag_context`` queries in its first
pass and labels "Your upload:", so ``/ingest/*`` must report it too; the
catalog alone can read empty while the manifest holds every upload.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

#: Manifest rows shaped like the real file: reference packs carry no
#: ``uploaded_at``; user uploads do.
MANIFEST = [
    {"library": "wikipedia_en_simple_all_maxi_2025_09",
     "collection": "wikipedia_en_simple", "chunks": 900000},
    {"library": "Doc One.docx", "collection": "Doc_One_docx",
     "chunks": 44, "uploaded_at": "2026-08-14T10:00:00"},
    {"library": "Doc Two.docx", "collection": "Doc_Two_docx",
     "chunks": 24, "uploaded_at": "2026-08-14T10:05:00"},
    {"library": "IMG_0001.png", "collection": "IMG_0001_png",
     "chunks": 12, "uploaded_at": "2026-08-15T09:00:00"},
]


@pytest.fixture()
def manifest(monkeypatch):
    monkeypatch.setattr(api, "_load_library_manifest", lambda: list(MANIFEST))


def _no_ingestion_catalog(monkeypatch):
    """The common condition: the ingestion catalog is empty."""
    import puffpai.ingestion as ingestion
    monkeypatch.setattr(ingestion, "list_ingested_docs", lambda: [])


def test_the_uploaded_documents_rag_cites_are_now_listed(manifest, monkeypatch):
    _no_ingestion_catalog(monkeypatch)
    docs = api._all_uploaded_docs()
    names = {d["filename"] for d in docs}
    assert names == {"Doc One.docx", "Doc Two.docx", "IMG_0001.png"}
    assert all(d["store"] == "knowledge_upload" for d in docs)


def test_reference_libraries_are_not_reported_as_uploads(manifest):
    names = {d["filename"] for d in api._uploaded_library_docs()}
    assert "wikipedia_en_simple_all_maxi_2025_09" not in names


def test_both_stores_are_merged_and_deduplicated(manifest, monkeypatch):
    import puffpai.ingestion as ingestion
    monkeypatch.setattr(ingestion, "list_ingested_docs", lambda: [
        {"id": "f1", "filename": "patient_record.txt",
         "classification": "medical", "indexed_at": "2026-08-01",
         "word_count": 120},
        # Same document as a manifest row: must not be counted twice.
        {"id": "f2", "filename": "Doc One.docx", "classification": "general",
         "indexed_at": "2026-08-14", "word_count": 900},
    ])
    docs = api._all_uploaded_docs()
    names = [d["filename"] for d in docs]
    assert names.count("Doc One.docx") == 1
    assert "patient_record.txt" in names
    assert len(docs) == 4


def test_an_unreadable_manifest_does_not_break_the_listing(monkeypatch):
    monkeypatch.setattr(api, "_load_library_manifest", lambda: [])
    _no_ingestion_catalog(monkeypatch)
    assert api._all_uploaded_docs() == []


def test_a_manifest_row_that_is_not_a_dict_is_ignored(monkeypatch):
    monkeypatch.setattr(api, "_load_library_manifest",
                        lambda: ["not a dict", None, MANIFEST[1]])
    docs = api._uploaded_library_docs()
    assert [d["filename"] for d in docs] == ["Doc One.docx"]


def test_listing_shape_is_unchanged_for_existing_clients(manifest, monkeypatch):
    _no_ingestion_catalog(monkeypatch)
    for doc in api._all_uploaded_docs():
        for key in ("id", "filename", "classification", "indexed_at",
                    "word_count"):
            assert key in doc, f"{key} missing — a client reads this shape"


def test_chunk_counts_come_from_the_manifest(manifest):
    total = sum(d["chunk_count"] for d in api._uploaded_library_docs())
    assert total == 44 + 24 + 12


def test_the_stats_arithmetic_no_longer_reports_zero_indexed(manifest,
                                                             monkeypatch):
    """The whole point: what RAG can retrieve is what stats must report."""
    _no_ingestion_catalog(monkeypatch)
    docs = api._all_uploaded_docs()
    library_docs = api._uploaded_library_docs()
    indexed_total = 0 + len(library_docs)      # 0 from the user_uploads collection
    assert len(docs) == 3
    assert indexed_total == 3
    assert indexed_total >= len(docs), "fully_indexed must be true"
