"""Tests for Concierge intent resolution + delegation (the "wrong agent" fix).

The Concierge used to hardcode ``list_devices`` for every "home" message, so
"take a snapshot from the back door camera" answered with a device list.
These tests pin the deterministic resolution table, entity resolution against a
*live* device list (never hardcoded ids), the state_changing/targets pairing
Sentinel's consent gate depends on, and the always-confirm siren gate.

Everything here runs with **no LLM engine attached** — chat lands on request
threads that may hit a cold model, so engine-less behaviour is the contract.

Pure in-process; no HA, no sockets.
Run: pytest tests/test_concierge_intents.py -q
"""

from __future__ import annotations

import pytest

from puffpai.agents.concierge import ConciergeAgent, resolve_intent
from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.steward import STATE_CHANGING_INTENTS


# ── stubs ───────────────────────────────────────────────────────────

HOUSE = [
    {"entity_id": "camera.back_door_fluent",
     "name": "Back Door Fluent", "state": "idle"},
    {"entity_id": "camera.front_door_fluent",
     "name": "Front Door Fluent", "state": "idle"},
    {"entity_id": "light.porch_light", "name": "Porch Light", "state": "off"},
    {"entity_id": "light.kitchen_light", "name": "Kitchen Light", "state": "on"},
    {"entity_id": "climate.hallway_thermostat",
     "name": "Hallway Thermostat", "state": "heat"},
    {"entity_id": "scene.movie_night", "name": "Movie Night", "state": "unknown"},
    {"entity_id": "siren.shed_siren", "name": "Shed Siren", "state": "off"},
    {"entity_id": "todo.household", "name": "Household", "state": "0"},
]


class StubBus:
    """Records every envelope the Concierge sends and replies canned dicts."""

    def __init__(self, devices=None, replies=None, review=True):
        self.devices = HOUSE if devices is None else devices
        self.replies = replies or {}
        self.sent: list[tuple[str, Envelope]] = []
        self.review = review

    def get(self, name):
        return object() if name in ("steward", "sentinel", "archivist",
                                    "majordomo") else None

    def send(self, envelope, to):
        self.sent.append((to, envelope))
        # Mirror the real bus: a state_changing envelope is vetoed first.
        if envelope.state_changing and not self.review:
            return {"ok": False, "denied": True, "by": "sentinel",
                    "verdict": "deny", "reason": "target(s) not consented"}
        if envelope.intent == "list_devices":
            return {"ok": True, "devices": list(self.devices)}
        canned = self.replies.get(envelope.intent)
        if canned is not None:
            return canned
        return {"ok": True}

    def envelopes(self, intent):
        return [e for _, e in self.sent if e.intent == intent]


def concierge(bus=None) -> tuple[ConciergeAgent, StubBus]:
    bus = bus or StubBus()
    agent = ConciergeAgent()
    agent.bind_bus(bus)
    return agent, bus


def chat(agent, text, *, max_actions=6) -> dict:
    env = Envelope(
        intent="chat", payload={"text": text}, requester="owner",
        origin_agent="api_server",
        budget=Budget(max_actions=max_actions, max_seconds=20.0),
    )
    return agent.handle(env)


# ── the resolution table (deterministic, engine-less) ───────────────

# (utterance, expected intent, state_changing, needs a resolved entity)
TABLE = [
    ("take a snapshot from the back door camera", "camera_snapshot", False, True),
    ("show me the front door camera", "camera_snapshot", False, True),
    ("is the porch light on?", "device_status", False, True),
    ("what's the status of the hallway thermostat", "device_status", False, True),
    # House-STATE reads that used to have no rule at all (they reached the
    # team only after the router stopped calling them knowledge questions).
    ("what is the temperature in the living room?", "device_status", False, True),
    ("how warm is it in here", "device_status", False, True),
    ("what's the humidity in the basement", "device_status", False, True),
    ("did i leave the garage door open", "device_status", False, True),
    ("turn on the porch light", "control_device", True, True),
    ("turn off the kitchen light", "control_device", True, True),
    ("dim the kitchen light to 30%", "control_device", True, True),
    ("activate movie night scene", "activate_scene", True, True),
    ("set the thermostat to 68 degrees", "set_climate", True, True),
    ("make it warmer in here", "set_climate", True, True),
    ("what's playing right now", "media_status", False, False),
    ("search my library for jazz albums", "media_search", False, False),
    ("notify me that the laundry is done", "notify_user", True, False),
    ("announce that dinner is ready", "announce", True, False),
    ("add milk to the shopping list", "list_add", True, False),
    ("sound the siren", "siren", True, True),
    ("list devices", "list_devices", False, False),
    ("tell me a joke about ducks", "list_devices", False, False),
]


