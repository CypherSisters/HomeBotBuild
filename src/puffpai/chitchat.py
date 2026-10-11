"""Small talk gets a small answer — not the encyclopedia.

Why this module exists
----------------------
Without it, "how's it going", "hey whats up", "good morning" and "you doing
okay today" all route to ``rag``, the same route as "how far is the moon from
earth", and come back with citations attached to a greeting, or a
retrieved-document non-sequitur such as "nothing urgent on the health front
and no new alerts in the local environment".

A greeting has no answer in a knowledge library, so it must not be sent to
one. :func:`match_social` recognises a **bounded** set of social utterances
and returns a short reply built here; ``sources`` stays empty because no
source was consulted.

The hard part is what it must NOT claim. "how's the weather going" and
"what's going on tomorrow" are greetings' near-neighbours and belong to the
weather and calendar paths. Three defences, in order:

1. This helper is consulted LATE in the instant-answer chain, so every
   hand-tuned fastpath (weather, calendar, timers, household reads…) has
   already had its turn.
2. :data:`_NOT_SMALL_TALK_RE` declines anything naming a household noun, a
   day/date, or a topic word — "how's the weather going" carries "weather",
   "what's going on tomorrow" carries "tomorrow".
3. Every pattern is anchored to the WHOLE utterance. A greeting with a
   question welded onto it ("hi, what's on the calendar") matches nothing
   here and falls through untouched.
"""

from __future__ import annotations

import re
from datetime import datetime

#: A household noun makes an utterance a request, whatever it is wrapped in.
#: Vetoes every branch below — "good morning, what's on the calendar" is a
#: calendar question with a greeting stuck to the front.
_NOT_SMALL_TALK_RE = re.compile(
    r"\b(?:weather|forecast|temperature|rain|snow|calendar|schedule|event|"
    r"events|appointment|meeting|task|tasks|reminder|reminders|timer|alarm|"
    r"light|lights|lock|door|camera|thermostat|music|play|news|bill|bills|"
    r"pantry|grocery|groceries|shopping|meal|dinner|lunch|breakfast|recipe|"
    r"medication|meds|pill|workout|streak|vault|network|device|devices|"
    r"morning\s+(?:routine|briefing)|briefing)\b",
    re.IGNORECASE)

