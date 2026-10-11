"""Pairing phrasings reach the consent queue, never the LLM and never a radio.

The failure mode: "pair a new zigbee bulb in the living room" and "I want to
pair a new zigbee light" answered "Which thermostat do you mean: Bedroom,
Living Room?" and left the consent queue empty.

The cause is not a missing rule: ``_ZIGBEE_PAIR_RE`` in
:mod:`puffpai.agents.concierge` owns these phrasings. A determiner alternation
with no "a"/"an" makes ``pair a new zigbee bulb`` match nothing,
``resolve_intent`` falls through to its "no rule matched" default, and the
optional LLM refinement classifies the utterance as ``set_climate``, whereupon
the climate entity resolver asks which thermostat was meant. A pairing request
answered by a language model is the failure mode the rule table exists to
prevent, so the rule itself covers these phrasings rather than a second route.

What is pinned here, in order of how badly it would hurt:

* no phrasing opens a pairing window — the only effect is one consent request;
* the reply asks for approval; it never claims a window is open;
* the consent gate still refuses a model-written duration;
* no new Home Assistant call site appears on the path;
* the neighbouring phrasings that share a verb ("add … to the shopping list",
  "connect to NPR", "turn on the living room light") still route where they did.

Run: py -m pytest tests/test_zigbee_pairing_routing.py -q
"""

from __future__ import annotations

import importlib

import pytest

from puffpai.agents.concierge import RESOLVABLE_INTENTS, resolve_intent
from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.steward import STATE_CHANGING_INTENTS, StewardAgent


# ══════════════════════════════════════════════════════════════════════
# Doubles — every actuator is a recorder or a landmine
# ══════════════════════════════════════════════════════════════════════

class Landmine:
    """Anything touched here is a bug: this stands in for a real house."""

    def __init__(self, what="the house"):
        self._what = what

    def __getattr__(self, name):
        raise AssertionError(
            f"{self._what}.{name} was reached on a path that must not actuate")

    def __call__(self, *a, **k):
        raise AssertionError(f"{self._what} was called on a path that must not "
                             f"actuate")


class QuietBridge:
    """A Steward bridge that records service calls and never actuates."""

    def __init__(self):
        self.calls: list[tuple] = []

    def call_service(self, domain, service, entity_id=None, **data):
        self.calls.append((domain, service, entity_id))
        return []

    def list_devices(self, domain=None, controllable_only=True):
        return []

    def get_all_states(self, *, force=False):
        return []


