"""E7b: a plain "yes" commits the meal plan, and "keep Sunday" is honoured.

"plan the rest of this week's meals, vegetarian"
asked "Proceed and overwrite?" — the gate held, correctly — and the answer
"Yes, overwrite, but keep Sunday dinner" was met with the SAME question again,
75 seconds later. Only the scripted sentence "yes, replace the plan" counted as
a confirmation, and a conditional confirm had no path at all.
"""

import pytest

from puffpai.agents import meal_plan_guard as guard


class FakePlanApp:
    """A FreshTable whose plan and shopping rebuild can be inspected."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.rebuilds = 0

    def _load(self, app_id):
        return {"mealPlan": list(self.rows)}

    def fresh_set_meal(self, day, meal, dish, notes=None, recipe_id=None):
        day = str(day).lower()
        self.rows = [r for r in self.rows
                     if not (str(r.get("day", "")).lower() == day
                             and r.get("meal") == meal)]
        self.rows.append({"day": day, "meal": meal, "dish": dish,
                          "notes": notes, "recipeId": recipe_id})
        return "ok"

    def fresh_grocery_from_plan(self):
        self.rebuilds += 1


SUNDAY = {"day": "sunday", "meal": "dinner", "dish": "salad and potato bar",
          "notes": None, "recipeId": None}


def _planner(app, fired):
    """A week planner that rewrites every day, exactly like auto_meal_plan."""

    def run():
        fired.append(True)
        app.rows = []
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday"):
            app.fresh_set_meal(day, "dinner", f"Generated {day}")
        app.fresh_grocery_from_plan()
        return "Dinner plan saved to FreshTable:\n  - Monday: Generated monday"

    return run


@pytest.fixture(autouse=True)
def _clean():
    guard.clear_pending()
    yield
    guard.clear_pending()


# ── a plain yes commits ──────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    "yes", "Yes.", "yeah", "yep", "sure", "ok", "okay", "confirm",
    "go ahead", "do it", "overwrite it", "Yes please", "replace it",
])
def test_a_plain_yes_executes_once_without_re_asking(answer):
    app = FakePlanApp([SUNDAY])
    fired = []
    guard.set_pending(1, "plan the rest of this week's meals")
    reply = guard.pending_reply(answer, _planner(app, fired), app)
    assert fired == [True], f"{answer!r} did not commit the pending plan"
    assert "Say 'yes" not in (reply or ""), "the question must not be re-asked"
    # T5 re-pinned this: a BARE yes fills the empty days and
    # keeps Sunday; only an answer that says "replace"/"overwrite" replaces.
    if guard._SAYS_REPLACE_RE.search(answer):
        assert "Replaced a 1-day plan" in reply
    else:
        assert "I left Sunday exactly as it was." in reply
        assert app.rows and any(r["day"] == "sunday"
                                and r["dish"] == "salad and potato bar"
                                for r in app.rows)


def test_the_scripted_sentence_still_works():
    app = FakePlanApp([SUNDAY])
    fired = []
    guard.set_pending(1, "plan this week's meals")
    reply = guard.pending_reply("yes, replace the plan", _planner(app, fired), app)
    assert fired == [True]
    assert "Replaced a 1-day plan" in reply


def test_a_second_yes_after_completion_does_not_re_run():
    app = FakePlanApp([SUNDAY])
    fired = []
    guard.set_pending(1, "plan this week's meals")
    guard.pending_reply("yes", _planner(app, fired), app)
    assert guard.get_pending() is None
    assert guard.pending_reply("yes", _planner(app, fired), app) is None
    assert fired == [True], "the week may never be planned twice on one confirm"


def test_a_decline_still_leaves_the_plan_alone():
    app = FakePlanApp([SUNDAY])
    fired = []
    guard.set_pending(1, "plan this week's meals")
    reply = guard.pending_reply("no, leave it alone", _planner(app, fired), app)
    assert fired == []
    assert reply == "Okay — I left your meal plan exactly as it was."


def test_a_command_for_another_capability_is_not_a_confirmation():
    app = FakePlanApp([SUNDAY])
    fired = []
    guard.set_pending(1, "plan this week's meals")
    assert guard.pending_reply("yes, turn on the kitchen lights",
                               _planner(app, fired), app) is None
    assert fired == []
    assert guard.get_pending() is not None, "the question keeps waiting"


def test_a_stale_confirmation_cannot_be_committed(monkeypatch):
    app = FakePlanApp([SUNDAY])
    fired = []
    guard.set_pending(1, "plan this week's meals")
    monkeypatch.setattr(guard, "PENDING_TTL", -1)
    assert guard.pending_reply("yes", _planner(app, fired), app) is None
    assert fired == []


def test_the_confirm_window_is_two_minutes():
    assert guard.PENDING_TTL == 120.0


# ── "keep Sunday" ────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,expected", [
    ("Yes, overwrite, but keep Sunday dinner", ["sunday"]),
    ("yes but keep sunday", ["sunday"]),
    ("yes, except Sunday", ["sunday"]),
    ("go ahead, leave Saturday alone", ["saturday"]),
    ("yes, keep Sunday and keep Monday", ["sunday", "monday"]),
    ("yes", []),
    ("yes, replace the plan", []),
])
def test_the_days_to_keep_are_read_off_the_confirmation(answer, expected):
    assert guard.keep_days(answer) == expected


def test_yes_but_keep_sunday_fills_the_rest_and_leaves_sunday():
    """The exact E7b sentence."""
    app = FakePlanApp([SUNDAY])
    fired = []
    guard.set_pending(1, "plan the rest of this week's meals, vegetarian")
    reply = guard.pending_reply("Yes, overwrite, but keep Sunday dinner",
                                _planner(app, fired), app)
    assert fired == [True]
    by_day = {r["day"]: r["dish"] for r in app.rows}
    assert by_day["sunday"] == "salad and potato bar", (
        "a day the household already decided must survive the week planner")
    assert by_day["monday"] == "Generated monday"
    assert len(by_day) == 7
    assert "I left Sunday exactly as it was." in reply
    assert app.rebuilds >= 2, (
        "the shopping list is derived from the plan and must be rebuilt after "
        "the kept day is restored")


def test_keeping_a_day_that_was_empty_says_so_instead_of_claiming_a_rescue():
    app = FakePlanApp([])
    fired = []
    guard.set_pending(1, "plan this week's meals")
    reply = guard.pending_reply("yes, keep Sunday", _planner(app, fired), app)
    assert fired == [True]
    assert "nothing to keep" in reply


def test_a_planner_that_comes_back_empty_restores_nothing():
    app = FakePlanApp([SUNDAY])

    def empty_run():
        return "The meal plan came back empty — try again."

    guard.set_pending(1, "plan this week's meals")
    reply = guard.pending_reply("yes, keep Sunday", empty_run, app)
    assert "came back empty" in reply
    assert "I left Sunday" not in reply
    assert app.rows == [SUNDAY]


# ── the turn is still recognised as an ANSWER ────────────────────────────

@pytest.mark.parametrize("answer", [
    "Yes, overwrite, but keep Sunday dinner",
    "yes, go ahead and overwrite it",
    "overwrite it",
])
def test_a_conditional_confirmation_is_an_answer_to_the_question(answer):
    assert guard.answers_pending(answer)
