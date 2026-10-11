"""Send to Puff — one store for "the documents you sent me".

With uploads split across two stores, a reader that only knows one of them
fails like this, even while ``/ingest/list`` reports 13 documents:

* "tell me what's in the document I sent you" answers *"No documents uploaded
  yet."* in 25 ms if the ``list_my_documents`` fastpath reads the INGESTION
  store, which is usually empty; every real upload lives in the
  knowledge-upload store,
* "what documents do you have" misses the capability read entirely and is
  answered by general RAG with 1843 German plant-geography ZIM packs,
* "what does the strength program document say" produces a fluent, invented
  four-phase training programme attributed to the user's file, because
  ``/ingest/search`` (ingestion store only) returns nothing of theirs and the
  answer comes from the encyclopedias instead.

These tests pin the behaviour: listing, retrieval and the document fastpath all read
``puffpai.upload_index``; a no-match answer is honest instead of borrowing the
knowledge base; and a fixture-class document never outranks a real upload.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai import upload_index  # noqa: E402

# The usual split: ingestion store empty, real uploads in the
# knowledge-upload manifest, plus a test fixture in the search index.
_MANIFEST = [
    {"collection": "SAMPLE_Strength_Program_docx",
     "library": "SAMPLE Strength Program.docx",
     "uploaded_at": "2026-09-10T00:00:00", "classification": "user_upload",
     "word_count": 900, "chunks": 44},
    {"collection": "SAMPLE_Morning_Routine_docx",
     "library": "SAMPLE Packing Checklist.docx",
     "uploaded_at": "2026-09-09T00:00:00", "classification": "user_upload",
     "word_count": 300, "chunks": 20},
    # A reference library — no 'uploaded_at', so never a "document you sent".
    {"collection": "wikispecies_all_nopic",
     "library": "Wikispecies (Zwei Pflanzengeographische Documente)",
     "content_type": "reference"},
]

_TRAINING_CHUNK = (
    "SAMPLE Strength Program — Week 1: back squat 5x5, bench press 5x5, "
    "barbell row 5x5. Week 2 adds the deadlift ladder.")


@pytest.fixture
def split_upload_stores(monkeypatch):
    monkeypatch.setattr(upload_index, "load_library_manifest",
                        lambda: [dict(d) for d in _MANIFEST])
    monkeypatch.setattr(upload_index, "upload_index_snapshot",
                        lambda: (0, {}))
    monkeypatch.setattr("puffpai.ingestion.list_ingested_docs",
                        lambda: [], raising=False)

    def _fake_hits(query, max_results):
        words = {w.lower() for w in query.split()}
        rows = []
        if words & {"training", "squat", "deadlift", "bench", "sheet", "program"}:
            rows.append({
                "id": "SAMPLE_Strength_Program_docx",
                "filename": "SAMPLE Strength Program.docx",
                "classification": "user_upload",
                "indexed_at": "2026-09-10T00:00:00", "word_count": 900,
                "score": 0.81, "snippet": _TRAINING_CHUNK,
                "store": "knowledge_upload",
            })
        return rows

    monkeypatch.setattr(upload_index, "_knowledge_upload_hits", _fake_hits)
    monkeypatch.setattr("puffpai.ingestion.search_indexed_docs",
                        lambda q, max_results=5: [], raising=False)


# ── (1) an /ingest/upload-path document reaches the fastpath's manifest ──────

@pytest.fixture
def real_ingest_env(tmp_path, monkeypatch):
    """The real ingest pipeline against a temp data dir.

    The ChromaDB write is stubbed because the embedding backend
    (sentence-transformers/torch) is not installed on a dev host — everything
    else (extraction, the PII screen, classification, the JSON catalog) is the
    production code path.
    """
    from puffpai import ingestion

    monkeypatch.setattr(ingestion, "_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(ingestion, "_DOC_INDEX",
                        str(tmp_path / "ingested_docs.json"))
    monkeypatch.setattr(ingestion, "_add_to_user_uploads_collection",
                        lambda *a, **k: None)
    monkeypatch.setattr(ingestion, "query_user_uploads",
                        lambda q, n_results=5: [])
    monkeypatch.setattr(upload_index, "load_library_manifest", lambda: [])
    monkeypatch.setattr(upload_index, "upload_index_snapshot", lambda: (0, {}))
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_ingest_upload_path_doc_is_in_the_manifest_the_fastpath_reads(
        real_ingest_env):
    """A doc written by the REAL ingest function is listed by the same reader
    ``list_my_documents`` and ``/ingest/list`` use."""
    from puffpai import ingestion

    tmp_path = real_ingest_env
    src = tmp_path / "uploads" / "SAMPLE_training_sheet.txt"
    src.write_text(_TRAINING_CHUNK, encoding="utf-8")

    result = ingestion.ingest_file(
        str(src), original_filename="SAMPLE_training_sheet.txt",
        user_hint="SAMPLE study notes", auto_apply=False)
    assert result.error is None, result.error

    catalog = json.loads((tmp_path / "ingested_docs.json").read_text("utf-8"))
    assert any(e["filename"] == "SAMPLE_training_sheet.txt" for e in catalog)

    names = [d.get("filename") for d in upload_index.all_uploaded_docs()]
    assert "SAMPLE_training_sheet.txt" in names


# ── (4) retrieval returns the doc's own chunk, with a citable filename ──────

def test_the_docs_own_terms_retrieve_its_chunk(real_ingest_env):
    """The core case: search for the document's OWN terms."""
    from puffpai import ingestion

    tmp_path = real_ingest_env
    src = tmp_path / "uploads" / "SAMPLE_training_sheet.txt"
    src.write_text(_TRAINING_CHUNK, encoding="utf-8")
    ingestion.ingest_file(
        str(src), original_filename="SAMPLE_training_sheet.txt",
        user_hint="SAMPLE study notes", auto_apply=False)

    results = upload_index.search_uploads("SAMPLE program")
    assert results, "the document's own terms retrieved nothing"
    top = results[0]
    assert top["filename"] == "SAMPLE_training_sheet.txt"
    assert "squat" in top["snippet"].lower()

    answer = upload_index.compose_document_answer(
        "SAMPLE program", results)
    assert "SAMPLE_training_sheet.txt" in answer  # a citable source


