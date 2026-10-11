"""A sentence that stops mid-phrase is asked back, never answered.

The case: "Every Monday, Tuesday, Thursday, and Friday morning at 6.15, I
have to take …" with the 12-second recorder cap closing the capture at
"take". The three-word fragment rule does not fire (the head is long and
grammatical, and ``is_fragment`` only knows the three-word dangling shape), so
without this guard the brain answers the piece it got, with "I'm not able to
set reminders", which is false, and an invented "your child … soccer".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import fragments  # noqa: E402

#: What the recorder hands over when the cap cuts the sentence.
CUT = ("Every Monday, Tuesday, Thursday, and Friday morning at 6.15, "
       "I have to take")


def test_the_cut_sentence_is_recognised():
    assert fragments.ends_mid_phrase(CUT)


def test_the_cut_sentence_is_asked_back_and_nothing_is_invented():
    reply = api._try_voice_fragment_guard(CUT)
    assert reply == ('I caught "…I have to take" but the end got cut off — '
                     'say it again?')
    for invented in ("child", "soccer", "reminder", "Tuesday at"):
        assert invented not in reply


@pytest.mark.parametrize("text", [
    "I need to go to the",
    "set a timer for ten minutes and",
    "put the milk in the fridge because",
    "we are going to the store at",
    "remind me about the appointment tomorrow...",
    "tell the kids they have to",
])
def test_other_mid_phrase_cuts_are_asked_back(text):
    assert fragments.ends_mid_phrase(text), text
    assert api._try_voice_fragment_guard(text) is not None


@pytest.mark.parametrize("text", [
    "turn the kitchen lights on",
    "what should I take for a headache",
    "play some jazz radio please",
    "add oat milk to the shopping list",
    "what is the weather like today",
    "do I need a jacket outside today",
    "how long is my workout streak",
    "set an alarm for seven",
    "stop",
    "yes please",
])
def test_a_whole_sentence_is_left_alone(text):
    assert not fragments.ends_mid_phrase(text), text


def test_the_three_word_fragment_rule_still_owns_its_shape():
    """"every Tuesday." keeps the older wording, which quotes the whole thing."""
    reply = api._try_voice_fragment_guard("every Tuesday.")
    assert reply is not None and "part of a sentence" in reply


def test_the_recorder_flag_is_believed_when_the_text_looks_complete():
    """If the voice service says the capture hit its cap, that settles it."""
    text = "every Tuesday morning I take the kids to soccer practice"
    assert not fragments.ends_mid_phrase(text)
    assert api._try_voice_fragment_guard(text) is None
    reply = api._try_voice_fragment_guard(text, truncated=True)
    assert reply is not None and "cut off" in reply


def test_the_ask_back_quotes_only_what_was_heard():
    reply = fragments.ask_again_cut(CUT)
    quoted = reply.split('"')[1]
    assert quoted.lstrip("…") in CUT


# "what radio sources do you have?" is a whole question, not a cut: a
# trailing auxiliary/modal is natural in a wh-question.
@pytest.mark.parametrize("text", [
    "what radio sources do you have?",
    "what movies do you have",
    "do you know where my keys are",
    "do you know what time it is",
    "how many lights do we have",
])
def test_a_wh_question_ending_in_an_auxiliary_is_not_a_cut(text):
    assert not fragments.ends_mid_phrase(text), text
    assert api._try_voice_fragment_guard(text) is None


@pytest.mark.parametrize("text", [
    CUT,
    "remind me tomorrow that I have",
    # A wh-word does not forgive a non-auxiliary tail.
    "tell me what is the",
    "what is the name of the",
    "I need to pick",
    "please add milk and",
])
def test_real_cuts_are_still_cuts_after_the_wh_rule(text):
    assert fragments.ends_mid_phrase(text), text
