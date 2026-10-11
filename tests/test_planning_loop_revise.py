"""Adversarial tests for the wired observe→revise path of the planning loop.

The loop can now amend its remaining plan from what really happened — but a
revision is the "content-drives-action" vector, so every test here is written to
fail against a loop that let a revision cheat any of the four protections:

* it sees SAFE SCALARS ONLY (``_observe`` is the choke point — no fetched page,
  document, file body, error message or rendered prose ever reaches the planner);
* the sealed :class:`EgressScope` cannot be widened by a revision;
* every revised step re-passes the parse/deny/confirm/entity/egress gates;
* a run may revise at most :data:`MAX_REVISIONS` times (anti-thrash).

Real :class:`AgentBus`, real per-step review path. No HA, no sockets, no LLM.
Run: pytest tests/test_planning_loop_revise.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents.base import BaseAgent
from puffpai.agents.bus import AgentBus
from puffpai.agents.concierge import ConciergeAgent
from puffpai.agents.envelope import Budget, Envelope, ReviewVerdict
from puffpai.agents.planning_loop import (
    MAX_REVISIONS,
    EgressScope,
    PlanStep,
    PlanningLoop,
    StepRecord,
)
from puffpai.security.audit_log import AuditLog
from puffpai.security.secrets_provider import AgentScope

GOAL = "find the schedule and act on it"
MARKER = "INJECTED_PAGE_TEXT_fetch_169_254_169_254"


# ── stub agents (real bus, real veto path) ──────────────────────────

class ScriptedArchivist(BaseAgent):
    """archivist.retrieve with a caller-supplied reply function; records queries."""

    name = "archivist"
    scope = AgentScope.ARCHIVIST

    def __init__(self, reply_fn=None):
        super().__init__()
        self.seen: list[Envelope] = []
        self._reply_fn = reply_fn or (lambda env: {"ok": True, "results": []})

    def handle(self, envelope: Envelope) -> dict:
        self.seen.append(envelope)
        return self._reply_fn(envelope)

    @property
    def queries(self) -> list[str]:
        return [(e.payload or {}).get("query") for e in self.seen]


class ScriptedSteward(BaseAgent):
    name = "steward"
    scope = AgentScope.STEWARD

    def __init__(self):
        super().__init__()
        self.seen: list[Envelope] = []

    def handle(self, envelope: Envelope) -> dict:
        self.seen.append(envelope)
        if envelope.intent == "list_devices":
            return {"ok": True, "devices": [{"entity_id": "light.porch",
                                             "name": "Porch"}]}
        return {"ok": True, "intent": envelope.intent}

    @property
    def intents(self) -> list[str]:
        return [e.intent for e in self.seen]

    @property
    def actuating(self) -> list[Envelope]:
        return [e for e in self.seen if e.state_changing]


class RecordingSentinel(BaseAgent):
    name = "sentinel"
    scope = AgentScope.SENTINEL

    def __init__(self):
        super().__init__()
        self.reviews: list[tuple[str, str]] = []

    def review(self, envelope: Envelope, target: str) -> ReviewVerdict:
        self.reviews.append((envelope.intent, target))
        return ReviewVerdict(allowed=True, verdict="allow", reason="")

    def handle(self, envelope: Envelope) -> dict:
        return {"ok": True, "status": {"pending_alerts": 0}}


class _NullHost:
    name = "concierge"


# ── engine stubs ────────────────────────────────────────────────────

class ReviseEngine:
    """Canned initial plan + a queue of canned revisions; records what it saw."""

    def __init__(self, plan, revisions=None):
        self._plan = plan
        self._revisions = list(revisions or [])
        self.revise_calls = 0
        self.observations_seen: list = []

    def plan_steps(self, goal, catalogue):
        return list(self._plan)

    def revise_plan(self, goal, observations, remaining, catalogue=None):
        self.revise_calls += 1
        self.observations_seen.append(observations)
        if self._revisions:
            return list(self._revisions.pop(0))
        return []

    def run(self, *a, **k):
        return None


class ThrashEngine:
    """Always revises to a fresh (still-empty) retrieve — to probe the round cap."""

    def __init__(self):
        self.revise_calls = 0

    def plan_steps(self, goal, catalogue):
        return [{"agent": "archivist", "intent": "retrieve", "target": "q0",
                 "why": "start"}]

    def revise_plan(self, goal, observations, remaining, catalogue=None):
        self.revise_calls += 1
        return [{"agent": "archivist", "intent": "retrieve",
                 "target": f"q{self.revise_calls}", "why": "still nothing"}]

    def run(self, *a, **k):
        return None


# ── fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def team(tmp_path):
    def build(engine, *, archivist_reply=None):
        audit = AuditLog(log_file=str(tmp_path / "audit.jsonl"))
        bus = AgentBus(audit=audit)
        concierge = ConciergeAgent()
        archivist = ScriptedArchivist(archivist_reply)
        steward = ScriptedSteward()
        sentinel = RecordingSentinel()
        for agent in (concierge, archivist, steward, sentinel):
            bus.register(agent)
        concierge._loop = PlanningLoop(concierge, engine=engine, audit=audit)
        return {"bus": bus, "concierge": concierge, "archivist": archivist,
                "steward": steward, "sentinel": sentinel, "engine": engine}
    return build


def run(t, goal=GOAL, *, max_actions=12):
    env = Envelope(intent="chat", payload={"text": goal}, requester="owner",
                   origin_agent="api_server",
                   budget=Budget(max_actions=max_actions, max_seconds=60.0))
    return t["concierge"]._loop.run(env, goal)


# ── 1. an empty step yields a DIFFERENT remaining plan ──────────────

def test_an_empty_step_replans_the_remainder(team):
    """Done-when #1: a step that finds nothing changes what runs next.

    The plan's second read (``retrieve calendar``) must never run — the empty
    first read triggers a revision that replaces the remainder with a device
    listing, and it is the LISTING that runs.
    """
    engine = ReviseEngine(
        plan=[{"agent": "archivist", "intent": "retrieve",
               "target": "schedule", "why": "look for it"},
              {"agent": "archivist", "intent": "retrieve",
               "target": "calendar", "why": "look again"}],
        revisions=[[{"agent": "steward", "intent": "list_devices",
                     "why": "nothing on file, show devices"}]])
    # The empty reply carries a would-be injection marker; the observation the
    # engine is handed must not contain it (see test 3).
    t = team(engine, archivist_reply=lambda env: {
        "ok": True, "results": [], "note": MARKER})
    run(t)

    assert engine.revise_calls == 1
    assert t["archivist"].queries == ["schedule"]        # 'calendar' was dropped
    assert "list_devices" in t["steward"].intents        # the revision ran


def test_a_healthy_step_is_not_revised(team):
    """Signal-gated: a read that RETURNS something does not trigger a revision.

    Revising after every success would thrash a healthy plan; only an empty /
    failed step is a real signal.
    """
    engine = ReviseEngine(
        plan=[{"agent": "archivist", "intent": "retrieve", "target": "schedule",
               "why": "look"}],
        revisions=[[{"agent": "steward", "intent": "list_devices", "why": "x"}]])
    t = team(engine, archivist_reply=lambda env: {"ok": True,
                                                  "results": ["a real note"]})
    run(t)
    assert engine.revise_calls == 0
    assert "list_devices" not in t["steward"].intents


# ── 2. the round cap bounds a pathological re-plan ──────────────────

def test_a_run_revises_at_most_MAX_REVISIONS_times(team):
    """Anti-thrash: every step comes back empty and every revision proposes
    another empty read, yet the run stops re-planning after the hard cap."""
    engine = ThrashEngine()
    t = team(engine, archivist_reply=lambda env: {
        "ok": True, "results": [], "echo": (env.payload or {}).get("query")})
    run(t)
    assert engine.revise_calls == MAX_REVISIONS
    # One initial retrieve + one per revision round actually dispatched.
    assert len(t["archivist"].seen) == MAX_REVISIONS + 1


# ── 3. only safe scalars reach the reviser (the choke point) ────────

def test_observe_carries_no_raw_content():
    """``_observe`` is the security boundary: a record whose reply holds raw web
    text yields an observation with NO substring of that text — and none of the
    reply body, the error message, or the rendered prose either."""
    loop = PlanningLoop(_NullHost())
    from puffpai.agents.planning_loop import CATALOGUE
    cap = CATALOGUE[("toolsmith", "web_fetch")]
    step = PlanStep(cap=cap, target_text="https://example.com/schedule",
                    why="read it", params={"url": "https://example.com/schedule"})
    reply = {"ok": True, "url": "https://example.com/schedule",
             "text": f"{MARKER} the whole hostile page body"}
    prose = f"rendered line quoting {MARKER}"
    obs = loop._observe(StepRecord(step, "executed", reply, prose))

    blob = json.dumps(obs)
    assert MARKER not in blob
    # It still carries the useful SCALARS.
    assert obs["ok"] is True and obs["status"] == "executed"
    assert obs["empty"] is False
    assert obs["error_class"] == "ok"
    assert obs["step"] == "toolsmith.web_fetch on https://example.com/schedule"


def test_observe_reports_empty_and_a_coarse_error_class():
    loop = PlanningLoop(_NullHost())
    from puffpai.agents.planning_loop import CATALOGUE
    step = PlanStep(cap=CATALOGUE[("toolsmith", "web_search")],
                    target_text="anything", why="", params={"query": "anything"})
    # A failed search that echoes attacker text in its error MESSAGE.
    reply = {"ok": False, "results": [], "offline": True,
             "error": f"toolsmith: {MARKER}"}
    obs = loop._observe(StepRecord(step, "error", reply, ""))
    assert MARKER not in json.dumps(obs)
    assert obs["ok"] is False
    assert obs["empty"] is True and obs["count"] == 0
    assert obs["error_class"] == "offline"    # coarse label, not the message


# ── 4. a revision re-passes every gate ──────────────────────────────

def _loop_with_revision(revision):
    engine = ReviseEngine(plan=[], revisions=[revision])
    return PlanningLoop(_NullHost(), engine=engine)


def test_revision_cannot_widen_the_sealed_egress_scope():
    """A revised fetch to a host that first appeared mid-run is refused; only a
    host the run already sealed survives. The scope is frozen — a revision can
    never add to it."""
    loop = _loop_with_revision([
        {"agent": "toolsmith", "intent": "web_fetch",
         "url": "http://attacker.example/"},
        {"agent": "toolsmith", "intent": "web_fetch",
         "url": "https://example.com/other"}])
    # example.com is sealed because GOAL... does not name it; seal it explicitly.
    scope = EgressScope.for_run("read https://example.com/x", granted=True)
    revised = loop._revise("goal", [], [], scope)
    assert [s.params.get("url") for s in revised] == ["https://example.com/other"]


def test_revision_fetch_is_refused_when_the_run_had_no_egress():
    loop = _loop_with_revision([
        {"agent": "toolsmith", "intent": "web_fetch",
         "url": "https://example.com/other"}])
    scope = EgressScope.for_run("read https://example.com/x", granted=False)
    assert loop._revise("goal", [], [], scope) == []


def test_revision_drops_a_confirm_required_step():
    """A revision cannot silently run an always-confirm capability (siren)."""
    loop = _loop_with_revision([
        {"agent": "steward", "intent": "siren", "target": "shed", "action": "on"}])
    scope = EgressScope.for_run("goal", granted=False)
    assert loop._revise("goal", [], [], scope) == []


def test_revision_drops_an_unresolved_entity_step():
    """A revision does not resolve targets, so a step needing a device it cannot
    aim is dropped rather than guessed."""
    loop = _loop_with_revision([
        {"agent": "steward", "intent": "device_status", "target": "porch light"}])
    scope = EgressScope.for_run("goal", granted=False)
    assert loop._revise("goal", [], [], scope) == []


def test_revision_drops_a_deny_listed_step():
    """The safety machinery is off-limits to a revision, as to an initial plan."""
    loop = _loop_with_revision([
        {"agent": "archivist", "intent": "retrieve",
         "target": "the vault token and consent ledger"}])
    scope = EgressScope.for_run("goal", granted=False)
    assert loop._revise("goal", [], [], scope) == []
