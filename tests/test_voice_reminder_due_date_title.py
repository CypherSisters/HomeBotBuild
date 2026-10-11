"""The due date and the title of a spoken reminder.

At 2:51 PM:

* "add another reminder to do the dishes today before nine" must store a task
  titled "do the dishes" due today at nine. If "before nine" is not
  recognised as naming a time, the time words stay in the title and the task
  falls to the tomorrow default, although the person said "today".
* "set a calendar reminder every weekday morning at 615 soccer practice" must
  create the recurring event titled "Soccer practice", not "Set a calendar
  reminder soccer practice": the command verb must not leak into the title.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import calendar_dates as cd  # noqa: E402
from puffpai import calendar_nl  # noqa: E402
import puffpai.api_server as api  # noqa: E402

#: The clock when the reminder is spoken.
NOW = datetime(2026, 9, 13, 14, 51)


# ── the due date ────────────────────────────────────────────────────────────

def test_today_before_nine_is_nine_tonight():
    found = cd.extract_date_phrase("do the dishes today before nine", now=NOW)
    assert found["title"] == "do the dishes"
    assert found["date"] == "2026-09-13"
    assert found["time"] == (21, 0)
    assert found["iso"] == "2026-09-13T21:00"


def test_tonight_before_nine_is_also_tonight():
    found = cd.extract_date_phrase("do the dishes tonight before nine", now=NOW)
    assert found["title"] == "do the dishes"
    assert found["iso"] == "2026-09-13T21:00"


def test_tomorrow_at_nine_is_nine_in_the_morning():
    found = cd.extract_date_phrase("call the vet tomorrow at nine", now=NOW)
    assert found["title"] == "call the vet"
    assert found["iso"] == "2026-09-14T09:00"


def test_an_hour_still_ahead_today_is_not_moved():
    found = cd.extract_date_phrase("start the laundry today at four", now=NOW)
    assert found["iso"] == "2026-09-13T16:00"


def test_a_stated_half_of_the_day_is_never_second_guessed():
    assert cd.extract_date_phrase("do the dishes today at 9am",
                                  now=NOW)["iso"] == "2026-09-13T09:00"
    assert cd.extract_date_phrase("do the dishes today at 9 pm",
                                  now=NOW)["iso"] == "2026-09-13T21:00"


def test_a_day_other_than_today_keeps_the_hour_it_was_given():
    assert cd.extract_date_phrase("call the dentist Monday at 9",
                                  now=NOW)["iso"] == "2026-09-14T09:00"


@pytest.mark.parametrize("text,expected", [
    ("pick up the dry cleaning today by five", "2026-09-13T17:00"),
    ("pack the bags today around eight", "2026-09-13T20:00"),
    ("call grandma today at six thirty", "2026-09-13T18:30"),
])
def test_the_other_spoken_prepositions_and_compound_times(text, expected):
    assert cd.extract_date_phrase(text, now=NOW)["iso"] == expected


# ── the whole fastpath, on the dishes sentence ──────────────────────────────

class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture()
def task_payload(monkeypatch):
    payloads = []

    class _Tool:
        def invoke(self, kwargs):
            payloads.append(kwargs)
            return "Added a task: %s." % kwargs.get("title")

    class _Tools:
        tool_dict = {"app_add_task": _Tool()}

    monkeypatch.setattr(cd, "datetime", _FrozenDatetime)
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    return payloads


def test_the_dishes_before_nine_sentence(task_payload):
    reply = api._try_add_task("add another reminder to do the dishes "
                              "today before nine")
    assert reply is not None
    payload = task_payload[-1]
    assert payload["title"] == "do the dishes"
    assert payload["due_date"] == "2026-09-13T21:00"


def test_the_sisters_reminder_still_lands_on_today(task_payload):
    api._try_add_task("put a reminder that the sisters are coming over today")
    payload = task_payload[-1]
    assert "today" not in payload["title"].lower()
    assert payload["due_date"].startswith("2026-09-13")


# ── the bare hour, at every hour of the clock ─────────────
#
# Near 1 AM, "add a reminder to do the dishes today before nine" could
# silently mean 9 AM; at 2:51 PM the same sentence could roll to tomorrow.
# One problem, an hour with no half of the day named, resolved silently, and
# the honest answer depends on the clock, so the clock is the parameter.

AFTERNOON = datetime(2026, 9, 17, 14, 0)
EVENING = datetime(2026, 9, 17, 20, 30)
SMALL_HOURS = datetime(2026, 9, 17, 0, 55)
JUST_BEFORE = datetime(2026, 9, 17, 8, 10)


@pytest.mark.parametrize("now,expected", [
    (AFTERNOON, "2026-09-17T21:00"),
    (EVENING, "2026-09-17T21:00"),
])
def test_a_bare_hour_deadline_lands_this_evening_not_tomorrow(now, expected):
    found = cd.extract_date_phrase("do the dishes today before nine", now=now)
    assert found["iso"] == expected
    assert found["ask_half"] is False
    assert not found["iso"].startswith("2026-09-18"), (
        "the person said TODAY; rolling to tomorrow discards their own word")


def test_in_the_small_hours_both_readings_are_live_so_it_asks():
    found = cd.extract_date_phrase("do the dishes today before nine",
                                   now=SMALL_HOURS)
    assert found["ask_half"] is True
    assert found["time"] == (9, 0)


def test_an_hour_minutes_away_is_not_worth_asking_about():
    """08:10, "before nine" — one reading, fifty minutes out."""
    found = cd.extract_date_phrase("do the dishes today before nine",
                                   now=JUST_BEFORE)
    assert found["ask_half"] is False
    assert found["iso"] == "2026-09-17T09:00"


@pytest.mark.parametrize("text,expected", [
    ("do the dishes tonight by 9", "2026-09-17T21:00"),
    ("do the dishes this evening by 9", "2026-09-17T21:00"),
    ("do the dishes this morning by 9", "2026-09-17T09:00"),
])
def test_a_daypart_in_the_words_settles_it_without_asking(text, expected):
    found = cd.extract_date_phrase(text, now=SMALL_HOURS)
    assert found["iso"] == expected
    assert found["ask_half"] is False


@pytest.mark.parametrize("text,expected", [
    ("take my pills this morning at 5", "2026-09-17T05:00"),
    ("take my pills this morning at 12", "2026-09-17T12:00"),
])
def test_the_small_hours_with_morning_said_stay_in_the_morning(text, expected):
    found = cd.extract_date_phrase(text, now=AFTERNOON)
    assert found["iso"] == expected
    assert found["ask_half"] is False


def test_a_stated_half_is_still_never_second_guessed_or_asked_about():
    for text, expected in (("do the dishes today at 9am", "2026-09-17T09:00"),
                           ("do the dishes today at 9 pm", "2026-09-17T21:00"),
                           ("do the dishes today at noon", "2026-09-17T12:00")):
        found = cd.extract_date_phrase(text, now=SMALL_HOURS)
        assert found["iso"] == expected and found["ask_half"] is False


def test_a_day_other_than_today_is_never_moved_and_never_asked_about():
    found = cd.extract_date_phrase("call the dentist Monday at 9",
                                   now=SMALL_HOURS)
    assert found["ask_half"] is False
    assert found["iso"].endswith("T09:00")


def test_the_recurring_event_parser_is_untouched():
    """"every weekday at 615" belongs to calendar_nl and stays there."""
    request = calendar_nl.parse_recurring_request(
        "set up a reminder every weekday at 615 to take the kids to practice")
    assert request is not None
    assert (request.hour, request.minute) == (6, 15)


# ── the fastpath ASKS instead of guessing, and the answer writes the row ────

class _SmallHours(datetime):
    @classmethod
    def now(cls, tz=None):
        return SMALL_HOURS


def test_the_dishes_sentence_at_one_am_asks_and_writes_nothing(
        task_payload, monkeypatch):
    from puffpai import pending_clarifier as pc
    from puffpai import turn_context

    monkeypatch.setattr(cd, "datetime", _SmallHours)
    pc.reset()
    turn_context.begin("conv-59")

    asked = api._try_add_task("add another reminder to do the dishes "
                              "today before nine")
    assert asked and "9 AM or 9 PM" in asked
    assert task_payload == [], "an ambiguous hour must write nothing"

    reply = api._try_instant_answer("PM", conversation_id="conv-59")
    assert reply is not None
    assert task_payload[-1] == {"title": "do the dishes",
                                "due_date": "2026-09-17T21:00"}


def test_answering_am_writes_the_morning_reading_of_the_same_task(
        task_payload, monkeypatch):
    from puffpai import pending_clarifier as pc
    from puffpai import turn_context

    monkeypatch.setattr(cd, "datetime", _SmallHours)
    pc.reset()
    turn_context.begin("conv-59-am")

    api._try_add_task("add a reminder to do the dishes today before nine")
    api._try_instant_answer("AM", conversation_id="conv-59-am")
    assert task_payload[-1] == {"title": "do the dishes",
                                "due_date": "2026-09-17T09:00"}


def test_a_change_of_subject_after_the_question_writes_no_task(
        task_payload, monkeypatch):
    from puffpai import pending_clarifier as pc
    from puffpai import turn_context

    monkeypatch.setattr(cd, "datetime", _SmallHours)
    pc.reset()
    turn_context.begin("conv-59-drop")
    api._try_add_task("add a reminder to do the dishes today before nine")
    assert api._try_pending_clarifier("never mind, what time is it",
                                      "conv-59-drop") is None
    assert task_payload == []
    assert pc.peek("conv-59-drop") is None


# ── the event title ─────────────────────────────────────────────────────────

def test_the_command_verb_does_not_leak_into_an_event_title():
    request = calendar_nl.parse_recurring_request(
        "set a calendar reminder every weekday morning at 615 "
        "soccer practice")
    assert request is not None
    assert request.title == "Soccer practice"
    assert request.recurrence == "weekdays"
    assert (request.hour, request.minute) == (6, 15)


@pytest.mark.parametrize("text,expected", [
    ("put the trash out every Tuesday at 7pm", "Put the trash out"),
    ("add a recurring event every Monday at 6pm book club", "Book club"),
    ("set up a reminder every weekday at 6:15 am to take the kids to "
     "soccer practice", "Take the kids to soccer practice"),
])
def test_the_titles_that_already_worked_are_unchanged(text, expected):
    request = calendar_nl.parse_recurring_request(text)
    assert request is not None and request.title == expected
