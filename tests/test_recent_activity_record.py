""""what did you just do" reads the record, not a self-portrait.

Answered by the model, two minutes after a thermostat write and a
shopping-list add, it says "I didn't change anything... I can't directly
control devices or run scripts." Both halves false.
"""

import inspect

import pytest

from puffpai import chat_activity, turn_context
from puffpai.agents import recent_activity


@pytest.fixture(autouse=True)
def _clean():
    turn_context.reset()
    turn_context.begin("conv-1")
    yield
    turn_context.reset()


def _seed():
    turn_context.note_action("Living Room set to 74.")
    turn_context.note_action("Added oat milk to the shopping list. "
                             "That's 32 items on it now.")
    turn_context.note_action("Sunday dinner is set to salad and potato bar.")


# ── the trigger table ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "what did you just do",
    "What did you just do?",
    "what did you do",
    "what have you done",
    "did you change something",
    "did you change anything?",
    "what did you change",
    "what have you done in the last 10 minutes",
    "what have you been doing",
])
def test_every_phrasing_is_an_activity_question(text):
    assert recent_activity.is_activity_question(text)


@pytest.mark.parametrize("text", [
    "what can you do",
    "what do you do with my data",
    "what did I just do",
    "did you find anything in the library",
    "what time is it",
    "what did you find",
])
def test_declines_everything_else(text):
    assert not recent_activity.is_activity_question(text)
    assert recent_activity.handle(text, [{"text": "Living Room set to 74."}]) is None


# ── the answer is the real list ──────────────────────────────────────────

def test_it_lists_what_actually_happened():
    _seed()
    reply = recent_activity.handle(
        "what did you just do", turn_context.recent_actions("conv-1"))
    assert "Living Room set to 74." in reply
    assert "Added oat milk to the shopping list." in reply
    assert "Sunday dinner is set to salad and potato bar." in reply
    # The read-back tail of a confirmation is not a second action.
    assert "That's 32 items" not in reply
    # And never the false self-description.
    assert "can't" not in reply and "didn't change anything" not in reply


def test_with_nothing_recorded_it_says_so_without_denying_the_capability():
    reply = recent_activity.handle("did you change something", [])
    assert reply == recent_activity.NOTHING
    assert "can't directly control" not in reply
    assert "control devices" not in reply


def test_this_conversation_comes_first():
    turn_context.note_action("Kitchen light turned off.",
                             conversation_id="conv-other")
    turn_context.note_action("Living Room set to 74.")
    records = turn_context.recent_actions("conv-1")
    assert records[0]["text"] == "Living Room set to 74."
    assert any("Kitchen light" in r["text"] for r in records)


def test_repeat_confirmations_are_spoken_once():
    turn_context.note_action("Living Room set to 74.")
    turn_context.note_action("Living Room set to 74.")
    reply = recent_activity.reply_for(turn_context.recent_actions("conv-1"))
    assert reply.count("Living Room set to 74.") == 1


def test_only_the_last_five_are_spoken():
    for n in range(8):
        turn_context.note_action(f"Action {n}.")
    reply = recent_activity.reply_for(turn_context.recent_actions(
        "conv-1", limit=0))
    assert "Action 7." in reply and "Action 2." not in reply


def test_actions_outside_the_window_are_not_claimed():
    turn_context.note_action("Living Room set to 74.")
    turn_context._ACTIONS[0]["ts"] -= 1200
    assert turn_context.recent_actions("conv-1", window=600) == []


@pytest.mark.parametrize("text,expected", [
    ("what did you just do", 600.0),
    ("what have you done in the last 5 minutes", 300.0),
    ("what have you done in the last 30 minutes", 1800.0),
    ("what have you done in the last 90 minutes", 1800.0),
    ("what have you done in the last 2 hours", 1800.0),
])
def test_the_window_follows_the_question(text, expected):
    assert recent_activity.window_for(text) == expected


def test_a_longer_window_is_named_in_the_reply():
    _seed()
    reply = recent_activity.reply_for(
        turn_context.recent_actions("conv-1"), window=1800.0)
    assert reply.startswith("In the last 30 minutes:")


# ── the record is filled by the write fastpaths ─────────────────────────

def test_the_write_fastpaths_record_their_confirmation():
    probed = chat_activity.probe("_try_climate_set",
                                 lambda msg: "Living Room set to 74.")
    turn_context.begin("conv-1")
    assert probed("set the living room to 74") == "Living Room set to 74."
    assert [r["text"] for r in turn_context.recent_actions("conv-1")] == [
        "Living Room set to 74."]


