"""D3b: the thermostat follow-up must keep the room.

Live, in ONE conversation id::

    "what's the temperature in the living room" -> "The living room is 73..."
    "set the thermostat to 74" -> "Which thermostat do you
                                                     mean: Upstairs
                                                     Bedroom, Living Room?"

The carry was built as ``climate_writes.carried_targets(history,...)``,
reading the room back out of ``conversation_history``. The spoken surface
(``/api/chat``) builds that history from the messages in the REQUEST BODY, and
the voice loop sends one message per turn — so on the surface that actually
speaks there was never any history to read, however stable the conversation id
was. The room is now recorded by the READ in:mod:`puffpai.turn_context`
(keyed by conversation id, TTL'd) and borrowed by the SET.

Both turns run through the hook layer — ``api_server._try_household_read`` then
``api_server._try_climate_set`` — against a fake bridge, because the defect was
in what one hook left behind for the next one, not in either reader.
"""

import pytest

from puffpai import api_server, turn_context
from puffpai.agents import climate_writes


class FakeBridge:
    """Two thermostats, named the way this household's really are."""

    def __init__(self):
        self.setpoints = {"climate.living_room": 73,
                          "climate.bedroom": 72}
        self.currents = {"climate.living_room": 73,
                         "climate.bedroom": 72}
        self.names = {"climate.living_room": "Living Room",
                      "climate.bedroom": "Upstairs Bedroom"}
        self.writes = []

    def _row(self, entity_id):
        return {
            "entity_id": entity_id,
            "state": "heat",
            "attributes": {
                "friendly_name": self.names[entity_id],
                "temperature": self.setpoints[entity_id],
                "current_temperature": self.currents[entity_id],
            },
        }

    def get_all_states(self):
        return [self._row(e) for e in self.setpoints]

    def get_entity_state(self, entity_id):
        return self._row(entity_id)

    def set_climate(self, entity_id, temperature=None, **_kw):
        self.writes.append((entity_id, temperature))
        self.setpoints[entity_id] = temperature
        return {"ok": True}


@pytest.fixture
def house(monkeypatch):
    bridge = FakeBridge()

    class _Tools:
        _ha_bridge = bridge

    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: bridge)
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _Tools())
    turn_context.reset()
    yield bridge
    turn_context.reset()


def _read(message="what's the temperature in the living room"):
    return api_server._try_household_read(message)


def _set(message="set the thermostat to 74"):
    return api_server._try_climate_set(message, None)


def test_the_read_answers_about_the_room(house):
    said = _read()
    assert said and "73" in said
    assert "climate." not in said


def test_the_follow_up_set_uses_the_room_the_read_named(house):
    turn_context.begin("conv-d3b")
    assert _read() is not None
    said = _set()
    assert said == "Living Room set to 74."
    assert "Which thermostat" not in said
    assert house.writes == [("climate.living_room", 74)]


def test_without_a_previous_read_it_still_asks(house):
    turn_context.begin("conv-cold")
    said = _set()
    assert said.startswith("Which thermostat do you mean")
    assert house.writes == []


def test_the_room_does_not_leak_into_another_conversation(house):
    turn_context.begin("conv-a")
    _read()
    turn_context.begin("conv-b")
    said = _set()
    assert said.startswith("Which thermostat do you mean")
    assert house.writes == []


def test_the_room_goes_cold(house, monkeypatch):
    turn_context.begin("conv-stale")
    _read()
    assert turn_context.last_room("conv-stale") == "living room"
    monkeypatch.setattr(turn_context, "ROOM_TTL", -1.0)
    assert turn_context.last_room("conv-stale") is None
    assert climate_writes.remembered_targets(
        climate_writes.list_thermostats(house)) == []


def test_a_named_room_still_wins_over_the_remembered_one(house):
    turn_context.begin("conv-named")
    _read()
    said = _set("set the bedroom to 70")
    assert said == "Upstairs Bedroom set to 70."
    assert house.writes == [("climate.bedroom", 70)]


def test_the_read_records_only_the_room_word(house):
    turn_context.begin("conv-word")
    _read()
    assert turn_context.last_room("conv-word") == "living room"


def test_a_question_that_names_no_room_records_nothing(house):
    turn_context.begin("conv-none")
    api_server._try_household_read("what's the temperature in here")
    assert turn_context.last_room("conv-none") is None
