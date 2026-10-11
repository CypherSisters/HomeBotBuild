"""Explicit "search my documents for X", answered from the upload index.

Why this module exists
----------------------
"search my documents for soccer" takes 40 to 60 s on the appliance through
the agent route. The reply is right; the route is the cost:

* the router sends the phrasing to ``agent``; qwen3 classifies (~1 s) and
  plans (4.4 to 6.4 s), and the plan can run dry
  ("planning run dry (error, 0 executed); falling back");
* the legacy gpt-oss agent then spends one whole round (8 to 16 s) only to
  choose ``search_my_documents``, and a second round (22 to 30 s) writing 355
  to 541 tokens, of which voice speaks two sentences.

The words already say which tool to call and with what topic, so no model is
needed to decide either. This module recognises the explicit shapes, extracts
the TOPIC, and renders the upload hits deterministically. The caller runs the
same retrieval the agent's tool runs (``upload_index.search_my_documents``).

Discipline, inherited from :mod:`puffpai.agents.knowledge_reads`
----------------------------------------------------------------
* Narrow and anchored: an explicit search verb, an explicit "my documents /
  docs / files / uploads / notes", and a non-empty topic. "my notes" is in
  because the registry already maps it to the same tool
  (``capability_registry`` ``knowledge.documents``).
* A veto for every neighbour that shares the words: the web, the knowledge
  library and "the books", email, the calendar. Vetoed anywhere in the turn,
  so a mixed ask ("my documents and the web") keeps its old route whole.
* One request, one plain topic. A joiner or sentence break in the topic, a
  run-on command, or anything but a greeting before the search stands down,
  so a second request is never dropped.
* Pure: ``match`` and ``render`` do no I/O, so both are testable without Flask,
  without ChromaDB and without a model. READ only, and never the network.
"""

from __future__ import annotations

import os
import re

__all__ = ["match", "without_topic", "render", "MAX_LEN", "MAX_DOCS"]