@pytest.mark.parametrize("text,intent,changing,needs_entity", TABLE)
def test_resolution_table(text, intent, changing, needs_entity):
    res = resolve_intent(text)
    assert res.intent == intent, f"{text!r} -> {res.intent!r}"
    assert res.state_changing is changing
    assert res.needs_entity is needs_entity
    assert (intent in STATE_CHANGING_INTENTS) is changing


@pytest.mark.parametrize("text", [
    "play some jazz",
    "watch a movie",
    "unlock the front door",
    "open the garage door",
])
def test_capabilities_the_team_lacks_are_left_to_the_legacy_path(text):
    res = resolve_intent(text)
    assert res.intent is None
    assert res.reason


def test_resolution_needs_no_engine():
    agent, _ = concierge()
    assert agent.health()["engine_attached"] is False


# ── entity resolution against the live device list ──────────────────

def test_snapshot_resolves_the_right_camera_end_to_end():
    agent, bus = concierge()
    out = chat(agent, "take a snapshot from the back door camera")

    assert out["intent"] == "camera_snapshot"
    assert out["handled"] is True
    snap = bus.envelopes("camera_snapshot")
    assert len(snap) == 1
    assert snap[0].payload["entity_id"] == "camera.back_door_fluent"
    # A snapshot is read-only: no Sentinel review, no targets.
    assert snap[0].state_changing is False


def test_entity_ids_are_never_hardcoded():
    """A different house yields a different id from the same words."""
    bus = StubBus(devices=[{"entity_id": "camera.utility_room_cam",
                            "name": "Mud Room Door", "state": "idle"}])
    agent, bus = concierge(bus)
    chat(agent, "snapshot of the mud room door camera")
    assert bus.envelopes("camera_snapshot")[0].payload["entity_id"] \
        == "camera.utility_room_cam"


def test_ambiguous_target_names_candidates_and_does_not_actuate():
    # A light and a switch that share a friendly name — a real house shape.
    bus = StubBus(devices=[
        {"entity_id": "light.porch", "name": "Porch", "state": "off"},
        {"entity_id": "switch.porch", "name": "Porch", "state": "off"},
    ])
    agent, bus = concierge(bus)
    out = chat(agent, "turn on the porch")

    assert out["handled"] is True
    assert "more than one" in out["reply"].lower()
    assert bus.envelopes("control_device") == []


def test_unresolvable_target_replies_helpfully_and_does_not_actuate():
    agent, bus = concierge()
    out = chat(agent, "turn on the greenhouse misters")

    assert out["handled"] is True
    assert "couldn't find" in out["reply"].lower() or "not sure" in out["reply"].lower()
    assert bus.envelopes("control_device") == []


# ── a literal entity id still has to be one of OURS ─────────────────
# ``is_valid_entity_id`` checks SHAPE, and "domain.object_id" is also the shape
# of a hostname. ``_resolve_entity`` used to short-circuit on that shape alone,
# so a URL in the text became a device target: "read https://nope.example/x and
# then turn on the porch light" resolved to targets=['nope.example'], which
# travelled all the way to the consent gate. Membership in the house's own
# device list is the check; shape never was one.


class NoDeviceListBus(StubBus):
    """A house that cannot be enumerated (HA down, Steward unreachable)."""

    def send(self, envelope, to):
        if envelope.intent == "list_devices":
            return {"ok": False, "error": "unavailable"}
        return super().send(envelope, to)


