"""The honest capability gap, and the far larger list of things it must NOT say.

The decline corpus comes first in this file because it came first in the
design. A detector that fires on "tell me I'm doing okay" with "I don't have a
way to do that yet" is worse than the model's own fumbling answer: it is a
household appliance responding to affection with a capability report. Fifteen of
the sentences below are chosen precisely because they LOOK like
requests; the rest cover knowledge, the house, media, the family and small talk.

The positives are the other half of honesty: an action-shaped request that no
capability claims and no tool binds, which the agent route has already declined.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.skills import gap                                 # noqa: E402


# ── the fifteen affection/small-talk sentences, verbatim ─────────────

AFFECTION_AND_SMALLTALK = [
    "tell me a joke",
    "check on the baby",
    "tell me I'm doing okay",
    "can you keep a secret?",
    "get me a glass of water",
    "tell me about your day",
    "check the mail",
    "can you help me think through the guest list?",
    "tell me the story about the hurricane again",
    "check my math on this",
    "can you look at this rash?",
    "tell me something good",
    "check if I already told you this",
    "get me out of this meeting",
    "tell me what you think of the name Fable.",
]

KNOWLEDGE = [
    "what is the average temperature in Texas in August",
    "who wrote the declaration of independence",
    "how do cicadas make that noise",
    "what does a red sky at night mean",
    "explain how a heat pump works",
    "when did the hurricane hit galveston",
    "what's a good substitute for buttermilk",
]

HOUSE = [
    "turn off the lights",
    "set the thermostat to seventy two",
    "is the front door locked",
    "what's the temperature in the living room",
    "lock the back door",
    "run the bedtime scene",
    "dim the kitchen lights",
]

MEDIA_AND_ROUTINE = [
    "play some jazz",
    "what's on my calendar tomorrow",
    "add milk to the shopping list",
    "set a timer for ten minutes",
    "what's the weather going to do",
    "start a trivia game",
    "what did I take my medication at",
    "pause the music",
]

SOCIAL = [
    "good morning",
    "how are you today",
    "thank you",
    "you're the best",
    "I had a rough day",
    "goodnight",
    "what should we name the dog",
    "do you ever get bored",
]

DECLINE_CORPUS = AFFECTION_AND_SMALLTALK + KNOWLEDGE + HOUSE + MEDIA_AND_ROUTINE + SOCIAL


# ── real gaps ───────────────────────────────────────────────────────

REAL_GAPS = [
    "make a list of nearby earthquakes",
    "make a list of the nearest gas prices",
    "make a list of today's rocket launches",
    "make a list of nearby wildfires",
    "make a list of the tide times",
    "make a list of the river levels",
    "make a list of today's magnitude four earthquakes",
    "make a list of the ferry times",
    "make a list of the bus times",
    "make a list of the surf height",
    "make a list of the moon phase",
    "make a list of the sunrise times",
    "make a list of the meteor showers",
    "make a list of the volcano alerts",
    "make a list of the fire danger ratings",
    "make a list of the road closures",
]


def test_the_decline_corpus_is_at_least_forty_sentences():
    assert len(DECLINE_CORPUS) >= 40, len(DECLINE_CORPUS)
    assert all(w in DECLINE_CORPUS for w in AFFECTION_AND_SMALLTALK)


@pytest.mark.parametrize("said", DECLINE_CORPUS)
def test_nothing_in_the_decline_corpus_produces_the_gap_sentence(said):
    assert gap.detect(said) is None, said
    assert gap.is_gap(said) is False, said


def test_at_least_fifteen_real_gaps_are_recognised():
    fired = [s for s in REAL_GAPS if gap.detect(s)]
    assert len(fired) >= 15, (len(fired), [s for s in REAL_GAPS
                                           if not gap.detect(s)])


@pytest.mark.parametrize("said", REAL_GAPS)
def test_a_real_gap_is_named_in_the_answer(said):
    honest = gap.detect(said)
    assert honest is not None, said
    assert honest.startswith("I don't have a way to ")
    assert honest.endswith(" yet.")
    assert said.rstrip(".?") in honest


# ── the gate: decline-only, and behind the route ────────────────────

def test_a_reply_that_is_not_a_refusal_is_left_alone():
    """The detector never replaces an ANSWER. It only ever rewrites a no."""
    said = REAL_GAPS[0]
    assert gap.detect(said, "Here are three earthquakes near you.") is None
    assert gap.detect(said, "I can't do that.") is not None


@pytest.mark.parametrize("reply", [
    "I can't do that.",
    "I'm unable to help with that.",
    "None of the provided functions can be used for that.",
    "I don't have a way to do that.",
    "That is not currently supported.",
    "I'm not equipped for that.",
])
def test_refusal_shapes_are_recognised(reply):
    assert gap.looks_like_decline(reply) is True


@pytest.mark.parametrize("reply", [
    "The living room is 71 degrees.",
    "Done -- the lights are off.",
    "There are three items on your shopping list.",
    "",
    None,
])
def test_answers_are_not_mistaken_for_refusals(reply):
    assert gap.looks_like_decline(reply) is False


def test_the_sentence_makes_no_offer_in_phase_one():
    """Phase 1 cannot write a skill, so it must not hint that it can."""
    honest = gap.sentence("make a list of nearby earthquakes")
    for promise in ("build", "want me to", "search the web", "write", "shall I"):
        assert promise not in honest.lower(), honest


def test_a_polite_wrapper_is_trimmed_out_of_the_quote():
    honest = gap.sentence("please make a list of nearby earthquakes")
    assert honest == "I don't have a way to make a list of nearby earthquakes yet."


def test_the_gate_fails_closed_when_the_registry_is_unavailable(monkeypatch):
    import puffpai.capability_registry as cr

    def _boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(cr, "tools_for_query", _boom)
    assert gap.is_gap(REAL_GAPS[0]) is False


def test_an_overlong_utterance_is_left_alone():
    assert gap.is_gap("make a list of " + "x " * 200) is False


def test_the_gate_is_the_single_action_shape_arm():
    """No "can you / tell me / check / get me" arm.

    Asserted structurally as well as behaviourally: those four openers are not
    part of the gate, so a sentence that is action-shaped ONLY because it starts
    with "can you" cannot reach the detector.
    """
    import inspect

    source = inspect.getsource(gap.is_gap)
    assert "_looks_like_action_request" in source
    assert "can you" not in source.lower()
    assert "tell me" not in source.lower()
