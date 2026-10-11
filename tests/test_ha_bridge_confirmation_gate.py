"""The always-confirm gate as a property of the *bridge*, not of its callers.

Enforcing the always-confirm class in the Steward's ``_GuardedHA`` protects
only the callers that come through the Steward. Three do not:

* ``assistant_tools.ha_control_device`` — an LLM tool on the legacy PuffPAIAgent,
  which chat still falls back to whenever the product team does not handle a
  turn. Its argument is a string the model wrote, so a prompt injection in
  anything the model reads could reach ``camera.<x>|turn_off``.
* ``assistant_tools.ha_run_scene`` — same surface, via scene membership.
* ``/ha/control``, ``/ha/service/<domain>/<service>`` and ``/ha/scene`` — HTTP
  routes calling the bridge directly.

Enforcement therefore lives in :mod:`puffpai.ha_bridge` itself, so there is one
enforcement point instead of one per caller. These tests pin that, and pin the
two properties the gate has to keep: it must not fire on ordinary lights and
switches, and it must not touch reads.

The working example throughout is ``camera.turn_off`` — disabling the
household's own witness — with ``cover`` (garage doors and gates) as the
physical-security case. Every entity id here is fabricated. **Nothing in this
file may reach a real Home Assistant instance**: the bridge is either a
recording stub or a real :class:`HABridge` whose ``_request`` is monkeypatched
to fail loudly, so an ungated call shows up as an exploded test rather than as
something happening in a real house.

Run: pytest tests/test_ha_bridge_confirmation_gate.py -q
"""

from __future__ import annotations

import contextlib
import pathlib
import sys

import pytest

from puffpai.ha_bridge import (
    HUMAN_CONFIRMED,
    SAFETY_CRITICAL_DOMAINS,
    ConfirmationRequired,
    HABridge,
    HABridgeError,
    enforce_confirmation,
    is_human_confirmation,
    scene_member_domains,
    touched_domains,
)

# Fabricated entities. None of these exist in any real house.
CAMERA = "camera.test_hallway_cam"
COVER = "cover.test_garage_door"
LIGHT = "light.test_porch_light"
SWITCH = "switch.test_desk_lamp"
SCENE_PLAIN = "scene.test_movie_night"
SCENE_CRITICAL = "scene.test_lockdown"


# ══════════════════════════════════════════════════════════════════════
#  A bridge that cannot reach a real house
# ══════════════════════════════════════════════════════════════════════

class WireTrap(HABridge):
    """A real :class:`HABridge` with the wire cut and a recorder in its place.

    Subclassing the real class rather than duck-typing it is the point: the gate
    under test is the real one, in the real methods, with the real argument
    handling. Only ``_request`` — the single method that would open a socket —
    is replaced, and it records instead. So "the gate failed" and "the test
    contacted Home Assistant" are the same event, and it is a recorded call in a
    list rather than something happening in a house.
    """

    def __init__(self, states=None):
        # Skip HABridge.__init__: it reads config and builds a requests.Session.
        self.wire: list[tuple[str, str, object]] = []
        self._states = states or {}
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
                self.headers = {"Content-Type": "image/jpeg"}
                self.content = b"\xff\xd8jpeg"

            def json(self):
                return self._payload

        if endpoint == "states":
            # The whole-house read, in HA's shape: a flat list of state dicts.
            return _Resp([{"entity_id": eid, **state}
                          for eid, state in self._states.items()])
        if endpoint.startswith("states/"):
            return _Resp(self._states.get(endpoint.split("/", 1)[1], {}))
        return _Resp([])

    # -- assertions ---------------------------------------------------
    @property
    def service_calls(self):
        """Only the calls that would actuate — POSTs to ``services/…``."""
        return [(e, m, d) for e, m, d in self.wire
                if e.startswith("services/") and m == "POST"]


def scene_state(members):
    return {"state": "on", "attributes": {"entity_id": list(members)}}


# ══════════════════════════════════════════════════════════════════════
#  The legacy LLM tools — the live hole this commit closes
# ══════════════════════════════════════════════════════════════════════

