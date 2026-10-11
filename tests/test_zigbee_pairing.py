"""The Zigbee pairing assistant asks twice and opens once.

The owner's story — "pair the new bulbs when I restart them; see them, and ask
if I want to add them" — contains two permission questions, and the whole point
of :mod:`puffpai.zigbee_pairing` is that neither one is answered by a model:

1. **May the window open?** An open ``zha.permit`` window admits ANY Zigbee
   device within radio range for its whole duration. The joiners cannot be
   enumerated in advance, so this is a person's decision. The Steward's
   ``zigbee_pair`` intent only *files* the question; the window is opened by
   ``complete_approved_pairing``, and only from the two approval surfaces.
2. **May a device that joined be managed?** Joining the radio is not consent to
   be actuated. Each new light/switch gets its own ``control_device`` request,
   and nothing here approves it.

The load-bearing assertions, in order of how badly they would hurt:

* ``complete_approved_pairing`` refuses when there is no fresh, human-resolved,
  unspent approval on the durable record — so "only the approval sites call it"
  is a control, not a comment;
* an approval is spent once and cannot be replayed into a second window;
* the ``zigbee_pair`` intent opens nothing, even though it is state-changing;
* the real ``ZigbeeManager`` gate still refuses an unconfirmed direct open, and
  still refuses a literal ``True``;
* the watcher only reads, files exactly one request per new device, and never
  auto-approves anything.

**Nothing here may reach a real Home Assistant.** Every actuator is a recorder
or a landmine, so "the gate failed" surfaces as a failed assertion about a list
rather than as a pairing window opening in someone's house.

Run: py -3.14 -m pytest tests/test_zigbee_pairing.py -q
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import threading
import time

import pytest

from puffpai.agents.concierge import RESOLVABLE_INTENTS, ConciergeAgent, resolve_intent
from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.steward import STATE_CHANGING_INTENTS, StewardAgent
from puffpai.ha_bridge import HUMAN_CONFIRMED, ConfirmationRequired


# ══════════════════════════════════════════════════════════════════════
#  Doubles
# ══════════════════════════════════════════════════════════════════════

BASELINE = [
    {"entity_id": "light.porch_light",
     "attributes": {"friendly_name": "Porch Light"}, "state": "off"},
    {"entity_id": "sensor.hall_temperature",
     "attributes": {"friendly_name": "Hall Temperature"}, "state": "68"},
]

NEW_BULB = {"entity_id": "light.new_bulb",
            "attributes": {"friendly_name": "New Bulb"}, "state": "on"}
NEW_SENSOR = {"entity_id": "sensor.new_bulb_energy",
              "attributes": {"friendly_name": "New Bulb Energy"}, "state": "0.4"}


class StubActuator:
    """Records permit calls and serves a mutable state list. Never a socket."""

    def __init__(self, states=None):
        self.states = list(BASELINE if states is None else states)
        self.opens: list[tuple[int, object]] = []
        self.closes = 0
        self._lock = threading.Lock()

    # -- reads --
    def get_all_states(self, *, force=False):
        with self._lock:
            return [dict(s) for s in self.states]

    def add(self, *entities):
        with self._lock:
            self.states.extend(dict(e) for e in entities)

    # -- the permit window --
    def open_permit_join(self, duration=60, ieee=None, *, human_confirmed=False):
        self.opens.append((duration, human_confirmed))
        return {"success": True, "duration": duration, "ieee": None,
                "closed": False, "error": None}

    def close_permit_join(self, ieee=None):
        self.closes += 1
        return {"success": True, "closed": True, "error": None}


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
        return [dict(s) for s in BASELINE]


# ══════════════════════════════════════════════════════════════════════
#  State isolation — everything writes under a tmp $PUFFPAI_DATA_DIR
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def paired(tmp_path, monkeypatch):
    """Reloaded consent queue + pairing module rooted at a tmp state dir.

    The consent queue, the consent ledger and the activity feed all cache their
    resolved directory, so each is reloaded (or told to forget) after the env
    moves — and again on teardown, so a later test in the same process does not
    inherit a tmp path that no longer exists.
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

    # Keep the watcher's wall-clock cost inside a test's patience.
    monkeypatch.setattr(zp, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(zp, "GRACE_SECONDS", 1)
    monkeypatch.setattr(zp, "QUIET_CLOSE_SECONDS", 0.2)

    yield zp, cr, tmp_path

    zp._session = None
    zp._spent_approvals.clear()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()


def _drain(zp, timeout=15.0):
    """Wait for the watcher thread to finish and clear the session."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if zp._session is None:
            for thread in threading.enumerate():
                if thread.name == "zigbee-pairing-watch":
                    thread.join(timeout=timeout)
            return True
        time.sleep(0.02)
    raise AssertionError("the pairing watcher did not finish inside its cap")


def _watchers():
    return [t for t in threading.enumerate() if t.name == "zigbee-pairing-watch"]


def _approve(cr, zp, by="user_via_chat"):
    return cr.resolve(zp.PAIRING_ENTITY_ID, approved=True, by=by)


# ══════════════════════════════════════════════════════════════════════
#  1. The resolver: the phrasings map, and nothing else does
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", [
    "pair my new light bulbs",
    "pair my new bulbs",
    "pair the new bulbs",
    "pair new zigbee devices",
    "pair my new devices",
    "put the house in pairing mode",
    "pairing mode please",
    "open the pairing window",
    "pair these switches",
])
def test_pairing_phrasings_resolve_to_zigbee_pair(text):
    res = resolve_intent(text)
    assert res.intent == "zigbee_pair", f"{text!r} -> {res.intent!r}"
    assert res.needs_entity is False
    assert res.state_changing is True


@pytest.mark.parametrize("text", [
    "pairing status",
    "what's the status of the pairing",
    "did you find any new devices",
    "did you find any new bulbs",
    "any new zigbee devices?",
    "what did you find",
    "what did you find when pairing?",
])
def test_status_phrasings_resolve_to_zigbee_pair_status(text):
    res = resolve_intent(text)
    assert res.intent == "zigbee_pair_status", f"{text!r} -> {res.intent!r}"
    assert res.needs_entity is False
    assert res.state_changing is False


@pytest.mark.parametrize("text", [
    # A knowledge question that merely shares a verb must NOT open a radio.
    "what did you find in my documents about the warranty",
    "what did you find out about the recipe",
    "add milk to the shopping list",
    "list devices",
    "turn on the porch light",
    "what's the status of the hallway thermostat",
])
def test_neighbouring_phrasings_are_not_hijacked(text):
    assert resolve_intent(text).intent not in ("zigbee_pair", "zigbee_pair_status")


def test_pairing_is_state_changing_but_status_is_not():
    assert "zigbee_pair" in STATE_CHANGING_INTENTS
    assert "zigbee_pair_status" not in STATE_CHANGING_INTENTS


def test_neither_intent_is_reachable_by_a_model_or_a_planner():
    """A pairing window is human-initiated or it does not happen.

    ``RESOLVABLE_INTENTS`` is the menu the optional LLM refinement path may pick
    from; ``CATALOGUE`` is what the planning loop may compose. Being absent from
    both leaves the deterministic phrase rules as the only route in.
    """
    from puffpai.agents.planning_loop import CATALOGUE

    assert "zigbee_pair" not in RESOLVABLE_INTENTS
    assert "zigbee_pair_status" not in RESOLVABLE_INTENTS
    assert not [key for key in CATALOGUE if key[1].startswith("zigbee_pair")]


# ══════════════════════════════════════════════════════════════════════
#  2. Filing the question opens nothing
# ══════════════════════════════════════════════════════════════════════

def test_request_pairing_files_a_request_and_opens_nothing(paired, monkeypatch):
    zp, cr, _ = paired
    from puffpai import zigbee_manager

    monkeypatch.setattr(zigbee_manager.ZigbeeManager, "open_permit_join",
                        Landmine("ZigbeeManager.open_permit_join"))

    out = zp.request_pairing("pair my new light bulbs")

    assert out["ok"] is True
    assert out["executed"] is False
    assert out["awaiting_approval"] is True
    open_reqs = cr.list_open()
    assert [r["action"] for r in open_reqs] == ["zigbee_pair"]
    assert open_reqs[0]["entity_id"] == zp.PAIRING_ENTITY_ID
    assert open_reqs[0]["context"]["duration"] == zp.DEFAULT_PAIRING_SECONDS
    # The reason has to say what an open window actually is.
    assert "any zigbee device" in open_reqs[0]["reason"].lower()
    assert "approve" in out["message"].lower()
    assert zp._session is None


def test_request_pairing_clamps_a_hostile_duration(paired):
    zp, cr, _ = paired

    zp.request_pairing("", duration=9999)

    assert cr.list_open()[0]["context"]["duration"] == zp.DEFAULT_PAIRING_SECONDS
    assert zp.DEFAULT_PAIRING_SECONDS <= zp.MAX_PAIRING_SECONDS


def test_the_steward_intent_files_the_request_and_actuates_nothing(paired,
                                                                   monkeypatch):
    zp, cr, _ = paired
    from puffpai import zigbee_manager

    monkeypatch.setattr(zigbee_manager.ZigbeeManager, "open_permit_join",
                        Landmine("ZigbeeManager.open_permit_join"))
    bridge = QuietBridge()
    steward = StewardAgent(ha_bridge=bridge)
    env = Envelope(intent="zigbee_pair",
                   payload={"task_text": "pair my new light bulbs"},
                   origin_agent="concierge", state_changing=True,
                   budget=Budget(max_actions=3))

    reply = steward.handle(env)

    assert reply["ok"] is True
    assert reply["awaiting_approval"] is True
    assert bridge.calls == []                    # nothing reached Home Assistant
    assert [r["action"] for r in cr.list_open()] == ["zigbee_pair"]


def test_a_read_only_envelope_cannot_carry_the_pairing_intent(paired):
    zp, cr, _ = paired
    steward = StewardAgent(ha_bridge=QuietBridge())
    env = Envelope(intent="zigbee_pair", payload={}, origin_agent="concierge",
                   state_changing=False, budget=Budget(max_actions=3))

    reply = steward.handle(env)

    assert reply["ok"] is False
    assert reply["requires_review"] is True
    assert cr.list_open() == []


# ══════════════════════════════════════════════════════════════════════
#  3. The completion hook is the only opener, and it needs the record
# ══════════════════════════════════════════════════════════════════════

def test_completion_without_an_approval_opens_nothing(paired):
    """The core control: no approved record, no window — from any caller."""
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs")          # filed, NOT approved
    out = zp.complete_approved_pairing(actuator)

    assert out["ok"] is False
    assert out["opened"] is False
    assert actuator.opens == []
    assert _watchers() == []
    assert zp._session is None


def test_a_denied_request_does_not_open_the_window(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs")
    cr.resolve(zp.PAIRING_ENTITY_ID, approved=False, by="user_via_chat")
    out = zp.complete_approved_pairing(actuator)

    assert out["opened"] is False
    assert actuator.opens == []


def test_an_agent_resolved_approval_is_not_a_human_approval(paired):
    """``resolved_by`` outside the human surfaces is not a mandate."""
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs")
    cr.resolve(zp.PAIRING_ENTITY_ID, approved=True, by="concierge")
    out = zp.complete_approved_pairing(actuator)

    assert out["opened"] is False
    assert actuator.opens == []


def test_a_stale_approval_is_not_a_mandate(paired, monkeypatch):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs")
    _approve(cr, zp)
    monkeypatch.setattr(zp, "APPROVAL_FRESHNESS_SECONDS", -1.0)

    out = zp.complete_approved_pairing(actuator)

    assert out["opened"] is False
    assert actuator.opens == []


def test_approval_opens_with_the_token_and_starts_exactly_one_watcher(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    out = zp.complete_approved_pairing(actuator)

    assert out["ok"] is True and out["opened"] is True
    assert len(actuator.opens) == 1
    duration, confirmation = actuator.opens[0]
    assert duration == 1
    # The token object itself — not True, not a truthy string.
    assert confirmation is HUMAN_CONFIRMED
    assert len(_watchers()) == 1
    assert "power-cycle" in out["message"].lower()

    _drain(zp)
    assert _watchers() == []


def test_an_approval_cannot_be_replayed_into_a_second_window(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    assert zp.complete_approved_pairing(actuator)["opened"] is True
    _drain(zp)

    # Same approval record, still inside the freshness horizon — and spent.
    again = zp.complete_approved_pairing(actuator)

    assert again["opened"] is False
    assert len(actuator.opens) == 1


def test_a_second_request_while_open_reports_status_instead_of_opening(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)

    second = zp.request_pairing("pair my new bulbs")

    assert second["already_open"] is True
    assert second["window_open"] is True
    assert len(actuator.opens) == 1
    assert len(_watchers()) == 1
    _drain(zp)


def test_a_second_completion_while_open_does_not_open_again(paired):
    """Even a fresh, unspent approval cannot open a second concurrent window.

    ``request_pairing`` refuses to file while a window is open, so the second
    request is filed directly here — the single-session guard is what is under
    test, not the request path that usually stops you reaching it.
    """
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)

    cr.create_request(entity_id=zp.PAIRING_ENTITY_ID, label=zp.PAIRING_LABEL,
                      action=zp.PAIRING_ACTION, reason="second ask",
                      requested_by="steward", context={"duration": 2})
    _approve(cr, zp, by="user_via_ui")
    second = zp.complete_approved_pairing(actuator)

    assert second.get("already_open") is True
    assert len(actuator.opens) == 1
    assert len(_watchers()) == 1
    _drain(zp)


def test_only_the_completion_hook_names_the_confirmation_token(paired):
    """Every *code* reference to the token lives in the one function that opens.

    Parsed rather than grepped so the cross-references in the docstrings — which
    are the point of the docstrings — do not have to be counted or kept at a
    fixed number.
    """
    import ast

    zp, _, _ = paired
    tree = ast.parse(inspect.getsource(zp))

    def refs(node):
        return [n for n in ast.walk(node)
                if (isinstance(n, ast.Name) and n.id == "HUMAN_CONFIRMED")
                or (isinstance(n, ast.Attribute) and n.attr == "HUMAN_CONFIRMED")
                or (isinstance(n, ast.alias) and n.name == "HUMAN_CONFIRMED")]

    opener = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "complete_approved_pairing")

    module_refs, opener_refs = refs(tree), refs(opener)
    assert opener_refs, "the opener must present the token"
    assert len(module_refs) == len(opener_refs), (
        "HUMAN_CONFIRMED is referenced outside complete_approved_pairing")


def _package_dir():
    import puffpai
    return os.path.dirname(os.path.abspath(puffpai.__file__))


def _function_source(relative_path: str, name: str) -> str:
    import ast

    path = os.path.join(_package_dir(), relative_path)
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found in {relative_path}")


@pytest.mark.parametrize("relative_path,function_name", [
    # The chat approval is the deterministic handler of the person's own
    # words; the LLM tool cannot approve.
    ("api_server.py", "_try_consent_approval"),         # chat/voice approval
    ("api_server.py", "sentinel_consent_resolve"),      # dashboard approval
])
def test_both_approval_surfaces_carry_the_completion_hook(relative_path,
                                                          function_name):
    """Deleting either hook is a silent feature outage — pin both."""
    source = _function_source(relative_path, function_name)
    assert "zigbee_pair" in source
    assert "complete_approved_pairing" in source


def test_nothing_outside_the_two_approval_surfaces_calls_the_opener():
    """The opener's callers are the approval sites, and only those.

    ``complete_approved_pairing`` refuses without a human-resolved record, so a
    stray caller could not actually open a window — but a stray caller would
    mean someone believes there is a second way in, so it fails here first.
    """
    import ast

    # The LLM tool in assistant_tools does not approve, so the deterministic
    # api_server surfaces are the only callers.
    allowed = {"api_server.py"}
    package = _package_dir()
    callers = set()
    for root, _dirs, files in os.walk(package):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(root, filename)
            with open(path, encoding="utf-8", errors="replace") as handle:
                source = handle.read()
            if "complete_approved_pairing" not in source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:                     # pragma: no cover
                continue
            # A *reference*, not a docstring cross-reference and not the
            # definition itself: the Steward documents the hook it must not call.
            referenced = any(
                (isinstance(n, ast.Attribute)
                 and n.attr == "complete_approved_pairing")
                or (isinstance(n, ast.Name)
                    and n.id == "complete_approved_pairing")
                for n in ast.walk(tree))
            if referenced:
                callers.add(os.path.relpath(path, package))

    assert callers == allowed, (
        f"callers of the opener are {sorted(callers)}, expected {sorted(allowed)}")


def test_the_real_manager_gate_still_refuses_an_unconfirmed_open():
    """Pin the layer underneath: nothing above may soften it."""
    from puffpai.zigbee_manager import ZigbeeManager

    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token",
                        bridge=Landmine("bridge"))
    mgr._rest = Landmine("_rest")

    with pytest.raises(ConfirmationRequired):
        mgr.open_permit_join(120)
    with pytest.raises(ConfirmationRequired):
        mgr.open_permit_join(120, human_confirmed=True)      # a bool is not the token
    with pytest.raises(ConfirmationRequired):
        mgr.open_permit_join(120, human_confirmed="yes")


# ══════════════════════════════════════════════════════════════════════
#  4. The watcher: reads, asks, never approves
# ══════════════════════════════════════════════════════════════════════

def test_watcher_asks_once_per_new_device_and_writes_a_report(paired):
    zp, cr, tmp_path = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)

    actuator.add(NEW_BULB, NEW_SENSOR)
    _drain(zp)

    # One consent request per newly paired ACTUATABLE device, exactly once.
    control = [r for r in cr._load()
               if r.get("action") == zp.DEVICE_CONSENT_ACTION]
    assert [r["entity_id"] for r in control] == ["light.new_bulb"]
    assert control[0]["status"] == "open"
    assert control[0]["context"]["source"] == zp.PAIRING_SOURCE
    assert control[0]["requested_by"] == "steward"
    assert "newly paired" in control[0]["reason"]

    # The report names everything new, actuatable or not.
    report_file = tmp_path / "zigbee_pairing_report.json"
    assert report_file.is_file()
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["window_seconds"] == 2
    assert report["started_at"]
    assert {d["entity_id"] for d in report["new_entities"]} == {
        "light.new_bulb", "sensor.new_bulb_energy"}
    assert report["consent_filed"] == ["light.new_bulb"]
    assert zp.read_report() == report


def test_a_newly_paired_device_is_not_auto_consented(paired):
    """The request IS the ask. Only the owner's answer makes it controllable."""
    zp, cr, _ = paired
    import puffpai.security.consent_gate as cg
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)
    actuator.add(NEW_BULB)
    _drain(zp)

    assert cg.get_store().status("light.new_bulb") != "approved"


