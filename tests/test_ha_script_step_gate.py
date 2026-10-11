"""Running a script is gated on what the script's *steps* do.

The hole this file closes. An automation is classified by what its actions
would do, at the ``call_service`` choke point. ``script.*`` is the identical
problem one door over:

* :func:`~puffpai.ha_bridge.scene_member_domains` expands *scene* members only.
  Handed ``script.x`` it returns early with ``known=True`` (a script has no
  member list, so there is nothing to expand) and the caller reads that as
  "determined, and harmless", because ``script`` is not a safety-critical
  domain.
* So without step classification ``POST /ha/scenes/script.x/activate``,
  ``POST /ha/control {"entity_id": "script.x"}``,
  ``POST /ha/service/script/turn_on {"entity_id": "script.x"}`` and the
  Steward's ``activate_scene`` would all fire a script whose sequence could
  contain ``camera.turn_off``, unchecked.
* And a fifth door: HA registers **one service per script**, so
  ``POST /ha/service/script/<object_id>`` runs the script while naming no entity
  at all. That call's domains are ``{"script"}``, which is never critical.

Deferred actuation is still actuation. A script that turns the hallway camera
off turns the hallway camera off; the instruction was simply written earlier.

The gate resolves the script's stored steps from ``config/script/config/<key>``
— keyed by the script's **object_id**, since a script has no ``id`` attribute
the way an automation does — and applies the identical policy to the domains
those steps reach. It **fails closed** when the steps cannot be read, because "I
could not find out what this would do" is not "it does nothing dangerous".

The worked examples are ``camera`` (the household's own witness) and ``cover``
(a garage door). **Every entity id, object id and config in this file is
fabricated, and nothing here may reach a real Home Assistant.** The bridge is a
real ``HABridge`` subclass whose ``_request`` — the only method that would open
a socket — is replaced by a recorder, so "the gate leaked" and "the test touched
a house" are the same observable event: an entry in ``bridge.wire``.

Run: pytest tests/test_ha_script_step_gate.py -q
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
    STORED_CONFIG_TTL_SECONDS,
    ConfirmationRequired,
    HABridge,
    HABridgeError,
    script_sequence_domains,
    stored_instruction_domains,
)

# Fabricated. None of these exist in any real house.
SCRIPT_CAMERA = "script.test_blind_the_hallway"
SCRIPT_LIGHT = "script.test_porch_light_on"
SCRIPT_COVER_NESTED = "script.test_nested_garage"
SCRIPT_YAML = "script.test_inline_yaml"          # editor store 404s
SCRIPT_CALLS_AUTOMATION = "script.test_calls_an_automation"
SCRIPT_TEMPLATED = "script.test_templated_dispatch"
SCRIPT_AREA = "script.test_targets_an_area"
SCRIPT_ACTIONS_KEY = "script.test_actions_spelling"

AUTO_CAMERA = "automation.test_blind_the_hallway"

CAMERA = "camera.test_hallway_cam"
COVER = "cover.test_garage_door"
LIGHT = "light.test_porch_light"


def key(entity_id: str) -> str:
    """The script editor key: the object_id, i.e. everything after the dot."""
    return entity_id.split(".", 1)[1]


# ══════════════════════════════════════════════════════════════════════
#  Fabricated script configs
# ══════════════════════════════════════════════════════════════════════
# Both spellings of every renamed key appear on purpose. A script's steps live
# under ``sequence:``; HA 2024.10 also accepts ``actions:``. The step key was
# renamed ``service:`` → ``action:`` in 2024.8. Both spellings still load, so a
# classifier that knows only one of them has a hole shaped exactly like the one
# this file exists to close.

CONFIG_CAMERA = {
    "alias": "Blind the hallway",
    "mode": "single",
    "sequence": [{"service": "camera.turn_off",
                  "target": {"entity_id": CAMERA}}],
}

CONFIG_LIGHT = {
    "alias": "Porch light on",
    "sequence": [{"action": "light.turn_on", "target": {"entity_id": LIGHT}}],
}

CONFIG_ACTIONS_KEY = {
    "alias": "Camera off, new block spelling",
    # ``actions:`` instead of ``sequence:`` — the 2024.10 spelling.
    "actions": [{"action": "camera.turn_off", "entity_id": CAMERA}],
}

CONFIG_COVER_NESTED = {
    "alias": "Nested garage",
    # cover is four levels down: choose → sequence → if → then.
    "sequence": [
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
         "default": [{"repeat": {"count": 2, "sequence": [
             {"parallel": [{"action": "light.turn_on",
                            "target": {"entity_id": LIGHT}}]}]}}]},
    ],
}

CONFIG_CALLS_AUTOMATION = {
    "alias": "Calls an automation",
    "sequence": [{"action": "automation.trigger",
                  "target": {"entity_id": AUTO_CAMERA}}],
}

CONFIG_TEMPLATED = {
    "alias": "Templated dispatch",
    "sequence": [{"action": "{{ chosen_service }}",
                  "target": {"entity_id": LIGHT}}],
}

CONFIG_AREA = {
    "alias": "Targets an area",
    "sequence": [{"action": "light.turn_off", "target": {"area_id": "garage"}}],
}


# ══════════════════════════════════════════════════════════════════════
#  A bridge that cannot reach a real house
# ══════════════════════════════════════════════════════════════════════

class ScriptTrap(HABridge):
    """A real :class:`HABridge` with the wire cut and a recorder in its place.

    Subclassing the real class is the point: the gate under test is the real
    gate, in the real methods. Only ``_request`` is replaced. It serves state
    reads and the script/automation editor APIs from fabricated dicts and
    records every call, so an ungated actuation shows up as an entry in ``wire``
    rather than as something happening in a house.
    """

    def __init__(self, states=None, scripts=None, automations=None):
        # Skip HABridge.__init__: it reads config and builds a Session.
        self.wire: list = []
        self._states = dict(states or {})
        self._scripts = dict(scripts or {})
        self._automations = dict(automations or {})
        self._enabled = True
        self._url = "http://ha.invalid"

        class _NoCache:
            def get(self, key): return None
            def set(self, key, value): pass
            def invalidate(self, key=None): pass

        # Only the *state* cache is neutered. The stored-config cache is the
        # real one, built lazily by the bridge, so the caching tests below
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
        if endpoint.startswith("config/script/config/"):
            object_id = endpoint.rsplit("/", 1)[1]
            if method == "GET":
                if object_id not in self._scripts:
                    # What HA does for a script the editor store has never seen
                    # (defined inline in configuration.yaml, or absent): 404,
                    # surfaced by the real _request as HABridgeError.
                    raise HABridgeError(
                        f"HA returned HTTP 404: config/script/config/{object_id}")
                return _Resp(self._scripts[object_id])
            return _Resp({"result": "ok"})
        if endpoint.startswith("config/automation/config/"):
            auto_id = endpoint.rsplit("/", 1)[1]
            if method == "GET":
                if auto_id not in self._automations:
                    raise HABridgeError(
                        f"HA returned HTTP 404: config/automation/config/{auto_id}")
                return _Resp(self._automations[auto_id])
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

    def config_fetches(self, object_id=None):
        return [e for e, m, _ in self.wire
                if m == "GET" and e.startswith("config/script/config/")
                and (object_id is None or e.endswith(f"/{object_id}"))]


def script_state(*, on: bool = False) -> dict:
    """An HA script entity state. Note: no ``id`` attribute — scripts have none.

    That absence is exactly why the fetch is keyed on the object_id instead.
    """
    return {"state": "on" if on else "off",
            "attributes": {"friendly_name": "fabricated", "mode": "single",
                           "last_triggered": None, "current": 0}}


@pytest.fixture
def trap():
    """A bridge holding one script of each interesting kind."""
    return ScriptTrap(
        states={
            SCRIPT_CAMERA: script_state(),
            SCRIPT_LIGHT: script_state(),
            SCRIPT_COVER_NESTED: script_state(),
            SCRIPT_YAML: script_state(),
            SCRIPT_CALLS_AUTOMATION: script_state(),
            SCRIPT_TEMPLATED: script_state(),
            SCRIPT_AREA: script_state(),
            SCRIPT_ACTIONS_KEY: script_state(),
        },
        scripts={
            key(SCRIPT_CAMERA): CONFIG_CAMERA,
            key(SCRIPT_LIGHT): CONFIG_LIGHT,
            key(SCRIPT_COVER_NESTED): CONFIG_COVER_NESTED,
            key(SCRIPT_CALLS_AUTOMATION): CONFIG_CALLS_AUTOMATION,
            key(SCRIPT_TEMPLATED): CONFIG_TEMPLATED,
            key(SCRIPT_AREA): CONFIG_AREA,
            key(SCRIPT_ACTIONS_KEY): CONFIG_ACTIONS_KEY,
            # key(SCRIPT_YAML) deliberately absent → editor API 404s.
        },
    )


# ══════════════════════════════════════════════════════════════════════
#  1. The classifier: what would this script actually do?
# ══════════════════════════════════════════════════════════════════════

class TestScriptSequenceDomains:

    def test_sequence_block_with_old_service_key(self):
        domains, known, why = script_sequence_domains(CONFIG_CAMERA)
        assert known, why
        assert "camera" in domains

    def test_actions_block_with_new_action_key(self):
        """Both renamed spellings at once: ``actions:`` block, ``action:`` step."""
        domains, known, why = script_sequence_domains(CONFIG_ACTIONS_KEY)
        assert known, why
        assert "camera" in domains

    def test_a_light_only_script_is_benign(self):
        domains, known, why = script_sequence_domains(CONFIG_LIGHT)
        assert known, why
        assert domains == {"light"}

    def test_nested_choose_if_repeat_parallel(self):
        """cover is four wrappers deep; the walk must still find it."""
        domains, known, why = script_sequence_domains(CONFIG_COVER_NESTED)
        assert known, why
        assert "cover" in domains

    def test_a_missing_sequence_is_not_knowable(self):
        domains, known, why = script_sequence_domains({"alias": "empty"})
        assert not known
        assert "no sequence block" in why

    def test_a_non_mapping_config_is_not_knowable(self):
        for junk in ([], "sequence", 7, None):
            domains, known, why = script_sequence_domains(junk)
            assert not known, junk
            assert "not a mapping" in why

    def test_a_scalar_sequence_is_not_knowable(self):
        domains, known, why = script_sequence_domains({"sequence": 5})
        assert not known
        assert "not a sequence" in why

    def test_a_templated_dispatch_is_not_knowable(self):
        domains, known, why = script_sequence_domains(CONFIG_TEMPLATED)
        assert not known
        assert "template" in why

    def test_an_area_target_is_not_knowable(self):
        domains, known, why = script_sequence_domains(CONFIG_AREA)
        assert not known
        assert "area_id" in why

    @pytest.mark.parametrize("selector", ["device_id", "floor_id", "label_id"])
    def test_every_opaque_selector_is_not_knowable(self, selector):
        domains, known, why = script_sequence_domains(
            {"sequence": [{"action": "light.turn_off",
                           "target": {selector: "whatever"}}]})
        assert not known
        assert selector in why

    def test_calling_another_stored_instruction_is_not_knowable(self):
        """The chained-deferral decision, asserted as behaviour.

        A script that calls an automation is **refused**, not expanded. See the
        module note on ``_OPAQUE_CALLEE_DOMAINS``: a step may pass ``variables:``
        into the callee, so one-level expansion could return a confident
        ``known=True`` that is wrong — and a wrong "known" is a silent bypass
        while a refusal is only a prompt.
        """
        domains, known, why = script_sequence_domains(CONFIG_CALLS_AUTOMATION)
        assert not known
        assert AUTO_CAMERA in why, "the refusal must name the callee"
        assert "inspect" in why, "the refusal must say what to do about it"

    def test_a_script_calling_a_script_is_also_refused(self):
        domains, known, why = script_sequence_domains(
            {"sequence": [{"action": "script.turn_on",
                           "target": {"entity_id": "script.test_other"}}]})
        assert not known
        assert "script.test_other" in why


# ══════════════════════════════════════════════════════════════════════
#  2. Fetching the config — and failing closed when it cannot be fetched
# ══════════════════════════════════════════════════════════════════════

class TestFetchScriptConfig:

    def test_the_editor_key_is_the_object_id_not_an_id_attribute(self, trap):
        """A script has no ``id``; HA keys scripts.yaml by object_id."""
        config, why = trap._fetch_script_config(SCRIPT_CAMERA)
        assert config == CONFIG_CAMERA, why
        assert trap.config_fetches() == [
            f"config/script/config/{key(SCRIPT_CAMERA)}"]
        # And it did NOT need to read the entity's state to find a key.
        assert not [e for e, _, _ in trap.wire if e.startswith("states")]

    def test_a_script_absent_from_the_editor_store_is_unreadable(self, trap):
        config, why = trap._fetch_script_config(SCRIPT_YAML)
        assert config is None
        assert "editor API did not return it" in why

    def test_a_non_dict_response_is_unreadable(self):
        trap = ScriptTrap(scripts={key(SCRIPT_CAMERA): ["not", "a", "dict"]})
        config, why = trap._fetch_script_config(SCRIPT_CAMERA)
        assert config is None
        assert "no config object" in why

    def test_a_malformed_entity_id_is_never_fetched(self, trap):
        for junk in ("script", "script.", "SCRIPT.Upper", "script.a b",
                     "script.../../secrets", 5, None):
            config, why = trap._fetch_script_config(junk)
            assert config is None, junk
            assert why
        assert trap.wire == [], "a malformed id must not become a request"

    def test_domains_report_the_unreadable_case_with_the_named_reason(self, trap):
        domains, known, detail = trap.script_actuation_domains(SCRIPT_YAML)
        assert not known
        assert "cannot inspect this script's steps" in detail


# ══════════════════════════════════════════════════════════════════════
#  3. The four doors. All of them, zero HA calls.
# ══════════════════════════════════════════════════════════════════════

class TestDoorOne_ActivateScene:
    """``HABridge.activate_scene`` — the route ``/ha/scenes/<id>/activate`` uses.

    This is the door ``scene_member_domains`` left open: handed a script it
    returned ``(domains, True)`` and the caller read that as "determined".
    """

    def test_a_camera_script_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.activate_scene(SCRIPT_CAMERA)
        assert trap.writes == []
        assert caught.value.proposal["domains"] == ["camera"]
        assert caught.value.proposal["entity_id"] == SCRIPT_CAMERA

    def test_a_light_script_is_allowed_unchanged(self, trap):
        result = trap.activate_scene(SCRIPT_LIGHT)
        assert result["success"] is True
        assert trap.service_calls == [
            ("services/script/turn_on", "POST", {"entity_id": SCRIPT_LIGHT})]

    def test_an_unreadable_script_is_refused_with_the_reason(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            trap.activate_scene(SCRIPT_YAML)
        assert trap.writes == []
        assert "cannot inspect this script's steps" in str(caught.value)

    def test_a_confirmed_camera_script_goes_through(self, trap):
        trap.activate_scene(SCRIPT_CAMERA, human_confirmed=HUMAN_CONFIRMED)
        assert len(trap.service_calls) == 1


class TestDoorTwo_ControlDevice:
    """``HABridge.control_device`` — the route ``/ha/control`` uses."""

    @pytest.mark.parametrize("action", ["turn_on", "toggle"])
    def test_a_camera_script_is_refused(self, trap, action):
        with pytest.raises(ConfirmationRequired):
            trap.control_device(SCRIPT_CAMERA, action)
        assert trap.writes == []

    def test_a_light_script_is_allowed_unchanged(self, trap):
        trap.control_device(SCRIPT_LIGHT, "turn_on")
        assert len(trap.service_calls) == 1

    def test_a_confirmed_camera_script_goes_through(self, trap):
        trap.control_device(SCRIPT_CAMERA, "turn_on",
                            human_confirmed=HUMAN_CONFIRMED)
        assert len(trap.service_calls) == 1


class TestDoorThree_CallService:
    """``HABridge.call_service`` — the route ``/ha/service/<d>/<s>`` uses."""

    def test_script_turn_on_by_entity_id_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("script", "turn_on", entity_id=SCRIPT_CAMERA)
        assert trap.writes == []

    def test_the_per_script_service_form_is_refused(self, trap):
        """``services/script/<object_id>`` names no entity at all.

        HA registers a service per script. Without reconstructing the entity
        from the service name this call carries the domains ``{"script"}`` —
        never critical — and the whole gate is one URL away from irrelevant.
        """
        with pytest.raises(ConfirmationRequired) as caught:
            trap.call_service("script", key(SCRIPT_CAMERA))
        assert trap.writes == []
        assert caught.value.proposal["domains"] == ["camera"]

    def test_the_per_script_service_form_for_a_light_script_is_allowed(self, trap):
        trap.call_service("script", key(SCRIPT_LIGHT))
        assert len(trap.service_calls) == 1

    def test_a_script_hidden_in_a_target_block_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("script", "turn_on",
                              target={"entity_id": SCRIPT_CAMERA})
        assert trap.writes == []

    def test_a_script_hidden_in_service_data_entity_id_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "turn_on",
                              **{"entity_id": SCRIPT_CAMERA})
        assert trap.writes == []

    def test_homeassistant_turn_on_of_a_script_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("homeassistant", "turn_on",
                              entity_id=SCRIPT_CAMERA)
        assert trap.writes == []

    def test_one_bad_script_in_a_list_gates_the_whole_call(self, trap):
        with pytest.raises(ConfirmationRequired):
            trap.call_service("script", "turn_on",
                              entity_id=[SCRIPT_LIGHT, SCRIPT_CAMERA])
        assert trap.writes == []

    def test_script_reload_is_not_read_as_a_script_named_reload(self, trap):
        """``script.reload`` is a generic service, not an object_id."""
        trap.call_service("script", "reload")
        assert trap.config_fetches() == []
        assert len(trap.service_calls) == 1

    def test_an_ordinary_light_call_reads_nothing_extra(self, trap):
        """The expansion must not turn every service call into two reads."""
        trap.call_service("light", "turn_on", entity_id=LIGHT)
        assert trap.wire == [
            ("services/light/turn_on", "POST", {"entity_id": LIGHT})]

    def test_a_notify_message_naming_a_script_is_not_an_actuation(self, trap):
        trap.call_service("notify", "mobile_app",
                          message=f"{SCRIPT_CAMERA} finished")
        assert len(trap.service_calls) == 1
        assert trap.config_fetches() == []


class TestDoorFour_StewardActivateScene:
    """The Steward's ``_GuardedHA`` — a *different* gate over the same bridge.

    It applies the shared policy itself rather than relying on the object
    underneath, because the bridge is injectable.
    """

    @staticmethod
    def _guard(bridge, *, confirmed=False, origin="concierge"):
        from puffpai.agents.envelope import Envelope
        from puffpai.agents.steward import _GuardedHA
        env = Envelope(intent="activate_scene",
                       payload={"entity_id": SCRIPT_CAMERA,
                                "confirmed": confirmed},
                       requester="owner", origin_agent=origin,
                       state_changing=True)
        return _GuardedHA(bridge, env)

    def test_a_camera_script_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            self._guard(trap).activate_scene(SCRIPT_CAMERA)
        assert trap.writes == []

    def test_a_light_script_is_allowed(self, trap):
        self._guard(trap).activate_scene(SCRIPT_LIGHT)
        assert len(trap.service_calls) == 1

    def test_an_unreadable_script_is_refused_with_the_reason(self, trap):
        with pytest.raises(ConfirmationRequired) as caught:
            self._guard(trap).activate_scene(SCRIPT_YAML)
        assert trap.writes == []
        assert "cannot inspect this script's steps" in str(caught.value)

    def test_control_device_on_a_camera_script_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            self._guard(trap).control_device(SCRIPT_CAMERA, "turn_on")
        assert trap.writes == []

    def test_call_service_on_the_per_script_service_form_is_refused(self, trap):
        with pytest.raises(ConfirmationRequired):
            self._guard(trap).call_service("script", key(SCRIPT_CAMERA))
        assert trap.writes == []

    def test_a_bridge_that_cannot_resolve_steps_fails_closed(self):
        """The whole reason ``_GuardedHA`` re-applies the policy itself.

        An injected double with no resolver must not be able to remove the gate
        by simply not having one.
        """
        class _Double:
            def __init__(self):
                self.calls = []

            def activate_scene(self, entity_id, **kw):
                self.calls.append(entity_id)
                return {"success": True}

        double = _Double()
        with pytest.raises(ConfirmationRequired) as caught:
            self._guard(double).activate_scene(SCRIPT_CAMERA)
        assert double.calls == []
        assert "cannot inspect this script's steps" in str(caught.value)


# ══════════════════════════════════════════════════════════════════════
#  4. _GuardedHA shadows every actuator, including the two that were missed
# ══════════════════════════════════════════════════════════════════════

class TestGuardedHAShadowsEveryActuator:
    """``trigger_automation`` / ``toggle_automation`` were reaching ``__getattr__``.

    With the real bridge underneath they are gated anyway — the bridge gates
    them itself. But a method left to ``__getattr__`` is a method whose gate
    exists only if the object underneath happens to implement one, and the
    bridge is injectable.
    """

    @staticmethod
    def _guard(bridge):
        from puffpai.agents.envelope import Envelope
        from puffpai.agents.steward import _GuardedHA
        return _GuardedHA(bridge, Envelope(intent="control_device",
                                           payload={}, requester="owner",
                                           origin_agent="concierge",
                                           state_changing=True))

    @pytest.mark.parametrize("name", [
        "call_service", "control_device", "activate_scene",
        "trigger_automation", "toggle_automation",
    ])
    def test_the_method_is_defined_on_the_proxy_not_delegated(self, name):
        from puffpai.agents.steward import _GuardedHA
        assert name in vars(_GuardedHA), (
            f"{name} falls through to __getattr__, so its gate is whatever the "
            f"injected bridge happens to have")

    class _Double:
        """No gate of its own, and no way to resolve stored steps."""

        def __init__(self):
            self.calls = []

        def trigger_automation(self, entity_id, **kw):
            self.calls.append(("trigger", entity_id))
            return {"success": True}

        def toggle_automation(self, entity_id, enable, **kw):
            self.calls.append(("toggle", entity_id, enable))
            return {"success": True}

    def test_trigger_automation_on_a_double_fails_closed(self):
        double = self._Double()
        with pytest.raises(ConfirmationRequired):
            self._guard(double).trigger_automation(AUTO_CAMERA)
        assert double.calls == []

    @pytest.mark.parametrize("enable", [True, False])
    def test_toggle_automation_on_a_double_fails_closed(self, enable):
        double = self._Double()
        with pytest.raises(ConfirmationRequired):
            self._guard(double).toggle_automation(AUTO_CAMERA, enable)
        assert double.calls == []

    def test_a_benign_automation_still_passes_through_the_real_bridge(self):
        trap = ScriptTrap(
            states={"automation.test_light": {
                "state": "on",
                "attributes": {"id": "1000000000002"}}},
            automations={"1000000000002": {
                "alias": "light only",
                "actions": [{"action": "light.turn_on",
                             "target": {"entity_id": LIGHT}}]}},
        )
        self._guard(trap).trigger_automation("automation.test_light")
        assert len(trap.service_calls) == 1


# ══════════════════════════════════════════════════════════════════════
#  5. The cache holds config, never a decision
# ══════════════════════════════════════════════════════════════════════

class TestCache:

    def test_the_ttl_is_short_and_shared_with_automations(self):
        assert STORED_CONFIG_TTL_SECONDS == 15.0

    def test_a_repeat_refusal_does_not_refetch(self, trap):
        for _ in range(3):
            with pytest.raises(ConfirmationRequired):
                trap.activate_scene(SCRIPT_CAMERA)
        assert len(trap.config_fetches(key(SCRIPT_CAMERA))) == 1

    def test_a_confirmation_is_never_replayed_from_the_cache(self, trap):
        """The rule: cache the config, never the verdict."""
        trap.activate_scene(SCRIPT_CAMERA, human_confirmed=HUMAN_CONFIRMED)
        assert len(trap.service_calls) == 1
        # Same script, same warm cache, no token this time.
        with pytest.raises(ConfirmationRequired):
            trap.activate_scene(SCRIPT_CAMERA)
        assert len(trap.service_calls) == 1, "a verdict was replayed"

    def test_one_script_never_borrows_another_verdict(self, trap):
        trap.activate_scene(SCRIPT_LIGHT)
        assert len(trap.service_calls) == 1
        with pytest.raises(ConfirmationRequired):
            trap.activate_scene(SCRIPT_CAMERA)
        assert len(trap.service_calls) == 1

    def test_a_script_and_an_automation_sharing_a_key_do_not_collide(self):
        """Cache keys are namespaced by kind.

        A script object_id and an automation editor id are different namespaces
        in HA and can be the same string. An unnamespaced cache would hand one
        entity the other's steps — and "light only" is exactly the config an
        attacker would want a camera script to be classified with.
        """
        shared = "night_mode"
        trap = ScriptTrap(
            states={f"script.{shared}": script_state(),
                    f"automation.{shared}": {
                        "state": "on", "attributes": {"id": shared}}},
            scripts={shared: CONFIG_CAMERA},
            automations={shared: {"alias": "light only",
                                  "actions": [{"action": "light.turn_on"}]}},
        )
        # Warm the cache with the benign automation first.
        trap.trigger_automation(f"automation.{shared}")
        assert len(trap.service_calls) == 1
        # The script with the same key must still be classified as a camera.
        with pytest.raises(ConfirmationRequired) as caught:
            trap.activate_scene(f"script.{shared}")
        assert caught.value.proposal["domains"] == ["camera"]


# ══════════════════════════════════════════════════════════════════════
#  6. The dispatcher
# ══════════════════════════════════════════════════════════════════════

class TestStoredInstructionDomains:

    def test_a_plain_actuator_is_classified_as_itself(self, trap):
        domains, known, why = stored_instruction_domains(trap, CAMERA)
        assert known and domains == {"camera"}
        assert trap.wire == [], "a direct actuator needs no lookup"

    def test_a_reader_without_a_resolver_fails_closed(self):
        domains, known, why = stored_instruction_domains(object(), SCRIPT_CAMERA)
        assert not known
        assert "cannot inspect this script's steps" in why

    def test_a_malformed_script_id_fails_closed_without_a_request(self, trap):
        domains, known, why = stored_instruction_domains(trap, "script.Bad Id")
        assert not known
        assert trap.wire == []


# ══════════════════════════════════════════════════════════════════════
#  7. The HTTP layer: 409 + the existing proposal shape, and the way to yes
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


def mint(client, headers, *, entity_id, action, kind):
    resp = client.post("/ha/confirm", headers=headers,
                       json={"kind": kind, "entity_id": entity_id,
                             "action": action})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


class TestRoutes:

    def test_activate_is_refused_with_409_and_the_existing_shape(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/scenes/{SCRIPT_CAMERA}/activate",
                           headers=headers, json={})
        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["needs_confirmation"] is True
        assert body["confirm_endpoint"] == "/ha/confirm"
        # No new envelope: the same proposal{service, entity_id, domains} the
        # dashboard already renders for /ha/control.
        assert set(body["proposal"]) >= {"service", "entity_id", "domains"}
        assert body["proposal"]["domains"] == ["camera"]
        assert trap.writes == []

    def test_control_is_refused_with_409(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": SCRIPT_CAMERA,
                                 "action": "turn_on"})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_service_turn_on_is_refused_with_409(self, wired):
        client, headers, trap, _ = wired
        resp = client.post("/ha/service/script/turn_on", headers=headers,
                           json={"entity_id": SCRIPT_CAMERA})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_the_per_script_service_route_is_refused_with_409(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/service/script/{key(SCRIPT_CAMERA)}",
                           headers=headers, json={})
        assert resp.status_code == 409, resp.get_json()
        assert trap.writes == []

    def test_a_body_claiming_confirmation_does_not_confirm(self, wired):
        client, headers, trap, _ = wired
        for route, payload in (
            (f"/ha/scenes/{SCRIPT_CAMERA}/activate", {}),
            ("/ha/control", {"entity_id": SCRIPT_CAMERA, "action": "turn_on"}),
        ):
            resp = client.post(route, headers=headers,
                               json={**payload, "human_confirmed": True,
                                     "confirmed": True, "force": True})
            assert resp.status_code == 409, route
        assert trap.writes == []

    def test_an_unreadable_script_is_refused_with_the_reason(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/scenes/{SCRIPT_YAML}/activate",
                           headers=headers, json={})
        assert resp.status_code == 409
        assert "cannot inspect this script's steps" in resp.get_json()["message"]
        assert trap.writes == []

    def test_a_light_script_is_unaffected(self, wired):
        client, headers, trap, _ = wired
        resp = client.post(f"/ha/scenes/{SCRIPT_LIGHT}/activate",
                           headers=headers, json={})
        assert resp.status_code == 200, resp.get_json()
        assert len(trap.service_calls) == 1

    def test_a_scene_kind_ticket_lets_the_activation_through_once(self, wired):
        """No new ticket kind: ``scene`` already binds this route."""
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=SCRIPT_CAMERA,
                      action="activate", kind="scene")["ticket"]

        first = client.post(f"/ha/scenes/{SCRIPT_CAMERA}/activate",
                            headers=headers, json={"ticket": ticket})
        assert first.status_code == 200, first.get_json()
        assert len(trap.service_calls) == 1

        second = client.post(f"/ha/scenes/{SCRIPT_CAMERA}/activate",
                             headers=headers, json={"ticket": ticket})
        assert second.status_code == 409, "the ticket was reusable"
        assert len(trap.service_calls) == 1

    def test_a_ticket_for_one_script_does_not_run_another(self, wired):
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=SCRIPT_LIGHT,
                      action="activate", kind="scene")["ticket"]
        resp = client.post(f"/ha/scenes/{SCRIPT_CAMERA}/activate",
                           headers=headers, json={"ticket": ticket})
        assert resp.status_code == 409
        assert trap.writes == []

    def test_a_control_ticket_lets_the_control_route_through(self, wired):
        client, headers, trap, _ = wired
        ticket = mint(client, headers, entity_id=SCRIPT_CAMERA,
                      action="turn_on", kind="control")["ticket"]
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": SCRIPT_CAMERA,
                                 "action": "turn_on", "ticket": ticket})
        assert resp.status_code == 200, resp.get_json()
        assert len(trap.service_calls) == 1

    def test_the_mint_proposal_is_honest_about_scripts(self, wired):
        """Minting must not claim a script is harmless because it is a script."""
        client, headers, _, _ = wired
        body = mint(client, headers, entity_id=SCRIPT_CAMERA,
                    action="activate", kind="scene")
        proposal = body["proposal"]
        assert proposal["safety_critical"] is None
        assert "steps" in proposal["note"]

    def test_minting_never_touches_home_assistant(self, wired):
        client, headers, trap, _ = wired
        mint(client, headers, entity_id=SCRIPT_CAMERA, action="activate",
             kind="scene")
        assert trap.wire == []
