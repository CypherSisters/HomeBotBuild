"""«Where did you find that?» is answered from the LAST turn, never by searching.

"How do I purify water?" can be answered correctly from the offline library
with real citations, and then the very next turn, "Where did you find that
information?", run through retrieval becomes a NEW search on the words of
the QUESTION and comes back with three Wiktionary entries for "findes",
"finds" and "finding fault", 45 seconds later.

A source question is not a query. It has exactly one honest answer and it is
already in hand:

* the previous turn cited things → name them;
* the previous turn was a deterministic read of a local store → say so, and say
  which store;
* there is no previous turn → say that.

None of those needs a search, and running one is how the appliance ends up
citing a dictionary for a water-purification answer. This module is pure: it
takes the record :mod:`puffpai.turn_context` kept and returns a sentence.
"""

from __future__ import annotations

import re

#: How many citations are spoken. A voice reply that reads eight library names
#: aloud is a list, not an answer.
MAX_SPOKEN = 4

#: The ways a household asks where an answer came from. Every one of these is
#: ABOUT the previous answer — none of them is a topic.
_SOURCE_QUESTION_RE = re.compile(
    r"(?i)^\s*(?:and\s+|so\s+|but\s+|ok(?:ay)?[,\s]+|hey[,\s]+)?"
    r"(?:"
    r"where(?:'d|\s+(?:did|do|does))\s+(?:you|that|this|it)\s+"
    r"(?:find|get|read|learn|source|come\s+from|hear)"
    r"|where(?:'s|\s+is)\s+(?:that|this|it)\s+from"
    r"|where\s+did\s+that\s+come\s+from"
    r"|what(?:'s|\s+is|\s+was)\s+(?:your|the)\s+source"
    r"|what\s+(?:are|were)\s+(?:your|the)\s+sources"
    r"|what\s+(?:did|do)\s+you\s+base\s+(?:that|this|it)\s+on"
    r"|how\s+do\s+you\s+know\s+(?:that|this|it)"
    r"|(?:can\s+you\s+|please\s+)?cite\s+(?:that|this|it|your\s+sources?)"
    r"|(?:what|which)\s+(?:document|documents|book|books|article|articles|"
    r"librar(?:y|ies))\s+(?:did\s+)?(?:that|you)\b"
    r")"
    # Only FILLER may follow. An open tail ("[^?.!]{0,40}") let "what's the
    # source of the leak in the roof" through, which is a household question
    # about a roof, not a citation ask.
    r"(?:\s+(?:that|this|it|them|those|information|info|answer|from|of|on|"
    r"for|exactly|then|again|please|one|stuff))*"
    r"\s*[?.!]*\s*$")


def is_source_question(text) -> bool:
    """True for a question about where the PREVIOUS answer came from. Pure."""
    return bool(_SOURCE_QUESTION_RE.match(str(text or "").strip()))


def _title_of(source) -> str:
    if isinstance(source, dict):
        for key in ("title", "library", "collection"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(source or "").strip()


def spoken_titles(sources) -> list:
    """Up to :data:`MAX_SPOKEN` distinct titles, in the order they were cited."""
    out = []
    for source in (sources or []):
        title = _title_of(source)
        if title and title not in out:
            out.append(title)
        if len(out) >= MAX_SPOKEN:
            break
    return out


def _join(titles) -> str:
    if len(titles) == 1:
        return titles[0]
    return ", ".join(titles[:-1]) + " and " + titles[-1]


def _capability_phrase(capability_id) -> str:
    """A speakable name for the local store a fastpath read. Never raises."""
    if not capability_id:
        return ""
    try:
        from puffpai import capability_registry as registry
        cap = registry.get(str(capability_id))
    except Exception:
        cap = None
    for attr in ("name", "title", "description", "summary"):
        value = str(getattr(cap, attr, "") or "").strip()
        if value:
            return value[0].lower() + value[1:]
    return str(capability_id).replace(".", " ").replace("_", " ")


def _fastpath_phrase(detail) -> str:
    """The store behind a fastpath name, for the 'direct reading' sentence."""
    try:
        from puffpai.chat_activity import FASTPATH_CAPABILITY
        capability_id = FASTPATH_CAPABILITY.get(str(detail or ""))
    except Exception:
        capability_id = None
    phrase = _capability_phrase(capability_id)
    if phrase:
        return phrase
    name = str(detail or "").strip()
    if name.startswith("_try_"):
        return name[5:].replace("_", " ")
    return ""


NO_PREVIOUS = "I don't have a previous answer to cite."


def reply_for(record) -> str:
    """The one honest sentence the previous turn's record supports.

    *record* is :func:`puffpai.turn_context.last_answer`'s dict, or None.
    """
    if not isinstance(record, dict):
        return NO_PREVIOUS
    titles = spoken_titles(record.get("sources"))
    if titles:
        plural = "them" if len(titles) > 1 else "it"
        return (f"I got {plural} from your knowledge library — "
                f"{_join(titles)}.")
    if record.get("kind") == "fastpath":
        phrase = _fastpath_phrase(record.get("detail"))
        if phrase:
            return ("That last answer didn't come from the library — it was a "
                    f"direct reading of {phrase}.")
        return ("That last answer didn't come from the library — it was a "
                "direct reading of your own data on this box.")
    if record.get("kind"):
        return ("That last answer didn't come from the library — nothing was "
                "retrieved for it, so there's nothing to cite.")
    return NO_PREVIOUS


def handle(user_message, record) -> "str | None":
    """Fastpath: answer a source question, or None to leave the turn alone."""
    if not is_source_question(user_message):
        return None
    return reply_for(record)
