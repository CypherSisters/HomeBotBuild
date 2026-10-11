"""A call that names *no* target sweeps the whole domain, and nothing asked.

The hole this file closes. ``service_call_targets`` answers "which entities does
this call name?", and ``unbounded_request_detail`` answers "is the set it names
enumerable?". Neither asks the question underneath both of them: what if the
call names nothing at all?

Home Assistant reads a missing target as *the whole domain*. So:

    ALLOWED   light.turn_off          {}   <- every light in the house
    ALLOWED   switch.turn_off         {}   <- every switch
    ALLOWED   media_player.turn_off   {}   <- every speaker and TV
    ALLOWED   climate.set_temperature {"temperature": 90}
    ALLOWED   homeassistant.turn_off  {}   <- every entity there is
    ALLOWED   zha.permit              {"duration": 60}   <- the pairing window

None of these carried a safety-critical domain in the classification (``light``,
``switch``, ``media_player``, ``climate`` and ``homeassistant`` are all outside
:data:`SAFETY_CRITICAL_DOMAINS`), and none named an opaque selector, so the gate
had nothing to refuse on. The blast radius was maximal and the prompt count was
zero — the exact inversion of what a confirmation gate is for.

Untargeted is therefore refused by default, and the exemptions are a short
written-down allow-list rather than a heuristic:

* ``reload`` and its variants, and ``check_config`` — they re-read configuration
  and name no entity by construction;
* ``notify`` and ``persistent_notification`` — they carry a message and move
  nothing. A gate that refuses notifications is a gate people route around;
* ``shopping_list`` — the integration owns one global list and exposes no
  ``entity_id`` parameter, so untargeted is the only way to spell any of its
  services, and the blast radius is a list of words. Section 4a is the one that
  matters in daily use: ``StewardAgent._list_add`` reaches this domain on every
  "add milk to the shopping list", and a confirmation in front of *that* is how
  a household learns to click through every other one. Exempted by *name*, not
  by domain: the eight services HA defines there are named one by one, so a
  ninth one HA adds later cannot inherit the pass unread;
* the per-script service form (``POST services/script/goodnight``), which names
  its target in the *service*; ``_expand_stored_targets`` already reconstructs
  and classifies it, and fails closed when it cannot read the steps;
* **the disarm direction** — ``zha.permit`` with ``duration=0``. Section 6 is
  the part of this file that must never be allowed to go green by accident: it
  is ZHA's *close* of the pairing window, there is no separate close service,
  and a gate that can refuse the close is a gate that can hold a pairing window
  open. Reducing exposure never needs permission.

Section 5 is the other half of the design and matters as much as the refusals:
every call that *does* name a target — by argument, by service data, by
``target:`` block, by ``scene.apply`` entities, or by area — is untouched.
A gate that prompts for "lights on in the garage" is a gate people learn to
dismiss without reading. There is exactly one exception, and it is not this
file's rule: ``entity_id: "all"`` is a named target, but the name it gives is
the whole house, so it now has its own rule with the same blast-radius refusal
(``broadcast_request_detail``; see tests/test_ha_broadcast_gate.py). This file
only pins that the untargeted check itself stands down on it and hands it over.

**Every entity id, area id and ieee address in this file is fabricated, and
nothing here may reach a real Home Assistant.** Every bridge is a real
``HABridge`` subclass whose ``_request`` — the only method that opens a socket —
is replaced by a recorder, so "the gate leaked" and "the test touched a house"
are the same observable event: an entry in ``bridge.wire``.

Run: pytest tests/test_ha_untargeted_gate.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.modpatch import patched_modules

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai.ha_bridge import (  # noqa: E402
    HUMAN_CONFIRMED,
    ConfirmationRequired,
    HABridge,
    HABridgeError,
    service_call_targets,
    untargeted_request_detail,
)
from puffpai.zigbee_manager import ZigbeeManager  # noqa: E402

# Fabricated. None of these exist in any real house.
AREA = "test_area_garage"
DEVICE = "test_device_0000"

LIGHT = "light.test_porch_light"
SWITCH = "switch.test_desk_lamp"
CAMERA = "camera.test_hallway_cam"
SCRIPT_LIGHT = "script.test_porch_light_on"
TODO_LIST = "todo.test_chores"
FAKE_IEEE = "00:11:22:33:44:55:66:77"

CONFIG_LIGHT = {
    "alias": "Porch light on",
    "sequence": [{"action": "light.turn_on", "target": {"entity_id": LIGHT}}],
}


# ══════════════════════════════════════════════════════════════════════
#  A bridge that cannot reach a real house
# ══════════════════════════════════════════════════════════════════════

class Trap(HABridge):
    """A real :class:`HABridge` with the wire cut and a recorder in its place.

    Subclassing the real class is the point: the gate under test is the real
    gate, in the real method. Only ``_request`` is replaced, so an ungated
    actuation shows up as an entry in ``wire`` rather than as something
    happening in a house.
    """

    def __init__(self, states=None, scripts=None):
        # Skip HABridge.__init__: it reads config and builds a Session.
        self.wire: list = []
        self._states = dict(states or {})
        self._scripts = dict(scripts or {})
        self._enabled = True
        self._url = "http://ha.invalid"

        class _NoCache:
            def get(self, key): return None
            def set(self, key, value): pass
            def invalidate(self, key=None): pass

        self._cache = _NoCache()

    def _request(self, endpoint, method="GET", data=None, timeout=None,
                 stream=False):
        self.wire.append((endpoint, method, data))

        class _Resp:
            def __init__(self, payload):
                self._payload = payload
                self.headers = {"Content-Type": "application/json"}
                self.content = b""

            def json(self):
                return self._payload

        if endpoint == "states":
            return _Resp([{"entity_id": eid, **state}
                          for eid, state in self._states.items()])
        if endpoint.startswith("states/"):
            return _Resp(self._states.get(endpoint.split("/", 1)[1], {}))
        if endpoint.startswith("config/script/config/"):
            object_id = endpoint.rsplit("/", 1)[1]
            if method == "GET":
                if object_id not in self._scripts:
                    raise HABridgeError(
                        f"HA returned HTTP 404: config/script/config/{object_id}")
                return _Resp(self._scripts[object_id])
            return _Resp({"result": "ok"})
        return _Resp([])

    @property
    def service_calls(self):
        """Only the calls that would actuate — POSTs to ``services/…``."""
        return [(e, m, d) for e, m, d in self.wire
                if e.startswith("services/") and m == "POST"]

    @property
    def writes(self):
        """Every POST of any kind. An actuation is always a POST."""
        return [(e, m, d) for e, m, d in self.wire if m == "POST"]


@pytest.fixture
def trap():
    return Trap(
        states={
            SCRIPT_LIGHT: {
                "state": "off",
                "attributes": {"friendly_name": "fabricated",
                               "mode": "single"}},
            # The *other* branch of ``_list_add`` — a named todo list, so the
            # untargeted rule never sees it. Section 4a pins that it stays that
            # way.
            TODO_LIST: {
                "state": "3",
                "attributes": {"friendly_name": "fabricated"}},
        },
        scripts={"test_porch_light_on": CONFIG_LIGHT},
    )


#: The house-wide sweeps. Each one was ALLOWED before this gate existed.
SWEEPS = [
    ("light", "turn_off", {}),
    ("switch", "turn_off", {}),
    ("media_player", "turn_off", {}),
    ("climate", "set_temperature", {"temperature": 90}),
    ("homeassistant", "turn_off", {}),
]


# ══════════════════════════════════════════════════════════════════════
#  1. The predicate on its own
# ══════════════════════════════════════════════════════════════════════

class TestUntargetedRequestDetail:

    @pytest.mark.parametrize("domain,service,data", SWEEPS)
    def test_a_call_naming_nothing_is_reported(self, domain, service, data):
        why = untargeted_request_detail(domain, service, [], data)
        assert why
        assert f"untargeted {domain}.{service}" in why, (
            "the reason must name the call it is refusing")

    def test_any_named_target_ends_the_question(self):
        assert untargeted_request_detail("light", "turn_off", [LIGHT], {}) == ""

    @pytest.mark.parametrize("selector", ["area_id", "device_id", "floor_id",
                                          "label_id"])
    def test_a_selector_hands_the_call_to_the_other_policy(self, selector):
        """A selector names *a* set, so this check is not the one that governs.

        ``unbounded_request_detail`` decides those, and it deliberately allows
        an area-targeted ``light.turn_on``. If this check fired too, that
        allowance would be silently cancelled — which is the regression
        section 5 guards end-to-end.
        """
        assert untargeted_request_detail(
            "light", "turn_off", [], {"target": {selector: "x"}}) == ""
        assert untargeted_request_detail(
            "light", "turn_off", [], {selector: "x"}) == ""

    def test_the_reason_names_the_blast_radius_for_an_entity_domain(self):
        why = untargeted_request_detail("light", "turn_off", [], {})
        assert "every light HA has" in why
        assert "name the entities" in why, (
            "a refusal with no remedy is a refusal people learn to click "
            "through")

    def test_the_reason_says_whole_house_for_the_domain_agnostic_service(self):
        why = untargeted_request_detail("homeassistant", "turn_off", [], {})
        assert "every entity in the house" in why

    def test_an_unknown_integration_domain_says_so_rather_than_guessing(self):
        why = untargeted_request_detail("zwave_js", "ping", [], {})
        assert why
        assert "whatever the zwave_js integration controls" in why

    def test_junk_service_data_is_survivable(self):
        for junk in (None, [], "entity_id", 7):
            assert untargeted_request_detail("light", "turn_off", [], junk)

    def test_a_per_script_service_names_its_target_in_the_service(self):
        """``POST services/script/goodnight`` runs ``script.goodnight``.

        ``_expand_stored_targets`` reconstructs that entity and classifies its
        steps (failing closed if it cannot read them), so this check must not
        also fire — it would turn every script invocation into a prompt while
        adding nothing the expansion did not already do.
        """
        assert untargeted_request_detail("script", "goodnight", [], {}) == ""

    @pytest.mark.parametrize("service", ["turn_on", "turn_off", "toggle"])
    def test_the_generic_script_services_are_not_a_script_name(self, service):
        why = untargeted_request_detail("script", service, [], {})
        assert why
        assert "every script HA has" in why


# ══════════════════════════════════════════════════════════════════════
#  2. The sweeps at the choke point. Zero HA writes.
# ══════════════════════════════════════════════════════════════════════

class TestTheSweepsAreRefused:
    """The recorder proves none of these reach a house."""

    @pytest.mark.parametrize("domain,service,data", SWEEPS)
    def test_each_sweep_is_refused(self, trap, domain, service, data):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service(domain, service, **data)
        assert trap.writes == []
        assert f"untargeted {domain}.{service}" in str(caught.value)

    def test_the_reason_reaches_the_person_through_the_refusal(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off")
        message = str(caught.value)
        assert "every light HA has" in message
        assert "could not enumerate" in message, (
            "the refusal keeps enforce_confirmation's existing wording for a "
            "force-refusal; only the detail is new")

    def test_the_proposal_shape_is_unchanged(self, trap):
        """``proposal{service, entity_id, domains}`` — what the dashboard renders."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off")
        proposal = caught.value.proposal
        assert set(proposal) == {"service", "entity_id", "domains"}
        assert proposal["service"] == "light.turn_off"
        assert proposal["entity_id"] is None
        assert proposal["domains"] == [], (
            "the refusal comes from force, not from a domain nobody named")

    @pytest.mark.parametrize("domain,service", [
        ("vacuum", "start"), ("vacuum", "return_to_base"), ("button", "press"),
        ("fan", "turn_on"), ("homeassistant", "toggle"),
        ("homeassistant", "restart"), ("homeassistant", "stop"),
        ("script", "turn_off"), ("scene", "turn_on"),
    ])
    def test_the_rest_of_the_untargeted_surface_is_refused_too(
            self, trap, domain, service):
        """Fail closed. ``homeassistant.restart`` is in here on purpose: it is
        untargeted and it is not on the allow-list, so it costs a confirmation.
        Over-matching costs a confirmation; under-matching costs the gate.
        """
        with pytest.raises(ConfirmationRequired):
            trap.call_service(domain, service)
        assert trap.writes == []

    def test_an_unknown_integration_domain_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("some_integration", "do_a_thing")
        assert trap.writes == []

    def test_a_refused_sweep_costs_no_reads_either(self, trap):
        """Not "before the POST" — before everything."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("light", "turn_off")
        assert trap.wire == []


# ══════════════════════════════════════════════════════════════════════
#  3. There is a way to yes, and only one thing that says it
# ══════════════════════════════════════════════════════════════════════

class TestOnlyTheTokenConfirms:

    @pytest.mark.parametrize("domain,service,data", SWEEPS)
    def test_the_token_lets_the_sweep_through_unchanged(self, trap, domain,
                                                        service, data):
        trap.call_service(domain, service, human_confirmed=HUMAN_CONFIRMED,
                          **data)
        assert trap.service_calls == [
            (f"services/{domain}/{service}", "POST", data)], (
            "the gate classifies; it does not rewrite the call")

    @pytest.mark.parametrize("spoof", [
        True, 1, "true", "yes", "HUMAN_CONFIRMED", "<HUMAN_CONFIRMED>",
        {"human_confirmed": True}, ["confirmed"], object(),
    ])
    def test_a_value_is_not_a_confirmation(self, trap, spoof):
        """Everything decoded JSON can express is a *value*. The token is an
        object identity in this process, which ``json.loads`` has no syntax
        for — so a request body, or a model-written tool argument, cannot
        forge one."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("light", "turn_off", human_confirmed=spoof)
        assert trap.writes == []

    def test_the_token_is_never_forwarded_to_home_assistant(self, trap):
        trap.call_service("light", "turn_off", human_confirmed=HUMAN_CONFIRMED)
        _, _, data = trap.service_calls[0]
        assert "human_confirmed" not in data


