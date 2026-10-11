"""Explicit knowledge-library searches, answered from FTS rather than a plan.

Why this module exists
----------------------
Without this module, "search the knowledge library for how to purify drinking
water" fails while the *neighbouring* phrasing ("what is the wingspan of a
Boeing 747") answers correctly with a `zim_fts` citation. The two questions
ask the same store; only the wording differs, and the wording decides the
route:

* The router's agent keyword list contains "search", so an explicit library
  search is classified ``RouteTarget.AGENT`` before the library rule is ever
  reached (``router._route_message``). The plain factual question has no agent
  keyword and lands on ``rag``, which is why it works.
* On the agent route the planning loop picks ``archivist.get_memory``, runs it,
  and reports ``"I ran 1 of 1 planned step(s): ... Everything I planned ran."``
  That report is a WORK LOG, not an answer, and it trips
  ``api_server._CHAT_LEAK_MARKERS`` ("planned step", "step(s):") — so the guard
  replaces a "successful" plan with ``"Sorry, I couldn't complete that one."``

The journal for such a turn (7.56 s with no cold model reload, so this is a
routing problem, not model eviction)::

    [Chat] Route: agent for: search the knowledge library for how to purify...
    [I] puffpai.agents.engine: engine plan ok in 5.72s (qwen3:30b-a3b)
    [Chat] Team handled: plan -> []
    [W] puffpai.api_server: suppressed chat reasoning-leak: I ran 1 of 1
        planned step(s): 1. archivist.get_memory - done

What this module does
---------------------
Recognises the explicit "search my library for X" shapes, extracts the TOPIC,
and hands it back so the caller can run the retrieval stack directly. The
retrieval itself is deterministic and needs no model, so a question whose
answer is sitting in FTS is never decided by whether a 20B model happens
to be warm: ``render()`` produces a cited answer from the hits alone, and the
caller only reaches for prose when retrieval has already found something.

Discipline, inherited from :mod:`puffpai.agents.household_reads`
----------------------------------------------------------------
* One narrow, anchored trigger, plus an exclude that vetoes the MEDIA readings
  of the same words ("search my movie library").
* Pure: ``match`` and ``render`` do no I/O, so both are testable without Flask,
  without ChromaDB and without a model.
* READ only. Nothing here writes, and nothing here reaches the network.
"""

from __future__ import annotations

import re

__all__ = ["match", "render", "MAX_LEN"]


def _re(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


#: The knowledge store, by the names a person uses for it. Deliberately does
#: NOT include a bare "library": "search my library" is ambiguous with the
#: media shelves, and ``_MEDIA_RE`` below cannot disambiguate what was never
#: qualified. A bare "library" therefore keeps its existing route.
_STORE = (r"(?:knowledge\s+(?:library|libraries|base|store)"
          r"|(?:reference|offline)\s+librar(?:y|ies))")

#: "search the knowledge library FOR <topic>" and its siblings. Each pattern
#: captures the topic in group "topic"; anything that matches but captures
#: nothing usable falls through to the old path.
_PATTERNS = (
    # search / check / look in / query the knowledge library for X
    _re(r"\b(?:search|check|query|scan|look)\s+(?:in\s+|through\s+)?"
        r"(?:the\s+|my\s+|our\s+|your\s+)?" + _STORE +
        r"\s+(?:for|about|on|regarding)\s+(?P<topic>.+)$"),
    # look X up in the knowledge library / find X in the knowledge base
    _re(r"\b(?:look\s+up|find|search\s+for)\s+(?P<topic>.+?)\s+"
        r"(?:in|from|using|against)\s+"
        r"(?:the\s+|my\s+|our\s+|your\s+)?" + _STORE + r"\b"),
    # what does the knowledge library say about X
    _re(r"\bwhat\s+does\s+(?:the\s+|my\s+|our\s+|your\s+)?" + _STORE +
        r"\s+(?:say|know|have|tell\s+(?:me|us))\s+"
        r"(?:about|on|regarding)\s+(?P<topic>.+)$"),
)

#: The media shelves answer to the same verbs. ``media_library`` owns those and
#: must keep them, so a qualified media noun vetoes this layer outright.
_MEDIA_RE = _re(r"\b(?:movie|film|music|song|album|artist|playlist|book|"
                r"audiobook|ebook|comic|photo|picture|video|tv|show|episode|"
                r"media|jellyfin|calibre|spotify)\b")

#: Longest utterance this layer will claim. Past this it is a conversation
#: that happens to mention the library, not a lookup.
MAX_LEN = 200

#: Filler that a captured topic may end with, and question punctuation.
_TRAILING = _re(r"^(?P<core>.*?)[\s,;:.!?\"'’”]*$")
_FILLER = _re(r"\b(?:please|thanks|thank\s+you|for\s+me|would\s+you|"
              r"if\s+you\s+can|right\s+now)\b")


def match(user_message: str) -> "str | None":
    """The TOPIC this utterance asks the knowledge store for, or None.

    Pure — no I/O, so the tests exercise the real trigger table.
    """
    text = (user_message or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    if _MEDIA_RE.search(text):
        return None
    for pattern in _PATTERNS:
        found = pattern.search(text)
        if not found:
            continue
        topic = _FILLER.sub(" ", found.group("topic") or "")
        topic = _TRAILING.sub(r"\g<core>", " ".join(topic.split()))
        # A one-character "topic" is a parse accident, not a question.
        if len(topic) < 2:
            return None
        return topic
    return None


def _hit_label(hit: dict) -> str:
    """"Title (Library)" for a retrieval hit, without repeating either."""
    library = str(hit.get("library") or "").strip()
    title = str(hit.get("title") or "").strip()
    if title and library and title.lower() != library.lower():
        return f"{title} ({library})"
    return title or library or "the knowledge library"


def render(topic: str, hits, max_passages: int = 3,
           passage_chars: int = 420) -> str:
    """A cited answer built from retrieval hits alone — no model involved.

    This is the floor the knowledge route now stands on: when the prose leg is
    unavailable (model evicted, ollama reloading, request timed out) the user
    still gets the passages and their sources instead of
    "Sorry, I couldn't complete that one." Returns "" when there is genuinely
    nothing to show, which the caller treats as "fall through unchanged".
    """
    rows = [h for h in (hits or []) if isinstance(h, dict)
            and str(h.get("text") or "").strip()]
    if not rows:
        return ""
    rows = rows[:max_passages]

    parts = [f"From your knowledge library on “{topic}”:"]
    for i, hit in enumerate(rows, 1):
        text = " ".join(str(hit.get("text") or "").split())
        if len(text) > passage_chars:
            cut = text.rfind(" ", 0, passage_chars)
            text = text[:cut if cut > 0 else passage_chars].rstrip() + "…"
        parts.append(f"{i}. {text}\n   — {_hit_label(hit)}")
    return "\n\n".join(parts)