def test_the_watcher_never_actuates_and_never_reopens(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)
    actuator.add(NEW_BULB)
    _drain(zp)

    # One open, ever. The only other permit call it may make is a CLOSE.
    assert len(actuator.opens) == 1
    assert actuator.closes <= 1


def test_a_watcher_whose_reads_explode_still_writes_a_report(paired):
    """The thread must never raise out; a dead HA is not a stuck session."""
    zp, cr, tmp_path = paired

    class BrokenReads(StubActuator):
        def get_all_states(self, *, force=False):
            raise RuntimeError("home assistant is down")

    actuator = BrokenReads()
    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    out = zp.complete_approved_pairing(actuator)

    assert out["opened"] is True
    _drain(zp)
    assert (tmp_path / "zigbee_pairing_report.json").is_file()
    assert zp.read_report()["new_entities"] == []
    assert zp._session is None


# ══════════════════════════════════════════════════════════════════════
#  5. Status, rendered for a person
# ══════════════════════════════════════════════════════════════════════

def test_status_before_anything_has_happened(paired):
    zp, _, _ = paired
    status = zp.pairing_status()

    assert status["window_open"] is False
    assert status["last_report"] is None
    assert status["awaiting_approval"] == []
    assert "haven't run" in status["message"]


def test_the_status_intent_renders_what_was_found(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)
    actuator.add(NEW_BULB)
    _drain(zp)

    steward = StewardAgent(ha_bridge=QuietBridge())
    env = Envelope(intent="zigbee_pair_status", payload={},
                   origin_agent="concierge", state_changing=False,
                   budget=Budget(max_actions=3))
    reply = steward.handle(env)

    assert reply["ok"] is True
    assert reply["window_open"] is False
    assert [a["entity_id"] for a in reply["awaiting_approval"]] == \
        ["light.new_bulb"]

    prose = ConciergeAgent()._render("zigbee_pair_status", None, reply)
    assert "New Bulb" in prose
    assert "light.new_bulb" in prose
    assert "approve" in prose.lower()


