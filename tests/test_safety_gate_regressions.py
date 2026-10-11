"""Regressions for four safety-gate breaches under adversarial testing.

Every test here is written to **fail** against the code as it stood before the
fix, so a later refactor that reintroduces the hole reintroduces a red test.

1. The always-confirm class was keyed on the intent *name* (``"siren"``), not on
   the target, so ``control_device`` on ``siren.shed_siren`` fired the siren.
   This is the safety-critical one, and it was live on the **single-step** path,
   not only inside the Phase 4 loop.
2. The planning loop's budget widened three ways: an exhausted parent still
   granted an action and a fresh second, per-step sub-budgets let the run spend
   more than its ceiling, and the wall clock was only checked between steps.
3. ``ConciergeAgent._send_sub`` passed a caller-supplied ``max_actions`` through
   unclamped, and mutated the caller's ``Budget`` in place.
4. Softer: the deny-list skipped parameters on read steps, and the repeat
   detector keyed on a planner-invented ``target`` for target-less intents.

Pure in-process: no HA, no sockets, no LLM.
Run: pytest tests/test_safety_gate_regressions.py -q
"""

from __future__ import annotations

import pytest

from puffpai.agents.base import BaseAgent
from puffpai.agents.bus import AgentBus
from puffpai.agents.concierge import ConciergeAgent
from puffpai.agents.envelope import Budget, BudgetExceeded, Envelope, ReviewVerdict
from puffpai.agents.planning_loop import (
    MAX_TARGETLESS_REPEATS,
    PLANNING_LOOP_ENV,
    PlanningLoop,
)
from puffpai.agents.steward import (
    SAFETY_CRITICAL_DOMAINS,
    StewardAgent,
    requires_human_confirmation,
)
from puffpai.security.audit_log import AuditLog
from puffpai.security.secrets_provider import AgentScope

HOUSE = [
    {"entity_id": "siren.shed_siren", "name": "Shed Siren"},
    {"entity_id": "lock.front_door", "name": "Front Door Lock"},
    {"entity_id": "light.porch_light", "name": "Porch Light"},
    {"entity_id": "light.kitchen_light", "name": "Kitchen Light"},
    {"entity_id": "light.hall_light", "name": "Hall Light"},
    {"entity_id": "light.study_light", "name": "Study Light"},
    {"entity_id": "light.attic_light", "name": "Attic Light"},
    {"entity_id": "light.garden_light", "name": "Garden Light"},
]

#: Six distinct targets, so a six-step plan is six *different* actions. Repeating
#: one target would trip the repeat detector at step two and the budget hole
#: would never get a chance to show — a test that stops early proves nothing.
SIX_LIGHTS = ["porch light", "kitchen light", "hall light", "study light",
              "attic light", "garden light"]


# ── stubs ───────────────────────────────────────────────────────────

class RecordingBridge:
    """HA bridge stand-in that records everything that reaches the wire."""

    def __init__(self, states=None):
        self.calls: list[tuple[str, str, str | None, dict]] = []
        self.states = states or {}

    def call_service(self, domain, service, entity_id=None, **data):
        self.calls.append((domain, service, entity_id, dict(data)))
        return []

    def control_device(self, entity_id, action, **kwargs):
        # Mirrors HABridge: the *service domain is derived from the entity id*,
        # which is what made an intent-name gate meaningless.
        self.call_service(entity_id.split(".")[0], action, entity_id, **kwargs)
        return {"success": True, "entity_id": entity_id, "action": action}

    def activate_scene(self, entity_id):
        self.call_service(entity_id.split(".")[0], "turn_on", entity_id)
        return {"success": True, "entity_id": entity_id, "action": "activated"}

    def get_entity_state(self, entity_id):
        return self.states.get(entity_id, {"state": "off"})

    def get_camera_image(self, entity_id):
        return b"\xff\xd8jpeg", "image/jpeg"

    def list_devices(self, domain=None, controllable_only=True):
        return [_Device(d["entity_id"], d["name"]) for d in HOUSE
                if domain is None or d["entity_id"].startswith(f"{domain}.")]


