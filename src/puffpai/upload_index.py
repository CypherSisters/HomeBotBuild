"""Shared read of the two upload stores.

There are two places a household upload can live:

* ``puffpai.ingestion`` — a JSON catalog plus the ``user_uploads`` ChromaDB
  collection.
* the knowledge-upload manifest (``indexed_libraries.json``), where each
  document gets its own collection and carries an ``uploaded_at`` stamp.
  ``api_server._get_rag_context`` queries these and cites them as
  "Your upload: X".

A reader that looks at only the first store can miss everything the
household has actually uploaded, because uploads commonly land in the second.
This module is the one place both ``api_server.py`` and
``document_lookup.py`` read from, so the two can never diverge.

Raw manifest reading (``indexed_libraries.json``) stays canonical in
``api_server._load_library_manifest`` — many existing tests patch that
attribute directly — and is reached here through a function-local import
(never at module load time), so this module and ``api_server`` can each
import the other without a circular-import error.
"""

from __future__ import annotations


def load_library_manifest() -> list:
    """Load indexed_libraries.json, or [] when it is missing/corrupt.

    Delegates to ``api_server._load_library_manifest`` so there is exactly
    one reader of the file, and so tests that monkeypatch that attribute
    keep working unchanged.
    """
    from puffpai.api_server import _load_library_manifest
    return _load_library_manifest()


def uploaded_library_docs() -> list:
    """The user's uploads that live in the knowledge-upload store."""
    docs = []
    for entry in load_library_manifest():
        if not isinstance(entry, dict) or 'uploaded_at' not in entry:
            continue
        docs.append({
            'id': entry.get('collection') or entry.get('library'),
            'filename': entry.get('library'),
            # The DISPLAY name: what the household called it on send, or the
            # label Puff wrote after reading it. The filename (and the
            # collection id under it) never changes. 
            'title': entry.get('title') or entry.get('library'),
            'classification': entry.get('classification', 'user_upload'),
            'indexed_at': entry.get('uploaded_at'),
            'word_count': entry.get('word_count', 0),
            'chunk_count': entry.get('chunks', 0),
            'store': 'knowledge_upload',
            'retrievable': True,
        })
    return docs


def upload_index_snapshot() -> tuple:
    """One read of the ``user_uploads`` search index: ``(chunks, {file_id: …})``."""
    from puffpai.ingestion import _get_user_uploads_collection
    docs: dict = {}
    chunks = 0
    try:
        coll = _get_user_uploads_collection()
        if coll is None:
            return 0, docs
        chunks = coll.count()
        data = coll.get(include=['metadatas']) or {}
        for meta in (data.get('metadatas') or []):
            if not isinstance(meta, dict):
                continue
            file_id = meta.get('file_id')
            if not file_id:
                continue
            entry = docs.setdefault(file_id, {
                'id': file_id,
                'filename': meta.get('filename') or file_id,
                'classification': meta.get('classification', 'user_upload'),
                'indexed_at': meta.get('indexed_at'),
                'word_count': 0,
                'chunk_count': 0,
                'store': 'index_only',
                'retrievable': True,
            })
            entry['chunk_count'] += 1
    except Exception:
        pass
    return chunks, docs


def all_uploaded_docs(index_docs: dict = None) -> list:
    """Every upload the appliance can actually retrieve, de-duplicated.

    Three sources, in trust order: the ingestion JSON catalog, the
    knowledge-upload manifest, and — last — the search index itself.
    """
    from puffpai.ingestion import list_ingested_docs
    docs = []
    seen = set()
    ids = set()
    try:
        for d in list_ingested_docs():
            entry = dict(d)
            entry.setdefault('store', 'ingestion')
            docs.append(entry)
            if entry.get('filename'):
                seen.add(str(entry['filename']).lower())
            if entry.get('id'):
                ids.add(str(entry['id']))
    except Exception:
        pass
    for d in uploaded_library_docs():
        if str(d.get('filename') or '').lower() in seen:
            continue
        docs.append(d)
        seen.add(str(d.get('filename') or '').lower())
        if d.get('id'):
            ids.add(str(d['id']))
    if index_docs is None:
        index_docs = upload_index_snapshot()[1]
    for file_id, d in (index_docs or {}).items():
        if str(file_id) in ids:
            continue
        if str(d.get('filename') or '').lower() in seen:
            continue
        docs.append(dict(d))
    # Every row carries a title, so a caller never has to know which store a
    # document came from to show its name.
    for entry in docs:
        if not str(entry.get('title') or '').strip():
            entry['title'] = entry.get('filename') or entry.get('id') or ''
    return docs


# ── One retrieval path for "documents the user sent" ─────────────────────────
#
# WHY. A search that reads the ingestion store alone (``user_uploads`` +
# ``ingested_docs.json``) can find nothing for a document ``/ingest/list``
# shows, because household uploads commonly live in the knowledge-upload
# store. "what does my budget worksheet say" would then retrieve nothing of
# the user's, fall through to encyclopedia RAG, and have a fluent answer
# invented and attributed to their file.
#
# ``search_uploads`` is the single retrieval entry point both stores answer
# from, so listing and retrieval can never disagree.

