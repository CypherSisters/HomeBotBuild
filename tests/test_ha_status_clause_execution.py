"""An HA-status clause inside a compound turn must really execute.

Without a reader for the status clause:

    POST /chat/send {"message": "tell me about the Great Wall of China,
                                 then check if any lights are on"}
      clause 1 -> real, correct Great Wall prose
      clause 2 -> "I haven't checked the lights for you yet. To see if any are
                   on, just say 'turn on the living room lights'..."

and likewise::

    "check the living room temperature, then..." -> no reader at all

The journal for such a turn reads ``Route: rag for: tell me about the Great Wall
of China``, ``Route: rag for: check if any lights are on``, ``Compound turn: 2
clauses executed in order``: the clause splitter and the per-clause dispatch do
their job, and each clause goes through the same routing a standalone turn
does. What matters is the READER: a phrasing with no trigger anywhere reaches
RAG on the compound path and on the standalone path alike, and RAG punts.

This file pins the triggers, the honest wording of the readers, and one more
property: an untargeted-house ACTION clause is refused by its own gate
inside a compound turn rather than being reported as "I couldn't complete it".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.agents import household_reads as hr  # noqa: E402

#: The light-status clause under test.
LIGHT_CLAUSE = "check if any lights are on"
#: A compound turn carrying it.
COMPOUND_TURN = ("tell me about the Great Wall of China, "
                     "then check if any lights are on")
#: The other clause named in the new-findings row.
TEMP_CLAUSE = "check the living room temperature"

#: What the punt said. It must not be producible from a reader.
PUNT = "I haven't checked the lights for you yet"


class _Bridge:
    def __init__(self, states):
        self._states = states

    def get_all_states(self):
        return list(self._states)


_STATES = [
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "light.porch", "state": "off",
     "attributes": {"friendly_name": "Porch Light"}},
    {"entity_id": "sensor.living_room_temperature", "state": "71.6",
     "attributes": {"friendly_name": "Living Room Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°F"}},
    {"entity_id": "sensor.openweathermap_temperature", "state": "94",
     "attributes": {"friendly_name": "Outdoor Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°F"}},
]


# ── the light-status clause ──────────────────────────────────────────────────

def test_the_light_status_clause_reaches_the_lights_reader():
    assert hr.match(LIGHT_CLAUSE) == "lights_on"


def test_the_light_status_clause_answers_from_live_state():
    reply = hr.answer(LIGHT_CLAUSE, bridge=_Bridge(_STATES))
    assert reply is not None
    assert "Kitchen Light" in reply
    assert PUNT not in reply


def test_every_natural_light_status_phrasing_reaches_the_reader():
    for phrasing in ("which lights are on",
                     "check if any lights are on",
                     "see if any lights are on",
                     "tell me if the lights are still on",
                     "are any lights on",
                     "any lights still on"):
        assert hr.match(phrasing) == "lights_on", phrasing


def test_the_action_reading_of_the_same_words_is_still_not_a_read():
    """The exclude must keep its veto: a command is never answered as a read."""
    for phrasing in ("turn on the living room lights",
                     "which lights should I switch on",
                     "turn off the lights"):
        assert hr.match(phrasing) != "lights_on", phrasing


def test_a_fixture_question_is_not_a_lit_question():
    assert hr.match("check the lights on the porch") != "lights_on"


# ── the room-temperature clause ──────────────────────────────────────────────

def test_the_temperature_clause_reaches_a_reader():
    assert hr.match(TEMP_CLAUSE) == "room_temperature"


def test_the_room_temperature_read_names_that_rooms_own_sensor():
    reply = hr.answer(TEMP_CLAUSE, bridge=_Bridge(_STATES))
    assert reply is not None
    assert "71.6" in reply
    assert "living room" in reply.lower()
    # The outdoor station is never the answer to a room question.
    assert "94" not in reply


def test_a_room_with_no_sensor_is_told_so_not_given_another_rooms_number():
    reply = hr.answer("whats the temperature in the garage",
                      bridge=_Bridge(_STATES))
    assert reply is not None
    assert "71.6" not in reply
    assert "missing sensor" in reply


def test_an_unreachable_bridge_says_so_rather_than_describing_the_house():
    class _Down:
        def get_all_states(self):
            raise OSError("connection refused")

    reply = hr.answer(TEMP_CLAUSE, bridge=_Down())
    assert reply is not None
    assert "can't reach Home Assistant" in reply


def test_outdoor_and_write_phrasings_are_left_to_their_own_paths():
    for phrasing in ("whats the temperature outside",
                     "whats the weather today",
                     "set the living room temperature to 72",
                     "turn up the heat in the bedroom"):
        assert hr.match(phrasing) != "room_temperature", phrasing


# ── the gated action clause ──────────────────────────────────────────────────

def test_an_untargeted_action_clause_is_refused_by_its_own_gate(monkeypatch):
    """"turn off everything, then..." keeps the gate's refusal, per clause."""
    from puffpai import api_server

    resolution = api_server._compound_action_resolution("turn off everything")
    assert resolution is not None, "the gate case must still be an ACTION clause"

    def _boom(*a, **k):                       # the team must never be reached
        raise AssertionError("a gated clause reached the team")

    monkeypatch.setattr(api_server, "_try_team_chat", _boom)
    reply = api_server._run_compound_action_clause(
        "turn off everything", resolution, "owner")
    assert reply, "the gate's refusal must be the clause's answer"
    assert "whole house" in reply or "room or a device" in reply
    assert "I couldn't complete" not in reply


def test_the_compound_splitter_splits_the_status_turn():
    from puffpai import compound_turns as ct
    assert ct.split_clauses(COMPOUND_TURN) == [
        "tell me about the Great Wall of China",
        LIGHT_CLAUSE,
    ]
