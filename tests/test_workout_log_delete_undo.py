"""Delete/undo a logged FitFam workout.

Without a delete or undo path for a logged FitFam entry on EVERY surface
(chat, the app action API, and the data layer), a mis-logged workout is
permanent and a stray entry has no product way to be removed.

The phrasings exercised here, verbatim: "delete my last workout", "undo
that", "remove the workout I just logged", "delete test-fakeworkout".

EVERY fixture in this file is fake: made-up slice keys, "Test Fakeperson"
names, and made-up workout titles. No real household data, name, or metric
appears anywhere in this file by design: FitFam entries are health data and a
child slice is permitted.
"""

from datetime import datetime, timedelta

import pytest

from puffpai import app_data as app_data_mod
from puffpai import fitfam_access as fa

_ENV = "PUFFPAI_DATA_DIR"

USER_A = "fakeuser1"
USER_B = "fakeuser2"


def _entry(title, days_ago=0):
    when = datetime.now() - timedelta(days=days_ago)
    return {"date": when.isoformat(), "type": title, "duration": 1,
            "notes": ""}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    ad = app_data_mod.AppData()
    ad.set_data("fitfam", {
        "users": {
            USER_A: {"name": "Test Fakeperson", "emoji": "\U0001f9d4",
                     "prefs": {}, "goals": [],
                     "history": [_entry("test-fakewarmup", days_ago=1),
                                 _entry("test-fakeworkout")],
                     "streak": 2, "totalWorkouts": 2},
            USER_B: {"name": "Test Fakeperson Two", "emoji": "\U0001f3c4",
                     "prefs": {}, "goals": [],
                     "history": [_entry("test-fakeworkout")],
                     "streak": 1, "totalWorkouts": 1},
        },
        "last_updated": "",
    })
    return ad


def _history(app, key):
    return app.get_data("fitfam")["users"][key]["history"]


def _titles(app, key):
    return [e["type"] for e in _history(app, key)]


# ── data layer ───────────────────────────────────────────────────────────────

class TestDeleteEntry:
    def test_delete_by_title_removes_exactly_that_entry(self, app):
        result = app.fit_delete_entry(USER_A, title="test-fakeworkout")
        assert result["ok"] is True
        assert result["removed"] == "test-fakeworkout"
        assert _titles(app, USER_A) == ["test-fakewarmup"]

    def test_delete_last_removes_the_most_recent_entry(self, app):
        result = app.fit_delete_entry(USER_A, last=True)
        assert result["ok"] is True
        assert result["removed"] == "test-fakeworkout"
        assert _titles(app, USER_A) == ["test-fakewarmup"]

    def test_a_fuzzy_title_still_finds_the_entry(self, app):
        result = app.fit_delete_entry(USER_A, title="test fakeworkout")
        assert result["ok"] is True
        assert result["removed"] == "test-fakeworkout"

    def test_it_touches_only_the_requesting_users_slice(self, app):
        before_other = _titles(app, USER_B)
        app.fit_delete_entry(USER_A, title="test-fakeworkout")
        assert _titles(app, USER_B) == before_other
        assert "test-fakeworkout" in _titles(app, USER_B)

    def test_it_refuses_an_unknown_user_and_names_the_slices(self, app):
        result = app.fit_delete_entry("nobody-fake", title="test-fakeworkout")
        assert result["ok"] is False
        assert result["reason"] == "unknown_user"
        assert set(result["users"]) == {USER_A, USER_B}

    def test_it_refuses_with_no_user_at_all(self, app):
        result = app.fit_delete_entry("", last=True)
        assert result["ok"] is False
        assert result["reason"] == "unknown_user"

    def test_a_title_matching_two_different_workouts_is_refused(self, app):
        result = app.fit_delete_entry(USER_A, title="test-fake")
        assert result["ok"] is False
        assert result["reason"] == "ambiguous"
        assert set(result["candidates"]) == {"test-fakewarmup",
                                             "test-fakeworkout"}
        assert len(_history(app, USER_A)) == 2

    def test_the_same_title_twice_deletes_the_most_recent(self, app):
        app.fit_log_workout(USER_A, "test-fakeworkout", 1)
        before = len(_history(app, USER_A))
        result = app.fit_delete_entry(USER_A, title="test-fakeworkout")
        assert result["ok"] is True
        assert len(_history(app, USER_A)) == before - 1
        assert "test-fakeworkout" in _titles(app, USER_A)

    def test_an_unknown_title_is_reported_not_guessed(self, app):
        result = app.fit_delete_entry(USER_A, title="test-fakenothing")
        assert result["ok"] is False
        assert result["reason"] == "no_match"
        assert len(_history(app, USER_A)) == 2

    def test_an_empty_history_is_reported(self, app):
        app.fit_delete_entry(USER_B, last=True)
        result = app.fit_delete_entry(USER_B, last=True)
        assert result["ok"] is False
        assert result["reason"] == "empty"

    def test_the_count_drops_by_exactly_one(self, app):
        before = len(_history(app, USER_A))
        app.fit_delete_entry(USER_A, last=True)
        assert len(_history(app, USER_A)) == before - 1

    def test_totals_and_streak_are_recounted_after_a_delete(self, app):
        app.fit_delete_entry(USER_A, title="test-fakeworkout")
        u = app.get_data("fitfam")["users"][USER_A]
        # One entry left, and it is not today's, so the run is broken.
        assert u["totalWorkouts"] == 1
        assert u["streak"] == 1

    def test_deleting_the_only_entry_zeroes_the_streak(self, app):
        app.fit_delete_entry(USER_B, last=True)
        u = app.get_data("fitfam")["users"][USER_B]
        assert u["streak"] == 0
        assert u["totalWorkouts"] == 0

    def test_the_reply_payload_carries_no_metric(self, app):
        result = app.fit_delete_entry(USER_A, title="test-fakeworkout")
        assert set(result) == {"ok", "user", "name", "removed", "remaining"}
        for banned in ("duration", "date", "streak", "age", "weight", "reps"):
            assert banned not in result

    def test_user_keys_returns_keys_not_contents(self, app):
        keys = app.fit_user_keys()
        assert set(keys) == {USER_A, USER_B}


