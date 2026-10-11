"""Calendar tasks, occurrences and medications, against the store.

Everything here runs on a REAL ``AppData`` pointed at a temp data dir, so the
rows written are the rows the box would write. Fixtures mirror the real
``calendar.json`` shape; every value is invented.
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
    # Point AppData at a temp dir WITHOUT reloading the module: a reload
    # swaps the class other already-imported modules hold, which is how one
    # test file poisons the ones that run after it.
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


def _raw(app):
    return json.loads(Path(app._files['calendar']).read_text(encoding="utf-8"))


# ── deleting a task takes its mirror event with it ───────────────────

def test_deleting_a_task_removes_the_event_it_created(app):
    app.cal_add_task("call the dentist", "2026-09-07")
    assert len(_raw(app)["events"]) == 1
    reply = app.cal_delete_task("call the dentist")
    assert "Deleted" in reply
    after = _raw(app)
    assert after["tasks"] == []
    assert after["events"] == [], "the linked event was orphaned"


def test_the_natural_delete_phrasing_finds_it(app):
    """A natural phrasing that names the event loosely: 'the dentist event'."""
    app.cal_add_task("call the dentist", "2026-09-07")
    reply = app.cal_delete_event("the dentist event")
    assert "Deleted" in reply
    assert _raw(app)["events"] == []


def test_delete_the_dentist_reminder_resolves_the_task(app):
    app.cal_add_task("call the dentist", "2026-09-07")
    reply = app.cal_delete_task("the dentist reminder")
    assert "Deleted" in reply
    assert _raw(app)["tasks"] == []


def test_remove_the_event_on_saturday_resolves_by_day(app):
    saturday = datetime.now() + timedelta(days=(5 - datetime.now().weekday()) % 7)
    app.cal_add_event("test-fakeevent",
                      saturday.strftime("%Y-%m-%dT15:00"))
    reply = app.cal_delete_event("the event on Saturday")
    assert "Deleted" in reply
    assert _raw(app)["events"] == []


def test_two_matches_ask_instead_of_deleting_both(app):
    app.cal_add_event("fake dentist checkup", "2026-09-07T10:00")
    app.cal_add_event("fake dentist follow-up", "2026-09-14T10:00")
    reply = app.cal_delete_event("dentist")
    assert "Which one" in reply
    assert len(_raw(app)["events"]) == 2, "an ambiguous name deleted rows"


def test_a_blank_identifier_still_cannot_wipe_the_calendar(app):
    app.cal_add_event("fake thing", "2026-09-07T10:00")
    assert "I need" in app.cal_delete_event("")
    assert "I need" in app.cal_delete_task(" ")
    assert len(_raw(app)["events"]) == 1


def test_deleting_the_mirror_event_takes_the_task_with_it(app):
    app.cal_add_task("call the fake plumber", "2026-09-07")
    app.cal_delete_event("call the fake plumber")
    after = _raw(app)
    assert after["events"] == [] and after["tasks"] == []


# ── the chat/voice surface expands recurrence too ────────────────────

def test_a_weekly_event_from_a_month_ago_is_listed(app):
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT10:00")
    app.cal_add_event("test-fakeevent-recurring", start, recurrence="weekly")
    listed = app.cal_list_events(7)
    assert "test-fakeevent-recurring" in listed, "recurrence dropped"


def test_expanded_occurrences_is_the_scheduler_interface(app):
    start = (datetime.now() - timedelta(days=30)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    app.cal_add_event("test-fakeevent-recurring",
                      start.strftime("%Y-%m-%dT10:00"), recurrence="weekly")
    rows = app.cal_expanded_occurrences(datetime.now() - timedelta(hours=1),
                                        datetime.now() + timedelta(days=14))
    assert len(rows) >= 2
    assert {"when", "title", "id", "rec", "taskId"} <= set(rows[0])
    assert (app.expanded_occurrences.__func__
            is app.cal_expanded_occurrences.__func__)


# ── a scoped read answers about that day only ────────────────────────

def _day(stamp):
    first = datetime.combine(stamp.date(), datetime.min.time())
    return first, first.replace(hour=23, minute=59, second=59)


def test_tomorrow_is_scoped_and_named(app):
    tomorrow = datetime.now() + timedelta(days=1)
    app.cal_add_event("fake tomorrow thing",
                      tomorrow.strftime("%Y-%m-%dT10:00"))
    app.cal_add_event("fake next week thing",
                      (datetime.now() + timedelta(days=4)).strftime(
                          "%Y-%m-%dT10:00"))
    first, last = _day(tomorrow)
    reply = app.cal_list_events(1, start=first, end=last, label="tomorrow")
    assert "fake tomorrow thing" in reply
    assert "fake next week thing" not in reply
    assert "tomorrow" in reply


def test_an_empty_day_says_nothing_on_that_day(app):
    saturday = datetime.now() + timedelta(days=(5 - datetime.now().weekday()) % 7)
    first, last = _day(saturday)
    reply = app.cal_list_events(1, start=first, end=last, label="Saturday")
    assert reply == "Nothing on Saturday."


def test_the_unscoped_read_is_unchanged(app):
    assert app.cal_list_events(7) == "No events in the next 7 days."


# ── vaulted medications ──────────────────────────────────────

FAKE_MED = "Fakezepam"


def test_a_vaulted_med_never_appears_by_name(app):
    app.cal_add_med(FAKE_MED, "5mg", "mom", next_time="8:00 AM")
    app.cal_vault_med(FAKE_MED, label="morning pills")
    listed = app.cal_list_meds()
    assert FAKE_MED not in listed
    assert "morning pills" in listed
    payload = json.dumps(
        app.cal_schedulable_reminders(datetime.now(),
                                      datetime.now() + timedelta(days=2)),
        default=str)
    assert FAKE_MED not in payload, "the name reached a scheduler payload"
    assert "morning pills" in payload


def test_vaulting_with_no_label_uses_a_generic_one(app):
    app.cal_add_med(FAKE_MED, "5mg", "dad")
    app.cal_vault_med("")
    assert FAKE_MED not in app.cal_list_meds()
    assert "your medication" in app.cal_list_meds()


def test_a_med_can_be_vaulted_from_the_start(app):
    reply = app.cal_add_med(FAKE_MED, "5mg", "dad", vaulted=True,
                            label="evening pills")
    assert FAKE_MED not in reply
    assert "evening pills" in reply
    assert FAKE_MED not in json.dumps(_raw(app)["meds"], default=str)


def test_the_name_leaves_the_row_entirely(app):
    app.cal_add_med(FAKE_MED, "5mg", "mom")
    app.cal_vault_med(FAKE_MED, label="morning pills")
    row = _raw(app)["meds"][0]
    assert FAKE_MED not in json.dumps(row)
    assert row["vaulted"] is True and row["med_id"]
    # The interim home for the name, until sec-app-data's encrypted
    # projection lands. No calendar reader touches it.
    assert _raw(app)["meds_vault_pending"][row["med_id"]]["name"] == FAKE_MED


def test_a_med_carries_a_real_schedule_not_a_bare_wall_clock(app):
    app.cal_add_med(FAKE_MED, "5mg", "mom", next_time="8am")
    row = _raw(app)["meds"][0]
    assert row["times"] == ["08:00"]
    assert row["days"] == [0, 1, 2, 3, 4, 5, 6]
    assert row["tz"] == "local"


def test_a_med_added_at_8am_fires_at_the_next_local_0800(app):
    app.cal_add_med(FAKE_MED, "5mg", "mom", next_time="8:00 AM")
    now = datetime.now()
    rows = app.cal_schedulable_reminders(now, now + timedelta(days=2))
    meds = [r for r in rows if r["kind"] == "medication"]
    assert meds, "a medication produced no schedulable occurrence"
    assert all(r["when"].hour == 8 and r["when"].minute == 0 for r in meds)
    assert meds[0]["when"] >= now
    assert meds[0]["when"] <= now + timedelta(days=1)


def test_no_refill_date_is_invented(app):
    app.cal_add_med(FAKE_MED, "5mg", "mom")
    assert _raw(app)["meds"][0]["refill"] is None
    app.cal_add_med("Fakestatin", "10mg", "dad", refill="2026-12-01")
    assert _raw(app)["meds"][1]["refill"] == "2026-12-01"


def test_twice_daily_gets_both_times(app):
    app.cal_add_med(FAKE_MED, "5mg", "mom", freq="twice-daily",
                    next_time="8:00 AM")
    assert _raw(app)["meds"][0]["times"] == ["08:00", "20:00"]


def test_the_rest_payload_never_carries_the_vaulted_name(app):
    """/apps/calendar/data is a reader like any other."""
    app.cal_add_med(FAKE_MED, "5mg", "mom")
    app.cal_vault_med(FAKE_MED, label="morning pills")
    payload = app.get_data("calendar")
    assert "meds_vault_pending" not in payload
    assert FAKE_MED not in json.dumps(payload, default=str)
    assert payload["meds"][0]["name"] == "morning pills"


def test_a_write_back_of_that_payload_does_not_drop_the_name(app):
    app.cal_add_med(FAKE_MED, "5mg", "mom")
    app.cal_vault_med(FAKE_MED, label="morning pills")
    app.set_data("calendar", app.get_data("calendar"))
    row = _raw(app)["meds"][0]
    assert _raw(app)["meds_vault_pending"][row["med_id"]]["name"] == FAKE_MED


def test_every_schedulable_row_has_a_stable_per_occurrence_id(app):
    """The scheduler dedupes per occurrence; `id` stays the stored row."""
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT10:00")
    app.cal_add_event("fake weekly thing", start, recurrence="weekly")
    rows = app.cal_schedulable_reminders(datetime.now() - timedelta(days=1),
                                         datetime.now() + timedelta(days=14))
    ids = [r["occurrence_id"] for r in rows]
    assert len(ids) == len(set(ids)), "two occurrences shared one key"
    for row in rows:
        assert row["occurrence_id"].startswith(str(row["id"]))
        assert row["when"].isoformat() in row["occurrence_id"]
