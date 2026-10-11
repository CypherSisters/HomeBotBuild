"""Ordered execution of a sequenced ("do X, then Y") chat turn.

Letting the instant-answer layer STAND DOWN on a sequenced turn and the
planner finish it handles the read + read case ("check whats in the pantry,
then tell me the network status too" answers both halves), but not the
ACTION + read case. Handed to the planner,

    turn on the porch light, then tell me the weather

with the light ``off`` beforehand, can come back as:

    "Porch Light has been turned on." + the weather card,
        while the entity's ``last_changed`` does not move at all
    "Done - light.porch turn on."  (weather dropped)

The first is the serious one: the planner's PROSE claims a device state change
that never happened. A house that says it turned a light on when it did not is
worse than a house that says nothing.

So an action-bearing sequence is not handed to the planner at all. It is split
into ordered clauses, each clause runs through its ordinary path, and an action
clause is worded **from the entity state read back after the call** -- never
from a model's account of what it intended to do.

This module is the pure half: splitting and wording. It has no Flask, no HTTP
and no Home Assistant import, so every rule below is testable on its own. The
execution half is ``api_server._try_compound_request``.
"""

from __future__ import annotations

import re

#: A turn that sequences a second request after the first. Kept byte-identical
#: in meaning to ``api_server._COMPOUND_REQUEST_RE`` -- an explicit sequencing
#: marker only. A bare "and" joins one action to one object far more often than
#: it joins two requests ("turn off the kitchen and hallway lights").
#: A second REQUEST introduced by "and" instead of "then".
#: "add oat milk to the shopping list and tell me how many items are on the
#: list" must not add the milk and drop the question without a word, when the
#: same turn said as "..., then tell me ..." answers both halves. A bare "and"
#: still joins one action to one object far more often than it joins two
#: requests ("salad and potato bar", "living room and bedroom"), so an "and"
#: only ends a clause when what FOLLOWS it opens a new request: a read-back ask
#: ("tell me", "read me", "show me", "give me", "let me know"), or an explicit
#: "and also" that is not pointing at another object. The marker itself is
#: zero-width after the connector, so the opener stays in the second clause.
_AND_READBACK = (r"(?:\s*,\s*|\s+)and\s+(?:then\s+|also\s+)?"
                 r"(?=(?:tell|read|show|give|list)\s+(?:me|us)\b"
                 r"|let\s+(?:me|us)\s+know\b)")
#: "and also <verb>" sequences; "and also THE fan" is still one object list.
_AND_ALSO = (r"(?:\s*,\s*|\s+)and\s+also\s+"
             r"(?!(?:the|a|an|my|our|your|his|her|their|its|this|that|these|"
             r"those|some|both|it|one)\b)(?=\w)")

SEQUENCE_RE = re.compile(
    r"(?i)(?:,\s*|\s)(?:and\s+)?then\s+\w"
    r"|\bafter\s+that\b"
    r"|\band\s+(?:also\s+)?after\s+(?:that|this)\b"
    r"|" + _AND_READBACK + r"|" + _AND_ALSO)

#: Ordinary English uses of "then" that sequence nothing.
FALSE_FRIEND_RE = re.compile(
    r"(?i)\b(?:back|even|since|by|until|right|way)\s+then\b"
    r"|\bnow\s+and\s+then\b|\bevery\s+now\s+and\s+then\b"
    r"|\bif\b[^.?!]{0,60}\bthen\b|\bthen\s+again\b")

#: Where a clause ENDS. Broader than :data:`SEQUENCE_RE` on purpose: this only
#: runs once the turn has already been recognised as sequenced.
_SPLIT_RE = re.compile(
    r"(?i)(?:\s*,\s*|\s+)(?:and\s+)?then\s+"
    r"|(?:\s*,\s*|\s+)(?:and\s+)?(?:also\s+)?after\s+(?:that|this)\s*,?\s*"
    r"|" + _AND_READBACK + r"|" + _AND_ALSO)

#: Leading filler a split leaves behind ("..., then also tell me ...").
_LEAD_RE = re.compile(r"(?i)^(?:and|also|next|too|please)\b[\s,]*")

#: More than this and the turn is not a household sequence, it is an essay.
#: Typical sequences are 2 clauses, and 3 must work.
MAX_CLAUSES = 4

#: The state a control action is supposed to leave the entity in.
EXPECTED_STATE = {
    "turn_on": "on",
    "turn_off": "off",
    "open": "open",
    "close": "closed",
    "lock": "locked",
    "unlock": "unlocked",
}


def is_compound(text: str) -> bool:
    """True when the turn sequences a second request after the first."""
    body = text or ""
    if not body.strip():
        return False
    if FALSE_FRIEND_RE.search(body):
        return False
    return bool(SEQUENCE_RE.search(body))


def split_clauses(text: str) -> list:
    """Split a sequenced turn into its ordered clauses.

    Returns ``[]`` when the text is not sequenced, or when splitting produces
    fewer than two usable clauses (a trailing "then" with nothing after it).
    The caller treats ``[]`` as "leave this turn alone".
    """
    body = (text or "").strip()
    if not is_compound(body):
        return []
    clauses = []
    for raw in _SPLIT_RE.split(body):
        piece = _LEAD_RE.sub("", (raw or "").strip()).strip(" ,;")
        if piece:
            clauses.append(piece)
    if len(clauses) < 2 or len(clauses) > MAX_CLAUSES:
        return []
    return clauses


def observed_action_reply(name: str, action: str, observed) -> str:
    """Word an action clause's reply FROM THE OBSERVED STATE.

    *observed* is the entity's state read back after the service call, or
    ``None`` when it could not be read. Nothing here is allowed to assert a
    state change that the read did not show.
    """
    who = (name or "that device").strip() or "that device"
    verb = str(action or "update").replace("_", " ")
    if observed is None:
        return (f"I sent the {verb} command to {who}, but I couldn't read its "
                f"state back, so I can't confirm it took effect.")
    seen = str(observed).strip()
    expected = EXPECTED_STATE.get(str(action or ""))
    if expected is None:
        # toggle / dim / anything without a single settled end state: report
        # what the house actually reads now, and claim nothing beyond it.
        return f"{who} now reads {seen}."
    if seen.lower() == expected:
        return f"{who} is now {expected}."
    if seen.lower() in ("unavailable", "unknown"):
        return (f"I sent the {verb} command, but {who} is reporting "
                f"'{seen}' — I can't confirm it took effect.")
    return f"I sent the {verb} command, but {who} still reads {seen}."


def join_clause_replies(replies) -> str:
    """Concatenate clause results in the order they were asked for."""
    parts = [str(r).strip() for r in (replies or []) if str(r or "").strip()]
    return "\n\n".join(parts)


__all__ = [
    "EXPECTED_STATE",
    "FALSE_FRIEND_RE",
    "MAX_CLAUSES",
    "SEQUENCE_RE",
    "is_compound",
    "join_clause_replies",
    "observed_action_reply",
    "split_clauses",
]