# ── phrasing ─────────────────────────────────────────────────────────────────

class TestDeclaredPhrasings:
    @pytest.mark.parametrize("phrase", [
        "delete my last workout",
        "remove the workout I just logged",
        "delete the test-fakeworkout workout entry",
        "undo my last workout",
    ])
    def test_the_declared_delete_phrasings_parse(self, phrase):
        assert fa.parse_delete(phrase) is not None

    def test_undo_that_parses_as_a_bare_undo(self):
        parsed = fa.parse_delete("undo that")
        assert parsed is not None
        assert parsed["bare_undo"] is True
        assert parsed["scope"] == "last"

    def test_delete_a_bare_title_needs_proof_from_real_data(self):
        parsed = fa.parse_delete("delete test-fakeworkout")
        assert parsed["scope"] == "title"
        assert parsed["needs_title_proof"] is True
        assert fa.norm_title(parsed["title"]) == "testfakeworkout"

    def test_a_named_person_is_picked_up(self):
        parsed = fa.parse_delete("delete the last workout for fakeuser1")
        assert parsed["user"] == "fakeuser1"

    @pytest.mark.parametrize("phrase", [
        "delete the 7am alarm",
        "remove milk from the shopping list",
        "delete the dentist event",
        "what is my streak",
        "log a workout",
    ])
    def test_it_declines_everything_that_is_not_a_workout_delete(self, phrase):
        assert fa.parse_delete(phrase) is None

    def test_a_title_only_matches_its_own_direction_when_strict(self):
        history = [{"type": "running", "date": datetime.now().isoformat()}]
        assert fa.find_by_title(history, "running shoes") == [0]
        assert fa.find_by_title(history, "running shoes", strict=True) == []

    def test_resolve_user_matches_key_or_display_name(self):
        users = {USER_A: {"name": "Test Fakeperson"}}
        assert fa.resolve_user(users, "fakeuser1")[0] == USER_A
        assert fa.resolve_user(users, "Test Fakeperson")[0] == USER_A
        assert fa.resolve_user(users, "nobody")[0] is None

    def test_recent_logger_is_none_when_two_people_just_logged(self, app):
        users = app.get_data("fitfam")["users"]
        assert fa.recent_logger(users) is None

    def test_recent_logger_finds_the_only_recent_slice(self, app):
        users = app.get_data("fitfam")["users"]
        users[USER_B]["history"] = []
        assert fa.recent_logger(users) == USER_A


# ── chat fastpath ────────────────────────────────────────────────────────────

@pytest.fixture()
def chat(app, monkeypatch):
    import puffpai.api_server as api
    monkeypatch.setattr(api, "_get_app_data", lambda: app)
    fa.clear_pending()
    yield api
    fa.clear_pending()


