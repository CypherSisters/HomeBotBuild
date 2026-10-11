"""The capability contract — one declaration, many consumers.

Adding a Steward capability means touching several places that each keep
their own copy of "what exists" and "what is dangerous":

  * ``planning_loop.CATALOGUE`` — what the planner may propose
  * ``steward.STATE_CHANGING_INTENTS`` — what the Steward refuses on a
    read-only envelope
  * ``concierge.RESOLVABLE_INTENTS`` — what the router may resolve to
  * the engine's decoder enums (already derived from the catalogue)

Parallel lists are exactly the failure class the multi-agent failure research
puts first: not a bad model, a mis-specified contract between components. These
tests are the contract. They do not stop anyone adding a capability — they stop
someone adding it in one place and silently not the others.

The long-term fix is a single declaration these views are generated FROM. Until
that lands, this file makes drift a failing test rather than a mystery.

Run: pytest tests/test_capability_contract.py -q
"""

from __future__ import annotations

from puffpai.agents.concierge import RESOLVABLE_INTENTS
from puffpai.agents.planning_loop import CATALOGUE
from puffpai.agents.steward import STATE_CHANGING_INTENTS

#: Steward intents deliberately NOT offered to the planner, with the reason.
#: Every entry is a capability the Steward can perform but the reasoning layer
#: may not reach — so each one needs a justification, not just an omission.
UNDECLARED_ON_PURPOSE = {
    # Playback needs a library item id that no target resolver produces from
    # natural language, so a planned step could never be filled in. Reachable
    # through the chat path, which has the media tools to pick an item.
    "media_play": "needs an item id the planner cannot resolve",
    # Legacy alias kept for older callers; ``control_device`` is the declared
    # spelling and the one the catalogue exposes.
    "control": "legacy alias of control_device",
    # Opening the Zigbee pairing window admits ANY device within radio range,
    # so it is human-initiated by construction: the intent only FILES a consent
    # request (puffpai.zigbee_pairing.request_pairing), and the window is opened
    # solely by complete_approved_pairing, from an approval surface, against a
    # human-resolved record. Declaring it would let the planner compose "…and
    # put the house in pairing mode" into a chain the owner never asked for.
    # Kept out of RESOLVABLE_INTENTS for the same reason, which is why the
    # router/catalogue balance above is undisturbed.
    "zigbee_pair": "a pairing window is human-initiated; the planner may never "
                   "propose opening the radio to strangers",
}


def _steward_catalogue_intents() -> set[str]:
    return {intent for (agent, intent) in CATALOGUE if agent == "steward"}


def _catalogue_state_changing() -> set[str]:
    return {intent for (agent, intent), cap in CATALOGUE.items()
            if agent == "steward" and cap.state_changing}


def test_no_planner_capability_escapes_the_stewards_guard():
    """The dangerous direction. A catalogue entry marked state-changing that the
    Steward does not guard would be actuation the planner can reach on a
    read-only envelope."""
    unguarded = _catalogue_state_changing() - set(STATE_CHANGING_INTENTS)
    assert not unguarded, (
        f"planner can propose state-changing {sorted(unguarded)} that "
        "steward.STATE_CHANGING_INTENTS does not guard")


def test_every_guarded_intent_is_declared_or_documented():
    """The capability-gap direction. A guarded intent missing from the
    catalogue is a hand the Steward has and the reasoning layer cannot use —
    fine, but only on purpose and in writing."""
    undeclared = set(STATE_CHANGING_INTENTS) - _catalogue_state_changing()
    assert undeclared <= set(UNDECLARED_ON_PURPOSE), (
        f"steward guards {sorted(undeclared - set(UNDECLARED_ON_PURPOSE))} "
        "which the planner cannot see; declare it in the catalogue or add it "
        "to UNDECLARED_ON_PURPOSE with a reason")


def test_documented_exceptions_are_real_and_still_needed():
    """Stops the exception list rotting into a lie once a gap is closed."""
    stale = {i for i in UNDECLARED_ON_PURPOSE
             if i not in STATE_CHANGING_INTENTS}
    assert not stale, (
        f"UNDECLARED_ON_PURPOSE lists {sorted(stale)}, which the Steward no "
        "longer guards — remove the entry")
    now_declared = set(UNDECLARED_ON_PURPOSE) & _catalogue_state_changing()
    assert not now_declared, (
        f"{sorted(now_declared)} is in the catalogue now — drop the exception")


def test_router_and_catalogue_agree_on_what_exists():
    """The router may only resolve to capabilities that exist, and every
    Steward capability the planner knows should be reachable by the router —
    otherwise a request works when planned and fails when spoken."""
    assert RESOLVABLE_INTENTS - _steward_catalogue_intents() == set(), (
        "router can resolve to intents the catalogue does not declare")
    assert _steward_catalogue_intents() - RESOLVABLE_INTENTS == set(), (
        "catalogue declares Steward intents the router can never reach")


def test_the_confirm_class_is_not_a_second_copy():
    """The always-confirm decision must come from the Steward's authority, not
    a list kept next to the catalogue — a local copy of 'what is dangerous' is
    what previously let control_device on a siren entity past the gate."""
    from puffpai.agents import steward as steward_mod
    assert hasattr(steward_mod, "requires_human_confirmation")
    confirm_caps = {intent for (agent, intent), cap in CATALOGUE.items()
                    if cap.confirm_required}
    # The flag may only ADD caution, so anything flagged must also be guarded.
    assert confirm_caps <= set(STATE_CHANGING_INTENTS), (
        f"{sorted(confirm_caps - set(STATE_CHANGING_INTENTS))} is confirm-"
        "required but not state-changing, which cannot be enforced")
