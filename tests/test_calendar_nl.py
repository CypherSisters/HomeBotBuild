"""A recurring-calendar conversation, turn by turn.

Three turns, each with its own failure mode:

1. "Every Monday, Tuesday, Thursday, and Friday morning at 6.15, I have to
   take the kids to soccer practice. Can you add that?" must not be routed to
   RAG and refused.
2. "Yes, set up a reminder every weekday morning except for Wednesdays at 615
   take the kids to soccer practice." must not become a TASK whose title is
   the whole sentence, due the next morning at 09:00, `rec: null`.
3. "Add it to the calendar view." must not add two MORE events with wrong
   recurrences ("weekdays", "weekly") and repeat turn 2's confirmation.

Every assertion below uses those phrasings verbatim, against a real `AppData`
on a temp directory; no network, no model.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

TURN_1 = ("Every Monday, Tuesday, Thursday, and Friday morning at 6.15, "
          "I have to take the kids to soccer practice. Can you add that?")
TURN_2 = ("Yes, set up a reminder every weekday morning except for Wednesdays "
          "at 615 take the kids to soccer practice.")
TURN_3 = "Add it to the calendar view."

TITLE = "Take the kids to soccer practice"

#: A Wednesday evening, the clock the conversation runs against.
NOW = datetime(2026, 9, 9, 18, 31)

#: The conversation an open "which one should I delete?" prompt was asked in.
#: A bare "both" only ever answers the prompt asked in ITS OWN conversation,
#: on the very next turn.
CONV = "conv-a"
OTHER_CONV = "conv-b"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


@pytest.fixture(autouse=True)
def clean_followup_memory():
    """The "what did the last turn create" record is module state."""
    from puffpai import calendar_nl
    calendar_nl.forget()
    yield
    calendar_nl.forget()


@pytest.fixture()
def nl():
    from puffpai import calendar_nl
    return calendar_nl


def _events(app):
    with app._locks['calendar']:
        return list(app._load('calendar').get('events', []))


def _tasks(app):
    with app._locks['calendar']:
        return list(app._load('calendar').get('tasks', []))


# ── 1. the sentence parses into ONE item ────────────────────────────────────

@pytest.mark.parametrize("phrase,days,clock", [
    (TURN_1, [0, 1, 3, 4], (6, 15)),
    (TURN_2, [0, 1, 3, 4], (6, 15)),
    ("Add a reminder every Monday, Tuesday, Thursday and Friday at 6:15 am "
     "to take the kids to soccer practice", [0, 1, 3, 4], (6, 15)),
    ("Set up a reminder every weekday except Wednesday at 6:15 to take the kids "
     "to soccer practice", [0, 1, 3, 4], (6, 15)),
    ("Add a reminder for Tuesdays and Thursdays at 6 to walk the dog",
     [1, 3], (6, 0)),
    ("Put piano lessons on the calendar every school day at 7:15",
     [0, 1, 2, 3, 4], (7, 15)),
])
def test_the_day_set_and_the_time_are_parsed(nl, phrase, days, clock):
    req = nl.parse_recurring_request(phrase, now=NOW)
    assert req is not None, "the fastpath declined a request it must own"
    assert sorted(req.days) == days
    assert (req.hour, req.minute) == clock
    assert (req.start.hour, req.start.minute) == clock
    assert req.start.weekday() in req.days
    assert req.start > NOW


def test_the_title_is_the_activity_not_the_sentence(nl):
    """Defect 2: the whole utterance was stored as the title."""
    for phrase in (TURN_1, TURN_2):
        req = nl.parse_recurring_request(phrase, now=NOW)
        assert req.title == TITLE
        assert "every" not in req.title.lower()
        assert "615" not in req.title and "6.15" not in req.title


def test_a_weekday_set_gets_its_own_recurrence(nl):
    """Neither "weekdays" nor "weekly" can say Mon/Tue/Thu/Fri."""
    req = nl.parse_recurring_request(TURN_1, now=NOW)
    assert req.recurrence == "custom:mon,tue,thu,fri"


def test_the_existing_vocabulary_still_wins_when_it_fits(nl):
    """A custom recurrence is only written when nothing simpler can say it."""
    every_weekday = nl.parse_recurring_request(
        "add a reminder every weekday at 7:15 to take out the recycling",
        now=NOW)
    assert every_weekday.recurrence == "weekdays"
    one_day = nl.parse_recurring_request(
        "add a reminder every Tuesday at 7:15 to take out the recycling",
        now=NOW)
    assert one_day.recurrence == "weekly"
    all_week = nl.parse_recurring_request(
        "add a reminder every day at 7:15 to take out the recycling", now=NOW)
    assert all_week.recurrence == "daily"


# ── 2. what must NOT be claimed ─────────────────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "can you add 2 and 2",
    "tell me about the calendar",
    "what's on my calendar every Tuesday",
    "show me my calendar for every Friday at 5",
    "remind me to do the dishes",
    "remind me to call the dentist Monday at 3pm",
    "set a timer for 5 minutes",
    "set an alarm for every weekday at 6:30",
    "add milk to the shopping list",
    "remind me to take my blood pressure pill every day at 8",
    "every Tuesday is soccer practice",
    "add soccer practice",
    # A duration is not a recurrence: "days" here means one day.
    "add a reminder in 3 days at 5 to call mom",
    "remind me in a couple of days at 5 to call mom",
    "add a dentist appointment on October 3 at 2pm",
    "add a reminder tomorrow at 8 to call the school",
    "schedule a haircut on Monday at 3pm",
])
def test_the_fastpath_declines_what_it_must_not_own(nl, phrase):
    assert nl.parse_recurring_request(phrase, now=NOW) is None


def test_the_reminder_fastpath_keeps_its_own_rows(app, nl):
    """Nothing without a day set AND a time reaches the store through here."""
    assert nl.handle("remind me to do the dishes", app, now=NOW) is None
    assert nl.handle("can you add 2 and 2", app, now=NOW) is None
    assert _events(app) == []


# ── 3. one row, the right row ───────────────────────────────────────────────

def test_turn_one_lands_as_one_recurring_event(app, nl):
    reply = nl.handle(TURN_1, app, now=NOW)
    assert reply
    events = _events(app)
    assert len(events) == 1
    event = events[0]
    assert event["title"] == TITLE
    assert event["start"] == "2026-09-10T06:15"
    assert event["rec"] == "custom:mon,tue,thu,fri"
    # It is an EVENT, not a to-do.
    assert _tasks(app) == []


def test_the_confirmation_reads_back_the_days_and_the_time(app, nl):
    reply = nl.handle(TURN_1, app, now=NOW)
    for word in ("Monday", "Tuesday", "Thursday", "Friday", "6:15 AM"):
        assert word in reply, reply
    assert "Wednesday" not in reply
    assert TITLE in reply


def test_the_recurrence_expands_on_the_right_days(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    week = app.cal_occurrences(datetime(2026, 9, 14),
                               datetime(2026, 9, 20, 23, 59))
    starts = [row["start"] for row in week]
    assert starts == ["2026-09-14T06:15:00", "2026-09-15T06:15:00",
                      "2026-09-17T06:15:00", "2026-09-18T06:15:00"]
    assert all(row["title"] == TITLE for row in week)


def test_the_week_view_endpoint_expands_it_too(app, nl, monkeypatch):
    """`/apps/calendar/occurrences` is what the dashboard week view reads."""
    api = pytest.importorskip("puffpai.api_server")
    nl.handle(TURN_1, app, now=NOW)
    monkeypatch.setattr(api, "_get_app_data", lambda: app)
    api.app.config["TESTING"] = True
    with api.app.test_client() as client:
        page = client.get("/apps/calendar/occurrences"
                          "?from=2026-09-14&to=2026-09-20",
                          headers={"X-API-Key": api._API_KEY})
    assert page.status_code == 200, page.data
    rows = page.get_json()["occurrences"]
    assert [row["start"] for row in rows] == [
        "2026-09-14T06:15:00", "2026-09-15T06:15:00",
        "2026-09-17T06:15:00", "2026-09-18T06:15:00"]


# ── 4. saying it twice does not write it twice ──────────────────────────────

def test_the_second_phrasing_of_the_same_thing_adds_nothing(app, nl):
    first = nl.handle(TURN_1, app, now=NOW)
    second = nl.handle(TURN_2, app, now=NOW)
    assert len(_events(app)) == 1
    assert second != first, "the confirmation was a verbatim repeat"
    assert "already" in second.lower()


def test_the_follow_up_that_says_add_it_creates_nothing(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    second = nl.handle(TURN_2, app, now=NOW)
    third = nl.handle(TURN_3, app, now=NOW)
    assert third, "the follow-up was not claimed"
    assert len(_events(app)) == 1, "the follow-up duplicated the item"
    assert third != second
    assert "already on the calendar" in third.lower()
    assert TITLE in third


def test_a_follow_up_with_no_prior_item_is_declined(app, nl):
    assert nl.handle(TURN_3, app, now=NOW) is None
    assert nl.handle("put that on the calendar too", app, now=NOW) is None
    assert _events(app) == []


def test_a_stale_follow_up_is_not_attached_to_yesterdays_item(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    from puffpai import calendar_nl
    calendar_nl._LAST_ITEM["at"] -= 2 * calendar_nl._LAST_ITEM_TTL_SECONDS
    assert nl.handle(TURN_3, app, now=NOW) is None


# ── 5. changing it updates it ───────────────────────────────────────────────

def test_make_that_weekly_updates_instead_of_adding(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle("make that weekly", app, now=NOW)
    assert reply
    events = _events(app)
    assert len(events) == 1
    assert events[0]["rec"] == "weekly"
    assert "Thursday" in reply and "6:15 AM" in reply


def test_a_changed_day_set_is_a_second_item_unless_it_is_referred_to(app, nl):
    """A different day set is a DIFFERENT commitment.

    Updating the Mon, Tue, Thu, Fri row in place would silently delete four
    real mornings from the household's week on a title match alone.
    """
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle(
        "add a reminder every Tuesday and Thursday at 6:15 am to take the kids "
        "to soccer practice", app, now=NOW)
    events = _events(app)
    assert len(events) == 2, "the existing Mon, Tue, Thu, Fri row was overwritten"
    assert {e["rec"] for e in events} == {"custom:mon,tue,thu,fri",
                                          "custom:tue,thu"}
    assert "second" in reply.lower()
    assert "6:15 AM" in reply


def test_referring_to_the_item_updates_it_instead_of_adding(app, nl):
    """"that" points at the row the last turn made — then it IS an edit."""
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle("put that on the calendar every Tuesday and Thursday "
                      "at 6:15 am instead", app, now=NOW)
    events = _events(app)
    assert len(events) == 1, "a referential edit added a copy"
    assert events[0]["rec"] == "custom:tue,thu"
    assert events[0]["title"] == TITLE
    assert "updated" in reply.lower()


# ── 6. the recurrence model itself ──────────────────────────────────────────

def test_custom_recurrence_round_trips_through_the_store(app):
    from puffpai import calendar_dates
    app.cal_add_event("Soccer practice", "2026-09-14T06:15",
                      recurrence="custom:TUE, thu")
    stored = _events(app)[0]
    assert stored["rec"] == "custom:tue,thu"
    assert calendar_dates.custom_days(stored["rec"]) == {1, 3}


def test_an_unparseable_recurrence_is_still_dropped(app):
    app.cal_add_event("Nonsense", "2026-09-14T06:15",
                      recurrence="custom:someday")
    assert _events(app)[0]["rec"] is None


def test_an_unknown_recurrence_degrades_to_one_occurrence(app):
    """A reader that predates custom: must not invent wrong days."""
    from puffpai import calendar_dates
    rows = calendar_dates.expanded_occurrences(
        [{"title": "X", "start": "2026-09-14T06:15", "rec": "fortnightly"}],
        datetime(2026, 9, 1), datetime(2026, 10, 1))
    assert len(rows) == 1


def test_the_scheduler_fallback_knows_the_weekday_set():
    from puffpai import scheduler
    base = datetime(2026, 9, 15, 6, 15)          # a Tuesday
    nxt = scheduler.next_occurrence(base, "custom:tue,thu", base)
    assert nxt == datetime(2026, 9, 17, 6, 15)   # the Thursday


# ── 7. the surface: the fastpath is registered ahead of the task fastpath ───

@pytest.fixture()
def api(app, monkeypatch):
    import puffpai.api_server as api_server
    monkeypatch.setattr(api_server, "_get_app_data", lambda: app)
    return api_server


def test_the_api_fastpath_claims_the_live_sentence(api, app):
    reply = api._try_calendar_recurring(TURN_1)
    assert reply and "6:15 AM" in reply
    assert len(_events(app)) == 1


def test_the_api_fastpath_declines_the_rows_it_must_not_take(api, app):
    for phrase in ("remind me to do the dishes", "can you add 2 and 2",
                   "tell me about the calendar"):
        assert api._try_calendar_recurring(phrase) is None
    assert _events(app) == []


def test_the_instant_answer_layer_reaches_it_before_the_task_fastpath(api, app,
                                                                     monkeypatch):
    """_try_add_task must not take it and store the whole sentence as a task."""
    claimed = []

    def _never(message):
        claimed.append(message)
        return None

    monkeypatch.setattr(api, "_try_add_task", _never)
    reply = api._try_instant_answer(TURN_2)
    assert reply and "already" not in reply.lower()
    assert claimed == [], "_try_add_task was consulted for a recurring item"
    assert len(_events(app)) == 1
    assert _tasks(app) == []


def test_a_plain_reminder_still_reaches_the_task_fastpath(api, app,
                                                          monkeypatch):
    seen = []

    def _task(message):
        seen.append(message)
        return "Task added."

    monkeypatch.setattr(api, "_try_add_task", _task)
    assert api._try_instant_answer("remind me to do the dishes") == \
        "Task added."
    assert seen == ["remind me to do the dishes"]


def test_the_next_occurrence_never_lands_in_the_past(nl):
    """The start is the next matching day, not today-at-a-time-gone-by."""
    friday_night = datetime(2026, 9, 11, 22, 0)
    req = nl.parse_recurring_request(
        "add a reminder every Friday at 6:15 am to take the kids to soccer "
        "practice", now=friday_night)
    assert req.start > friday_night
    assert req.start.weekday() == 4
    assert req.start - friday_night < timedelta(days=8)


# ══════════════════════════════════════════════════════════════════════════
# Identity, ambiguous hours and deletion
# ══════════════════════════════════════════════════════════════════════════

SATURDAY = ("add a reminder every Saturday at 9 am to take the kids to soccer "
            "practice")


# ── 8. a title match is not an identity ─────────────────────────────────────

def test_a_second_practice_does_not_overwrite_the_first(app, nl):
    """A Saturday practice must not UPDATE the Mon, Tue, Thu, Fri 06:15 row."""
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle(SATURDAY, app, now=NOW)
    events = _events(app)
    assert len(events) == 2, "the Mon, Tue, Thu, Fri practice was overwritten"
    by_rec = {e["rec"]: e for e in events}
    assert set(by_rec) == {"custom:mon,tue,thu,fri", "weekly"}
    # The original is untouched, to the minute.
    assert by_rec["custom:mon,tue,thu,fri"]["start"] == "2026-09-10T06:15"
    # The new one is a Saturday at 9.
    saturday = datetime.fromisoformat(by_rec["weekly"]["start"])
    assert saturday.weekday() == 5
    assert (saturday.hour, saturday.minute) == (9, 0)


def test_the_reply_says_the_first_one_survived(app, nl):
    """A household that hears only "added" cannot know the other is still real."""
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle(SATURDAY, app, now=NOW)
    assert "second" in reply.lower()
    assert "Saturday" in reply and "9:00 AM" in reply
    assert "6:15 AM" in reply, "the untouched one was not mentioned"
    assert "alone" in reply.lower() or "unchanged" in reply.lower()


def test_a_time_and_days_with_no_activity_asks_and_writes_nothing(app, nl):
    """With no activity to name, nothing may be written or repurposed."""
    reply = nl.handle("add a reminder every Tuesday at 6", app, now=NOW)
    assert reply, "the turn was dropped instead of asking"
    assert "what is that for" in reply.lower()
    assert _events(app) == []
    assert _tasks(app) == []


def test_the_follow_up_memory_is_only_read_when_pointed_at(app, nl):
    """The same utterance, with a remembered item sitting there. Still asks."""
    nl.handle(TURN_1, app, now=NOW)
    before = _events(app)
    reply = nl.handle("add a reminder every Tuesday at 6", app, now=NOW)
    assert "what is that for" in reply.lower()
    assert _events(app) == before, "the remembered row was repurposed"


def test_make_that_weekly_still_updates_the_remembered_row(app, nl):
    """The referential case must keep working — it is the whole point."""
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle("make that weekly", app, now=NOW)
    events = _events(app)
    assert len(events) == 1
    assert events[0]["rec"] == "weekly"
    assert "changed how that repeats" in reply.lower()


def test_the_same_item_said_again_is_still_recognised(app, nl):
    """Tightening identity must not reintroduce the duplicate it replaced."""
    nl.handle(TURN_1, app, now=NOW)
    second = nl.handle(TURN_2, app, now=NOW)
    assert len(_events(app)) == 1
    assert "already" in second.lower()


# ── 9. an hour with no half of the day ──────────────────────────────────────

def test_a_bare_hour_asks_which_half_of_the_day(app, nl):
    """A bare 6 is never silently written as 6:00 AM."""
    reply = nl.handle("remind me every Tuesday at 6 to take the bins out",
                      app, now=NOW)
    assert reply
    assert "6 AM or 6 PM" in reply
    assert _events(app) == [], "an ambiguous request still wrote a row"
    assert _tasks(app) == []


def test_a_stated_meridiem_is_taken_at_its_word(app, nl):
    nl.handle("remind me every Tuesday at 6 pm to take the bins out",
              app, now=NOW)
    events = _events(app)
    assert len(events) == 1
    assert datetime.fromisoformat(events[0]["start"]).hour == 18


def test_a_daypart_settles_it_without_asking(app, nl):
    nl.handle("remind me every Tuesday morning at 6 to take the bins out",
              app, now=NOW)
    events = _events(app)
    assert len(events) == 1
    assert datetime.fromisoformat(events[0]["start"]).hour == 6


def test_the_transcript_turns_are_not_treated_as_ambiguous(nl):
    """"morning at 6.15" and "every weekday morning... at 615" both say AM."""
    for phrase in (TURN_1, TURN_2):
        req = nl.parse_recurring_request(phrase, now=NOW)
        assert req.ambiguous is False
        assert (req.hour, req.minute) == (6, 15)


def test_a_custom_recurrence_keeps_its_day_when_it_is_normalised():
    """The latent trap: normalise_recurrence("custom:tue") returned "weekly"."""
    from puffpai import calendar_dates
    assert calendar_dates.normalise_recurrence("custom:tue") == "custom:tue"
    assert calendar_dates.normalise_recurrence("custom:THU, tue") == \
        "custom:tue,thu"
    assert calendar_dates.normalise_recurrence("weekly") is None


def test_a_single_day_custom_recurrence_survives_the_store(app):
    from puffpai import calendar_dates
    app.cal_add_event("Piano", "2026-09-15T16:00", recurrence="custom:tue")
    stored = _events(app)[0]
    assert stored["rec"] == "custom:tue"
    rows = calendar_dates.expanded_occurrences(
        [stored], datetime(2026, 9, 14), datetime(2026, 9, 28))
    assert [when.weekday() for when, _row in rows] == [1, 1]


# ── 10. deleting actually deletes, and says what it deleted ─────────────────

def _add_practice_task(app):
    """A to-do of the same name — cal_add_task also writes a mirror event."""
    return app.cal_add_task(TITLE, "2026-09-10T07:00")


def test_deleting_the_only_match_removes_it_and_confirms_what_it_was(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle_delete("delete the soccer practice event", app)
    assert reply, "the delete was not claimed"
    assert _events(app) == [], reply
    assert "deleted" in reply.lower()
    assert TITLE in reply
    assert "recurring event" in reply.lower()
    assert "6:15 AM" in reply
    assert "nothing else by that name" in reply.lower()


def test_a_task_and_its_mirror_event_are_one_thing(app, nl):
    """cal_delete_event offered "'X', 'X'" and removed nothing."""
    _add_practice_task(app)
    assert len(_events(app)) == 1 and len(_tasks(app)) == 1
    reply = nl.handle_delete("delete the soccer practice reminder", app)
    assert reply
    assert "which one" not in reply.lower(), reply
    assert _tasks(app) == []
    assert _events(app) == [], "the mirror event was left behind"
    assert "a task due September 10" in reply


def test_two_different_things_disambiguate_by_kind_and_schedule(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    reply = nl.handle_delete("delete the soccer practice reminder", app)
    assert reply
    assert "a task due September 10" in reply
    assert "a recurring event" in reply
    assert "6:15 AM" in reply
    assert "both" in reply.lower()
    # Nothing was removed while the question is open.
    assert len(_tasks(app)) == 1
    assert len(_events(app)) == 2


def test_saying_both_deletes_both(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    nl.handle_delete("delete the soccer practice reminder", app, CONV)
    reply = nl.handle_delete("both", app, CONV)
    assert reply
    assert _tasks(app) == []
    assert _events(app) == []
    assert "nothing else by that name" in reply.lower()


def test_saying_all_of_them_deletes_both(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    nl.handle_delete("delete the soccer practice reminder", app, CONV)
    nl.handle_delete("all of them", app, CONV)
    assert _tasks(app) == [] and _events(app) == []


def test_naming_one_kind_deletes_only_that_one(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    nl.handle_delete("delete the soccer practice reminder", app, CONV)
    reply = nl.handle_delete("the task", app, CONV)
    assert reply
    assert _tasks(app) == []
    events = _events(app)
    assert len(events) == 1, "the recurring event went too"
    assert events[0]["rec"] == "custom:mon,tue,thu,fri"
    assert "still there" in reply.lower()
    assert "a recurring event" in reply


def test_the_confirmation_is_read_back_from_the_store(app, nl, monkeypatch):
    """The reply never says "deleted" about a row that is still there."""
    nl.handle(TURN_1, app, now=NOW)
    monkeypatch.setattr(type(app), "cal_delete_event",
                        lambda self, ident: "Deleted 'whatever'.")
    reply = nl.handle_delete("delete the soccer practice event", app)
    assert reply
    assert "couldn't remove" in reply.lower(), reply
    assert "deleted" not in reply.lower().split("couldn't")[0]
    assert len(_events(app)) == 1


def test_deleting_something_that_is_not_there_is_declined(app, nl):
    assert nl.handle_delete("delete the soccer practice event", app) is None
    assert nl.handle_delete("delete the dentist appointment", app) is None


def test_the_delete_handler_never_takes_another_app_s_words(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    for phrase in ("delete the alarm",
                   "cancel the 6 am alarm",
                   "cancel the timer",
                   "remove milk from the shopping list",
                   "delete my blood pressure pill reminder",
                   "delete everything",
                   "delete all my events",
                   "clear the whole calendar",
                   "how do I delete an event"):
        assert nl.handle_delete(phrase, app) is None, phrase
    assert len(_events(app)) == 1, "another app's phrasing deleted a row"


def test_a_bare_delete_names_nothing_and_is_declined(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    for phrase in ("delete it", "remove that", "delete the event",
                   "cancel the reminder", "delete", "remove the calendar"):
        assert nl.handle_delete(phrase, app) is None, phrase
    assert len(_events(app)) == 1


# ── 11. the surface ─────────────────────────────────────────────────────────

def test_the_api_delete_fastpath_claims_it(api, app, nl):
    nl.handle(TURN_1, app, now=NOW)
    reply = api._try_calendar_delete("delete the soccer practice event")
    assert reply and "deleted" in reply.lower()
    assert _events(app) == []


def test_the_api_delete_fastpath_leaves_alarm_phrases_alone(api, app, nl):
    nl.handle(TURN_1, app, now=NOW)
    assert api._try_calendar_delete("delete the alarm") is None
    assert api._try_calendar_delete("cancel my 6:30 alarm") is None
    assert len(_events(app)) == 1


def test_the_instant_answer_layer_reaches_the_delete_fastpath(api, app, nl,
                                                              monkeypatch):
    nl.handle(TURN_1, app, now=NOW)
    monkeypatch.setattr(api, "_try_add_task", lambda message: None)
    reply = api._try_instant_answer("delete the soccer practice event")
    assert reply and "deleted" in reply.lower()
    assert _events(app) == []


@pytest.fixture(autouse=True)
def clean_pending_delete():
    from puffpai import calendar_nl
    calendar_nl.forget_pending_delete()
    yield
    calendar_nl.forget_pending_delete()


# ── 12. referential edits, word-level matching and the daypart title ───────

def test_a_referential_edit_moves_the_row_the_last_turn_made(app, nl):
    """Row A must survive untouched.

    "that" points at the SATURDAY reminder turn 2 has just created. Rewriting
    ``candidates[0]`` instead would hit the OLDEST title match, which is the
    Mon, Tue, Thu, Fri 06:15 row: four real mornings, silently gone.
    """
    nl.handle(TURN_1, app, now=NOW)                       # row A, Mon/Tue/Thu/Fri 6:15
    nl.handle("add a reminder every Saturday at 9 am to take the kids to soccer "
              "practice", app, now=NOW)              # row B, Sat 9:00
    row_a = next(e for e in _events(app) if e["rec"].startswith("custom:"))
    row_b = next(e for e in _events(app) if e["start"].endswith("T09:00"))

    reply = nl.handle("put that on the calendar every Sunday at 9:30 am "
                      "instead", app, now=NOW)
    assert "updated" in reply.lower(), reply
    events = {e["id"]: e for e in _events(app)}
    assert len(events) == 2, "the referential edit added or dropped a row"
    assert events[row_a["id"]]["rec"] == "custom:mon,tue,thu,fri"
    assert events[row_a["id"]]["start"] == row_a["start"]
    assert events[row_b["id"]]["rec"] == "weekly"
    assert events[row_b["id"]]["start"].endswith("T09:30")


def test_a_referential_edit_asks_when_the_remembered_row_is_gone(app, nl):
    """Never candidates[0] as a consolation prize: ask instead."""
    nl.handle(TURN_1, app, now=NOW)
    row_a = _events(app)[0]
    nl.handle("add a reminder every Saturday at 9 am to take the kids to soccer "
              "practice", app, now=NOW)
    row_b = next(e for e in _events(app) if e["start"].endswith("T09:00"))
    app.cal_delete_event(row_b["id"])

    reply = nl.handle("put that on the calendar every Sunday at 9:30 am "
                      "instead", app, now=NOW)
    assert reply and "not sure which one" in reply.lower(), reply
    events = _events(app)
    assert len(events) == 1
    assert events[0]["id"] == row_a["id"]
    assert events[0]["rec"] == "custom:mon,tue,thu,fri"


@pytest.mark.parametrize("phrase", [
    "delete the kids soccer reminder",
    "delete the kids soccer practice",
])
def test_a_phrase_that_is_not_a_substring_still_names_the_row(app, nl, phrase):
    """Every word said is IN the title — that is the same thing named.

    Neither phrase is a substring of "Take the kids to soccer practice", so
    substring matching resolves both to nothing and the delete silently
    declines.
    """
    nl.handle(TURN_1, app, now=NOW)
    reply = nl.handle_delete(phrase, app, CONV)
    assert reply and "deleted" in reply.lower(), reply
    assert _events(app) == []


def test_token_matching_does_not_widen_the_vetoes(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    for phrase in ("delete the alarm", "delete everything",
                   "delete all my events", "clear the whole calendar",
                   "delete all of the reminders"):
        assert nl.handle_delete(phrase, app, CONV) is None, phrase
    assert len(_events(app)) == 1


def test_token_matching_needs_every_word_and_at_least_two(app):
    app.cal_add_event(TITLE, "2026-09-14T06:15")
    app.cal_add_event("Water the plants", "2026-09-15T18:00")
    # A word that is NOT in the title means it is not that thing.
    assert app.cal_find_items("kids piano practice") == []
    # One significant word never reaches the token path at all.
    assert app.cal_find_items("reminder plants") == []
    # The plants row is still found by its own two words, said in any order.
    assert len(app.cal_find_items("plants water")) == 1


def test_an_immediate_answer_still_deletes(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    assert nl.handle_delete("delete the soccer practice reminder", app,
                            CONV)
    reply = nl.handle_delete("both", app, CONV)
    assert reply and _tasks(app) == [] and _events(app) == []


def test_an_intervening_turn_closes_the_prompt(app, nl):
    """A "both" that does not answer THIS turn's question deletes nothing."""
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    assert nl.handle_delete("delete the soccer practice reminder", app,
                            CONV)
    nl.note_turn("what's the weather like", CONV)     # any other turn at all
    assert nl.handle_delete("both", app, CONV) is None
    assert len(_tasks(app)) == 1 and len(_events(app)) == 2