def test_status_names_a_window_that_is_still_open(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)

    status = zp.pairing_status()
    assert status["window_open"] is True
    assert 0 < status["seconds_remaining"] <= 2
    assert "power-cycle" in status["message"].lower()
    _drain(zp)


def test_the_pairing_reply_reaches_the_user_as_prose(paired, monkeypatch):
    zp, cr, _ = paired
    from puffpai import zigbee_manager

    monkeypatch.setattr(zigbee_manager.ZigbeeManager, "open_permit_join",
                        Landmine("ZigbeeManager.open_permit_join"))
    steward = StewardAgent(ha_bridge=QuietBridge())
    env = Envelope(intent="zigbee_pair", payload={"task_text": "pair my bulbs"},
                   origin_agent="concierge", state_changing=True,
                   budget=Budget(max_actions=3))
    reply = steward.handle(env)

    prose = ConciergeAgent()._render("zigbee_pair", None, reply)
    assert "approval" in prose.lower() or "approve" in prose.lower()


# ══════════════════════════════════════════════════════════════════════
#  6. Stale pairing requests auto-expire — and a timeout is never a denial
# ══════════════════════════════════════════════════════════════════════
#
# Auto-expiry: the "may I open the window?" ask
# lives 6 hours, the per-device "may I manage this?" ask lives 30 days, and a
# lapsed request flips to a distinct ``expired`` status. Expiry withdraws the
# question; it must NEVER record a verdict, because a request nobody answered is
# not a request somebody denied.


