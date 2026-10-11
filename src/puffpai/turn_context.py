"""What the LAST turn actually did, so this turn can answer questions about it.

A turn answered with no memory of the turn before it either runs a NEW
search for a question about the previous answer, or describes the appliance
from a template instead of reading what it just did:

* "Where did you find that information?" would run a fresh library search on
  the words of the question and cite Wiktionary entries for "find".
* "what did you just do / did you change something" would answer "I didn't
  change anything ... I can't directly control devices or run scripts", two
  minutes after setting a thermostat and adding to the shopping list.

This module is the small, honest memory those answers need. It holds only what
the appliance itself produced: the citation list a reader returned, the kind of
path that answered, and the confirmation sentences the WRITE fastpaths spoke.
It never holds the user's words — a source question and a "what did you do"
question are both answered from the appliance's own record, so nothing here
needs the prompt.

In-process and bounded (a ring buffer plus one slot per conversation, both
TTL'd). It is a conversational convenience, not a ledger: the durable record is
``agent-activity.jsonl``, owned by :mod:`puffpai.agent_activity`.
"""

from __future__ import annotations

import re
import threading
import time

#: How long the previous turn stays citable. Long enough for the owner to hear
#: an answer and ask where it came from; short enough that "where did you find
#: that" an hour later is honestly "I don't have a previous answer to cite".
ANSWER_TTL = 600.0

#: How far back "what did you just do" may look, and how long a confirmation
#: sentence is kept at all.
ACTION_TTL = 1800.0
ACTION_WINDOW = 600.0

#: Hard cap on the ring buffer. A household turn is a few hundred bytes.
MAX_ACTIONS = 60

_LOCK = threading.Lock()
_LOCAL = threading.local()

#: conversation_id (or "") -> {"ts", "sources", "kind", "detail"}
_ANSWERS: dict[str, dict] = {}

#: [{"ts", "conversation", "text"}] — newest last.
_ACTIONS: list[dict] = []

#: How long the room the household last talked about stays borrowable by a
#: command that names none. Shorter than ANSWER_TTL: "set it to 74" means the
#: room from a minute ago, not the room from ten.
ROOM_TTL = 300.0

#: conversation_id (or "") -> {"ts", "room"}
_ROOMS: dict[str, dict] = {}


def reset() -> None:
    """Drop every record. Tests only."""
    with _LOCK:
        _ANSWERS.clear()
        _ACTIONS.clear()
        _ROOMS.clear()
    _LOCAL.conversation = None


# ── turn scope ──────────────────────────────────────────────────────

def begin(conversation_id=None) -> None:
    """Open a turn: everything recorded until the next ``begin`` is this one's.

    Thread-local, like :mod:`puffpai.chat_activity`'s turn scope, and set from
    the same two places (the chat and voice pipelines).
    """
    _LOCAL.conversation = str(conversation_id or "")


def current_conversation() -> str:
    return getattr(_LOCAL, "conversation", None) or ""


# ── what answered, and with what citations ──────────────────────────

def note_answer(kind: str, detail=None, sources=None,
                conversation_id=None) -> None:
    """Record how this turn was answered.

    *kind* is a fixed word from the caller's own vocabulary ("rag", "fastpath",
    "briefing", ...), *detail* a short label for it (a fastpath name, a
    capability id) and *sources* the citation records a reader returned.
    """
    key = str(conversation_id) if conversation_id is not None \
        else current_conversation()
    record = {"ts": time.time(), "kind": str(kind or ""),
              "detail": str(detail or "") or None,
              "sources": list(sources or [])}
    with _LOCK:
        _ANSWERS[key] = record


def note_sources(sources, conversation_id=None) -> None:
    """Attach citations to the turn in progress without changing its kind.

    Called by the readers themselves (``_try_knowledge_search``), which run
    before the caller knows what the turn's route will be.
    """
    key = str(conversation_id) if conversation_id is not None \
        else current_conversation()
    with _LOCK:
        record = _ANSWERS.get(key)
        if record is None or time.time() - record.get("ts", 0) > ANSWER_TTL:
            record = {"ts": time.time(), "kind": "", "detail": None,
                      "sources": []}
            _ANSWERS[key] = record
        record["ts"] = time.time()
        record["sources"] = list(sources or [])


def last_answer(conversation_id=None):
    """The previous turn's record for this conversation, or None."""
    key = str(conversation_id) if conversation_id is not None \
        else current_conversation()
    with _LOCK:
        record = _ANSWERS.get(key)
        if record is None:
            return None
        if time.time() - record.get("ts", 0) > ANSWER_TTL:
            _ANSWERS.pop(key, None)
            return None
        return dict(record)


# ── the room this conversation is talking about ─────────────────

