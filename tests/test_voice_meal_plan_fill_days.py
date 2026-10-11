"""T5: a confirmed "yes" fills the EMPTY days, with the constraint that was asked.

Live: "plan the rest of this week's meals, vegetarian"
→ "yes" rewrote all seven days — Sunday's "salad and potato bar" became "Beef
and Potato Shepherd's Pie" — and three of the seven dishes were meat, because
the confirmation turn called the planner with no dietary preference at all.
"""

import pytest

from puffpai.agents import meal_plan_guard as guard

WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday")


class FakePlanApp:
    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]
        self.rebuilds = 0

    def _load(self, app_id):
        return {"mealPlan": [dict(r) for r in self.rows]}

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


def _planner(app, seen):
    """auto_meal_plan's contract: it honours `preserve` and `constraint`."""

    def run(preserve=None, constraint=""):
        seen.append({"preserve": list(preserve or []),
                     "constraint": constraint})
        for day in WEEK:
            if day in set(preserve or []):
                continue
            app.fresh_set_meal(day, "dinner", f"Generated {day}")
        app.fresh_grocery_from_plan()
        return ("Dinner plan saved to FreshTable:\n  - Monday: "
                "Generated monday")

    return run


@pytest.fixture(autouse=True)
def _clean():
    guard.clear_pending()
    yield
    guard.clear_pending()


# ── the failure itself ───────────────────────────────────────────────────

def test_a_plain_yes_plans_only_the_empty_days():
    app = FakePlanApp([SUNDAY])
    seen = []
    guard.set_pending(1, "plan the rest of this week's meals, vegetarian")
    reply = guard.pending_reply("yes", _planner(app, seen), app)
    assert seen[0]["preserve"] == ["sunday"], "Sunday was already decided"
    sunday = [r for r in app.rows if r["day"] == "sunday"]
    assert sunday and sunday[0]["dish"] == "salad and potato bar"
    assert len(app.rows) == 7
    assert "Replaced" not in reply
    assert "Planned Monday, Tuesday, Wednesday, Thursday, Friday and Saturday" \
        in reply
    assert "I left Sunday exactly as it was." in reply


def test_the_constraint_from_the_original_ask_reaches_the_planner():
    app = FakePlanApp([SUNDAY])
    seen = []
    guard.set_pending(1, "plan the rest of this week's meals, vegetarian")
    guard.pending_reply("yes", _planner(app, seen), app)
    assert seen[0]["constraint"] == "vegetarian"


def test_a_constraint_added_in_the_confirmation_comes_too():
    app = FakePlanApp([SUNDAY])
    seen = []
    guard.set_pending(1, "plan this week's meals")
    guard.pending_reply("yes, gluten free please", _planner(app, seen), app)
    assert seen[0]["constraint"] == "gluten free"


def test_an_empty_week_plans_all_seven_days():
    app = FakePlanApp([])
    seen = []
    reply = guard.plan_reply("plan this week's meals, vegetarian",
                             _planner(app, seen), app)
    assert seen[0]["preserve"] == []
    assert seen[0]["constraint"] == "vegetarian"
    assert sorted({r["day"] for r in app.rows}) == sorted(WEEK)
    assert "nothing was replaced" in reply


def test_a_partly_full_week_keeps_every_day_it_already_had():
    app = FakePlanApp([SUNDAY,
                       {"day": "wednesday", "meal": "dinner",
                        "dish": "leftovers", "notes": None, "recipeId": None}])
    seen = []
    guard.set_pending(2, "plan the rest of this week's meals")
    reply = guard.pending_reply("go ahead", _planner(app, seen), app)
    assert seen[0]["preserve"] == ["wednesday", "sunday"]
    dishes = {r["day"]: r["dish"] for r in app.rows}
    assert dishes["sunday"] == "salad and potato bar"
    assert dishes["wednesday"] == "leftovers"
    assert "I left Wednesday and Sunday exactly as it was." in reply


def test_saying_replace_still_replaces_the_whole_week():
    app = FakePlanApp([SUNDAY])
    seen = []
    guard.set_pending(1, "plan this week's meals")
    reply = guard.pending_reply("yes, replace the plan", _planner(app, seen),
                                app)
    assert seen[0]["preserve"] == []
    assert {r["day"]: r["dish"] for r in app.rows}["sunday"] == \
        "Generated sunday"
    assert "Replaced a 1-day plan" in reply