class _LegacyToolHarness:
    """The legacy agent's tool bodies, bound to a trapped bridge.

    ``AssistantTools.__init__`` builds an LLM backend, a config and ~90 tools, so
    constructing one in a unit test is neither cheap nor hermetic. The tools are
    closures over ``self._ha_bridge``, so binding ``create_tools`` to a stand-in
    that has only that attribute exercises the real tool bodies — the real
    argument parsing, the real exception arms — against a bridge that cannot
    reach a house.
    """

    def __init__(self, bridge):
        self._ha_bridge = bridge

    def tools(self):
        from puffpai.assistant_tools import AssistantTools
        with _legacy_import_path():
            made = AssistantTools.create_tools(self)
        return {t.name: t for t in made}


@contextlib.contextmanager
def _legacy_import_path():
    """Make ``src/puffpai`` importable as a flat directory for the duration.

    ``create_tools`` builds every tool in one function and a few of the
    non-HA ones still use pre-package imports (``from app_data import
    AppData``). Those resolve only when ``src/puffpai`` is itself on the path;
    from the repo root the bare name ``app_data`` binds to the unrelated
    ``./app_data`` *directory* as an empty namespace package. Prepending is
    scoped to the call and undone afterwards so it cannot alter imports in
    other tests.
    """
    flat = str(pathlib.Path(__file__).resolve().parents[1] / "src" / "puffpai")
    sys.path.insert(0, flat)
    try:
        yield
    finally:
        try:
            sys.path.remove(flat)
        except ValueError:
            pass


@pytest.fixture
def legacy_tools():
    def build(states=None):
        bridge = WireTrap(states=states)
        return _LegacyToolHarness(bridge).tools(), bridge
    return build


def test_legacy_control_tool_cannot_turn_off_a_camera(legacy_tools):
    """THE hole: an LLM tool call actuating the always-confirm class.

    ``ha_control_device`` called ``HABridge.control_device`` directly, and
    ``control_device`` derives its HA service domain from the entity id — so a
    model that wrote ``camera.<x>|turn_off`` disabled a camera with no human
    anywhere in the loop. Zero calls must reach the wire.
    """
    tools, bridge = legacy_tools()

    out = tools["ha_control_device"].invoke(f"{CAMERA}|turn_off")

    assert bridge.service_calls == [], "the camera was actuated by a tool call"
    assert "always-confirm" in out
    assert "confirm" in out.lower()


def test_legacy_control_tool_cannot_open_a_garage_door(legacy_tools):
    """The physical-security case: ``cover`` is a way into the house."""
    tools, bridge = legacy_tools()

    out = tools["ha_control_device"].invoke(f"{COVER}|turn_on")

    assert bridge.service_calls == []
    assert "always-confirm" in out


def test_legacy_control_tool_refusal_does_not_read_as_a_retryable_error(
        legacy_tools):
    """``ConfirmationRequired`` subclasses ``HABridgeError``.

    If the generic ``except HABridgeError`` arm caught it first the model would
    see "Error: …", which reads as a transient fault worth retrying. The
    confirmation arm has to come first.
    """
    tools, bridge = legacy_tools()

    out = tools["ha_control_device"].invoke(f"{CAMERA}|turn_off")

    assert not out.startswith("Error:")
    assert "Do not retry" in out
    assert bridge.service_calls == []


def test_legacy_scene_tool_cannot_run_a_scene_with_a_critical_member(
        legacy_tools):
    """A scene is a wrapper — gate on what it would actually actuate.

    ``scene`` is not a safety-critical domain, so a gate that looked only at the
    entity id would wave this through. The member list has to be expanded.
    """
    tools, bridge = legacy_tools(states={
        SCENE_CRITICAL: scene_state([CAMERA, LIGHT]),
        SCENE_PLAIN: scene_state([LIGHT]),
    })

    out = tools["ha_run_scene"].invoke(SCENE_CRITICAL)

    assert bridge.service_calls == [], "a scene actuated a camera"
    assert "always-confirm" in out


def test_legacy_scene_tool_fails_closed_when_membership_is_unreadable(
        legacy_tools):
    """Unknown members must not be read as harmless members.

    A scene whose state carries no ``entity_id`` attribute could contain
    anything. "I could not find out what this would do" has to refuse.
    """
    tools, bridge = legacy_tools(states={
        SCENE_PLAIN: {"state": "on", "attributes": {}},
    })

    out = tools["ha_run_scene"].invoke(SCENE_PLAIN)

    assert bridge.service_calls == []
    assert "always-confirm" in out