class TestChatFastpath:
    def test_it_asks_whose_workout_instead_of_guessing(self, chat):
        reply = chat._try_fitness_delete("delete my last workout")
        assert reply is not None
        assert "whose workout" in reply.lower()
        assert USER_A in reply and USER_B in reply

    def test_the_answer_to_whose_workout_completes_the_delete(self, chat, app):
        assert "whose workout" in chat._try_fitness_delete(
            "delete my last workout").lower()
        reply = chat._try_fitness_delete(USER_A)
        assert "test-fakeworkout" in reply
        assert _titles(app, USER_A) == ["test-fakewarmup"]

    def test_a_named_person_deletes_without_asking(self, chat, app):
        reply = chat._try_fitness_delete(
            "remove the workout I just logged for " + USER_A)
        assert "test-fakeworkout" in reply
        assert _titles(app, USER_A) == ["test-fakewarmup"]

    def test_a_bare_title_owned_by_two_people_asks(self, chat, app):
        reply = chat._try_fitness_delete("delete test-fakeworkout")
        assert "whose workout" in reply.lower()
        assert len(_history(app, USER_A)) == 2

    def test_a_bare_title_owned_by_one_person_just_deletes(self, chat, app):
        app.fit_delete_entry(USER_B, title="test-fakeworkout")
        reply = chat._try_fitness_delete("delete test-fakeworkout")
        assert "test-fakeworkout" in reply
        assert _titles(app, USER_A) == ["test-fakewarmup"]

    def test_undo_that_falls_through_when_nothing_was_just_logged(
            self, chat, app):
        data = app.get_data("fitfam")
        for key in (USER_A, USER_B):
            for entry in data["users"][key]["history"]:
                entry["date"] = (datetime.now()
                                 - timedelta(days=3)).isoformat()
        app.set_data("fitfam", data)
        assert chat._try_fitness_delete("undo that") is None

    def test_undo_that_removes_the_one_just_logged(self, chat, app):
        data = app.get_data("fitfam")
        data["users"][USER_B]["history"] = []
        app.set_data("fitfam", data)
        reply = chat._try_fitness_delete("undo that")
        assert "test-fakeworkout" in reply
        assert _titles(app, USER_A) == ["test-fakewarmup"]

    def test_an_ambiguous_title_asks_which_one_then_deletes_it(
            self, chat, app):
        reply = chat._try_fitness_delete(
            "delete the test-fake workout for " + USER_A)
        assert "which one" in reply.lower()
        assert len(_history(app, USER_A)) == 2
        follow = chat._try_fitness_delete("test-fakewarmup")
        assert "test-fakewarmup" in follow
        assert _titles(app, USER_A) == ["test-fakeworkout"]

    def test_an_unknown_person_is_not_silently_redirected(self, chat, app):
        reply = chat._try_fitness_delete(
            "delete the last workout for nobodyfake")
        assert "whose workout" in reply.lower()
        assert len(_history(app, USER_A)) == 2

    def test_the_confirmation_carries_no_metric(self, chat, app):
        reply = chat._try_fitness_delete("delete test-fakewarmup")
        low = reply.lower()
        for banned in ("minute", "streak", "total", "day", "age"):
            assert banned not in low

    @pytest.mark.parametrize("phrase", [
        "delete the 7am alarm",
        "remove milk from the shopping list",
        "what is my streak",
        "how long is my workout streak",
        "log todays workout",
    ])
    def test_it_declines_and_falls_through(self, chat, phrase):
        assert chat._try_fitness_delete(phrase) is None


# ── app action API ───────────────────────────────────────────────────────────

class TestActionApi:
    def test_delete_is_exposed_for_fitfam(self):
        import puffpai.api_server as api
        assert "fit_delete_entry" in api._APP_ACTIONS["fitfam"]
        assert "delete_entry" in api._APP_ACTIONS["fitfam"]

    def test_the_page_alias_maps_to_the_method(self):
        import puffpai.api_server as api
        assert api._APP_ACTION_ALIASES["fitfam"]["delete_entry"] == \
            "fit_delete_entry"

    def test_no_other_app_can_call_it(self):
        import puffpai.api_server as api
        for name, actions in api._APP_ACTIONS.items():
            if name == "fitfam":
                continue
            assert "fit_delete_entry" not in actions
            assert "delete_entry" not in actions


# ── per-user gate on the program reader ──────────────────────────────────────

class TestProgramUserGate:
    def test_chat_asks_rather_than_reading_the_first_profile(
            self, tmp_path, monkeypatch):
        import json
        import puffpai.api_server as api
        path = tmp_path / "profiles.json"
        path.write_text(json.dumps({"profiles": [
            {"id": "fakeone", "display_name": "Test Fakeperson",
             "preferences": {"workout_level": "beginner"}},
            {"id": "faketwo", "display_name": "Test Fakeperson Two",
             "preferences": {"workout_level": "beginner"}},
        ]}), encoding="utf-8")
        monkeypatch.setattr("puffpai.profile_manager.profiles_path",
                            lambda: str(path))
        data, reason = api._fitness_program_data("", strict_user=True)
        assert data is None
        assert reason == "ambiguous user"
        # The HTTP route keeps its historical fallback.
        data, reason = api._fitness_program_data("")
        assert data is not None

    def test_one_profile_is_never_ambiguous(self, tmp_path, monkeypatch):
        import json
        import puffpai.api_server as api
        path = tmp_path / "profiles.json"
        path.write_text(json.dumps({"profiles": [
            {"id": "fakeone", "display_name": "Test Fakeperson",
             "preferences": {"workout_level": "beginner"}},
        ]}), encoding="utf-8")
        monkeypatch.setattr("puffpai.profile_manager.profiles_path",
                            lambda: str(path))
        data, _reason = api._fitness_program_data("", strict_user=True)
        assert data is not None