def test_knowledge_upload_store_is_searchable_too(split_upload_stores):
    results = upload_index.search_uploads("strength program")
    assert [r["filename"] for r in results] == [
        "SAMPLE Strength Program.docx"]
    assert results[0]["store"] == "knowledge_upload"


# ── (2) "what's in the document I sent you" names it ────────────────────────

def test_document_list_names_the_upload_instead_of_no_documents(
        split_upload_stores):
    docs = upload_index.all_uploaded_docs()
    names = [d["filename"] for d in docs]
    assert "SAMPLE Strength Program.docx" in names
    assert names, "the fastpath would say 'No documents uploaded yet.'"


# ── (3) "what documents do you have" lists uploads, not ZIM packs ───────────

def test_the_list_is_uploads_only_never_zim_packs(split_upload_stores):
    names = [d["filename"] for d in upload_index.all_uploaded_docs()]
    assert not any("Wikispecies" in n for n in names)
    assert not any("Pflanzengeographische" in n for n in names)


def test_what_documents_do_you_have_reaches_the_document_read():
    """The phrasing that fell through to RAG must match the capability rule."""
    from puffpai import api_server
    rules = {name: (match, exclude)
             for name, match, exclude, _q in api_server._CAPREAD_RULES}
    match, exclude = rules["list_my_documents"]
    for phrasing in ("what documents do you have",
                     "what documents do I have",
                     "what documents have I sent you",
                     "tell me what's in the document I sent you"):
        assert match.search(phrasing), phrasing
        assert exclude is None or not exclude.search(phrasing)


def test_a_named_document_question_is_not_answered_by_rag():
    """"what does the strength program document say" must resolve a DOCUMENT."""
    from puffpai import api_server
    seen = {}

    class _FakeLookup:
        @staticmethod
        def resolve(reference):
            seen["reference"] = reference
            return None

        @staticmethod
        def known_titles(limit=5):
            return ["SAMPLE Strength Program.docx"]

    sys.modules["puffpai"].document_lookup = _FakeLookup  # type: ignore[attr-defined]
    try:
        reply = api_server._try_document_read(
            "what does the SAMPLE strength program document say")
    finally:
        import importlib
        sys.modules["puffpai"].document_lookup = importlib.import_module(
            "puffpai.document_lookup")
    assert reply is not None, "fell through to RAG"
    assert "sample strength program" in seen["reference"].lower()
    assert "don't have a document" in reply


def test_a_list_question_still_belongs_to_list_my_documents():
    from puffpai import api_server
    assert api_server._try_document_read(
        "tell me what's in the document I sent you") is None


# ── (5) an honest miss — no fabricated summary, no borrowed ZIM chunk ───────

