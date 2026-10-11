"""Contract tests binding the planner's plannable catalogue to the registry.

The planner composes multi-step plans from a HAND-AUTHORED table
(:data:`planning_loop._PLAN_SPECS` → :data:`planning_loop.CATALOGUE`), on
purpose: this is the auditable file, and a safety flag must never migrate into
the churn-prone capability registry. But two sources of truth drift, and the
registry never got the test that would have caught it. This file is that test.

It pins four properties (the design's T1-T4), plus the two operational facts the
pass this file ships with depends on — the loop default and the engine enum:

* **T1 safety** — the load-bearing one. A spec's ``state_changing`` must equal
  what the owning AGENT MODULE says (``<mod>.STATE_CHANGING_INTENTS``), and a
  steward spec's ``confirm_required`` must equal ``CONFIRM_REQUIRED_INTENTS``.
  So a plannable state-changer that the agent does not treat as one — the exact
  shape of a veto-skipping bug — fails here.
* **T2 ownership** — every spec that names a ``cap_id`` maps to a registry
  capability that exists and whose ``agent`` agrees. This is what makes the
  registry the single source for OWNERSHIP without moving safety policy into it.
* **T3 no-silent-drop** — the built catalogue's pairs equal the declared specs'
  pairs. A registry or ``_AGENT_FILL`` change can never silently prune a
  plannable pair, because the catalogue is the hand spec verbatim.
* **T4 exclusions stay excluded** — the named dangerous intents are absent, and
  plannable state-changers live ONLY on steward + toolsmith.

No network, no real app_data: the one engine assertion injects its transport.

Run: pytest tests/test_planner_registry_contract.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai import capability_registry as reg
from puffpai.agents import planning_loop as pl
from puffpai.agents import (
    archivist,
    caretaker,
    majordomo,
    sentinel_agent,
    steward,
    toolsmith,
)
from puffpai.agents.engine import OllamaEngine, _catalogue_pairs

# Roster id → the agent module that OWNS its handle() intents. ``human`` (the
# ASK_HUMAN pseudo-capability) has no module and no state-changing set.
_AGENT_MODULE = {
    "steward": steward,
    "sentinel": sentinel_agent,
    "archivist": archivist,
    "majordomo": majordomo,
    "caretaker": caretaker,
    "toolsmith": toolsmith,
    "human": None,
}

#: Intents that must NEVER be plannable — safety machinery, unreviewed writes,
#: self-perpetuating schedulers, service control, and intents needing an id no
#: resolver produces. (Design's T4 list.)
EXCLUDED_INTENTS = frozenset({
    "media_play", "zigbee_pair", "zigbee_pair_start", "camera_frame",
    "block_device", "unblock_device", "review", "remember",
    "run_workflow", "daily_briefing", "enqueue", "delete_task",
    "restart_service", "verify_update_pack",
})


def _state_changing_set(agent: str) -> frozenset:
    mod = _AGENT_MODULE.get(agent)
    return getattr(mod, "STATE_CHANGING_INTENTS", frozenset()) if mod else frozenset()


# ── T1: safety flags match the owning agent, not just each other ────

def test_state_changing_flag_matches_the_owning_agent():
    """The veto only fires on intents the AGENT calls state-changing. A spec that
    disagrees would either skip Sentinel or block a harmless read."""
    for cap in pl._PLAN_SPECS:
        if cap is pl.ASK_HUMAN:
            assert cap.state_changing is False
            continue
        expected = cap.intent in _state_changing_set(cap.agent)
        assert cap.state_changing == expected, (
            f"{cap.agent}.{cap.intent}: spec state_changing={cap.state_changing} "
            f"but agent module says {expected}")


def test_confirm_required_matches_the_steward_authority():
    """Only steward has always-confirm intents; every other spec is confirm=False."""
    for cap in pl._PLAN_SPECS:
        if cap.agent == "steward":
            assert cap.confirm_required == (
                cap.intent in steward.CONFIRM_REQUIRED_INTENTS), cap.key
        else:
            assert cap.confirm_required is False, cap.key


def test_plannable_state_changers_are_only_steward_and_toolsmith():
    """The denial logic keys on entities the Steward resolves; a state-changer on
    any other agent would be a route around it. The Toolsmith's state-changers
    are admissible only because they name no entity and cannot actuate the
    house: ``workspace_write`` puts text in a directory, and ``workspace_exec``
    and ``document_convert`` run in a bubblewrap jail with no network namespace
    and no path outside that same directory (see toolsmith._jail_argv). All
    three still ride a state-changing envelope, so Drawbridge reviews them.

    This set is pinned deliberately. Adding a third Toolsmith state-changer must
    be a conscious act with the same question answered: can it name a house
    entity, and can it reach anything the workspace does not contain?"""
    changer_agents = {c.agent for c in pl.CATALOGUE.values() if c.state_changing}
    assert changer_agents == {"steward", "toolsmith"}
    toolsmith_changers = {c.intent for c in pl.CATALOGUE.values()
                          if c.state_changing and c.agent == "toolsmith"}
    assert toolsmith_changers == {"workspace_write", "workspace_exec",
                                  "document_convert"}
    # search_and_read reaches the internet but mutates nothing — it must NOT be a
    # state-changer, or it would demand a consent gate it has no entity for.
    assert pl.CATALOGUE[("toolsmith", "search_and_read")].state_changing is False


# ── T2: the registry is the single source for OWNERSHIP ─────────────

def test_capped_specs_agree_with_the_registry_on_ownership():
    for cap in pl._PLAN_SPECS:
        if not cap.cap_id:
            continue
        reg_cap = reg.get(cap.cap_id)
        assert reg_cap is not None, (
            f"{cap.agent}.{cap.intent} names cap_id {cap.cap_id!r} the registry "
            "does not define")
        assert reg_cap.agent == cap.agent, (
            f"{cap.agent}.{cap.intent}: registry cap {cap.cap_id!r} is owned by "
            f"{reg_cap.agent!r}, not {cap.agent!r}")


def test_specs_without_a_cap_are_the_known_uncapped_intents():
    """Only the intents that genuinely have no registry capability may omit a
    cap_id — a guard against a real mapping being forgotten."""
    uncapped = {c.key for c in pl._PLAN_SPECS if not c.cap_id}
    assert uncapped == {
        ("steward", "notify_user"),
        ("steward", "announce"),
        ("steward", "siren"),
        ("toolsmith", "web_fetch"),
        ("human", "ask"),
    }


# ── T3: the catalogue is the hand spec, verbatim ───────────────────

def test_catalogue_is_exactly_the_declared_specs_no_silent_drop():
    built = set(pl.CATALOGUE)
    declared = {c.key for c in pl._PLAN_SPECS}
    assert built == declared
    # And a rebuild is stable and identical (the registry read never prunes).
    assert set(pl.catalogue_from_registry()) == declared


def test_catalogue_survives_a_missing_registry(monkeypatch):
    """If the registry import fails, the planner must still get its full
    catalogue — ownership is a cross-check, never a gate."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "puffpai.capability_registry" or name.endswith(
                "capability_registry"):
            raise ImportError("registry unavailable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    rebuilt = pl.catalogue_from_registry()
    assert set(rebuilt) == {c.key for c in pl._PLAN_SPECS}


# ── T4: exclusions stay excluded ────────────────────────────────────

def test_excluded_intents_are_absent_from_the_catalogue():
    present_intents = {intent for _, intent in pl.CATALOGUE}
    leaked = present_intents & EXCLUDED_INTENTS
    assert not leaked, f"excluded intents leaked into the catalogue: {leaked}"


def test_specific_dangerous_pairs_are_absent():
    for pair in (("sentinel", "unblock_device"), ("sentinel", "block_device"),
                 ("majordomo", "enqueue"), ("majordomo", "delete_task"),
                 ("majordomo", "run_workflow"), ("caretaker", "restart_service"),
                 ("archivist", "remember"), ("steward", "media_play"),
                 ("steward", "zigbee_pair")):
        assert pair not in pl.CATALOGUE, pair


# ── every emitted catalogue line parses to a valid pair ─────────────

def test_every_catalogue_line_parses_to_a_catalogue_pair():
    """The engine builds its decoder enums from catalogue_lines(); every line it
    renders must resolve back to a real CATALOGUE entry (and vice versa)."""
    pairs = set(_catalogue_pairs(pl.catalogue_lines()))
    assert pairs == set(pl.CATALOGUE)


def test_no_capability_parameter_is_a_privilege_dial():
    for cap in pl.CATALOGUE.values():
        assert not (set(cap.params) & pl.DENIED_PAYLOAD_KEYS), cap.key


# ── the widening is real: new intents are plannable end-to-end ──────

def test_newly_added_intents_are_plannable():
    for pair in (("steward", "media_search"), ("steward", "notify_user"),
                 ("archivist", "get_memory"), ("caretaker", "health"),
                 ("toolsmith", "search_and_read")):
        assert pair in pl.CATALOGUE, pair


def test_engine_enum_covers_the_wider_set_and_decodes_a_new_intent():
    """The engine's schema enums derive from catalogue_lines(), so a newly-added
    intent is decodable without any engine change. Prove both: the enum contains
    the new pair, and a plan naming it survives plan_steps."""
    lines = pl.catalogue_lines()

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return {"message": {"content": json.dumps(self._p)}}

        def raise_for_status(self):
            pass

    class _HTTP:
        def __init__(self, payload):
            self._p = payload
            self.calls = []

        def post(self, url, json=None, timeout=None):
            self.calls.append(json)
            return _Resp(self._p)

    plan = {"reasoning": "look it up then note it", "steps": [
        {"agent": "caretaker", "intent": "health", "why": "check the box"},
        {"agent": "toolsmith", "intent": "search_and_read",
         "target": "local news", "why": "read a story"},
    ]}
    http = _HTTP(plan)
    engine = OllamaEngine(url="http://stub:11434", http=http)
    out = engine.plan_steps("how is the appliance and what's the news", lines)

    assert out == plan["steps"]
    schema = http.calls[0]["format"]["properties"]["steps"]["items"]["properties"]
    assert "caretaker" in schema["agent"]["enum"]
    assert "health" in schema["intent"]["enum"]
    assert "search_and_read" in schema["intent"]["enum"]


# ── the shipped loop default flipped ON ─────────────────────────────

def test_loop_default_is_on_when_nothing_overrides(monkeypatch):
    """The shipped default is ON. With no env and no config
    override, the loop is enabled."""
    monkeypatch.delenv(pl.PLANNING_LOOP_ENV, raising=False)
    assert pl.planning_loop_enabled() is True


def test_kill_switch_still_disables_the_loop(monkeypatch):
    """The default being ON must not defeat the kill-switch."""
    monkeypatch.setenv(pl.PLANNING_LOOP_ENV, "0")
    assert pl.planning_loop_enabled() is False