def test_another_conversation_cannot_answer_the_prompt(app, nl):
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    assert nl.handle_delete("delete the soccer practice reminder", app,
                            CONV)
    assert nl.handle_delete("both", app, OTHER_CONV) is None
    assert len(_tasks(app)) == 1 and len(_events(app)) == 2


def test_a_prompt_with_no_conversation_cannot_be_answered_at_all(app, nl):
    """No id to scope it to — ask, but never resolve a later bare word."""
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    assert nl.handle_delete("delete the soccer practice reminder", app)
    assert nl.handle_delete("both", app) is None
    assert len(_tasks(app)) == 1 and len(_events(app)) == 2


def test_the_prompt_expires_after_two_minutes(app, nl, monkeypatch):
    import time as _time
    nl.handle(TURN_1, app, now=NOW)
    _add_practice_task(app)
    assert nl.handle_delete("delete the soccer practice reminder", app,
                            CONV)
    assert nl._PENDING_DELETE_TTL_SECONDS == 120
    real = _time.time
    monkeypatch.setattr(nl.time, "time", lambda: real() + 121)
    assert nl.handle_delete("both", app, CONV) is None
    assert len(_tasks(app)) == 1 and len(_events(app)) == 2


@pytest.mark.parametrize("phrase,title", [
    ("add a reminder every day at 6 in the evening to take the bins out",
     "Take the bins out"),
    ("add a reminder every Saturday at 7 in the morning to feed the cat",
     "Feed the cat"),
    ("add a reminder every Friday at 6 tonight to take the bins out",
     "Take the bins out"),
    ("add a reminder every Tuesday after school at 4 to walk the dog",
     "Walk the dog"),
])
def test_a_trailing_daypart_is_not_part_of_the_title(nl, phrase, title):
    """"In the evening to take the bins out" must not become a row title."""
    req = nl.parse_recurring_request(phrase, now=NOW)
    assert req is not None, phrase
    assert req.title == title


def test_a_daypart_that_is_the_activity_keeps_its_name(nl):
    req = nl.parse_recurring_request(
        "add a reminder every Tuesday at 4 pm for evening yoga", now=NOW)
    assert req is not None and req.title == "Evening yoga"
