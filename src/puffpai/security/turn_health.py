"""The health half of a scrub-and-vault turn: scrub PERSISTENCE, not ROUTING.

WHY THIS EXISTS
---------------
With ``PUFFPAI_SCRUB_AND_VAULT=1`` (as a hardened appliance sets it in
``/etc/puffpai/api.env``), handing the *scrubbed* text to routing breaks the
medication turn:

    'add insulin to my meds'  ->  'add [REDACTED-HEALTH] to my [REDACTED-HEALTH]'

The deterministic medication fastpath would never see a medication, nothing
would be written to ``calendar.meds[]``, and the turn would still carry the
notice *"I spotted a health detail and did not keep it — it was removed from
this conversation and not saved anywhere."* That notice would be **false on the
reminder phrasing**, a calendar task titled ``take my blood pressure pill at
8`` has just been written, and the medication phrasing would keep nothing at
all while the model's reply said it had.

The calendar KEEPS a medication list, and hiding one is a per-med opt-in. So
the problem is one of ORDER, not of scrubbing.

WHAT THIS DOES, PRECISELY
-------------------------
Route and store on the text as spoken; scrub the copy that is PERSISTED to
conversation memory. The at-rest gate holds: the transcript the appliance
keeps carries ``[REDACTED-HEALTH]`` in place of every disclosed health term.
What does not happen is the deterministic router being handed a redacted
sentence it cannot act on.

The VAULT is not involved. ``pii_quality_gate.is_vault_worthy`` does not
recognise ``health.condition``, so a health mention is always
SCRUBBED-NOT-VAULTED — there is no handle to mint, lose, or re-mint here. The
seven format detectors (SSN, card, DOB, address, email, phone, name) apply to
both texts: those values are scrubbed and vaulted before either the LLM or the
transcript sees them.

THE SECOND JOB: "was it actually kept?"
---------------------------------------
``turn_writes`` answers "which data CLASSES did this turn store", which is enough
for ``calendar.meds[]`` (tagged ``health.condition``) but not for
``calendar.tasks[]`` — a reminder task is not a tagged health position, yet its
title can carry the very words the notice claims were not kept. So this module
keeps a second, narrower record: **did any document written during this turn
contain this turn's health terms?**

The terms live in a thread-local for the life of the turn and NOTHING exposes
them — ``health_was_stored()`` returns a bool. That keeps ``turn_writes``' own
"classes and schema paths, never a value" contract intact while letting the
honesty notice answer the only question that makes it true or false.

Rounding, stated plainly and deliberately one-way: ``_save`` is handed a finished
document, not a delta, so a term that was ALREADY in the document counts as
"kept". The worst that does is keep PuffPAI **silent** about something it really
did discard. It can never make PuffPAI claim a keep that did not happen.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import re
import threading

_state = threading.local()

#: A document larger than this is not scanned for the turn's health terms.
#: App documents are small (the largest shipped one is a few tens of KB); a
#: pathological one must not put a regex walk on the request path. Fail-safe
#: direction: an unscanned write does not mark the term as kept, so the notice
#: is KEPT — over-honest rather than under-honest.
_MAX_SCAN_CHARS = 512 * 1024


def begin_turn() -> None:
    """Open (or reopen) this thread's health record. Call once per served turn,
    alongside ``turn_writes.begin_turn()``, BEFORE any routing runs."""
    _state.active = True
    _state.pattern = None
    _state.kept = False


def end_turn() -> None:
    """Close this thread's health record. Used by the request path's siblings and
    by tests, so a turn opened in one place cannot silence a notice elsewhere."""
    _state.active = False
    _state.pattern = None
    _state.kept = False


def turn_active() -> bool:
    """True while a served turn is recording on this thread."""
    return bool(getattr(_state, "active", False))


def arm(terms) -> None:
    """Record the health terms detected on this turn, for the kept-check only.

    Word-bounded and case-insensitive, matching how ``DataClassifier.redact_pii``
    removes them, so "Insulin" at the start of a stored sentence still counts and
    "meds" does not match inside "medsafe". No accessor returns these terms.
    """
    if not turn_active():
        return
    try:
        cleaned = [str(t) for t in (terms or []) if str(t).strip()]
        if not cleaned:
            _state.pattern = None
            return
        _state.pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(t) for t in cleaned) + r")\b",
            re.IGNORECASE)
    except Exception:
        # An unarmable record must not silence the notice: leaving pattern None
        # means health_was_stored() stays False and the notice is kept.
        _state.pattern = None


def note_write(app, document) -> None:
    """Record whether an app-data write carried this turn's health terms.

    A no-op outside a turn and a no-op when the turn has no health terms, so the
    cost is zero on every turn that is not a flag-ON health turn. Never raises:
    this is an honesty aid, and a bug in it must never be the reason a household
    loses a calendar entry.
    """
    if not turn_active() or getattr(_state, "kept", False):
        return
    pattern = getattr(_state, "pattern", None)
    if pattern is None:
        return
    try:
        blob = json.dumps(document, default=str, ensure_ascii=False)
        if len(blob) > _MAX_SCAN_CHARS:
            return
        if pattern.search(blob):
            _state.kept = True
    except Exception:
        return


def health_was_stored() -> bool:
    """True when a document written during this turn holds this turn's health
    terms. False outside a turn, and False when nothing was detected."""
    return bool(getattr(_state, "kept", False))


def redact_turn_terms(text):
    """Replace THIS turn's health terms in ``text`` with ``[REDACTED-HEALTH]``.

    The free-text detector needs a disclosure cue ("my insulin", "add X to my
    meds") before it will fire. An assistant reply does not have one: the
    medication fastpath answers
    ``"Medication added: insulin (1 dose), daily for all."`` and the detector
    sees no cue, so without this the drug name would go into conversation
    memory in the clear on every surface the fastpath serves, a consequence of
    routing on the unscrubbed text, which lets the reply carry the word at all.

    The terms this turn already disclosed are known here, so the persisted copy
    is cleaned against them directly. Strictly additive: it only ever removes
    MORE than the classifier did, never less, and it returns redacted TEXT — no
    accessor exposes the terms themselves.

    A no-op outside a turn and when nothing was detected. Never raises.
    """
    if not text or not turn_active():
        return text
    pattern = getattr(_state, "pattern", None)
    if pattern is None:
        return text
    try:
        return pattern.sub("[REDACTED-HEALTH]", str(text))
    except Exception:
        return text


def capture_redactor():
    """Bind THIS turn's health redaction to a callable that outlives the turn.

    ``/chat/stream``'s real streaming path persists the assistant reply from
    inside the SSE generator, which Flask consumes after the view has returned —
    after ``turn_scope``'s ``teardown_request`` has closed this record, and (as
    the sibling ``_stream_notice`` comment in ``api_server`` already notes) not
    necessarily on the request thread at all. A generator that called
    :func:`redact_turn_terms` would therefore find an empty record and store the
    reply UNREDACTED: less scrubbing than today, which is the one direction this
    work is not allowed to move.

    So the caller takes this closure on the request thread, while the record is
    still open, and hands it to the persistence scrub. The compiled pattern is
    held only inside the closure; the callable maps text to redacted text and
    nothing more, so the module's "no accessor returns the terms" contract is
    unchanged.

    Outside a turn, or with nothing detected, the returned callable is identity —
    the same no-op :func:`redact_turn_terms` would have been.
    """
    pattern = getattr(_state, "pattern", None)
    if not turn_active() or pattern is None:
        return lambda text: text

    def _redact(text):
        if not text:
            return text
        try:
            return pattern.sub("[REDACTED-HEALTH]", str(text))
        except Exception:
            return text

    return _redact


#: A stored medication name shorter than this is not redacted out of a reply.
#: One- and two-character "names" are far more likely to be a typo than a drug,
#: and word-bounded substitution of a two-letter token would shred ordinary
#: sentences. The floor is the same one ``_extract_med_name`` enforces on entry.
_MIN_STORED_NAME_CHARS = 3


def redact_known_medications(text, names):
    """Replace any of ``names`` in ``text`` with ``[REDACTED-HEALTH]``.

    :func:`redact_turn_terms` only knows the terms THIS turn disclosed, which
    is enough for "add insulin to my meds" and nothing else. "what meds do I
    have" discloses no drug, so the medication-list fastpath's reply names a
    drug read out of ``calendar.json``, and without this it would go into
    conversation memory in the clear on four surfaces - the same leak, closed
    for the turn where the user says the drug and open for the turn where the
    appliance reads it back.

    Screening against the STORED names closes it generally: any reply that
    echoes a medication the household has on file is cleaned before it is
    persisted, whichever fastpath, tool or model produced it.

    Never raises, and never returns a partially-substituted string on error.
    """
    if not text or not names:
        return text
    try:
        wanted = sorted({str(n).strip() for n in names
                         if n and len(str(n).strip()) >= _MIN_STORED_NAME_CHARS},
                        key=len, reverse=True)
        if not wanted:
            return text
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(n) for n in wanted) + r")\b",
            re.IGNORECASE)
        return pattern.sub("[REDACTED-HEALTH]", str(text))
    except Exception:
        return text


def split_for_persistence(clean_text, *, classifier=None):
    """Return ``(persist_text, health_terms_found)`` for an already-scrubbed turn.

    ``clean_text`` is what ``scrub_and_vault`` produced with the seven shipped
    detectors — vault handles minted, raw SSN/card/DOB/address/email/phone/name
    already gone. This applies the free-text ``health.condition`` pass to the
    copy that will be PERSISTED, and arms the kept-check above with what it
    found. The caller keeps ``clean_text`` for routing and the LLM.

    Fail-safe: on any error the ORIGINAL clean_text is returned as the persisted
    copy together with an empty term list. That is the pre-health flag-ON
    behaviour (health words persisted, no notice claimed about them) — it never
    emits a value the seven detectors had scrubbed, and it never produces a
    "did not keep it" sentence this module cannot stand behind.
    """
    text = clean_text if isinstance(clean_text, str) else str(clean_text or "")
    try:
        if classifier is None:
            from puffpai.security.data_classifier import DataClassifier
            classifier = DataClassifier()
        redacted, detection = classifier.redact_pii(text, include_health=True)
        terms = list(getattr(detection, "health_conditions", None) or [])
    except Exception:
        return text, []
    arm(terms)
    return redacted, terms


__all__ = [
    "begin_turn",
    "end_turn",
    "turn_active",
    "arm",
    "note_write",
    "health_was_stored",
    "redact_turn_terms",
    "capture_redactor",
    "redact_known_medications",
    "split_for_persistence",
]
