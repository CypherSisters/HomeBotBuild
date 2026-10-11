"""Say so when the answer comes out of a made-up world.

Why this module exists
----------------------
"what is the capital of Wakanda" can be answered "Birnin Zana... the Golden
City", and "how many dragons currently live in Westeros" "just one...
Drogon", both grounded in real ZIM articles, both cited, and neither
carrying a word to tell a household member (a child, most of all) that these
places do not exist. The knowledge library holds real articles *about*
fiction, and without this nothing downstream distinguishes them from
articles about the world.

The signal is in the retrieved rows themselves, so no model is asked:

1. **A named universe** in a hit's title or lead text — "Marvel", "A Dance
   with Dragons", "Star Wars". Those give a specific framing clause, which
   is the useful one: "In the Marvel universe, …".
2. **The encyclopedia's own lead sentence**, which for this class of article
   almost always says it outright: "Wakanda is a fictional country in East
   Africa in American comic books published by Marvel Comics." A "is a
   fictional …" / "fictitious" lead earns the generic "In fiction, …".

Limits of the heuristic, stated plainly
---------------------------------------
* It reads only the top few retrieved rows, and only their first ~400
  characters. An article whose lead buries the word "fictional" below that
  window is not detected, and the answer goes out unframed — the same
  behaviour as before this module, never a wrong claim.
* It cannot tell a question ABOUT fiction ("who wrote Game of Thrones")
  from a question INSIDE it ("how many dragons are in Westeros"). Both get
  framed. Framing a literary-history answer is mildly redundant; failing to
  frame an in-universe one is the defect, so the trade goes this way.
* A real-world article that merely mentions a novel can supply the signal if
  it lands in the top rows. :data:`_TOP_HITS` keeps that window small, and
  the universe map is checked against titles first for the same reason.
* It never removes or rewrites the answer. It prepends one clause.
"""

from __future__ import annotations

import re

#: How many of the ranked rows may supply the signal. The rows are sorted by
#: relevance, so this is "what the answer was actually written from".
_TOP_HITS = 3

#: How much of a row's text counts as its lead.
_LEAD_CHARS = 400

#: Named universes → the framing clause. Matched against a row's title first
#: (a title is what the article IS about), then its lead text.
_UNIVERSES = (
    (re.compile(r"\bmarvel\b|\bwakanda\b|\bavengers\b|\bblack\s+panther\b",
                re.I), "In the Marvel universe"),
    (re.compile(r"\bwesteros\b|\bsong\s+of\s+ice\s+and\s+fire\b|"
                r"\bgame\s+of\s+thrones\b|\bdance\s+with\s+dragons\b|"
                r"\bhouse\s+of\s+the\s+dragon\b|\btargaryen\b", re.I),
     "In George R. R. Martin's Westeros"),
    (re.compile(r"\bstar\s+wars\b|\btatooine\b|\bjedi\b", re.I),
     "In the Star Wars universe"),
    (re.compile(r"\bstar\s+trek\b|\bstarfleet\b|\bvulcan\s+\(planet\)", re.I),
     "In the Star Trek universe"),
    (re.compile(r"\bmiddle[-\s]earth\b|\btolkien\b|\blord\s+of\s+the\s+rings\b|"
                r"\bhobbit\b|\bmordor\b", re.I), "In Tolkien's Middle-earth"),
    (re.compile(r"\bharry\s+potter\b|\bhogwarts\b|\bwizarding\s+world\b", re.I),
     "In the Harry Potter books"),
    (re.compile(r"\bdc\s+comics\b|\bgotham\b|\bkrypton\b|\bjustice\s+league\b",
                re.I), "In the DC Comics universe"),
    (re.compile(r"\bpok[eé]mon\b", re.I), "In the Pokémon games"),
    (re.compile(r"\bnarnia\b", re.I), "In C. S. Lewis's Narnia"),
    (re.compile(r"\bdiscworld\b", re.I), "In Terry Pratchett's Discworld"),
)

#: The encyclopedia saying it itself.
_FICTIONAL_LEAD_RE = re.compile(
    r"\b(?:is|are|was|were)\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,2}"
    r"fiction(?:al|alis\w+)?\b"
    r"|\bin\s+the\s+fictional\b|\bfictional\s+(?:country|nation|city|place|"
    r"planet|kingdom|continent|universe|character|world|setting|realm)\b"
    r"|\bfictitious\b",
    re.IGNORECASE)

#: A metadata field that says "fiction" outright, if a pack ever carries one.
_FICTION_TAG_RE = re.compile(r"\bfiction|\bnovels?\b|\bcomics?\b", re.IGNORECASE)

#: Openers that read badly capitalised after a prepended clause. A row of
#: proper nouns ("Birnin Zana is …") is left exactly as it is.
_LOWERABLE_FIRST_WORDS = frozenset({
    "the", "a", "an", "this", "that", "there", "these", "those", "it", "its",
    "as", "according", "based", "currently", "yes", "no", "both", "one",
    "two", "three", "several", "many", "most", "some", "at", "in", "on",
    "right", "just", "only", "however", "although", "while", "dragons",
})

#: Already-framed answers must not be framed twice.
_ALREADY_FRAMED_RE = re.compile(
    r"\bfiction(?:al)?\b|\bnot\s+real\b|\bmade[-\s]up\b|\bin\s+the\s+\w+\s+"
    r"universe\b|\bin\s+the\s+(?:books?|comics?|films?|movies?|games?|show)\b",
    re.IGNORECASE)


def _row_fields(row) -> tuple:
    """``(title, lead, tag_text)`` for one retrieved row, defensively."""
    if not isinstance(row, dict):
        return "", "", ""
    title = str(row.get("title") or "")
    lead = str(row.get("text") or row.get("snippet") or "")[:_LEAD_CHARS]
    tag = " ".join(str(row.get(k) or "") for k in
                   ("category", "namespace", "content_type", "collection"))
    return title, lead, tag


def detect_framing(hits) -> str | None:
    """The framing clause for these retrieved rows, or ``None``.

    Returns e.g. ``"In the Marvel universe"``. ``None`` means no reliable
    fiction signal was found — which is a decision to say nothing, never a
    claim that the subject is real.
    """
    rows = list(hits or [])[:_TOP_HITS]
    if not rows:
        return None
    fictional = False
    for row in rows:
        title, lead, tag = _row_fields(row)
        if not (title or lead):
            continue
        row_is_fiction = bool(_FICTIONAL_LEAD_RE.search(lead))
        if not row_is_fiction and tag and _FICTION_TAG_RE.search(tag):
            row_is_fiction = True
        for pattern, clause in _UNIVERSES:
            if pattern.search(title) or pattern.search(lead):
                return clause
        fictional = fictional or row_is_fiction
    return "In fiction" if fictional else None


def frame_reply(reply: str, framing: str | None) -> str:
    """Prepend *framing* as one clause. Idempotent and never destructive."""
    text = (reply or "").strip()
    if not framing or not text:
        return reply
    if _ALREADY_FRAMED_RE.search(text[:160]):
        return reply
    first = text.split(None, 1)[0].strip(",.;:").lower()
    if first in _LOWERABLE_FIRST_WORDS and not text.startswith("I "):
        text = text[0].lower() + text[1:]
    return "%s, %s" % (framing, text)


def frame_for_hits(reply: str, hits) -> str:
    """``frame_reply(reply, detect_framing(hits))``, exception-proof."""
    try:
        return frame_reply(reply, detect_framing(hits))
    except Exception:  # pragma: no cover - framing must never break an answer
        return reply
