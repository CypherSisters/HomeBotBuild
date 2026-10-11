"""
PuffPAI Document Registry

JSON-backed metadata tracker for ingested documents. ChromaDB stores
chunks but has no concept of "documents" — this registry provides
document-level metadata (title, source, zone, date, chunk count) for
the Knowledge Manager UI panel.

Usage:
    registry = DocumentRegistry("path/to/registry.json")
    registry.register(doc)          # after KnowledgeIngestion.ingest_*()
    docs = registry.list_documents()
    results = registry.search("python")
    registry.delete("doc_abc123")

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from typing import Optional


class DocumentRegistry:
    """Track ingested documents with JSON file persistence.

    Each document entry stores:
      - doc_id: unique document identifier (from content hash)
      - title: display title
      - source: how it was added (e.g. document_upload, zim_pack)
      - zone: which zone it was stored in (vault or knowledge)
      - ingested_at: ISO timestamp
      - chunk_count: number of chunks created
      - file_origin: original file path (if applicable)
      - has_pii: whether PII was detected
    """

    def __init__(self, registry_path: str):
        """Initialize with path to the JSON registry file.

        Args:
            registry_path: Absolute or relative path to the registry JSON.
                Created automatically if it doesn't exist.
        """
        self._path = registry_path
        self._documents: list[dict] = []
        self._load()

    def _load(self) -> None:
        """Load the registry from disk."""
        if os.path.exists(self._path):
            try:
                with open(self._path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._documents = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                self._documents = []
        else:
            self._documents = []

    def _save(self) -> None:
        """Persist the registry to disk."""
        os.makedirs(os.path.dirname(self._path) or '.', exist_ok=True)
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(self._documents, f, indent=2, ensure_ascii=False)

    def register(self, doc) -> dict:
        """Register an ingested document.

        Args:
            doc: An IngestedDocument dataclass from the ingestion pipeline.

        Returns:
            The registered document dict.
        """
        entry = {
            'doc_id': doc.doc_id,
            'title': doc.title,
            'source': doc.source.value if hasattr(doc.source, 'value') else str(doc.source),
            'zone': doc.target_zone.value if hasattr(doc.target_zone, 'value') else str(doc.target_zone),
            'ingested_at': doc.ingested_at,
            'chunk_count': doc.chunk_count,
            'file_origin': doc.file_origin,
            'has_pii': doc.has_pii,
        }

        # Replace if same doc_id exists (re-upload)
        self._documents = [d for d in self._documents if d['doc_id'] != doc.doc_id]
        self._documents.append(entry)
        self._save()
        return entry

    def list_documents(self, zone: str | None = None) -> list[dict]:
        """List all registered documents, optionally filtered by zone.

        Args:
            zone: Filter by zone value (e.g. "puffpai_vault", "puffpai_knowledge").

        Returns:
            List of document dicts, newest first.
        """
        docs = self._documents
        if zone:
            docs = [d for d in docs if d.get('zone') == zone]
        return sorted(docs, key=lambda d: d.get('ingested_at', ''), reverse=True)

    def search(self, query: str) -> list[dict]:
        """Search documents by title or file origin.

        Args:
            query: Case-insensitive search string.

        Returns:
            Matching document dicts, newest first.
        """
        q = query.lower()
        results = []
        for doc in self._documents:
            title = (doc.get('title') or '').lower()
            origin = (doc.get('file_origin') or '').lower()
            if q in title or q in origin:
                results.append(doc)
        return sorted(results, key=lambda d: d.get('ingested_at', ''), reverse=True)

    def get(self, doc_id: str) -> dict | None:
        """Get a document by ID.

        Args:
            doc_id: The document ID.

        Returns:
            Document dict, or None if not found.
        """
        for doc in self._documents:
            if doc['doc_id'] == doc_id:
                return doc
        return None

    def delete(self, doc_id: str) -> bool:
        """Remove a document from the registry.

        Note: This does NOT delete chunks from ChromaDB — that must
        be done separately via the ChromaDB client.

        Args:
            doc_id: The document ID to remove.

        Returns:
            True if the document was found and removed.
        """
        before = len(self._documents)
        self._documents = [d for d in self._documents if d['doc_id'] != doc_id]
        if len(self._documents) < before:
            self._save()
            return True
        return False

    @property
    def count(self) -> int:
        """Total number of registered documents."""
        return len(self._documents)

    @property
    def total_chunks(self) -> int:
        """Sum of chunk_count across all documents."""
        return sum(d.get('chunk_count', 0) for d in self._documents)
