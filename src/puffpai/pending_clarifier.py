"""A question the appliance ASKED, kept just long enough to hear the answer.

Three failures that voice use runs into are one defect wearing three hats:

* "delete the soccer reminder" correctly asks "which one?", and the answer
  "the every-weekday one" is routed as a brand-new query. It reaches the
  knowledge library and comes back with a dictionary definition of "every
  weekday". Nothing is deleted.
* "set an alarm for seven" correctly asks "7 AM or 7 PM?", and "PM" comes
  back as a dictionary entry for *post-mortem / prime minister / private
  message*. No alarm is created.
* a medication reminder that asks for a name, a dose and a time AND writes a
  garbled row anyway, so the answer makes a second one.

The shared cause is a clarifying question that leaves no state behind: the
next turn re-enters routing as if the question had never been asked. Per-hook
patches would have to be written once per question, and every future question
would ship the bug again. So the state lives HERE, once, and the hooks that ask
register it.

The shape deliberately copies the two stores that already work —
:mod:`puffpai.turn_context` (module-level dict, one slot per conversation, a
short TTL) and ``calendar_nl._PENDING_DELETES`` (conversation-scoped, 120 s,
dropped by any intervening turn). A record is:

* **conversation-scoped** — an answer resolves only the question asked in the
  conversation it was asked in; a surface with no id gets an anonymous key that
  nothing can ever look up, so a stray "both" cannot delete somebody's calendar.
* **short-lived** — :data:`PENDING_TTL` seconds, the same two minutes the meal
  plan's confirmation and the calendar's delete prompt already use.
* **single use** — cleared BEFORE the completion runs, so a completion that
  asks a follow-up question of its own (the medication rule does) re-registers
  rather than leaving the old record behind.
* **honest about not knowing** — an utterance that does not resolve the
  question clears the record and returns None, and the turn carries on exactly
  as it does today. The failure mode must be "it forgot", never "it acted on
  something nobody chose".

Nothing here reads a store, writes a store or speaks: the caller supplies a
``complete`` callable and this module decides only WHICH option the person
named (or, for a free-form question, hands the raw words over).
"""

from __future__ import annotations

import re
import threading
import time
import uuid

#: How long a question stays answerable. Same two minutes as the calendar's
#: delete prompt and ``meal_plan_guard.PENDING_TTL``.
PENDING_TTL = 120.0

#: Longer than this is a new request, not an answer to a question.
MAX_ANSWER_CHARS = 160

_LOCK = threading.Lock()

#: conversation key -> record
_PENDING: dict[str, dict] = {}


def reset() -> None:
    """Drop every open question. Tests only."""
    with _LOCK:
        _PENDING.clear()


# ── the words an answer is made of ──────────────────────────────────

#: Words that carry no choice in them. Stripped before an option is matched so
#: "the every-weekday one" is compared on ``every weekday``.
_FILLER = frozenset("""
a an and at be but by can could delete do does doing done for from get go
i id ill im in is it its just keep let lets like make me mean meant my of ok
okay on one ones or our please put remove said say set should so thanks that
thats the their them then there they this to use want was we with would yes
yeah yep you your cancel choice choose option pick take
""".split())

_ORDINALS = {
    "first": 0, "1st": 0, "former": 0, "top": 0, "earlier": 0,
    "second": 1, "2nd": 1, "latter": -1, "other": 1,
    "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
    "last": -1, "latest": -1, "bottom": -1, "final": -1,
}

_BOTH_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:delete|remove|cancel|do)?\s*(?:them\s+)?"
    r"(?:both|all\s+of\s+(?:them|those|it)|all\s+three|all)"
    r"(?:\s+of\s+them)?\s*(?:please)?\s*[.!?]*\s*$")

_THAT_ONE_RE = re.compile(
    r"(?i)^\s*(?:yes[,\s]+)?(?:that|this|the)\s+one\s*[.!?]*\s*$"
    r"|^\s*(?:yes|yeah|yep|that's\s+(?:it|the\s+one))\s*[.!?]*\s*$")

_BARE_NUMBER_RE = re.compile(r"(?i)^\s*(?:number\s+|option\s+|#)?(\d{1,2})"
                             r"\s*(?:please)?\s*[.!?]*\s*$")


def _words(text) -> list:
    """Lower-case alphanumeric words. "every-weekday" -> [every, weekday]."""
    return [w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if w]


def _option(spec, index: int) -> dict:
    """Normalise one caller-supplied option into {label, value, tokens}."""
    if isinstance(spec, dict):
        label = str(spec.get("label") or spec.get("value") or "").strip()
        value = spec.get("value", spec.get("label"))
        extra = spec.get("tokens") or ()
    else:
        label = str(spec or "").strip()
        value = spec
        extra = ()
    tokens = set(_words(label)) | {w for t in extra for w in _words(t)}
    return {"label": label, "value": value, "index": index,
            "tokens": tokens - _FILLER}