class _Device:
    """Minimal DeviceInfo stand-in — what ``StewardAgent._device_dict`` reads."""

    def __init__(self, entity_id, name, state="off"):
        self.entity_id = entity_id
        self.name = name
        self.state = state


class AllowingSentinel(BaseAgent):
    """A Sentinel that consents to everything — the point is what happens next.

    The bypass was verified with the siren *already consented*: Sentinel's
    consent gate is not the always-confirm gate, and a consented siren must
    still refuse to fire without a human confirmation.
    """

    name = "sentinel"
    scope = AgentScope.SENTINEL

    def __init__(self):
        super().__init__()
        self.reviews: list[tuple[str, str]] = []

    def review(self, envelope: Envelope, target: str) -> ReviewVerdict:
        self.reviews.append((target, envelope.intent))
        return ReviewVerdict(allowed=True, verdict="allow", reason="consented")

    def handle(self, envelope: Envelope) -> dict:
        return {"ok": True}


def house_team(tmp_path, bridge=None):
    """Real bus + real Concierge + REAL Steward + an all-allowing Sentinel."""
    bridge = bridge or RecordingBridge()
    audit = AuditLog(log_file=str(tmp_path / "audit.jsonl"))
    bus = AgentBus(audit=audit)
    concierge = ConciergeAgent()
    steward = StewardAgent(ha_bridge=bridge)
    for agent in (concierge, steward, AllowingSentinel()):
        bus.register(agent)
    return bus, concierge, bridge, audit


def steward_env(intent, payload, *, origin="concierge", actions=4):
    return Envelope(
        intent=intent,
        payload=payload,
        requester="owner",
        origin_agent=origin,
        budget=Budget(max_actions=actions, max_seconds=30.0),
        state_changing=True,
    )


# ══════════════════════════════════════════════════════════════════════
#  BREACH 1 — the always-confirm class is a property of the TARGET
# ══════════════════════════════════════════════════════════════════════

def test_safety_critical_domains_cover_the_physical_risk_set():
    for domain in ("siren", "lock", "alarm_control_panel", "cover", "valve"):
        assert domain in SAFETY_CRITICAL_DOMAINS


@pytest.mark.parametrize("entity_id", [
    "siren.shed_siren",
    "lock.front_door",
    "alarm_control_panel.house",
    "cover.garage_door",
    "valve.main_water",
])
def test_control_device_on_a_safety_critical_entity_never_actuates(tmp_path,
                                                                   entity_id):
    """THE breach: a real Steward, a consenting Sentinel, no human origin.

    ``control_device`` derives its HA service domain from the entity id, so
    keying the always-confirm class on the intent name let this through as
    ``siren.turn_on``. Zero calls must reach the bridge.
    """
    bridge = RecordingBridge()
    steward = StewardAgent(ha_bridge=bridge)

    reply = steward.handle(steward_env(
        "control_device", {"entity_id": entity_id, "action": "turn_on"}))

    assert bridge.calls == [], f"{entity_id} actuated without confirmation"
    assert reply.get("executed") is False
    assert reply.get("requires_confirmation") is True


def test_a_human_confirmed_safety_critical_control_does_execute(tmp_path):
    """The gate must engage, not seize."""
    bridge = RecordingBridge()
    steward = StewardAgent(ha_bridge=bridge)

    reply = steward.handle(steward_env(
        "control_device",
        {"entity_id": "siren.shed_siren", "action": "turn_on", "confirmed": True},
        origin="voice"))

    assert reply["ok"] is True
    assert bridge.calls == [("siren", "turn_on", "siren.shed_siren", {})]


def test_confirmed_from_an_agent_origin_is_still_refused():
    """An agent cannot mint its own confirmation: origin must be human."""
    bridge = RecordingBridge()
    steward = StewardAgent(ha_bridge=bridge)

    reply = steward.handle(steward_env(
        "control_device",
        {"entity_id": "siren.shed_siren", "action": "turn_on", "confirmed": True},
        origin="concierge"))

    assert bridge.calls == []
    assert reply.get("requires_confirmation") is True


