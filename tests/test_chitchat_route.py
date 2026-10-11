"""Small talk must not ride the RAG route.

Sent down the RAG route, small talk comes back with citations:

    | "how's it going" | rag | 2 sources | "So, all's steady on this
    | | | | side-nothing urgent on
    | | | | the health front and no
    | | | | new alerts in the local
    | | | | environment."
    | "hey whats up" | rag | 1 source | "Hey! Just hanging out..."
    | "good morning" | rag | 0 sources | "Good morning! How can I
    | | | | help you today?"
    | "you doing okay today" | rag | 1 source | "I'm doing fine..."

Three of four attach knowledge-library citations to a greeting, and the
first is a retrieved-document non-sequitur. A greeting has no answer in an
encyclopedia, so it must not be sent to one.

The lookalike corpus is the other half of the contract: a greeting-shaped
utterance that is really a request keeps its own path.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import chitchat  # noqa: E402

#: The four above, plus "fine I just got back from Seattle", plus five more a
#: household actually says.
SOCIAL = [
    "how's it going",
    "hey whats up",
    "good morning",
    "you doing okay today",
    "fine I just got back from Seattle",
    "hey there",
    "thanks",
    "never mind",
    "good night",
    "how have you been",
]

#: Greeting-shaped, but really a request. Every one must fall through.
LOOKALIKES = [
    "how's the weather going",
    "what's going on tomorrow",
    "good morning, what's on the calendar",
    "how's my workout streak",
    "what's up with the kitchen lights",
    "how are the bills looking this week",
]

#: The live non-sequitur. Nothing here may produce it.
NON_SEQUITUR = ("health front", "local environment", "no new alerts")


@pytest.mark.parametrize("utterance", SOCIAL)
def test_social_utterance_gets_a_short_local_reply(utterance):
    reply = chitchat.match_social(utterance)
    assert reply, f"{utterance!r} was not recognised as small talk"
    # Two sentences at most, and short enough to speak.
    assert reply.count(".") + reply.count("!") + reply.count("?") <= 2
    assert len(reply) <= 120
    low = reply.lower()
    for leak in NON_SEQUITUR:
        assert leak not in low


@pytest.mark.parametrize("utterance", SOCIAL)
def test_social_utterance_is_answered_before_rag_with_no_sources(utterance):
    """The instant-answer chain returns it, so ``sources`` is [] by
    construction — that path never consults a knowledge library."""
    assert api._try_chitchat(utterance)
    assert api._try_instant_answer(utterance)


@pytest.mark.parametrize("utterance", LOOKALIKES)
def test_lookalikes_are_not_claimed_by_the_social_route(utterance):
    assert chitchat.match_social(utterance) is None, \
        f"{utterance!r} was swallowed by small talk"


@pytest.mark.parametrize("utterance", [
    "how far is the moon from earth",
    "who wrote Romeo and Juliet",
    "whats the boiling point of water at sea level",
])
def test_factual_questions_still_fall_through(utterance):
    assert chitchat.match_social(utterance) is None


def test_greeting_reply_follows_the_clock():
    morning = chitchat.match_social("hey", now=datetime(2026, 9, 9, 8, 0))
    evening = chitchat.match_social("hey", now=datetime(2026, 9, 9, 20, 0))
    assert morning.startswith("Morning")
    assert evening.startswith("Evening")


def test_a_greeting_with_a_question_welded_on_is_not_small_talk():
    assert chitchat.match_social("hi, how many dragons live in Westeros") is None


def test_empty_and_long_utterances_are_declined():
    assert chitchat.match_social("") is None
    assert chitchat.match_social("hey " + "words " * 40) is None