def test_keep_sunday_still_wins_over_a_replace_word():
    app = FakePlanApp([SUNDAY])
    seen = []
    guard.set_pending(1, "plan this week's meals")
    reply = guard.pending_reply("yes, overwrite, but keep Sunday dinner",
                                _planner(app, seen), app)
    assert seen[0]["preserve"] == ["sunday"]
    assert {r["day"]: r["dish"] for r in app.rows}["sunday"] == \
        "salad and potato bar"
    assert "I left Sunday exactly as it was." in reply


def test_a_zero_argument_planner_still_works():
    """Older callers pass a callable that takes nothing at all."""
    app = FakePlanApp([])
    fired = []
    reply = guard.plan_reply("plan this week's meals",
                             lambda: (fired.append(1),
                                      "Dinner plan saved to FreshTable:")[1],
                             app)
    assert fired == [1]
    assert "nothing was replaced" in reply


def test_constraint_from_reads_both_turns():
    assert guard.constraint_from("plan meals, vegetarian", "yes") == \
        "vegetarian"
    assert guard.constraint_from("", "yes, low carb and quick") == \
        "low carb, quick"
    assert guard.constraint_from("plan this week's meals") == ""


# ── the wiring: fastpath → tool ───────────────────────────────────────────

def test_the_fastpath_forwards_preserve_and_the_diet_word(monkeypatch):
    """`_run_plan` is what carries T5's two arguments to auto_meal_plan."""
    import puffpai.api_server as api

    calls = []

    def _invoke(arg):
        calls.append(dict(arg))
        return "Dinner plan saved to FreshTable:\n  - Monday: Chili"

    class _Tools:
        tool_dict = {"auto_meal_plan":
                     type("T", (), {"invoke": staticmethod(_invoke)})()}

    app = FakePlanApp([SUNDAY])
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app", lambda: app)
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())

    asked = api._try_meal_plan("plan this week's meals, vegetarian")
    assert "replace your current 1-day plan" in asked
    assert calls == [], "the gate still holds before the confirmation"

    api._try_meal_plan("yes")
    assert calls and calls[0]["confirm"] is True
    assert calls[0]["preserve"] == "sunday"
    assert calls[0]["dietary_preferences"] == "vegetarian"


def test_the_agent_route_keeps_the_diet_word_across_the_gate():
    """The tool sets the pending slot itself and never sees the sentence —
    the `dietary_preferences` it was given IS the constraint to carry."""
    app = FakePlanApp([SUNDAY])
    seen = []
    guard.set_pending(1, "auto_meal_plan", "vegetarian")
    guard.pending_reply("yes", _planner(app, seen), app)
    assert seen[0]["constraint"] == "vegetarian"
    assert seen[0]["preserve"] == ["sunday"]


def test_auto_meal_plan_never_writes_a_preserved_day(monkeypatch):
    """The tool itself honours `preserve` — it is the last line of defence."""
    import json
    import sys
    from pathlib import Path

    from puffpai import assistant_tools as rt

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"
                           / "puffpai"))
    import app_data as _flat

    class _Store:
        _MEAL_DAYS = list(WEEK)

        def __init__(self):
            self.written = []

        def _load(self, app_id):
            return {"mealPlan": [dict(SUNDAY)], "shoppingList": [],
                    "recipes": []}

        def data_path(self, app_id):
            return "unused"

        def fresh_set_meal(self, day, meal, dish, notes=None, recipe_id=None):
            self.written.append((day, dish))
            return "ok"

        def fresh_grocery_from_plan(self):
            self.written.append(("grocery", ""))

    store = _Store()
    monkeypatch.setattr(_flat, "AppData", _Store)
    monkeypatch.setattr(_Store, "__new__", lambda cls, *a, **k: store)
    # The model labels its days however it likes; the days it was GIVEN win.
    monkeypatch.setattr(
        rt.AssistantTools, "_llm_chat_json",
        lambda self, prompt, model=None, **k: json.dumps(
            {"meals": [{"day": "monday", "dish": f"Dish {i}",
                        "ingredients": []} for i in range(6)]}))

    fn = rt.AssistantTools().tool_dict['auto_meal_plan']
    reply = fn.invoke({"confirm": True, "preserve": "sunday",
                       "dietary_preferences": "vegetarian"})
    days = [d for d, _dish in store.written if d != "grocery"]
    assert "sunday" not in days
    assert days == ["monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday"]
    assert "Sunday" not in reply