def test_legacy_light_tool_still_works(legacy_tools):
    """Regression: the gate must not fire on ordinary lights."""
    tools, bridge = legacy_tools()

    out = tools["ha_control_device"].invoke(f"{LIGHT}|turn_on")

    assert bridge.service_calls == [
        ("services/light/turn_on", "POST", {"entity_id": LIGHT})]
    assert "Successfully" in out


def test_legacy_switch_tool_still_works(legacy_tools):
    tools, bridge = legacy_tools()

    out = tools["ha_control_device"].invoke(f"{SWITCH}|toggle")

    assert bridge.service_calls == [
        ("services/switch/toggle", "POST", {"entity_id": SWITCH})]
    assert "Successfully" in out


def test_legacy_raw_api_shim_no_longer_exists():
    """``ha_api_call`` is gone, and must not come back.

    The shim took a free-form ``endpoint`` string and handed it to
    :meth:`HABridge._request`, which sits *underneath* every gate the bridge
    applies — ``services/camera/turn_off`` would have actuated with nothing
    classified and nobody asked. It was mitigated for a while by refusing any
    method but ``GET``, which made it read-only but left the bypass itself in
    place: one ``if`` edit away from actuating again, in a method whose name
    still advertised it as the easy way to talk to HA.

    A mitigated bypass is still a bypass, so it was deleted rather than fenced.
    Anything that needs to actuate goes through the bridge's own methods
    (``control_device`` / ``call_service`` / ``activate_scene``), which classify
    the target and require a human confirmation for the always-confirm class.
    This test is the pin: reintroducing a raw-endpoint escape hatch on
    ``AssistantTools`` under this name turns it red.
    """
    from puffpai.assistant_tools import AssistantTools

    assert not hasattr(AssistantTools, "ha_api_call"), (
        "the raw-endpoint shim is back — it reaches _request beneath the gate")


def test_legacy_zigbee_pair_tool_no_longer_exists(legacy_tools):
    """``ha_pair_zigbee_devices`` is gone, and must not come back.

    The tool handed a model-written duration straight to
    ``ZigbeeManager.open_permit_join``. Once the permit gate landed
    (tests/test_zigbee_permit_gate.py) that call could only ever raise
    ``ConfirmationRequired``, which the tool's blanket ``except`` folded into
    the string "Zigbee pair failed: …" — a dead end that read as a failure
    instead of asking the person. Meanwhile its docstring still advertised the
    exact phrases ("pair my zigbee bulbs", "put the hub in pairing mode") that
    the consent flow in :mod:`puffpai.zigbee_pairing` owns: ``request_pairing``
    files the question, and the only opener is ``complete_approved_pairing``,
    behind a fresh, human-resolved, unspent approval.

    Unlike ``ha_api_call`` above, this one WAS model-reachable — a ``@tool``
    inside ``create_tools`` — so the pin looks where a model would: the
    registered tool names. Registering anything under this name again turns it
    red, and the companion test below proves that is not vacuous.
    """
    tools, _ = legacy_tools()

    assert "ha_pair_zigbee_devices" not in tools, (
        "the retired pairing tool is registered again — pairing goes through "
        "zigbee_pairing's consent flow, never a model-written tool call")


def test_the_pair_tool_pin_would_catch_a_reintroduction(legacy_tools,
                                                        monkeypatch):
    """The pin above is load-bearing, not vacuously green.

    A name-scoped absence check proves nothing if the lens it looks through
    stops showing registered tools. So put the method back: register a
    stand-in under the retired name through ``create_tools`` — the same path
    a real reintroduction would take — and the same lens the pin reads must
    light up.
    """
    from puffpai.assistant_tools import AssistantTools, tool

    real_create_tools = AssistantTools.create_tools

    def create_tools_with_legacy_pair(self):
        made = real_create_tools(self)

        @tool
        def ha_pair_zigbee_devices(duration_seconds: str = "60") -> str:
            """Stand-in for the retired pairing tool. Never invoked."""
            raise AssertionError("the reintroduction probe must never run")

        made.append(ha_pair_zigbee_devices)
        return made

    monkeypatch.setattr(AssistantTools, "create_tools",
                        create_tools_with_legacy_pair)
    tools, _ = legacy_tools()

    assert "ha_pair_zigbee_devices" in tools, (
        "a tool registered under the retired name did not show up where the "
        "retirement pin looks — the pin is vacuous")


