""""log my workout as complete" completes today's planned session.

The box must not ask four questions (member, type, minutes, notes) about a
session it has already planned and can name, such as "Lower Volume, 45 min".
The plan is the answer. The confirmation names the session and nothing else:
no minutes against a person, no streak, no weights (a fitness confirmation
carries no personal values).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

#: A FIXTURE plan, shaped like a real one. No household values in the repo.
SESSION = {"name": "Lower Volume, 45 min", "exercises": []}
PLAN = {"user": "Owner", "program": "Fixture program", "days_per_week": 4,
        "session": SESSION, "guidance": ""}


class FakeAppData:
    def __init__(self, users=None):
        self.users = users if users is not None else {"owner": {"name": "Owner"}}
        self.logged = []

    def get_data(self, doc):
        return {"users": self.users} if doc == "fitfam" else {}

    def fit_log_workout(self, user, workout_type, duration, notes=None):
        if user not in self.users:
            return "Unknown user '%s'." % user
        self.logged.append((user, workout_type, duration))
        return "Logged Owner's %s-minute %s workout. That's 4 days in a row!" % (
            duration, workout_type)


@pytest.fixture()
def bench(monkeypatch):
    store = FakeAppData()
    plan = {"data": PLAN, "reason": ""}
    monkeypatch.setattr(api, "_app_installed", lambda name: True)
    monkeypatch.setattr(api, "_get_app_data", lambda: store)
    monkeypatch.setattr(api, "_fitness_program_data",
                        lambda user_q="", strict_user=False: (plan["data"],
                                                              plan["reason"]))
    store.plan = plan
    return store


@pytest.mark.parametrize("said", [
    "log my workout as complete",          # the plain phrasing
    "log my workout",
    "I finished my workout",
    "mark today's workout done",
    "my workout is done",
    "I did my workout",
])
def test_the_planned_session_is_completed_in_one_step(bench, said):
    reply = api._try_workout_log(said)
    assert reply == "Marked today's session done: Lower Volume, 45 min."
    assert bench.logged == [("owner", "Lower Volume, 45 min", 45)]


def test_the_confirmation_carries_no_personal_values(bench):
    reply = api._try_workout_log("log my workout as complete")
    for value in ("days in a row", "streak", "45-minute", "Owner's"):
        assert value not in reply


def test_nothing_planned_today_asks_instead_of_inventing(bench):
    bench.plan["data"] = dict(PLAN, session=None)
    reply = api._try_workout_log("log my workout as complete")
    assert "nothing planned for today" in reply.lower()
    assert bench.logged == []


def test_more_than_one_person_and_no_active_profile_asks_whose(bench,
                                                               monkeypatch):
    """Two people genuinely training today is the ONLY case that asks.

    An empty profile list with a single FitFam member is not ambiguity at
    all: that case answers from the one plan (or says there is none).
    """
    bench.plan["data"], bench.plan["reason"] = None, "ambiguous user"
    monkeypatch.setattr(api, "_load_workout_library", lambda: LIBRARY)
    bench.users = {
        "alex": {"name": "Alex",
                 "prefs": {"program": "functional_system"}},
        "casey": {"name": "Casey", "prefs": {"program": "youth_developmental"}},
    }
    reply = api._try_workout_log("log my workout as complete")
    assert "whose workout" in reply.lower()
    assert bench.logged == []


def test_a_second_member_is_resolved_by_the_active_profile(bench):
    bench.users = {"owner": {"name": "Owner"}, "other": {"name": "Other"}}
    api._try_workout_log("log my workout")
    assert bench.logged == [("owner", "Lower Volume, 45 min", 45)]


@pytest.mark.parametrize("said", [
    "how long is my workout streak",
    "what workout am I supposed to do today",
    "delete the test workout entry",
    "show me my workout history",
    "plan my workouts for next week",
    "how many workouts did I do last week",
])
def test_the_reads_and_the_deletes_are_left_alone(bench, said):
    assert api._try_workout_log(said) is None


def test_it_runs_before_the_program_read():
    import ast
    import inspect

    src = inspect.getsource(api._try_instant_answer)
    order = [n.func.id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id.startswith("_try_")]
    assert order.index("_try_workout_log") < order.index("_try_fitness_program")


# ── no "Whose workout?" when the household has an owner and a plan ──────────
#
# "log my workout as complete" must not answer "Whose workout? More than one
# person is set up here..." and log nothing: that is the one question this
# flow forbids. An unauthenticated call has no active profile, but the
# HOUSEHOLD still has an owner and a plan, and the plan is the answer.

OTHER_SESSION = {"name": "Upper Volume, 30 min", "exercises": []}


@pytest.fixture(autouse=True)
def _no_open_questions():
    from puffpai import pending_clarifier
    pending_clarifier.reset()
    yield
    pending_clarifier.reset()


def _ambiguous(bench, profiles, per_user):
    """No active profile (the usual state), with a plan for each named member."""
    bench.plan["data"], bench.plan["reason"] = None, "ambiguous user"

    def _program(user_q="", strict_user=False):
        if not user_q:
            return None, "ambiguous user"
        found = per_user.get(str(user_q).lower())
        return (found, "") if found else (None, "no profiles configured")

    return _program, profiles


def test_the_owner_profile_answers_whose_workout_without_asking(bench,
                                                                monkeypatch):
    plans = {"alex": dict(PLAN, user="Alex"),
             "casey": dict(PLAN, user="Casey", session=OTHER_SESSION)}
    program, profiles = _ambiguous(
        bench, [{"id": "owner", "display_name": "Alex"},
                {"id": "p2", "display_name": "Casey"}], plans)
    monkeypatch.setattr(api, "_fitness_program_data", program)
    monkeypatch.setattr(api, "_workout_profiles", lambda: profiles)
    bench.users = {"alex": {"name": "Alex"}, "casey": {"name": "Casey"}}

    reply = api._try_workout_log("log my workout as complete")
    assert reply == "Marked today's session done: Lower Volume, 45 min."
    assert "whose" not in reply.lower()
    assert bench.logged == [("alex", "Lower Volume, 45 min", 45)]


def test_the_only_member_with_a_session_today_is_used(bench, monkeypatch):
    plans = {"casey": dict(PLAN, user="Casey", session=OTHER_SESSION)}
    program, profiles = _ambiguous(
        bench, [{"id": "p1", "display_name": "Alex"},
                {"id": "p2", "display_name": "Casey"}], plans)
    monkeypatch.setattr(api, "_fitness_program_data", program)
    monkeypatch.setattr(api, "_workout_profiles", lambda: profiles)
    bench.users = {"alex": {"name": "Alex"}, "casey": {"name": "Casey"}}

    reply = api._try_workout_log("log my workout as complete")
    assert reply == "Marked today's session done: Upper Volume, 30 min."
    assert bench.logged == [("casey", "Upper Volume, 30 min", 30)]


def test_nobody_has_a_session_today_says_so_instead_of_asking_who(bench,
                                                                  monkeypatch):
    program, profiles = _ambiguous(
        bench, [{"id": "p1", "display_name": "Alex"},
                {"id": "p2", "display_name": "Casey"}], {})
    monkeypatch.setattr(api, "_fitness_program_data", program)
    monkeypatch.setattr(api, "_workout_profiles", lambda: profiles)

    reply = api._try_workout_log("log my workout as complete")
    assert "nothing planned for today" in reply.lower()
    assert "whose" not in reply.lower()
    assert bench.logged == []


def test_two_people_training_today_ask_once_and_the_name_completes_it(
        bench, monkeypatch):
    from puffpai import turn_context

    plans = {"alex": dict(PLAN, user="Alex"),
             "casey": dict(PLAN, user="Casey", session=OTHER_SESSION)}
    program, profiles = _ambiguous(
        bench, [{"id": "p1", "display_name": "Alex"},
                {"id": "p2", "display_name": "Casey"}], plans)
    monkeypatch.setattr(api, "_fitness_program_data", program)
    monkeypatch.setattr(api, "_workout_profiles", lambda: profiles)
    bench.users = {"alex": {"name": "Alex"}, "casey": {"name": "Casey"}}

    turn_context.begin("conv-f4")
    asked = api._try_workout_log("log my workout as complete")
    assert "whose workout" in asked.lower()
    assert bench.logged == []

    reply = api._try_instant_answer("Casey", conversation_id="conv-f4")
    assert reply == "Marked today's session done: Upper Volume, 30 min."
    assert bench.logged == [("casey", "Upper Volume, 30 min", 30)]


# ── the placeholder "Owner" profile matches no FitFam member ──────────────
#
# Owner-first resolution alone falls off the end: the shipped
# config/profiles.json carries the PLACEHOLDER display_name "Owner", which
# matches neither FitFam key (alex, casey), so _workout_user_key returns None
# and the turn would ask "Whose workout?" anyway. The plans in FitFam itself
# are the answer. Shapes below mirror a real store: users keyed by first name,
# each with prefs.program naming a library program.

LIBRARY = {"programs": {
    "functional_system": {
        "title": "Functional Strength System",
        "sessions": [{"name": "Day 2 + Bar: Squat + Push", "duration_min": 45}],
    },
    "youth_developmental": {
        "title": "Youth Developmental",
        "sessions": [{"name": "Circuit A, 30 min"}],
    },
}}

#: A FitFam store: Alex carries a program, Casey carries only a level.
FITFAM_USERS = {
    "alex": {"name": "Alex", "prefs": {"duration": 45, "level": "mid",
                                       "program": "functional_system",
                                       "type": "full"}},
    "casey": {"name": "Casey", "prefs": {"duration": 30, "level": "mid",
                                       "type": "rom"}},
}


@pytest.fixture()
def household_shape(bench, monkeypatch):
    """A typical box: one placeholder "Owner" profile, two FitFam members."""
    bench.users = {k: dict(v) for k, v in FITFAM_USERS.items()}
    bench.plan["data"] = dict(PLAN, user="Owner")     # the placeholder name
    monkeypatch.setattr(api, "_load_workout_library", lambda: LIBRARY)
    monkeypatch.setattr(api, "_workout_profiles",
                        lambda: [{"id": "owner", "display_name": "Owner"}])
    return bench


def test_the_placeholder_owner_name_falls_through_to_the_only_plan(household_shape):
    reply = api._try_workout_log("log my workout as complete")
    assert reply == ("Logged Alex's planned session for today as complete: "
                     "Day 2 + Bar: Squat + Push.")
    assert "whose" not in reply.lower()
    assert household_shape.logged == [("alex", "Day 2 + Bar: Squat + Push", 45)]


def test_a_member_with_no_program_is_not_training_today(household_shape):
    """Only ``prefs.program`` is a plan — a bare level is not one."""
    assert api._fitfam_planned_today("casey", FITFAM_USERS["casey"]) is None
    assert [p["key"] for p in api._workout_planned_members()] == ["alex"]


def test_nobody_planned_says_so_and_writes_nothing(household_shape):
    for row in household_shape.users.values():
        row["prefs"] = {"level": "mid"}
    reply = api._try_workout_log("log my workout as complete")
    assert reply.startswith(
        "Nothing is planned for today, so there's nothing to log.")
    assert "whose" not in reply.lower()
    assert household_shape.logged == []


def test_two_members_planned_is_the_only_case_that_asks(household_shape):
    household_shape.users["casey"]["prefs"]["program"] = "youth_developmental"
    reply = api._try_workout_log("log my workout as complete")
    assert "whose workout" in reply.lower()
    assert household_shape.logged == []


def test_the_ask_that_remains_still_completes_on_the_name(household_shape):
    from puffpai import turn_context

    household_shape.users["casey"]["prefs"]["program"] = "youth_developmental"
    turn_context.begin("conv-49")
    assert "whose workout" in api._try_workout_log(
        "log my workout as complete").lower()

    reply = api._try_instant_answer("Casey", conversation_id="conv-49")
    assert reply == "Marked today's session done: Lower Volume, 45 min."
    assert household_shape.logged == [("casey", "Lower Volume, 45 min", 45)]


# ── the profile names its own FitFam slice ─────────────────────────
#
# Matching a member by display_name alone means the owner profile's
# placeholder ("Owner") matches nobody and every "log my workout" falls back to
# guessing from the plans. A profile carries ``fitfam_key`` (and its id is
# a slug of the name), and both are tried BEFORE the display name.

class _RosterStore:
    def __init__(self, users):
        self.users = users

    def get_data(self, doc):
        return {"users": self.users} if doc == "fitfam" else {}


def test_fitfam_key_on_the_profile_wins_over_the_display_name(monkeypatch):
    from puffpai import household

    monkeypatch.setattr(household, "members", lambda: [
        {"id": "alex", "display_name": "Alex", "role": "owner"}])
    monkeypatch.setattr(household, "_manager", lambda: _FakeManager(
        {"id": "alex", "display_name": "Alex", "fitfam_key": "casey"}))
    store = _RosterStore({"alex": {"name": "Alex"}, "casey": {"name": "Casey"}})
    assert household.fitfam_keys_for("Alex")[0] == "casey"
    assert api._workout_user_key(store, "Alex") == "casey"


def test_the_profile_id_is_tried_when_no_fitfam_key_is_set(monkeypatch):
    from puffpai import household

    monkeypatch.setattr(household, "members", lambda: [
        {"id": "riley", "display_name": "Riley", "role": "child"}])
    monkeypatch.setattr(household, "_manager", lambda: _FakeManager(
        {"id": "riley", "display_name": "Riley"}))
    store = _RosterStore({"alex": {"name": "Alex"}, "riley": {"name": "Riley"}})
    assert household.fitfam_keys_for("Riley")[0] == "riley"
    assert api._workout_user_key(store, "Riley") == "riley"


def test_an_unknown_name_still_falls_through_to_the_old_matching(monkeypatch):
    from puffpai import household

    monkeypatch.setattr(household, "members", lambda: [])
    store = _RosterStore({"alex": {"name": "Alex"}, "casey": {"name": "Casey"}})
    assert api._workout_user_key(store, "Alex") == "alex"
    assert api._workout_user_key(store, "Nobody") is None


class _FakeManager:
    def __init__(self, profile):
        self._profile = profile

    def get_profile(self, profile_id):
        return self._profile if profile_id == self._profile["id"] else None
