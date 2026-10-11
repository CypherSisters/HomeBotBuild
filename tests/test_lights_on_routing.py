""""which lights are on" is answered from Home Assistant, never denied.

If no reader claims the turn, it reaches a model with no HA tool bound, and
the answer is a denial even while ``GET /ha/entities`` returns every state::

    which lights are on right now -> 16.5 s
    -> "I don't have a way to read the current on/off status of your lights
        directly from Home Assistant."

    [Chat] Route: agent for: which lights are on right now...
    [I] puffpai.agents.engine: engine classify ok in 2.01s (qwen3:30b-a3b)
    [Agent] LLM round (bind_tools) took 14.3s

The phrasings below are the ones a reader must claim, plus the neighbours that
must not move.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai.agents import household_reads  # noqa: E402


# ── fixtures: the shape /ha/entities actually returns ───────────────────

class _Bridge:
    """Stands in for HomeAssistantBridge with a canned state list."""

    def __init__(self, states):
        self._states = states

    def get_all_states(self):
        return self._states


class _DeadBridge:
    def get_all_states(self):
        raise RuntimeError("connection refused")


def _light(entity, name, state):
    return {"entity_id": entity, "state": state,
            "attributes": {"friendly_name": name}}


def _orin_shaped_states(on_names=("Office Status LED",)):
    """210 entities, a realistic household count, with 12 lights."""
    states = []
    names = ["Office Status LED", "Kitchen Ceiling", "Porch Light",
             "Hallway Sconce", "Desk Lamp", "Garage Bulb", "Bedroom Lamp",
             "Living Room Strip", "Pantry Light", "Bathroom Vanity",
             "Front Walk", "Back Step"]
    for name in names:
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        states.append(_light(f"light.{slug}", name,
                             "on" if name in on_names else "off"))
    # The other 198: sensors, switches, media players, binary sensors.
    for i in range(198):
        domain = ("sensor", "switch", "binary_sensor", "media_player")[i % 4]
        states.append({"entity_id": f"{domain}.filler_{i}",
                       "state": "off",
                       "attributes": {"friendly_name": f"Filler {i}"}})
    assert len(states) == 210
    return states


# ── "which lights are on" ───────────────────────────────────────────────────

LIGHT_PHRASINGS = [
    "which lights are on right now",
    "which lights are on",
    "what lights are on",
    "any lights on",
    "are any lights on",
    "is anything on",
    "how many lights are on",
    "are the lights still on",
]


@pytest.mark.parametrize("phrase", LIGHT_PHRASINGS)
def test_a_lights_question_reaches_the_lights_reader(phrase):
    assert household_reads.match(phrase) == "lights_on"


def test_which_lights_are_on_right_now_names_the_lights_that_are_on():
    bridge = _Bridge(_orin_shaped_states(
        on_names=("Office Status LED", "Porch Light")))
    reply = household_reads.answer("which lights are on right now",
                                   bridge=bridge)
    assert reply is not None
    # The failure to prevent is a denial of having any HA access at all.
    assert "don't have a way" not in reply.lower()
    assert "Office Status LED" in reply
    assert "Porch Light" in reply
    assert "Kitchen Ceiling" not in reply       # it is off
    assert "2 of 12 lights are on" in reply


def test_no_lights_on_is_a_real_answer_not_a_shrug():
    bridge = _Bridge(_orin_shaped_states(on_names=()))
    reply = household_reads.answer("which lights are on right now",
                                   bridge=bridge)
    assert reply is not None
    assert reply.startswith("No lights are on")
    assert "12" in reply


def test_an_unreachable_bridge_says_so_rather_than_reporting_darkness():
    reply = household_reads.answer("which lights are on right now",
                                   bridge=_DeadBridge())
    assert reply is not None
    assert "can't reach Home Assistant" in reply
    assert "No lights are on" not in reply


def test_an_unavailable_light_is_counted_but_never_claimed_to_be_off():
    states = _orin_shaped_states(on_names=("Porch Light",))
    states[1]["state"] = "unavailable"
    reply = household_reads.answer("which lights are on", bridge=_Bridge(states))
    assert "not reporting" in reply
    assert "Kitchen Ceiling" not in reply


def test_no_light_entities_at_all_is_said_plainly():
    reply = household_reads.answer(
        "which lights are on",
        bridge=_Bridge([{"entity_id": "sensor.x", "state": "1",
                         "attributes": {}}]))
    assert "No light entities are set up" in reply


@pytest.mark.parametrize("phrase", [
    "turn on the kitchen lights",
    "turn off all the lights",
    "switch on the porch light",
    "which lights should I turn on",
    "dim the lights",
    "set the lights to 50 percent",
])
def test_an_action_on_the_same_words_is_not_a_read(phrase):
    assert household_reads.match(phrase) is None


@pytest.mark.parametrize("phrase,reader", [
    # A matched rule's exclude vetoes the WHOLE table, so the new rule must not
    # merely decline these — it must not touch them.
    ("what's the status of the house", "house_status"),
    ("how's the house doing", "house_status"),
    ("what's on the menu tonight", "meal_today"),
    ("what's for dinner", "meal_today"),
    ("what's on our calendar today", "calendar_upcoming"),
    ("any events coming up", "calendar_upcoming"),
    ("do we owe any money on bills", "bills_status"),
    ("did I take my medication today", "medication_status"),
])
def test_the_neighbouring_household_reads_still_route_where_they_did(phrase,
                                                                    reader):
    assert household_reads.match(phrase) == reader


@pytest.mark.parametrize("phrase", [
    "is anything on the calendar",
    "is anything on TV tonight",
    "is anything on your list",
])
def test_is_anything_on_something_is_not_a_lights_question(phrase):
    assert household_reads.match(phrase) != "lights_on"


def test_the_house_summary_still_counts_rather_than_names():
    bridge = _Bridge(_orin_shaped_states(on_names=("Porch Light",)))
    reply = household_reads.answer("what's the status of the house",
                                   bridge=bridge)
    assert reply.startswith("House status")
    assert "1 of 12 lights are on" in reply
