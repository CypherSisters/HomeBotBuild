"""
PuffPAI Dual-Zone RAG (Retrieval-Augmented Generation)

Queries two ChromaDB collections simultaneously:
  - Zone 1 (Vault): Personal knowledge -- requires authenticated session
  - Zone 2 (Knowledge): General knowledge -- open access

Results are labeled with source provenance so the user always knows
whether information came from "your vault" vs "the knowledge base".

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class ResultSource(Enum):
    """Where a RAG result came from."""
    VAULT = "vault"         # Zone 1: personal knowledge
    KNOWLEDGE = "knowledge"  # Zone 2: general knowledge base


@dataclass
class RAGResult:
    """A single retrieval result with provenance."""
    text: str
    source: ResultSource
    title: str = ""
    score: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def provenance_label(self) -> str:
        """Human-readable source label for display."""
        if self.source == ResultSource.VAULT:
            return "from your vault"
        return "from knowledge base"


@dataclass
class RAGResponse:
    """Combined RAG query response with results from both zones."""
    query: str
    results: list  # List[RAGResult]
    vault_count: int = 0
    knowledge_count: int = 0

    @property
    def has_vault_results(self) -> bool:
        return self.vault_count > 0

    @property
    def has_knowledge_results(self) -> bool:
        return self.knowledge_count > 0

    def context_string(self, max_results: int = 8) -> str:
        """Build a context string for LLM prompting with provenance labels.

        Returns text like:
            [From your vault] Title: Medical Notes
            My doctor is Dr. Smith...

            [From knowledge base] Title: Python Programming
            Python is a high-level...
        """
        # Read-side scrub (Increment 2, flag-gated, default OFF): before Zone-1
        # (vault) content is injected into the prompt, redact any RAW PII it
        # still carries. Defense-in-depth for legacy/unscrubbed stored content —
        # the write side may have scrubbed nothing for chunks created before
        # scrub-and-vault existed. Zone-2 (general knowledge) is NEVER touched:
        # it must stay fast and unredacted. Existing {{vault:}} handles are left
        # as-is (they match no PII pattern, so no double-processing).
        scrub_vault = False
        _redact_for_prompt = None
        try:
            from puffpai.security.scrub_and_vault import (
                scrub_and_vault_enabled, redact_for_prompt as _redact_for_prompt)
            scrub_vault = scrub_and_vault_enabled()
        except Exception:
            scrub_vault = False

        parts = []
        for r in self.results[:max_results]:
            label = r.provenance_label
            header = f"[{label.title()}]"
            if r.title:
                header += f" Title: {r.title}"
            text = r.text
            if scrub_vault and r.source == ResultSource.VAULT and _redact_for_prompt:
                text = _redact_for_prompt(text)
            parts.append(f"{header}\n{text}")

        return "\n\n---\n\n".join(parts)

    def vault_results_only(self) -> list:
        return [r for r in self.results if r.source == ResultSource.VAULT]

    def knowledge_results_only(self) -> list:
        return [r for r in self.results if r.source == ResultSource.KNOWLEDGE]


class DualZoneRAG:
    """Query both personal (vault) and general (knowledge) ChromaDB collections.

    Usage:
        rag = DualZoneRAG(vault_collection, knowledge_collection)

        # Unauthenticated: only knowledge base results
        response = rag.query("What is Python?")

        # Authenticated: vault + knowledge base results
        response = rag.query("What is my doctor's name?", authenticated=True)

        # Build LLM context with provenance
        context = response.context_string()

        # Access indexed library collections directly
        collection = rag.client.get_collection(name="kiwix_wikipedia_en_all")
    """

    def __init__(self, vault_collection=None, knowledge_collection=None,
                 db_path: str | None = None):
        """Initialize with ChromaDB collection handles.

        Args:
            vault_collection: Zone 1 personal knowledge (auth-gated).
            knowledge_collection: Zone 2 general knowledge (open).
            db_path: Path to the ChromaDB persistent storage directory.
                If *None*, reads ``paths.chromadb_path`` from the global
                config (falling back to ``chromadb.path`` for legacy compat).
        """
        self._vault = vault_collection
        self._knowledge = knowledge_collection
        self._db_path = db_path
        self._client = None  # lazy-init in .client property

    # ── ChromaDB client (backwards-compat with KiwixRAG) ─────────────

    @property
    def client(self):
        """Lazy-initialised ``chromadb.PersistentClient``.

        Provides the same ``.client.get_collection(name=...)`` API that the
        old ``KiwixRAG`` exposed, so ``chat_view.py`` and ``api_server.py``
        can query indexed library collections without changes.
        """
        if self._client is None:
            import os
            import chromadb

            db_path = self._db_path
            if db_path is None:
                # Read from centralized config
                try:
                    from puffpai.config import get_config
                    cfg = get_config()
                    db_path = cfg.paths.chromadb_path or cfg.chromadb.path
                except Exception:
                    db_path = "./kiwix_rag_db"

            # Resolve relative paths against REPO_ROOT so we always
            # find the real data regardless of the current working dir.
            if not os.path.isabs(db_path):
                try:
                    from puffpai.config import get_config
                    repo_root = get_config().REPO_ROOT
                except Exception:
                    repo_root = os.path.dirname(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    )
                db_path = os.path.join(repo_root, db_path)

            self._client = chromadb.PersistentClient(path=db_path)
        return self._client

    def query(self, query_text: str, n_results: int = 5,
              authenticated: bool = False,
              vault_only: bool = False,
              knowledge_only: bool = False,
              vault_owner: str | None = None) -> RAGResponse:
        """Query one or both zones and return merged, ranked results.

        Args:
            query_text: The search query.
            n_results: Max results per zone.
            authenticated: Whether the user has an active vault session.
                If False, vault results are excluded.
            vault_only: Only query the vault (requires authenticated=True).
            knowledge_only: Only query the knowledge base.
            vault_owner: The calling principal (multi-user, Increment 1). When
                given, Zone-1 (vault) results are restricted to that principal's
                own content; Zone-2 (knowledge) is NEVER filtered — it stays
                shared for everyone. ``None`` (the default, and the flag-off
                world) means no owner filtering at all — today's behavior.

        Returns:
            RAGResponse with merged results and provenance labels.
        """
        all_results = []

        # Query Zone 2 (Knowledge) -- always available AND ALWAYS SHARED. No
        # owner filter is ever applied here: general knowledge is common to the
        # whole household.
        if not vault_only and self._knowledge is not None:
            kb_results = self._query_collection(
                self._knowledge, query_text, n_results, ResultSource.KNOWLEDGE)
            all_results.extend(kb_results)

        # Query Zone 1 (Vault) -- only if authenticated, and (multi-user) only
        # the caller's own content.
        if not knowledge_only and authenticated and self._vault is not None:
            # DB-side precision filter for a NON-OWNER principal (they never need
            # legacy/untagged content). The OWNER queries unfiltered at the DB so
            # legacy content with no `owner` field still returns, then the
            # Python post-filter below enforces ownership authoritatively for
            # everyone (and is DB-agnostic). This is access-control, not crypto:
            # the single vault DEK still decrypts every principal's Zone-1 data;
            # the boundary is this filter refusing to surface another's rows.
            vault_where = None
            if vault_owner is not None:
                try:
                    from puffpai.security.multiuser import (
                        OWNER_USER_ID, caller_principal)
                    principal = caller_principal(vault_owner)
                    if principal != OWNER_USER_ID:
                        vault_where = {"owner": principal}
                except Exception:
                    vault_where = None
            vault_results = self._query_collection(
                self._vault, query_text, n_results, ResultSource.VAULT,
                where=vault_where)
            if vault_owner is not None:
                try:
                    from puffpai.security.multiuser import can_access
                    vault_results = [
                        r for r in vault_results
                        if can_access(r.metadata.get("owner"), vault_owner)
                    ]
                except Exception:
                    # Fail closed: if ownership cannot be decided, surface no
                    # Zone-1 rows rather than risk leaking another principal's.
                    vault_results = []
            all_results.extend(vault_results)

        # Sort by relevance score (lower distance = better match)
        all_results.sort(key=lambda r: r.score)

        vault_count = sum(1 for r in all_results if r.source == ResultSource.VAULT)
        knowledge_count = sum(1 for r in all_results if r.source == ResultSource.KNOWLEDGE)

        return RAGResponse(
            query=query_text,
            results=all_results,
            vault_count=vault_count,
            knowledge_count=knowledge_count,
        )

    def _query_collection(self, collection, query_text: str,
                          n_results: int, source: ResultSource,
                          where: dict | None = None) -> list:
        """Query a single ChromaDB collection and wrap results.

        ``where`` is an optional ChromaDB metadata filter (used by the Zone-1
        owner filter). It is passed through only when provided, so unfiltered
        queries are byte-for-byte as before.
        """
        # query_embeddings=, not query_texts= — this is the main DualZoneRAG
        # retrieval path (assistant_tools, chat_view, agents/archivist). Letting
        # chroma embed the question would use whatever each collection
        # persisted, so after an embedder change the query would search the
        # old vector space while writes use the new one.
        try:
            from puffpai.embeddings import embed_documents
            query_kwargs = {
                "query_embeddings": embed_documents([query_text]),
                "n_results": n_results,
            }
            if where is not None:
                query_kwargs["where"] = where
            results = collection.query(**query_kwargs)
        except Exception:
            return []

        if not results or not results.get('documents'):
            return []

        documents = results['documents'][0]
        distances = results.get('distances', [[]])[0]
        metadatas = results.get('metadatas', [[]])[0]

        wrapped = []
        for i, doc in enumerate(documents):
            meta = metadatas[i] if i < len(metadatas) else {}
            score = distances[i] if i < len(distances) else 999.0

            wrapped.append(RAGResult(
                text=doc,
                source=source,
                title=meta.get('title', ''),
                score=score,
                metadata=meta,
            ))

        return wrapped

    # ── Health check ────────────────────────────────────────────────

    def health_check(self, libraries: list | None = None,
                     test_query: str = "test",
                     skip_threshold: int = 300) -> list[dict]:
        """Check the health of all indexed ChromaDB collections.

        For each collection, reports:
          - name: collection name
          - library: human-readable library name
          - chunks: number of stored chunks
          - status: "healthy" | "partial" | "empty" | "error"
          - queryable: True if a test query succeeds
          - error: error message (if any)

        Args:
            libraries: List of library dicts from indexed_libraries.json.
                Each dict must have ``collection`` and ``library`` keys.
                If *None*, loads the manifest from the default path.
            test_query: A sample query string to test retrieval.
            skip_threshold: Collections with >= this many chunks are
                considered "healthy".

        Returns:
            List of health-report dicts, one per collection.
        """
        if libraries is None:
            libraries = self._load_libraries_manifest()

        client = self.client
        report: list[dict] = []

        for lib in libraries:
            coll_name = lib['collection']
            lib_name = lib.get('library', coll_name)
            entry: dict = {
                'name': coll_name,
                'library': lib_name,
                'chunks': 0,
                'status': 'empty',
                'queryable': False,
                'error': None,
            }

            try:
                coll = client.get_collection(name=coll_name)
                count = coll.count()
                entry['chunks'] = count

                if count == 0:
                    entry['status'] = 'empty'
                elif count < skip_threshold:
                    entry['status'] = 'partial'
                else:
                    entry['status'] = 'healthy'

                # Test-query to detect HNSW corruption. Embed with the
                # configured function rather than query_texts=: chroma would
                # otherwise use whatever the collection persisted, so after
                # an embedder change this probe would hit a dimension
                # mismatch and report every healthy collection as corrupt.
                if count > 0:
                    from puffpai.embeddings import embed_documents
                    results = coll.query(
                        query_embeddings=embed_documents([test_query]),
                        n_results=1)
                    if results and results.get('documents') and results['documents'][0]:
                        entry['queryable'] = True
                    else:
                        entry['status'] = 'error'
                        entry['error'] = 'Query returned no documents'

            except Exception as exc:
                entry['status'] = 'error'
                entry['error'] = str(exc)

            report.append(entry)

        return report

    @staticmethod
    def _load_libraries_manifest() -> list:
        """Load indexed_libraries.json from the default location."""
        import os
        import json
        # Walk up from this file to repo root
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        manifest = os.path.join(
            repo_root, 'src', 'puffpai', 'indexed_libraries.json')
        with open(manifest, 'r', encoding='utf-8') as f:
            return json.load(f)

    # ── Convenience add methods ──────────────────────────────────────

    def add_to_vault(self, doc_id: str, text: str, metadata: dict | None = None,
                     owner: str | None = None):
        """Convenience: add a single document directly to the vault collection.

        Multi-user (Increment 1): stamps an ``owner`` (principal) on the Zone-1
        row so the query-side owner filter can restrict reads to that principal.
        ``owner`` defaults to the household OWNER (also how untagged legacy rows
        are attributed), so a caller that does not specify one keeps the row in
        the owner's Zone-1. This metadata is inert until the query passes a
        ``vault_owner`` (i.e. until the multi-user flag drives a filtered read).
        """
        if self._vault is None:
            raise RuntimeError("Vault collection not configured")
        meta = metadata or {}
        meta['zone'] = 'puffpai_vault'
        try:
            from puffpai.security.multiuser import record_owner
            meta['owner'] = record_owner(owner)
        except Exception:
            meta.setdefault('owner', 'owner')
        from puffpai.embeddings import embed_documents
        self._vault.add(ids=[doc_id], documents=[text], metadatas=[meta],
                        embeddings=embed_documents([text]))

    def add_to_knowledge(self, doc_id: str, text: str, metadata: dict | None = None):
        """Convenience: add a single document to the knowledge collection."""
        if self._knowledge is None:
            raise RuntimeError("Knowledge collection not configured")
        meta = metadata or {}
        meta['zone'] = 'puffpai_knowledge'
        from puffpai.embeddings import embed_documents
        self._knowledge.add(ids=[doc_id], documents=[text], metadatas=[meta],
                            embeddings=embed_documents([text]))
