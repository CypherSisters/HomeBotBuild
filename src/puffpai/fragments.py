"""Is this utterance a CUT SENTENCE rather than a whole request?

``PUFF_SILENCE_HANG`` is 1.0 s, and people pause after a fronted time phrase.
The capture loop closes the utterance at the pause, so "every week." and
"every Tuesday." can be shipped to the brain as complete turns, and each would
get a confident encyclopedia answer ("Every Tuesday is simply a recurring day
of the week that falls once each seven days…") while the speaker has to repeat
the whole sentence.

One rule, two users:

* ``puffpai.voice.service`` calls it right after STT and re-opens the mic once
  for a bounded extra window, so the tail of the sentence joins the head before
  anything is sent.
* ``puffpai.api_server`` calls it as the last line of defence — STT can always
  hand over a fragment — and asks the sentence back instead of answering it.

Deliberately narrow, and deterministic: no model, no history, no guessing. A
fragment is three words or fewer, led by a dangling connective or a bare time
phrase, carrying no verb of its own, and not one of the real short commands.
"""

from __future__ import annotations

import re

#: Short utterances that are REAL commands and must never be treated as a cut
#: sentence. Every one of these is either owned by a fastpath or is a plain
#: yes/no, and each is a negative test in
#: ``tests/test_meals_voice_defects.py``.
SHORT_COMMANDS = frozenset({
    "stop", "next", "louder", "quieter", "yes", "no", "yeah", "nope",
    "pause", "resume", "play", "skip", "back", "mute", "unmute", "help",
    "again", "ok", "okay", "cancel", "snooze", "dismiss", "quiet", "repeat",
    "continue", "wait", "hello", "hi", "hey", "thanks", "thank you",
    "goodbye", "bye", "volume up", "volume down", "turn it up",
    "turn it down", "shut up", "never mind", "nevermind", "go ahead",
    "do it", "sounds good", "what time", "good morning", "good night",
    "lights on", "lights off", "yes please", "sure", "please",
})

#: A cut sentence starts with a dangling connective or a bare time phrase.
#: Prepositions that commonly START a real command ("on", "in", "to", "off",
#: "up", "down") are deliberately absent.
_LEAD_RE = re.compile(
    r"(?i)^(?:every|each|and|then|but|or|so|because|although|while|"
    r"at|about|around|with|for|during|before|after|until|plus|also|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"weekdays?|weekends?|tomorrow|tonight|today|mornings?|evenings?|"
    r"afternoons?|nights?)\b")

#: Any of these means the utterance carries its own predicate — a command or a
#: question, not a fragment.
_VERB_RE = re.compile(
    r"(?i)\b(?:is|are|was|were|am|be|do|does|did|can|could|will|would|should|"
    r"set|add|play|stop|turn|show|tell|call|remind|make|go|get|take|put|send|"
    r"start|open|close|lock|unlock|dim|read|find|search|order|buy|log|"
    r"what|what's|whats|when|where|who|why|how|which)\b")

#: Words a fragment may carry, at most this many.
MAX_WORDS = 3


def core_text(text: str) -> str:
    """The utterance without the punctuation STT hangs off the end."""
    return str(text or "").strip().strip(" \t.!?,;:").strip()


def is_fragment(text: str) -> bool:
    """True for an obviously cut-off utterance."""
    core = core_text(text)
    low = core.lower()
    if not low or low in SHORT_COMMANDS:
        return False
    if len(low.split()) > MAX_WORDS:
        return False
    if not _LEAD_RE.match(low):
        return False
    # A possessive turns the lead word into a MODIFIER of a real noun phrase:
    # "tomorrow's weather" is a whole question, not a cut sentence.
    if re.search(r"'s\b", low):
        return False
    return not _VERB_RE.search(low)


def ask_again(text: str) -> str:
    """The reply a fragment gets — short, and it repeats what was heard so the
    person knows exactly how much of the sentence landed."""
    return (f"I only caught \"{core_text(text)}\" — that sounded like part of "
            f"a sentence. Could you say the whole thing?")