# ══════════════════════════════════════════════════════════════════════
#  4. The exemptions, each one exercised
# ══════════════════════════════════════════════════════════════════════

class TestTheExemptions:
    """Every entry here is a hole by design, so every entry gets a test that
    would fail if someone quietly widened the allow-list into it."""

    @pytest.mark.parametrize("domain,service", [
        ("automation", "reload"), ("scene", "reload"), ("script", "reload"),
        ("homeassistant", "reload_all"),
        ("homeassistant", "reload_config_entry"),
        ("homeassistant", "reload_core_config"),
        ("homeassistant", "check_config"),
    ])
    def test_a_config_reload_is_not_an_actuation(self, trap, domain, service):
        trap.call_service(domain, service)
        assert len(trap.service_calls) == 1

    def test_notify_carries_a_message_and_moves_nothing(self, trap):
        trap.call_service("notify", "notify", message="the porch light is on")
        assert len(trap.service_calls) == 1

    def test_any_notify_service_is_covered_not_just_the_default(self, trap):
        trap.call_service("notify", "mobile_app_fabricated", message="hello")
        assert len(trap.service_calls) == 1

    def test_a_persistent_notification_is_a_card_not_an_actuation(self, trap):
        trap.call_service("persistent_notification", "create",
                          message="hello", title="fabricated")
        assert len(trap.service_calls) == 1

    def test_a_per_script_service_still_passes_when_its_steps_are_benign(
            self, trap):
        """``script.test_porch_light_on`` turns on a light; the expansion reads
        the stored steps and finds nothing safety-critical."""
        trap.call_service("script", "test_porch_light_on")
        assert trap.service_calls == [
            ("services/script/test_porch_light_on", "POST", {})]

    def test_a_per_script_service_whose_steps_cannot_be_read_still_fails_closed(
            self, trap):
        """The exemption defers to ``_expand_stored_targets``; it does not
        replace it. An unreadable script is refused there."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("script", "test_no_such_script")
        assert trap.writes == []

    @pytest.mark.parametrize("service", [
        "reload_the_garage_open", "check_config_and_unlock", "preload",
        "notifyx",
    ])
    def test_the_allow_list_is_matched_exactly_not_as_a_substring(
            self, trap, service):
        """``reload_and_open_the_garage`` must not inherit ``reload``'s pass."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("cover", service)
        assert trap.writes == []

    def test_a_domain_merely_containing_notify_is_not_exempt(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("notifyd", "send")
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  4a. THE SHOPPING LIST — the exemption a household actually feels
# ══════════════════════════════════════════════════════════════════════
#
# HA's ``shopping_list`` integration owns one global list and takes no
# ``entity_id`` on any of its services, so untargeted is not a sloppy spelling
# of these calls — it is the only spelling there is. Before the domain was
# exempted, ``StewardAgent._list_add`` prompted for "add milk to the shopping
# list", which is the single most ordinary thing a household assistant does; a
# gate that stops that is a gate people learn to dismiss without reading, and
# then it protects nothing.
#
# The honest residual is asserted rather than papered over: the destructive
# services in the domain (``clear_completed_items``, ``complete_all``,
# ``remove_item``) are exempt too, because they reach the same text. That is a
# lost list of words, not a lock. What the tests below then pin hard is that the
# exemption is worth exactly those eight service names and not a character more.
#
# The exemption is name-scoped for a reason worth stating twice. Written as a
# whole domain it would have been name-agnostic: ``shopping_list.unlock``,
# ``shopping_list.press`` and any actuating service HA ever adds to the domain
# would have reached the wire untargeted and unasked, with nothing here going
# red. That is the same widening the file already refuses for service names
# (``reload_and_open_the_garage`` must not inherit ``reload``'s pass), so it is
# refused here too. ``TestTheDomainDoesNotWidenForward`` is the pin.

#: Every service the integration exposes, and — since the exemption is
#: name-scoped — the exact contents of the allow-list entry. All of them are
#: untargeted by construction, so all of them are exempt or none of them are.
SHOPPING_LIST_SERVICES = [
    "add_item", "remove_item", "complete_item", "incomplete_item",
    "complete_all", "incomplete_all", "clear_completed_items", "sort",
]

#: Services HA does *not* define in ``shopping_list`` today. Each is spelled as
#: something that actuates in the domain it normally lives in, plus one plainly
#: invented name, because the question this section answers is not "does HA have
#: this service" but "what happens the day it does".
SHOPPING_LIST_NON_SERVICES = [
    "unlock", "press", "open_cover", "turn_off", "turn_on",
    "some_future_actuating_service",
]


class TestTheShoppingListDomain:

    def test_add_item_reaches_the_wire_with_no_token(self, trap):
        """The motivating call, at the choke point. No confirmation of any
        kind is passed, and the recorded wire call is the unmodified one."""
        trap.call_service("shopping_list", "add_item", name="oat milk")
        assert trap.service_calls == [
            ("services/shopping_list/add_item", "POST", {"name": "oat milk"})]

    @pytest.mark.parametrize("service", SHOPPING_LIST_SERVICES)
    def test_every_service_in_the_domain_passes_untargeted(self, trap, service):
        """All eight named services pass, including the ones that destroy list
        data — the accepted residual, asserted so that removing it is a
        deliberate act rather than a quiet drift.

        Name-scoped, not domain-scoped: the pass is worth exactly this list.
        ``TestTheDomainDoesNotWidenForward`` holds the other end.
        """
        trap.call_service("shopping_list", service)
        assert trap.service_calls == [
            (f"services/shopping_list/{service}", "POST", {})]

    def test_the_allow_list_entry_is_exactly_the_eight_documented_services(self):
        """The data and the justification beside it must not drift apart. This
        is the assertion the previous shape could not make at all: a domain
        entry has no service names to compare against."""
        from puffpai.ha_bridge import _NON_ACTUATING_DOMAIN_SERVICES
        assert (_NON_ACTUATING_DOMAIN_SERVICES["shopping_list"]
                == frozenset(SHOPPING_LIST_SERVICES))

    def test_the_domain_is_not_also_exempt_wholesale(self):
        """Belt and braces on the refactor: if ``shopping_list`` were left in
        the domain-wide set as well, every forward-tightness test below would
        pass for the wrong reason — the domain check runs first."""
        from puffpai.ha_bridge import _NON_ACTUATING_SERVICE_DOMAINS
        assert "shopping_list" not in _NON_ACTUATING_SERVICE_DOMAINS

    @pytest.mark.parametrize("domain", ["shopping_list_extra", "shopping",
                                        "shoppinglist", "shopping_lists",
                                        "myshopping_list"])
    def test_a_lookalike_domain_inherits_nothing(self, trap, domain):
        """Exact-set membership, never a prefix or a substring."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service(domain, "add_item", name="milk")
        assert trap.writes == []

    def test_the_domain_name_used_as_a_service_is_not_exempt(self, trap):
        """``light.shopping_list`` borrows the word, not the reasoning."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("light", "shopping_list")
        assert trap.writes == []

    @pytest.mark.parametrize("domain,service", [
        ("light", "turn_off"), ("homeassistant", "turn_off"),
    ])
    def test_the_sweeps_next_door_are_still_refused(self, trap, domain,
                                                    service):
        """Re-pinned here rather than only in section 2: the cheapest way to
        widen an allow-list by accident is to widen the *matching*, and this
        would go green if someone did."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service(domain, service)
        assert trap.writes == []

    @pytest.mark.parametrize("domain,service,entity", [
        ("lock", "unlock", "lock.test_front_door"),
        ("cover", "open_cover", "cover.test_garage_door"),
    ])
    def test_the_safety_critical_domains_are_untouched(self, trap, domain,
                                                       service, entity):
        """A *named* lock or cover was always gated by
        :data:`SAFETY_CRITICAL_DOMAINS` and still is. The shopping list is the
        other end of the scale this gate's authority is drawn around: a lost
        list is retyped, an opened lock is not."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service(domain, service, entity_id=entity)
        assert trap.writes == []
        trap.call_service(domain, service, entity_id=entity,
                          human_confirmed=HUMAN_CONFIRMED)
        assert len(trap.service_calls) == 1

    def test_a_targeted_todo_add_item_is_the_other_branch_and_still_passes(
            self, trap):
        """``list='todo'`` names a ``todo.*`` entity, so the untargeted rule
        never sees it. ``todo`` is deliberately *not* on the allow-list."""
        trap.call_service("todo", "add_item", entity_id=TODO_LIST,
                          item="water plants")
        assert trap.service_calls == [
            ("services/todo/add_item", "POST",
             {"entity_id": TODO_LIST, "item": "water plants"})]

    def test_an_untargeted_todo_call_is_still_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("todo", "add_item", item="water plants")
        assert trap.writes == []


class TestTheDomainDoesNotWidenForward:
    """The day HA adds an actuating service to ``shopping_list``.

    This is the failure the name-scoping exists to prevent, and it is not
    hypothetical in kind: a domain-wide exemption is name-agnostic by
    construction, so it grants a pass to services that do not exist yet and
    were never reasoned about. Nothing about "one global list of words" is true
    of a service called ``unlock``; the exemption's justification enumerates
    eight services, and the code now enumerates the same eight.

    Refused *and* nothing on the wire — the recorder is the proof, because "the
    gate declined" and "the call went out anyway" are otherwise indistinguishable
    from the exception alone.
    """

    @pytest.mark.parametrize("service", SHOPPING_LIST_NON_SERVICES)
    def test_an_unnamed_service_in_the_domain_is_refused(self, trap, service):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("shopping_list", service)
        assert trap.writes == []
        assert f"untargeted shopping_list.{service}" in str(caught.value), (
            "the refusal must name the call, not the domain's reputation")

    @pytest.mark.parametrize("service", SHOPPING_LIST_NON_SERVICES)
    def test_the_predicate_says_the_same_thing_on_its_own(self, service):
        assert untargeted_request_detail("shopping_list", service, [], {})

    @pytest.mark.parametrize("service", [
        "add_item_and_unlock", "add_item2", "sortx", "clear_completed",
    ])
    def test_the_service_names_are_matched_exactly_not_by_prefix(
            self, trap, service):
        """``add_item`` must not lend its pass to ``add_item_and_unlock``, and a
        truncation of a listed name is not a listed name either."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("shopping_list", service)
        assert trap.writes == []

    def test_a_named_service_from_another_domain_is_not_borrowed(self, trap):
        """``add_item`` is exempt *in ``shopping_list``* and nowhere else. The
        entry is a (domain, name) pair, not a second global name list."""
        for domain in ("lock", "cover", "todo"):
            trap.wire.clear()
            with pytest.raises(ConfirmationRequired):
                trap.call_service(domain, "add_item")
            assert trap.writes == []


class TestTheDomainWideExemptionsStillWork:
    """``notify`` cannot be name-scoped, and the refactor must not have taken
    the domain-wide path away from it.

    The service name in ``notify`` *is* the destination — it is whatever the
    notifiers in a given install are called — so there is no set to enumerate.
    That is the distinction the allow-lists now encode: bounded domains get a
    name list, unbounded ones stay domain-wide.
    """

    @pytest.mark.parametrize("service", [
        "notify", "mobile_app_pixel", "mobile_app_fabricated",
        "persistent_notification", "some_new_destination",
        "a_notifier_named_after_a_phone_bought_next_year",
    ])
    def test_any_notify_destination_still_passes_untargeted(self, trap,
                                                            service):
        trap.call_service("notify", service, message="hello")
        assert trap.service_calls == [
            (f"services/notify/{service}", "POST", {"message": "hello"})]

    @pytest.mark.parametrize("service", ["create", "dismiss", "dismiss_all"])
    def test_persistent_notification_still_passes_untargeted(self, trap,
                                                             service):
        """Bounded, and therefore a candidate for the same name-scoping — but
        deliberately unchanged in this pass. If that ever changes, this test is
        where the decision shows up."""
        trap.call_service("persistent_notification", service)
        assert trap.service_calls == [
            (f"services/persistent_notification/{service}", "POST", {})]


class TestListAddEndToEnd:
    """The regression that motivated the exemption, driven through the real
    :class:`StewardAgent` over the trapped real bridge.

    Nothing here is a double. The agent is the shipping one, the proxy it
    builds in ``_house`` is the shipping one, the bridge is a real
    :class:`HABridge` with the socket replaced by a recorder — so "the autonomous
    add works again" and "the wire saw exactly one ``shopping_list.add_item``"
    are the same observable fact.
    """

    @staticmethod
    def _steward(bridge):
        from puffpai.agents.steward import StewardAgent
        return StewardAgent(ha_bridge=bridge, announce_hosts=())

    @staticmethod
    def _envelope(payload, *, confirmed=False, origin="concierge"):
        """A NON-human origin and NO confirmation — the autonomous case.

        This is the envelope shape "add milk to the shopping list" arrives on
        when the Concierge routes it: state-changing, so Sentinel reviewed it,
        but nothing in it is evidence of a person standing there.
        """
        from puffpai.agents.envelope import Budget, Envelope
        body = dict(payload)
        if confirmed:
            body["confirmed"] = True
        return Envelope(intent="list_add", payload=body, requester="owner",
                        origin_agent=origin,
                        budget=Budget(max_actions=3, max_seconds=30.0),
                        state_changing=True)

    def test_add_milk_succeeds_with_no_confirmation(self, trap):
        agent = self._steward(trap)
        env = self._envelope({"item": "milk"})
        reply = agent.handle(env)

        assert reply["ok"] is True, reply
        assert reply.get("requires_confirmation") is not True, (
            "an autonomous add must not come back as a proposal")
        assert reply["service"] == "shopping_list.add_item"
        assert trap.service_calls == [
            ("services/shopping_list/add_item", "POST", {"name": "milk"})]
        assert env.budget.actions_used == 1

    def test_the_todo_branch_still_works_through_the_agent(self, trap):
        agent = self._steward(trap)
        reply = agent.handle(self._envelope({"item": "water plants",
                                             "entity_id": TODO_LIST}))
        assert reply["ok"] is True, reply
        assert reply["service"] == "todo.add_item"
        assert trap.service_calls == [
            ("services/todo/add_item", "POST",
             {"entity_id": TODO_LIST, "item": "water plants"})]

    def test_the_proxy_alone_pins_the_same_case(self, trap):
        """The narrower pin, in case driving the whole agent ever becomes the
        wrong shape for this file: an unconfirmed envelope from an agent origin
        is the exact combination that was raising."""
        from puffpai.agents.envelope import Envelope
        from puffpai.agents.steward import _GuardedHA
        env = Envelope(intent="call_service", payload={"confirmed": False},
                       requester="owner", origin_agent="concierge",
                       state_changing=True)
        _GuardedHA(trap, env).call_service("shopping_list", "add_item",
                                           name="milk")
        assert trap.service_calls == [
            ("services/shopping_list/add_item", "POST", {"name": "milk"})]

    def test_a_sweep_through_the_same_agent_is_still_a_proposal(self, trap):
        """The exemption did not soften ``StewardAgent``. An intent that
        reaches an untargeted actuation still comes back refused, with nothing
        on the wire."""
        from puffpai.agents.envelope import Budget, Envelope
        from puffpai.agents.steward import _GuardedHA
        env = Envelope(intent="call_service", payload={"confirmed": False},
                       requester="owner", origin_agent="concierge",
                       budget=Budget(max_actions=3, max_seconds=30.0),
                       state_changing=True)
        with pytest.raises(ConfirmationRequired):
            _GuardedHA(trap, env).call_service("light", "turn_off")
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  5. The regression guard: every targeted form is untouched
# ══════════════════════════════════════════════════════════════════════

class TestTargetedCallsAreUnaffected:
    """Half the design. A gate that prompts for ordinary traffic is a gate
    people learn to dismiss without reading, and then it protects nothing.
    """

    def test_the_entity_id_argument(self, trap):
        trap.call_service("light", "turn_off", entity_id=LIGHT)
        assert trap.wire == [
            ("services/light/turn_off", "POST", {"entity_id": LIGHT})]

    def test_a_list_of_entity_ids(self, trap):
        trap.call_service("light", "turn_off", entity_id=[LIGHT, SWITCH])
        assert len(trap.service_calls) == 1

    def test_an_entity_id_inside_the_service_data(self):
        """Reachable through the shared classifier rather than through
        ``call_service``, whose signature consumes ``entity_id`` as a parameter
        — so this pins the classifier's own reading of that place."""
        assert service_call_targets(None, {"entity_id": LIGHT}) == [LIGHT]
        assert untargeted_request_detail(
            "light", "turn_off",
            service_call_targets(None, {"entity_id": LIGHT}),
            {"entity_id": LIGHT}) == ""

    def test_a_modern_target_block(self, trap):
        trap.call_service("light", "turn_off", target={"entity_id": LIGHT})
        assert len(trap.service_calls) == 1

    def test_scene_apply_entities(self, trap):
        trap.call_service("scene", "apply", entities={LIGHT: "on"})
        assert len(trap.service_calls) == 1

    def test_an_area_targeted_light_is_still_ordinary_traffic(self, trap):
        """The regression this gate could most easily have caused: an area is
        not a name, but it *is* a bound, and ``unbounded_request_detail``
        deliberately allows it. The new check must not fire behind its back."""
        trap.call_service("light", "turn_on", target={"area_id": AREA})
        assert trap.wire == [
            ("services/light/turn_on", "POST", {"target": {"area_id": AREA}})]

    @pytest.mark.parametrize("domain,service", [
        ("light", "turn_on"), ("switch", "turn_on"), ("fan", "turn_on"),
        ("climate", "set_temperature"), ("media_player", "media_pause"),
    ])
    def test_area_and_device_targeting_stays_allowed(self, trap, domain,
                                                     service):
        trap.call_service(domain, service, target={"area_id": AREA})
        trap.call_service(domain, service, target={"device_id": DEVICE})
        assert len(trap.service_calls) == 2

    def test_a_selector_on_an_unbounded_domain_is_still_the_other_refusal(
            self, trap):
        """Not a change of verdict, a change of *reason*: this stays refused by
        ``unbounded_request_detail``, and the new check must not claim it."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("homeassistant", "turn_on",
                              target={"area_id": AREA})
        message = str(caught.value)
        assert "not knowable here" in message
        assert "untargeted" not in message
        assert trap.writes == []

    def test_the_broadcast_entity_id_is_now_the_other_refusal(self, trap):
        """The residual the old version of this test documented is closed.

        ``entity_id: "all"`` is HA's explicit broadcast, and it is still a
        *named* target — so this check still stands down on it, exactly as it
        did. What changed is what happens next: ``broadcast_request_detail``
        now picks it up and refuses it with the same blast radius, because a
        sweep spelled out loud is still a sweep. So this is not a change of
        verdict for *this* rule, it is a hand-off; the refusal below carries
        the other rule's wording, not this one's.
        """
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off", entity_id="all")
        message = str(caught.value)
        assert 'entity_id "all"' in message
        assert "untargeted" not in message, (
            "this check did not fire; the broadcast rule did")
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  6. THE DISARM EXEMPTION — closing the Zigbee pairing window
# ══════════════════════════════════════════════════════════════════════

class TestZhaPermitClose:
    """``zha.permit duration=0`` is ZHA's *close*; there is no separate service.

    This is the one exemption whose failure mode is worse than a false prompt.
    ``ZigbeeManager.close_permit_join`` reaches the bridge with **no token at
    all**, by design ("reducing exposure never needs permission"), so a gate
    that refuses it is a gate that can hold a pairing window open — during
    which any Zigbee device within radio range may join the network.
    """

    def test_the_close_reaches_the_wire_ungated(self, trap):
        trap.call_service("zha", "permit", None, duration=0)
        assert trap.service_calls == [
            ("services/zha/permit", "POST", {"duration": 0})]

    def test_the_close_through_a_named_router_is_ungated_too(self, trap):
        trap.call_service("zha", "permit", None, duration=0, ieee=FAKE_IEEE)
        assert trap.service_calls == [
            ("services/zha/permit", "POST",
             {"duration": 0, "ieee": FAKE_IEEE})]

    def test_a_string_zero_also_closes(self, trap):
        """The LLM tool layer passes durations as text."""
        trap.call_service("zha", "permit", None, duration="0")
        assert len(trap.service_calls) == 1

    def test_the_open_direction_is_refused_without_a_token(self, trap):
        """The hardening this change adds: a direct
        ``POST /ha/service/zha/permit {"duration": 60}`` bypasses
        ``ZigbeeManager.open_permit_join`` and its gate entirely. It used to
        slip the bridge too. It does not any more."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("zha", "permit", None, duration=60)
        assert trap.writes == []
        assert "untargeted zha.permit" in str(caught.value)

    def test_the_open_direction_goes_through_with_the_token(self, trap):
        """``ZigbeeManager.open_permit_join`` passes the token down after a
        person has confirmed, so the real open path is unaffected."""
        trap.call_service("zha", "permit", None, duration=60,
                          human_confirmed=HUMAN_CONFIRMED)
        assert trap.service_calls == [
            ("services/zha/permit", "POST", {"duration": 60})]

    def test_a_missing_duration_is_refused(self, trap):
        """HA defaults a missing ``zha.permit`` duration to a sixty-second
        **open**, so absence must never read as the close."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("zha", "permit", None)
        assert trap.writes == []

    @pytest.mark.parametrize("duration", [
        False, True, None, "", "abc", "0x0", " 0 0", "00abc", 0.0, -0.0,
        [0], {"duration": 0}, "٠",
    ])
    def test_only_a_real_zero_is_the_close(self, trap, duration):
        """``False == 0`` in Python and ``bool`` is an ``int`` subclass, so a
        loose comparison would have made ``duration=False`` a free close —
        which is really an *open*, because HA would reject the value and the
        argument would never have meant what the check thought it did. Every
        ambiguity resolves to "gate it"."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("zha", "permit", None, duration=duration)
        assert trap.writes == []

    @pytest.mark.parametrize("spelling", [0, "0", "00", "  0  "])
    def test_the_spellings_of_zero_that_do_close(self, trap, spelling):
        trap.call_service("zha", "permit", None, duration=spelling)
        assert len(trap.service_calls) == 1

    def test_the_exemption_is_bound_to_this_one_service(self, trap):
        """``duration=0`` is not a magic word. It exempts ``zha.permit`` and
        nothing else."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("zha", "issue_zigbee_cluster_command",
                              duration=0)
        with pytest.raises(ConfirmationRequired):
            trap.call_service("light", "turn_off", duration=0)
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  7. ZigbeeManager end to end, over a trapped real bridge
# ══════════════════════════════════════════════════════════════════════

class Landmine:
    """Stands in for anything that must not be touched. Attribute access
    explodes, so a call that goes around the bridge fails loudly."""

    def __init__(self, what: str):
        self._what = what

    def __getattr__(self, name):
        raise AssertionError(f"{self._what}.{name} was reached")

    def __call__(self, *a, **k):
        raise AssertionError(f"{self._what} was called")


@pytest.fixture
def zigbee(monkeypatch, trap):
    """A real :class:`ZigbeeManager` over the trapped real bridge.

    ``is_zha_installed`` opens a WebSocket and ``_rest`` is the direct-POST
    session the choke-point fix removed; the first is stubbed and the second is
    mined, so the only way out of this manager is through the bridge.
    """
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", lambda self: True)
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token",
                        bridge=trap)
    mgr._rest = Landmine("_rest")
    return mgr, trap


class TestZigbeeManagerCloseStillWorks:

    def test_close_permit_join_succeeds_end_to_end_ungated(self, zigbee):
        """The whole chain: the manager's own exemption, then the bridge's.

        ``close_permit_join`` passes no confirmation of any kind, so if the new
        check does not exempt the close, this raises instead of returning."""
        mgr, bridge = zigbee
        result = mgr.close_permit_join()
        assert result["success"] is True
        assert result["closed"] is True
        assert bridge.service_calls == [
            ("services/zha/permit", "POST", {"duration": 0})]

    def test_close_through_a_named_router_succeeds(self, zigbee):
        mgr, bridge = zigbee
        result = mgr.close_permit_join(ieee=FAKE_IEEE)
        assert result["success"] is True
        assert bridge.service_calls[0][2] == {"duration": 0,
                                              "ieee": FAKE_IEEE}

    def test_the_confirmed_open_still_reaches_the_wire(self, zigbee):
        mgr, bridge = zigbee
        result = mgr.open_permit_join(duration=45,
                                      human_confirmed=HUMAN_CONFIRMED)
        assert result["success"] is True
        assert bridge.service_calls == [
            ("services/zha/permit", "POST", {"duration": 45})]

    def test_the_unconfirmed_open_is_still_refused_before_anything(self, zigbee):
        mgr, bridge = zigbee
        with pytest.raises(ConfirmationRequired):
            mgr.open_permit_join(duration=60)
        assert bridge.wire == []


# ══════════════════════════════════════════════════════════════════════
#  8. The Steward's proxy inherits the gate from the bridge underneath
# ══════════════════════════════════════════════════════════════════════

class TestStewardProxy:
    """Enforcement lives at the choke point, so the proxy gets it for free.

    ``_GuardedHA`` translates between an :class:`Envelope` and the shared
    policy; it does not re-derive the target set. These run it over a real
    bridge, which is the only configuration that ships.
    """

    @staticmethod
    def _guard(bridge, *, confirmed=False, origin="concierge"):
        from puffpai.agents.envelope import Envelope
        from puffpai.agents.steward import _GuardedHA
        env = Envelope(intent="call_service",
                       payload={"confirmed": confirmed},
                       requester="owner", origin_agent=origin,
                       state_changing=True)
        return _GuardedHA(bridge, env)

    @pytest.mark.parametrize("domain,service,data", SWEEPS)
    def test_a_sweep_is_refused_through_the_proxy(self, trap, domain, service,
                                                  data):
        with pytest.raises(ConfirmationRequired):
            self._guard(trap).call_service(domain, service, **data)
        assert trap.writes == []

    def test_a_confirmed_envelope_from_a_human_origin_gets_through(self, trap):
        self._guard(trap, confirmed=True, origin="owner").call_service(
            "light", "turn_off")
        assert len(trap.service_calls) == 1

    def test_a_confirmed_envelope_from_an_agent_origin_does_not(self, trap):
        """``confirmed: true`` written by an agent is not evidence of a human.
        The origin has to be one a person actually arrives through."""
        with pytest.raises(ConfirmationRequired):
            self._guard(trap, confirmed=True,
                        origin="concierge").call_service("light", "turn_off")
        assert trap.writes == []

    def test_an_ordinary_targeted_call_still_passes_the_proxy(self, trap):
        self._guard(trap).call_service("light", "turn_off", entity_id=LIGHT)
        assert len(trap.service_calls) == 1


# ══════════════════════════════════════════════════════════════════════
#  9. The HTTP layer: 409 in the shape the dashboard already renders
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def api_mod():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        mod = importlib.import_module("puffpai.api_server")
    mod.app.config["TESTING"] = True
    return mod


@pytest.fixture
def wired(api_mod, monkeypatch, trap):
    monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: trap)
    api_mod._rate_limit_store.clear()
    api_mod._ha_tickets.clear()
    headers = {"X-API-Key": api_mod._API_KEY}
    with api_mod.app.test_client() as client:
        yield client, headers, trap, api_mod
    api_mod._ha_tickets.clear()


class TestRoutes:

    @pytest.mark.parametrize("domain,service,body", [
        (d, s, dict(data)) for d, s, data in SWEEPS
    ])
    def test_an_untargeted_body_is_refused_with_409(self, wired, domain,
                                                    service, body):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/service/{domain}/{service}", headers=headers,
                           json=body)
        assert resp.status_code == 409, resp.get_json()
        payload = resp.get_json()
        assert payload["needs_confirmation"] is True
        # No new envelope: the same proposal the dashboard already renders.
        assert set(payload["proposal"]) >= {"service", "entity_id", "domains"}
        assert f"untargeted {domain}.{service}" in payload["message"]
        assert trap.writes == []

    def test_an_empty_body_is_the_sweep(self, wired):
        """``POST /ha/service/light/turn_off {}`` — the shape from the docstring."""
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/light/turn_off", headers=headers,
                           json={})
        assert resp.status_code == 409, resp.get_json()
        assert "every light HA has" in resp.get_json()["message"]
        assert trap.writes == []

    def test_a_body_claiming_confirmation_does_not_confirm(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/light/turn_off", headers=headers,
                           json={"human_confirmed": True, "confirmed": True,
                                 "force": True})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_naming_the_entity_is_the_way_to_yes(self, wired):
        """The remedy the refusal message points at, with no ticket needed:
        a named light is not the always-confirm class."""
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/light/turn_off", headers=headers,
                           json={"entity_id": LIGHT})
        assert resp.status_code == 200, resp.get_json()
        assert len(trap.service_calls) == 1

    def test_a_direct_permit_open_over_http_is_refused(self, wired):
        """``POST /ha/service/zha/permit {"duration": 60}`` goes nowhere near
        ``ZigbeeManager.open_permit_join``, so until now it slipped both gates.
        This is the hardening; it is asserted here so it stays."""
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/zha/permit", headers=headers,
                           json={"duration": 60})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_a_notification_over_http_is_still_ordinary_traffic(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/persistent_notification/create",
                           headers=headers, json={"message": "fabricated"})
        assert resp.status_code == 200, resp.get_json()
        assert len(trap.service_calls) == 1