@pytest.fixture
def paired(tmp_path, monkeypatch):
    """Consent queue + pairing module rooted at a tmp state dir.

    Same shape as the fixture in ``tests/test_zigbee_pairing.py``: the consent
    queue, the ledger and the activity feed each cache their resolved
    directory, so each is reloaded (or told to forget) after the env moves, and
    again on teardown so a later test in the process does not inherit a tmp
    path that no longer exists.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

    import puffpai.agent_activity as aa
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr
    import puffpai.zigbee_pairing as zp

    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()
    zp._session = None
    zp._spent_approvals.clear()

    yield zp, cr

    zp._session = None
    zp._spent_approvals.clear()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()


# ══════════════════════════════════════════════════════════════════════
# The phrasings
# ══════════════════════════════════════════════════════════════════════

#: The two determiner-bearing utterances, the bare plural, and
#: three more variants across the verbs and nouns the registry advertises for
#: ``home.onboarding``.
PAIRING_PHRASINGS = [
    "pair a new zigbee bulb in the living room",     # determiner "a"
    "I want to pair a new zigbee light",             # determiner "a"
    "pair my bulbs",                                 # determiner "my"
    "connect a new zigbee sensor",
    "add another smart plug",
    "join a new device",
]

#: Phrasings that share a verb or a noun with pairing and must keep their own
#: route. "add … to the shopping list" and "add a task" are list writes,
#: "connect to NPR" is media, and the two light utterances are control/read.
NEGATIVE_CONTROLS = [
    "turn on the living room light",
    "which lights are on",
    "add SAMPLE oat milk to the shopping list",
    "add a task",
    "connect to NPR",
]


@pytest.mark.parametrize("text", PAIRING_PHRASINGS)
def test_pairing_phrasings_resolve_deterministically(text):
    """The rule table answers, so the LLM refinement is never consulted.

    ``reason`` is the tell: the refinement path is reached only from the
    "no rule matched" default, so an empty reason here is the proof that these
    utterances never leave the deterministic table.
    """
    res = resolve_intent(text)

    assert res.intent == "zigbee_pair", f"{text!r} -> {res.intent!r}"
    assert not (res.reason or ""), f"{text!r} fell through to: {res.reason!r}"
    assert res.needs_entity is False
    assert res.state_changing is True


@pytest.mark.parametrize("text", NEGATIVE_CONTROLS)
def test_neighbouring_phrasings_do_not_reach_pairing(text):
    assert resolve_intent(text).intent not in ("zigbee_pair", "zigbee_pair_status")


def test_the_defect_phrasings_no_longer_resolve_to_climate():
    """The exact symptom: a pairing request read as a thermostat change."""
    for text in ("pair a new zigbee bulb in the living room",
                 "I want to pair a new zigbee light"):
        res = resolve_intent(text)
        assert res.intent != "set_climate"
        assert "climate" not in (res.domains or ())


def test_a_model_still_cannot_choose_to_open_a_pairing_window():
    """The refinement menu is unchanged: neither intent is on it."""
    assert "zigbee_pair" not in RESOLVABLE_INTENTS
    assert "zigbee_pair_status" not in RESOLVABLE_INTENTS
    assert "zigbee_pair" in STATE_CHANGING_INTENTS


# ══════════════════════════════════════════════════════════════════════
# Invariant 1 & 4: one consent request, no window, no HA call
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", PAIRING_PHRASINGS)
def test_each_phrasing_files_exactly_one_request_and_actuates_nothing(
        text, paired, monkeypatch):
    zp, cr = paired
    from puffpai import zigbee_manager

    monkeypatch.setattr(zigbee_manager.ZigbeeManager, "open_permit_join",
                        Landmine("ZigbeeManager.open_permit_join"))
    bridge = QuietBridge()
    steward = StewardAgent(ha_bridge=bridge)
    env = Envelope(intent=resolve_intent(text).intent,
                   payload={"task_text": text},
                   origin_agent="concierge", state_changing=True,
                   budget=Budget(max_actions=3))

    reply = steward.handle(env)

    assert reply["ok"] is True
    assert reply["executed"] is False
    assert reply["awaiting_approval"] is True
    # Invariant 4: nothing on this path speaks to Home Assistant.
    assert bridge.calls == [], f"{text!r} reached HA: {bridge.calls!r}"
    # Invariant 1: exactly one request, and it is the pairing question.
    open_reqs = cr.list_open()
    assert [r["action"] for r in open_reqs] == ["zigbee_pair"]
    assert open_reqs[0]["requested_by"] == "steward"
    # The reason is the one written where the facts are, not a paraphrase.
    assert "ANY Zigbee device within radio range" in open_reqs[0]["reason"]
    # Invariant 1, second half: the window itself is still shut.
    assert zp._live_session() is None
    assert zp.pairing_status().get("open") is not True


@pytest.mark.parametrize("text", PAIRING_PHRASINGS)
def test_the_reply_asks_for_approval_and_never_claims_a_window_is_open(
        text, paired):
    """Invariant 2: the wording is the one from:mod:`puffpai.zigbee_pairing`."""
    zp, _ = paired
    steward = StewardAgent(ha_bridge=QuietBridge())
    env = Envelope(intent="zigbee_pair", payload={"task_text": text},
                   origin_agent="concierge", state_changing=True,
                   budget=Budget(max_actions=3))

    message = steward.handle(env)["message"]

    assert "needs your approval" in message
    assert "tap Approve on the dashboard" in message
    lowered = message.lower()
    for claim in ("pairing window open", "window is open", "i've opened",
                  "i opened", "now open"):
        assert claim not in lowered, f"{text!r} claimed a window: {message!r}"


def test_a_second_ask_does_not_file_a_second_request(paired):
    """Idempotent per (entity, action): the owner is asked once, not twice."""
    zp, cr = paired
    steward = StewardAgent(ha_bridge=QuietBridge())

    for text in ("pair a new zigbee bulb in the living room",
                 "I want to pair a new zigbee light"):
        steward.handle(Envelope(intent="zigbee_pair",
                                payload={"task_text": text},
                                origin_agent="concierge", state_changing=True,
                                budget=Budget(max_actions=3)))

    assert [r["action"] for r in cr.list_open()] == ["zigbee_pair"]
    assert zp._live_session() is None


# ══════════════════════════════════════════════════════════════════════
# Invariant 3: the duration is still ours, not the model's
# ══════════════════════════════════════════════════════════════════════

def test_the_widened_rule_carries_no_duration_for_a_model_to_write(paired):
    """A phrasing cannot smuggle a window length past the clamp.

    ``resolve_intent`` returns no payload for either pairing intent, so the
    duration on the filed request is the module default however the request was
    worded — including when the wording names a number.
    """
    zp, cr = paired
    steward = StewardAgent(ha_bridge=QuietBridge())
    hostile = "pair a new zigbee bulb for 9999 seconds"

    res = resolve_intent(hostile)
    assert res.intent == "zigbee_pair"
    assert not res.payload

    steward.handle(Envelope(intent="zigbee_pair",
                            payload={"task_text": hostile},
                            origin_agent="concierge", state_changing=True,
                            budget=Budget(max_actions=3)))

    filed = cr.list_open()[0]["context"]["duration"]
    assert filed == zp.DEFAULT_PAIRING_SECONDS
    assert filed <= zp.MAX_PAIRING_SECONDS


def test_a_read_only_envelope_still_cannot_carry_the_widened_intent(paired):
    """Widening the rule did not widen what an unreviewed envelope may do."""
    _, cr = paired
    steward = StewardAgent(ha_bridge=QuietBridge())
    env = Envelope(intent="zigbee_pair",
                   payload={"task_text": "pair a new zigbee bulb"},
                   origin_agent="concierge", state_changing=False,
                   budget=Budget(max_actions=3))

    reply = steward.handle(env)

    assert reply["ok"] is False
    assert reply["requires_review"] is True
    assert cr.list_open() == []


# ══════════════════════════════════════════════════════════════════════
# Invariant 4, structurally: no new call site appeared
# ══════════════════════════════════════════════════════════════════════

def test_the_concierge_rule_table_holds_no_home_assistant_call_site():
    """The fix is a regex, not a new hand.

    ``resolve_intent`` is documented as pure and deterministic. Pinning that it
    still names no service-call or permit-open API keeps a later "just actuate
    it here" edit from landing in the one function that runs before consent.
    """
    import inspect

    from puffpai.agents import concierge

    source = inspect.getsource(concierge.resolve_intent)
    for forbidden in ("call_service", "open_permit_join", "permit_join",
                      "zha.permit", "complete_approved_pairing"):
        assert forbidden not in source, (
            f"resolve_intent now references {forbidden!r}")