def test_url_hostname_never_becomes_a_device_target():
    agent, bus = concierge()
    out = chat(agent, "read https://nope.example/x and then turn on the porch light")

    assert "nope.example" not in (out["targets"] or [])
    # Not anywhere on the wire either — not as a target, not as an entity_id.
    for _, env in bus.sent:
        payload = env.payload or {}
        assert payload.get("entity_id") != "nope.example"
        assert "nope.example" not in (payload.get("targets") or [])
    # And the sentence still works: the porch light is what the user meant, and
    # the bogus literal no longer hijacks resolution before scoring runs.
    assert out["targets"] == ["light.porch_light"]


def test_literal_entity_id_absent_from_the_house_does_not_actuate():
    """An id the model invented is not a device just because it parses."""
    agent, bus = concierge()
    out = chat(agent, "turn on light.does_not_exist")

    assert out["targets"] == []
    assert bus.envelopes("control_device") == []


def test_literal_entity_id_that_exists_still_resolves():
    """The fast path is intact for ids this house actually has."""
    agent, bus = concierge()
    out = chat(agent, "turn on light.porch_light")

    assert out["targets"] == ["light.porch_light"]
    assert bus.envelopes("control_device")[0].payload["entity_id"] \
        == "light.porch_light"


def test_literal_entity_id_fails_closed_when_the_house_cannot_be_listed():
    """No device list means nothing to check against — so nothing actuates.

    The old short-circuit answered from the text alone and never needed the
    list, which is exactly how an unverified string reached the gate.
    """
    agent, bus = concierge(NoDeviceListBus())
    out = chat(agent, "turn on light.porch_light")

    assert out["targets"] == []
    assert bus.envelopes("control_device") == []


# ── house-STATE reads: "what is the temperature in the living room" ─
# The router must not classify this as ``rag``, or it never reaches the
# team and the tool-less RAG path answers that it cannot read the
# thermostat. It can. These pin the whole path from words to a climate entity.

#: A house shaped like the reported one: a living-room thermostat, plus a
#: living-room *lamp* that shares every room token with it.
CLIMATE_HOUSE = [
    {"entity_id": "climate.living_room", "name": "Living Room", "state": "heat"},
    {"entity_id": "climate.bedroom", "name": "Bedroom", "state": "off"},
    {"entity_id": "light.living_room_lamp",
     "name": "Living Room Lamp", "state": "on"},
]

LIVING_ROOM_STATE = {
    "entity_id": "climate.living_room",
    "state": "heat",
    "attributes": {"friendly_name": "Living Room",
                   "current_temperature": 71.5, "temperature": 70},
}


def test_temperature_question_reaches_device_status_on_a_climate_entity():
    bus = StubBus(devices=CLIMATE_HOUSE,
                  replies={"device_status": {"ok": True,
                                             "state": LIVING_ROOM_STATE}})
    agent, bus = concierge(bus)
    out = chat(agent, "what is the temperature in the living room?")

    assert out["intent"] == "device_status"
    assert out["handled"] is True
    sent = bus.envelopes("device_status")
    assert len(sent) == 1
    # The climate entity, not the lamp that shares its room name.
    assert sent[0].payload["entity_id"] == "climate.living_room"
    # A read: no Sentinel review, no targets.
    assert sent[0].state_changing is False
    assert "targets" not in sent[0].payload
    # And the answer actually contains the reading, not the HVAC mode alone.
    assert "71.5" in out["reply"]


def test_temperature_reply_reports_the_reading_not_just_the_hvac_mode():
    """A climate entity's *state* is "heat" — the number is in the attributes."""
    bus = StubBus(devices=CLIMATE_HOUSE,
                  replies={"device_status": {"ok": True,
                                             "state": LIVING_ROOM_STATE}})
    agent, _ = concierge(bus)
    reply = chat(agent, "how warm is it in the bedroom")["reply"]
    assert "currently" in reply


