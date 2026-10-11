"""Scrub-and-Vault — the request-path choke point for PII.

This module is a NATIVE, always-available PuffPAI capability. Once wired in, the
request pipeline runs every user-visible message through :func:`scrub_and_vault`
before that text can reach any LLM-visible surface (the prompt, chat history,
long-term memory). There is no external actor and no per-message human decision.

The two halves of the name are deliberately different in kind:

* **SCRUB** — reflexive, deterministic, always-on. Every detected sensitive span
  is removed from the outgoing text. This never depends on any agent's judgement
  and it fails closed: if anything goes wrong we redact rather than emit raw.

* **VAULT** — a quality-gated decision owned by the Archivist
  (:mod:`puffpai.agents.pii_quality_gate`). The ORIGINAL value is stored only if
  it is GOOD, real data. Test / placeholder / structurally-invalid data is
  scrubbed anyway but NOT stored.

After the decision, the span in the outgoing ``clean_text`` is replaced by:
  * a handle ``{{vault:<ref-id>}} (<label>)`` when the value was vaulted — the
    opaque ref-id lets a FUTURE resolver rehydrate it on an authorized surface,
    and the plain-language label (e.g. "your phone number") lets the model still
    reason about the turn; or
  * a plain ``[REDACTED-<TYPE>]`` placeholder when it was scrubbed-not-vaulted.

The raw value NEVER appears in ``clean_text`` in either case.

Feature flag: the NEW vaulting behavior (handle substitution + persisting the
original into the ref store) is gated by :func:`scrub_and_vault_enabled`, which
reads the ``PUFFPAI_SCRUB_AND_VAULT`` environment variable and DEFAULTS OFF.
Callers gate on it so that, until it is deliberately turned on, the live paths
keep their current behavior. It is a config attr in spirit; it is read from the
environment here (not added to puffpai.config) so this increment ships without
editing the shared config module. See the flag docstring for exactly what it
gates and why it ships off.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from puffpai.logging_config import get_logger

logger = get_logger(__name__)


# ── status codes ────────────────────────────────────────────────────────────
STATUS_OK = "OK"                        # scrub completed; vaulting (if any) ok
STATUS_VAULT_LOCKED = "VAULT_LOCKED"    # PII scrubbed, but couldn't be vaulted
STATUS_CLASSIFIER_ERROR = "CLASSIFIER_ERROR"  # scan failed; failed closed
STATUS_DISABLED = "DISABLED"            # feature flag off (informational)


# The tier each detected type is assigned, mirroring PIIDetection.severity so
# the vault record carries a sensitivity tier without re-deriving it.
_TYPE_TIER = {
    "ssn": "critical",
    "credit_card": "critical",
    # child.any is critical and health.condition is high, matching
    # PIIDetection.severity. The app-data ladder (app_data_tags) calls the
    # STRUCTURED versions of both P4 because a schema position is proof; a
    # free-text mention is a weaker signal, so it is tiered on its own evidence
    # rather than rounded up to match. P4 -> critical, P3 -> high is the bridge.
    "child.any": "critical",
    "address": "high",
    "date_of_birth": "high",
    "health.condition": "high",
    "email": "medium",
    "phone": "medium",
    "name": "medium",
}

# The [REDACTED-<TYPE>] token per type — matches the classifier's own tokens so
# scrubbed-not-vaulted output is indistinguishable from a normal redaction.
_REDACT_TOKEN = {
    "ssn": "[REDACTED-SSN]",
    "credit_card": "[REDACTED-CC]",
    "date_of_birth": "[REDACTED-DOB]",
    "address": "[REDACTED-ADDRESS]",
    "email": "[REDACTED-EMAIL]",
    "phone": "[REDACTED-PHONE]",
    "name": "[REDACTED-NAME]",
    "health.condition": "[REDACTED-HEALTH]",
    "child.any": "[REDACTED-CHILD]",
}

# Generic, non-sensitive label that rides alongside a vault handle so the model
# can still reason about what was removed without seeing the value.
_LABEL = {
    "ssn": "your SSN",
    "credit_card": "your credit card number",
    "date_of_birth": "your date of birth",
    "address": "your home address",
    "email": "your email address",
    "phone": "your phone number",
    "name": "a name",
    "health.condition": "a health detail",
    "child.any": "information about a child",
}

# Substitution order: most-specific / longest patterns first, so a broad pattern
# cannot clobber a span already replaced by a specific one (same order the
# classifier's redact_pii uses).
_ORDER = ("ssn", "credit_card", "date_of_birth",
          "address", "email", "phone", "name",
          # Last on purpose: the health term is a bare word ("insulin", "meds")
          # and a bare word must never be substituted into a span a
          # longer/more-specific pattern is still going to claim.
          "health.condition", "child.any")


@dataclass
class Sanitized:
    """Result of running text through the scrub-and-vault choke point."""
    clean_text: str
    handles: list = field(default_factory=list)   # [{ref_id,type,tier,label}]
    scrubbed_not_vaulted_count: int = 0
    vaulted_count: int = 0
    status: str = STATUS_OK
    pii_detected: bool = False
    pii_summary: str | None = None
    pii_types: list = field(default_factory=list)
    severity: str = "none"


def scrub_and_vault_enabled() -> bool:
    """True if the NEW scrub-and-vault vaulting behavior is turned on.

    Reads ``PUFFPAI_SCRUB_AND_VAULT`` (default OFF). Truthy values: 1, true,
    yes, on (case-insensitive). Anything else — including unset — is OFF, so the
    capability fails safe toward the current, reviewed behavior.

    WHAT THE FLAG GATES (only these; the always-on reflexive scrub of log lines
    and the voice path is NOT gated by it):
      * substituting ``{{vault:<ref-id>}}`` handles into the outgoing text, and
      * persisting the ORIGINAL good values into the encrypted ``pii_refs.json``
        reference store.
    WHY IT SHIPS OFF:
      * handles are only safe once a PIN recovery code exists, so a forgotten
        PIN is not permanent loss of everything the handles point at, and
      * a resolver must be able to rehydrate the handles, so handles are never
        minted in the live paths without a deliberate, reviewed opt-in.
    """
    return os.environ.get("PUFFPAI_SCRUB_AND_VAULT", "").strip().lower() in (
        "1", "true", "yes", "on")


# ── Go-live interlock: recovery-code requirement ────────────────────────────
#
# The raw flag (scrub_and_vault_enabled) is NOT sufficient to start vaulting live
# data. Once scrub-and-vault mints handles, the vault holds the ONLY copy of the
# scrubbed originals — so a forgotten PIN with no recovery code would be permanent
# loss. The ACTIVATION decision (scrub_and_vault_active) therefore requires BOTH
# the flag AND an enrolled PIN-recovery code. When the flag is on but no recovery
# code exists, the write/vault callers fall back to their legacy (flag-off)
# behavior — they do NOT vault — and a prominent one-time warning is logged.
#
# NOTE ON SCOPE: only the WRITE/VAULT-on-ingest path is gated by this interlock.
# The read-side rehydration (resolve_handle / the reveal endpoint) and the
# recovery endpoints themselves stay gated on the raw flag only — they must keep
# working with no recovery code (the recovery endpoints are the PREREQUISITE for
# minting one, and resolve/reveal must still open handles that were minted while a
# code existed, even after that code is later consumed by recover_with_code).
#
# PERFORMANCE: has_recovery_code is a keyring-file read. The result is cached
# (keyed by the vault's keyring path so distinct vaults never share an entry) and
# invalidated explicitly whenever the recovery wrap changes (generate/consume),
# with a short TTL backstop for any path that does not call the hook.
_RECOVERY_CACHE_TTL = 45.0  # seconds; backstop only — invalidation is the primary
_recovery_cache_lock = threading.Lock()
_recovery_cache: dict[str, tuple[bool, float]] = {}  # keyring-path -> (present, ts)
_warned_no_recovery = False  # one-time "enabled but no recovery code" latch


def invalidate_recovery_code_cache() -> None:
    """Drop the cached ``has_recovery_code`` result so the NEXT activation check
    re-reads the keyring.

    Wired into the recovery-code generate success path and into
    :meth:`Vault.recover_with_code` (which consumes the recovery wrap) so that
    minting a code flips activation LIVE — and consuming one flips it back —
    without a restart. Also re-arms the one-time "no recovery code" warning so a
    later return to the no-code state warns again.
    """
    global _warned_no_recovery
    with _recovery_cache_lock:
        _recovery_cache.clear()
        _warned_no_recovery = False


def _recovery_cache_key(vault) -> str | None:
    """Stable cache key for a vault: its keyring file path (unique per vault_dir).
    None if it cannot be determined (then the result is simply not cached)."""
    try:
        return str(vault._keyring_file)
    except Exception:
        return None


def _vault_has_recovery_code(vault) -> bool:
    """Cached read of ``vault.has_recovery_code`` (a keyring-file check).

    Avoids parsing the keyring on every message. Cache is invalidated explicitly
    via :func:`invalidate_recovery_code_cache` and expires after
    ``_RECOVERY_CACHE_TTL`` as a backstop. Fail-closed: a None vault or any error
    reading it is treated as NO recovery code (activation stays OFF).
    """
    if vault is None:
        return False
    key = _recovery_cache_key(vault)
    now = time.time()
    if key is not None:
        with _recovery_cache_lock:
            hit = _recovery_cache.get(key)
        if hit is not None and (now - hit[1]) < _RECOVERY_CACHE_TTL:
            return hit[0]
    # Compute outside the lock (keyring read); a rare concurrent double-read is
    # harmless (same answer written twice).
    try:
        present = bool(vault.has_recovery_code)
    except Exception:
        present = False
    if key is not None:
        with _recovery_cache_lock:
            _recovery_cache[key] = (present, now)
    return present


def _warn_recovery_missing_once() -> None:
    """Emit the prominent one-time warning that the feature is enabled but inert
    because no recovery code exists. Latched so it fires once per no-code state
    (re-armed by invalidate_recovery_code_cache). Never logs any secret."""
    global _warned_no_recovery
    with _recovery_cache_lock:
        if _warned_no_recovery:
            return
        _warned_no_recovery = True
    logger.warning(
        "Scrub & Vault is ENABLED (PUFFPAI_SCRUB_AND_VAULT) but NO PIN recovery "
        "code exists -- it will NOT activate until you generate one "
        "(POST /vault/recovery/generate). Refusing to vault live data with no "
        "recovery net; running legacy passthrough until a code is minted."
    )


def scrub_and_vault_active(vault=None) -> bool:
    """The ACTIVATION decision for live scrub-and-vault (the go-live interlock).

    Returns True ONLY when BOTH hold:
      (a) the ``PUFFPAI_SCRUB_AND_VAULT`` flag is set (scrub_and_vault_enabled),
          AND
      (b) the vault has an enrolled PIN-recovery code (``vault.has_recovery_code``,
          cached).

    When the flag is on but NO recovery code exists this returns False, so every
    write/vault caller keeps its current flag-OFF legacy behavior (it does NOT
    vault), and a prominent one-time warning is logged. Flag off -> False with no
    vault read and no warning. Fail-closed: if the vault cannot be resolved or
    read, returns False.

    ``vault`` may be passed by callers that already hold it (the chat/voice choke
    point, the Archivist, the upload path); when omitted it is resolved via
    ``api_server._get_vault`` exactly like :func:`scrub_and_vault`.
    """
    if not scrub_and_vault_enabled():
        return False
    if vault is None:
        try:
            from puffpai.api_server import _get_vault
            vault = _get_vault()
        except Exception:
            vault = None
    if _vault_has_recovery_code(vault):
        return True
    _warn_recovery_missing_once()
    return False


def _new_ref_id() -> str:
    """Opaque, random, fixed-length reference id.

    128 random bits, base32, padding stripped -> 26 chars of [A-Z2-7]. NOT
    derived from the value (so the id leaks nothing and two identical values get
    distinct ids).
    """
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def _detection_items(detection):
    """Yield (type, value) pairs from a PIIDetection in substitution order."""
    buckets = {
        "ssn": detection.ssns,
        "credit_card": detection.credit_cards,
        "date_of_birth": detection.dates_of_birth,
        "address": detection.addresses,
        "email": detection.emails,
        "phone": detection.phones,
        "name": detection.names,
        "health.condition": detection.health_conditions,
        "child.any": detection.child_indicators,
    }
    for t in _ORDER:
        for value in buckets.get(t, []):
            v = value.strip() if isinstance(value, str) else value
            if v:
                yield t, v


def redact_for_log(text: str, classifier=None, limit: int = 80) -> str:
    """Return a PII-safe, length-capped slice of ``text`` for a log line.

    ALWAYS-ON (flag-independent): this only sanitizes a string about to be
    printed, it stores nothing and mints no handles. Log lines use it instead
    of a raw ``user_message[:80]`` slice. Fails closed: if the classifier is
    unavailable or raises, returns a generic placeholder rather than the raw
    slice.
    """
    if not text:
        return ""
    try:
        if classifier is None:
            from puffpai.security.data_classifier import DataClassifier
            classifier = DataClassifier()
        # Health is INCLUDED here unconditionally. A log line is never the
        # reason a household needs its medication list, so there is no utility
        # to weigh against it: journald and puffpai.log are read over SSH, get
        # copied into bug reports, and outlive the conversation.
        try:
            redacted, _ = classifier.redact_pii(text, include_health=True)
        except TypeError:
            redacted, _ = classifier.redact_pii(text)
        return redacted[:limit]
    except Exception:
        return "[redacted: pii-scan unavailable]"


def redact_for_prompt(text, classifier=None) -> str:
    """Redact raw PII from Zone-1 (vault) content just before it is injected
    into an LLM prompt/context. READ-SIDE defense-in-depth.

    Increment 1 cleaned the WRITE side, but retrieved Zone-1 content can still
    carry raw PII: legacy content stored before scrub-and-vault existed, or
    content that reached the vault by a path that never scrubbed. When such a
    chunk is about to be re-injected into the prompt, this pass redacts any raw
    PII deterministically (``[REDACTED-<TYPE>]``).

    This is scrub-only: it NEVER vaults and NEVER mints new handles (that is the
    write side's job). Existing ``{{vault:<ref-id>}}`` handles are left intact —
    they do not match any PII pattern, so a chunk already carrying handles is not
    double-processed. Zone-2 (general knowledge) content must NOT be passed here;
    it stays fast and unredacted.

    Fails closed: if the classifier is unavailable or raises, returns a generic
    placeholder rather than the raw text, so a scan failure can never re-surface
    raw vault PII into the prompt.
    """
    if not text:
        return text
    try:
        if classifier is None:
            from puffpai.security.data_classifier import DataClassifier
            classifier = DataClassifier()
        redacted, _ = classifier.redact_pii(text)
        return redacted
    except Exception:
        return "[redacted: vault content scan unavailable]"


def scrub_and_vault(text, user_id=None, session_id=None, source="chat",
                    *, vault=None, classifier=None, include_tiers=None,
                    include_health=False) -> Sanitized:
    """Scrub every detected PII span from ``text``; vault the good originals.

    This is the choke point. For each detected span it ALWAYS removes the value
    from ``clean_text``; it then asks the Archivist quality gate whether the
    value is worth keeping. GOOD values are encrypted into the reference store
    and replaced by a ``{{vault:<ref-id>}} (<label>)`` handle; test/bad values
    (and any value that cannot be vaulted because the vault is locked) are
    replaced by ``[REDACTED-<TYPE>]``.

    Fail-closed contract:
      * classifier raises  -> status CLASSIFIER_ERROR, clean_text carries NO raw
        value (a generic placeholder is returned instead).
      * vault locked / write fails -> the span is scrubbed (never left raw) and
        status is VAULT_LOCKED.

    Parameters ``user_id`` / ``session_id`` / ``source`` are recorded on the
    vault reference for the future resolver/audit; the vault itself encrypts
    under any currently-live session key (single-PIN vault), so no PIN is needed
    here.

    ``include_tiers`` scopes WHICH sensitivity tiers are acted on. ``None``
    (the default, used by the chat path) means all tiers — critical, high, and
    medium — are scrubbed/vaulted, so the chat contract is unchanged. A caller
    may pass a subset (e.g. ``frozenset({"critical", "high"})``) to act only on
    those tiers and LEAVE spans of the excluded tiers in ``clean_text`` verbatim.
    This is how the document-upload path preserves MEDIUM-tier PII (a doctor's
    name, an email) so redaction does not gut document utility, while still
    vaulting/redacting high/critical values. Excluded-tier spans are never
    vaulted and never counted toward the VAULT_LOCKED downgrade.

    ``include_health`` turns on the free-text ``health.condition`` detector and
    DEFAULTS OFF, so ingestion is unaffected. The conversational callers
    (chat, voice) pass True: otherwise a medication named in a chat turn would
    be persisted to chat history in the clear, because none of the seven format
    detectors can see it. Note that ``is_vault_worthy`` does not recognise the
    class, so a health mention is always SCRUBBED-NOT-VAULTED, which makes the
    honesty notice ("I
    spotted a health detail and did not keep it") a true sentence rather than a
    hedge.
    """
    text = text if isinstance(text, str) else ("" if text is None else str(text))

    # ── Detect (fail closed on scan error) ──────────────────────────────
    try:
        if classifier is None:
            from puffpai.security.data_classifier import DataClassifier
            classifier = DataClassifier()
        try:
            detection = classifier.scan_for_pii(
                text, include_health=include_health)
        except TypeError:
            # A classifier injected by an older caller / test double may not
            # accept the keyword. Degrade to the legacy seven detectors rather
            # than treating a signature mismatch as a scan failure — the latter
            # would replace the whole message with a placeholder.
            detection = classifier.scan_for_pii(text)
    except Exception as exc:
        logger.error("scrub_and_vault: classifier error: %s", exc)
        # Fail closed: we could not identify spans, so we must not emit raw.
        return Sanitized(
            clean_text="[redacted: PII scan unavailable]",
            status=STATUS_CLASSIFIER_ERROR,
        )

    result = Sanitized(
        clean_text=text,
        pii_detected=detection.has_pii,
        pii_summary=detection.summary() if detection.has_pii else None,
        pii_types=detection.pii_types if detection.has_pii else [],
        severity=detection.severity,
    )
    if not detection.has_pii:
        return result

    # Resolve the quality gate (Archivist-owned) and the vault once.
    try:
        from puffpai.agents.pii_quality_gate import is_vault_worthy
    except Exception as exc:  # pragma: no cover - import guard
        logger.error("scrub_and_vault: gate unavailable: %s", exc)
        is_vault_worthy = lambda *_a, **_k: False  # noqa: E731 - fail toward scrub

    if vault is None:
        try:
            from puffpai.api_server import _get_vault
            vault = _get_vault()
        except Exception:
            vault = None

    # Can we vault at all right now? has_live_session is the vault's own
    # read-only fail-closed guard (initialized AND a live session holds a key).
    vault_writable = False
    if vault is not None:
        try:
            vault_writable = bool(vault.has_live_session)
        except Exception:
            vault_writable = False

    clean = text
    status = STATUS_OK

    for pii_type, value in _detection_items(detection):
        # A broader pattern may already have consumed this span; skip if gone.
        if value not in clean:
            continue
        # Unmapped types default to "critical" (fail-closed): an unknown type is
        # treated as maximally sensitive, so it is always scrubbed and — in the
        # upload path's {critical,high} scope — never preserved verbatim. If a
        # future PII type is added to DataClassifier but not to _TYPE_TIER, it
        # is redacted/vaulted rather than silently leaking into the index.
        tier = _TYPE_TIER.get(pii_type, "critical")

        # Tier scoping: an excluded tier is left in clean_text verbatim (never
        # scrubbed, never vaulted). Used by the upload path to preserve medium.
        if include_tiers is not None and tier not in include_tiers:
            continue

        vaulted = False
        if vault_writable and is_vault_worthy(pii_type, value):
            ref_id = _new_ref_id()
            record = {
                "type": pii_type,
                "tier": tier,
                "value": value,
                "created": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "user_id": user_id,
            }
            try:
                ok = vault.append_pii_ref(ref_id, record)
            except Exception:
                ok = False
            if ok:
                label = _LABEL.get(pii_type, "sensitive data")
                clean = clean.replace(value, f"{{{{vault:{ref_id}}}}} ({label})")
                result.handles.append({
                    "ref_id": ref_id, "type": pii_type,
                    "tier": tier, "label": label,
                })
                result.vaulted_count += 1
                vaulted = True
            else:
                # Wanted to vault but couldn't (locked / write failed): the
                # value MUST still be scrubbed, and we surface the locked state.
                status = STATUS_VAULT_LOCKED

        if not vaulted:
            token = _REDACT_TOKEN.get(pii_type, "[REDACTED]")
            clean = clean.replace(value, token)
            result.scrubbed_not_vaulted_count += 1

    # If there was vault-worthy PII but the vault was never writable, make the
    # locked state explicit even though every span was safely scrubbed.
    if status == STATUS_OK and not vault_writable and result.pii_detected:
        # Only downgrade to VAULT_LOCKED if something would actually have been
        # vaultable; a message whose only PII is test data stays OK.
        for pii_type, value in _detection_items(detection):
            # Fail-closed default (see the main loop): unmapped -> critical.
            tier = _TYPE_TIER.get(pii_type, "critical")
            if include_tiers is not None and tier not in include_tiers:
                continue
            if is_vault_worthy(pii_type, value):
                status = STATUS_VAULT_LOCKED
                break

    result.clean_text = clean
    result.status = status
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Handle resolver (rehydration) — the READ side of Scrub-and-Vault
# ═══════════════════════════════════════════════════════════════════════════
#
# scrub_and_vault() WRITES {{vault:<ref-id>}} handles; this is the only
# sanctioned way to turn one back into its original value. It is deliberately
# narrow: rehydrating a scrubbed secret into a chat prompt, the conversation
# history, long-term memory, the RAG index, or a log would re-create exactly the
# leak the scrub just closed. So the resolver NEVER "returns text to whoever
# asked" — it delivers plaintext only into an explicitly-authorized, non-LLM,
# non-persistent SINK, and it gates that delivery on the value's sensitivity
# tier (medium / high / critical), failing closed at every step.

# ── resolver status codes ──────────────────────────────────────────────────
STATUS_RESOLVE_OK = "OK"                       # delivered to the sink
STATUS_NOT_FOUND = "NOT_FOUND"                 # ref-id unknown / dangling
STATUS_SINK_REFUSED = "SINK_REFUSED"           # sink is unauthorized (LLM/persist)
STATUS_STEP_UP_REQUIRED = "STEP_UP_REQUIRED"   # critical tier needs fresh re-auth
STATUS_RESOLVE_ERROR = "RESOLVE_ERROR"         # unexpected failure (fail closed)

# Sink kinds that may NEVER receive rehydrated plaintext. Enforced by NAME in
# addition to the authorized-flag, so a store mistakenly tagged authorized still
# cannot receive a value if its kind names an LLM-context / persistence surface.
_FORBIDDEN_SINK_KINDS = frozenset({
    "llm_context", "prompt", "chat_history", "conversation_history",
    "user_memory", "memory", "rag_index", "rag", "index", "persistence",
    "store", "log", "logs", "audit",
})

# Step-up proofs (a TOTP code, etc.) are single-use for a short window so a code
# captured from one critical resolve cannot be replayed into another. We store
# only a salted hash of the proof, never the proof itself.
_STEP_UP_TTL_SECONDS = 90
_consumed_step_up: dict[str, float] = {}
_STEP_UP_SALT = secrets.token_bytes(16)  # process-local; not persisted

# ref-id shape minted by _new_ref_id(): base32 (A-Z2-7), no padding.
_REF_ID_RE = re.compile(r"^[A-Z2-7]+$")
_HANDLE_RE = re.compile(r"\{\{vault:([A-Z2-7]+)\}\}")


class ResolveSink:
    """Base class for a rehydration destination.

    A sink is the ONLY way plaintext leaves the resolver. Subclasses that are
    safe to receive a secret set ``authorized = True`` and a ``kind`` that does
    NOT name an LLM/persistence surface. The default is unauthorized, so an
    arbitrary object (or a persistence store passed by mistake) is refused.
    """
    authorized: bool = False
    kind: str = "unknown"

    def write(self, plaintext: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class FormFieldSink(ResolveSink):
    """Authorized sink: a single outbound form field the owner is filling in.

    Holds the delivered value in memory for the immediate outbound action (e.g.
    posting a form). It is NOT a persistence store and is never fed to an LLM.
    """
    authorized = True
    kind = "form_field"

    def __init__(self, field_name: str):
        self.field_name = field_name
        self.value: str | None = None

    def write(self, plaintext: str) -> None:
        self.value = plaintext


class UserRevealSink(ResolveSink):
    """Authorized sink: a one-time, owner-requested reveal (e.g. show me my SSN).

    The value lands here for a single user-facing display the owner explicitly
    asked for; the caller must render it directly and must not route it back into
    any model context or store.
    """
    authorized = True
    kind = "user_reveal"

    def __init__(self):
        self.value: str | None = None

    def write(self, plaintext: str) -> None:
        self.value = plaintext


class LLMContextSink(ResolveSink):
    """FORBIDDEN sink (prompt / chat history / conversation). Never authorized.

    Exists so callers can name this destination explicitly and be REFUSED — and
    so the refusal is unit-tested. Its write() must never run.
    """
    authorized = False
    kind = "llm_context"

    def write(self, plaintext: str) -> None:  # pragma: no cover - must never run
        raise AssertionError("plaintext must never be written to an LLM sink")


class PersistenceSink(ResolveSink):
    """FORBIDDEN sink (user_memory / RAG index / logs / any store). Never
    authorized. Same purpose as LLMContextSink: named so it can be refused."""
    authorized = False
    kind = "persistence"

    def write(self, plaintext: str) -> None:  # pragma: no cover - must never run
        raise AssertionError("plaintext must never be written to a persistence sink")


@dataclass
class ResolveResult:
    """Outcome of a resolve_handle() call. Never carries plaintext except the
    ``value`` field, and that is populated ONLY for the medium tier (which the
    caller may display); high/critical deliver via the sink alone."""
    status: str
    ref_id: str | None = None
    tier: str | None = None
    type: str | None = None
    label: str | None = None
    delivered: bool = False
    value: str | None = None      # medium-tier convenience only; None otherwise
    detail: str | None = None     # human-readable reason (no secret content)


def _extract_ref_id(handle_or_ref_id: str) -> str | None:
    """Pull a ref-id out of a ``{{vault:<ref-id>}}`` handle or a bare ref-id.
    Returns None if the input is not a well-formed handle/ref-id."""
    if not isinstance(handle_or_ref_id, str):
        return None
    s = handle_or_ref_id.strip()
    m = _HANDLE_RE.search(s)
    if m:
        return m.group(1)
    return s if _REF_ID_RE.match(s) else None


def _sink_is_authorized(sink) -> bool:
    """Fail-closed sink check: must be a ResolveSink, flagged authorized, whose
    kind does not name an LLM/persistence surface."""
    if not isinstance(sink, ResolveSink):
        return False
    if not getattr(sink, "authorized", False):
        return False
    if getattr(sink, "kind", "unknown") in _FORBIDDEN_SINK_KINDS:
        return False
    return True


def _consume_step_up(factor: str, code: str) -> bool:
    """Register a step-up proof as spent; return False if already spent within
    the TTL (replay) so the caller refuses it. Stores only a salted hash."""
    now = time.time()
    # opportunistic cleanup of expired entries
    for h, exp in list(_consumed_step_up.items()):
        if exp <= now:
            _consumed_step_up.pop(h, None)
    digest = hashlib.sha256(
        _STEP_UP_SALT + factor.encode("utf-8") + b":" + code.encode("utf-8")
    ).hexdigest()
    if digest in _consumed_step_up:
        return False
    _consumed_step_up[digest] = now + _STEP_UP_TTL_SECONDS
    return True


def _verify_step_up(vault, step_up) -> tuple[bool, str]:
    """Verify a fresh step-up proof for a critical-tier resolve.

    Uses the required_factors()/register_auth_factor() seam: if a second factor
    is enrolled (e.g. TOTP), a fresh code for it is required and verified via its
    registered verifier, then burned (single-use). If NO second factor is
    enrolled, falls back to PIN RE-ENTRY (verified through the rate-limited PIN
    gate) — never a pass-through on the already-unlocked session.

    Returns (ok, detail). ``detail`` never contains secret material.
    """
    try:
        extra = [f for f in vault.required_factors() if f != "pin"]
    except Exception:
        extra = []

    if extra:
        if not isinstance(step_up, dict):
            return False, "second factor required for critical-tier reveal"
        for factor in extra:
            code = step_up.get(factor)
            if not code:
                return False, f"{factor} code required for critical-tier reveal"
            verifier = getattr(vault, "_factor_verifiers", {}).get(factor)
            try:
                ok = bool(verifier and verifier(code))
            except Exception:
                ok = False
            if not ok:
                return False, f"invalid {factor} code"
            if not _consume_step_up(factor, code):
                return False, f"{factor} code already used; provide a fresh code"
        return True, "second-factor step-up verified"

    # No second factor enrolled -> require fresh PIN re-entry (rate-limited).
    pin = step_up.get("pin") if isinstance(step_up, dict) else None
    if not pin:
        return False, "PIN re-entry required for critical-tier reveal"
    try:
        tmp = vault.authenticate(pin)  # rate-limited, fail-closed
    except Exception:
        return False, "invalid PIN"
    try:
        vault.logout(tmp.session_id)   # step-up proves possession; no lingering session
    except Exception:
        pass
    return True, "PIN re-entry verified"


def resolve_handle(handle_or_ref_id, session_id, sink, *, vault=None,
                   step_up=None, require_feature_flag=True,
                   caller_user_id=None) -> ResolveResult:
    """Rehydrate a ``{{vault:<ref-id>}}`` handle to its original value — safely.

    The plaintext is delivered ONLY into ``sink`` (an authorized, non-LLM,
    non-persistent :class:`ResolveSink`); it is never returned to the caller for
    high/critical values and never written to any model context, store, or log.

    Authorization (fail-closed at each gate):
      * sink must be an authorized ResolveSink                -> else SINK_REFUSED
      * the given session must be UNLOCKED                    -> else VAULT_LOCKED
      * ref-id must resolve to a stored record OWNED BY THE CALLER -> else NOT_FOUND
      * tier == "medium"   : the unlocked session suffices; value ALSO returned
      * tier == "high"     : delivered to the sink only (never returned)
      * tier == "critical" : STEP-UP re-auth required (fresh 2nd factor, or PIN
                             re-entry if none enrolled)        -> else STEP_UP_REQUIRED
      * unknown/missing tier is treated as "critical" (most restrictive).

    Multi-user owner filter (Increment 1): when ``PUFFPAI_MULTIUSER`` is on, a
    ref whose recorded owner is not ``caller_user_id`` resolves to NOT_FOUND —
    indistinguishable from a dangling ref, so the owner cannot even confirm the
    existence of the other principal's vaulted values, and vice versa. With the
    flag off there
    is no owner filtering (today's behavior). This is access-control, not
    crypto: the single vault DEK still decrypts both principals' records; the
    boundary is this code path refusing to hand one principal another's value.

    Feature-gated on PUFFPAI_SCRUB_AND_VAULT by default (same flag as the write
    side); pass ``require_feature_flag=False`` for an internal authorized caller
    that must resolve regardless. Never raises; unexpected failures return
    RESOLVE_ERROR. Never logs the resolved value.
    """
    # 0) Feature gate (consistent with the write side).
    if require_feature_flag and not scrub_and_vault_enabled():
        return ResolveResult(status=STATUS_DISABLED,
                             detail="scrub-and-vault feature flag is off")

    # 1) Sink discipline FIRST — refuse before touching the vault, so plaintext
    #    is never even fetched for an unauthorized destination.
    if not _sink_is_authorized(sink):
        return ResolveResult(
            status=STATUS_SINK_REFUSED,
            detail="destination is not an authorized non-LLM output sink")

    try:
        ref_id = _extract_ref_id(handle_or_ref_id)
        if not ref_id:
            return ResolveResult(status=STATUS_NOT_FOUND,
                                 detail="not a valid vault handle or ref-id")

        # 2) Resolve the vault.
        if vault is None:
            try:
                from puffpai.api_server import _get_vault
                vault = _get_vault()
            except Exception:
                vault = None
        if vault is None:
            return ResolveResult(status=STATUS_VAULT_LOCKED, ref_id=ref_id,
                                 detail="vault unavailable")

        # 3) The CALLER's session must be unlocked (fail-closed, graceful).
        try:
            unlocked = bool(vault.is_unlocked(session_id))
        except Exception:
            unlocked = False
        if not unlocked:
            return ResolveResult(status=STATUS_VAULT_LOCKED, ref_id=ref_id,
                                 detail="unlock the vault to reveal this value")

        # 4) Fetch the record (encrypted lookup). None => dangling/missing OR,
        #    under multi-user, owned by a different principal (owner filter is
        #    enforced inside get_pii_ref so a cross-principal ref reads as
        #    NOT_FOUND — it never even reveals that the ref exists).
        try:
            from puffpai.security.multiuser import multiuser_enabled
            _enforce_owner = multiuser_enabled()
        except Exception:
            _enforce_owner = False
        try:
            rec = vault.get_pii_ref(ref_id, caller_user_id=caller_user_id,
                                    enforce_owner=_enforce_owner)
        except Exception:
            rec = None
        if not isinstance(rec, dict) or "value" not in rec:
            return ResolveResult(status=STATUS_NOT_FOUND, ref_id=ref_id,
                                 detail="no stored value for this ref-id")

        tier = rec.get("tier") or "critical"   # fail-closed default (missing)
        # Any tier that is not a recognized level is treated as critical, the
        # most restrictive — an unknown/garbage tier must never bypass step-up.
        if tier not in ("medium", "high", "critical"):
            tier = "critical"
        ptype = rec.get("type")
        label = _LABEL.get(ptype, "sensitive data")

        # 5) Tier gate.
        if tier == "critical":
            ok, detail = _verify_step_up(vault, step_up)
            if not ok:
                _audit(vault, "vault_resolve", "step_up_required",
                       f"ref={ref_id[:6]}.. tier=critical")
                return ResolveResult(status=STATUS_STEP_UP_REQUIRED, ref_id=ref_id,
                                     tier=tier, type=ptype, label=label,
                                     detail=detail)

        plaintext = rec.get("value")
        if not isinstance(plaintext, str):
            plaintext = "" if plaintext is None else str(plaintext)

        # 6) Deliver to the authorized sink ONLY.
        try:
            sink.write(plaintext)
        except Exception:
            return ResolveResult(status=STATUS_RESOLVE_ERROR, ref_id=ref_id,
                                 tier=tier, type=ptype, label=label,
                                 detail="sink delivery failed")

        _audit(vault, "vault_resolve", "success",
               f"ref={ref_id[:6]}.. tier={tier} sink={sink.kind}")

        # medium may be returned for display; high/critical go via the sink ONLY.
        returned_value = plaintext if tier == "medium" else None
        return ResolveResult(status=STATUS_RESOLVE_OK, ref_id=ref_id, tier=tier,
                             type=ptype, label=label, delivered=True,
                             value=returned_value)
    except Exception as exc:  # never crash the caller; never log the value
        logger.error("resolve_handle failed: %s", type(exc).__name__)
        return ResolveResult(status=STATUS_RESOLVE_ERROR,
                             detail="unexpected resolver error")


def _audit(vault, action, result, details):
    """Best-effort content-free audit line via the vault's audit log."""
    try:
        vault._audit.log(action=action, user="resolver",
                         result=result, details=details)
    except Exception:
        pass


__all__ = [
    "Sanitized", "scrub_and_vault", "scrub_and_vault_enabled",
    "scrub_and_vault_active", "invalidate_recovery_code_cache",
    "redact_for_log", "redact_for_prompt",
    "STATUS_OK", "STATUS_VAULT_LOCKED", "STATUS_CLASSIFIER_ERROR",
    "STATUS_DISABLED",
    # handle resolver (read side)
    "resolve_handle", "ResolveResult",
    "ResolveSink", "FormFieldSink", "UserRevealSink",
    "LLMContextSink", "PersistenceSink",
    "STATUS_RESOLVE_OK", "STATUS_NOT_FOUND", "STATUS_SINK_REFUSED",
    "STATUS_STEP_UP_REQUIRED", "STATUS_RESOLVE_ERROR",
]
