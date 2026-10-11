"""A compound turn whose first clause is an HA ACTION.

The failure mode, with the LED ``off`` before each run of
``turn on the office led, then tell me the weather``:

    run 1 "Office LED has been turned on." + the full weather card,
           entity afterwards ``off``, last_changed unmoved
           -> a FABRICATED action
    run 2 "Done - light.office_status_led turn on." (weather dropped)
    run 3 same

The contract this file pins:

* the reply for an action clause is worded from the entity state read back
  AFTER the service call, so a mocked HA that APPLIES the change and one that
  IGNORES it must produce different replies;
* no clause is dropped, read or action (a read-only sequence is covered
  in full by test_compound_turn_readonly_sequenced.py);
* a plain "and" is still not a sequence marker.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import compound_turns as ct  # noqa: E402

#: The compound phrasing under test.
COMPOUND = "turn on the office led, then tell me the weather"
LED = "light.office_status_led"
LED_NAME = "Office LED"
WEATHER = "It's 82°F in the default city, humidity 78%."


# ── the pure half ───────────────────────────────────────────────────────────

def test_the_compound_phrasing_splits_into_two_ordered_clauses():
    assert ct.split_clauses(COMPOUND) == [
        "turn on the office led", "tell me the weather"]


def test_a_three_clause_sequence_keeps_all_three_in_order():
    assert ct.split_clauses(
        "turn on the office led, then tell me the weather, "
        "then what time is it") == [
        "turn on the office led",
        "tell me the weather",
        "what time is it"]


def test_after_that_is_also_a_sequence_marker():
    assert ct.split_clauses(
        "turn on the office led, and after that tell me a fun fact"
    ) == ["turn on the office led", "tell me a fun fact"]


@pytest.mark.parametrize("text", [
    # A plain "and" joins one action to one object, not two requests.
    "turn off the kitchen and hallway lights",
    "add milk and eggs to the shopping list",
    "what's the weather and the forecast",
    # False friends.
    "back then we used a different router",
    "every now and then the camera drops out",
    "if the door is open then tell me",
])
def test_a_single_request_never_splits(text):
    assert ct.split_clauses(text) == []
    assert api._is_compound_request(text) is False


def test_observed_state_wording_differs_when_the_change_did_not_apply():
    applied = ct.observed_action_reply(LED_NAME, "turn_on", "on")
    ignored = ct.observed_action_reply(LED_NAME, "turn_on", "off")
    assert applied != ignored
    assert applied == "Office LED is now on."
    assert "still reads off" in ignored
    # The fabricated wording from run 1 must never be producible from an
    # observation that contradicts it.
    assert "has been turned on" not in ignored


def test_an_unreadable_entity_is_reported_as_unconfirmed():
    said = ct.observed_action_reply(LED_NAME, "turn_on", None)
    assert "can't confirm" in said
    assert "is now on" not in said


# ── the executing half ──────────────────────────────────────────────────────

class _FakeBridge:
    """Minimal HA stand-in: records the state and hands it back on read."""

    def __init__(self, state, applies=True):
        self.state = state
        self.applies = applies
        self.reads = 0

    def apply(self, action):
        if self.applies:
            self.state = "on" if action == "turn_on" else "off"

    def get_entity_state(self, entity_id):
        self.reads += 1
        assert entity_id == LED
        return {"entity_id": entity_id, "state": self.state,
                "attributes": {"friendly_name": LED_NAME}}


def _wire(monkeypatch, bridge, *, applies=True):
    """Point the compound path at a fake HA and a fake team."""
    monkeypatch.setattr(api, "_get_ha_bridge", lambda: bridge)
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "agent")

    def _team(clause, route, requester="owner", history=None):
        bridge.apply("turn_on")
        # The prose the Concierge/planner would have produced. Deliberately the
        # run-1 fabrication, so the test proves the reply is NOT taken from it.
        return {"response": "Office LED has been turned on.",
                "sources": [], "suggestions": [],
                "team": {"agent": "steward", "intent": "control_device",
                         "targets": [LED],
                         "result": {"ok": True,
                                    "result": {"action": "turn_on"}}}}

    monkeypatch.setattr(api, "_try_team_chat", _team)


def test_the_reply_reports_the_action_when_ha_really_applied_it(monkeypatch):
    bridge = _FakeBridge("off", applies=True)
    _wire(monkeypatch, bridge)
    out = api._try_compound_request(COMPOUND, lambda clause: WEATHER)
    assert out is not None
    assert "Office LED is now on." in out
    assert WEATHER in out
    # Order preserved: the action clause was asked for first.
    assert out.index("is now on") < out.index(WEATHER)
    assert bridge.reads >= 1


def test_the_reply_refuses_to_claim_an_action_ha_ignored(monkeypatch):
    """Run 1's exact failure: HA ignores the call, the reply must say so."""
    bridge = _FakeBridge("off", applies=False)
    _wire(monkeypatch, bridge)
    out = api._try_compound_request(COMPOUND, lambda clause: WEATHER)
    assert out is not None
    assert "still reads off" in out
    assert "has been turned on" not in out
    assert "is now on" not in out
    # And the second clause still arrives.
    assert WEATHER in out