# ══════════════════════════════════════════════════════════════════════
#  The bridge methods themselves
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("domain", sorted(SAFETY_CRITICAL_DOMAINS))
def test_every_safety_critical_domain_is_gated_in_control_device(domain):
    """Every member of the class, not just the ones with a named test."""
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.control_device(f"{domain}.test_fixture_entity", "turn_off")

    assert bridge.service_calls == []


@pytest.mark.parametrize("domain", sorted(SAFETY_CRITICAL_DOMAINS))
def test_every_safety_critical_domain_is_gated_in_call_service(domain):
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.call_service(domain, "turn_off",
                            entity_id=f"{domain}.test_fixture_entity")

    assert bridge.service_calls == []


def test_a_service_call_with_no_entity_is_still_classified():
    """``call_service("camera", "turn_off")`` with no entity hits every camera.

    Deriving the class only from the entity id would miss this entirely.
    """
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.call_service("camera", "turn_off")

    assert bridge.service_calls == []


def test_a_benign_service_domain_does_not_launder_a_critical_target():
    """``homeassistant.turn_off`` on a camera is still turning off a camera."""
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.call_service("homeassistant", "turn_off", entity_id=CAMERA)

    assert bridge.service_calls == []


def test_a_list_of_entities_is_classified_by_every_member():
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.call_service("homeassistant", "turn_off",
                            entity_id=[LIGHT, CAMERA])

    assert bridge.service_calls == []


def test_activate_scene_cannot_be_used_to_actuate_a_camera_directly():
    """``activate_scene`` calls ``<domain>.turn_on`` off the entity id.

    So it will happily "activate" a camera — an actuation dressed as a scene.
    """
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.activate_scene(CAMERA)

    assert bridge.service_calls == []


def test_ordinary_scene_still_activates():
    bridge = WireTrap(states={SCENE_PLAIN: scene_state([LIGHT])})

    result = bridge.activate_scene(SCENE_PLAIN)

    assert result["success"] is True
    assert bridge.service_calls == [
        ("services/scene/turn_on", "POST", {"entity_id": SCENE_PLAIN})]


def test_set_light_cannot_be_pointed_at_a_cover():
    """``set_light`` hardcodes the service domain; the entity id is free."""
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.set_light(COVER, brightness_pct=100)

    assert bridge.service_calls == []


def test_set_climate_cannot_be_pointed_at_a_water_heater():
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.set_climate("water_heater.test_tank", temperature=140)

    assert bridge.service_calls == []


def test_create_automation_naming_a_critical_domain_is_gated():
    """Deferred actuation is actuation.

    ``create_automation`` POSTs to the config API, not through
    ``call_service`` — so an automation whose action is ``camera.turn_off``,
    plus a trigger, actuates the class with no human in the loop and nothing
    downstream ever sees a camera service call.
    """
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.create_automation({
            "alias": "test fixture",
            "trigger": [{"platform": "time", "at": "03:00:00"}],
            "action": [{"service": "camera.turn_off",
                        "target": {"entity_id": CAMERA}}],
        })

    assert bridge.wire == []


def test_create_automation_for_ordinary_domains_still_works():
    bridge = WireTrap()

    result = bridge.create_automation({
        "alias": "test fixture",
        "trigger": [{"platform": "time", "at": "07:00:00"}],
        "action": [{"service": "light.turn_on", "target": {"entity_id": LIGHT}}],
    })

    assert result["success"] is True
    assert len(bridge.wire) == 1


# ══════════════════════════════════════════════════════════════════════
#  The opt-in: only the token counts
# ══════════════════════════════════════════════════════════════════════

