"""ONE searchable truth: a document is findable through every door.

The appliance has two knowledge stores, and without a unified search each door
sees only its own.

* ``POST /ingest/upload`` (the Documents page) writes the ingestion store — the
  JSON catalog plus the ``user_uploads`` collection — which only
  ``/ingest/search`` read.
* ``POST /knowledge/upload`` (the Knowledge page) writes the knowledge-upload
  store — one collection per document, recorded in ``indexed_libraries.json`` —
  which is what ``/knowledge/search`` and the chat's RAG citations read.

So a document sent on one page was invisible on the other, silently, and the
owner's own file could not be cited in chat. These tests upload through BOTH
routes and then assert BOTH search doors return BOTH documents.

No model and no embedding model: the two stores are backed by an in-memory
fake collection, and the "embedding" carries the text it was made from so the
fake can score a query the way a cosine collection would. Everything under
test — which stores are read, the merge, the de-duplication, the PII rules — is
ours; nothing here is exercising chromadb.

Run: pytest tests/test_unified_knowledge_search.py -q
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server as mod          # noqa: E402
from puffpai import ingestion                  # noqa: E402
from puffpai import upload_index               # noqa: E402


# ── the fake store ──────────────────────────────────────────────────

def _words(text):
    return {w for w in re.findall(r"\w+", str(text).lower()) if len(w) >= 3}


def _distance(query, text):
    """A cosine-shaped distance: 0.0 when every query word is in the text."""
    q = _words(query)
    if not q:
        return 1.0
    return 1.0 - (len(q & _words(text)) / len(q))


class FakeCollection:
    def __init__(self, name, metadata=None):
        self.name = name
        self.metadata = dict(metadata or {})
        self.metadata.setdefault("hnsw:space", "cosine")
        self._docs = {}

    # write side
    def add(self, ids, documents, metadatas=None, embeddings=None):
        metas = metadatas or [{} for _ in ids]
        for i, doc_id in enumerate(ids):
            self._docs[doc_id] = (documents[i], dict(metas[i] or {}))

    def delete(self, where=None, ids=None):
        for doc_id in list(self._docs):
            meta = self._docs[doc_id][1]
            if ids is not None and doc_id not in ids:
                continue
            if where and any(meta.get(k) != v for k, v in where.items()):
                continue
            del self._docs[doc_id]

    # read side
    def count(self):
        return len(self._docs)

    def get(self, include=None, where=None, **_kw):
        ids, docs, metas = [], [], []
        for doc_id, (text, meta) in self._docs.items():
            if where and any(meta.get(k) != v for k, v in where.items()):
                continue
            ids.append(doc_id)
            docs.append(text)
            metas.append(meta)
        return {"ids": ids, "documents": docs, "metadatas": metas}

    def query(self, query_embeddings=None, query_texts=None, n_results=3,
              **_kw):
        query = (query_texts or [None])[0]
        if query is None and query_embeddings:
            query = query_embeddings[0][0]
        rows = sorted(((_distance(query, text), text, meta)
                       for text, meta in self._docs.values()),
                      key=lambda r: r[0])[:max(1, n_results)]
        return {
            "documents": [[r[1] for r in rows]],
            "metadatas": [[r[2] for r in rows]],
            "distances": [[r[0] for r in rows]],
            "ids": [[f"{self.name}_{i}" for i in range(len(rows))]],
        }


class FakeClient:
    def __init__(self):
        self.collections = {}

    def get_collection(self, name=None, **_kw):
        if name not in self.collections:
            raise KeyError(f"no collection {name}")
        return self.collections[name]

    def create_collection(self, name=None, metadata=None, **_kw):
        self.collections[name] = FakeCollection(name, metadata)
        return self.collections[name]

    def get_or_create(self, name, metadata=None):
        if name not in self.collections:
            return self.create_collection(name=name, metadata=metadata)
        return self.collections[name]


class FakeRag:
    def __init__(self, client):
        self.client = client


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """Both upload stores, empty, backed by one in-memory fake client."""
    client = FakeClient()
    uploads = client.get_or_create(ingestion.USER_UPLOADS_COLLECTION)

    # The embedding IS the text, so the fake collection can score a query
    # without a model. Both stores use the same function, which is the property
    # that makes their scores comparable in the first place.
    embed = lambda texts: [[t] for t in texts]                    # noqa: E731
    monkeypatch.setattr("puffpai.embeddings.embed_documents", embed,
                        raising=False)
    monkeypatch.setattr(mod, "_get_query_embedder", lambda: embed)
    monkeypatch.setattr(mod, "_get_dual_zone_rag", lambda: FakeRag(client))
    monkeypatch.setattr(ingestion, "_get_user_uploads_collection",
                        lambda: uploads)
    monkeypatch.setattr(
        ingestion, "get_or_create_upload_collection",
        lambda c, name, extra_metadata=None, embedding_function=None:
            client.get_or_create(name, extra_metadata))

    # Manifest, catalog and retained originals all in the temp tree.
    monkeypatch.setattr(mod, "_SRC_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "_LIBRARY_SOURCES_DIR",
                        str(tmp_path / "library_sources"))
    monkeypatch.setattr(ingestion, "_UPLOAD_DIR", str(tmp_path / "uploads"),
                        raising=False)
    monkeypatch.setattr(ingestion, "_DOC_INDEX",
                        str(tmp_path / "ingested_docs.json"), raising=False)
    (tmp_path / "indexed_libraries.json").write_text("[]", encoding="utf-8")

    # No model, no network, no ZIM shards, and an embedder that is "up".
    monkeypatch.setattr(ingestion, "classify_document", lambda text: "note")
    monkeypatch.setattr(mod, "_embedding_status",
                        lambda: {"available": True, "message": ""})
    monkeypatch.setattr(mod, "_get_zim_fts_results",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(mod, "_fts_searcher", lambda: None, raising=False)
    monkeypatch.setattr(mod, "_discover_zim_libraries", lambda: {},
                        raising=False)
    monkeypatch.setattr("puffpai.suggestions.queue_suggestion_async",
                        lambda **kw: None, raising=False)
    return client


@pytest.fixture()
def client(stores):
    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as c:
        yield c, {"X-API-Key": mod._API_KEY}


#: Two documents with no words in common, so a hit can only come from the
#: store that actually holds the one being asked about.
DOC_A = ("Garage door opener programming: hold the learn button for three "
         "seconds, then press the remote twice.")
DOC_B = ("Sourdough starter feeding schedule: fifty grams flour and fifty "
         "grams water every morning.")


def _post_ingest(c, headers, name, text):
    resp = c.post('/ingest/upload', headers=headers,
                  data={'file': (io.BytesIO(text.encode()), name),
                        'auto_apply': 'false'},
                  content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['success'] is True, resp.get_json()
    return resp.get_json()


def _post_knowledge(c, headers, name, text, title=None):
    data = {'file': (io.BytesIO(text.encode()), name)}
    if title:
        data['title'] = title
    resp = c.post('/knowledge/upload', headers=headers, data=data,
                  content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['success'] is True, resp.get_json()
    return resp.get_json()


def _titles(payload, key='results'):
    out = []
    for row in payload.get(key) or []:
        out.append(str(row.get('library') or row.get('title')
                       or row.get('filename') or ''))
    return out


# ===========================================================================
# 1. Both doors, both stores
# ===========================================================================

class TestBothStoresAreOneTruth:

    @pytest.fixture()
    def both_uploaded(self, client):
        c, headers = client
        started = time.monotonic()
        _post_ingest(c, headers, 'garage.txt', DOC_A)
        _post_knowledge(c, headers, 'sourdough.txt', DOC_B)
        # "Indexes immediately" is part of the ask, so it is asserted rather
        # than assumed: no background pass, no rescan, no restart.
        assert time.monotonic() - started < 2.0
        return c, headers

    def test_ingest_upload_is_found_by_knowledge_search(self, both_uploaded):
        c, headers = both_uploaded
        resp = c.get('/knowledge/search?q=garage door opener', headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert any('garage.txt' in t for t in _titles(body)), body
        assert any(s.get('source_type') == 'user_upload'
                   for s in body.get('sources') or []), body

    def test_knowledge_upload_is_still_found_by_knowledge_search(
            self, both_uploaded):
        c, headers = both_uploaded
        resp = c.get('/knowledge/search?q=sourdough starter feeding',
                     headers=headers)
        body = resp.get_json()
        assert any('sourdough' in t.lower() for t in _titles(body)), body

    def test_knowledge_upload_is_found_by_ingest_search(self, both_uploaded):
        c, headers = both_uploaded
        resp = c.get('/ingest/search?q=sourdough starter feeding',
                     headers=headers)
        assert resp.status_code == 200
        names = [r.get('filename') for r in resp.get_json()['results']]
        assert any('sourdough' in str(n).lower() for n in names), names

    def test_ingest_upload_is_found_by_ingest_search(self, both_uploaded):
        c, headers = both_uploaded
        resp = c.get('/ingest/search?q=garage door opener', headers=headers)
        names = [r.get('filename') for r in resp.get_json()['results']]
        assert 'garage.txt' in names, names

    @pytest.mark.parametrize('query,expected', [
        ('garage', 'garage.txt'),
        ('sourdough', 'sourdough.txt'),
    ])
    def test_title_search_covers_both_stores(self, both_uploaded, query,
                                             expected):
        c, headers = both_uploaded
        resp = c.get(f'/knowledge/search/titles?q={query}', headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert any(expected in str(r.get('title')) for r in body['results']), \
            body

    def test_both_listings_show_both_documents(self, both_uploaded):
        c, headers = both_uploaded
        ingest_names = {d.get('filename')
                        for d in c.get('/ingest/list',
                                       headers=headers).get_json()['documents']}
        libraries = c.get('/knowledge/libraries',
                          headers=headers).get_json()['libraries']
        library_names = {str(r.get('name')) for r in libraries}
        for name in ('garage.txt', 'sourdough.txt'):
            assert name in ingest_names, ingest_names
            assert any(name in n for n in library_names), library_names

    def test_the_chat_rag_path_retrieves_from_either_store(self, both_uploaded):
        """What chat cites comes from _get_rag_context — both stores, one list."""
        for query, expected in ((DOC_A, 'garage.txt'), (DOC_B, 'sourdough')):
            result = mod._get_rag_context(query, n_results=8)
            assert result is not None, query
            libs = [s['library'] for s in result['sources']]
            assert any(expected in lib for lib in libs), (query, libs)

    def test_one_document_in_both_stores_is_cited_once(self, both_uploaded):
        """The same file sent through BOTH doors is one row, not two."""
        c, headers = both_uploaded
        _post_knowledge(c, headers, 'garage.txt', DOC_A)
        result = mod._get_rag_context(DOC_A, n_results=8)
        garage = [s for s in result['sources'] if 'garage' in s['library']]
        assert len(garage) == 1, result['sources']

        body = c.get('/knowledge/search?q=garage door opener',
                     headers=headers).get_json()
        rows = [t for t in _titles(body) if 'garage' in t]
        assert len(rows) == 1, body


# ===========================================================================
# 2. The PII rules of each door are unchanged
# ===========================================================================

class TestPiiRoutingIsUnchanged:
    """Unifying the READ side must not move a document, or make a document one
    door refused reachable through the other."""

    def test_a_refused_ingest_upload_is_searchable_nowhere(self, client,
                                                           monkeypatch):
        c, headers = client

        def _refuse(*a, **kw):
            raise ingestion.PiiVaultRefusal(
                "the vault is locked, so nothing was indexed")
        monkeypatch.setattr(ingestion, "screen_text_for_pii", _refuse)

        text = "Social security number 123-45-6789 for Rutherford Pemberton."
        resp = c.post('/ingest/upload', headers=headers,
                      data={'file': (io.BytesIO(text.encode()),
                                     'ssn_card.txt'),
                            'auto_apply': 'false'},
                      content_type='multipart/form-data')
        assert resp.get_json()['success'] is False

        for path in ('/knowledge/search?q=Rutherford Pemberton',
                     '/knowledge/search/titles?q=ssn_card',
                     '/ingest/search?q=Rutherford Pemberton'):
            body = c.get(path, headers=headers).get_json()
            # The echoed query carries the words by definition; what must be
            # empty is everything the search RETURNED.
            blob = json.dumps({k: v for k, v in body.items()
                               if k != 'query'}).lower()
            assert 'ssn_card' not in blob, (path, body)
            assert 'pemberton' not in blob, (path, body)

    def test_a_vaulted_ingest_upload_is_searchable_as_the_redacted_text(
            self, client, monkeypatch):
        """The documented rule for this door: values to the vault, the REDACTED
        document stays findable. It must not become the plaintext through the
        other door either."""
        c, headers = client
        redacted = "Policy for [REDACTED_NAME], premium [REDACTED] monthly."
        monkeypatch.setattr(
            ingestion, "screen_text_for_pii",
            lambda text, filename, **kw: ingestion.PiiScreenResult(
                text_for_index=redacted, pii_detected=True, pii_vaulted=True,
                pii_types=['ssn'], summary='1 ssn'))

        _post_ingest(c, headers, 'policy.txt',
                     "Rutherford Pemberton SSN 123-45-6789 premium 40 dollars")

        body = c.get('/knowledge/search?q=premium monthly policy',
                     headers=headers).get_json()
        blob = json.dumps(body)
        assert 'REDACTED' in blob, body
        assert 'Pemberton' not in blob, body
        assert '123-45-6789' not in blob, body

    def test_a_refused_knowledge_upload_stays_out_of_ingest_search(
            self, client, monkeypatch):
        c, headers = client

        def _refuse(*a, **kw):
            raise ingestion.PiiVaultRefusal("the vault is locked")
        monkeypatch.setattr(ingestion, "screen_text_for_pii", _refuse)

        resp = c.post('/knowledge/upload', headers=headers,
                      data={'file': (io.BytesIO(b"card 4111111111111111"),
                                     'card.txt')},
                      content_type='multipart/form-data')
        assert resp.status_code == 409

        for path in ('/ingest/search?q=4111111111111111',
                     '/knowledge/search/titles?q=card',
                     '/ingest/list'):
            blob = json.dumps(c.get(path, headers=headers).get_json())
            assert 'card.txt' not in blob, path
            assert '4111111111111111' not in blob, path


# ===========================================================================
# 3. The merge itself
# ===========================================================================

class TestTitleMatching:

    def test_title_search_needs_no_embedder(self, stores, monkeypatch):
        """Names are text. This is the one search that still answers when the
        embedding backend is down."""
        monkeypatch.setattr(upload_index, "load_library_manifest",
                            lambda: [{"collection": "warranty_pdf",
                                      "library": "Warranty.pdf",
                                      "title": "Fridge warranty",
                                      "uploaded_at": "2026-09-01T00:00:00",
                                      "chunks": 2}])
        monkeypatch.setattr("puffpai.ingestion.list_ingested_docs",
                            lambda: [{"id": "d1", "filename": "Mower.txt"}],
                            raising=False)
        monkeypatch.setattr(upload_index, "upload_index_snapshot",
                            lambda: (0, {}))
        assert [r['title'] for r in upload_index.title_matches("fridge")] == \
            ["Fridge warranty"]
        assert [r['title'] for r in upload_index.title_matches("mower")] == \
            ["Mower.txt"]
        assert upload_index.title_matches("submarine") == []
