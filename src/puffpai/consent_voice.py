"""Spoken approval of an open consent request — the deterministic half.

The Zigbee pairing assistant tells the owner: *Say "approve pairing" here, or
tap Approve on the dashboard.* The dashboard half is
``api_server.sentinel_consent_resolve``. As an LLM tool only
(``assistant_tools.sentinel_approve_device``), the spoken half would send the
answer to a question PuffPAI had just asked to a model, which can take close to
a minute, call the wrong tool entirely (``ha_discover_new_devices``), and leave
the pairing window the owner had just approved unopened.

This module is the *matcher*, not an approval surface: it decides whether a
message is an approval or a denial of something that is actually open, and
returns the requests it would apply to. Resolving them and running the
completion hooks stays in ``api_server`` — which, with ``assistant_tools``, is one
of exactly two files allowed to call
:func:`puffpai.zigbee_pairing.complete_approved_pairing` and
:func:`puffpai.ha_provisioning.apply_approved_change` (pinned by
``tests/test_zigbee_pairing.py``). Keeping the opener out of here means the new
spoken path adds a *phrase rule*, not a third way into the radio.

Two rules carry the safety of the whole thing:

* **Nothing open, nothing claimed.** :func:`decide` reads the consent queue
  (``puffpai.security.consent_requests`` — the one store, never a parallel
  list) and returns ``(None, [])`` when no eligible request is pending, so an
  unrelated "yes" falls through to whatever would have answered it before.
* **A bare affirmation is only an answer to a recent question.** "yes" / "go
  ahead" / "do it" count only within :data:`BARE_WINDOW_SECONDS` of the request
  being raised; an explicit "approve the pairing" gets
  :data:`EXPLICIT_WINDOW_SECONDS`.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: The actions a spoken yes may complete. Both have a completion hook that
#: re-reads the durable record and refuses without it, so approving one here is
#: an answer to a question PuffPAI asked — never an act of its own. Device
#: consent requests (``control_device`` and friends) are deliberately absent:
#: those are a standing permission on a device, not a window that is about to
#: close, and they are answered in the UI where the device can be seen.
APPROVABLE_ACTIONS = frozenset({"zigbee_pair", "ha_provision"})

#: An explicit "approve the pairing" names its errand, so it may reach back a
#: little further than a bare "yes".
EXPLICIT_WINDOW_SECONDS = 600.0

#: A bare affirmation is only an ANSWER while the question is fresh.
BARE_WINDOW_SECONDS = 300.0

_PUNCT_RE = re.compile(r"[.!?,;:\s]+$")
_SPACE_RE = re.compile(r"\s+")

#: Softeners that may precede either verdict ("yes, approve it", "ok go ahead
#: and approve that"). Bounded alternation, never ``.*``.
_PREFIX = r"(?:(?:yes|yeah|yep|ok|okay|sure|alright)[, ]+)?(?:(?:please|go ahead and|just)\s+)*"

#: What the verdict may be ABOUT. The object is optional — "approve" on its own
#: is unambiguous when something is open, and nothing is claimed when nothing
#: is (see :func:`decide`).
_OBJECT = (r"(?:\s+(?:it|that|this|them|pairing|the\s+pairing|the\s+pair|"
           r"the\s+request|the\s+requests|the\s+change|the\s+changes))?")

#: Past tense included on purpose: Whisper transcribed the owner's "approve
#: pairing" as "Approved pairing." — the defect that started this module.
_APPROVE_RE = re.compile(
    rf"^{_PREFIX}approved?{_OBJECT}$"
    rf"|^{_PREFIX}(?:the\s+)?pairing\s+(?:is\s+)?approved?$"
    rf"|^{_PREFIX}i\s+approve{_OBJECT}$")

_DENY_RE = re.compile(
    rf"^{_PREFIX}(?:deny|denied|reject|rejected|decline|declined|cancel|"
    rf"don'?t\s+approve|do\s+not\s+approve){_OBJECT}$"
    rf"|^{_PREFIX}(?:the\s+)?pairing\s+(?:is\s+)?(?:denied|rejected|cancelled|"
    rf"canceled)$"
    rf"|^{_PREFIX}no,?\s+(?:don'?t|do\s+not)\s+(?:approve|pair)"
    rf"(?:\s+(?:it|that|this|them|anything))?$")

#: The whole vocabulary of a bare yes. Short and closed: every phrase here is
#: claimed WITHOUT the word "approve" appearing anywhere, so widening it widens
#: what a stray "sure" can consent to.
_BARE_APPROVE_RE = re.compile(
    r"^(?:yes|yes\s+please|yeah|yep|yup|sure|go\s+ahead|go\s+for\s+it|"
    r"do\s+it|please\s+do)$")

_BARE_DENY_RE = re.compile(
    r"^(?:no|no\s+thanks|no\s+thank\s+you|nope|not\s+now|never\s+mind|"
    r"nevermind|forget\s+it|stop)$")


def normalise(text: str) -> str:
    """Lower-case, whitespace-collapsed, trailing punctuation stripped."""
    lowered = _SPACE_RE.sub(" ", str(text or "").lower()).strip()
    return _PUNCT_RE.sub("", lowered).strip()


def classify(text: str) -> tuple[str | None, bool]:
    """``(verdict, explicit)`` for *text* alone — no store, no I/O.

    *verdict* is ``"approve"``, ``"deny"`` or ``None``; *explicit* says whether
    the words themselves named the errand (which is what buys the longer
    window in :func:`decide`).
    """
    phrase = normalise(text)
    if not phrase:
        return None, False
    if _APPROVE_RE.match(phrase):
        return "approve", True
    if _DENY_RE.match(phrase):
        return "deny", True
    if _BARE_APPROVE_RE.match(phrase):
        return "approve", False
    if _BARE_DENY_RE.match(phrase):
        return "deny", False
    return None, False


def _age_seconds(req: dict, now: datetime) -> float | None:
    stamp = req.get("created_at")
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds()


def eligible_requests(within_seconds: float,
                      now: datetime | None = None) -> list[dict]:
    """Open ``zigbee_pair`` / ``ha_provision`` requests, oldest first.

    Reads the consent queue itself. A request whose ``created_at`` cannot be
    parsed is dropped rather than assumed fresh — an undatable question is not
    one the owner can be answering right now.
    """
    from puffpai.security import consent_requests as _creq

    now = now or datetime.now(timezone.utc)
    dated: list[tuple[float, dict]] = []
    try:
        open_requests = _creq.list_open()
    except Exception as exc:                        # never break the turn
        logger.debug("consent voice: could not read the queue: %s", exc)
        return []
    for req in open_requests:
        if not isinstance(req, dict):
            continue
        if req.get("action") not in APPROVABLE_ACTIONS:
            continue
        age = _age_seconds(req, now)
        if age is None or age > within_seconds or age < -60.0:
            continue
        dated.append((age, req))
    # Oldest first: the question that has been waiting longest is the one the
    # owner has been looking at, and a pairing window is the shorter fuse.
    return [req for _age, req in sorted(dated, key=lambda pair: -pair[0])]


def decide(text: str) -> tuple[str | None, list[dict]]:
    """``(verdict, requests)`` — what this message answers, if anything.

    Returns ``(None, [])`` whenever the words do not match, or nothing eligible
    is open. The caller must then leave the turn exactly as it found it.
    """
    verdict, explicit = classify(text)
    if verdict is None:
        return None, []
    window = EXPLICIT_WINDOW_SECONDS if explicit else BARE_WINDOW_SECONDS
    requests = eligible_requests(window)
    if not requests:
        return None, []
    return verdict, requests


#: The two instant answers for an EXPLICIT verdict with nothing open.
NOTHING_OPEN_APPROVE = "Nothing is waiting for your approval right now."
NOTHING_OPEN_DENY = "There's nothing open to decline."


def _anything_open() -> bool:
    """Is ANY consent request open at all? Unknown counts as yes.

    Wider than :data:`APPROVABLE_ACTIONS` and blind to the freshness windows,
    both on purpose. An hour-old pairing request is too old for a spoken
    "approve" to *resolve*, and a ``control_device`` request is answered in the
    UI rather than out loud — but each is still something waiting, and
    "nothing is waiting for your approval" would be a false sentence. The
    fastpath only speaks when the queue is genuinely empty.
    """
    from puffpai.security import consent_requests as _creq

    try:
        return any(isinstance(req, dict) for req in _creq.list_open())
    except Exception as exc:
        logger.debug("consent voice: could not read the queue: %s", exc)
        return True                 # never claim "nothing" on a failed read


def nothing_open_reply(text: str) -> str | None:
    """The instant decline for an explicit approve/deny with nothing open.

    Sent to a model, "approve pairing" with an empty queue can take the best
    part of a minute while the model shops through device-approval tools
    before settling on the right answer, and the same non-deterministic tool
    selection can open a pairing window nobody asked for. A question
    with a one-line answer ("is anything open? no") does not belong to a model.

    Only an EXPLICIT phrase is answered here. A bare "yes" or "no" with
    nothing open is not about approval at all — it is an answer to whatever
    was really being discussed — so it keeps falling through untouched.

    Returns None (leaving the turn exactly as it found it, no side effects and
    no store write) for anything else.
    """
    verdict, explicit = classify(text)
    if verdict is None or not explicit:
        return None
    if _anything_open():
        return None
    return NOTHING_OPEN_APPROVE if verdict == "approve" else NOTHING_OPEN_DENY
