"""A call that targets by *area* names nothing the gate can classify.

The hole this file closes. ``service_call_targets`` read ``entity_id``, service
data ``entity_id`` and ``target.entity_id`` — and nothing else. So the domains
handed to the gate were the service domain plus whatever *named* entities the
request happened to carry, and four call shapes reached the wire unclassified:

    ALLOWED   script.turn_on         target={"area_id": "garage"}
    ALLOWED   homeassistant.turn_on  target={"area_id": "garage"}
    ALLOWED   homeassistant.turn_on  target={"device_id": "abc"}
    ALLOWED   scene.apply            entities={"camera.hall": "off"}
    refused   cover.open_cover       target={"area_id": "garage"}   <- service domain

Two separate causes:

1. **Opaque selectors.** ``area_id`` / ``device_id`` / ``floor_id`` /
   ``label_id`` name a set this appliance cannot enumerate without the device
   and area registries, so the only remaining cap on the actuation is the
   *service* domain. For ``light.turn_on`` that cap is real and the call is
   ordinary. For the domain-agnostic services — ``homeassistant.turn_on/off/
   toggle``, ``script``, ``scene``, ``automation`` — there is no cap at all: an
   area holding a camera or a garage door was actuated with nothing checked.
   The irony is that ``automation_action_domains`` already treats exactly these
   selectors as not-knowable *inside* a stored config
   (``_OPAQUE_TARGET_KEYS``); the same rule was simply never applied to the live
   request.
2. **``scene.apply``** takes a whole scene body inline as ``entities:
   {entity_id: state}``. Every entity it names is actuated, and none of them
   were read — the call carried the domains ``{"scene"}``, which is never
   safety-critical.

The fix is deliberately **not** "refuse every area-targeted call".
``light.turn_on`` by area is ordinary dashboard traffic; prompting for it would
teach people to click through confirmations, which is how a gate stops meaning
anything. Only the combination — an opaque selector *and* a service domain that
bounds nothing — forces a confirmation. Section 6 is the regression guard for
that half, and it matters as much as the refusals do.

The worked examples are ``camera`` (the household's own witness) and ``cover``
(a garage door). **Every entity id, area id and device id in this file is
fabricated, and nothing here may reach a real Home Assistant.** The bridge is a
real ``HABridge`` subclass whose ``_request`` — the only method that would open
a socket — is replaced by a recorder, so "the gate leaked" and "the test touched
a house" are the same observable event: an entry in ``bridge.wire``.

Run: pytest tests/test_ha_opaque_target_gate.py -q
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
    request_opaque_selectors,
    service_call_targets,
    unbounded_request_detail,
)

# Fabricated. None of these exist in any real house.
AREA = "test_area_garage"
DEVICE = "test_device_0000"
FLOOR = "test_floor_ground"
LABEL = "test_label_outdoors"

CAMERA = "camera.test_hallway_cam"
COVER = "cover.test_garage_door"
LIGHT = "light.test_porch_light"
SCRIPT_LIGHT = "script.test_porch_light_on"

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
        states={SCRIPT_LIGHT: {
            "state": "off",
            "attributes": {"friendly_name": "fabricated", "mode": "single"}}},
        scripts={"test_porch_light_on": CONFIG_LIGHT},
    )


# ══════════════════════════════════════════════════════════════════════
#  1. Reading the selectors out of a live request
# ══════════════════════════════════════════════════════════════════════

class TestRequestOpaqueSelectors:

    @pytest.mark.parametrize("selector", ["area_id", "device_id", "floor_id",
                                          "label_id"])
    def test_every_opaque_selector_is_seen_in_a_target_block(self, selector):
        assert request_opaque_selectors({"target": {selector: "x"}}) == [selector]

    @pytest.mark.parametrize("selector", ["area_id", "device_id", "floor_id",
                                          "label_id"])
    def test_every_opaque_selector_is_seen_at_the_top_level(self, selector):
        """HA honours the legacy flat form too, so it is read too."""
        assert request_opaque_selectors({selector: "x"}) == [selector]

    def test_both_places_at_once_are_reported_once_each(self):
        assert request_opaque_selectors(
            {"area_id": AREA, "target": {"device_id": DEVICE,
                                         "area_id": AREA}}
        ) == ["area_id", "device_id"]

    def test_a_named_entity_target_is_not_opaque(self):
        assert request_opaque_selectors({"target": {"entity_id": LIGHT}}) == []

    def test_an_empty_selector_names_nothing_and_is_ignored(self):
        """``{"area_id": None}`` targets nothing; it must not cost a prompt."""
        for empty in (None, "", [], {}):
            assert request_opaque_selectors({"target": {"area_id": empty}}) == []

    def test_junk_service_data_is_survivable(self):
        for junk in (None, [], "area_id", 7, {"target": "not_a_dict"}):
            assert request_opaque_selectors(junk) == []


# ══════════════════════════════════════════════════════════════════════
#  2. Does the service domain bound what the call can reach?
# ══════════════════════════════════════════════════════════════════════

class TestUnboundedRequestDetail:

    @pytest.mark.parametrize("domain", ["homeassistant", "script", "scene",
                                        "automation"])
    def test_the_domain_agnostic_services_bound_nothing(self, domain):
        why = unbounded_request_detail(domain, "turn_on",
                                       {"target": {"area_id": AREA}})
        assert why
        assert "area_id" in why
        assert f"{domain}.turn_on" in why, "the reason must name the call"

    @pytest.mark.parametrize("domain", ["light", "switch", "fan", "climate",
                                        "media_player"])
    def test_a_real_entity_domain_is_its_own_bound(self, domain):
        """The whole point of not blanket-refusing. See section 6."""
        assert unbounded_request_detail(domain, "turn_on",
                                        {"target": {"area_id": AREA}}) == ""

    def test_an_unrecognised_service_domain_bounds_nothing(self):
        """An integration service — ``zwave_js.set_config_parameter``, say —
        takes ``device_id``/``area_id`` and reconfigures whatever hardware is
        there, locks included. Not an entity domain, so not a bound.
        """
        assert unbounded_request_detail(
            "zwave_js", "set_config_parameter",
            {"target": {"area_id": AREA}})

    def test_no_selector_means_nothing_to_report(self):
        assert unbounded_request_detail(
            "homeassistant", "turn_on", {"target": {"entity_id": LIGHT}}) == ""

    def test_the_reason_says_what_it_could_not_know_and_what_to_do(self):
        why = unbounded_request_detail("homeassistant", "turn_on",
                                       {"target": {"area_id": AREA}})
        assert "not knowable here" in why
        assert "name the entities" in why, (
            "a refusal with no remedy is a refusal people learn to click "
            "through")


# ══════════════════════════════════════════════════════════════════════
#  3. scene.apply carries a whole scene body inline
# ══════════════════════════════════════════════════════════════════════

class TestSceneApplyEntities:

    def test_the_entities_mapping_keys_are_targets(self):
        assert service_call_targets(
            None, {"entities": {CAMERA: "off", LIGHT: "on"}},
            service_domain="scene", service="apply") == [CAMERA, LIGHT]

    def test_scene_create_is_not_folded_in(self):
        """``scene.create`` writes a definition; it does not actuate.

        Applying that scene later comes back through the gate as
        ``scene.turn_on``, where the stored member list is expanded.
        """
        assert service_call_targets(
            None, {"entities": {CAMERA: "off"}},
            service_domain="scene", service="create") == []

    def test_another_domain_with_an_entities_key_is_not_folded_in(self):
        assert service_call_targets(
            None, {"entities": {CAMERA: "off"}},
            service_domain="light", service="apply") == []

    def test_a_non_mapping_entities_value_is_survivable(self):
        for junk in ([CAMERA], "camera.x", 7, None):
            assert service_call_targets(None, {"entities": junk},
                                        service_domain="scene",
                                        service="apply") == []

    def test_named_targets_are_still_collected_alongside(self):
        assert service_call_targets(
            LIGHT, {"entities": {CAMERA: "off"}},
            service_domain="scene", service="apply") == [LIGHT, CAMERA]


# ══════════════════════════════════════════════════════════════════════
#  4. The four leaked shapes, at the choke point. Zero HA writes.
# ══════════════════════════════════════════════════════════════════════

class TestTheLeakedShapes:
    """Each of these was ALLOWED before. The recorder proves none reach a house."""

    def test_script_turn_on_by_area(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("script", "turn_on",
                              target={"area_id": AREA})
        assert trap.writes == []
        assert "area_id" in str(caught.value)

    def test_homeassistant_turn_on_by_area(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "turn_on",
                              target={"area_id": AREA})
        assert trap.writes == []

    def test_homeassistant_turn_on_by_device(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "turn_on",
                              target={"device_id": DEVICE})
        assert trap.writes == []

    def test_scene_apply_naming_a_camera(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("scene", "apply",
                              entities={CAMERA: "off"})
        assert trap.writes == []
        assert caught.value.proposal["domains"] == ["camera"], (
            "the camera must be classified, not merely refused as unknowable")

    @pytest.mark.parametrize("selector", ["floor_id", "label_id"])
    def test_the_other_two_selectors_too(self, trap, selector):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "toggle",
                              target={selector: "whatever"})
        assert trap.writes == []

    def test_the_legacy_flat_selector_form_too(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "turn_off", area_id=AREA)
        assert trap.writes == []

    def test_a_cover_by_area_is_still_refused_on_the_service_domain(self, trap):
        """The one shape that was already refused stays refused."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("cover", "open_cover", target={"area_id": AREA})
        assert trap.writes == []
        assert caught.value.proposal["domains"] == ["cover"]

    def test_scene_apply_naming_a_cover(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("scene", "apply",
                              entities={COVER: {"state": "open"}})
        assert trap.writes == []
        assert caught.value.proposal["domains"] == ["cover"]


# ══════════════════════════════════════════════════════════════════════
#  5. The refusal keeps the existing shape, and there is a way to yes
# ══════════════════════════════════════════════════════════════════════

class TestTheRefusalShape:

    def test_no_new_envelope(self, trap):
        """``proposal{service, entity_id, domains}`` — what the dashboard renders."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("homeassistant", "turn_on",
                              target={"area_id": AREA})
        proposal = caught.value.proposal
        assert set(proposal) == {"service", "entity_id", "domains"}
        assert proposal["service"] == "homeassistant.turn_on"

    def test_an_unknowable_target_set_reports_no_domains_not_a_guess(self, trap):
        """``domains`` is the *critical* set, and here it is honestly empty.

        The refusal comes from ``force``, not from a domain nobody could name.
        A fabricated "camera" in that list would be a guess presented as a fact.
        """
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("homeassistant", "turn_on",
                              target={"area_id": AREA})
        assert caught.value.proposal["domains"] == []
        assert "could not enumerate" in str(caught.value)

    @pytest.mark.parametrize("kwargs", [
        {"target": {"area_id": AREA}},
        {"target": {"device_id": DEVICE}},
        {"entities": {CAMERA: "off"}},
    ])
    def test_a_confirmed_call_goes_through_unchanged(self, trap, kwargs):
        service = "apply" if "entities" in kwargs else "turn_on"
        domain = "scene" if "entities" in kwargs else "homeassistant"
        trap.call_service(domain, service, human_confirmed=HUMAN_CONFIRMED,
                          **kwargs)
        assert len(trap.service_calls) == 1
        # And the selector reached HA untouched: the gate classifies, it does
        # not rewrite the call.
        endpoint, _, data = trap.service_calls[0]
        assert endpoint == f"services/{domain}/{service}"
        assert data == kwargs

    def test_a_bare_true_still_does_not_confirm(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "turn_on", human_confirmed=True,
                              target={"area_id": AREA})
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  6. The regression guard: ordinary area calls must NOT prompt
# ══════════════════════════════════════════════════════════════════════

class TestOrdinaryAreaCallsAreUnaffected:
    """Half the design. A gate that prompts for "lights on in the garage" is a
    gate people learn to dismiss without reading, and then it protects nothing.
    """

    @pytest.mark.parametrize("domain,service", [
        ("light", "turn_on"), ("light", "turn_off"), ("light", "toggle"),
        ("switch", "turn_on"), ("fan", "turn_on"), ("climate", "set_temperature"),
        ("media_player", "media_pause"),
    ])
    def test_a_bounded_domain_by_area_is_allowed(self, trap, domain, service):
        trap.call_service(domain, service, target={"area_id": AREA})
        assert len(trap.service_calls) == 1

    def test_a_bounded_domain_by_device_is_allowed(self, trap):
        trap.call_service("light", "turn_on", target={"device_id": DEVICE})
        assert len(trap.service_calls) == 1

    def test_an_area_call_costs_no_extra_reads(self, trap):
        """The check is pure: no registry fetch, no state read, one POST."""
        trap.call_service("light", "turn_on", target={"area_id": AREA})
        assert trap.wire == [
            ("services/light/turn_on", "POST", {"target": {"area_id": AREA}})]

    def test_a_named_entity_call_is_untouched(self, trap):
        trap.call_service("light", "turn_on", entity_id=LIGHT)
        assert trap.wire == [
            ("services/light/turn_on", "POST", {"entity_id": LIGHT})]

    def test_a_benign_script_by_name_still_passes(self, trap):
        """Naming the script is knowable; only naming an *area* is not."""
        trap.call_service("script", "turn_on", entity_id=SCRIPT_LIGHT)
        assert len(trap.service_calls) == 1

    def test_a_notify_message_mentioning_an_area_is_not_a_target(self, trap):
        trap.call_service("notify", "mobile_app",
                          message=f"lights in {AREA} are on")
        assert len(trap.service_calls) == 1

    def test_a_scene_apply_of_lights_only_is_allowed(self, trap):
        trap.call_service("scene", "apply", entities={LIGHT: "on"})
        assert len(trap.service_calls) == 1


# ══════════════════════════════════════════════════════════════════════
#  7. The Steward's proxy classifies the same call the same way
# ══════════════════════════════════════════════════════════════════════

class TestStewardProxy:
    """The proxy inherits this gate from the real bridge underneath it.

    ``_GuardedHA`` is the Steward's *envelope adapter*: it decides whether an
    envelope carries a real human confirmation and shapes the refusal into a
    proposal. It is deliberately **not** a second place where the target set is
    re-derived. The whole point of the choke point is that
    every caller — the Steward, the legacy LLM tools, the ``/ha/*`` routes —
    passes through ``HABridge.call_service``, so the classification happens once,
    where the request leaves the appliance.

    A per-agent copy of the derivation is the wrong design: it can only ever
    protect the callers that choose to come through that agent, and keeping two
    derivations in step is exactly the drift the choke point exists to prevent.
    Consequently these tests run the proxy over a *real* bridge, which is the
    only configuration that ships. (An injected double that implements
    ``call_service`` itself has no gate — that is a property of the double, not
    of the proxy, and asserting otherwise would pin the rejected design.)
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

    @pytest.mark.parametrize("domain,service,data", [
        ("script", "turn_on", {"target": {"area_id": AREA}}),
        ("homeassistant", "turn_on", {"target": {"area_id": AREA}}),
        ("homeassistant", "turn_on", {"target": {"device_id": DEVICE}}),
        ("scene", "apply", {"entities": {CAMERA: "off"}}),
        # Not one of the four, but the same shape: a named entity that only
        # appears inside ``target:``.
        ("homeassistant", "turn_off", {"target": {"entity_id": CAMERA}}),
    ])
    def test_every_leaked_shape_is_refused_through_the_proxy(self, trap, domain,
                                                             service, data):
        """Section 4, reached through the Steward instead of directly.

        Same bridge, same verdict, and — the part that matters — zero writes:
        the proxy hands the call down, the choke point refuses it, and
        ``ConfirmationRequired`` propagates up so ``StewardAgent.handle`` can
        turn it into a proposal reply.
        """
        with pytest.raises(ConfirmationRequired):
            self._guard(trap).call_service(domain, service, **data)
        assert trap.writes == []

    def test_the_real_bridge_refuses_through_the_proxy_too(self, trap):
        with pytest.raises(ConfirmationRequired):
            self._guard(trap).call_service("homeassistant", "turn_on",
                                           target={"area_id": AREA})
        assert trap.writes == []

    def test_an_ordinary_area_call_still_passes_the_proxy(self, trap):
        self._guard(trap).call_service("light", "turn_on",
                                       target={"area_id": AREA})
        assert len(trap.service_calls) == 1

    def test_a_confirmed_envelope_from_a_human_origin_gets_through(self, trap):
        self._guard(trap, confirmed=True, origin="owner").call_service(
            "homeassistant", "turn_on", target={"area_id": AREA})
        assert len(trap.service_calls) == 1


# ══════════════════════════════════════════════════════════════════════
#  8. The HTTP layer: 409 in the shape the dashboard already renders
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

    @pytest.mark.parametrize("path,body", [
        ("/ha/service/script/turn_on", {"target": {"area_id": AREA}}),
        ("/ha/service/homeassistant/turn_on", {"target": {"area_id": AREA}}),
        ("/ha/service/homeassistant/turn_on", {"target": {"device_id": DEVICE}}),
        ("/ha/service/scene/apply", {"entities": {CAMERA: "off"}}),
    ])
    def test_each_leaked_shape_is_refused_with_409(self, wired, path, body):
        client, headers, trap, _ = wired
        resp = client.post(path, headers=headers, json=body)
        assert resp.status_code == 409, resp.get_json()
        payload = resp.get_json()
        assert payload["needs_confirmation"] is True
        # No new envelope: the same proposal the dashboard already renders.
        assert set(payload["proposal"]) >= {"service", "entity_id", "domains"}
        assert trap.writes == []

    def test_the_message_explains_what_could_not_be_known(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/homeassistant/turn_on", headers=headers,
                           json={"target": {"area_id": AREA}})
        message = resp.get_json()["message"]
        assert "area_id" in message and "not knowable here" in message
        assert trap.writes == []

    def test_a_body_claiming_confirmation_does_not_confirm(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/homeassistant/turn_on", headers=headers,
                           json={"target": {"area_id": AREA},
                                 "human_confirmed": True, "confirmed": True,
                                 "force": True})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_an_ordinary_area_call_is_unaffected(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/light/turn_on", headers=headers,
                           json={"target": {"area_id": AREA}})
        assert resp.status_code == 200, resp.get_json()
        assert len(trap.service_calls) == 1

    def test_a_selector_only_call_has_no_ticket_path_and_stays_refused(self, wired):
        """An honest statement of a consequence, not an endorsement of it.

        ``/ha/confirm`` binds a ticket to one *entity_id*, and it requires a
        non-empty one — so a call whose only target is an area cannot be
        confirmed over HTTP at all. It is refused, permanently, until the caller
        names the entities. That is the fail-closed reading and it is what ships;
        it is asserted here so that the day someone wants a way to say yes to an
        area, they change this test on purpose rather than discovering the
        behaviour from a support message.
        """
        client, headers, trap, _ = wired
        minted = client.post("/ha/confirm", headers=headers,
                             json={"kind": "service", "entity_id": "",
                                   "action": "homeassistant.turn_on"})
        assert minted.status_code == 400, "an empty binding target was minted"

        resp = client.post("/ha/service/homeassistant/turn_on", headers=headers,
                           json={"target": {"area_id": AREA}})
        assert resp.status_code == 409
        assert trap.writes == []

    def test_naming_the_entities_is_the_way_to_yes(self, wired):
        """The remedy the refusal message points at, end to end."""
        client, headers, trap, _ = wired
        minted = client.post("/ha/confirm", headers=headers,
                             json={"kind": "service", "entity_id": CAMERA,
                                   "action": "homeassistant.turn_off"})
        assert minted.status_code == 201, minted.get_json()
        resp = client.post("/ha/service/homeassistant/turn_off", headers=headers,
                           json={"entity_id": CAMERA,
                                 "ticket": minted.get_json()["ticket"]})
        assert resp.status_code == 200, resp.get_json()
        assert len(trap.service_calls) == 1