def test_no_match_is_honest_and_forbids_the_knowledge_base_standing_in():
    kb_lines = ["\n--- Knowledge Base Results (2 sources) ---",
                "  Wikispecies (Zwei Pflanzengeographische Documente)"]
    answer = upload_index.compose_document_answer(
        "strength program", [], kb_lines)
    lowered = answer.lower()
    assert "nothing in your documents matched" in lowered
    assert "not from any document the user sent" in lowered
    assert "do not present them as the user's document" in lowered
    # Nothing that could be read back as a summary of a file the user owns.
    assert "phase" not in lowered
    assert "Found 1 uploaded document" not in answer


def test_no_match_with_no_knowledge_base_says_so_in_both_sources():
    answer = upload_index.compose_document_answer("zorp fizzbin", [])
    assert "Nothing in your documents matched 'zorp fizzbin'" in answer
    assert "either source" in answer


# ── (6) a real upload outranks a classification: unknown fixture ────────────

def test_a_real_upload_outranks_a_fixture_class_document(monkeypatch):
    """A fixture like ``patient_record.txt`` must not top every document question."""
    fixture = {
        "id": "patient_record_txt", "filename": "patient_record.txt",
        "classification": "unknown", "word_count": 40, "score": 0.92,
        "snippet": "Name: [REDACTED_NAME] SSN: [REDACTED_SSN]",
        "store": "ingestion",
    }
    real = {
        "id": "SAMPLE_Strength_Program_docx",
        "filename": "SAMPLE Strength Program.docx",
        "classification": "user_upload", "word_count": 900, "score": 0.41,
        "snippet": _TRAINING_CHUNK, "store": "knowledge_upload",
    }
    monkeypatch.setattr("puffpai.ingestion.search_indexed_docs",
                        lambda q, max_results=5: [dict(fixture)],
                        raising=False)
    monkeypatch.setattr(upload_index, "_knowledge_upload_hits",
                        lambda q, n: [dict(real)])

    results = upload_index.search_uploads("program")
    assert [r["filename"] for r in results] == [
        "SAMPLE Strength Program.docx", "patient_record.txt"]


def test_fixture_detection_covers_classification_and_pii_markers():
    assert upload_index.is_fixture_doc("unknown")
    assert upload_index.is_fixture_doc("")
    assert upload_index.is_fixture_doc(
        "medical", "DOB: [REDACTED_DOB] on file")
    assert not upload_index.is_fixture_doc("user_upload", _TRAINING_CHUNK)
    assert not upload_index.is_fixture_doc("study", "week 1 back squat")


# -- (7) a FRESH upload is stamped classification: unknown ------------------
#
# /ingest/upload stamps every Send-to-Puff upload
# "unknown", so the fixture guard demoted a real 0.992 hit below twelve
# knowledge_upload rows scoring 0.38-0.50 and off a 5-result page.

def test_fresh_unknown_upload_with_text_ranks_by_score(monkeypatch):
    fresh = {
        "id": "SAMPLE_rank_check_txt", "filename": "SAMPLE_rank_check.txt",
        "classification": "unknown", "word_count": 104, "score": 0.992,
        "snippet": _TRAINING_CHUNK, "store": "ingestion",
    }
    lower = [
        {"id": f"kb_{i}", "filename": f"library_{i}.pdf",
         "classification": "knowledge_upload", "word_count": 5000,
         "score": 0.50 - i * 0.01, "snippet": "reference library excerpt",
         "store": "knowledge_upload"}
        for i in range(12)
    ]
    monkeypatch.setattr("puffpai.ingestion.search_indexed_docs",
                        lambda q, max_results=5: [dict(fresh)],
                        raising=False)
    monkeypatch.setattr(upload_index, "_knowledge_upload_hits",
                        lambda q, n: [dict(r) for r in lower])

    results = upload_index.search_uploads("SAMPLE phase two bench")
    assert results, "the fresh upload fell off the page again"
    assert results[0]["filename"] == "SAMPLE_rank_check.txt"
    assert not upload_index.is_fixture_doc("unknown", _TRAINING_CHUNK, 104)


def test_pii_marker_doc_still_ranks_last_even_with_text(monkeypatch):
    """patient_record.txt has word_count > 0 too -- the PII branch sinks it."""
    assert upload_index.is_fixture_doc(
        "unknown", "Name: [REDACTED_NAME] SSN: [REDACTED_SSN]", 40)
    pii = {
        "id": "patient_record_txt", "filename": "patient_record.txt",
        "classification": "unknown", "word_count": 40, "score": 0.99,
        "snippet": "Name: [REDACTED_NAME] SSN: [REDACTED_SSN]",
        "store": "ingestion",
    }
    fresh = {
        "id": "SAMPLE_rank_check_txt", "filename": "SAMPLE_rank_check.txt",
        "classification": "unknown", "word_count": 104, "score": 0.41,
        "snippet": _TRAINING_CHUNK, "store": "ingestion",
    }
    monkeypatch.setattr("puffpai.ingestion.search_indexed_docs",
                        lambda q, max_results=5: [dict(pii), dict(fresh)],
                        raising=False)
    monkeypatch.setattr(upload_index, "_knowledge_upload_hits",
                        lambda q, n: [])

    results = upload_index.search_uploads("bench press notes")
    assert [r["filename"] for r in results] == [
        "SAMPLE_rank_check.txt", "patient_record.txt"]


