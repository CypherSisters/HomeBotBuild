"""Spoken-reply cosmetics: things a voice reply must not sound like.

* "what's the temperature in the living room" must not read a thermostat and
  its own companion sensor as a list ("The living room is, Living Room 75
  degrees; Living Room Current Temperature 75.2 degrees Fahrenheit.").
* A task confirmation is a sentence, not field labels ("Task added: do the
  dishes, due September 13, assigned to all").
* "how many items are on the list" answers with the number, not the number
  AND every name.
* "what can you do" marks "web tools" as off while chat turns are not allowed
  online.
* Stripping the "[Trivia ...]" label must not leave its full stop behind.
"""

from datetime import datetime, timedelta

import pytest

from puffpai import capability_registry as capreg
from puffpai import voice_text
from puffpai.agents import household_reads as hr


# ── one room, one sentence ───────────────────────────────────────────────

class TempBridge:
    def __init__(self, states):
        self._states = states

    def get_all_states(self):
        return self._states


LIVING_ROOM = [
    {"entity_id": "climate.living_room",
     "attributes": {"friendly_name": "Living Room",
                    "current_temperature": 75, "temperature": 75}},
    {"entity_id": "sensor.living_room_current_temperature", "state": "75.2",
     "attributes": {"friendly_name": "Living Room Current Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°F"}},
]


def test_the_living_room_reads_one_sentence_not_two_entities():
    reply = hr.room_temperature(bridge=TempBridge(LIVING_ROOM),
                                room="living room")
    assert ";" not in reply
    assert reply.count("Living Room") == 1
    assert "75" in reply


def test_the_thermostat_is_what_the_room_reads():
    reply = hr.room_temperature(bridge=TempBridge(LIVING_ROOM),
                                room="living room")
    assert "Current Temperature" not in reply


def test_two_sensors_that_agree_are_read_once():
    states = [
        {"entity_id": "sensor.den_temp", "state": "68",
         "attributes": {"friendly_name": "Den Temp",
                        "device_class": "temperature"}},
        {"entity_id": "sensor.den_temperature", "state": "68.4",
         "attributes": {"friendly_name": "Den Temperature",
                        "device_class": "temperature"}},
    ]
    reply = hr.room_temperature(bridge=TempBridge(states), room="den")
    assert ";" not in reply


def test_a_whole_house_read_still_lists_every_room():
    states = [
        {"entity_id": "sensor.den_temp", "state": "68",
         "attributes": {"friendly_name": "Den", "device_class": "temperature"}},
        {"entity_id": "sensor.office_temp", "state": "71",
         "attributes": {"friendly_name": "Office",
                        "device_class": "temperature"}},
    ]
    reply = hr.room_temperature(bridge=TempBridge(states))
    assert "Den" in reply and "Office" in reply


# ── a count answers with the number ──────────────────────────────────────

class ListApp:
    def __init__(self, n):
        self.n = n

    def fresh_shopping_items(self):
        return [{"name": f"SAMPLE item {i}"} for i in range(self.n)]


def test_how_many_items_answers_with_the_number_only():
    reply = hr.answer("how many items are on the shopping list",
                      app=ListApp(41))
    assert reply == "There are 41 items on your shopping list."


def test_the_contents_read_still_names_the_items():
    reply = hr.shopping_status(app=ListApp(3))
    assert "SAMPLE item 0" in reply


def test_one_item_still_reads_as_one():
    reply = hr.answer("how many things are on the list", app=ListApp(1))
    assert reply == "There is 1 item on your shopping list."


# ── a task confirmation is a sentence ────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


def _add_task(app, due):
    return app.cal_add_task("Sisters coming over", due)


def test_a_task_due_today_says_today(app):
    today = datetime.now().date().isoformat()
    assert _add_task(app, today) == \
        "Added a task: Sisters coming over, due today."


def test_a_task_due_tomorrow_says_tomorrow(app):
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    assert _add_task(app, tomorrow) == \
        "Added a task: Sisters coming over, due tomorrow."


def test_a_task_further_out_keeps_its_date(app):
    later = (datetime.now() + timedelta(days=9))
    reply = _add_task(app, later.date().isoformat())
    assert reply.startswith("Added a task: Sisters coming over, due ")
    assert "assigned to" not in reply
    assert later.strftime("%B") in reply


def test_a_person_named_on_purpose_is_still_named(app):
    reply = app.cal_add_task("Sisters coming over",
                             datetime.now().date().isoformat(),
                             assigned_to="Alex")
    assert reply == ("Added a task: Sisters coming over, due today, "
                     "for Alex.")


# ── web tools are marked off when this turn cannot go online ─────────────

def test_the_overview_marks_web_tools_off_when_chat_may_not_go_online(
        monkeypatch):
    import puffpai.security.egress_doors as ed
    monkeypatch.setattr(ed, "egress_allowed", lambda door_id, *a, **k: (True, ""))
    monkeypatch.setattr(capreg, "_chat_egress_on", lambda: False)
    said = capreg.describe_for("what can you do")
    assert "web tools" + capreg.EGRESS_OFF_NOTE in said


def test_both_halves_open_makes_the_plain_claim(monkeypatch):
    import puffpai.security.egress_doors as ed
    monkeypatch.setattr(ed, "egress_allowed", lambda door_id, *a, **k: (True, ""))
    monkeypatch.setattr(capreg, "_chat_egress_on", lambda: True)
    said = capreg.describe_for("what can you do")
    assert "and web tools." in said


def test_an_unreadable_opt_in_does_not_invent_an_off(monkeypatch):
    monkeypatch.setattr(capreg, "_chat_egress_on", capreg._chat_egress_on)
    import puffpai.security.egress_policy as ep
    def _boom(policy_id):
        raise RuntimeError("registry unreadable")
    monkeypatch.setattr(ep, "enabled", _boom)
    assert capreg._chat_egress_on() is True


# ── the stripped label leaves no punctuation behind ──────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("[Trivia — General Knowledge]. Which planet is closest to the sun?",
     "Which planet is closest to the sun?"),
    ("[Trivia — General Knowledge]\nWhich planet is closest to the sun?",
     "Which planet is closest to the sun?"),
    (". Which planet is closest to the sun?",
     "Which planet is closest to the sun?"),
])
def test_the_label_takes_its_punctuation_with_it(raw, expected):
    assert voice_text.voice_cosmetics(raw) == expected


def test_ordinary_speech_is_untouched():
    assert voice_text.voice_cosmetics("Timer set for 2 minutes.") == \
        "Timer set for 2 minutes."