def test_the_two_ha_outcomes_do_not_produce_the_same_reply(monkeypatch):
    applied_bridge = _FakeBridge("off", applies=True)
    _wire(monkeypatch, applied_bridge)
    applied = api._try_compound_request(COMPOUND, lambda c: WEATHER)
    ignored_bridge = _FakeBridge("off", applies=False)
    _wire(monkeypatch, ignored_bridge)
    ignored = api._try_compound_request(COMPOUND, lambda c: WEATHER)
    assert applied != ignored


def test_the_second_clause_is_never_dropped(monkeypatch):
    """Runs 2 and 3: the weather clause vanished with no error."""
    bridge = _FakeBridge("off", applies=True)
    _wire(monkeypatch, bridge)
    seen = []

    def _read(clause):
        seen.append(clause)
        return WEATHER

    out = api._try_compound_request(COMPOUND, _read)
    assert seen == ["tell me the weather"]
    assert WEATHER in out


def test_a_three_clause_sequence_answers_all_three(monkeypatch):
    bridge = _FakeBridge("off", applies=True)
    _wire(monkeypatch, bridge)
    text = ("turn on the office led, then tell me the weather, "
            "then what time is it")
    answers = {"tell me the weather": WEATHER,
               "what time is it": "It's 7:09 AM on Thursday, September 03."}
    out = api._try_compound_request(text, lambda c: answers[c])
    assert "Office LED is now on." in out
    assert WEATHER in out
    assert "7:09 AM" in out
    assert out.index(WEATHER) < out.index("7:09 AM")


def test_an_unhandled_later_clause_is_reported_not_dropped(monkeypatch):
    bridge = _FakeBridge("off", applies=True)
    _wire(monkeypatch, bridge)
    out = api._try_compound_request(COMPOUND, lambda c: None)
    assert "Office LED is now on." in out
    assert "couldn't complete" in out
    assert "tell me the weather" in out


def test_planner_prose_is_never_repeated_when_the_team_took_another_route(
        monkeypatch):
    """`Team handled: plan -> []` — the prose is a model's account, not fact."""
    bridge = _FakeBridge("off", applies=False)
    monkeypatch.setattr(api, "_get_ha_bridge", lambda: bridge)
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "agent")
    monkeypatch.setattr(
        api, "_try_team_chat",
        lambda *a, **k: {"response": "Office LED has been turned on.",
                         "team": {"intent": "plan", "targets": []}})
    # First clause unhandled and nothing has run yet -> the whole turn is left
    # to the existing path rather than half-answered from invented prose.
    assert api._try_compound_request(COMPOUND, lambda c: WEATHER) is None