def test_textless_unknown_doc_ranks_below_text_docs(monkeypatch):
    """A text-less image row is still fixture-shaped and sorts last."""
    assert upload_index.is_fixture_doc("unknown", "", 0)
    image = {
        "id": "img_png", "filename": "screenshot.png",
        "classification": "unknown", "word_count": 0, "score": 0.88,
        "snippet": "", "store": "ingestion",
    }
    fresh = {
        "id": "SAMPLE_rank_check_txt", "filename": "SAMPLE_rank_check.txt",
        "classification": "unknown", "word_count": 104, "score": 0.33,
        "snippet": _TRAINING_CHUNK, "store": "ingestion",
    }
    monkeypatch.setattr("puffpai.ingestion.search_indexed_docs",
                        lambda q, max_results=5: [dict(image), dict(fresh)],
                        raising=False)
    monkeypatch.setattr(upload_index, "_knowledge_upload_hits",
                        lambda q, n: [])

    results = upload_index.search_uploads("bench press notes")
    assert [r["filename"] for r in results] == [
        "SAMPLE_rank_check.txt", "screenshot.png"]


def test_delete_orphan_doc_removes_chroma_chunks(monkeypatch, tmp_path):
    """No JSON catalog entry, but chunks live in user_uploads -- delete should
    still succeed (the orphan patient_record.txt case)."""
    from puffpai import ingestion

    doc_index = tmp_path / "doc_index.json"
    doc_index.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ingestion, "_doc_index", lambda: doc_index)
    monkeypatch.setattr(ingestion, "_upload_dir", lambda: tmp_path)

    deleted = {}

    class FakeCollection:
        def get(self, where=None, limit=None):
            return {"ids": ["patient_record.txt_chunk_0"]}

        def delete(self, where=None):
            deleted["file_id"] = where.get("file_id")

    monkeypatch.setattr(ingestion, "_get_user_uploads_collection",
                        lambda: FakeCollection())

    assert ingestion.delete_ingested_doc("patient_record.txt") is True
    assert deleted["file_id"] == "patient_record.txt"


def test_delete_unknown_doc_id_returns_false(monkeypatch, tmp_path):
    """An id with no catalog entry AND no chunks is genuinely unknown."""
    from puffpai import ingestion

    doc_index = tmp_path / "doc_index.json"
    doc_index.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ingestion, "_doc_index", lambda: doc_index)
    monkeypatch.setattr(ingestion, "_upload_dir", lambda: tmp_path)

    class FakeCollection:
        def get(self, where=None, limit=None):
            return {"ids": []}

        def delete(self, where=None):
            raise AssertionError("delete should not be called for an unknown id")

    monkeypatch.setattr(ingestion, "_get_user_uploads_collection",
                        lambda: FakeCollection())

    assert ingestion.delete_ingested_doc("nonexistent.txt") is False


def test_delete_catalog_present_path_unchanged(monkeypatch, tmp_path):
    """Catalog-hit behaviour is untouched by the orphan-delete path."""
    from puffpai import ingestion

    doc_index = tmp_path / "doc_index.json"
    doc_index.write_text(json.dumps([
        {"id": "known.txt", "filename": "known.txt"},
        {"id": "other.txt", "filename": "other.txt"},
    ]), encoding="utf-8")
    monkeypatch.setattr(ingestion, "_doc_index", lambda: doc_index)
    monkeypatch.setattr(ingestion, "_upload_dir", lambda: tmp_path)

    deleted = {}

    class FakeCollection:
        def get(self, where=None, limit=None):
            raise AssertionError("get() should not be needed on the catalog-hit path")

        def delete(self, where=None):
            deleted["file_id"] = where.get("file_id")

    monkeypatch.setattr(ingestion, "_get_user_uploads_collection",
                        lambda: FakeCollection())

    assert ingestion.delete_ingested_doc("known.txt") is True
    remaining = json.loads(doc_index.read_text(encoding="utf-8"))
    assert [e["id"] for e in remaining] == ["other.txt"]
    assert deleted["file_id"] == "known.txt"
