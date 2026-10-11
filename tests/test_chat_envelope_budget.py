"""The chat envelope has to be big enough for the run it is the parent of.

Turning the Phase 4 loop on live exposed a seam between two files that are both
individually correct. ``api_server`` sized the chat envelope for single-step
delegation (6 actions / 20s), and ``PlanningLoop._derive_budget`` takes the
*minimum* of its own limits and its parent's — invariant 2, a run may never mint
or extend its own budget. The loop was right to refuse, so the parent budget
became the loop's real ceiling: a genuine two-step request spent its wall clock
on the planner call and degraded to "I'm not able to ... right now."

These tests pin the seam from the caller's side, and deliberately assert the
*relationship* rather than the literals — 12 and 120.0 may be retuned, but the
chat envelope must never again be what stops the loop.

No HA, no sockets, no LLM.
Run: pytest tests/test_chat_envelope_budget.py -q
"""

from __future__ import annotations

import pytest

from puffpai.agents.envelope import Budget, BudgetExceeded, Envelope
from puffpai.agents.planning_loop import (
    DEFAULT_MAX_ACTIONS,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_STEPS,
    PLANNING_LOOP_ENV,
    PlanningLoop,
)


@pytest.fixture(scope="module")
def api_server():
    """The real module — the point is to read the budget production uses."""
    import puffpai.api_server as mod
    return mod


def _chat_envelope(budget: Budget) -> Envelope:
    """A chat envelope shaped exactly like ``_route_via_concierge`` builds one."""
    return Envelope(
        intent="chat",
        payload={"text": "check the living room temperature and list my devices"},
        requester="owner",
        origin_agent="api_server",
        budget=budget,
        state_changing=False,
    )


def _derive(budget: Budget) -> Budget:
    """What the loop would actually get to spend under *budget*.

    ``_derive_budget`` reads only ``self._max_*`` and the envelope, so a bare
    host object is honest here — the same construction ``test_planning_loop``
    uses for the host-free cases.
    """
    return PlanningLoop(object(), engine=None)._derive_budget(_chat_envelope(budget))


# ── the loop is on ──────────────────────────────────────────────────

def test_chat_budget_does_not_clamp_the_planning_loop(api_server, monkeypatch):
    """With the loop on, the parent must not be the binding constraint."""
    monkeypatch.setenv(PLANNING_LOOP_ENV, "1")

    derived = _derive(api_server._chat_envelope_budget())

    # Equality, not ">=": if the parent clamped by even one action or second,
    # the loop is quietly running a smaller plan than it was configured for.
    assert derived.max_actions == DEFAULT_MAX_ACTIONS
    assert derived.max_seconds == DEFAULT_MAX_SECONDS


def test_chat_budget_funds_every_step_the_loop_may_plan(api_server, monkeypatch):
    """A full-length plan must be affordable, charged the way the loop charges.

    Spends the derived budget in the loop's real pattern — one metered
    device-list read for target resolution, then one action per planned step —
    rather than asserting an inequality that could drift from the code.
    """
    monkeypatch.setenv(PLANNING_LOOP_ENV, "1")

    derived = _derive(api_server._chat_envelope_budget())

    derived.consume(1)  # _resolve_targets: one device list for the whole run
    for step in range(DEFAULT_MAX_STEPS):
        try:
            derived.consume(1)
        except BudgetExceeded as exc:  # pragma: no cover - the bug being fixed
            pytest.fail(f"step {step + 1} of {DEFAULT_MAX_STEPS} unaffordable: {exc}")

    assert derived.actions_remaining >= 0


def test_the_old_single_step_budget_would_have_clamped_the_loop():
    """The assertions above are not vacuous: the shipped budget really did clamp.

    This pins a real defect: ``_derive_budget`` is the
    minimum of the two, so a 6-action / 20-second parent handed the loop a
    6-action / 20-second run — under half its wall clock, with the planner call
    still to pay for.
    """
    derived = _derive(Budget(max_actions=6, max_seconds=20.0, allow_egress=False))

    assert derived.max_actions < DEFAULT_MAX_ACTIONS
    assert derived.max_seconds < DEFAULT_MAX_SECONDS


# ── the loop is off ─────────────────────────────────────────────────

def test_single_step_budget_is_unchanged_when_the_loop_is_off(api_server,
                                                              monkeypatch):
    """Chat with the loop off is one delegated action plus one lookup, as before.

    Set explicitly to "0" rather than deleted: ``planning_loop_enabled`` falls
    through to ``puffpai_config.json`` when the variable is absent, and this
    test must not depend on how the machine running it is configured.
    """
    monkeypatch.setenv(PLANNING_LOOP_ENV, "0")

    budget = api_server._chat_envelope_budget()

    assert budget.max_actions == 6
    assert budget.max_seconds == 20.0


# ── true either way ─────────────────────────────────────────────────

@pytest.mark.parametrize("enabled", ["1", "0"])
def test_chat_never_buys_egress(api_server, monkeypatch, enabled):
    """Nothing in the catalogue leaves the appliance; neither size may allow it."""
    monkeypatch.setenv(PLANNING_LOOP_ENV, enabled)

    assert api_server._chat_envelope_budget().allow_egress is False


def test_each_call_returns_a_fresh_budget(api_server, monkeypatch):
    """A Budget is mutable and carries a clock; sharing one across chat turns
    would let an early request spend a later one's ceiling."""
    monkeypatch.setenv(PLANNING_LOOP_ENV, "1")

    first = api_server._chat_envelope_budget()
    first.consume(1)

    assert api_server._chat_envelope_budget().actions_used == 0
