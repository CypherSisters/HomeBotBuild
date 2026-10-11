"""``web_search`` inside the planning loop — egress is granted correctly.

Two shapes of egress, and the loop must tell them apart:

* ``web_fetch`` carries egress only when its URL's host is in the run's SEALED
  scope (and the run inherited egress). A host the plan discovered mid-run is
  not in the seal, so its fetch gets no egress.
* ``web_search`` carries egress when the run merely INHERITED it, with no host
  to match — because it reaches the owner's fixed SearXNG through the Toolsmith
  pinhole, not a plan-chosen URL. It must still get nothing on a run the caller
  never granted egress.

Run: pytest tests/test_planning_loop_web_search.py -q
"""

from __future__ import annotations

from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.planning_loop import (
    CATALOGUE,
    WEB_FETCH_KEY,
    WEB_SEARCH_KEY,
    EgressScope,
    PlanStep,
    PlanningLoop,
    catalogue_lines,
)


class FakeHost:
    """Captures the sub-budget each dispatched step was handed."""

    name = "concierge"

    def __init__(self):
        self.sent = []

    def _send_sub_metered(self, envelope, agent, intent, payload, *,
                          state_changing, budget):
        self.sent.append({
            "agent": agent, "intent": intent,
            "allow_egress": budget.allow_egress,
        })
        return ({"ok": True}, 1)


def _dispatch(cap_key, *, granted, hosts=frozenset(), params=None):
    host = FakeHost()
    loop = PlanningLoop(host, engine=None)
    step = PlanStep(cap=CATALOGUE[cap_key], params=params or {})
    scope = EgressScope(granted=granted, hosts=frozenset(hosts))
    budget = Budget(max_actions=5, max_seconds=30.0, allow_egress=granted)
    budget.start()
    env = Envelope(intent="chat", payload={}, budget=budget,
                   origin_agent="user")
    loop._dispatch(env, "goal", step, budget, scope)
    return host.sent[0]["allow_egress"]


# ── the capability exists and is advertised ─────────────────────────

def test_web_search_is_in_the_catalogue():
    assert WEB_SEARCH_KEY in CATALOGUE
    cap = CATALOGUE[WEB_SEARCH_KEY]
    assert cap.params == ("query", "max_results")
    assert cap.state_changing is False
    # The planner prompt shows it, so the model can choose it.
    assert any("toolsmith.web_search" in line for line in catalogue_lines())


# ── egress plumbing ─────────────────────────────────────────────────

def test_web_search_gets_egress_when_run_inherited_it():
    # No host in the sealed set — web_search needs none.
    assert _dispatch(WEB_SEARCH_KEY, granted=True,
                     params={"query": "cats"}) is True


def test_web_search_gets_no_egress_when_run_was_not_granted():
    assert _dispatch(WEB_SEARCH_KEY, granted=False,
                     params={"query": "cats"}) is False


def test_web_fetch_still_needs_the_host_in_the_seal():
    # Same granted run, but a fetch to a host that was NOT sealed gets no
    # egress — proving web_search's grant did not loosen web_fetch's.
    assert _dispatch(WEB_FETCH_KEY, granted=True, hosts=frozenset(),
                     params={"url": "https://example.com/x"}) is False
    # And a fetch to a sealed host does get egress.
    assert _dispatch(WEB_FETCH_KEY, granted=True,
                     hosts=frozenset({"example.com"}),
                     params={"url": "https://example.com/x"}) is True
