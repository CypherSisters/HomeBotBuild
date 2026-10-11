"""`cal_list_day`: the manifest's `list_day` utterance, backed by a method.

`apps/calendar/app.json` advertises a `list_day` action; without it the only
calendar read would be `cal_list_events(days_ahead=7)`, a fixed week. This
file pins the real day-scoped action, its allowlisting, and the three exact manifest phrasings
reaching the calendar reader.

Real `AppData` on a temp data dir. Every row is invented.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


def _iso(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat()


# ── the action itself ───────────────────────────────────────────────────────

def test_the_method_exists_and_is_a_day_scoped_read(app):
    assert callable(getattr(app, "cal_list_day", None))


def test_an_empty_named_day_names_the_window_back(app):
    """A bare 'no events' is not enough; the reply must say the day."""
    saturday = datetime.now() + timedelta(
        days=(5 - datetime.now().weekday()) % 7 or 7)
    reply = app.cal_list_day("Saturday")
    assert reply.startswith("Nothing on Saturday")
    assert "next 7 days" not in reply
    assert saturday is not None


def test_an_empty_tomorrow_says_tomorrow(app):
    reply = app.cal_list_day("tomorrow")
    assert reply == "Nothing tomorrow."


def test_no_argument_means_today_not_the_next_seven_days(app):
    later_today = datetime.now().replace(hour=23, minute=0, second=0,
                                         microsecond=0)
    next_week = datetime.now() + timedelta(days=4)
    app.cal_add_event("test-fakeevent-today", _iso(later_today))
    app.cal_add_event("test-fakeevent-later", _iso(next_week))
    reply = app.cal_list_day()
    assert "test-fakeevent-today" in reply
    assert "test-fakeevent-later" not in reply, \
        "an unscoped week leaked into a one-day read"


def test_tomorrow_lists_only_tomorrow(app):
    tomorrow = (datetime.now() + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    day_after = tomorrow + timedelta(days=1)
    app.cal_add_event("test-fakeevent-tomorrow", _iso(tomorrow))
    app.cal_add_event("test-fakeevent-day-after", _iso(day_after))
    reply = app.cal_list_day("tomorrow")
    assert "test-fakeevent-tomorrow" in reply
    assert "test-fakeevent-day-after" not in reply
    assert "tomorrow" in reply


def test_a_recurring_event_is_expanded_into_the_named_day(app):
    """cal_list_day goes through expanded_occurrences, not the raw rows."""
    start = (datetime.now() - timedelta(days=14)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    app.cal_add_event("test-fakeevent-weekly", _iso(start),
                      recurrence="daily")
    reply = app.cal_list_day("tomorrow")
    assert "test-fakeevent-weekly" in reply, \
        "a recurring event vanished after its first occurrence (CAL-08)"


def test_an_iso_date_is_accepted(app):
    when = (datetime.now() + timedelta(days=3)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    app.cal_add_event("test-fakeevent-iso", _iso(when))
    reply = app.cal_list_day(when.date().isoformat())
    assert "test-fakeevent-iso" in reply


def test_an_unparseable_day_asks_instead_of_guessing(app):
    reply = app.cal_list_day("the twelfth of never")
    assert "couldn't tell which day" in reply
    assert "Nothing" not in reply


# ── the allowlist ───────────────────────────────────────────────────────────

def test_cal_list_day_is_allowlisted_to_the_calendar_app():
    from puffpai import api_server
    assert "cal_list_day" in api_server._APP_ACTIONS["calendar"]
    assert "cal_list_day" in api_server._ALLOWED_APP_ACTIONS
    for name, actions in api_server._APP_ACTIONS.items():
        if name != "calendar":
            assert "cal_list_day" not in actions


# ── the manifest's utterances reach the calendar reader ─────────────────────

@pytest.mark.parametrize("said", [
    "what is on my calendar tomorrow",     # the manifest's own `says`
    "what's on Saturday",
    "anything today",
    "anything tomorrow",
    "anything on the calendar today",
    "what do we have going on today",
    "what's happening tomorrow",
    "any plans this weekend",
])
def test_the_list_day_phrasings_reach_the_calendar_reader(said):
    from puffpai.agents import household_reads
    assert household_reads.match(said) == "calendar_upcoming", said


@pytest.mark.parametrize("said", [
    "what's the weather today",
    "any news today",
    "anything good on tv tonight",
    "anything to watch tonight",
    "whats for dinner tonight",
    "any bills due this week",
    "what medications do I have today",
    "add a dentist appointment tomorrow",
    "remind me to call mom tomorrow",
    "is it going to rain tomorrow",
])
def test_the_decline_corpus_is_not_a_calendar_read(said):
    from puffpai.agents import household_reads
    assert household_reads.match(said) != "calendar_upcoming", said


def test_anything_today_answers_from_the_calendar_and_names_the_day(app):
    from puffpai.agents import household_reads
    reply = household_reads.answer("anything today", app=app)
    assert reply is not None
    assert "today" in reply.lower()
    assert "next 7 days" not in reply


def test_a_scoped_answer_quotes_only_the_calendar(app):
    """A calendar reply may never carry upload/file metadata ."""
    later_today = datetime.now().replace(hour=23, minute=0, second=0,
                                         microsecond=0)
    app.cal_add_event("test-fakeevent-today", _iso(later_today))
    from puffpai.agents import household_reads
    reply = household_reads.answer("anything today", app=app) or ""
    for leak in (".jpg", ".png", "IMG_", "upload", "wiktionary"):
        assert leak.lower() not in reply.lower()
    raw = json.loads(Path(app._files["calendar"]).read_text(encoding="utf-8"))
    assert raw["events"], "the fixture never landed"