import re as _re

#: A test FIXTURE looks like a document but is not the household's. A planted
#: file such as ``patient_record.txt`` (``classification: unknown``, carrying
#: PII redaction markers) can be the top hit for EVERY document question.
#: Fixtures are never deleted here (that is the owner's call), only ranked
#: last.
_FIXTURE_CLASSIFICATIONS = frozenset({'', 'unknown', 'test', 'fixture'})

#: Classifications a document only ever carries because it was planted.
_ALWAYS_FIXTURE_CLASSIFICATIONS = frozenset({'test', 'fixture'})

#: Classifications that say nothing either way. ``/ingest/upload`` stamps
#: EVERY fresh Send-to-Puff upload ``unknown``, so
#: sinking on ``unknown`` alone demoted a real 0.992 hit below twelve
#: ``knowledge_upload`` rows scoring 0.38-0.50 - four of them text-less
#: images - and off the end of a 5-result page. An ``unknown`` row that
#: carries retrievable text is a real upload and ranks by score.
_AMBIGUOUS_CLASSIFICATIONS = frozenset({'', 'unknown'})

_PII_MARKER_RE = _re.compile(
    r'\[(?:REDACTED|PII)[^\]]{0,40}\]|\bREDACTED[_ ](?:SSN|DOB|ADDRESS|CC)\b',
    _re.IGNORECASE)


def _has_retrievable_text(snippet, word_count) -> bool:
    """True when the row carries real content to answer from."""
    if word_count is not None:
        try:
            return int(word_count) > 0
        except (TypeError, ValueError):
            pass
    return bool(str(snippet or '').strip())


def is_fixture_doc(classification, snippet: str = '', word_count=None) -> bool:
    """True when a hit is fixture-shaped rather than a real household upload.

    A PII-marker row is fixture-class regardless of content, which keeps
    ``patient_record.txt`` ranked last. ``unknown``/blank classifications are
    fixture-class only when the row has NO retrievable text.
    """
    if _PII_MARKER_RE.search(str(snippet or '')):
        return True
    cls = str(classification or '').strip().lower()
    if cls in _ALWAYS_FIXTURE_CLASSIFICATIONS:
        return True
    if cls in _AMBIGUOUS_CLASSIFICATIONS:
        return not _has_retrievable_text(snippet, word_count)
    return False


def _knowledge_upload_hits(query: str, max_results: int) -> list:
    """Best chunk per knowledge-upload document, scored like the other store."""
    entries = [e for e in load_library_manifest()
               if isinstance(e, dict) and 'uploaded_at' in e]
    if not entries:
        return []
    try:
        from puffpai.api_server import (
            _get_dual_zone_rag, _get_query_embedder,
            _distance_to_relevance, _get_collection_space)
        rag = _get_dual_zone_rag()
        query_emb = _get_query_embedder()([query])
    except Exception:
        return []

    words = [w for w in _re.findall(r'\w+', query.lower()) if len(w) >= 3]
    out = []
    for entry in entries:
        name = entry.get('collection') or entry.get('library')
        if not name:
            continue
        try:
            collection = rag.client.get_collection(name=name)
            space = _get_collection_space(collection, name)
            res = collection.query(query_embeddings=query_emb, n_results=3)
        except Exception:
            continue
        docs = (res.get('documents') or [[]])
        docs = docs[0] if docs else []
        dists = (res.get('distances') or [[]])
        dists = dists[0] if dists else []
        best_rel, best_text = -1.0, ''
        for i, text in enumerate(docs):
            rel = _distance_to_relevance(
                dists[i] if i < len(dists) else None, space)
            if rel > best_rel:
                best_rel, best_text = rel, text or ''
        if best_rel < 0:
            continue
        filename = entry.get('library') or name
        fname_hits = sum(1 for w in words if w in str(filename).lower())
        out.append({
            'id': name,
            'filename': filename,
            'classification': entry.get('classification', 'user_upload'),
            'indexed_at': entry.get('uploaded_at', ''),
            'word_count': entry.get('word_count', 0),
            'score': round(best_rel + fname_hits * 0.3, 3),
            'snippet': best_text[:800],
            'store': 'knowledge_upload',
        })
    out.sort(key=lambda r: r['score'], reverse=True)
    return out[:max(1, max_results * 3)]


