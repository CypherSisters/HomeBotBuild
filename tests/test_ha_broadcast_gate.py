"""The house-wide sweep spelled out loud: ``entity_id: "all"``.

The hole this file closes. ``untargeted_request_detail`` refuses a call that
names *no* target, because Home Assistant reads a missing target as the whole
domain. It cannot refuse the call that names *everything*: ``entity_id: "all"``
is HA's explicit broadcast (``ENTITY_MATCH_ALL``), and to every classifier in
this file's neighbourhood it is an ordinary named target —
``service_call_targets`` returns ``["all"]``, the untargeted check stands down
by construction, and ``entity_domain("all")`` is ``""`` so the domains handed to
the gate are just the service's own. Same blast radius, different spelling,
and without a rule of its own this spelling gets through:

    ALLOWED   light.turn_off          {"entity_id": "all"}   <- every light
    ALLOWED   switch.turn_off         {"entity_id": "all"}   <- every switch
    ALLOWED   media_player.turn_off   {"entity_id": "all"}
    ALLOWED   climate.set_temperature {"entity_id": "all", "temperature": 90}
    ALLOWED   homeassistant.turn_off  {"entity_id": "all"}   <- everything
    ALLOWED   light.turn_off          {"target": {"entity_id": "all"}}
    ALLOWED   scene.apply             {"entities": {"all": "off"}}

The rule gives it the same refusal and the same remedy as the untargeted call.

Refused by default, with **no allow-list at all**. Nothing in this appliance
needs to spell ``"all"``: the light routines in ``api_server``
(``_agent_task_lights``) enumerate the lights they mean, so they keep their
sweep over a call that names each entity (``tests/test_agent_task_ha_gate.py``
pins that wire shape). An unused exemption is a free hole.

Three things the rule deliberately leaves alone, each with a section here:

* ``entity_id: "none"`` — HA's ``ENTITY_MATCH_NONE``. It names zero entities, HA
  expands it to nothing, so the call moves nothing. Its existing verdict (a
  named target; it reaches the wire) is kept as-is. Noted rather than endorsed,
  and left where the existing policy put it.
* ``light.all`` — a real entity whose object id is literally ``all``. One lamp
  with an unfortunate name is not a broadcast.
* ``zha.permit duration=0`` — the Zigbee pairing window's *close*. It names no
  entity at all, so it never meets this rule. Section 5 is the part of this file
  that must never go green by accident: a gate that can refuse the close is a
  gate that can hold a pairing window open.

Mutual exclusivity with the untargeted rule is asserted in both directions, by
wording: that one needs every naming place empty, this one needs ``"all"``
named, and a call cannot be both.

**Every entity id in this file is fabricated, and nothing here may reach a real
Home Assistant.** Every bridge is a real ``HABridge`` subclass whose
``_request`` — the only method that opens a socket — is replaced by a recorder,
so "the gate leaked" and "the test touched a house" are the same observable
event: an entry in ``bridge.wire``.

Run: pytest tests/test_ha_broadcast_gate.py -q
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
    broadcast_request_detail,
    service_call_targets,
)

# Fabricated. None of these exist in any real house.
AREA = "test_area_garage"

LIGHT = "light.test_porch_light"
SWITCH = "switch.test_desk_lamp"
SCRIPT_LIGHT = "script.test_porch_light_on"

#: A real entity whose object id happens to be the broadcast word. Not the
#: broadcast: one lamp, badly named.
LIGHT_NAMED_ALL = "light.all"

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


#: The broadcasts. Each one was ALLOWED before this rule existed, and each
#: reaches the same set the untargeted form of itself would have.
BROADCASTS = [
    ("light", "turn_off", {}),
    ("switch", "turn_off", {}),
    ("media_player", "turn_off", {}),
    ("climate", "set_temperature", {"temperature": 90}),
    ("homeassistant", "turn_off", {}),
]

#: ``domain`` → what a broadcast in it reaches, said the way the refusal says it.
RADIUS = [
    ("light", "every light HA has"),
    ("switch", "every switch HA has"),
    ("media_player", "every media_player HA has"),
    ("climate", "every climate HA has"),
    ("vacuum", "every vacuum HA has"),
    ("script", "every script HA has"),
    ("scene", "every scene HA has"),
    ("homeassistant", "every entity in the house"),
]


# ══════════════════════════════════════════════════════════════════════
#  1. The predicate on its own
# ══════════════════════════════════════════════════════════════════════

class TestBroadcastRequestDetail:

    @pytest.mark.parametrize("domain,radius", RADIUS)
    def test_the_broadcast_is_reported_with_its_blast_radius(self, domain,
                                                             radius):
        why = broadcast_request_detail(domain, "turn_off", ["all"])
        assert why
        assert f"{domain}.turn_off" in why, (
            "the reason must name the call it is refusing")
        assert 'entity_id "all"' in why
        assert radius in why
        assert "name the entities" in why, (
            "a refusal with no remedy is a refusal people learn to click "
            "through")

    def test_an_unknown_integration_domain_says_so_rather_than_guessing(self):
        why = broadcast_request_detail("zwave_js", "ping", ["all"])
        assert why
        assert "whatever the zwave_js integration controls" in why

    def test_one_all_among_real_targets_is_still_the_sweep(self):
        """The list does not have to be *only* ``all``. HA honours every element,
        so one broadcast in a list of five named lights is still the broadcast.
        """
        assert broadcast_request_detail("light", "turn_off",
                                        [LIGHT, "all", SWITCH])

    @pytest.mark.parametrize("spelling", ["all", "All", "ALL", " all ", "aLl"])
    def test_case_and_whitespace_variants_still_fire(self, spelling):
        """HA's ``ENTITY_MATCH_ALL`` is the exact lowercase string, so ``"All"``
        would expand to nothing in a real house. Matched anyway: the caller
        plainly means the sweep, and over-matching costs one confirmation on a
        call that was going to do nothing, while the strict reading hands the
        sweep a spelling that walks past the gate."""
        assert broadcast_request_detail("light", "turn_off", [spelling])

    def test_a_bare_string_target_is_read_as_one_target(self):
        """``as_targets`` folds a lone string; the caller does not have to
        wrap it."""
        assert broadcast_request_detail("light", "turn_off", "all")

    def test_real_entity_ids_are_ordinary_traffic(self):
        assert broadcast_request_detail("light", "turn_off",
                                        [LIGHT, SWITCH]) == ""

    def test_no_targets_at_all_is_the_other_rule(self):
        """Mutual exclusivity by construction: an empty target list is
        ``untargeted_request_detail``'s territory and this check must not also
        claim it, or the two reasons would stack on one refusal."""
        assert broadcast_request_detail("light", "turn_off", []) == ""

    def test_the_match_none_magic_value_is_not_matched(self):
        """``"none"`` is HA's ``ENTITY_MATCH_NONE``: it expands to zero
        entities and the call moves nothing. Deliberately left with its
        existing verdict rather than folded in here."""
        assert broadcast_request_detail("light", "turn_off", ["none"]) == ""

    def test_an_entity_literally_named_all_is_not_the_broadcast(self):
        assert broadcast_request_detail("light", "turn_off",
                                        [LIGHT_NAMED_ALL]) == ""

    @pytest.mark.parametrize("junk", [
        None, 7, 0, [], [None], [7], [{"entity_id": "all"}],
        {"entity_id": "all"}, object(), [["all"]],
    ])
    def test_junk_targets_are_survivable_and_never_the_broadcast(self, junk):
        """Only a string spells the broadcast. Anything else is classified
        elsewhere (or is a caller error), and this check must not raise on the
        way past — a predicate that throws inside the choke point would turn a
        malformed call into a 500 instead of a verdict."""
        assert broadcast_request_detail("light", "turn_off", junk) == ""

    def test_the_reason_never_says_untargeted(self):
        """The two rules are told apart by their wording, in the dashboard and
        in every assertion below."""
        why = broadcast_request_detail("light", "turn_off", ["all"])
        assert "untargeted" not in why


# ══════════════════════════════════════════════════════════════════════
#  2. The choke point: every naming place, zero HA writes
# ══════════════════════════════════════════════════════════════════════

class TestTheBroadcastsAreRefused:
    """The recorder proves none of these reach a house."""

    @pytest.mark.parametrize("domain,service,data", BROADCASTS)
    def test_the_entity_id_argument(self, trap, domain, service, data):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service(domain, service, entity_id="all", **data)
        assert trap.writes == []
        message = str(caught.value)
        assert f"{domain}.{service}" in message
        assert 'entity_id "all"' in message

    def test_a_single_element_list(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off", entity_id=["all"])
        assert trap.writes == []
        assert 'entity_id "all"' in str(caught.value)

    def test_a_list_mixing_a_real_light_with_the_broadcast(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("light", "turn_off", entity_id=[LIGHT, "all"])
        assert trap.writes == []

    def test_the_modern_target_block(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off",
                              target={"entity_id": "all"})
        assert trap.writes == []
        assert 'entity_id "all"' in str(caught.value)

    def test_an_entity_id_inside_the_service_data(self):
        """Reachable through the shared classifier rather than through
        ``call_service``, whose signature consumes ``entity_id`` as a parameter
        — so this pins the classifier's own reading of that place, which is the
        reading this rule relies on instead of re-reading the service data."""
        assert service_call_targets(None, {"entity_id": "all"}) == ["all"]
        assert broadcast_request_detail(
            "light", "turn_off",
            service_call_targets(None, {"entity_id": "all"}))

    def test_scene_apply_entities_keys(self, trap):
        """``scene.apply`` carries a whole scene body inline; its ``entities:``
        keys are targets, and one of them can be the broadcast."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("scene", "apply", entities={"all": "off"})
        assert trap.writes == []
        assert 'entity_id "all"' in str(caught.value)

    def test_a_refused_broadcast_costs_no_reads_either(self, trap):
        """Not "before the POST" — before everything."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("light", "turn_off", entity_id="all")
        assert trap.wire == []

    def test_the_proposal_shape_is_unchanged(self, trap):
        """``proposal{service, entity_id, domains}`` — what the dashboard
        already renders. A new rule is not a new envelope."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off", entity_id="all")
        proposal = caught.value.proposal
        assert set(proposal) == {"service", "entity_id", "domains"}
        assert proposal["service"] == "light.turn_off"
        assert proposal["entity_id"] == "all"
        assert proposal["domains"] == [], (
            "the refusal comes from force, not from a domain nobody named")

    def test_the_refusal_keeps_the_existing_force_wording(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off", entity_id="all")
        assert "could not enumerate" in str(caught.value), (
            "only the detail is new; enforce_confirmation's wording for a "
            "force-refusal is unchanged")

    def test_the_broadcast_refusal_does_not_claim_to_be_untargeted(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off", entity_id="all")
        assert "untargeted" not in str(caught.value)

    def test_the_untargeted_refusal_does_not_claim_to_be_the_broadcast(self,
                                                                       trap):
        """The other direction of the same property: a call that names nothing
        is still the untargeted rule's, and this one must not have taken it."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("light", "turn_off")
        message = str(caught.value)
        assert "untargeted light.turn_off" in message
        assert 'entity_id "all"' not in message
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  3. There is a way to yes, and only one thing that says it
# ══════════════════════════════════════════════════════════════════════

class TestOnlyTheTokenConfirms:

    def test_the_token_lets_the_broadcast_through_unchanged(self, trap):
        trap.call_service("light", "turn_off", entity_id="all",
                          human_confirmed=HUMAN_CONFIRMED)
        assert trap.service_calls == [
            ("services/light/turn_off", "POST", {"entity_id": "all"})], (
            "the gate classifies; it does not rewrite the call")

    def test_the_token_is_never_forwarded_to_home_assistant(self, trap):
        trap.call_service("light", "turn_off", entity_id="all",
                          human_confirmed=HUMAN_CONFIRMED)
        _, _, data = trap.service_calls[0]
        assert "human_confirmed" not in data

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
            trap.call_service("light", "turn_off", entity_id="all",
                              human_confirmed=spoof)
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  4. The near-misses stay ordinary traffic
# ══════════════════════════════════════════════════════════════════════

class TestTheNearMissesAreUnaffected:
    """Half the design. A gate that prompts for ordinary traffic is a gate
    people learn to dismiss without reading, and then it protects nothing.
    """

    def test_a_named_entity(self, trap):
        trap.call_service("light", "turn_off", entity_id=LIGHT)
        assert trap.wire == [
            ("services/light/turn_off", "POST", {"entity_id": LIGHT})]

    def test_a_list_of_named_entities(self, trap):
        trap.call_service("light", "turn_off", entity_id=[LIGHT, SWITCH])
        assert len(trap.service_calls) == 1

    def test_an_area_targeted_light(self, trap):
        """An area is not a name, but it *is* a bound, and
        ``unbounded_request_detail`` deliberately allows it. The new check must
        not fire behind its back."""
        trap.call_service("light", "turn_on", target={"area_id": AREA})
        assert trap.wire == [
            ("services/light/turn_on", "POST", {"target": {"area_id": AREA}})]

    def test_the_match_none_magic_value_keeps_its_current_verdict(self, trap):
        """``entity_id: "none"`` is HA's ``ENTITY_MATCH_NONE``. It reaches the
        wire as one service call, and HA expands it to zero entities, so the
        call moves nothing. Recorded rather than endorsed: this is where the
        existing policy put it, and this change does not move it."""
        trap.call_service("light", "turn_off", entity_id="none")
        assert trap.service_calls == [
            ("services/light/turn_off", "POST", {"entity_id": "none"})]

    def test_an_entity_literally_named_all_passes(self, trap):
        """``light.all`` is one lamp with an unfortunate object id, not a
        broadcast."""
        trap.call_service("light", "turn_off", entity_id=LIGHT_NAMED_ALL)
        assert trap.service_calls == [
            ("services/light/turn_off", "POST",
             {"entity_id": LIGHT_NAMED_ALL})]

    def test_a_scene_apply_naming_real_entities_still_passes(self, trap):
        trap.call_service("scene", "apply", entities={LIGHT: "on"})
        assert len(trap.service_calls) == 1


# ══════════════════════════════════════════════════════════════════════
#  5. THE DISARM EXEMPTION — closing the Zigbee pairing window
# ══════════════════════════════════════════════════════════════════════

class TestZhaPermitCloseIsNeverGated:
    """``zha.permit duration=0`` is ZHA's *close*; there is no separate service.

    It names no entity at all, so this rule cannot reach it — but that is a
    property worth asserting rather than reasoning about, because the failure
    mode is worse than a false prompt. ``ZigbeeManager.close_permit_join``
    reaches the bridge with **no token at all**, by design ("reducing exposure
    never needs permission"), so a gate that refuses it is a gate that can hold
    a pairing window open, during which any Zigbee device within radio range
    may join the network.
    """

    def test_the_close_reaches_the_wire_ungated(self, trap):
        trap.call_service("zha", "permit", None, duration=0)
        assert trap.service_calls == [
            ("services/zha/permit", "POST", {"duration": 0})]

    @pytest.mark.parametrize("spelling", [0, "0", "00", "  0  "])
    def test_every_spelling_of_the_close_still_closes(self, trap, spelling):
        trap.call_service("zha", "permit", None, duration=spelling)
        assert len(trap.service_calls) == 1

    def test_the_open_direction_is_still_refused_by_the_untargeted_rule(self,
                                                                        trap):
        """Unchanged by this rule, and asserted so it stays that way: the open
        names no entity, so it is the untargeted refusal, not this one."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("zha", "permit", None, duration=60)
        assert trap.writes == []
        message = str(caught.value)
        assert "untargeted zha.permit" in message
        assert 'entity_id "all"' not in message


# ══════════════════════════════════════════════════════════════════════
#  6. The Steward's proxy inherits the gate from the bridge underneath
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

    def test_a_broadcast_is_refused_through_the_proxy(self, trap):
        with pytest.raises(ConfirmationRequired):
            self._guard(trap).call_service("light", "turn_off",
                                           entity_id="all")
        assert trap.writes == []

    def test_a_confirmed_envelope_from_a_human_origin_gets_through(self, trap):
        self._guard(trap, confirmed=True, origin="owner").call_service(
            "light", "turn_off", entity_id="all")
        assert trap.service_calls == [
            ("services/light/turn_off", "POST", {"entity_id": "all"})]

    def test_a_confirmed_envelope_from_an_agent_origin_does_not(self, trap):
        """``confirmed: true`` written by an agent is not evidence of a human.
        The origin has to be one a person actually arrives through."""
        with pytest.raises(ConfirmationRequired):
            self._guard(trap, confirmed=True, origin="concierge").call_service(
                "light", "turn_off", entity_id="all")
        assert trap.writes == []

    def test_an_ordinary_targeted_call_still_passes_the_proxy(self, trap):
        self._guard(trap).call_service("light", "turn_off", entity_id=LIGHT)
        assert len(trap.service_calls) == 1


# ══════════════════════════════════════════════════════════════════════
#  7. The HTTP layer: 409 in the shape the dashboard already renders
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

    @pytest.mark.parametrize("body", [
        {"entity_id": "all"},
        {"entity_id": ["all"]},
        {"target": {"entity_id": "all"}},
    ])
    def test_a_broadcast_body_is_refused_with_409(self, wired, body):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/light/turn_off", headers=headers,
                           json=body)
        assert resp.status_code == 409, resp.get_json()
        payload = resp.get_json()
        assert payload["needs_confirmation"] is True
        # No new envelope: the same proposal the dashboard already renders.
        assert set(payload["proposal"]) >= {"service", "entity_id", "domains"}
        assert 'entity_id "all"' in payload["message"]
        assert "every light HA has" in payload["message"]
        assert trap.writes == []

    def test_the_whole_house_broadcast_is_refused_too(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/homeassistant/turn_off",
                           headers=headers, json={"entity_id": "all"})
        assert resp.status_code == 409, resp.get_json()
        assert "every entity in the house" in resp.get_json()["message"]
        assert trap.writes == []

    def test_a_body_claiming_confirmation_does_not_confirm(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/light/turn_off", headers=headers,
                           json={"entity_id": "all", "human_confirmed": True,
                                 "confirmed": True, "force": True})
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
