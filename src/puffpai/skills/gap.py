"""The honest capability gap: "I don't have a way to do X yet."

This is the cheap half of the self-extension work and it ships WITHOUT the
expensive half on purpose. It does not offer to build anything -- Phase 1 has
no builder -- and it never claims a new ability. It replaces one sentence with
a better one, in exactly one situation: the agent route already declined, and
the request was action-shaped, and nothing in the registry claims it.

**Decline-only, and behind the agent route.** It is not a fastpath. It never
sees a turn the appliance answered. That ordering is the reason it cannot
"steal" anything: by the time it runs, every fastpath, the registry read, the
team and the legacy agent have all had the turn and produced an "I can't".

**The gate is one arm, not two.** The design's second arm -- "can you / tell me
/ check / get me" -- is deliberately absent. Measured against the decline corpus
in ``tests/test_capability_gap.py``, that arm is what turns "tell me I'm doing
okay", "check on the baby" and "can you keep a secret?" into a robotic
non-sequitur about capabilities. Comfort, curiosity and small talk are not
feature requests. What is left is
:func:`puffpai.router._looks_like_action_request`, which wants an action VERB
and an action OBJECT.

**Why the gate is not "``capability_registry.find()`` returns None".**
Measured, ``find`` is an eager
ranking function -- it names a best guess for almost every household sentence,
including "make a list of nearby earthquakes" (``pantry.shopping``) -- so using
it as the gate makes this detector fire never. The registry itself draws this
distinction: ``find`` ranks, ``find_for_dispatch`` decides. So the gate here is
the DECIDING pair (``find_for_dispatch`` and ``mentions_dispatch_subject``,
both None) plus the tool-binding condition, which is the one that actually
protects honesty: if any capability's keywords bind a real tool for this query,
the appliance may well have a way and this sentence would be a lie.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MAX_LEN = 200

#: Mirrors ``api_server._REFUSAL_RE`` in intent (kept local so this module does
#: not import the API server, which imports this one). Only used to decide
#: whether the reply we are about to REPLACE was already a refusal -- never to
#: decide what the appliance can do.
DECLINE_RE = re.compile(
    r"(?i)\b(?:can(?:'|no)?t\b|cannot\b|unable to\b|not able to\b|"
    r"no (?:such )?(?:function|tool|way)\b|"
    r"none of the\b[^.!?]{0,50}\b(?:functions?|tools?)\b|"
    r"none of the (?:provided|available)\b|"
    r"not equipped\b|"
    r"n't have (?:a|the|any)\b|not have (?:a|the|any)\b|"
    r"i do not have\b|i don'?t have\b|"
    r"not (?:currently )?(?:possible|supported|available)\b)")

#: Openers that wrap a request without changing it. Trimmed off the front of the
#: sentence the reply quotes back, so the answer reads "I don't have a way to
#: make a list of X yet" rather than "...to please make a list of X yet".
_LEAD_RE = re.compile(
    r"^(?:hey\s+puff(?:pai)?|puff(?:pai)?|ok(?:ay)?|hi|hello)\b[\s,]*"
    r"|^(?:please|can\s+you|could\s+you|would\s+you|will\s+you|"
    r"i\s+want\s+you\s+to|i\s+need\s+you\s+to|go\s+ahead\s+and)\b[\s,]*",
    re.IGNORECASE)


def looks_like_decline(reply) -> bool:
    """True when a reply already said no. Cheap, and deliberately generous."""
    return bool(isinstance(reply, str) and reply.strip()
                and DECLINE_RE.search(reply))


def _always_tools() -> set:
    from puffpai import capability_registry as registry
    names: set = set()
    for cap in registry.all_capabilities():
        if cap.always:
            names.update(cap.all_tools)
    return names


def is_gap(text: str) -> bool:
    """The three conditions, in cheapest-first order. Never raises.

    Fails CLOSED (returns False) on any error: an appliance that cannot consult
    its own registry has no business telling the household what it cannot do.
    """
    said = (text or "").strip()
    if not said or len(said) > MAX_LEN:
        return False
    try:
        from puffpai import capability_registry as registry
        from puffpai import router

        if not router._looks_like_action_request(said.lower()):
            return False
        if registry.find_for_dispatch(said) is not None:
            return False
        if registry.mentions_dispatch_subject(said) is not None:
            return False
        always = _always_tools()
        bound = [name for name in registry.tools_for_query(said)
                 if name not in always]
        return not bound
    except Exception as exc:
        logger.debug("skills: gap check unavailable (%s)", exc)
        return False


def _quoted(text: str) -> str:
    said = " ".join(str(text or "").strip().split())
    said = _LEAD_RE.sub("", said).strip()
    return said.rstrip(" ?.!,")


def sentence(text: str) -> str:
    """The honest sentence. No offer -- Phase 1 cannot build anything."""
    return f"I don't have a way to {_quoted(text)} yet."


def detect(text: str, reply: str | None = None) -> str | None:
    """The replacement sentence for a declined turn, or None to leave it alone.

    *reply* is the answer the agent route produced. Passing it is the normal
    case: when it is given and it does NOT look like a refusal, this returns
    None, because a turn the appliance answered is not a gap. Passing None asks
    the shape question alone, which is what the tests do.
    """
    if reply is not None and not looks_like_decline(reply):
        return None
    if not is_gap(text):
        return None
    return sentence(text)


__all__ = ["DECLINE_RE", "MAX_LEN", "detect", "is_gap", "looks_like_decline",
           "sentence"]