def _re(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


#: The household's own uploads, by the names a person uses for them. Always
#: behind "my", so "search the documents" (whose?) keeps its old route.
_STORE = (r"my\s+(?:uploaded\s+)?(?:documents|docs|files|uploads|notes)")

#: The same without "files", for the "find X in my files" shape only: "find
#: the matrix in my files" and "look for my keys in my files" are media and
#: household asks far more often than a document search.
_STORE_NO_FILES = r"my\s+(?:uploaded\s+)?(?:documents|docs|uploads|notes)"

#: "all my", "all of my", "through my": optional glue before the store.
_GLUE = r"(?:(?:in|through|in\s+through)\s+)?(?:all\s+(?:of\s+)?)?"

#: Each pattern captures the topic in group "topic".
_PATTERNS = (
    # search / check / look in / look through / dig through my documents for X
    _re(r"\b(?:search|check|scan|look|dig|go)\s+" + _GLUE + _STORE +
        r"\s+(?:for|about|on|regarding)\s+(?P<topic>.+)$"),
    # find X in my documents / search for X in my docs / look up X in my notes
    _re(r"\b(?:find|search\s+for|look\s+for|look\s+up)\s+(?P<topic>.+?)\s+"
        r"(?:in|from|among|across)\s+(?:all\s+(?:of\s+)?)?" +
        _STORE_NO_FILES + r"[\s,;:.!?\"']*$"),
    # what's in my documents about X
    _re(r"\bwhat(?:['’]?s|\s+is)\s+in\s+(?:all\s+(?:of\s+)?)?" + _STORE +
        r"\s+(?:about|on|regarding)\s+(?P<topic>.+)$"),
    # what do my documents say about X
    _re(r"\bwhat\s+do\s+(?:all\s+(?:of\s+)?)?" + _STORE +
        r"\s+(?:say|have|mention)\s+(?:about|on|regarding)\s+(?P<topic>.+)$"),
)

#: Neighbours that share the verbs. Any of these anywhere in the turn and the
#: turn keeps its old route: web search is Toolsmith's (and egress-gated), the
#: knowledge library is _try_knowledge_search's, and mail and the calendar
#: have their own readers.
_VETO_RE = _re(
    r"\b(?:web|online|internet|google|bing|duckduckgo|website|the\s+net)\b"
    r"|\b(?:knowledge|encyclopedias?|wikipedia|kiwix)\b"
    r"|\b(?:the|my|our|your|reference|offline)\s+(?:librar(?:y|ies)|books)\b"
    r"|\b(?:e-?mails?|inbox|gmail|mailbox)\b"
    r"|\bcalendar\b")

#: This layer answers exactly ONE plain topic. Anything shaped like two parts
#: keeps its old route whole, because the old route (the team) can do both
#: halves and this one cannot: "search my documents for soccer and turn
#: on the lights" answered as a search for "soccer and turn on the
#: lights" silently drops the lights. A verb list cannot be complete ("and
#: print it", "and close the garage", "the lease. turn on the lights" all
#: slipped past one), so the rule is structural: any
#: joiner or sentence break inside the topic stands down. "salt and pepper"
#: pays with the slow path, which is the safe side.
_SECOND_PART_RE = _re(
    r"[,;:.!?&]|\b(?:and|then|plus|also|or|but)\b|\bafter\s+that\b")

#: A run-on second request with no joiner at all ("soccer turn on the
#: lights"): an imperative verb followed by a particle, pronoun or article.
_RUN_ON_RE = _re(
    r"\b(?:turn|switch|set|play|read|tell|remind|show|open|close|lock|"
    r"unlock|start|stop|pause|mute|dim|call|text|email|send|print|save|"
    r"share|forward|put|add|make|give|heat|cool)\s+"
    r"(?:on|off|up|down|me|us|it|them|him|her|the|a|an|my|our|some|to)\b")

#: What may come before the search itself: a greeting, the bot's name and
#: politeness. Anything else in front ("turn on the lights and search my
#: documents for X") is a second request, so the turn keeps its old route.
_LEAD_IN_RE = _re(
    r"^(?:(?:hey|hi|hello|ok|okay|so|um|uh)\b[\s,.!]*)*"
    r"(?:puff(?:\s*(?:pie|pai|py))?\b[\s,.!]*)?"
    r"(?:(?:please|can|could|would|will)\b\s*(?:you\b\s*)?"
    r"(?:please\b\s*)?)?"
    r"(?:(?:go\s+ahead\s+and|i\s+want\s+you\s+to|i'?d\s+like\s+you\s+to)"
    r"\s+)?$")

#: A topic that is the upload itself ("the file I sent you") is the
#: inventory question, which ``_try_my_files`` answers with the list.
_UPLOAD_ITSELF_RE = _re(
    r"\b(?:files?|documents?|docs?|uploads?|pictures?|photos?|pdfs?)\s+"
    r"(?:I|we|you)\b|\bI\s+(?:sent|uploaded|gave|shared|added)\b")

#: A topic about media belongs to the media fastpaths, not to the documents.
_MEDIA_TOPIC_RE = _re(
    r"\b(?:movies?|films?|songs?|music|playlists?|albums?|artists?|tracks?|"
    r"videos?|episodes?)\b")

#: A secret is never looked up here. Whole words only: "pin" is not
#: "spinach" or "pinball". Kept local on purpose; the vault gate's own keyword
#: list (``api_server._is_protected_query``) is not widened by this.
_SECRET_TOPIC_RE = _re(
    r"\b(?:passwords?|passcodes?|passphrases?|pin|logins?|credentials?)\b")

#: Longest utterance this layer will claim. Past this it is a conversation
#: that happens to mention documents, not a lookup.
MAX_LEN = 200

#: How many documents a rendered reply names and quotes.
MAX_DOCS = 3

_TRAILING = _re(r"^(?P<core>.*?)[\s,;:.!?\"'’”]*$")
_FILLER = _re(r"\b(?:please|thanks|thank\s+you|for\s+me|would\s+you|"
              r"if\s+you\s+can|right\s+now)\b")
#: "anything about soccer" asks about soccer.
_LEAD = _re(r"^(?:anything|everything|all|something|stuff|info|information|"
            r"things?|"
            r"mentions?\s+of)\s+(?:about|on|regarding|related\s+to|of)\s+")
#: A topic that names nothing is an inventory question, which
#: ``list_my_documents`` owns.
_EMPTY_TOPICS = frozenset({
    "anything", "something", "stuff", "everything", "things", "it", "that",
    "this", "them", "info", "information",
})


def _find(text: str):
    """(topic, the regex match it came from), or (None, None)."""
    if not text or len(text) > MAX_LEN:
        return None, None
    if _VETO_RE.search(text):
        return None, None
    for pattern in _PATTERNS:
        found = pattern.search(text)
        if not found:
            continue
        if not _LEAD_IN_RE.match(text[:found.start()]):
            return None, None
        raw = found.group("topic") or ""
        topic = _FILLER.sub(" ", raw)
        topic = _TRAILING.sub(r"\g<core>", " ".join(topic.split()))
        if (_SECOND_PART_RE.search(topic) or _RUN_ON_RE.search(topic)
                or _MEDIA_TOPIC_RE.search(raw) or _SECRET_TOPIC_RE.search(raw)
                or _UPLOAD_ITSELF_RE.search(raw)):
            return None, None
        topic = _LEAD.sub("", topic).strip()
        if len(topic) < 2 or topic.lower() in _EMPTY_TOPICS:
            return None, None
        return topic, found
    return None, None


def match(user_message: str) -> "str | None":
    """The TOPIC this utterance asks the household's documents for, or None.

    Pure: no I/O, so the tests exercise the real trigger table.
    """
    return _find((user_message or "").strip())[0]


def without_topic(user_message: str) -> str:
    """The request with its topic cut out, as said ("everything about X").

    For the voice speech budget: "search my documents for the list of chores"
    is not a request to read a list back, whatever the topic's words are.
    Returns the text unchanged when this layer does not claim it.
    """
    text = (user_message or "").strip()
    topic, found = _find(text)
    if topic is None:
        return text
    return (text[:found.start("topic")] + " "
            + text[found.end("topic"):]).strip()


def _title(row: dict) -> str:
    """What to call a document: its filename, without a file extension."""
    name = str(row.get("filename") or row.get("id") or "").strip()
    stem, ext = os.path.splitext(name)
    if stem and re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext or ""):
        return stem.strip()
    return name or "an untitled document"