#: A sentence that ends MID-PHRASE. For example, the
#: 12-second recorder cap cut "Every Monday, Tuesday, Thursday, and Friday
#: morning at 6.15, I have to take" and the brain answered the piece it got —
#: inventing details nobody had said. The
#: head of that sentence is long and grammatical, so ``is_fragment`` (three
#: words, dangling lead) cannot see it; what gives it away is the TAIL.
#:
#: Closed-class words only, plus the "to <transitive verb>" shape. Words that
#: legitimately END a command are deliberately absent: "on", "off", "up",
#: "down", "out", "it", "that", "do", "now".
_CUT_TAIL_RE = re.compile(
    r"(?i)(?:^|\s)(?:to|and|or|but|the|a|an|my|our|your|his|her|their|of|"
    r"with|from|into|about|at|for|because|is|are|was|were|am|be|been|"
    r"have|has|had|will|would|should|could|can|gonna|going|want|need|"
    r"every|each)$")

#: An auxiliary or modal at the tail is NATURAL in a wh-question: "what radio
#: sources do you have", "do you know where my keys are", "what time it is".
#: Without this, "what radio sources do you have?" is asked back as a cut.
#: Only these tail words are forgiven, and only when a wh-word comes earlier;
#: "what is the" still ends on "the" and is still a cut.
_AUX_TAIL_RE = re.compile(
    r"(?i)(?:^|\s)(?:is|are|was|were|am|be|been|have|has|had|will|would|"
    r"should|could|can)$")
_WH_RE = re.compile(r"(?i)\b(?:what|which|who|whom|whose|where|when|why|how)\b")

#: A verb that cannot end a sentence without an object. Only counted after
#: "to"/"have to"/"going to", and only in a LONG utterance, so "I'll take it"
#: and "what should I take" are untouched.
_CUT_TRANSITIVE_RE = re.compile(
    r"(?i)\b(?:to|gonna|wanna)\s+(?:take|put|bring|grab|pick|drop|give|"
    r"drive|feed|get|hand|carry|meet|text|email|show)$")

#: An explicit trailing ellipsis is STT saying the same thing.
_CUT_ELLIPSIS = ("...", "\u2026")

#: Below this many words a cut sentence is already ``is_fragment``'s job, and
#: short complete commands live here ("turn the lights on").
_CUT_MIN_WORDS = 4


def ends_mid_phrase(text: str) -> bool:
    """True when a long utterance stops in the middle of a phrase.

    Deterministic and conservative: a dangling closed-class word, a "to
    <transitive verb>" with nothing after it, or a trailing ellipsis. It never
    guesses the rest of the sentence — the only thing this licenses is asking.
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.endswith(_CUT_ELLIPSIS):
        return True
    core = core_text(raw)
    low = core.lower()
    if len(low.split()) < _CUT_MIN_WORDS or low in SHORT_COMMANDS:
        return False
    if _CUT_TRANSITIVE_RE.search(low):
        return True
    if not _CUT_TAIL_RE.search(low):
        return False
    aux = _AUX_TAIL_RE.search(low)
    if aux and _WH_RE.search(low[:aux.start()]):
        return False
    return True


def ask_again_cut(text: str, words: int = 4) -> str:
    """The reply a mid-phrase cut gets: the tail that landed, and a question.

    Quotes only what was actually heard. Nothing after the cut is invented —
    answering a cut sentence invents people and activities nobody named.
    """
    tail = " ".join(core_text(text).split()[-words:])
    return (f"I caught \"\u2026{tail}\" but the end got cut off — "
            f"say it again?")


def join_extension(head: str, tail: str) -> str:
    """Stitch a fragment to the tail captured by the extended listen."""
    head = str(head or "").strip().rstrip(" .,;:")
    tail = str(tail or "").strip()
    if not tail:
        return str(head or "").strip()
    if not head:
        return tail
    return f"{head} {tail}"
