"""«What did you just do?» is answered from the record, not from a self-portrait.

Left to the model, two minutes after the appliance has set a thermostat and
added oat milk to the shopping list::

    "what did you just do / did you change something"
    -> "I didn't change anything ... I can't directly control devices or
        run scripts."

Two false sentences in one reply: it HAS changed something, and it CAN control
devices: the thermostat write is in the store. The model answers from a
description of itself instead of from what happened, which is
the same class of defect as a confirmation composed from the request.

Every write fastpath's own confirmation sentence — already evidence-backed, a
read-back rather than a claim — is kept by :mod:`puffpai.turn_context`. This
module matches the question and reads them back. It is pure: the caller hands
in the records.
"""

from __future__ import annotations

import re

#: The window the plain "what did you just do" asks about.
DEFAULT_WINDOW = 600.0

#: The longest window a spoken "in the last N minutes" may open, matching what
#: :mod:`puffpai.turn_context` keeps at all.
MAX_WINDOW = 1800.0

MAX_SPOKEN = 5

_ACTIVITY_QUESTION_RE = re.compile(
    r"(?i)^\s*(?:and\s+|so\s+|ok(?:ay)?[,\s]+|hey[,\s]+)?"
    r"(?:"
    r"what\s+(?:did|have)\s+you\s+(?:just\s+)?(?:do|done|change|changed)\b"
    r"|what\s+(?:did|have)\s+you\s+do(?:ne)?\s+(?:in|over|during)\b"
    r"|what\s+have\s+you\s+been\s+doing\b"
    r"|did\s+you\s+(?:just\s+)?(?:change|do|touch|update|set)\s+"
    r"(?:some|any)?thing\b"
    r"|did\s+you\s+change\s+anything\b"
    r"|have\s+you\s+(?:done|changed)\s+(?:any|some)thing\b"
    r"|what\s+(?:did|have)\s+you\s+change[d]?\b"
    r")")

_MINUTES_RE = re.compile(r"(?i)\blast\s+(\d{1,3})\s*(minutes?|mins?|hours?|hrs?)\b")


def is_activity_question(text) -> bool:
    """True for "what did you just do" and its household variants. Pure."""
    return bool(_ACTIVITY_QUESTION_RE.match(str(text or "").strip()))


def window_for(text) -> float:
    """The window the question asks about, clamped to what is kept."""
    match = _MINUTES_RE.search(str(text or ""))
    if not match:
        return DEFAULT_WINDOW
    value = int(match.group(1))
    seconds = value * (3600 if match.group(2).lower().startswith(("hour", "hr"))
                       else 60)
    return float(max(60, min(seconds, MAX_WINDOW)))


def _first_sentence(text: str) -> str:
    """The confirmation, without its read-back tail.

    "Added oat milk to the shopping list. That's 32 items on it now." is one
    action; read aloud in a list of three, the second half is noise.
    """
    body = " ".join(str(text or "").split())
    match = re.match(r"^(.+?[.!?])(?:\s|$)", body)
    return (match.group(1) if match else body).strip()


NOTHING = ("I haven't changed anything in the last few minutes — nothing I did "
           "in this conversation wrote to your data.")


def reply_for(records, window=DEFAULT_WINDOW) -> str:
    """The real list of what happened, or an honest "nothing". Pure."""
    seen, lines = set(), []
    for record in (records or []):
        sentence = _first_sentence((record or {}).get("text"))
        if sentence and sentence not in seen:
            seen.add(sentence)
            lines.append(sentence)
    if not lines:
        return NOTHING
    lines = lines[-MAX_SPOKEN:]
    minutes = int(round(float(window) / 60.0))
    span = ("the last few minutes" if minutes <= 10
            else f"the last {minutes} minutes")
    return f"In {span}: " + " ".join(lines)


def handle(user_message, records, window=None) -> "str | None":
    """Fastpath: answer an activity question, or None to leave the turn alone."""
    if not is_activity_question(user_message):
        return None
    return reply_for(records, window if window is not None
                     else window_for(user_message))