@pytest.mark.parametrize("text,intent", [
    # Names the siren, so the Concierge resolves the ``siren`` intent — the path
    # the old intent-name gate did cover.
    ("turn on the shed siren", "siren"),
    # Does NOT name it. "turn on the shed" is an ordinary control phrase, so it
    # resolves to ``control_device``, whose capability declares no domains, and
    # fuzzy matching lands it on siren.shed_siren. This is the live bypass: same
    # entity, same HA service, different intent name.
    ("turn on the shed", "control_device"),
])
def test_single_step_chat_path_cannot_fire_a_siren(tmp_path, text, intent):
    """The path that is live today — no planning loop involved at all."""
    bus, _concierge, bridge, _audit = house_team(tmp_path)

    env = Envelope(intent="chat", payload={"text": text},
                   requester="owner", origin_agent="api_server",
                   budget=Budget(max_actions=6, max_seconds=20.0))
    out = bus.send(env, to="concierge")

    # The test is only meaningful if the turn really took the route it names and
    # really aimed at the siren.
    assert out["intent"] == intent
    assert out["targets"] == ["siren.shed_siren"]
    assert bridge.calls == []


def test_scene_that_contains_a_safety_critical_member_is_refused():
    """A scene is a wrapper: gate on what it would actually actuate."""
    bridge = RecordingBridge(states={
        "scene.panic": {"state": "on",
                        "attributes": {"entity_id": ["siren.shed_siren",
                                                     "light.porch_light"]}},
    })
    steward = StewardAgent(ha_bridge=bridge)

    reply = steward.handle(steward_env("activate_scene",
                                       {"entity_id": "scene.panic"}))

    assert bridge.calls == []
    assert reply.get("requires_confirmation") is True


def test_ordinary_scene_still_activates():
    bridge = RecordingBridge(states={
        "scene.movie_night": {"state": "on",
                              "attributes": {"entity_id": ["light.porch_light"]}},
    })
    steward = StewardAgent(ha_bridge=bridge)

    reply = steward.handle(steward_env("activate_scene",
                                       {"entity_id": "scene.movie_night"}))

    assert reply["ok"] is True
    assert bridge.calls == [("scene", "turn_on", "scene.movie_night", {})]


def test_ordinary_control_is_untouched():
    bridge = RecordingBridge()
    steward = StewardAgent(ha_bridge=bridge)
    reply = steward.handle(steward_env(
        "control_device", {"entity_id": "light.porch_light", "action": "turn_on"}))
    assert reply["ok"] is True
    assert bridge.calls == [("light", "turn_on", "light.porch_light", {})]


def test_planning_loop_confirm_gate_is_single_sourced():
    """The loop must consult the Steward's authority, not its own name list."""
    from puffpai.agents.planning_loop import CATALOGUE, PlanStep

    loop = PlanningLoop(host=None)
    step = PlanStep(cap=CATALOGUE[("steward", "control_device")],
                    target_text="shed siren", params={"action": "turn_on"})
    step.entity_id = "siren.shed_siren"
    assert loop._needs_confirmation(step) is True

    benign = PlanStep(cap=CATALOGUE[("steward", "control_device")],
                      target_text="porch light", params={"action": "turn_on"})
    benign.entity_id = "light.porch_light"
    assert loop._needs_confirmation(benign) is False

    # And a read against the same entity is not gated — reads never actuate.
    read = PlanStep(cap=CATALOGUE[("steward", "device_status")],
                    target_text="shed siren")
    read.entity_id = "siren.shed_siren"
    assert loop._needs_confirmation(read) is False


def test_requires_human_confirmation_is_the_exported_authority():
    assert requires_human_confirmation(intent="control_device",
                                       entity_id="lock.front_door") is True
    assert requires_human_confirmation(intent="control_device",
                                       entity_id="light.kitchen") is False
    assert requires_human_confirmation(intent="siren") is True
    # Reads are never in the class.
    assert requires_human_confirmation(intent="device_status",
                                       entity_id="lock.front_door",
                                       state_changing=False) is False