def test_unknown_room_degrades_to_a_helpful_miss():
    """Worse than the old behaviour would be routing here and then failing."""
    bus = StubBus(devices=CLIMATE_HOUSE)
    agent, bus = concierge(bus)
    out = chat(agent, "what is the temperature in the greenhouse?")

    assert out["handled"] is True
    assert "couldn't find" in out["reply"].lower()
    assert bus.envelopes("device_status") == []


def test_unreachable_device_list_falls_back_instead_of_guessing():
    bus = StubBus()
    bus.send = lambda envelope, to: {"ok": False, "error": "ha down"}
    agent, _ = concierge(bus)
    out = chat(agent, "turn on the porch light")
    assert out["handled"] is False
    assert out["reply"] is None


def test_budget_exhaustion_degrades_instead_of_raising():
    agent, bus = concierge()
    out = chat(agent, "turn on the porch light", max_actions=0)
    assert out["ok"] is True
    assert out["handled"] is False
    assert bus.envelopes("control_device") == []


# ── state_changing / targets pairing (Sentinel's consent gate) ──────

def test_state_changing_intent_mints_a_reviewed_envelope_with_targets():
    agent, bus = concierge()
    out = chat(agent, "turn on the porch light")

    env = bus.envelopes("control_device")[0]
    assert env.state_changing is True
    assert env.payload["targets"] == ["light.porch_light"]
    assert env.payload["action"] == "turn_on"
    assert out["targets"] == ["light.porch_light"]


def test_dim_carries_a_brightness_percentage():
    agent, bus = concierge()
    chat(agent, "dim the kitchen light to 30%")
    env = bus.envelopes("control_device")[0]
    assert env.payload["kwargs"] == {"brightness_pct": 30}


def test_read_only_intent_is_not_marked_state_changing():
    agent, bus = concierge()
    chat(agent, "is the porch light on?")
    env = bus.envelopes("device_status")[0]
    assert env.state_changing is False
    assert "targets" not in env.payload


def test_concierge_never_stamps_a_human_origin():
    agent, bus = concierge()
    chat(agent, "turn on the porch light")
    for _, env in bus.sent:
        assert env.origin_agent == "concierge"


def test_a_sentinel_denial_is_surfaced_not_laundered():
    agent, bus = concierge(StubBus(review=False))
    out = chat(agent, "turn on the porch light")
    assert out["handled"] is True
    assert "blocked" in out["reply"].lower()


def test_climate_setpoint_is_parsed_and_targeted():
    agent, bus = concierge()
    chat(agent, "set the thermostat to 68 degrees")
    env = bus.envelopes("set_climate")[0]
    assert env.payload["entity_id"] == "climate.hallway_thermostat"
    assert env.payload["temperature"] == 68.0
    assert env.payload["targets"] == ["climate.hallway_thermostat"]


def test_shopping_list_add_needs_no_entity():
    agent, bus = concierge(StubBus(replies={
        "list_add": {"ok": True, "list": "shopping", "item": "milk"}}))
    out = chat(agent, "add milk to the shopping list")
    env = bus.envelopes("list_add")[0]
    assert env.payload["item"] == "milk"
    assert env.payload["list"] == "shopping"
    assert env.state_changing is True
    assert "milk" in out["reply"]


# ── the always-confirm siren gate ───────────────────────────────────

def test_siren_via_chat_only_ever_proposes():
    agent, bus = concierge(StubBus(replies={"siren": {
        "ok": True, "executed": False, "requires_confirmation": True,
        "proposal": {"service": "siren.turn_on", "entity_id": "siren.shed_siren"},
        "reason": "always-confirm",
    }}))
    out = chat(agent, "sound the siren")

    env = bus.envelopes("siren")[0]
    assert env.payload["entity_id"] == "siren.shed_siren"
    assert env.state_changing is True
    assert env.origin_agent == "concierge"       # never a HUMAN_ORIGIN
    assert env.payload.get("confirmed") is not True
    assert out["handled"] is True
    assert "confirmation" in out["reply"].lower()