def note_room(room, conversation_id=None) -> None:
    """Record the room a READ just answered about.

    After "what's the temperature in the living room", "set the thermostat to
    74" in the same conversation should not ask "Which thermostat do you
    mean?". The room cannot be read back out of ``conversation_history``:
    ``/api/chat`` builds that history from the messages in the REQUEST body,
    and the spoken surface sends one message per turn, so the room would be
    gone by the next turn even though the conversation id is the same. It is
    kept here instead, beside the rest of what the last turn did, where the
    conversation id is the key.

    Only the room WORD is kept — no entity, no reading, no prompt text.
    """
    room = " ".join(str(room or "").split()).lower()
    if not room:
        return
    key = str(conversation_id) if conversation_id is not None \
        else current_conversation()
    with _LOCK:
        _ROOMS[key] = {"ts": time.time(), "room": room}


def last_room(conversation_id=None):
    """The room this conversation last named, or None once it has gone cold."""
    key = str(conversation_id) if conversation_id is not None \
        else current_conversation()
    with _LOCK:
        record = _ROOMS.get(key)
        if record is None:
            return None
        if time.time() - record.get("ts", 0) > ROOM_TTL:
            _ROOMS.pop(key, None)
            return None
        return record.get("room") or None


# ── what the appliance DID ──────────────────────────────────────────

#: A clarifying question, not an account of anything done: "what did you just
#: do" must not recite "Which thermostat do you mean: Upstairs, Living Room?"
#: as an action. A write fastpath answers with
#: a question or a decline often enough that its prose alone cannot be trusted
#: to mean "I did this".
_ASK_OPENER_RE = re.compile(
    r"(?i)^(?:which|whose|what|who|where|when|how\s+many|did\s+you\s+mean|"
    r"do\s+you\s+(?:want|mean)|would\s+you|should\s+i|shall\s+i|"
    r"there(?:'s|\s+is)\s+more\s+than\s+one|say\s+)")

_DECLINE_OPENER_RE = re.compile(
    r"(?i)^(?:i\s+(?:couldn't|could\s+not|can't|cannot|didn't|did\s+not|"
    r"won't|will\s+not|don't|do\s+not|need)|i'd\s+rather\s+not|"
    r"i'm\s+not\s+|nothing\s+(?:was|changed)|that\s+didn't)")

#: A true statement about state that changed nothing ("X wasn't on the list").
_READ_ONLY_RE = re.compile(
    r"(?i)(?:wasn't|was\s+not|isn't|is\s+not)\s+(?:on|in)\s+the\b"
    r"|^(?:you\s+have|the\s+\w+\s+is)\b")


def outcome_kind(sentence) -> str:
    """``write`` | ``read`` | ``ask`` for a fastpath's own reply. Pure.

    Only the first sentence decides: "Living Room set to 74. … Want me to try
    again?" reports a real write and merely ends with an offer.
    """
    text = " ".join(str(sentence or "").split())
    if not text:
        return "ask"
    head = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if head.endswith("?"):
        return "ask"
    if _ASK_OPENER_RE.match(head) or _DECLINE_OPENER_RE.match(head):
        return "ask"
    if _READ_ONLY_RE.search(head):
        return "read"
    return "write"


#: The kinds "what did you just do" is allowed to recite.
DID_KINDS = ("write", "read")


def note_action(sentence, conversation_id=None, kind=None) -> None:
    """Keep a confirmation sentence a WRITE fastpath just spoke.

    The sentence is the appliance's own evidence-backed prose (a read-back, not
    a claim), so "what did you just do" can repeat it instead of guessing —
    and it is tagged with what it actually WAS, so a clarifying question never
    comes back as something the appliance did.
    """
    text = " ".join(str(sentence or "").split())
    if not text:
        return
    kind = str(kind or outcome_kind(text))
    key = str(conversation_id) if conversation_id is not None \
        else current_conversation()
    now = time.time()
    with _LOCK:
        _ACTIONS.append({"ts": now, "conversation": key, "text": text,
                         "kind": kind})
        cutoff = now - ACTION_TTL
        while _ACTIONS and (_ACTIONS[0]["ts"] < cutoff
                            or len(_ACTIONS) > MAX_ACTIONS):
            _ACTIONS.pop(0)


def recent_actions(conversation_id=None, window=ACTION_WINDOW, limit=6,
                   kinds=DID_KINDS):
    """Confirmation sentences from the last *window* seconds, newest last.

    This conversation's own actions come FIRST and the rest of the household's
    follow, because "what did you just do" is asked by the person who was in
    the conversation.

    A turn that only ASKED ("Which thermostat do you mean…") is filtered
    out here: it is kept in the record, because it is part of the account of
    what happened, but it is not something the appliance DID. ``kinds=None``
    returns everything.
    """
    key = str(conversation_id) if conversation_id is not None \
        else current_conversation()
    cutoff = time.time() - float(window)
    with _LOCK:
        fresh = [dict(a) for a in _ACTIONS if a["ts"] >= cutoff]
    if kinds:
        wanted = set(kinds)
        fresh = [a for a in fresh if a.get("kind", "write") in wanted]
    mine = [a for a in fresh if a["conversation"] == key]
    theirs = [a for a in fresh if a["conversation"] != key]
    ordered = mine + theirs
    return ordered[-int(limit):] if limit else ordered