def test_fresh_pairing_requests_carry_a_ttl_but_do_not_expire(paired):
    """The horizons are the owner's, and a just-filed request is nowhere near."""
    zp, cr, _ = paired
    assert zp.WINDOW_REQUEST_TTL_SECONDS == 6 * 60 * 60
    assert zp.DEVICE_REQUEST_TTL_SECONDS == 30 * 24 * 60 * 60
    assert zp.WINDOW_REQUEST_TTL_SECONDS < zp.DEVICE_REQUEST_TTL_SECONDS

    zp.request_pairing("pair my new bulbs")

    open_reqs = cr.list_open()
    assert [r["action"] for r in open_reqs] == ["zigbee_pair"]
    assert "expires_at" in open_reqs[0]          # opted in, but comfortably ahead


def test_an_unanswered_window_request_expires_and_stops_nagging(paired,
                                                                monkeypatch):
    zp, cr, _ = paired
    # File it already past due: expires_at = now + (-1 s), so the next read sweeps.
    monkeypatch.setattr(zp, "WINDOW_REQUEST_TTL_SECONDS", -1)
    zp.request_pairing("pair my new bulbs")

    status = zp.pairing_status()

    assert status["pending_request"] is False
    assert "waiting on your approval" not in status["message"].lower()
    assert cr.list_open() == []
    # The durable record holds a distinct terminal status, not a denial.
    record = [r for r in cr._load() if r.get("action") == "zigbee_pair"]
    assert len(record) == 1
    assert record[0]["status"] == "expired"
    assert record[0]["expired_at"]