def match_options(options, text):
    """The option(s) *text* names, or None. Pure — no state, no side effects.

    Returns a LIST (so "both" is expressible) or None for "that did not answer
    the question", which the caller must treat as a change of subject rather
    than as a guess.
    """
    # Registered records arrive normalised; a caller (and every test) may pass
    # the raw specs instead, so normalise anything that is not already one.
    options = [opt if isinstance(opt, dict) and isinstance(opt.get("tokens"),
                                                          set)
               else _option(opt, index)
               for index, opt in enumerate(options or [])]
    if not options:
        return None
    raw = " ".join(str(text or "").split())
    if not raw or len(raw) > MAX_ANSWER_CHARS:
        return None

    if len(options) > 1 and _BOTH_RE.match(raw):
        return list(options)

    words = _words(raw)
    # "the second one" / "the recurring one" both end in "one"; the ordinal is
    # checked first because it is unambiguous when it is present at all.
    for word in words:
        if word in _ORDINALS:
            index = _ORDINALS[word]
            if index == -1:
                return [options[-1]]
            if index < len(options):
                return [options[index]]
            return None

    number = _BARE_NUMBER_RE.match(raw)
    if number:
        index = int(number.group(1))
        return [options[index - 1]] if 1 <= index <= len(options) else None

    if _THAT_ONE_RE.match(raw):
        # "that one" points at nothing when there is more than one thing to
        # point at. With a single candidate it is a plain confirmation.
        return [options[0]] if len(options) == 1 else None

    content = set(words) - _FILLER
    if not content:
        return None
    scores = [len(content & opt["tokens"]) for opt in options]
    best = max(scores)
    if best <= 0 or scores.count(best) != 1:
        return None
    return [options[scores.index(best)]]


# ── the record ──────────────────────────────────────────────────────

def _key(conversation_id) -> str:
    if conversation_id is None:
        try:
            from puffpai import turn_context
            conversation_id = turn_context.current_conversation()
        except Exception:      # pragma: no cover - import guard
            conversation_id = ""
    return str(conversation_id or "").strip()


def register(kind: str, complete, options=None, *, conversation_id=None,
             prompt: str = "") -> None:
    """Remember that this turn ASKED something, and how to finish it.

    *complete* is called as ``complete(chosen, text)`` — ``chosen`` being the
    list of matched options (None for a free-form question) and ``text`` the
    person's exact words — and returns the sentence to speak, or None when the
    answer still is not usable.
    """
    if not callable(complete):
        return
    key = _key(conversation_id)
    if not key:
        # No conversation to answer in. File it where nothing can look it up:
        # asking is still right, but a later "both" from another surface must
        # not resolve a question it never heard.
        key = "anon:" + uuid.uuid4().hex
    record = {
        "kind": str(kind or ""),
        "options": [_option(o, i) for i, o in enumerate(options or [])],
        "complete": complete,
        "prompt": str(prompt or ""),
        "created": time.time(),
    }
    with _LOCK:
        _PENDING[key] = record


def peek(conversation_id=None):
    """The open question for this conversation, or None. Expires in place."""
    key = _key(conversation_id)
    if not key:
        return None
    with _LOCK:
        record = _PENDING.get(key)
        if record is None:
            return None
        if time.time() - record.get("created", 0) > PENDING_TTL:
            _PENDING.pop(key, None)
            return None
        return record


def clear(conversation_id=None) -> None:
    key = _key(conversation_id)
    if key:
        with _LOCK:
            _PENDING.pop(key, None)


def resolve(text, conversation_id=None):
    """Finish the open question with *text*, or None to route normally.

    None means one of three honest things: nothing was asked, the question has
    expired, or the person said something else. In every one of them the record
    is gone afterwards, because a question nobody answered must not be waiting
    for the turn after next.
    """
    record = peek(conversation_id)
    if record is None:
        return None
    raw = " ".join(str(text or "").split())
    chosen = None
    if record["options"]:
        chosen = match_options(record["options"], raw)
        if chosen is None:
            clear(conversation_id)
            return None
    elif not raw or len(raw) > MAX_ANSWER_CHARS:
        clear(conversation_id)
        return None
    # Cleared BEFORE completing: single use, and a completion that asks a
    # follow-up of its own re-registers into an empty slot.
    clear(conversation_id)
    try:
        reply = record["complete"](chosen, raw)
    except Exception:
        return None
    return reply or None


__all__ = ["MAX_ANSWER_CHARS", "PENDING_TTL", "clear", "match_options",
           "peek", "register", "reset", "resolve"]
