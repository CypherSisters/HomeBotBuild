"""Resolve a spoken document reference to a real ingested upload.

WHY. Without it, ``can you read the Budget Worksheet document and convert it
for me`` can come back *"I couldn't locate the file you mentioned... the system
didn't find a match"* while that exact file is being cited, by name, as a
``user_upload`` RAG source in the same session. The capability works; the
LOOKUP is the problem. ``document_convert`` resolves paths
inside the toolsmith workspace, and the household's documents do not live
there: they live in the ingestion manifest that RAG reads.

So this module resolves a reference the way a person gives one — a title, with
or without the extension, with or without "the ... document" around it —
against that same manifest, and says honestly what it could not find. It never
guesses: below :data:`MIN_RATIO` it returns no match and offers the closest
titles it really holds, which is the difference between "I don't have that"
and "I couldn't locate the file you mentioned" said about a file that exists.

Read-only. It opens no document, converts nothing and moves nothing.
"""

from __future__ import annotations

import difflib
import os
import re

#: Similarity below which a fuzzy candidate is NOT a match. Deliberately high:
#: naming the wrong household document is worse than admitting a miss, and the
#: caller always lists near-misses so the user can pick.
MIN_RATIO = 0.72

#: Words that wrap a title in speech and are not part of it.
_WRAPPER_RE = re.compile(
    r'\b(?:the|my|our|a|an|that|this|please|file|files|document|documents|'
    r'doc|docs|attachment|upload|uploaded|paper|sheet\s+file)\b',
    re.IGNORECASE)

_PUNCT_RE = re.compile(r'[^a-z0-9]+')


def _norm(text: str) -> str:
    """Lowercase, drop punctuation and extension noise, squeeze whitespace."""
    return _PUNCT_RE.sub(" ", str(text or "").lower()).strip()


def _stem(filename: str) -> str:
    return os.path.splitext(str(filename or ""))[0]


def _key(text: str) -> str:
    """The comparison key: normalized, with wrapper words removed."""
    stripped = _WRAPPER_RE.sub(" ", str(text or ""))
    return re.sub(r'\s+', ' ', _norm(stripped)).strip()


def _load_index() -> list:
    """Every upload across BOTH stores, or ``[]``. Never raises.

    ``puffpai.ingestion.list_ingested_docs()`` alone can be empty while the
    household's uploads live in the OTHER store (the knowledge-upload
    manifest, ``indexed_libraries.json``), the same split
    ``api_server._uploaded_library_docs()`` documents. Read through the shared
    :mod:`puffpai.upload_index` module so this can never drift from what
    ``api_server.py`` reads.
    """
    try:
        from puffpai.upload_index import all_uploaded_docs
        docs = all_uploaded_docs()
    except Exception:
        return []
    return [d for d in (docs or []) if isinstance(d, dict) and d.get("filename")]


def known_titles(limit: int = 5) -> list:
    """Filenames the manifest really holds, newest first, for an honest miss."""
    docs = _load_index()
    docs = sorted(docs, key=lambda d: str(d.get("indexed_at") or ""),
                  reverse=True)
    return [str(d["filename"]) for d in docs[:max(0, limit)]]


def resolve(reference: str) -> "dict | None":
    """The manifest entry *reference* names, or ``None`` if it names none.

    Match order, each stricter than the next one is loose:

    1. exact filename (case-insensitive),
    2. the normalized title with wrapper words stripped, against the filename
       stem — this is what "the Strength Program document" needs,
    3. a difflib ratio at or above :data:`MIN_RATIO`, for a typo or a dropped
       word in a voice transcript.

    Returns the manifest dict (``id``, ``filename``, ``classification``,
    ``word_count``, ``indexed_at``) plus ``match`` naming which rule fired.
    """
    wanted_raw = str(reference or "").strip()
    if not wanted_raw:
        return None
    docs = _load_index()
    if not docs:
        return None

    lowered = wanted_raw.lower()
    for doc in docs:
        if str(doc["filename"]).lower() == lowered:
            return dict(doc, match="exact")

    wanted = _key(wanted_raw)
    if not wanted:
        return None
    for doc in docs:
        if _key(_stem(doc["filename"])) == wanted:
            return dict(doc, match="title")

    best = None
    best_ratio = 0.0
    for doc in docs:
        ratio = difflib.SequenceMatcher(
            None, wanted, _key(_stem(doc["filename"]))).ratio()
        if ratio > best_ratio:
            best, best_ratio = doc, ratio
    if best is not None and best_ratio >= MIN_RATIO:
        return dict(best, match="fuzzy", ratio=round(best_ratio, 3))
    return None


def text_of(entry: "dict | None", limit: int = 6000) -> str:
    """The extracted text the ingester stored for *entry*, truncated.

    The ingestion catalog keeps the extracted text alongside the metadata, so
    reading a document back needs neither the sandbox nor LibreOffice.
    Knowledge-upload-store entries have no such catalog row:
    their text lives as chunks in their own ChromaDB collection (``id`` is
    the collection name) — so those are read back by joining the chunks in
    stored order. ``""`` when the entry has none (an image with no OCR text,
    say, or the collection is unreadable).
    """
    if not entry:
        return ""
    if entry.get("store") == "knowledge_upload":
        return _text_from_knowledge_upload(entry, limit)
    try:
        import json
        from puffpai.ingestion import _doc_index
        path = _doc_index()
        with open(path, "r", encoding="utf-8") as handle:
            index = json.load(handle)
    except Exception:
        return ""
    for item in index if isinstance(index, list) else []:
        if isinstance(item, dict) and item.get("id") == entry.get("id"):
            return str(item.get("text") or "")[:max(0, limit)]
    return ""


def _text_from_knowledge_upload(entry: dict, limit: int) -> str:
    """Join a knowledge-upload-store document's chunks back into text."""
    collection_name = str(entry.get("id") or "")
    if not collection_name:
        return ""
    try:
        from puffpai.api_server import _get_dual_zone_rag
        rag = _get_dual_zone_rag()
        collection = rag.client.get_collection(name=collection_name)
        data = collection.get(include=["documents"]) or {}
        chunks = [c for c in (data.get("documents") or []) if c]
    except Exception:
        return ""
    return "\n\n".join(chunks)[:max(0, limit)]