def test_a_read_only_sequence_is_answered_clause_by_clause(monkeypatch):
    """A read-only sequence goes through the splitter too.

    Left to the planner, the pantry-then-network phrasing answers "Your pantry
    is currently empty" for a pantry holding one item, while the single clause
    is correct. So a read-only sequence goes through the same splitter, each
    clause through its own reader. The team is not involved: these are reads.
    Full contract in test_compound_turn_readonly_sequenced.py.

    The network clause reaches its registry reader after normalisation, so it
    does not fall through to *read_handler*; the property under test (two
    clauses, in order, no team call) holds either way.
    """
    called = []
    monkeypatch.setattr(api, "_try_team_chat",
                        lambda *a, **k: called.append(a) or None)

    class _T:
        def invoke(self, arg):
            return "Network scan: 19 devices seen."

    monkeypatch.setattr(
        api, "_get_assistant_tools",
        lambda: type("X", (), {"tool_dict": {"privacy_scan_network": _T()}})())
    text = "check whats in the pantry, then tell me the network status too"
    assert ct.split_clauses(text)  # it IS a sequence
    out = api._try_compound_request(text, lambda c: "answer to: " + c)
    assert out == "answer to: check whats in the pantry\n\n" \
                  "Network scan: 19 devices seen."
    assert called == [], "a read clause must not be run as an action"


def test_a_single_intent_turn_is_not_taken_over(monkeypatch):
    monkeypatch.setattr(api, "_try_team_chat",
                        lambda *a, **k: pytest.fail("team called"))
    assert api._try_compound_request(
        "turn on the office led", lambda c: "x") is None
    assert api._try_compound_request(
        "turn off the kitchen and hallway lights", lambda c: "x") is None


def test_the_read_handler_is_not_re_entered_recursively(monkeypatch):
    """A clause is never itself treated as a compound turn."""
    bridge = _FakeBridge("off", applies=True)
    _wire(monkeypatch, bridge)
    depth = []

    def _read(clause):
        depth.append(clause)
        # Whatever the calling pipeline does, re-entry must stand down.
        assert api._try_compound_request(clause, _read) is None
        return WEATHER

    out = api._try_compound_request(COMPOUND, _read)
    assert WEATHER in out
    assert depth == ["tell me the weather"]


# ── wiring: the whole chat turn, not just the helper ────────────────────────

def test_process_chat_query_returns_both_clauses(monkeypatch):
    """End-to-end through the chat front door, with HA and the team faked."""
    bridge = _FakeBridge("off", applies=True)
    _wire(monkeypatch, bridge)
    monkeypatch.setattr(api, "_augment_persona_with_memory",
                        lambda persona, uid: persona)
    monkeypatch.setattr(api, "_load_persona", lambda: "persona")
    monkeypatch.setattr(api, "_remember_for_user", lambda uid, msg: None)
    monkeypatch.setattr(api, "_requester_for", lambda uid: "owner")
    # The weather clause is answered by its own instant fastpath.
    monkeypatch.setattr(
        api, "_try_instant_answer",
        lambda text, **kw: (WEATHER if text == "tell me the weather"
                            else None))

    out = api._process_chat_query(COMPOUND)
    assert "Office LED is now on." in out["response"]
    assert WEATHER in out["response"]


def test_process_chat_query_leaves_a_single_turn_alone(monkeypatch):
    monkeypatch.setattr(api, "_augment_persona_with_memory",
                        lambda persona, uid: persona)
    monkeypatch.setattr(api, "_load_persona", lambda: "persona")
    monkeypatch.setattr(api, "_remember_for_user", lambda uid, msg: None)
    monkeypatch.setattr(api, "_requester_for", lambda uid: "owner")
    monkeypatch.setattr(api, "_try_instant_answer",
                        lambda text, **kw: "single")
    assert api._process_chat_query("whats in the pantry")["response"] == "single"