def test_a_read_fastpath_records_nothing():
    probed = chat_activity.probe("_try_time_answer", lambda msg: "It's 2:39 PM.")
    turn_context.begin("conv-1")
    probed("what time is it")
    assert turn_context.recent_actions("conv-1") == []


def test_the_two_self_referential_fastpaths_never_become_the_last_answer():
    for name in ("_try_recent_activity", "_try_source_citation"):
        assert name in chat_activity.SELF_REFERENTIAL_FASTPATHS
    turn_context.begin("conv-1")
    turn_context.note_answer("rag", None, [{"title": "Water purification"}])
    probed = chat_activity.probe("_try_recent_activity", lambda msg: "In the...")
    probed("what did you just do")
    record = turn_context.last_answer("conv-1")
    assert record["kind"] == "rag", (
        "answering a question ABOUT the last turn must not overwrite it")


# ── the dispatcher wiring ───────────────────────────────────────────────

def test_it_runs_before_the_capability_blurb():
    from tests.test_instant_answer_precedence import _call_order

    from puffpai import api_server

    order = [name for _lineno, name in _call_order()]
    assert order.index("_try_recent_activity") < order.index("_try_capabilities")
    assert order.count("_try_recent_activity") == 1
    src = inspect.getsource(api_server._try_instant_answer)
    assert "_try_recent_activity" in src


# ── a question is not an action ──────────────────────────────────────────
#
# Recording every truthy return makes "what did you just do" recite "Which
# thermostat do you mean: Upstairs Bedroom, Living Room?" among the
# real writes. _try_climate_set is a WRITE fastpath whose prose is sometimes a
# clarifying question, which must not be recorded as a deed.

@pytest.mark.parametrize("sentence,kind", [
    ("Living Room set to 74.", "write"),
    ("Living Room set to 74. Bedroom is still at 73 — it didn't take. "
     "Want me to try again?", "write"),
    ("Added SAMPLE oat milk to the shopping list. That's 42 items now.",
     "write"),
    ("Took SAMPLE oat milk off the shopping list.", "write"),
    ("Which thermostat do you mean: Upstairs Bedroom, Living Room?",
     "ask"),
    ("Whose workout? More than one person is set up here.", "ask"),
    ("Which item should I take off the shopping list?", "ask"),
    ("I couldn't find a thermostat for 'kitchen'.", "ask"),
    ("I'd rather not clear the whole shopping list in one go — tell me "
     "which items to take off.", "ask"),
    ("SAMPLE nothing wasn't on the list.", "read"),
])
def test_outcome_kind_reads_the_reply_not_the_fastpath(sentence, kind):
    assert turn_context.outcome_kind(sentence) == kind


def test_a_clarifying_question_is_not_recited_as_something_done():
    turn_context.begin("conv-1")
    turn_context.note_action(
        "Which thermostat do you mean: Upstairs Bedroom, Living Room?")
    turn_context.note_action("Living Room set to 74.")
    texts = [r["text"] for r in turn_context.recent_actions("conv-1")]
    assert texts == ["Living Room set to 74."]
    reply = recent_activity.reply_for(turn_context.recent_actions("conv-1"))
    assert "Which thermostat" not in reply


def test_the_question_is_still_kept_in_the_record():
    """Filtered from "what did you just do", not erased from the account."""
    turn_context.begin("conv-1")
    turn_context.note_action("Whose workout? Tell me who and I'll mark it.")
    assert turn_context.recent_actions("conv-1") == []
    everything = turn_context.recent_actions("conv-1", kinds=None)
    assert [r["kind"] for r in everything] == ["ask"]


def test_a_turn_with_only_questions_says_nothing_happened():
    turn_context.begin("conv-1")
    turn_context.note_action("Which thermostat do you mean: A, B?")
    reply = recent_activity.reply_for(turn_context.recent_actions("conv-1"))
    assert "Which thermostat" not in (reply or "")


def test_an_older_record_with_no_kind_still_counts_as_a_write():
    turn_context.begin("conv-1")
    turn_context.note_action("Living Room set to 74.")
    turn_context._ACTIONS[-1].pop("kind")
    assert [r["text"] for r in turn_context.recent_actions("conv-1")] == [
        "Living Room set to 74."]
