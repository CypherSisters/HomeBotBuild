"""document_lookup must read BOTH upload stores, the way api_server does.

A ``document_lookup._load_index`` that calls only
``puffpai.ingestion.list_ingested_docs`` finds nothing on a typical install:
that store is empty and the household's uploads live in the OTHER store (the
knowledge-upload manifest / ``indexed_libraries.json``), the split
``api_server._uploaded_library_docs`` documents. A test that only patches
``document_lookup``'s internals with ``monkeypatch.setattr`` never exercises a
real two-store split with an EMPTY ingestion store.

This test fixture is that split: ``list_ingested_docs`` returns nothing, and
the 13 sample titles live only in the knowledge-upload manifest that
:mod:`puffpai.upload_index` also reads.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai import document_lookup  # noqa: E402
from puffpai import upload_index  # noqa: E402

# 13 sample titles, all in the knowledge-upload manifest only (empty
# ingestion store), the typical split.
_LIBRARY_MANIFEST = [
    {"collection": f"upload_{i}", "library": title,
     "uploaded_at": f"2026-08-{i + 1:02d}T00:00:00",
     "classification": "user_upload", "word_count": 100 * (i + 1),
     "chunks": i + 1}
    for i, title in enumerate([
        "Strength Program.docx", "Gym Session Notes.docx",
        "patient_record.txt", "Home Warranty.pdf", "Router Manual.pdf",
        "Grocery Receipts Q1.pdf", "Vacation Itinerary.docx",
        "Insurance Policy.pdf", "Car Loan Statement.pdf",
        "Recipe Collection.docx", "School Forms.pdf",
        "Utility Bill March.pdf", "Passport Copy.pdf",
    ])
]


@pytest.fixture
def two_store_split(monkeypatch):
    """Empty ingestion store, 13 sample titles only in the knowledge-upload
    manifest."""
    monkeypatch.setattr("puffpai.ingestion.list_ingested_docs",
                        lambda: [], raising=False)
    monkeypatch.setattr(upload_index, "load_library_manifest",
                        lambda: [dict(d) for d in _LIBRARY_MANIFEST])
    monkeypatch.setattr(upload_index, "upload_index_snapshot",
                        lambda: (0, {}))


def test_a_real_title_is_found_when_ingestion_store_is_empty(two_store_split):
    entry = document_lookup.resolve("Strength Program")
    assert entry is not None
    assert entry["filename"] == "Strength Program.docx"
    assert entry["store"] == "knowledge_upload"


def test_a_fake_title_is_still_an_honest_miss(two_store_split):
    assert document_lookup.resolve("Annual Zorp Fizzbin Ledger") is None


def test_known_titles_lists_the_real_manifest_not_an_empty_list(two_store_split):
    titles = document_lookup.known_titles(20)
    assert len(titles) == 13
    assert "Strength Program.docx" in titles


def test_wrapper_words_and_extension_are_stripped(two_store_split):
    for reference in ("the Router Manual", "router manual.pdf",
                      "Router Manual document"):
        entry = document_lookup.resolve(reference)
        assert entry and entry["filename"] == "Router Manual.pdf", reference


def test_an_ingestion_store_document_still_resolves_alongside(monkeypatch):
    """Both stores contribute — an ingestion-catalog document (the OLD
    working path) is not broken by reading the second store too."""
    monkeypatch.setattr(
        "puffpai.ingestion.list_ingested_docs",
        lambda: [{"id": "d1", "filename": "Old Catalog Doc.docx",
                  "classification": "user_upload",
                  "indexed_at": "2026-01-01T00:00:00", "word_count": 50}],
        raising=False)
    monkeypatch.setattr(upload_index, "load_library_manifest",
                        lambda: [dict(d) for d in _LIBRARY_MANIFEST])
    monkeypatch.setattr(upload_index, "upload_index_snapshot",
                        lambda: (0, {}))
    old_entry = document_lookup.resolve("Old Catalog Doc")
    new_entry = document_lookup.resolve("Strength Program")
    assert old_entry and old_entry["filename"] == "Old Catalog Doc.docx"
    assert new_entry and new_entry["filename"] == "Strength Program.docx"
