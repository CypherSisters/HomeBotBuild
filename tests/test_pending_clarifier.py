"""A clarifying question remembers what it asked.

Three answers that must reach the question that asked for them:

* "delete the soccer reminder" asks which of two events to remove; the answer
  "the every-weekday one" must delete that event, not be routed as a fresh
  query (which answers with a dictionary definition of "every weekday").
* "set an alarm for seven" asks "7 AM or 7 PM?"; "PM" must set the alarm, not
  come back as post-mortem / prime minister / private message.
* the medication reminder asks for a name, a dose and a time, and must not
  write a row while it is still asking.

The mechanism under test is :mod:`puffpai.pending_clarifier`: the hook that
ASKS registers what it asked and how to finish it, and the answer completes the
ORIGINAL intent instead of re-entering routing.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from puffpai import pending_clarifier as pc      # noqa: E402

CONV = "conv-first"
OTHER = "conv-somebody-else"


@pytest.fixture(autouse=True)
def clean_records():
    pc.reset()
    yield
    pc.reset()


def _options():
    return [
        {"label": "a recurring event, every Monday, Tuesday, Thursday and "
                  "Friday at 6:15 AM", "value": "mon-tue-thu-fri"},
        {"label": "a recurring event, every weekday at 6:15 AM",
         "value": "weekdays"},
    ]


def _values(chosen):
    return [c["value"] for c in (chosen or [])]


# ── A. which option did the household name? (pure) ──────────────────────────

@pytest.mark.parametrize("said, expected", [
    ("the every-weekday one", ["weekdays"]),
    ("every weekday", ["weekdays"]),
    ("the Tuesday one", ["mon-tue-thu-fri"]),
    ("the first one", ["mon-tue-thu-fri"]),
    ("the second one", ["weekdays"]),
    ("the last one", ["weekdays"]),
    ("2", ["weekdays"]),
    ("number 1", ["mon-tue-thu-fri"]),
    ("both", ["mon-tue-thu-fri", "weekdays"]),
    ("all of them", ["mon-tue-thu-fri", "weekdays"]),
])
def test_an_answer_names_exactly_one_of_the_options(said, expected):
    assert _values(pc.match_options(_options(), said)) == expected


@pytest.mark.parametrize("said", [
    "what's the weather tomorrow",          # a change of subject
    "at 6:15 AM",                           # true of BOTH candidates
    "the recurring one",                    # true of both; calendar_nl owns it
    "",
    "9",                                    # no ninth option
])
def test_anything_that_does_not_choose_is_not_guessed_at(said):
    assert pc.match_options(_options(), said) is None


def test_am_and_pm_are_answers(monkeypatch):
    options = [{"label": "AM", "value": "am", "tokens": ["am", "morning"]},
               {"label": "PM", "value": "pm",
                "tokens": ["pm", "evening", "night"]}]
    assert _values(pc.match_options(options, "PM")) == ["pm"]
    assert _values(pc.match_options(options, "7 PM")) == ["pm"]
    assert _values(pc.match_options(options, "in the evening")) == ["pm"]
    assert _values(pc.match_options(options, "AM")) == ["am"]
    assert _values(pc.match_options(options, "the morning one")) == ["am"]


def test_that_one_points_at_nothing_when_there_are_two_of_them():
    assert pc.match_options(_options(), "that one") is None
    single = [{"label": "the 7 PM alarm", "value": "alarm"}]
    assert _values(pc.match_options(single, "that one")) == ["alarm"]


# ── B. the record: register, resolve, forget ────────────────────────────────

def _register(done, options=None, conversation_id=CONV):
    def _complete(chosen, said):
        done.append((_values(chosen), said))
        return "deleted %s" % ",".join(_values(chosen))
    pc.register("test", _complete, options if options is not None else
                _options(), conversation_id=conversation_id)


def test_the_answer_completes_the_original_intent():
    done = []
    _register(done)
    assert pc.resolve("the every-weekday one", CONV) == "deleted weekdays"
    assert done == [(["weekdays"], "the every-weekday one")]


def test_a_record_is_single_use():
    done = []
    _register(done)
    assert pc.resolve("the every-weekday one", CONV)
    assert pc.resolve("the every-weekday one", CONV) is None
    assert len(done) == 1


def test_a_change_of_subject_clears_the_record_and_routes_normally():
    done = []
    _register(done)
    assert pc.resolve("what's the weather tomorrow", CONV) is None
    assert done == []
    # And the question is GONE: the answer does not attach itself two turns on.
    assert pc.peek(CONV) is None
    assert pc.resolve("the every-weekday one", CONV) is None


def test_a_question_expires(monkeypatch):
    done = []
    _register(done)
    monkeypatch.setattr(pc.time, "time",
                        lambda base=time.time(): base + pc.PENDING_TTL + 1)
    assert pc.peek(CONV) is None
    assert pc.resolve("the every-weekday one", CONV) is None
    assert done == []


def test_an_answer_only_resolves_its_own_conversation():
    done = []
    _register(done)
    assert pc.resolve("the every-weekday one", OTHER) is None
    assert done == []
    assert pc.resolve("the every-weekday one", CONV) == "deleted weekdays"


def test_a_surface_with_no_conversation_id_can_never_be_answered():
    """An anonymous question is asked, but no stray "both" may resolve it."""
    done = []
    _register(done, conversation_id="")
    assert pc.resolve("both", "") is None
    assert pc.resolve("both", CONV) is None
    assert done == []


def test_a_completion_that_asks_again_survives_the_single_use_clear():
    """The medication answer re-asks when it is still missing the time."""
    asked = []

    def _complete(chosen, said):
        asked.append(said)
        if "7" not in said:
            pc.register("test", _complete, conversation_id=CONV)
            return "I still need what time."
        return "written"

    pc.register("test", _complete, conversation_id=CONV)
    assert pc.resolve("lisinopril", CONV) == "I still need what time."
    assert pc.resolve("7 AM", CONV) == "written"
    assert asked == ["lisinopril", "7 AM"]


# ── C. the live two-step beats, end to end ──────────────────────────────────

class _FakeAlarmTool:
    def __init__(self, sink):
        self.sink = sink

    def invoke(self, payload):
        self.sink.append(payload)
        hour = payload["hour"]
        return "Alarm set: %s, ringing at %d:%02d %s." % (
            payload["description"], hour % 12 or 12, payload["minute"],
            "AM" if hour < 12 else "PM")


def test_c7_answering_pm_sets_the_alarm_the_question_was_about(monkeypatch):
    """"set an alarm for seven" then "PM" — one alarm, at 19:00."""
    import puffpai.api_server as api
    from puffpai import turn_context

    written: list = []
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: type("T", (), {"tool_dict": {
                            "app_set_alarm": _FakeAlarmTool(written)}})())
    monkeypatch.setattr(api, "_app_installed", lambda name: True)
    turn_context.begin(CONV)

    asked = api._try_alarm_clarify("set an alarm for seven")
    assert "7 AM or 7 PM" in asked
    assert written == [], "an ambiguous alarm must write nothing"

    reply = api._try_instant_answer("PM", conversation_id=CONV)
    assert reply and "7:00 PM" in reply
    assert written == [{"description": "Alarm", "hour": 19, "minute": 0,
                        "repeat": "once"}]


def test_c7_a_change_of_subject_after_the_question_writes_no_alarm(monkeypatch):
    import puffpai.api_server as api
    from puffpai import turn_context

    written: list = []
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: type("T", (), {"tool_dict": {
                            "app_set_alarm": _FakeAlarmTool(written)}})())
    turn_context.begin(CONV)
    api._try_alarm_clarify("set an alarm for seven")
    assert api._try_pending_clarifier("never mind, what time is it",
                                      CONV) is None
    assert written == []
    assert pc.peek(CONV) is None


TITLE = "Take the kids to soccer practice"


@pytest.fixture()
def calendar(tmp_path, monkeypatch):
    """Real AppData on a temp dir, with the two same-named recurring events."""
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    app = app_data_module.AppData()
    start = datetime(2026, 9, 14, 6, 15).isoformat(timespec="minutes")
    app.cal_add_event(TITLE, start, recurrence="custom:mon,tue,thu,fri")
    app.cal_add_event(TITLE, start, recurrence="weekdays")
    return app


def _events(app):
    return (app.get_data("calendar") or {}).get("events", [])


def test_the_every_weekday_one_deletes_that_one(calendar, monkeypatch):
    """Both turns of the delete conversation, through the instant-answer layer."""
    import puffpai.api_server as api
    from puffpai import calendar_nl

    calendar_nl.forget_pending_delete()
    monkeypatch.setattr(api, "_get_app_data", lambda: calendar)

    asked = api._try_calendar_delete("delete the soccer reminder", CONV)
    assert asked and "which one should i delete" in asked.lower()
    assert len(_events(calendar)) == 2, "nothing goes while the question is open"

    reply = api._try_instant_answer("the every-weekday one",
                                    conversation_id=CONV)
    assert reply, "the answer fell through to routing again"
    assert "deleted" in reply.lower()
    left = _events(calendar)
    assert [e["rec"] for e in left] == ["custom:mon,tue,thu,fri"], reply


def test_the_answer_is_not_honoured_in_another_conversation(calendar,
                                                               monkeypatch):
    import puffpai.api_server as api
    from puffpai import calendar_nl

    calendar_nl.forget_pending_delete()
    monkeypatch.setattr(api, "_get_app_data", lambda: calendar)
    api._try_calendar_delete("delete the soccer reminder", CONV)
    assert api._try_pending_clarifier("the every-weekday one", OTHER) is None
    assert len(_events(calendar)) == 2


def test_a_pin_waiting_elsewhere_does_not_take_the_answer(calendar,
                                                          monkeypatch):
    """The PIN hook runs first, and the answer still finishes the delete."""
    import puffpai.api_server as api
    from puffpai import calendar_nl

    calendar_nl.forget_pending_delete()
    monkeypatch.setattr(api, "_get_app_data", lambda: calendar)
    api._ha_pin_reset()
    try:
        api._ha_pin_slot_add("automation.puffpai_waiting", OTHER)
        api._try_calendar_delete("delete the soccer reminder", CONV)
        reply = api._try_instant_answer("the every-weekday one",
                                        conversation_id=CONV)
        assert reply and "deleted" in reply.lower(), reply
        left = _events(calendar)
        assert [e["rec"] for e in left] == ["custom:mon,tue,thu,fri"], reply
        assert api._ha_pin_slot_ids(OTHER) == ["automation.puffpai_waiting"]
    finally:
        api._ha_pin_reset()


def test_the_clarifier_runs_before_every_hook_that_hears_requests():
    """Structural: an answer must not be seen by a rule that hears requests.

    The one hook allowed ahead of the clarifier is the PIN reply to an approved
    Home Assistant change that waits for the vault. Every chat entry point runs
    that same rule before routing, and it has to mark a PIN turn private before
    any other rule reads the text. It hears no requests: it declines unless a
    change is waiting for a PIN, and while one waits it still declines an
    answer with no numbers in it, so the clarifier gets that answer.
    """
    import ast
    import inspect

    import puffpai.api_server as api

    src = inspect.getsource(api._try_instant_answer)
    calls = sorted((n.lineno, n.func.id) for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id.startswith("_try_"))
    order = [name for _lineno, name in calls]
    assert order[:2] == ["_try_ha_change_pin_followup",
                         "_try_pending_clarifier"], order[:4]

    answers = ("the every-weekday one", "PM", "both", "the first one")
    api._ha_pin_reset()
    try:
        for answer in answers:
            assert api._try_ha_change_pin_followup(answer, CONV) is None, answer
        api._ha_pin_slot_add("automation.puffpai_waiting", OTHER)
        for answer in answers:
            assert api._try_ha_change_pin_followup(answer, CONV) is None, answer
    finally:
        api._ha_pin_reset()