def test_expiry_records_no_verdict_in_the_consent_store(paired, monkeypatch):
    """The load-bearing safety property: a lapsed question is not a denial."""
    zp, cr, _ = paired
    import puffpai.security.consent_gate as cg
    monkeypatch.setattr(zp, "WINDOW_REQUEST_TTL_SECONDS", -1)

    zp.request_pairing("pair my new bulbs")
    cr.list_open()                                # triggers the sweep

    store = cg.get_store()
    assert store.status(zp.PAIRING_ENTITY_ID) == "pending"    # never "denied"
    assert store._data.get(zp.PAIRING_ENTITY_ID) is None      # no record written


def test_an_expired_window_cannot_be_approved_into_a_radio(paired, monkeypatch):
    """A week-late 'approve pairing' finds nothing to approve, on either surface."""
    zp, cr, _ = paired
    actuator = StubActuator()
    monkeypatch.setattr(zp, "WINDOW_REQUEST_TTL_SECONDS", -1)
    zp.request_pairing("pair my new bulbs")

    # Dashboard surface: resolve() sweeps first, so it sees no open request.
    assert cr.resolve(zp.PAIRING_ENTITY_ID, approved=True, by="user_via_ui") is None
    # Chat surface: find_open() reads through the same sweep.
    assert cr.find_open(zp.PAIRING_ENTITY_ID) is None
    # And the opener refuses on its own re-read of the record: no window, no watcher.
    out = zp.complete_approved_pairing(actuator)
    assert out["opened"] is False
    assert actuator.opens == []
    assert _watchers() == []
    assert zp._session is None