def search_uploads(query: str, max_results: int = 5,
                   min_score: float = 0.0) -> list:
    """Search EVERY store a household upload can live in, best chunk per doc.

    Returns the row shape ``ingestion.search_indexed_docs`` returns (``id``,
    ``filename``, ``classification``, ``snippet``, ``score``, …) plus
    ``store``. Real uploads always rank ahead of fixture-shaped documents, so
    a leftover test fixture can never be the answer to "what's in the document
    I sent you".
    """
    if not query or not str(query).strip():
        return []
    query = str(query).strip()

    rows = []
    try:
        from puffpai.ingestion import search_indexed_docs
        for row in search_indexed_docs(query, max_results=max_results * 2) or []:
            entry = dict(row)
            entry.setdefault('store', 'ingestion')
            rows.append(entry)
    except Exception:
        pass
    try:
        rows.extend(_knowledge_upload_hits(query, max_results))
    except Exception:
        pass

    merged: dict = {}
    for row in rows:
        key = str(row.get('filename') or row.get('id') or '').lower()
        if not key:
            continue
        if key not in merged or row.get('score', 0) > merged[key].get('score', 0):
            merged[key] = row

    results = [r for r in merged.values()
               if float(r.get('score') or 0.0) >= min_score]
    results.sort(
        key=lambda r: (is_fixture_doc(r.get('classification'),
                                      r.get('snippet', ''),
                                      r.get('word_count')),
                       -float(r.get('score') or 0.0)))
    return results[:max_results]


def title_matches(query: str, limit: int = 20) -> list:
    """Uploads whose NAME matches *query*, across both stores.

    Title search is a different question from semantic search ("which of my
    files is this?" rather than "which chunk answers this?") and it needs no
    embedding model, so it answers on a box whose embedder is down. Used by
    ``/knowledge/search/titles``, which searched only the ZIM shards and could
    therefore not find a document the household had uploaded five seconds
    earlier — in either store.
    """
    q = str(query or '').strip().lower()
    if not q:
        return []
    words = [w for w in _re.findall(r'\w+', q) if len(w) >= 2]
    out = []
    for doc in all_uploaded_docs():
        title = str(doc.get('title') or doc.get('filename') or '')
        haystack = f"{title} {doc.get('filename') or ''}".lower()
        if q in haystack:
            hits = len(words) + 1          # a whole-phrase match ranks first
        else:
            hits = sum(1 for w in words if w in haystack)
            if not hits:
                continue
        row = dict(doc)
        row['match_words'] = hits
        out.append(row)
    out.sort(key=lambda r: (-r['match_words'],
                            str(r.get('title') or '').lower()))
    return out[:max(1, limit)]


#: What the brain's ``search_my_documents`` tool asks for. Anything below 0.35
#: relevance is noise and made the model fabricate document names.
DOCUMENT_SEARCH_MAX_RESULTS = 5
DOCUMENT_SEARCH_MIN_SCORE = 0.35


def search_my_documents(query: str) -> list:
    """The upload rows ``search_my_documents`` answers from, and nothing else.

    One call for the agent tool (``assistant_tools``) and the "search my documents
    for X" fastpath (``api_server._try_documents_search``), so the two can
    never search with different limits or thresholds.
    """
    return search_uploads(query, max_results=DOCUMENT_SEARCH_MAX_RESULTS,
                          min_score=DOCUMENT_SEARCH_MIN_SCORE)


#: Prefix that keeps a knowledge-base excerpt from being read back as if it
#: were the user's own file. The rule it enforces: no claim about a household
#: document without a retrieved chunk of that document.
_NO_UPLOAD_MATCH_RULE = (
    "The excerpts below are from the offline reference library, NOT from any "
    "document the user sent. Do NOT present them as the user's document, do "
    "NOT summarize them as if they were, and do not name or describe any file "
    "of the user's that is not listed above.")


def compose_document_answer(query: str, results: list,
                            kb_lines: list = None) -> str:
    """Format the document-search reply, honestly, for the LLM to read out.

    With no matching upload the reply says so in the user's own terms and
    forbids the knowledge base from standing in for the missing document.
    """
    kb_lines = list(kb_lines or [])
    lines = []
    if results:
        lines.append(f"Found {len(results)} uploaded document"
                     f"{'s' if len(results) != 1 else ''}:")
        for r in results:
            lines.append(
                f"\n--- {r['filename']} ({r.get('classification', 'document')}"
                f", {r.get('word_count', 0)} words) ---")
            lines.append(str(r.get('snippet', ''))[:800])
    else:
        lines.append(
            f"Nothing in your documents matched '{query}'. Tell the user "
            f"plainly that you have nothing in the documents they sent you "
            f"about '{query}'.")

    if kb_lines:
        if not results:
            lines.append("\n" + _NO_UPLOAD_MATCH_RULE)
        lines.extend(kb_lines)
        if results:
            lines.append(
                "\nPresent BOTH your uploaded document results AND the "
                "knowledge base results to the user. Combine them into "
                "a helpful, unified answer.")
    elif not results:
        lines.append(
            f"\nNo knowledge base results matched '{query}' either. "
            f"Let the user know you couldn't find information on "
            f"this topic in either source.")
    return "\n".join(lines)
