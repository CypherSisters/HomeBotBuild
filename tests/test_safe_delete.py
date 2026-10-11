"""A delete by description never removes a
different item.

If a resolver matches rows sharing ANY one word, "delete the SAMPLE dishes
reminder" (no such item) could delete a task and two
events called "do the dishes" and reply as if it had removed what was asked.
The fastpath declines (the words do not all match), so the planner calls
``app_delete_task`` / ``app_delete_event``, whose resolver must match on the
whole description. Everything here runs on a real ``AppData`` on a temp dir.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CONV = "conv-safe-delete"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


def _raw(app):
    return json.loads(Path(app._files['calendar']).read_text(encoding="utf-8"))


def _live_shape(app):
    """The live store: a task (with its mirror event) and a separate event."""
    app.cal_add_task("do the dishes", "2026-09-17")
    app.cal_add_event("do the dishes", "2026-09-19T18:00")
    raw = _raw(app)
    assert len(raw["tasks"]) == 1 and len(raw["events"]) == 2
    return raw


# ── no match: nothing changes, and the reply says so ────────────────────────

@pytest.mark.parametrize("said", [
    "SAMPLE dishes", "the SAMPLE dishes reminder", "SAMPLE dishes reminder",
])
def test_an_extra_word_is_not_a_match_for_the_task_tool(app, said):
    before = _live_shape(app)
    reply = app.cal_delete_task(said)
    assert reply.startswith("No task matches"), reply
    assert "nothing was deleted" in reply
    assert _raw(app) == before


@pytest.mark.parametrize("said", ["SAMPLE dishes", "the SAMPLE dishes event"])
def test_an_extra_word_is_not_a_match_for_the_event_tool(app, said):
    before = _live_shape(app)
    reply = app.cal_delete_event(said)
    assert reply.startswith("No event matches"), reply
    assert "nothing was deleted" in reply
    assert _raw(app) == before


def test_the_live_sentence_through_the_fastpath_changes_nothing(app):
    from puffpai import calendar_nl
    calendar_nl.forget_pending_delete()
    before = _live_shape(app)
    calendar_nl.handle_delete("delete the SAMPLE dishes reminder", app,
                              conversation_id=CONV)
    assert _raw(app) == before


def test_a_part_of_a_word_is_not_a_match(app):
    app.cal_add_event("Doctor visit", "2026-09-21T10:00")
    reply = app.cal_delete_event("do")
    assert reply.startswith("No event matches"), reply
    assert len(_raw(app)["events"]) == 1


def test_a_name_plus_a_day_does_not_fall_back_to_the_day_alone(app):
    saturday = datetime.now() + timedelta(days=(5 - datetime.now().weekday()) % 7)
    app.cal_add_event("test-fake dentist", saturday.strftime("%Y-%m-%dT15:00"))
    reply = app.cal_delete_event("dishes on Saturday")
    assert reply.startswith("No event matches"), reply
    assert len(_raw(app)["events"]) == 1


# ── exactly one match: deleted, and named with its date ─────────────────────

def test_the_dishes_reminder_deletes_the_one_task_and_names_its_date(app):
    app.cal_add_task("do the dishes", "2026-09-17")
    reply = app.cal_delete_task("the dishes reminder")
    assert reply == ("Deleted the task 'do the dishes', due September 17, "
                     "and removed it from the calendar."), reply
    raw = _raw(app)
    assert raw["tasks"] == [] and raw["events"] == []


def test_the_dishes_event_deletes_the_one_event_and_names_its_time(app):
    app.cal_add_event("do the dishes", "2026-09-19T18:00")
    reply = app.cal_delete_event("the dishes event")
    assert reply == "Deleted the event 'do the dishes' on September 19 at 6 PM.", reply
    assert _raw(app)["events"] == []


def test_the_dishes_reminder_through_the_fastpath_names_its_date(app):
    from puffpai import calendar_nl
    calendar_nl.forget_pending_delete()
    app.cal_add_task("do the dishes", "2026-09-17")
    reply = calendar_nl.handle_delete("delete the dishes reminder", app,
                                      conversation_id=CONV)
    assert reply and reply.startswith("Deleted"), reply
    assert "September 17" in reply and "do the dishes" in reply
    assert _raw(app)["tasks"] == []


# ── several matches: ask, list them with dates, change nothing ──────────────

def test_two_matching_tasks_ask_which_and_delete_nothing(app):
    app.cal_add_task("do the dishes", "2026-09-17")
    app.cal_add_task("do the dishes", "2026-09-19")
    before = _raw(app)
    reply = app.cal_delete_task("the dishes reminder")
    assert "Which one should I delete?" in reply, reply
    assert "September 17" in reply and "September 19" in reply
    assert "Nothing has been deleted yet." in reply
    assert _raw(app) == before


def test_two_matching_events_ask_which_and_delete_nothing(app):
    app.cal_add_event("do the dishes", "2026-09-17T18:00")
    app.cal_add_event("do the dishes", "2026-09-19T18:00")
    before = _raw(app)
    reply = app.cal_delete_event("dishes")
    assert "Which one should I delete?" in reply, reply
    assert _raw(app) == before


# ── the calendar delete clarifier is unchanged ─────────────────────

def test_c4_flow_still_asks_then_deletes_only_the_chosen_one(app, monkeypatch):
    import puffpai.api_server as api
    from puffpai import calendar_nl, pending_clarifier
    pending_clarifier.reset()
    calendar_nl.forget_pending_delete()
    monkeypatch.setattr(api, "_get_app_data", lambda: app)
    title = "Take the kids to soccer practice"
    start = datetime(2026, 9, 14, 6, 15).isoformat(timespec="minutes")
    app.cal_add_event(title, start, recurrence="custom:mon,tue,thu,fri")
    app.cal_add_event(title, start, recurrence="weekdays")

    asked = api._try_calendar_delete("delete the soccer reminder", CONV)
    assert asked and "which one should i delete" in asked.lower()
    assert len(_raw(app)["events"]) == 2

    reply = api._try_instant_answer("the every-weekday one",
                                    conversation_id=CONV)
    assert reply and "deleted" in reply.lower()
    assert [e["rec"] for e in _raw(app)["events"]] == ["custom:mon,tue,thu,fri"]
    pending_clarifier.reset()


# ── ignored words only name nothing (review follow-up) ────────────────────

@pytest.mark.parametrize("said", ["the", "my", "the reminder", "my task"])
def test_only_ignored_words_delete_no_task(app, said):
    app.cal_add_task("do the dishes", "2026-09-17")
    before = _raw(app)
    reply = app.cal_delete_task(said)
    assert reply.startswith("No task matches"), reply
    assert _raw(app) == before


@pytest.mark.parametrize("said", ["my", "the", "my event"])
def test_only_ignored_words_delete_no_event(app, said):
    app.cal_add_event("my dentist", "2026-09-19T18:00")
    before = _raw(app)
    reply = app.cal_delete_event(said)
    assert reply.startswith("No event matches"), reply
    assert _raw(app) == before


# ── the vault request shares the "which one" prompt (review regression) ──

def test_an_ambiguous_vault_request_asks_which_and_changes_nothing(app):
    app.cal_add_med("Lisinopril 10mg", "10mg", "all")
    app.cal_add_med("Lisinopril 20mg", "20mg", "all")
    before = _raw(app)
    reply = app.cal_vault_med("lisinopril")
    assert reply.startswith("I found 2 medications that match 'lisinopril'"), reply
    assert "Which one should I move to the Vault? Nothing has changed yet." in reply
    assert "delete" not in reply.lower()
    assert _raw(app) == before


def test_a_single_vault_match_still_vaults(app):
    app.cal_add_med("Lisinopril 10mg", "10mg", "all")
    app.cal_vault_med("lisinopril")
    assert _raw(app)["meds"][0].get("vaulted") is True