# ── on the real bus (real Sentinel veto, real Steward) ──────────────

class HouseBridge:
    """HABridge stand-in holding the fixture house."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.images: list[str] = []

    def list_devices(self, domain=None, controllable_only=True):
        return [type("D", (), {"entity_id": d["entity_id"], "name": d["name"],
                               "state": d["state"]})()
                for d in HOUSE
                if domain is None or d["entity_id"].startswith(domain + ".")]

    def get_entity_state(self, entity_id):
        return {"state": "on"}

    def get_camera_image(self, entity_id):
        self.images.append(entity_id)
        return b"\xff\xd8jpeg", "image/jpeg"

    def call_service(self, domain, service, entity_id=None, **data):
        self.calls.append((domain, service, entity_id))
        return []

    def control_device(self, entity_id, action, **kwargs):
        self.call_service(entity_id.split(".")[0], action, entity_id, **kwargs)
        return {"success": True, "entity_id": entity_id, "action": action}


@pytest.fixture
def real_bus(tmp_path, monkeypatch):
    """A real AgentBus with Concierge + Steward + Sentinel and no HA."""
    import puffpai.security.consent_gate as cg
    from puffpai.agents.bus import AgentBus
    from puffpai.agents.sentinel_agent import SentinelAgent
    from puffpai.agents.steward import StewardAgent
    from puffpai.security.audit_log import AuditLog

    monkeypatch.setattr(cg, "REVIEW_LOG", str(tmp_path / "reviews.jsonl"))
    store = cg.ConsentStore(path=str(tmp_path / "consent.json"))
    monkeypatch.setattr(cg, "_store", store)
    monkeypatch.setattr(cg.ConsentStore, "_legacy_cache", set())

    audit = AuditLog(log_file=str(tmp_path / "audit.jsonl"))
    bus = AgentBus(audit=audit)
    bridge = HouseBridge()
    bus.register(ConciergeAgent())
    bus.register(StewardAgent(ha_bridge=bridge))
    bus.register(SentinelAgent(audit=audit, device_sentinel=False))
    return bus, bridge, store


def _ask(bus, text):
    env = Envelope(intent="chat", payload={"text": text}, requester="owner",
                   origin_agent="api_server", budget=Budget(max_actions=6))
    return bus.send(env, to="concierge")


def test_real_bus_snapshot_reaches_the_camera(real_bus):
    bus, bridge, _ = real_bus
    out = _ask(bus, "take a snapshot from the back door camera")

    assert out["intent"] == "camera_snapshot"
    assert out["handled"] is True
    assert bridge.images == ["camera.back_door_fluent"]
    assert "snapshot" in out["reply"].lower()


def test_real_bus_unconsented_control_is_vetoed_before_any_service_call(real_bus):
    bus, bridge, _ = real_bus
    out = _ask(bus, "turn on the porch light")

    assert out["intent"] == "control_device"
    assert out["delegate_reply"]["denied"] is True
    assert bridge.calls == []              # the veto ran BEFORE the Steward
    assert "blocked" in out["reply"].lower()


def test_real_bus_consented_control_executes(real_bus):
    bus, bridge, store = real_bus
    store.approve("light.porch_light", by="test")

    out = _ask(bus, "turn on the porch light")

    assert out["delegate_reply"]["ok"] is True
    assert bridge.calls == [("light", "turn_on", "light.porch_light")]
    assert "porch" in out["reply"]


def test_siren_reaches_a_real_steward_as_a_proposal_only():
    """End-to-end through the real Steward: proposed, never executed."""
    from puffpai.agents.steward import StewardAgent

    class Bridge:
        def __init__(self):
            self.calls = []

        def call_service(self, domain, service, entity_id=None, **data):
            self.calls.append((domain, service, entity_id))
            return []

        def get_entity_state(self, entity_id):
            return {"state": "off"}

        def list_devices(self, domain=None, controllable_only=True):
            return []

    bridge = Bridge()
    steward = StewardAgent(ha_bridge=bridge)
    env = Envelope(intent="siren",
                   payload={"entity_id": "siren.shed_siren", "action": "turn_on"},
                   origin_agent="concierge", state_changing=True,
                   budget=Budget(max_actions=3))
    reply = steward.handle(env)

    assert reply["executed"] is False
    assert reply["requires_confirmation"] is True
    assert bridge.calls == []


# ── device-TYPE words gate the domain (the climate-flip regression) ──

# A living room where the lamps have NO entity (they
# hang off an automation), and the only strong name match for "living room"
# is the thermostat.
LIVING_ROOM_HOUSE = [
    {"entity_id": "climate.living_room", "name": "Living Room",
     "state": "heat_cool"},
    {"entity_id": "climate.bedroom", "name": "Upstairs Bedroom",
     "state": "heat"},
    {"entity_id": "media_player.den_tv", "name": "Den TV",
     "state": "off"},
    {"entity_id": "automation.puffpai_livingroom_lamps_control",
     "name": "PuffPAI — Livingroom Lamps Control", "state": "on"},
]


def test_a_light_request_never_lands_on_the_thermostat():
    """Live regression: 'turn off the living room light' switched the CLIMATE.

    climate.living_room's friendly name "Living Room" was a perfect two-token
    match, and nothing said a request for a LIGHT must land on something
    light-shaped. A state-changing verb aimed at a named device type must
    refuse honestly rather than actuate the nearest name in another domain.
    """
    agent, bus = concierge(StubBus(devices=LIVING_ROOM_HOUSE))
    reply = chat(agent, "can you turn off the living room light")

    assert bus.envelopes("control_device") == []
    assert "couldn't find" in (reply.get("reply") or "").lower()


def test_type_worded_requests_still_find_their_own_domain():
    agent, bus = concierge()
    chat(agent, "turn off the porch light")
    sent = bus.envelopes("control_device")
    assert len(sent) == 1
    assert sent[0].payload["entity_id"] == "light.porch_light"


def test_thermostat_requests_still_reach_climate():
    # (An existing rule quirk reads "turn off the ... thermostat" as a STATUS
    # request — the point here is only that the "thermostat" type word still
    # RESOLVES to the climate entity, whatever intent the rule table picks.)
    agent, bus = concierge()
    chat(agent, "turn off the hallway thermostat")
    targeted = [e for _, e in bus.sent if (e.payload or {}).get("entity_id")]
    assert targeted
    assert targeted[-1].payload["entity_id"] == "climate.hallway_thermostat"


BEDROOM_HOUSE = [
    {"entity_id": "sensor.bedroom_temperature", "name": "Bedroom Temperature",
     "state": "73.5"},
    {"entity_id": "climate.bedroom", "name": "Upstairs Bedroom",
     "state": "heat"},
]


def test_a_temperature_control_never_targets_a_sensor():
    """'temperature' must not imply the
    sensor domain for a CONTROL action, landing it on sensor.bedroom_temperature
    (2 tokens) instead of climate.bedroom (1 token). A sensor is never a
    valid thing to actuate."""
    agent, bus = concierge(StubBus(devices=BEDROOM_HOUSE))
    chat(agent, "turn the temperature down in the bedroom")
    targeted = [e for _, e in bus.sent if (e.payload or {}).get("entity_id")]
    for env in targeted:
        assert not env.payload["entity_id"].startswith("sensor."), env.payload


def test_no_match_refusal_names_what_the_house_has():
    """'add my second reolink camera' must list the real cameras back."""
    cams = [
        {"entity_id": "camera.back_door_fluent",
         "name": "Back Door Fluent", "state": "idle"},
        {"entity_id": "camera.driveway_fluent",
         "name": "Driveway Fluent", "state": "unavailable"},
    ]
    agent, bus = concierge(StubBus(devices=cams))
    reply = chat(agent, "take a snapshot from the second reolink camera")
    msg = str(reply.get("reply") or "")
    assert "Back Door Fluent" in msg
    assert "Driveway Fluent" in msg