def test_a_stale_per_device_request_expires_without_a_denial(paired, monkeypatch):
    zp, cr, _ = paired
    import puffpai.security.consent_gate as cg
    monkeypatch.setattr(zp, "DEVICE_REQUEST_TTL_SECONDS", -1)

    zp._file_device_consent("light.new_bulb", "New Bulb")

    assert cr.list_open() == []
    record = [r for r in cr._load()
              if r.get("action") == zp.DEVICE_CONSENT_ACTION]
    assert len(record) == 1
    assert record[0]["status"] == "expired"
    assert record[0]["context"]["source"] == zp.PAIRING_SOURCE
    # The bulb was never denied — the appliance simply stopped asking about it.
    assert cg.get_store().status("light.new_bulb") == "pending"


def test_expiry_is_scoped_to_opted_in_requests_only(paired, monkeypatch):
    """Only requests that asked for a TTL expire; every other Sentinel ask stays.

    Proves the scope. A camera add filed by a different producer carries no
    ``ttl_seconds``, so the sweep never touches it — even sitting next to a
    pairing request that is already past due.
    """
    zp, cr, _ = paired
    monkeypatch.setattr(zp, "WINDOW_REQUEST_TTL_SECONDS", -1)

    cr.create_request(entity_id="camera.front_door", label="Front Door",
                      action="dashboard_add_camera",
                      reason="a Sentinel block unrelated to pairing")
    zp.request_pairing("pair my new bulbs")            # opted in, and already stale

    assert sorted(r["action"] for r in cr.list_open()) == ["dashboard_add_camera"]
    camera = [r for r in cr._load()
              if r.get("action") == "dashboard_add_camera"][0]
    assert camera["status"] == "open"
    assert "expires_at" not in camera