# ══════════════════════════════════════════════════════════════════════
#  BREACH 2 — the budget must only ever shrink
# ══════════════════════════════════════════════════════════════════════

class _NullHost:
    name = "concierge"


def test_exhausted_parent_budget_yields_nothing(monkeypatch):
    """``max(1, remaining)`` granted an action off an exhausted parent."""
    loop = PlanningLoop(host=_NullHost())
    spent = Budget(max_actions=2, max_seconds=30.0)
    spent.consume(2)
    env = Envelope(intent="chat", budget=spent)

    with pytest.raises(BudgetExceeded):
        loop._derive_budget(env)


def test_parent_past_its_wall_clock_yields_nothing():
    loop = PlanningLoop(host=_NullHost())
    stale = Budget(max_actions=5, max_seconds=0.0)
    stale.start()
    env = Envelope(intent="chat", budget=stale)

    with pytest.raises(BudgetExceeded):
        loop._derive_budget(env)


def test_derived_budget_never_exceeds_the_parent():
    loop = PlanningLoop(host=_NullHost(), max_actions=12, max_seconds=60.0)
    parent = Budget(max_actions=4, max_seconds=9.0)
    derived = loop._derive_budget(Envelope(intent="chat", budget=parent))
    assert derived.max_actions == 4
    assert derived.max_seconds <= 9.0
    assert derived.allow_egress is False


class SpendingSteward(BaseAgent):
    """A Steward that spends its whole sub-budget on every step."""

    name = "steward"
    scope = AgentScope.STEWARD

    def __init__(self):
        super().__init__()
        self.spent = 0

    def handle(self, envelope: Envelope) -> dict:
        if envelope.intent == "list_devices":
            return {"ok": True, "devices": list(HOUSE)}
        while True:
            try:
                envelope.budget.consume(1)
            except BudgetExceeded:
                break
            self.spent += 1
        return {"ok": True, "intent": envelope.intent,
                "entity_id": (envelope.payload or {}).get("entity_id"),
                "n": self.spent}


class GreedyPlanEngine:
    def __init__(self, steps):
        self.steps = steps

    def plan_steps(self, goal, catalogue):
        return list(self.steps)

    def run(self, *a, **kw):
        return None


def test_run_ceiling_bounds_actual_downstream_spend(tmp_path, monkeypatch):
    """A 6-step run with a 12-action ceiling spent 18 real actions."""
    monkeypatch.setenv(PLANNING_LOOP_ENV, "1")
    audit = AuditLog(log_file=str(tmp_path / "audit.jsonl"))
    bus = AgentBus(audit=audit)
    concierge = ConciergeAgent()
    steward = SpendingSteward()
    for agent in (concierge, steward, AllowingSentinel()):
        bus.register(agent)

    steps = [{"agent": "steward", "intent": "control_device",
              "target": light, "action": "turn_on"} for light in SIX_LIGHTS]
    concierge._loop = PlanningLoop(concierge, engine=GreedyPlanEngine(steps),
                                   audit=audit, max_steps=6, max_actions=12,
                                   max_seconds=60.0)

    env = Envelope(intent="chat", payload={"text": "do the thing"},
                   requester="owner", origin_agent="api_server",
                   budget=Budget(max_actions=40, max_seconds=60.0))
    reply = bus.send(env, to="concierge")

    # Guard the guard: if the run stopped after one step the ceiling was never
    # under pressure and this assertion would be vacuous.
    assert reply["plan"]["executed"] >= 3, reply["plan"]["stop_reason"]
    assert steward.spent <= 12, (
        f"run ceiling was 12 actions but the house really spent {steward.spent}")