def test_the_token_lets_a_confirmed_call_through():
    """The gate must engage, not seize."""
    bridge = WireTrap()

    result = bridge.control_device(CAMERA, "turn_off",
                                   human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert bridge.service_calls == [
        ("services/camera/turn_off", "POST", {"entity_id": CAMERA})]


def test_the_token_is_never_forwarded_to_home_assistant():
    """It is an explicit keyword-only parameter, so it is consumed here.

    If it fell into ``**service_data`` it would be POSTed to HA as service data.
    """
    bridge = WireTrap()

    bridge.control_device(CAMERA, "turn_off", human_confirmed=HUMAN_CONFIRMED)

    _endpoint, _method, data = bridge.service_calls[0]
    assert data == {"entity_id": CAMERA}, (
        "the confirmation token leaked into the HA service payload")


@pytest.mark.parametrize("spoof", [
    True, 1, "true", "HUMAN_CONFIRMED", "yes", [1], {"confirmed": True},
    object(),
])
def test_a_json_shaped_confirmation_is_not_a_confirmation(spoof):
    """The anti-spoof property, stated directly.

    Every value here is something an LLM tool call or an HTTP body could
    contain. None of them is the in-process token, so none of them actuates.
    A ``human_confirmed: bool = True`` contract would have let the first four
    through.
    """
    bridge = WireTrap()

    with pytest.raises(ConfirmationRequired):
        bridge.control_device(CAMERA, "turn_off", human_confirmed=spoof)

    assert bridge.service_calls == []


def test_the_control_route_body_splat_cannot_confirm_itself():
    """``/ha/control`` does ``control_device(entity_id, action, **extra)``.

    ``extra`` is every unrecognised key in the request JSON, so a client really
    can put ``human_confirmed`` in the body and have it bind to the parameter.
    This is the concrete reason the contract is an object identity and not a
    bool: reproduced here as the route builds it.
    """
    bridge = WireTrap()
    body = {"entity_id": CAMERA, "action": "turn_off", "human_confirmed": True,
            "confirmed": True}
    extra = {k: v for k, v in body.items() if k not in ("entity_id", "action")}

    with pytest.raises(ConfirmationRequired):
        bridge.control_device(body["entity_id"], body["action"], **extra)

    assert bridge.service_calls == []


def test_is_human_confirmation_only_accepts_the_token():
    assert is_human_confirmation(HUMAN_CONFIRMED) is True
    assert is_human_confirmation(False) is False
    assert is_human_confirmation(None) is False
    assert is_human_confirmation(True) is False


def test_the_token_does_not_render_as_a_json_value():
    """Belt and braces: it must not stringify into something quotable."""
    assert repr(HUMAN_CONFIRMED) == "<HUMAN_CONFIRMED>"
    assert not isinstance(HUMAN_CONFIRMED, (bool, int, str, bytes))


# ══════════════════════════════════════════════════════════════════════
#  Reads are untouched
# ══════════════════════════════════════════════════════════════════════

def test_camera_reads_still_work():
    """``camera`` is gated for *service calls* only.

    Snapshots go through ``camera_proxy``, not a service call, so gating the
    domain costs no read capability — and the dashboard camera proxy that calls
    ``/ha/camera/<id>`` must keep working.
    """
    bridge = WireTrap()

    image, content_type = bridge.get_camera_image(CAMERA)

    assert image == b"\xff\xd8jpeg"
    assert content_type == "image/jpeg"
    assert bridge.service_calls == []
    assert bridge.wire == [(f"camera_proxy/{CAMERA}", "GET", None)]


def test_reading_a_camera_state_is_not_gated():
    bridge = WireTrap(states={CAMERA: {"state": "recording"}})

    assert bridge.get_entity_state(CAMERA) == {"state": "recording"}


def test_reading_a_lock_state_is_not_gated():
    """A question about a lock is not an unlocking."""
    bridge = WireTrap(states={"lock.test_front": {"state": "locked"}})

    assert bridge.get_entity_state("lock.test_front")["state"] == "locked"


def test_listing_states_is_not_gated():
    bridge = WireTrap()
    assert bridge.get_all_states() == []


def test_get_history_on_a_critical_entity_is_not_gated():
    bridge = WireTrap()
    assert bridge.get_history(CAMERA) == []
    assert bridge.service_calls == []


# ══════════════════════════════════════════════════════════════════════
#  The shared policy helpers
# ══════════════════════════════════════════════════════════════════════

def test_touched_domains_unions_service_and_targets():
    assert touched_domains("homeassistant", CAMERA) == {"homeassistant", "camera"}
    assert touched_domains("camera", None) == {"camera"}
    assert touched_domains(None, [LIGHT, COVER]) == {"light", "cover"}


def test_scene_member_domains_reports_unknown_membership():
    class _Blind:
        def get_entity_state(self, entity_id):
            raise HABridgeError("unreachable")

    domains, known = scene_member_domains(_Blind(), SCENE_PLAIN)
    assert known is False


def test_enforce_confirmation_carries_the_proposal():
    """The refusal has to say what would have happened, so a UI can ask."""
    with pytest.raises(ConfirmationRequired) as caught:
        enforce_confirmation({"camera"}, service="camera.turn_off",
                             entity_id=CAMERA, confirmation=False)

    exc = caught.value
    assert exc.proposal["entity_id"] == CAMERA
    assert exc.proposal["service"] == "camera.turn_off"
    assert exc.proposal["domains"] == ["camera"]
    assert isinstance(exc, HABridgeError)  # every caller already handles this


def test_enforce_confirmation_is_a_no_op_for_ordinary_domains():
    enforce_confirmation({"light", "switch"}, service="light.turn_on",
                         entity_id=LIGHT, confirmation=False)


# ══════════════════════════════════════════════════════════════════════
#  The Steward still behaves as before, now delegating
# ══════════════════════════════════════════════════════════════════════

def test_steward_still_returns_a_proposal_not_an_exception():
    """The Steward's API is unchanged: a refusal is an ``ok`` proposal reply."""
    from puffpai.agents.envelope import Budget, Envelope
    from puffpai.agents.steward import StewardAgent

    bridge = WireTrap()
    steward = StewardAgent(ha_bridge=bridge)
    env = Envelope(intent="control_device",
                   payload={"entity_id": CAMERA, "action": "turn_off"},
                   requester="owner", origin_agent="concierge",
                   budget=Budget(max_actions=4, max_seconds=30.0),
                   state_changing=True)

    reply = steward.handle(env)

    assert reply["ok"] is True
    assert reply["executed"] is False
    assert reply["requires_confirmation"] is True
    assert bridge.service_calls == []


def test_steward_hands_the_real_bridge_a_token_when_a_human_confirmed():
    """The Steward is the one caller that may present the token.

    It does so only on the three conditions in ``human_confirmed``: a
    state-changing envelope, ``confirmed=True``, and a *human* origin.
    """
    from puffpai.agents.envelope import Budget, Envelope
    from puffpai.agents.steward import StewardAgent

    bridge = WireTrap()
    steward = StewardAgent(ha_bridge=bridge)
    env = Envelope(intent="control_device",
                   payload={"entity_id": CAMERA, "action": "turn_off",
                            "confirmed": True},
                   requester="owner", origin_agent="voice",
                   budget=Budget(max_actions=4, max_seconds=30.0),
                   state_changing=True)

    reply = steward.handle(env)

    assert reply["ok"] is True
    assert bridge.service_calls == [
        ("services/camera/turn_off", "POST", {"entity_id": CAMERA})]


def test_steward_cannot_confirm_on_an_agent_origin():
    """An agent stamps its own name as origin, so it cannot enter HUMAN_ORIGINS."""
    from puffpai.agents.envelope import Budget, Envelope
    from puffpai.agents.steward import StewardAgent

    bridge = WireTrap()
    steward = StewardAgent(ha_bridge=bridge)
    env = Envelope(intent="control_device",
                   payload={"entity_id": CAMERA, "action": "turn_off",
                            "confirmed": True},
                   requester="owner", origin_agent="concierge",
                   budget=Budget(max_actions=4, max_seconds=30.0),
                   state_changing=True)

    reply = steward.handle(env)

    assert reply["requires_confirmation"] is True
    assert bridge.service_calls == []


def test_steward_ordinary_control_is_unaffected():
    from puffpai.agents.envelope import Budget, Envelope
    from puffpai.agents.steward import StewardAgent

    bridge = WireTrap()
    steward = StewardAgent(ha_bridge=bridge)
    env = Envelope(intent="control_device",
                   payload={"entity_id": LIGHT, "action": "turn_on"},
                   requester="owner", origin_agent="concierge",
                   budget=Budget(max_actions=4, max_seconds=30.0),
                   state_changing=True)

    reply = steward.handle(env)

    assert reply["ok"] is True
    assert bridge.service_calls == [
        ("services/light/turn_on", "POST", {"entity_id": LIGHT})]