def _quote(snippet: str, limit: int) -> str:
    """A short quotable piece of a snippet: its first sentence, or a cut.

    Returned WITHOUT its closing full stop, because the caller closes the
    line after the quote mark: the sentence splitter behind the spoken cap
    only breaks on punctuation followed by a space, and a stop inside the
    quote would run two documents into one spoken sentence.
    """
    text = " ".join(str(snippet or "").split())
    if not text:
        return ""
    end = re.search(r"[.!?](?=\s|$)", text[:limit + 1])
    if end and end.end() >= 20:
        return text[:end.start()].rstrip()
    if len(text) <= limit:
        return text.rstrip(" .")
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip(" ,;:.") + "..."


def _join(names: list) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


#: Words too common to show that a document is about the topic.
_STOP_WORDS = frozenset({
    "the", "a", "an", "my", "our", "your", "of", "for", "to", "in", "on",
    "and", "or", "about", "with", "from", "at", "by", "is", "are", "it",
    "this", "that", "these", "those", "any", "all", "some",
})


def _topic_words(topic: str) -> set:
    """The topic's content words, each with a plain singular form beside it."""
    words = set()
    for word in re.findall(r"[a-z0-9]+", topic.lower()):
        if len(word) < 3 or word in _STOP_WORDS:
            continue
        words.add(word)
        if word.endswith("ies") and len(word) > 4:
            words.add(word[:-3] + "y")                  # batteries, battery
        elif word.endswith(("ches", "shes", "sses", "xes", "zes")):
            words.add(word[:-2])                        # boxes, box
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            words.add(word[:-1])                        # recipes, recipe
    return words


def _mentions(row: dict, words: set) -> bool:
    """True when the document's name or its matched text names the topic.

    The index answers by meaning, so with nothing on the topic it still
    returns its nearest rows ("recipes" can find a workout plan and a radio
    station). Without a model to say "none of these are it",
    only rows that actually mention the topic are presented as about it.
    """
    text = (str(row.get("filename") or "") + " "
            + str(row.get("snippet") or "")).lower()
    return any(re.search(r"\b" + re.escape(word), text) for word in words)


def render(topic: str, rows, *, failed: bool = False,
           max_docs: int = MAX_DOCS, quote_chars: int = 160) -> str:
    """The reply, built from the upload hits alone. No model involved.

    Sentence one names the documents, sentence two quotes the best one, so
    the spoken cap (two sentences) says what matched and what it says, and
    the screen gets every quote. ``failed`` means the search itself could not
    run, which is said as such rather than as "nothing found".
    """
    topic = str(topic or "").strip() or "that"
    if failed:
        return (f"I couldn't search your documents just now, so I can't "
                f"tell you what they say about {topic}.")
    docs = [r for r in (rows or []) if isinstance(r, dict)]
    if not docs:
        return f"I didn't find anything about {topic} in your documents."
    words = _topic_words(topic)
    about = [r for r in docs if _mentions(r, words)] if words else docs
    if not about:
        closest = [_title(r) for r in docs[:2]]
        return (f"I didn't find anything that mentions {topic} in your "
                f"documents. The closest were {_join(closest)}.")
    docs = about[:max_docs]
    names = [_title(r) for r in docs]
    count = len(docs)
    lines = [f"I found {count} document{'s' if count != 1 else ''} "
             f"about {topic}: {_join(names)}."]
    for name, row in zip(names, docs):
        quote = _quote(row.get("snippet"), quote_chars)
        if quote:
            lines.append(f"{name} says: \"{quote}\".")
    return "\n".join(lines)