def test_step_slice_never_outlives_the_run_wall_clock():
    """A step's time slice comes out of what the run has left, not a fresh 1.0s."""
    loop = PlanningLoop(host=_NullHost(), max_seconds=5.0)
    run = Budget(max_actions=6, max_seconds=0.5)
    run.start()
    slice_seconds = loop._step_seconds(run)
    assert slice_seconds <= 0.5


# ══════════════════════════════════════════════════════════════════════
#  BREACH 3 — _send_sub must clamp, and must not mutate the caller
# ══════════════════════════════════════════════════════════════════════

class CountingSteward(BaseAgent):
    name = "steward"
    scope = AgentScope.STEWARD

    def __init__(self):
        super().__init__()
        self.seen: list[Envelope] = []

    def handle(self, envelope: Envelope) -> dict:
        self.seen.append(envelope)
        return {"ok": True, "max_actions": envelope.budget.max_actions}


def _counting_team(tmp_path):
    audit = AuditLog(log_file=str(tmp_path / "audit.jsonl"))
    bus = AgentBus(audit=audit)
    concierge = ConciergeAgent()
    steward = CountingSteward()
    for agent in (concierge, steward, AllowingSentinel()):
        bus.register(agent)
    return concierge, steward


def test_send_sub_clamps_an_oversized_caller_budget(tmp_path):
    """9999 was honoured verbatim."""
    concierge, steward = _counting_team(tmp_path)
    parent = Envelope(intent="chat", requester="owner", origin_agent="api_server",
                      budget=Budget(max_actions=5, max_seconds=20.0))

    concierge._send_sub(parent, "steward", "device_status",
                        {"entity_id": "light.porch_light"},
                        state_changing=False,
                        budget=Budget(max_actions=9999, max_seconds=9999.0))

    sub = steward.seen[-1]
    assert sub.budget.max_actions <= 5
    assert sub.budget.max_seconds <= 20.0


def test_send_sub_does_not_mutate_the_callers_budget(tmp_path):
    concierge, _steward = _counting_team(tmp_path)
    parent = Envelope(intent="chat", requester="owner", origin_agent="api_server",
                      budget=Budget(max_actions=5, max_seconds=20.0))
    mine = Budget(max_actions=2, max_seconds=5.0, allow_egress=True)

    concierge._send_sub(parent, "steward", "device_status",
                        {"entity_id": "light.porch_light"},
                        state_changing=False, budget=mine)

    assert mine.allow_egress is True, "_send_sub wrote to the caller's Budget"
    assert mine.actions_used == 0


def test_send_sub_denies_egress_when_the_parent_has_none(tmp_path):
    """A caller asking for egress off a parent that has none still gets none.

    Renamed from ``test_send_sub_always_denies_egress_on_the_wire``: the
    assertion is unchanged and still the point, but "always" became a lie when
    ``_sub_budget`` learned to pass egress through for a parent that really
    holds it. The old name would have read as a guarantee this test never made —
    the parent here has no egress, so this only ever pinned the *no-minting*
    half. The other half is pinned directly below.
    """
    concierge, steward = _counting_team(tmp_path)
    parent = Envelope(intent="chat", requester="owner", origin_agent="api_server",
                      budget=Budget(max_actions=5, max_seconds=20.0))
    concierge._send_sub(parent, "steward", "device_status",
                        {"entity_id": "light.porch_light"},
                        state_changing=False,
                        budget=Budget(max_actions=1, allow_egress=True))
    assert steward.seen[-1].budget.allow_egress is False


