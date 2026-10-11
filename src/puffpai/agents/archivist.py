"""Archivist — WF-4 memory & knowledge (the only path to Zone-1).

The Archivist is the sole holder of vault access and the dual-zone RAG
(``AgentScope.ARCHIVIST``). This is the load-bearing split from Cortex: the agent
that renders arbitrary conversation (Concierge) is *structurally unable* to read
Zone-1. It must ask the Archivist, which enforces, per request:

* **Requester identity** — whose memory/vault is being touched.
* **Privacy mode** — a *kid* profile is Zone-2 (knowledge) only; vault results are
  never returned and the query is never run authenticated. Zone policy is
  enforcement, not metadata.
* **Zone policy** — vault (Zone-1) results require an authenticated vault session
  *and* a non-kid profile; otherwise only Zone-2 knowledge is queried.

Composition: wraps :class:`DualZoneRAG`, :class:`Vault`, and :class:`UserMemory`.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from puffpai.logging_config import get_logger
from puffpai.security.audit_log import AuditLog
from puffpai.security.secrets_provider import AgentScope, SecretsProvider

from .base import BaseAgent
from .envelope import Envelope

logger = get_logger(__name__)

# Profile identities that are restricted to Zone-2 (kid-safe) knowledge only.
_KID_PROFILES = {"child", "kid", "minor"}


def _is_kid(envelope: Envelope) -> bool:
    p = envelope.payload or {}
    profile = str(p.get("profile", "")).lower()
    privacy_mode = str(p.get("privacy_mode", "")).lower()
    return (
        profile in _KID_PROFILES
        or privacy_mode in _KID_PROFILES
        or str(envelope.requester).lower() in _KID_PROFILES
    )


class ArchivistAgent(BaseAgent):
    """Memory + knowledge retrieval with zone/privacy enforcement."""

    name = "archivist"
    scope = AgentScope.ARCHIVIST

    def __init__(self, *, secrets: SecretsProvider | None = None,
                 rag=None, vault=None, memory_factory=None,
                 audit: AuditLog | None = None) -> None:
        super().__init__(secrets=secrets)
        self._rag = rag
        self._vault = vault
        self._memory_factory = memory_factory  # callable(user_id) -> UserMemory
        self._audit = audit or AuditLog()

    # ── engine accessors (lazy, guarded) ────────────────────────────
    def _rag_engine(self):
        if self._rag is None:
            from puffpai.knowledge.rag import DualZoneRAG
            self._rag = DualZoneRAG()
        return self._rag

    def _memory(self, user_id: str):
        if self._memory_factory is not None:
            return self._memory_factory(user_id)
        from puffpai.user_memory import UserMemory
        return UserMemory(user_id)

    def _session_unlocked(self, session_id) -> bool:
        """True only if a vault is available AND the given session is a live,
        unlocked vault session (holds a usable encryption key).

        Fail-closed: a missing session id, no injected vault (so unlock cannot
        be verified), or any error verifying it → False. This is the Zone-1
        gate: a mere non-empty ``vault_session_id`` string is NOT proof of an
        unlocked vault, so it must never by itself authorize a vault query.
        """
        if not session_id:
            return False
        vault = self._vault
        if vault is None:
            # Cannot verify the session is unlocked -> refuse Zone-1 access.
            return False
        try:
            return bool(vault.is_unlocked(session_id))
        except Exception:
            return False

    # ── scrub-and-vault (Archivist owns the quality gate + memory sink) ──
    def vault_quality_gate(self, pii_type: str, value: str) -> bool:
        """Archivist's decision: is this GOOD data worth vaulting?

        Thin accessor over :mod:`puffpai.agents.pii_quality_gate`, the module
        the Archivist owns. Exposed so the request-path choke point can consult
        the gate through the agent that actually holds Zone-1 authority.
        """
        from .pii_quality_gate import is_vault_worthy
        return is_vault_worthy(pii_type, value)

    def _scrub_memory_text(self, envelope: Envelope) -> str:
        """Return the text to memorize: scrubbed clean_text when the capability
        is enabled, otherwise the raw text (current behavior, unchanged).

        Fail-closed: if the choke point raises for any reason, fall back to the
        classifier's deterministic redaction rather than memorizing raw PII.
        """
        p = envelope.payload or {}
        raw = str(p.get("text", ""))
        try:
            from puffpai.security.scrub_and_vault import (
                scrub_and_vault, scrub_and_vault_active,
            )
            # Go-live interlock: vault only when the flag is on AND a PIN-recovery
            # code exists; otherwise memorize raw (current flag-off behavior).
            if not scrub_and_vault_active(vault=self._vault):
                return raw
            san = scrub_and_vault(
                raw,
                user_id=str(envelope.requester),
                session_id=p.get("vault_session_id"),
                source="memory",
                vault=self._vault,
            )
            return san.clean_text
        except Exception as exc:  # never memorize raw on an unexpected failure
            logger.error("archivist: memory scrub failed, redacting: %s", exc)
            try:
                from puffpai.security.data_classifier import DataClassifier
                redacted, _ = DataClassifier().redact_pii(raw)
                return redacted
            except Exception:
                return "[redacted: PII scan unavailable]"

    # ── request handling ────────────────────────────────────────────
    def handle(self, envelope: Envelope) -> dict:
        intent = envelope.intent
        p = envelope.payload or {}
        try:
            if intent in ("retrieve", "query", "knowledge"):
                return self._retrieve(envelope)
            if intent in ("get_memory", "memory_context"):
                mem = self._memory(str(envelope.requester))
                return {"ok": True, "context": mem.get_memory_context()}
            if intent == "remember":
                mem = self._memory(str(envelope.requester))
                # SCRUB-AND-VAULT sink: long-term memory must never ingest raw
                # PII. When the capability is enabled, route the text through the
                # request-path choke point so only clean_text (handles / redacted
                # placeholders) reaches extract_and_store; the original good
                # values are vaulted, not memorized. Flag OFF -> unchanged.
                mem.extract_and_store(self._scrub_memory_text(envelope))
                mem.save()
                return {"ok": True}
            return {"ok": False, "error": f"archivist: unknown intent {intent!r}"}
        except KeyError as exc:
            return {"ok": False, "error": f"archivist: missing payload key {exc}"}

    def _retrieve(self, envelope: Envelope) -> dict:
        p = envelope.payload or {}
        query = str(p.get("query", ""))
        n_results = int(p.get("n_results", 5))
        kid = _is_kid(envelope)

        # Zone policy: vault (Zone-1) requires BOTH a VERIFIED-unlocked vault
        # session and a non-kid profile. Kid profiles are hard-pinned to Zone-2
        # knowledge only. Fail-closed: we verify the session is actually unlocked
        # against the vault (via _session_unlocked) rather than trusting the mere
        # presence of a vault_session_id string — an expired/bogus/never-unlocked
        # id must not open Zone-1.
        session_unlocked = self._session_unlocked(p.get("vault_session_id"))
        authenticated = session_unlocked and not kid
        knowledge_only = kid or not authenticated

        # Multi-user (Increment 1): scope Zone-1 retrieval to the requesting
        # principal so the owner's session never surfaces the other adult's vault
        # content and vice versa. Zone-2 knowledge stays shared. Passed only when
        # flag is on and a vault query will actually run; None otherwise keeps
        # today's unfiltered behavior.
        vault_owner = None
        try:
            from puffpai.security.multiuser import multiuser_enabled
            if multiuser_enabled() and authenticated and not knowledge_only:
                vault_owner = str(envelope.requester)
        except Exception:
            vault_owner = None

        rag = self._rag_engine()
        # Pass vault_owner ONLY when multi-user actually scopes this query, so
        # the flag-off call is byte-for-byte the old signature (a rag engine or
        # test double predating the param keeps working).
        query_kwargs = dict(
            n_results=n_results,
            authenticated=authenticated,
            knowledge_only=knowledge_only,
        )
        if vault_owner is not None:
            query_kwargs["vault_owner"] = vault_owner
        response = rag.query(query, **query_kwargs)

        zones = ["knowledge"]
        if authenticated and not knowledge_only:
            zones.append("vault")

        self._audit.log(
            action="archivist_retrieve",
            user=str(envelope.requester),
            result="success",
            details=f"kid={kid} zones={zones}",
            extra={"zones_queried": zones, "kid_profile": kid,
                   "authenticated": authenticated},
        )

        return {
            "ok": True,
            "kid_profile": kid,
            "authenticated": authenticated,
            "zones_queried": zones,
            "vault_count": getattr(response, "vault_count", 0),
            "knowledge_count": getattr(response, "knowledge_count", 0),
            "context": response.context_string() if hasattr(response, "context_string") else "",
        }

    def health(self) -> dict:
        base = super().health()
        base["rag_available"] = self._rag is not None or True  # lazy-constructible
        return base


__all__ = ["ArchivistAgent"]
