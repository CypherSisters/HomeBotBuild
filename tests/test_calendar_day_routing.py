"""A day name is a calendar question, and it stays scoped.

Sent to knowledge/RAG, the single word "Saturday" comes back with trivia about
the word plus filenames and dates out of the household's own uploaded photo
library. Two things must hold at once: the
day name reaches the calendar reader, and titles that merely CONTAIN a day
name still decline.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.agents import household_reads  # noqa: E402


# ── the day name reaches the calendar  ──────────────────────────────

@pytest.mark.parametrize("text", [
    "Saturday",
    "saturday",
    "Monday",
    "on Tuesday",
    "what's on Saturday",
    "what is on Saturday",
    "what do we have on Saturday",
    "anything on Friday",
    "what's happening Saturday",
    "what is on the calendar tomorrow",
])
def test_a_day_name_reaches_the_calendar_reader(text):
    assert household_reads.match(text) == "calendar_upcoming"


#: Ordinary English carrying a day name that is NOT a calendar question. The
#: show title is the one the brief names; the rest are the same shape.
DECLINE_CORPUS = [
    "Saturday Night Live",
    "when is Saturday Night Live on",
    "play Saturday Night Live",
    "who hosted Saturday Night Live last week",
    "put on Sunday Bloody Sunday",
    "play Ruby Tuesday",
    "what movie should we watch on Saturday",
    "add an event on Saturday",
    "delete the event on Saturday",
    "remind me to call the dentist Monday",
    "what day is it",
    "how do you spell Wednesday",
]


@pytest.mark.parametrize("text", DECLINE_CORPUS)
def test_the_decline_corpus_still_declines(text):
    assert household_reads.match(text) != "calendar_upcoming"


# ── the reader answers about the window, and names it  ──────────────

class _FakeApp:
    """AppData-shaped double that records the window it was asked for."""

    def __init__(self, events=None):
        self._events = events or []
        self.calls = []

    def cal_list_events(self, days_ahead=7, start=None, end=None, label=None):
        self.calls.append({"days": days_ahead, "start": start, "end": end,
                           "label": label})
        if start is None:
            rows = self._events
        else:
            rows = [e for e in self._events if start <= e["when"] <= end]
        if not rows:
            return f"Nothing on {label}." if label else \
                f"No events in the next {days_ahead} days."
        listed = "\n".join(f"{e['when']:%b %d} - {e['title']}" for e in rows)
        return f"{len(rows)} events:\n{listed}"

    def cal_list_tasks(self, status="pending"):
        return f"No {status} tasks."


TOMORROW = datetime.now() + timedelta(days=1)
NEXT_WEEK = datetime.now() + timedelta(days=5)
CAL = _FakeApp([
    {"when": TOMORROW.replace(hour=10), "title": "fake tomorrow thing"},
    {"when": NEXT_WEEK.replace(hour=10), "title": "fake later thing"},
])


def test_tomorrow_answers_about_tomorrow_only():
    """A scoped reply must not be byte-identical to 'anything coming up'."""
    app = _FakeApp(CAL._events)
    scoped = household_reads.answer("what is on the calendar tomorrow", app=app)
    unscoped = household_reads.answer("any events or reminders coming up",
                                      app=_FakeApp(CAL._events))
    assert scoped != unscoped, "CAL-02: 'tomorrow' was not scoped"
    assert "fake tomorrow thing" in scoped
    assert "fake later thing" not in scoped
    assert app.calls[0]["label"] == "tomorrow"
    assert app.calls[0]["start"].date() == TOMORROW.date()


def test_an_empty_day_names_the_day():
    app = _FakeApp([])
    reply = household_reads.answer("what's on Saturday", app=app)
    assert "Saturday" in reply
    assert app.calls[0]["label"] == "Saturday"


def test_a_calendar_answer_only_ever_quotes_the_calendar():
    """The real harm: uploaded-photo metadata in a calendar answer."""
    app = _FakeApp(CAL._events)
    reply = household_reads.answer("Saturday", app=app)
    assert reply is not None
    for leak in (".jpg", ".jpeg", ".png", ".heic", ".pdf", "IMG_", "upload",
                 "wiktionary"):
        assert leak.lower() not in reply.lower()


def test_an_older_store_that_only_knows_days_ahead_still_answers():
    """The reader must not break on a store without the scoped signature."""

    class _OldApp:
        def cal_list_events(self, days_ahead=7):
            return "1 events:\nSep 06 - fake tomorrow thing"

        def cal_list_tasks(self, status="pending"):
            return f"No {status} tasks."

    reply = household_reads.answer("what is on the calendar tomorrow",
                                   app=_OldApp())
    assert "fake tomorrow thing" in reply