#: A day or a date makes it a lookup — "what's going on tomorrow". Vetoes the
#: loose statement branch only: the anchored greeting/how-are-you patterns
#: below permit a trailing "today" ("you doing okay today" is small talk).
_TEMPORAL_RE = re.compile(
    r"\b(?:tomorrow|yesterday|this\s+week|next\s+week|weekend|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE)

#: A trailing time-of-day word the social patterns tolerate.
_SOCIAL_TAIL = r"(?:\s+(?:today|tonight|right\s+now|lately|so\s+far|this\s+morning))?"

#: Greetings. "good morning/afternoon/evening" are here rather than in the
#: topic list above; the block regex only excludes "morning routine"/"morning
#: briefing", which are real requests.
_GREETING_RE = re.compile(
    r"\A\W*(?:hi|hey|hello|yo|howdy|hiya|sup|heya|hi\s+there|hey\s+there|"
    r"hello\s+there|good\s+(?:morning|afternoon|evening)|morning|evening)"
    r"(?:\s+(?:puff|puffpai|puff\s*pai|puff\s*pie|there|again|everyone|all))?"
    + _SOCIAL_TAIL + r"\W*\Z",
    re.IGNORECASE)

#: "how's it going" and its family — a question, but about the assistant, and
#: no library holds the answer.
_HOW_ARE_YOU_RE = re.compile(
    r"\A\W*(?:so\s+|hey\s+|hi\s+|well\s+)*"
    r"(?:how(?:'?s|\s+is|\s+are|\s+ya|\s+you)?\s+"
    r"(?:it\s+going|things|everything|you|you\s+doing|you\s+going|"
    r"your\s+day(?:\s+going)?|it\s+been|you\s+been|things\s+going)"
    r"|how\s+have\s+you\s+been"
    r"|how\s+ya\s+doin[g']?"
    r"|what(?:'?s|\s+is)\s+(?:up|new|good|happening)"
    r"|you\s+(?:doing\s+)?(?:ok|okay|good|alright|well)"
    r"|are\s+you\s+(?:ok|okay|good|alright|well|there)"
    r"|you\s+there)"
    + _SOCIAL_TAIL + r"\W*\Z",
    re.IGNORECASE)

#: Acknowledgements — "thanks", "got it", "never mind". No answer is owed
#: beyond acknowledging them.
_THANKS_RE = re.compile(
    r"\A\W*(?:thanks|thank\s+you|thx|ty|much\s+appreciated|appreciate\s+it|"
    r"thanks\s+(?:puff|a\s+lot|so\s+much|again))"
    r"(?:[\s,]+(?:puff|puffpai|puff\s*pie|so\s+much|a\s+lot|again))?\W*\Z",
    re.IGNORECASE)
_ACK_RE = re.compile(
    r"\A\W*(?:ok|okay|k|cool|nice|great|awesome|got\s+it|gotcha|sounds\s+good|"
    r"perfect|alright|sure|right|yep|yeah|yup|no\s+worries|never\s+mind|"
    r"nevermind|forget\s+it|makes\s+sense)\W*\Z",
    re.IGNORECASE)
_BYE_RE = re.compile(
    r"\A\W*(?:bye|goodbye|good\s*night|night|see\s+ya|see\s+you|"
    r"talk\s+to\s+you\s+later|later|catch\s+you\s+later|i'?m\s+off|"
    r"heading\s+(?:out|to\s+bed))"
    r"(?:\s+(?:puff|puffpai|puff\s*pie))?\W*\Z",
    re.IGNORECASE)

#: A social ANSWER, optionally trailing a bit of personal news:
#: "fine I just got back from Seattle". Statement only — a question mark or a
#: question word sends it back to normal routing, and the block regex above
#: has already removed anything with a household noun or a date in it.
_SOCIAL_OPENER_RE = re.compile(
    r"\A\W*(?:i'?m\s+)?(?:fine|good|great|ok|okay|alright|well|not\s+bad|"
    r"pretty\s+good|doing\s+(?:well|good|fine|ok|okay)|tired|"
    r"busy|hanging\s+in\s+there|same\s+old|can'?t\s+complain)\b",
    re.IGNORECASE)
_QUESTION_WORD_RE = re.compile(
    r"\b(?:what|when|where|why|who|which|how|can|could|would|will|should|"
    r"do|does|did|is|are|was|were|please)\b", re.IGNORECASE)

#: Longest utterance treated as small talk. Anything wordier is a request.
_MAX_WORDS = 14


#: What the wake word comes back as. STT hears "Puff-Pies", "puff pie",
#: "puffpie", "puff pi". Without this, "Thanks, Puff-Pies!" misses the thanks
#: rule, "pies" is read as PASTRY, and the speaker is offered kitchen help.
#: Every social pattern below matches on the normalised text, so the name
#: only has to be spelled once.
_NAME_VARIANT_RE = re.compile(
    r"(?i)\bpuff[\s-]?(?:pai|pie|pies|pis|pys|py|pi)\b|\bpuffpai\b")

#: "I love you" must not get "I'm just a program - I don't have the
#: capacity to feel emotions." from something a person just spent an hour
#: with. Warm, short, in character, and never a disclaimer about
#: what it is: nobody asked.
_LOVE_RE = re.compile(
    r"(?i)\A\W*(?:i\'?(?:ve| have)?\s+)?(?:love|\u2764|heart)\s*(?:you|ya|u)?(?:\s+(?:so\s+much|too|puff|buddy))*\W*\Z")
_LOVE_REPLIES = (
    "Love you too. Now — what can I do for you?",
    "Aw, love you too. I'm right here whenever you need me.",
    "Love you too — that is the nicest thing anyone has said to me all "
    "day.",
)


def normalise_name(text: str) -> str:
    """The utterance with every spelling of the wake name folded to "puff"."""
    return _NAME_VARIANT_RE.sub("puff", str(text or ""))


def _greeting_reply(now: datetime) -> str:
    hour = now.hour
    if hour < 12:
        part = "Morning"
    elif hour < 18:
        part = "Afternoon"
    else:
        part = "Evening"
    return "%s! What can I do for you?" % part


def match_social(text: str, now=None) -> str | None:
    """A short reply for a purely social utterance, else ``None``.

    ``None`` means "not small talk" and the caller must carry on with normal
    routing. Every reply here is at most two sentences and cites nothing,
    because nothing was consulted.
    """
    raw = normalise_name((text or "").strip())
    if not raw:
        return None
    if len(raw.split()) > _MAX_WORDS or len(raw) > 100:
        return None
    if _NOT_SMALL_TALK_RE.search(raw):
        return None
    now = now or datetime.now()

    if _GREETING_RE.match(raw):
        return _greeting_reply(now)
    if _HOW_ARE_YOU_RE.match(raw):
        return ("Doing well, thanks — everything here is running normally. "
                "What do you need?")
    if _THANKS_RE.match(raw):
        return "You're welcome."
    if _LOVE_RE.match(raw):
        # Deterministic rotation rather than a random pick: the same minute
        # gives the same answer, so a test can pin one and a repeat within a
        # conversation still varies.
        return _LOVE_REPLIES[now.minute % len(_LOVE_REPLIES)]
    if _BYE_RE.match(raw):
        return "Talk to you later."
    if _ACK_RE.match(raw):
        return "Got it."
    if _SOCIAL_OPENER_RE.match(raw):
        # "fine I just got back from Seattle" — a statement, not a lookup.
        if "?" in raw or _TEMPORAL_RE.search(raw):
            return None
        rest = _SOCIAL_OPENER_RE.sub("", raw, count=1).strip(" ,.!-")
        if rest and _QUESTION_WORD_RE.search(rest):
            return None
        if rest:
            return "Good to hear. I'm here whenever you need something."
        return "Glad to hear it. What can I do for you?"
    return None
