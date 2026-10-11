"""T1c: a spoken room matches a thermostat by token, not by its whole name.

Live: "set the living room and bedroom to 74" set the
Living Room and said nothing at all about the bedroom — this box's second unit
is called **"Upstairs Bedroom"**, and `named_targets` looked for the
full friendly name as a phrase, so the word "bedroom" matched nothing and the
target was dropped in silence.
"""

from puffpai.agents import climate_writes


class BoxBridge:
    """The real names on the Orin: "Living Room" + "Upstairs Bedroom"."""

    def __init__(self, entities=None):
        self.entities = entities or {
            "climate.living_room": {"name": "Living Room", "temp": 75,
                                    "area": "Living Room"},
            "climate.upstairs": {"name": "Upstairs Bedroom",
                                        "temp": 73, "area": "Bedroom"},
        }
        self.calls = []

    def get_all_states(self):
        return [{"entity_id": eid,
                 "attributes": {"friendly_name": row["name"],
                                "area": row.get("area", ""),
                                "temperature": row["temp"],
                                "current_temperature": row["temp"]}}
                for eid, row in self.entities.items()]

    def get_entity_state(self, entity_id):
        for raw in self.get_all_states():
            if raw["entity_id"] == entity_id:
                return raw
        return {}

    def set_climate(self, entity_id, temperature=None, hvac_mode=None):
        self.calls.append((entity_id, temperature))
        self.entities[entity_id]["temp"] = temperature
        return {"success": True}


# ── the failure itself ────────────────────────────────────────────────────

def test_bedroom_reaches_the_upstairs_bedroom():
    bridge = BoxBridge()
    reply = climate_writes.handle("set the living room and bedroom to 74",
                                  bridge)
    assert sorted(e for e, _t in bridge.calls) == [
        "climate.living_room", "climate.upstairs"]
    assert "Living Room set to 74." in reply
    assert "Upstairs Bedroom set to 74." in reply


def test_living_room_still_matches_exactly():
    bridge = BoxBridge()
    reply = climate_writes.handle("set the living room to 74", bridge)
    assert bridge.calls == [("climate.living_room", 74)]
    assert reply == "Living Room set to 74."


def test_the_order_of_the_rooms_is_the_order_they_were_said():
    bridge = BoxBridge()
    climate_writes.handle("set the bedroom and the living room to 70", bridge)
    assert [e for e, _t in bridge.calls] == ["climate.upstairs",
                                             "climate.living_room"]


# ── ambiguity is asked about, never guessed ───────────────────────────────

def test_two_bedrooms_ask_which_one():
    bridge = BoxBridge({
        "climate.master": {"name": "Master Bedroom", "temp": 72, "area": ""},
        "climate.guest": {"name": "Guest Bedroom", "temp": 72, "area": ""},
    })
    reply = climate_writes.handle("set the bedroom to 74", bridge)
    assert bridge.calls == [], "nothing may be written on an ambiguous room"
    assert "Which thermostat do you mean for 'bedroom'" in reply
    assert "Guest Bedroom" in reply and "Master Bedroom" in reply


def test_the_area_breaks_the_tie():
    bridge = BoxBridge({
        "climate.master": {"name": "Master Bedroom", "temp": 72,
                           "area": "Master Bedroom"},
        "climate.up": {"name": "Upstairs Bedroom Unit", "temp": 72,
                       "area": "Upstairs"},
    })
    reply = climate_writes.handle("set the master bedroom to 74", bridge)
    assert bridge.calls == [("climate.master", 74)]
    assert "Master Bedroom set to 74." in reply


# ── a named room that resolves to nothing is SAID ─────────────────────────

def test_an_unknown_room_is_never_dropped_in_silence():
    bridge = BoxBridge()
    reply = climate_writes.handle("set the living room and kitchen to 74",
                                  bridge)
    assert bridge.calls == [("climate.living_room", 74)]
    assert "Living Room set to 74." in reply
    assert "I couldn't find a thermostat for 'kitchen'." in reply


def test_only_an_unknown_room_answers_with_the_miss_alone():
    bridge = BoxBridge()
    reply = climate_writes.handle("set the garage thermostat to 74", bridge)
    assert bridge.calls == []
    assert reply == "I couldn't find a thermostat for 'garage'."


# ── the resolver itself ───────────────────────────────────────────────────

def test_resolve_targets_accounts_for_every_named_room():
    bridge = BoxBridge()
    thermostats = climate_writes.list_thermostats(bridge)
    resolved = climate_writes.resolve_targets(
        "set the bedroom and the attic to 68", thermostats)
    assert [t["entity_id"] for t in resolved["targets"]] == [
        "climate.upstairs"]
    assert resolved["missing"] == ["attic"]
    assert resolved["ambiguous"] == []


def test_a_pronoun_follow_up_still_carries_the_room():
    bridge = BoxBridge()
    history = [{"user": "what's the temperature in the living room"}]
    reply = climate_writes.handle("set it to 74", bridge, history)
    assert bridge.calls == [("climate.living_room", 74)]
    assert "Living Room set to 74." in reply