# ── the close report counts devices, in its thread ──

def test_the_close_report_counts_devices_and_goes_to_the_starting_thread(
        paired, monkeypatch):
    zp, cr, _ = paired
    from puffpai import announcer
    spoken, posted = [], []
    monkeypatch.setattr(announcer, "announce",
                        lambda text, **kw: spoken.append(text) or {"ok": True})
    monkeypatch.setattr(zp, "_current_conversation", lambda: "home-owner")
    zp.set_thread_poster(lambda text, conv=None: posted.append((text, conv)))
    try:
        actuator = StubActuator()
        zp.request_pairing("pair my new bulbs", duration=2)
        _approve(cr, zp)
        monkeypatch.setattr(zp, "_current_conversation", lambda: "other-thread")
        zp.complete_approved_pairing(actuator)
        actuator.add(NEW_BULB, NEW_SENSOR)       # one bulb, two entities
        _drain(zp)
    finally:
        zp.set_thread_poster(None)
        zp.clear_reopen_offer()
    text, conv = posted[-1]
    assert conv == "home-owner"
    assert text.startswith("The pairing window closed. 1 new device joined: "
                           "New Bulb.")
    assert spoken[-1] == text
    report = zp.read_report()
    assert report["device_count"] == 1 and report["new_devices"] == ["New Bulb"]


def test_an_empty_window_reports_nothing_new_joined_to_its_thread(
        paired, monkeypatch):
    zp, cr, _ = paired
    from puffpai import announcer
    posted = []
    monkeypatch.setattr(announcer, "announce", lambda text, **kw: {"ok": True})
    monkeypatch.setattr(zp, "_current_conversation", lambda: "home-owner")
    zp.set_thread_poster(lambda text, conv=None: posted.append((text, conv)))
    try:
        zp.request_pairing("pair my new bulbs", duration=1)
        _approve(cr, zp)
        zp.complete_approved_pairing(StubActuator())
        _drain(zp)
    finally:
        zp.set_thread_poster(None)
        zp.clear_reopen_offer()
    text, conv = posted[-1]
    assert conv == "home-owner"
    assert text.startswith("The pairing window closed and nothing new joined.")


def test_the_status_read_counts_devices_not_entities(paired):
    zp, cr, _ = paired
    actuator = StubActuator()
    zp.request_pairing("pair my new bulbs", duration=2)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)
    actuator.add(NEW_BULB, NEW_SENSOR)
    _drain(zp)
    prose = zp._status_prose(False, 0, False, [], zp.read_report())
    assert "found 1 new device: New Bulb (" in prose
    assert "2 new device" not in prose
    # A report written before new_devices existed is grouped too.
    old = dict(zp.read_report())
    old.pop("new_devices")
    prose = zp._status_prose(False, 0, False, [], old)
    assert "found 1 new device: New Bulb (" in prose
