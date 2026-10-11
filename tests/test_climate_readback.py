"""D3c / D3b: a thermostat set is confirmed by reading each thermostat back.



* D3c "living room and bedroom to 74" → "The thermostat in the living room and
  bedroom is now set to 74." Verified after: living room 74, bedroom **73**.
* D3b "what's the temperature in the living room" → "set the thermostat to 74"
  → "Which thermostat do you mean: Bedroom, Living Room?"
"""

import pytest

from puffpai.agents import climate_writes


class FakeBridge:
    """Two thermostats. ``broken`` names the one whose write does nothing."""

    def __init__(self, broken=(), setpoints=None):
        self.broken = set(broken)
        self.setpoints = dict(setpoints or {"climate.living_room": 75,
                                            "climate.bedroom": 73})
        self.calls = []

    def get_all_states(self):
        return [
            {"entity_id": "climate.living_room",
             "attributes": {"friendly_name": "Living Room",
                            "temperature": self.setpoints["climate.living_room"],
                            "current_temperature": 75.2}},
            {"entity_id": "climate.bedroom",
             "attributes": {"friendly_name": "Bedroom",
                            "temperature": self.setpoints["climate.bedroom"],
                            "current_temperature": 73.0}},
        ]

    def get_entity_state(self, entity_id):
        for raw in self.get_all_states():
            if raw["entity_id"] == entity_id:
                return raw
        return {}

    def set_climate(self, entity_id, temperature=None, hvac_mode=None):
        self.calls.append((entity_id, temperature))
        if entity_id in self.broken:
            return {"success": True}
        self.setpoints[entity_id] = temperature
        return {"success": True}


def _handle(bridge, text, history=None):
    return climate_writes.handle(text, bridge, history)


# ── A. every target is called, every target is read back ──────────────────

def test_both_targets_are_called_and_both_are_confirmed():
    bridge = FakeBridge()
    reply = _handle(bridge, "set the living room and bedroom to 74")
    assert sorted(e for e, _t in bridge.calls) == ["climate.bedroom",
                                                   "climate.living_room"]
    assert "Living Room set to 74." in reply
    assert "Bedroom set to 74." in reply
    assert "didn't take" not in reply


def test_the_second_target_failing_is_reported_not_claimed():
    """The exact D3c shape: the bedroom write no-ops and stays at 73."""
    bridge = FakeBridge(broken={"climate.bedroom"})
    reply = _handle(bridge, "set the living room and bedroom to 74")
    assert len(bridge.calls) == 2, "the second target must still be CALLED"
    assert "Living Room set to 74." in reply
    assert "Bedroom is still at 73" in reply
    assert "didn't take" in reply and "try again" in reply
    # The failure may never be spoken as a success.
    assert "Bedroom set to 74" not in reply


def test_a_single_named_target_reads_back_too():
    bridge = FakeBridge()
    reply = _handle(bridge, "set the bedroom to 70")
    assert bridge.calls == [("climate.bedroom", 70)]
    assert reply == "Bedroom set to 70."


def test_a_failed_single_target_never_confirms():
    bridge = FakeBridge(broken={"climate.living_room"})
    reply = _handle(bridge, "set the living room thermostat to 68")
    assert "still at 75" in reply and "didn't take" in reply


def test_spoken_number_words_are_a_setpoint():
    bridge = FakeBridge()
    reply = _handle(bridge, "set the bedroom to seventy four")
    assert bridge.calls == [("climate.bedroom", 74)]
    assert "Bedroom set to 74." in reply


# ── B. follow-up context ─────────────────────────────────────────────────

def test_the_room_is_carried_from_the_previous_turn():
    """The exact two-turn chain from D3b."""
    bridge = FakeBridge()
    history = [{"user": "what's the temperature in the living room",
                "assistant": "The living room is 75 degrees."}]
    reply = _handle(bridge, "set it to 74", history)
    assert bridge.calls == [("climate.living_room", 74)]
    assert "Living Room set to 74." in reply


def test_the_room_is_carried_for_the_thermostat_phrasing_too():
    bridge = FakeBridge()
    history = [{"user": "what's the temperature in the living room",
                "assistant": "The living room is 75 degrees."}]
    reply = _handle(bridge, "set the thermostat to 74", history)
    assert bridge.calls == [("climate.living_room", 74)]
    assert "Living Room set to 74." in reply


def test_a_fresh_command_with_no_prior_context_still_asks():
    bridge = FakeBridge()
    reply = _handle(bridge, "set the thermostat to 74", [])
    assert bridge.calls == [], "nothing may be written before the room is known"
    assert reply == "Which thermostat do you mean: Bedroom, Living Room?"


def test_a_previous_turn_naming_two_rooms_is_not_carried():
    bridge = FakeBridge()
    history = [{"user": "how warm are the living room and bedroom",
                "assistant": "..."}]
    reply = _handle(bridge, "set it to 74", history)
    assert bridge.calls == []
    assert reply.startswith("Which thermostat")


def test_only_the_last_two_user_turns_are_carried():
    bridge = FakeBridge()
    history = [{"user": "what's the temperature in the living room"},
               {"user": "what time is it"},
               {"user": "thanks"}]
    reply = _handle(bridge, "set it to 74", history)
    assert bridge.calls == []
    assert reply.startswith("Which thermostat")


# ── what the fastpath must NOT claim ─────────────────────────────────────

@pytest.mark.parametrize("text", [
    "set the volume to 74",
    "set a timer for 74 minutes",
    "set an alarm for 7",
    "add 74 eggs to the shopping list",
    "what's the temperature in the living room",
    "turn on the living room lamp",
    "set the living room to 740",
])
def test_declines_everything_that_is_not_a_setpoint(text):
    bridge = FakeBridge()
    assert _handle(bridge, text) is None
    assert bridge.calls == []


def test_declines_when_the_house_has_no_thermostat():
    class NoClimate(FakeBridge):
        def get_all_states(self):
            return [{"entity_id": "light.kitchen", "attributes": {}}]

    bridge = NoClimate()
    assert _handle(bridge, "set the living room to 74") is None


def test_declines_an_unreadable_house_rather_than_guessing():
    class Broken(FakeBridge):
        def get_all_states(self):
            raise RuntimeError("HA unreachable")

    assert _handle(Broken(), "set the living room to 74") is None


def test_a_single_thermostat_house_needs_no_room():
    class OneStat(FakeBridge):
        def get_all_states(self):
            return [{"entity_id": "climate.hallway",
                     "attributes": {"friendly_name": "Hallway Thermostat",
                                    "temperature": self.setpoints.get(
                                        "climate.hallway", 68)}}]

        def set_climate(self, entity_id, temperature=None, hvac_mode=None):
            self.calls.append((entity_id, temperature))
            self.setpoints[entity_id] = temperature
            return {"success": True}

    bridge = OneStat()
    reply = _handle(bridge, "set the thermostat to 72")
    assert bridge.calls == [("climate.hallway", 72)]
    assert "Hallway Thermostat set to 72." in reply


# ── the reply is composed from evidence, never from the request ──────────

def test_reply_for_is_built_only_from_read_backs():
    results = [{"name": "Living Room", "verified": True, "observed": 74},
               {"name": "Bedroom", "verified": False, "observed": 73}]
    reply = climate_writes.reply_for(results, 74)
    assert reply == ("Living Room set to 74. Bedroom is still at 73 — "
                     "it didn't take. Want me to try again?")


def test_an_unreadable_thermostat_says_so():
    results = [{"name": "Bedroom", "verified": False, "observed": None}]
    reply = climate_writes.reply_for(results, 74)
    assert "can't read a setpoint back" in reply
