""""do X, then Y" must not silently drop Y.

The failure mode: ``POST /chat/send {"message":"check whats in the pantry, then
tell me the network status too"}`` answers ONLY the pantry. The journal shows
``[Chat] Capability read: app_list_pantry`` and nothing else: the instant
answer layer matches clause one, returns, and the second clause vanishes with
no error and no partial-completion notice.

The contrast case ("turn on the office led, and after that tell me a fun
fact") completes BOTH clauses, because neither clause hits a fastpath and the
turn reaches the planner. So the planner already handles these; the
instant-answer layer stands down on a sequenced turn rather than teaching every
fastpath to resume.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

#: The primary phrasing, plus the shapes around it.
COMPOUND = [
    "check whats in the pantry, then tell me the network status too",
    "turn on the office led, and after that tell me a fun fact",
    "what's the weather then set a timer for 10 minutes",
    "whats in the pantry then what's the network status",
    "tell me the time, then play some jazz radio",
    "set a timer for 5 minutes and then tell me the weather",
]

#: Single-intent turns that must KEEP their fastpath. Several of these carry an
#: "and", which is why a bare "and" is not a compound marker.
SINGLE = [
    "whats in the pantry",
    "what is the network status",
    "turn off the kitchen and hallway lights",
    "what's the weather and the forecast",
    "add milk and eggs to the shopping list",
    "set an alarm for 7am",
    "is the vault locked",
    "whats the status of the house",
    "back then we used a different router",
    "every now and then the camera drops out",
    "if the door is open then tell me",
    "what were things like way back then",
]


#: Seen live: "add oat milk to the shopping list and tell
#: me how many items are on the list" added the milk and answered nothing,
#: while the same turn with ", then tell me" answered both halves. An "and"
#: followed by a REQUEST OPENER sequences; an "and" followed by another object
#: still does not (the NOT_COMPOUND_AND list below is the floor for that).
AND_COMPOUND = [
    "add SAMPLE oat milk to the shopping list and tell me how many items "
    "are on the list",
    "add milk to the list and tell me the weather",
    "whats in the pantry and read me the calendar",
    "turn on the hallway light and show me the camera",
    "add milk and also tell me the time",
    "add milk and also turn off the kitchen light",
    "put out the bins and let me know the forecast",
]

#: The "and" that joins one action to one object. Splitting any of these would
#: invent a second request out of half an item.
NOT_COMPOUND_AND = [
    "add the salad and potato bar to the menu",
    "turn off the living room and bedroom lights",
    "turn on the lamp and also the fan",
    "add chicken and stars soup to the shopping list",
    "add peanut butter and jelly to the shopping list",
    "tell me about the moon and the tides",
]


@pytest.mark.parametrize("text", COMPOUND)
def test_a_sequenced_turn_is_recognised_as_compound(text):
    assert api._is_compound_request(text) is True


@pytest.mark.parametrize("text", AND_COMPOUND)
def test_an_and_that_opens_a_second_request_sequences_too(text):
    from puffpai import compound_turns as ct
    assert api._is_compound_request(text) is True
    assert ct.is_compound(text) is True, (
        "api_server._COMPOUND_REQUEST_RE and compound_turns.SEQUENCE_RE must "
        "stay in step, or the layer stands down for a turn the splitter "
        "refuses to split and the whole turn is dropped")
    clauses = ct.split_clauses(text)
    assert len(clauses) == 2, clauses
    assert not clauses[1].lower().startswith(("and ", "also "))


@pytest.mark.parametrize("text", NOT_COMPOUND_AND)
def test_a_bare_and_between_two_objects_is_not_a_sequence(text):
    from puffpai import compound_turns as ct
    assert api._is_compound_request(text) is False
    assert ct.split_clauses(text) == []


def test_the_second_clause_of_the_live_turn_is_the_question_asked():
    from puffpai import compound_turns as ct
    clauses = ct.split_clauses(
        "add SAMPLE oat milk to the shopping list and tell me how many "
        "items are on the list")
    assert clauses == ["add SAMPLE oat milk to the shopping list",
                       "tell me how many items are on the list"]


@pytest.mark.parametrize("text", SINGLE)
def test_a_single_intent_turn_is_not_compound(text):
    assert api._is_compound_request(text) is False


def test_the_instant_layer_stands_down_on_the_pantry_then_network_phrasing(monkeypatch):
    """The pantry fastpath must NOT claim the compound turn."""
    invoked = []

    class _Tool:
        def __init__(self, name):
            self.name = name

        def invoke(self, arg):
            invoked.append(self.name)
            return f"{self.name} answered"

    class _Tools:
        tool_dict = {"app_list_pantry": _Tool("app_list_pantry"),
                     "privacy_scan_network": _Tool("privacy_scan_network")}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    text = "check whats in the pantry, then tell me the network status too"
    assert api._try_instant_answer(text) is None
    assert invoked == [], (
        "a fastpath claimed a compound turn and would have dropped clause two")


def test_the_same_first_clause_alone_still_gets_its_fastpath(monkeypatch):
    """Proof the gate is about the sequence, not about the pantry."""
    invoked = []

    class _Tool:
        def invoke(self, arg):
            invoked.append("app_list_pantry")
            return "Pantry (1 item): ..."

    class _Tools:
        tool_dict = {"app_list_pantry": _Tool()}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    assert api._try_instant_answer("whats in the pantry") is not None
    assert invoked == ["app_list_pantry"]


def test_a_compound_turn_still_routes_to_the_agent_path():
    """Standing down is only correct if something downstream can finish it."""
    from puffpai.router import RouteTarget, route_message
    text = "check whats in the pantry, then tell me the network status too"
    assert route_message(text) in (RouteTarget.AGENT, RouteTarget.HOME)
    assert api._detect_route(text) == "agent"
