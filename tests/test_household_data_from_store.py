"""Household answers must come from the store.

The fixtures below mirror the shapes of the real ``app_data`` documents
(``freshtable.json``, ``calendar.json``) as the running API reads them: key
names and value shapes only, no household values.

The running API reads ``$PUFFPAI_DATA_DIR/app_data``
(``/data/puffpai/state/app_data`` on the appliance, the same store
``GET /apps/freshtable/data`` serves), not ``/opt/puffpai/app_data/``, so that
is the store every answer here must come from. The meal answer is the one a
model will otherwise fabricate, and that is what these tests hold hardest.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.agents import household_reads  # noqa: E402


class _FakeApp:
    """AppData-shaped double over in-memory documents."""

    def __init__(self, freshtable=None, calendar=None):
        self._fresh = freshtable or {}
        self._cal = calendar or {}

    # freshtable
    def fresh_get_meal_plan(self):
        return list(self._fresh.get("mealPlan", []))

    # calendar
    def cal_list_meds(self):
        meds = self._cal.get("meds", [])
        if not meds:
            return "No medications tracked."
        return f"{len(meds)} medications:\n" + "\n".join(
            f"{m['name']} ({m['dose']}) - {m['freq']} for {m['to']}, "
            f"next: {m['next']}" for m in meds)

    def cal_list_bills(self, status="pending"):
        bills = [b for b in self._cal.get("bills", [])
                 if b.get("status") == status]
        if not bills:
            return f"No {status} bills."
        total = sum(b.get("amt", 0) for b in bills)
        return (f"{len(bills)} {status} bills (total ${total:.2f}):\n"
                + "\n".join(f"{b['name']}: ${b['amt']:.2f}" for b in bills))

    def cal_list_events(self, days_ahead=7):
        events = self._cal.get("events", [])
        if not events:
            return f"No events in the next {days_ahead} days."
        return f"{len(events)} events:\n" + "\n".join(
            f"{e['start']} - {e['title']} ({e.get('cat', 'other')})"
            for e in events)

    def cal_list_tasks(self, status="pending"):
        tasks = [t for t in self._cal.get("tasks", [])
                 if t.get("status") == status]
        if not tasks:
            return f"No {status} tasks."
        return f"{len(tasks)} {status} tasks:\n" + "\n".join(
            t["title"] for t in tasks)


# A real mealPlan carries one entry per day: {id, day, meal, dish}.
PLANNED_WEEK = {
    "pantry": [], "shoppingList": [], "recipes": [],
    "mealPlan": [
        {"id": "id_1", "day": "monday", "meal": "dinner", "dish": "Dish One"},
        {"id": "id_2", "day": "thursday", "meal": "dinner", "dish": "Dish Four"},
    ],
}

A_THURSDAY = datetime(2026, 9, 3, 18, 0)
A_TUESDAY = datetime(2026, 9, 1, 18, 0)


# ── "whats for dinner tonight" reads the plan, never invents a dish ────

def test_dinner_tonight_is_the_dish_the_plan_records():
    answer = household_reads.meal_today(app=_FakeApp(PLANNED_WEEK),
                                        now=A_THURSDAY)
    assert "Dish Four" in answer
    assert "Thursday" in answer


def test_dinner_tonight_never_invents_a_dish_for_an_unplanned_day():
    answer = household_reads.meal_today(app=_FakeApp(PLANNED_WEEK),
                                        now=A_TUESDAY)
    assert "Nothing recorded yet" in answer
    assert "Dish One" not in answer and "Dish Four" not in answer


def test_dinner_tonight_on_an_empty_plan_says_nothing_recorded():
    empty = dict(PLANNED_WEEK, mealPlan=[])
    answer = household_reads.meal_today(app=_FakeApp(empty), now=A_THURSDAY)
    assert "Nothing recorded yet" in answer


def test_dinner_reader_reports_the_other_meals_it_does_have():
    lunch_only = dict(PLANNED_WEEK, mealPlan=[
        {"id": "id_9", "day": "thursday", "meal": "lunch", "dish": "Dish Nine"}])
    answer = household_reads.meal_today(app=_FakeApp(lunch_only), now=A_THURSDAY)
    assert "no dinner planned" in answer.lower()
    assert "Dish Nine" in answer


@pytest.mark.parametrize("text", [
    "whats for dinner tonight",
    "what's for dinner tonight",
    "what is for dinner",
    "what meal is planned for today",
    "what are we having for dinner",
])
def test_dinner_phrasings_reach_the_meal_reader(text):
    assert household_reads.match(text) == "meal_today"


@pytest.mark.parametrize("text", [
    "plan my meals for the week",       # a WRITE — keeps _try_meal_plan
    "plan our dinners for next week",
    "make me a recipe for dinner",
    "order groceries",
    "add chicken to the shopping list",
])
def test_meal_reader_leaves_the_write_paths_alone(text):
    assert household_reads.match(text) is None


# ── medications (the honesty half) ──────────────────────────────────

def test_medication_answer_is_honest_when_nothing_is_tracked():
    answer = household_reads.medication_status(app=_FakeApp(calendar={"meds": []}))
    assert "Nothing recorded yet" in answer
    # It must not be read as "you did not take it". A health question deserves
    # the distinction spelled out.
    assert "don't treat that as confirmation" in answer


def test_medication_answer_reports_what_is_tracked():
    cal = {"meds": [{"name": "Vitamin D", "dose": "1000 IU", "freq": "daily",
                     "to": "all", "next": "2026-09-03"}]}
    answer = household_reads.medication_status(app=_FakeApp(calendar=cal))
    assert "Vitamin D" in answer
    assert "only report what has been logged" in answer


def test_medication_answer_never_mentions_a_media_server():
    """A media-status misroute would return jellyfin_url, calibre_web_url."""
    answer = household_reads.medication_status(app=_FakeApp(calendar={"meds": []}))
    for word in ("jellyfin", "calibre", "media", "http"):
        assert word not in answer.lower()


# ── bills (the honesty half) ────────────────────────────────────────

def test_bills_answer_is_honest_when_nothing_is_owed():
    answer = household_reads.bills_status(app=_FakeApp(calendar={"bills": []}))
    # "Nothing recorded yet" alone is a shrug, and a shrug is the shape a
    # model rewrites into "I don't have a way to pull up your bills". So the
    # miss names the store it just read.
    assert "No unpaid bills recorded" in answer
    assert "read your bill records" in answer
    assert "nothing is owed" in answer


def test_bills_answer_reports_real_pending_bills():
    cal = {"bills": [{"name": "Electric", "amt": 142.5, "status": "pending",
                      "due": "2026-09-15"}]}
    answer = household_reads.bills_status(app=_FakeApp(calendar=cal))
    assert "Electric" in answer and "142.50" in answer


def test_bills_answer_never_lists_home_assistant_entities():
    """A device-status misroute would return automation.*/switch.* matches."""
    answer = household_reads.bills_status(app=_FakeApp(calendar={"bills": []}))
    for token in ("automation.", "switch.", "sensor.", "entity"):
        assert token not in answer.lower()


# ── calendar  ─────────────────────────────────────────────────────

CAL = {
    "events": [{"id": "e1", "title": "Event One", "cat": "sports",
                "start": "2026-09-05T09:15:00", "end": "2026-09-05T10:15:00",
                "to": "all", "prio": "medium", "loc": "Somewhere"}],
    "tasks": [{"id": "t1", "title": "Task One", "cat": "other",
               "due": "2026-08-31", "to": "all", "prio": "medium",
               "status": "pending"}],
    "meds": [], "bills": [],
}


def test_calendar_answer_lists_real_events_and_pending_reminders():
    answer = household_reads.calendar_upcoming(app=_FakeApp(calendar=CAL))
    assert "Event One" in answer
    assert "Task One" in answer


def test_calendar_answer_is_honest_when_the_calendar_is_empty():
    empty = {"events": [], "tasks": [], "meds": [], "bills": []}
    answer = household_reads.calendar_upcoming(app=_FakeApp(calendar=empty))
    assert "Nothing recorded yet" in answer


def test_calendar_answer_never_dumps_the_device_inventory():
    """A device-status misroute would dump every Home Assistant entity."""
    answer = household_reads.calendar_upcoming(app=_FakeApp(calendar=CAL))
    for token in ("media_player.", "binary_sensor.", "script.", "button.",
                  "device(s)"):
        assert token not in answer


def test_a_today_question_narrows_to_one_day():
    seen = {}

    class _Recording(_FakeApp):
        def cal_list_events(self, days_ahead=7):
            seen["days"] = days_ahead
            return super().cal_list_events(days_ahead)

    household_reads.answer("what is on our calendar today",
                           app=_Recording(calendar=CAL))
    assert seen["days"] == 1
    seen.clear()
    household_reads.answer("do we have any events or reminders coming up",
                           app=_Recording(calendar=CAL))
    assert seen["days"] == 7


# ── every reader degrades honestly when its store is unreadable ──────────────

class _Broken:
    def __getattr__(self, name):
        def _boom(*a, **k):
            raise OSError("app_data unreadable")
        return _boom


@pytest.mark.parametrize("reader", [
    household_reads.medication_status,
    household_reads.bills_status,
    household_reads.calendar_upcoming,
    household_reads.meal_today,
])
def test_a_broken_store_produces_an_admission_not_an_answer(reader):
    answer = reader(app=_Broken())
    assert "couldn't" in answer.lower() or "could not" in answer.lower()