def test_send_sub_passes_egress_only_when_parent_and_caller_both_grant_it(
        tmp_path):
    """Both halves required, in both directions. Never minted, never silent."""
    concierge, steward = _counting_team(tmp_path)
    granting = Envelope(intent="chat", requester="owner",
                        origin_agent="api_server",
                        budget=Budget(max_actions=5, max_seconds=20.0,
                                      allow_egress=True))
    plain = Envelope(intent="chat", requester="owner",
                     origin_agent="api_server",
                     budget=Budget(max_actions=5, max_seconds=20.0))

    # Parent grants + caller asks → the only case that passes.
    concierge._send_sub(granting, "steward", "device_status", {},
                        state_changing=False,
                        budget=Budget(max_actions=1, allow_egress=True))
    assert steward.seen[-1].budget.allow_egress is True

    # Parent grants + caller silent → False. Egress is not inherited by a step
    # that never asked for it; least privilege is per sub-request.
    concierge._send_sub(granting, "steward", "device_status", {},
                        state_changing=False,
                        budget=Budget(max_actions=1))
    assert steward.seen[-1].budget.allow_egress is False

    # Parent grants + caller passes no budget at all → False.
    concierge._send_sub(granting, "steward", "device_status", {},
                        state_changing=False)
    assert steward.seen[-1].budget.allow_egress is False

    # Parent has none + caller asks → False. This is the minting case.
    concierge._send_sub(plain, "steward", "device_status", {},
                        state_changing=False,
                        budget=Budget(max_actions=1, allow_egress=True))
    assert steward.seen[-1].budget.allow_egress is False


# ══════════════════════════════════════════════════════════════════════
#  BREACH 4 (softer) — deny-list on reads, repeat detector on target-less
# ══════════════════════════════════════════════════════════════════════

def test_deny_list_checks_params_on_read_steps_too():
    from puffpai.agents.planning_loop import CATALOGUE, PlanStep

    loop = PlanningLoop(host=_NullHost())
    step = PlanStep(cap=CATALOGUE[("archivist", "retrieve")],
                    params={"query": "the consent ledger and the vault token"})
    assert loop._deny_listed(step) is True


def test_targetless_intents_do_not_get_a_free_identity_from_a_fake_target():
    from puffpai.agents.planning_loop import CATALOGUE, PlanStep

    cap = CATALOGUE[("steward", "notify_user")]
    a = PlanStep(cap=cap, target_text="alpha", params={"message": "x"})
    b = PlanStep(cap=cap, target_text="bravo", params={"message": "x"})
    assert a.key == b.key, "a planner-invented target must not create identity"


class NotifyCountingSteward(BaseAgent):
    name = "steward"
    scope = AgentScope.STEWARD

    def __init__(self):
        super().__init__()
        self.notifications = 0

    def handle(self, envelope: Envelope) -> dict:
        if envelope.intent == "list_devices":
            return {"ok": True, "devices": list(HOUSE)}
        if envelope.intent == "notify_user":
            self.notifications += 1
            return {"ok": True, "service": "notify.persistent_notification",
                    "message": (envelope.payload or {}).get("message")}
        return {"ok": True}


def test_targetless_state_changing_intent_is_rate_capped(tmp_path, monkeypatch):
    """Eight notify steps with distinct targets used to produce six real ones."""
    monkeypatch.setenv(PLANNING_LOOP_ENV, "1")
    audit = AuditLog(log_file=str(tmp_path / "audit.jsonl"))
    bus = AgentBus(audit=audit)
    concierge = ConciergeAgent()
    steward = NotifyCountingSteward()
    for agent in (concierge, steward, AllowingSentinel()):
        bus.register(agent)

    steps = [{"agent": "steward", "intent": "notify_user",
              "target": f"dummy-{i}", "message": f"ping {i}"}
             for i in range(8)]
    concierge._loop = PlanningLoop(concierge, engine=GreedyPlanEngine(steps),
                                   audit=audit, max_steps=8, max_actions=20,
                                   max_seconds=60.0)

    env = Envelope(intent="chat", payload={"text": "do the thing"},
                   requester="owner", origin_agent="api_server",
                   budget=Budget(max_actions=40, max_seconds=60.0))
    reply = bus.send(env, to="concierge")

    assert steward.notifications <= MAX_TARGETLESS_REPEATS, (
        f"the loop pushed {steward.notifications} notifications")
    # And it must have stopped *because of the cap*, not because it happened to
    # run out of budget or trip the no-progress detector on identical replies.
    assert reply["plan"]["stop_reason"] == "repeat", reply["plan"]["stop_reason"]
