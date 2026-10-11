"""Firing an automation is gated on what the automation *does*.

The hole this file closes. The always-confirm class is a property of the target,
enforced inside :class:`~puffpai.ha_bridge.HABridge`, and a person has a way to
say yes. Both classify on the domain of the thing being called, and
``automation`` is not a safety-critical domain. So:

* creating an automation whose action is ``camera.turn_off`` is refused
  (``create_automation`` is gated: a stored instruction to actuate is
  actuation), but
* **firing one that already exists** goes through ``call_service("automation",
  "trigger", …)``, sees the benign ``automation`` domain, and would be waved
  through on the domain alone.

Deferred actuation is still actuation. The instruction was simply written
earlier, possibly by the household, possibly by an agent, possibly by someone
who had HA access. ``automation.trigger`` on an
automation whose action turns off the hallway camera turns off the hallway
camera.

The gate classifies by resolving the automation's stored actions
(``config/automation/config/<id>``, the same endpoint ``create_automation``
writes to) and applying the identical policy to the domains those actions reach
— and **fails closed** when the actions cannot be read at all, because "I could
not find out what this would do" is not "it does nothing dangerous".

The worked examples are ``camera`` (turning off the household's own witness) and
``cover`` (a garage door). **Every entity id, automation id and config in this
file is fabricated, and nothing here may reach a real Home Assistant.** The
bridge is a real ``HABridge`` subclass whose ``_request`` — the only method that
would open a socket — is replaced by a recorder, so "the gate leaked" and "the
test touched a house" are the same observable event: an entry in ``bridge.wire``.

Run: pytest tests/test_ha_automation_action_gate.py -q
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
    automation_action_domains,
)

# Fabricated. None of these exist in any real house.
AUTO_CAMERA = "automation.test_blind_the_hallway"
AUTO_LIGHT = "automation.test_porch_light_at_dusk"
AUTO_COVER_NESTED = "automation.test_nested_garage"
AUTO_YAML = "automation.test_yaml_defined"
AUTO_MISSING = "automation.test_not_in_editor_store"

CAMERA = "camera.test_hallway_cam"
COVER = "cover.test_garage_door"
LIGHT = "light.test_porch_light"


# ══════════════════════════════════════════════════════════════════════
#  Fabricated automation configs
# ══════════════════════════════════════════════════════════════════════
# Both spellings of every renamed key appear on purpose. HA 2024.8 renamed the
# block ``action:`` → ``actions:`` and the step key ``service:`` → ``action:``,
# and both spellings still load, so a classifier that knows only one of them has
# a hole shaped exactly like the one this file exists to close.

CONFIG_CAMERA_OLD = {
    "alias": "Blind the hallway",
    # old spellings throughout: block ``action``, step key ``service``
    "trigger": [{"platform": "state", "entity_id": "binary_sensor.test_dusk"}],
    "action": [{"service": "camera.turn_off",
                "target": {"entity_id": CAMERA}}],
}

CONFIG_LIGHT_NEW = {
    "alias": "Porch light at dusk",
    # new spellings throughout: block ``actions``, step key ``action``
    "triggers": [{"trigger": "sun", "event": "sunset"}],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": LIGHT}}],
}

CONFIG_LIGHT_TRIGGERED_BY_COVER = {
    "alias": "Light when the garage opens",
    # The *trigger* names a safety-critical domain; the actions do not. Firing
    # this actuates a light and nothing else, so it must not be gated.
    "trigger": [{"platform": "state", "entity_id": COVER, "to": "open"}],
    "condition": [{"condition": "state", "entity_id": COVER, "state": "open"}],
    "action": [{"service": "light.turn_on", "target": {"entity_id": LIGHT}}],
}

CONFIG_COVER_NESTED = {
    "alias": "Nested garage",
    "trigger": [],
    # cover is four levels down, inside choose → sequence → if → then.
    "actions": [
        {"choose": [
            {"conditions": [{"condition": "sun", "after": "sunset"}],
             "sequence": [
                 {"if": [{"condition": "state", "entity_id": "input_boolean.x",
                          "state": "on"}],
                  "then": [{"action": "cover.open_cover",
                            "target": {"entity_id": COVER}}],
                  "else": [{"action": "light.turn_off",
                            "target": {"entity_id": LIGHT}}]},
             ]},
        ],
         "default": [{"action": "light.turn_on",
                      "target": {"entity_id": LIGHT}}]},
    ],
}


def automation_state(auto_id, *, on: bool = True) -> dict:
    """An HA automation entity state. ``auto_id=None`` = YAML-defined, no id."""
    attributes: dict = {"friendly_name": "fabricated", "last_triggered": None,
                        "mode": "single"}
    if auto_id is not None:
        attributes["id"] = auto_id
    return {"state": "on" if on else "off", "attributes": attributes}


# ══════════════════════════════════════════════════════════════════════
#  A bridge that cannot reach a real house
# ══════════════════════════════════════════════════════════════════════

class AutomationTrap(HABridge):
    """A real :class:`HABridge` with the wire cut and a recorder in its place.

    Subclassing the real class is the point: the gate under test is the real
    gate, in the real methods. Only ``_request`` is replaced. It serves state
    reads and the automation editor API from fabricated dicts and records every
    call, so an ungated actuation shows up as an entry in ``wire`` rather than
    as something happening in a house.
    """

    def __init__(self, states=None, configs=None):
        # Skip HABridge.__init__: it reads config and builds a Session.
        self.wire: list = []
        self._states = dict(states or {})
        self._configs = dict(configs or {})
        self._enabled = True
        self._url = "http://ha.invalid"

        class _NoCache:
            def get(self, key): return None
            def set(self, key, value): pass
            def invalidate(self, key=None): pass

        # Only the *state* cache is neutered. The automation-config cache is
        # the real one, built lazily by the bridge, so the caching tests below
        # exercise the shipped object rather than a stand-in.
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
        if endpoint.startswith("config/automation/config/"):
            auto_id = endpoint.rsplit("/", 1)[1]
            if method == "GET":
                if auto_id not in self._configs:
                    # What HA does for an automation the editor store has never
                    # seen: 404, surfaced by the real _request as HABridgeError.
                    raise HABridgeError(
                        f"HA returned 404 for config/automation/config/{auto_id}")
                return _Resp(self._configs[auto_id])
            return _Resp({"result": "ok"})
        return _Resp([])

    # -- assertions ---------------------------------------------------
    @property
    def service_calls(self):
        """Only the calls that would actuate — POSTs to ``services/…``."""
        return [(e, m, d) for e, m, d in self.wire
                if e.startswith("services/") and m == "POST"]

    @property
    def writes(self):
        """Every POST of any kind. An actuation is always a POST."""
        return [(e, m, d) for e, m, d in self.wire if m == "POST"]

    def config_fetches(self, auto_id=None):
        return [e for e, m, _ in self.wire
                if m == "GET" and e.startswith("config/automation/config/")
                and (auto_id is None or e.endswith(f"/{auto_id}"))]


@pytest.fixture
def trap():
    """A bridge holding one automation of each interesting kind."""
    return AutomationTrap(
        states={
            AUTO_CAMERA: automation_state("1000000000001"),
            AUTO_LIGHT: automation_state("1000000000002"),
            AUTO_COVER_NESTED: automation_state("1000000000003"),
            AUTO_YAML: automation_state(None),            # no editor id
            AUTO_MISSING: automation_state("1000000000009"),  # id, no config
        },
        configs={
            "1000000000001": CONFIG_CAMERA_OLD,
            "1000000000002": CONFIG_LIGHT_NEW,
            "1000000000003": CONFIG_COVER_NESTED,
            # 1000000000009 deliberately absent → editor API 404s.
        },
    )


# ══════════════════════════════════════════════════════════════════════
#  1. The classifier: what would this automation actually do?
# ══════════════════════════════════════════════════════════════════════

class TestAutomationActionDomains:

    def test_old_block_and_old_service_key(self):
        domains, known, why = automation_action_domains(CONFIG_CAMERA_OLD)
        assert known, why
        assert "camera" in domains

    def test_new_block_and_new_action_key(self):
        domains, known, why = automation_action_domains(CONFIG_LIGHT_NEW)
        assert known, why
        assert domains == {"light"}

    def test_triggers_and_conditions_are_not_actuation(self):
        """An automation that *fires on* a cover but only turns a light on.

        Classifying on the whole config would gate this, and a gate that fires
        on benign automations gets clicked through — which is how a confirmation
        stops meaning anything.
        """
        domains, known, why = automation_action_domains(
            CONFIG_LIGHT_TRIGGERED_BY_COVER)
        assert known, why
        assert "cover" not in domains
        assert domains == {"light"}

    def test_nested_choose_if_then_else(self):
        domains, known, why = automation_action_domains(CONFIG_COVER_NESTED)
        assert known, why
        assert "cover" in domains, "cover hidden inside choose→sequence→if→then"

    @pytest.mark.parametrize("actions", [
        # repeat → sequence
        [{"repeat": {"count": 3,
                     "sequence": [{"action": "camera.turn_off"}]}}],
        # parallel
        [{"parallel": [{"service": "camera.turn_off"}]}],
        # a wrapper key nobody has taught this code about
        [{"some_future_block": {"steps": [{"action": "camera.turn_off"}]}}],
        # entity named only in the target, service named generically
        [{"action": "homeassistant.turn_off",
          "target": {"entity_id": CAMERA}}],
        # list of targets
        [{"service": "homeassistant.turn_off",
          "entity_id": [LIGHT, CAMERA]}],
    ])
    def test_camera_is_found_however_it_is_spelled(self, actions):
        domains, known, why = automation_action_domains({"actions": actions})
        assert known, why
        assert "camera" in domains

    def test_a_single_step_stored_as_a_bare_mapping(self):
        """HA stores a one-step automation's action as a mapping, not a list.

        Shape taken from a real editor-API response body (community.
        home-assistant.io/t/rest-api-docs-for-automations/119997), with the
        entity swapped for a fabricated camera.
        """
        domains, known, why = automation_action_domains({
            "alias": "Bedroom lights at dawn",
            "id": "22650126ff7c46059363912643842fd6",
            "trigger": [{"at": "06:15:00", "platform": "time"}],
            "action": {"service": "camera.turn_off",
                       "data": {"entity_id": CAMERA}},
        })
        assert known, why
        assert "camera" in domains

    def test_a_bare_string_action_block_in_dispatch_position(self):
        _domains, known, why = automation_action_domains(
            {"actions": "{{ whatever_it_decides }}"})
        assert not known
        assert "template" in why

    def test_missing_action_block_is_unknowable(self):
        domains, known, why = automation_action_domains({"alias": "x"})
        assert not known
        assert "no action block" in why

    @pytest.mark.parametrize("config", [None, [], "actions: []", 7])
    def test_a_non_config_is_unknowable(self, config):
        _domains, known, why = automation_action_domains(config)
        assert not known and why

    def test_templated_service_is_unknowable(self):
        """``action: "{{ x }}.turn_off"`` names no domain until it runs."""
        _domains, known, why = automation_action_domains(
            {"actions": [{"action": "{{ chosen_service }}"}]})
        assert not known
        assert "template" in why

    def test_a_template_in_message_data_is_not_dispatch(self):
        """Templates are everywhere in benign automations; only dispatch counts."""
        domains, known, why = automation_action_domains({"actions": [
            {"action": "notify.mobile_app",
             "data": {"message": "porch light is {{ states('light.x') }}"}}]})
        assert known, why
        assert "camera" not in domains

    def test_targeting_by_area_is_unknowable(self):
        """``target: {area_id: garage}`` reaches whatever is in the garage."""
        _domains, known, why = automation_action_domains({"actions": [
            {"action": "homeassistant.turn_off",
             "target": {"area_id": "garage"}}]})
        assert not known
        assert "area_id" in why

    def test_calling_another_stored_instruction_is_unknowable(self):
        """A script is one more level of deferral, not a leaf."""
        _domains, known, why = automation_action_domains({"actions": [
            {"action": "script.turn_on",
             "target": {"entity_id": "script.test_nightly"}}]})
        assert not known
        assert "stored instruction" in why


# ══════════════════════════════════════════════════════════════════════
#  2. trigger_automation: the hole itself
# ══════════════════════════════════════════════════════════════════════

class TestTriggerAutomation:

    def test_camera_automation_is_refused_and_nothing_is_actuated(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.trigger_automation(AUTO_CAMERA)

        assert trap.service_calls == [], "the automation fired anyway"
        assert trap.writes == [], "something was POSTed to Home Assistant"
        assert caught.value.proposal["domains"] == ["camera"]
        assert caught.value.proposal["entity_id"] == AUTO_CAMERA
        assert caught.value.proposal["service"] == "automation.trigger"

    def test_nested_cover_automation_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.trigger_automation(AUTO_COVER_NESTED)
        assert trap.writes == []
        assert "cover" in caught.value.proposal["domains"]

    def test_light_only_automation_still_fires(self, trap):
        result = trap.trigger_automation(AUTO_LIGHT)
        assert result["success"] is True
        assert trap.service_calls == [
            ("services/automation/trigger", "POST", {"entity_id": AUTO_LIGHT})]

    def test_automation_triggered_by_a_cover_still_fires(self):
        """Regression guard on over-matching, which costs consent its meaning."""
        trap = AutomationTrap(
            states={AUTO_LIGHT: automation_state("2000000000001")},
            configs={"2000000000001": CONFIG_LIGHT_TRIGGERED_BY_COVER})
        trap.trigger_automation(AUTO_LIGHT)
        assert len(trap.service_calls) == 1

    def test_unfetchable_config_is_refused_with_the_reason(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.trigger_automation(AUTO_MISSING)
        assert trap.writes == []
        assert "cannot inspect this automation's actions" in str(caught.value)

    def test_yaml_automation_without_an_editor_id_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.trigger_automation(AUTO_YAML)
        assert trap.writes == []
        assert "cannot inspect this automation's actions" in str(caught.value)
        assert trap.config_fetches() == [], (
            "with no id there is nothing to fetch; it must not guess one")

    def test_an_unknown_automation_is_refused_not_fired(self):
        """An entity that is not in HA at all reads back as ``{}``."""
        trap = AutomationTrap(states={}, configs={})
        with pytest.raises(ConfirmationRequired):
            trap.trigger_automation("automation.test_does_not_exist")
        assert trap.writes == []

    def test_a_bad_entity_id_makes_no_request_at_all(self, trap):
        with pytest.raises(HABridgeError):
            trap.trigger_automation("../../auth/providers")
        assert trap.wire == [], "validation must precede every read"

    def test_the_token_lets_it_through(self, trap):
        result = trap.trigger_automation(AUTO_CAMERA,
                                         human_confirmed=HUMAN_CONFIRMED)
        assert result["action"] == "triggered"
        assert trap.service_calls == [
            ("services/automation/trigger", "POST", {"entity_id": AUTO_CAMERA})]

    @pytest.mark.parametrize("spoof", [True, 1, "yes", "HUMAN_CONFIRMED",
                                       {"confirmed": True}])
    def test_a_value_that_is_not_the_token_is_not_a_confirmation(self, trap,
                                                                 spoof):
        with pytest.raises(ConfirmationRequired):
            trap.trigger_automation(AUTO_CAMERA, human_confirmed=spoof)
        assert trap.writes == []


# ══════════════════════════════════════════════════════════════════════
#  3. toggle_automation: disabling a safeguard is a security action
# ══════════════════════════════════════════════════════════════════════

class TestToggleAutomation:
    """Same rule as triggering: gate on the actions, both directions.

    Disabling the automation that closes the garage at night leaves no trace in
    any cover or camera state — nothing else in the system notices the safeguard
    is gone. Enabling one arms a stored instruction to actuate. Neither is
    gated for a light schedule, because a gate that fires on everything gets
    clicked through.
    """

    @pytest.mark.parametrize("enable", [True, False])
    def test_camera_automation_cannot_be_switched_either_way(self, trap,
                                                             enable):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.toggle_automation(AUTO_CAMERA, enable)
        assert trap.writes == []
        assert caught.value.proposal["service"] == (
            "automation.enable" if enable else "automation.disable")

    @pytest.mark.parametrize("enable", [True, False])
    def test_unfetchable_config_cannot_be_switched_either_way(self, trap,
                                                             enable):
        with pytest.raises(ConfirmationRequired):
            trap.toggle_automation(AUTO_MISSING, enable)
        assert trap.writes == []

    def test_light_automation_switches_freely(self, trap):
        trap.toggle_automation(AUTO_LIGHT, True)
        trap.toggle_automation(AUTO_LIGHT, False)
        assert trap.service_calls == [
            ("services/automation/turn_on", "POST", {"entity_id": AUTO_LIGHT}),
            ("services/automation/turn_off", "POST", {"entity_id": AUTO_LIGHT}),
        ]

    def test_enable_is_not_a_flip(self, trap):
        """``enable=True`` used to call ``automation.toggle``.

        Which *flips* — so asking to enable an already-enabled automation
        disabled it. For a safety automation that is the exact failure the gate
        exists to prevent, arriving through the argument that was supposed to
        prevent it.
        """
        trap.toggle_automation(AUTO_LIGHT, True)
        assert [e for e, _m, _d in trap.service_calls] == [
            "services/automation/turn_on"]

    def test_the_token_lets_a_disable_through(self, trap):
        trap.toggle_automation(AUTO_CAMERA, False,
                               human_confirmed=HUMAN_CONFIRMED)
        assert trap.service_calls == [
            ("services/automation/turn_off", "POST", {"entity_id": AUTO_CAMERA})]


# ══════════════════════════════════════════════════════════════════════
#  4. Caching content, never a verdict
# ══════════════════════════════════════════════════════════════════════

class TestConfigCache:

    def test_a_repeated_trigger_reuses_the_fetched_config(self, trap):
        trap.trigger_automation(AUTO_LIGHT)
        trap.trigger_automation(AUTO_LIGHT)
        assert len(trap.config_fetches("1000000000002")) == 1
        assert len(trap.service_calls) == 2

    def test_the_verdict_is_re_derived_every_time(self, trap):
        """A refusal is not memoised, and neither is an approval.

        The cache holds the *config*; the policy runs again on every call. A
        confirmed call therefore cannot leave anything behind that makes the
        next unconfirmed one succeed.
        """
        trap.trigger_automation(AUTO_CAMERA, human_confirmed=HUMAN_CONFIRMED)
        before = len(trap.service_calls)
        with pytest.raises(ConfirmationRequired):
            trap.trigger_automation(AUTO_CAMERA)
        assert len(trap.service_calls) == before, (
            "an earlier confirmation authorised a later unconfirmed call")

    def test_one_automations_config_is_not_served_for_another(self, trap):
        """The cache is keyed by editor id, so a benign read cannot mask a
        dangerous automation (or vice versa)."""
        trap.trigger_automation(AUTO_LIGHT)          # caches the benign config
        with pytest.raises(ConfirmationRequired) as caught:
            trap.trigger_automation(AUTO_CAMERA)     # must fetch its own
        assert caught.value.proposal["domains"] == ["camera"]
        assert trap.config_fetches("1000000000001")

    def test_a_failed_fetch_is_not_cached_as_success(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.trigger_automation(AUTO_MISSING)
        with pytest.raises(ConfirmationRequired):
            trap.trigger_automation(AUTO_MISSING)
        assert len(trap.config_fetches("1000000000009")) == 2


# ══════════════════════════════════════════════════════════════════════
#  5. The door next to the one being closed
# ══════════════════════════════════════════════════════════════════════

class TestEveryRouteToFiringIt:
    """Gating only ``trigger_automation`` would leave the generic paths open.

    ``call_service`` is the choke point every mutating helper goes through, so
    the automation expansion lives there too. Otherwise
    ``/ha/service/automation/trigger`` and ``/ha/control`` on an
    ``automation.*`` entity reach the identical actuation with the identical
    ``automation`` domain the gate was already waving through.
    """

    @pytest.mark.parametrize("domain,service", [
        ("automation", "trigger"),
        ("automation", "turn_on"),
        ("automation", "turn_off"),
        ("automation", "toggle"),
        ("homeassistant", "turn_on"),   # generic service, automation target
    ])
    def test_the_generic_passthrough_is_gated_too(self, trap, domain, service):
        with pytest.raises(ConfirmationRequired):
            trap.call_service(domain, service, entity_id=AUTO_CAMERA)
        assert trap.writes == []

    def test_control_device_on_an_automation_is_gated(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.control_device(AUTO_CAMERA, "turn_on")
        assert trap.writes == []

    def test_a_target_block_is_a_target(self, trap):
        """``target: {entity_id: …}`` presented no entity to the gate at all."""
        with pytest.raises(ConfirmationRequired):
            trap.call_service("automation", "trigger",
                              target={"entity_id": AUTO_CAMERA})
        assert trap.writes == []

    def test_a_camera_hidden_in_a_target_block_is_gated(self, trap):
        """Not automation-specific: the same blind spot hid a raw camera."""
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("homeassistant", "turn_off",
                              target={"entity_id": CAMERA})
        assert trap.writes == []
        assert caught.value.proposal["domains"] == ["camera"]

    def test_a_camera_in_service_data_entity_id_is_gated(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "turn_off",
                              **{"entity_id": CAMERA})
        assert trap.writes == []

    def test_one_bad_target_in_a_list_gates_the_whole_call(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("automation", "trigger",
                              entity_id=[AUTO_LIGHT, AUTO_CAMERA])
        assert trap.writes == []

    def test_an_ordinary_light_call_reads_nothing_extra(self, trap):
        """The expansion must not turn every service call into two reads."""
        trap.call_service("light", "turn_on", entity_id=LIGHT)
        assert trap.wire == [
            ("services/light/turn_on", "POST", {"entity_id": LIGHT})]

    def test_a_notify_message_naming_a_camera_is_not_an_actuation(self, trap):
        """Over-matching free text would make people route around the gate."""
        trap.call_service("notify", "mobile_app",
                          message=f"{CAMERA} went offline")
        assert len(trap.service_calls) == 1


# ══════════════════════════════════════════════════════════════════════
#  6. The HTTP layer: 409 + proposal, and the way to yes
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


def mint(client, headers, *, entity_id, action, kind="automation_run"):
    resp = client.post("/ha/confirm", headers=headers,
                       json={"kind": kind, "entity_id": entity_id,
                             "action": action})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


class TestRoutes:

    def test_trigger_is_refused_with_409_and_a_renderable_proposal(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/trigger",
                           headers=headers, json={})

        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["needs_confirmation"] is True
        assert body["confirm_endpoint"] == "/ha/confirm"
        # Same proposal shape the dashboard already renders for /ha/control.
        assert body["proposal"]["domains"] == ["camera"]
        assert body["proposal"]["entity_id"] == AUTO_CAMERA
        assert trap.writes == []

    def test_toggle_is_refused_with_409(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/toggle",
                           headers=headers, json={"enable": False})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_a_body_claiming_confirmation_does_not_confirm(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/trigger",
                           headers=headers,
                           json={"human_confirmed": True, "confirmed": True,
                                 "force": True})
        assert resp.status_code == 409
        assert trap.writes == []

    def test_a_light_automation_is_unaffected(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/automations/{AUTO_LIGHT}/trigger",
                           headers=headers, json={})
        assert resp.status_code == 200, resp.get_json()
        assert len(trap.service_calls) == 1

    def test_a_ticket_lets_the_trigger_through_once(self, wired):
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=AUTO_CAMERA,
                      action="trigger")["ticket"]

        first = client.post(f"/ha/automations/{AUTO_CAMERA}/trigger",
                            headers=headers, json={"ticket": ticket})
        assert first.status_code == 200, first.get_json()
        assert len(trap.service_calls) == 1

        second = client.post(f"/ha/automations/{AUTO_CAMERA}/trigger",
                             headers=headers, json={"ticket": ticket})
        assert second.status_code == 409, "the ticket was reusable"
        assert len(trap.service_calls) == 1

    def test_a_ticket_for_one_automation_does_not_fire_another(self, wired):
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=AUTO_LIGHT,
                      action="trigger")["ticket"]
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/trigger",
                           headers=headers, json={"ticket": ticket})
        assert resp.status_code == 409
        assert trap.writes == []

    def test_an_enable_ticket_does_not_authorise_a_disable(self, wired):
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=AUTO_CAMERA,
                      action="enable")["ticket"]
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/toggle",
                           headers=headers,
                           json={"enable": False, "ticket": ticket})
        assert resp.status_code == 409
        assert trap.writes == []

    def test_a_disable_ticket_authorises_that_disable(self, wired):
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=AUTO_CAMERA,
                      action="disable")["ticket"]
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/toggle",
                           headers=headers,
                           json={"enable": False, "ticket": ticket})
        assert resp.status_code == 200, resp.get_json()
        assert trap.service_calls == [
            ("services/automation/turn_off", "POST",
             {"entity_id": AUTO_CAMERA})]

    def test_a_trigger_ticket_does_not_authorise_a_toggle(self, wired):
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=AUTO_CAMERA,
                      action="trigger")["ticket"]
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/toggle",
                           headers=headers,
                           json={"enable": True, "ticket": ticket})
        assert resp.status_code == 409
        assert trap.writes == []

    def test_a_create_ticket_does_not_authorise_a_run(self, wired):
        """``automation`` (create, by alias) and ``automation_run`` (by entity
        id) are separate kinds precisely so this cannot redeem."""
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=AUTO_CAMERA, action="trigger",
                      kind="automation")["ticket"]
        resp = client.post(f"/ha/automations/{AUTO_CAMERA}/trigger",
                           headers=headers, json={"ticket": ticket})
        assert resp.status_code == 409
        assert trap.writes == []

    @pytest.mark.parametrize("enable", ["yes", "no", 1, 0, None, [], "maybe"])
    def test_a_non_boolean_enable_is_a_400_not_a_guess(self, wired, enable):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/automations/{AUTO_LIGHT}/toggle",
                           headers=headers, json={"enable": enable})
        assert resp.status_code == 400, resp.get_json()
        assert trap.writes == []

    @pytest.mark.parametrize("text,expected", [("true", "turn_on"),
                                               ("false", "turn_off")])
    def test_boolean_strings_are_accepted_unambiguously(self, wired, text,
                                                        expected):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/automations/{AUTO_LIGHT}/toggle",
                           headers=headers, json={"enable": text})
        assert resp.status_code == 200, resp.get_json()
        assert trap.service_calls[0][0] == f"services/automation/{expected}"

    def test_the_service_passthrough_cannot_fire_it_either(self, wired):
        """The bypass one door over: same actuation, generic route."""
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/automation/trigger", headers=headers,
                           json={"entity_id": AUTO_CAMERA})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_control_route_cannot_fire_it_either(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": AUTO_CAMERA,
                                 "action": "turn_on"})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_minting_does_not_touch_home_assistant(self, wired):
        client, headers, trap, _ = wired
        body = mint(client, headers, entity_id=AUTO_CAMERA, action="trigger")
        assert trap.wire == [], "minting a ticket read or wrote to HA"
        # The domain of an automation entity is never safety-critical — which is
        # the whole hole — so the endpoint must not claim to know.
        assert body["proposal"]["safety_critical"] is None
        assert "fails closed" in body["proposal"]["note"]
