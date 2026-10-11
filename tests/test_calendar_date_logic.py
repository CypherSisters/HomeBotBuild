"""The calendar's date logic.

Every phrasing here is one a person says out loud. The clock is faked to a
Saturday (2026-09-05), the day an off-by-one turns "Monday" into Sunday the
6th.

Fixtures are shaped like the real ``calendar.json`` (``family, events,
tasks, meds, bills, last_updated``) with invented rows only.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import calendar_dates as cd  # noqa: E402

# The fixed Saturday every test resolves from.
SATURDAY = datetime(2026, 9, 5, 12, 0)
WEEKDAYS = list(cd.WEEKDAY_NAMES)


# ── a weekday name resolves to that weekday ──────────────────────────

@pytest.mark.parametrize("from_offset", range(7))
@pytest.mark.parametrize("target", WEEKDAYS)
def test_every_weekday_from_every_weekday(target, from_offset):
    """The rule in one line: the stored date must LAND on the day said.

    Checked from all seven possible "todays" so an off-by-one cannot hide
    behind the day the suite happens to run on.
    """
    now = SATURDAY + timedelta(days=from_offset)
    got = cd.resolve_date(target, now=now)
    assert got is not None
    assert cd.WEEKDAY_NAMES[got.weekday()] == target
    assert got > now.date() or got == now.date()


@pytest.mark.parametrize("from_offset", range(7))
def test_a_weekday_is_never_in_the_past(from_offset):
    now = SATURDAY + timedelta(days=from_offset)
    for name in WEEKDAYS:
        assert cd.resolve_date(name, now=now) >= now.date()


def test_monday_said_on_saturday_is_monday_not_sunday():
    """'remind me to call the dentist Monday', said on a Saturday."""
    assert cd.resolve_date("Monday", now=SATURDAY).isoformat() == "2026-09-07"


def test_same_weekday_with_a_later_time_means_today():
    monday_9am = datetime(2026, 9, 7, 9, 0)
    assert cd.resolve_date("monday", now=monday_9am,
                           time_hint=(15, 0)).isoformat() == "2026-09-07"


def test_same_weekday_with_a_past_time_means_next_week():
    monday_4pm = datetime(2026, 9, 7, 16, 0)
    assert cd.resolve_date("monday", now=monday_4pm,
                           time_hint=(9, 0)).isoformat() == "2026-09-14"


def test_same_weekday_with_no_time_means_next_week():
    monday = datetime(2026, 9, 7, 9, 0)
    assert cd.resolve_date("monday", now=monday).isoformat() == "2026-09-14"


@pytest.mark.parametrize("phrase,expected", [
    ("tomorrow", "2026-09-06"),
    ("today", "2026-09-05"),
    ("this weekend", "2026-09-05"),     # said ON Saturday
    ("next friday", "2026-09-11"),
    ("in 3 days", "2026-09-08"),
    ("next week", "2026-09-12"),
    ("the day after tomorrow", "2026-09-07"),
])
def test_the_other_relative_phrases_resolve(phrase, expected):
    assert cd.resolve_date(phrase, now=SATURDAY).isoformat() == expected


def test_this_weekend_before_the_weekend_is_the_coming_saturday():
    wednesday = datetime(2026, 9, 2, 12, 0)
    assert cd.resolve_date("this weekend",
                           now=wednesday).isoformat() == "2026-09-05"


def test_a_non_date_stays_unresolved():
    assert cd.resolve_date("someday", now=SATURDAY) is None
    assert cd.resolve_date("", now=SATURDAY) is None


# ── the date said in the sentence is pulled out of the TITLE ──────────

def test_the_day_said_in_the_sentence_becomes_the_due_date():
    found = cd.extract_date_phrase("call the dentist Monday", now=SATURDAY)
    assert found["title"] == "call the dentist"
    assert found["date"] == "2026-09-07"


def test_a_time_in_the_sentence_survives_too():
    found = cd.extract_date_phrase("call the dentist on Monday at 3pm",
                                   now=SATURDAY)
    assert found["title"] == "call the dentist"
    assert found["iso"] == "2026-09-07T15:00"


def test_a_titleless_sentence_is_left_alone():
    found = cd.extract_date_phrase("do the dishes", now=SATURDAY)
    assert found["title"] == "do the dishes"
    assert found["date"] is None


def test_a_bare_time_means_today_or_tomorrow():
    """'remind me to take my medication at 8' — a time, no day."""
    found = cd.extract_date_phrase("take my medication at 8", now=SATURDAY)
    assert found["title"] == "take my medication"
    assert found["iso"] == "2026-09-06T08:00"    # 8am has passed at noon
    early = cd.extract_date_phrase("take my medication at 8",
                                   now=datetime(2026, 9, 5, 6, 0))
    assert early["iso"] == "2026-09-05T08:00"


# ── the window a question names ───────────────────────────────

@pytest.mark.parametrize("text,label,first,last", [
    ("what is on the calendar tomorrow", "tomorrow", "2026-09-06", "2026-09-06"),
    ("what's on my calendar today", "today", "2026-09-05", "2026-09-05"),
    ("Saturday", "Saturday", "2026-09-05", "2026-09-05"),
    ("what's on Tuesday", "Tuesday", "2026-09-08", "2026-09-08"),
    ("anything this weekend", "this weekend", "2026-09-05", "2026-09-06"),
    ("what's on the calendar this week", "this week", "2026-09-05", "2026-09-06"),
])
def test_day_scope_is_exact(text, label, first, last):
    scope = cd.day_scope(text, now=SATURDAY)
    assert scope is not None
    assert scope["label"] == label
    assert scope["start"].date().isoformat() == first
    assert scope["end"].date().isoformat() == last


def test_an_unqualified_question_has_no_scope():
    assert cd.day_scope("any events or reminders coming up",
                        now=SATURDAY) is None


# ── recurrence expansion ─────────────────────────────────────────────

def _event(title, start, rec=None, **extra):
    row = {"id": f"e-{title}", "title": title, "start": start, "cat": "family",
           "to": "all", "prio": "medium", "src": "puffpai", "rec": rec,
           "loc": None}
    row.update(extra)
    return row


WEEK = (datetime(2026, 9, 5, 0, 0), datetime(2026, 9, 12, 23, 59))


def test_a_weekly_event_from_a_month_ago_still_shows_up():
    """A weekly event dated a month back still lands in this week."""
    rows = cd.expanded_occurrences(
        [_event("fake-recurring", "2026-08-01T10:00", rec="weekly")], *WEEK)
    assert [w.isoformat() for w, _ in rows] == ["2026-09-05T10:00:00",
                                               "2026-09-12T10:00:00"]


def test_daily_and_weekday_recurrence():
    daily = cd.expanded_occurrences(
        [_event("fake-daily", "2026-09-01T07:00", rec="daily")], *WEEK)
    assert len(daily) == 8
    weekdays = cd.expanded_occurrences(
        [_event("fake-weekdays", "2026-09-01T07:00", rec="weekdays")], *WEEK)
    assert all(when.weekday() < 5 for when, _ in weekdays)
    assert len(weekdays) == 5      # Mon 7 through Fri 11, inside the window


def test_monthly_and_biweekly_and_yearly():
    monthly = cd.expanded_occurrences(
        [_event("fake-monthly", "2026-06-08T10:00", rec="monthly")], *WEEK)
    assert [w.isoformat() for w, _ in monthly] == ["2026-09-08T10:00:00"]
    biweekly = cd.expanded_occurrences(
        [_event("fake-biweekly", "2026-08-29T10:00", rec="biweekly")], *WEEK)
    assert [w.isoformat() for w, _ in biweekly] == ["2026-09-12T10:00:00"]
    yearly = cd.expanded_occurrences(
        [_event("fake-yearly", "2020-09-07T10:00", rec="yearly")], *WEEK)
    assert [w.isoformat() for w, _ in yearly] == ["2026-09-07T10:00:00"]


def test_a_one_off_event_is_not_multiplied():
    rows = cd.expanded_occurrences(
        [_event("fake-once", "2026-09-06T10:00")], *WEEK)
    assert len(rows) == 1


def test_expansion_never_runs_away_on_a_bad_row():
    rows = cd.expanded_occurrences(
        [_event("fake-broken", "not-a-date", rec="daily"),
         {"title": "no start"}, "not even a dict"], *WEEK)
    assert rows == []


def test_an_event_dated_after_the_window_is_skipped():
    rows = cd.expanded_occurrences(
        [_event("fake-later", "2026-10-01T10:00", rec="weekly")], *WEEK)
    assert rows == []
