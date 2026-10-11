"""End-to-end loop test: a PLANNED online search composes into a workflow.

This is the primary goal of the search-composition pass. ``web_search`` alone
cannot be composed — a URL it discovers is not in the run's sealed
:class:`EgressScope`, so a following ``web_fetch`` is refused. ``search_and_read``
fuses the search and the read into ONE step, so the plan

    search_and_read → workspace_write

runs end-to-end: the step reaches the internet on the run's INHERITED egress
(``scope.granted``, NOT a pre-sealed host), and the page it reads is copied into
the note by the loop's deterministic capture — the model never in the byte path.

The bus underneath is real: every dispatch goes through
``concierge._send_sub`` → ``bus.send`` → Sentinel. Only the Toolsmith is a stub,
and it mirrors the real agent's egress gate so "the step ran" really means "the
budget carried the grant to the wire". No HA, no sockets, no LLM.

Run: pytest tests/test_planning_loop_search_and_read.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents import preferences as prefs
from puffpai.agents.base import BaseAgent
from puffpai.agents.bus import AgentBus
from puffpai.agents.concierge import ConciergeAgent
from puffpai.agents.envelope import Budget, Envelope, ReviewVerdict
from puffpai.agents.planning_loop import CAPTURE_HEADER, PlanningLoop
from puffpai.security.audit_log import AuditLog
from puffpai.security.secrets_provider import AgentScope

PAGE = "Bin day is Thursday. Recycling is Tuesday."
RESULT_URL = "https://news.example/story"
#: A goal that names NO host on purpose: search_and_read must reach the internet
#: on the inherited grant alone, since the host it reads is DISCOVERED, not named.
GOAL = "look up when the bins go out and keep a copy"


class StubToolsmith(BaseAgent):
    """Mirrors the real Toolsmith's egress gate for search_and_read + write."""

    name = "toolsmith"
    scope = AgentScope.TOOLSMITH

    def __init__(self, page: str = PAGE):
        super().__init__()
        self.seen: list[Envelope] = []
        self.written: list[dict] = []
        self.page = page

    def handle(self, envelope: Envelope) -> dict:
        self.seen.append(envelope)
        payload = envelope.payload or {}
        if envelope.intent == "search_and_read":
            if not getattr(envelope.budget, "allow_egress", False):
                return {"ok": False, "egress_denied": True,
                        "error": "toolsmith: search_and_read needs allow_egress"}
            return {"ok": True, "url": RESULT_URL, "status": 200,
                    "text": self.page, "truncated": False, "untrusted": True,
                    "query": payload.get("query"), "title": "Bin schedule"}
        if envelope.intent == "workspace_write":
            self.written.append(dict(payload))
            return {"ok": True, "path": payload.get("path"),
                    "bytes": len(str(payload.get("content") or ""))}
        return {"ok": True, "intent": envelope.intent}

    @property
    def searched(self) -> list[Envelope]:
        return [e for e in self.seen if e.intent == "search_and_read"]


class AllowingSentinel(BaseAgent):
    name = "sentinel"
    scope = AgentScope.SENTINEL

    def __init__(self):
        super().__init__()
        self.reviews: list[tuple[str, str]] = []

    def review(self, envelope: Envelope, target: str) -> ReviewVerdict:
        self.reviews.append((target, envelope.intent))
        return ReviewVerdict(allowed=True, verdict="allow", reason="")

    def handle(self, envelope: Envelope) -> dict:
        return {"ok": True, "status": {"pending_alerts": 0}}


class PlanEngine:
    def __init__(self, steps):
        self.steps = steps

    def plan_steps(self, goal, catalogue):
        return list(self.steps)

    def run(self, *a, **kw):
        return None


@pytest.fixture(autouse=True)
def no_inherited_config(monkeypatch):
    monkeypatch.setattr(prefs, "_config_path", lambda: None)
    prefs.reset_cache()
    yield
    prefs.reset_cache()


@pytest.fixture
def team(tmp_path):
    def build(steps, *, page=PAGE):
        audit = AuditLog(log_file=str(tmp_path / "audit.jsonl"))
        bus = AgentBus(audit=audit)
        concierge = ConciergeAgent()
        toolsmith = StubToolsmith(page)
        sentinel = AllowingSentinel()
        for agent in (concierge, toolsmith, sentinel):
            bus.register(agent)
        concierge._loop = PlanningLoop(concierge, engine=PlanEngine(steps),
                                       audit=audit)
        return {"bus": bus, "concierge": concierge, "toolsmith": toolsmith,
                "sentinel": sentinel}

    return build


def plan(t, *, egress=False, max_actions=12):
    env = Envelope(intent="chat", payload={"text": GOAL}, requester="owner",
                   origin_agent="api_server",
                   budget=Budget(max_actions=max_actions, max_seconds=60.0,
                                 allow_egress=egress))
    return t["concierge"]._loop.run(env, GOAL)


SEARCH = {"agent": "toolsmith", "intent": "search_and_read",
          "query": "bin collection day", "why": "find the schedule"}


def write_step(**extra):
    step = {"agent": "toolsmith", "intent": "workspace_write",
            "path": "notes/bins.md", "why": "keep a copy"}
    step.update(extra)
    return step


# ── search_and_read reaches the internet on the INHERITED grant ─────

def test_search_and_read_runs_on_a_granted_run_with_no_named_host(team):
    """The whole point: no host in the goal, yet the step reaches the internet
    because the run inherited egress. web_fetch could not do this."""
    t = team([SEARCH])
    run = plan(t, egress=True)

    assert run["stop_reason"] == "goal_satisfied"
    assert len(t["toolsmith"].searched) == 1
    assert t["toolsmith"].searched[0].budget.allow_egress is True


def test_search_and_read_is_refused_on_an_ungranted_run(team):
    """No inherited egress → the step still dispatches but with allow_egress
    False, and the stub (like the real agent) fails closed."""
    t = team([SEARCH])
    run = plan(t, egress=False)

    assert t["toolsmith"].searched[0].budget.allow_egress is False
    assert run["stop_reason"] != "goal_satisfied"


# ── composition: the fetched page is copied into the note ───────────

def test_search_and_read_composes_into_a_workspace_write(team):
    """search_and_read → workspace_write: the page text is written by the loop's
    deterministic capture, with the planner never reading the bytes."""
    t = team([SEARCH, write_step()])
    run = plan(t, egress=True)

    assert run["executed"] == 2
    assert len(t["toolsmith"].written) == 1
    content = t["toolsmith"].written[0]["content"]
    # The page body made it into the note, under the provenance header keyed to
    # the DISCOVERED result url.
    assert PAGE in content
    assert CAPTURE_HEADER.format(url=RESULT_URL) in content
    # Least privilege per step: the write carried no network reach.
    by_intent = {e.intent: e for e in t["toolsmith"].seen}
    assert by_intent["workspace_write"].budget.allow_egress is False


def test_a_planner_supplied_note_body_is_not_overwritten(team):
    """Composition fills a GAP: if the plan authored its own content, the loop
    leaves it alone."""
    t = team([SEARCH, write_step(content="my own summary")])
    plan(t, egress=True)

    assert t["toolsmith"].written[0]["content"] == "my own summary"
