"""Tests for the Steward's Phase-3a house tools.

Covers the six new actuators (``notify_user``, ``announce``, ``camera_snapshot``,
``set_climate``, ``siren``, ``list_add``): the exact HA service + payload on the
happy path, refusal of bad input *before* any HTTP, offline devices answering
"unavailable" instead of crashing or silently succeeding, budget metering, the
state_changing/Sentinel classification, and the always-confirm siren gate.

Pure in-process; no HA, no sockets.
Run: pytest tests/test_steward_tools.py -q
"""

from __future__ import annotations

import base64

import pytest

from puffpai import ha_bridge
from puffpai.agents.envelope import Budget, BudgetExceeded, Envelope
from puffpai.agents.steward import (
    STATE_CHANGING_INTENTS,
    StewardAgent,
)
from puffpai.ha_bridge import HABridgeError


# ── stubs ───────────────────────────────────────────────────────────

class StubBridge:
    """HABridge stand-in that records every service call it is asked for."""

    def __init__(self, states=None, image=b"\xff\xd8jpegbytes"):
        self.calls: list[tuple[str, str, dict]] = []
        self.image_calls: list[str] = []
        self.states = states if states is not None else {}
        self.default_state = {"state": "on"}
        self._image = image

    def call_service(self, domain, service, entity_id=None, **data):
        ha_bridge.validate_segment(domain, kind="service domain")
        ha_bridge.validate_segment(service, kind="service name")
        payload = dict(data)
        if entity_id is not None:
            payload["entity_id"] = entity_id
        self.calls.append((domain, service, payload))
        return []

    def get_entity_state(self, entity_id):
        ha_bridge.validate_entity_id(entity_id)
        if entity_id in self.states:
            return self.states[entity_id]
        return dict(self.default_state)

    def get_camera_image(self, entity_id):
        ha_bridge.validate_entity_id(entity_id)
        self.image_calls.append(entity_id)
        return self._image, "image/jpeg"

    def list_devices(self, domain=None, controllable_only=True):
        return []


class StubDevice:
    def __init__(self, entity_id, state="idle"):
        self.entity_id = entity_id
        self.state = state


#: The house media host, as the appliance's own config names it. Announcement
#: audio has to come from a configured house host or a public one, so the tests
#: inject the allow-list rather than depending on whether config files exist.
MEDIA_HOST = "media.local"
MEDIA_URL = f"http://{MEDIA_HOST}:8096/tts/dinner.mp3"


def steward(bridge=None, *, announce_hosts=(MEDIA_HOST,)
            ) -> tuple[StewardAgent, StubBridge]:
    bridge = bridge or StubBridge()
    return StewardAgent(ha_bridge=bridge,
                        announce_hosts=announce_hosts), bridge


def env(intent, payload=None, *, actions=3, state_changing=True,
        origin="concierge") -> Envelope:
    return Envelope(
        intent=intent,
        payload=payload or {},
        requester="owner",
        origin_agent=origin,
        budget=Budget(max_actions=actions, max_seconds=30.0),
        state_changing=state_changing,
    )


# Ids that must never reach an HTTP request (mirrors the ha_bridge suite).
TRAVERSAL_IDS = [
    "../../auth/providers",
    "light.kitchen/../../auth",
    "light.KITCHEN",
    "light.kitchen\n",
    "light",
    "",
]

NEW_INTENTS = ["notify_user", "announce", "set_climate", "siren", "list_add"]


# ── classification ──────────────────────────────────────────────────

@pytest.mark.parametrize("intent", NEW_INTENTS)
def test_new_actuators_are_declared_state_changing(intent):
    assert intent in STATE_CHANGING_INTENTS


@pytest.mark.parametrize("intent", NEW_INTENTS)
def test_actuator_on_a_read_only_envelope_is_refused_with_no_call(intent):
    """No Sentinel review on the envelope means no actuation."""
    agent, bridge = steward()
    reply = agent.handle(env(intent, {"entity_id": "light.kitchen",
                                      "message": "hi", "text": "hi",
                                      "item": "milk", "temperature": 70},
                             state_changing=False))
    assert reply["ok"] is False
    assert reply["requires_review"] is True
    assert bridge.calls == []


def test_camera_snapshot_is_not_state_changing():
    assert "camera_snapshot" not in STATE_CHANGING_INTENTS
    agent, bridge = steward(StubBridge(states={"camera.shed": {"state": "idle"}}))
    reply = agent.handle(env("camera_snapshot", {"entity_id": "camera.shed"},
                             state_changing=False))
    assert reply["ok"] is True


# ── notify_user ─────────────────────────────────────────────────────

def test_notify_user_calls_persistent_notification():
    agent, bridge = steward()
    e = env("notify_user", {"message": "Shed door open", "title": "PuffPAI"})
    reply = agent.handle(e)

    assert reply["ok"] is True
    assert reply["service"] == "notify.persistent_notification"
    assert bridge.calls == [
        ("notify", "persistent_notification",
         {"message": "Shed door open", "title": "PuffPAI"})]
    assert e.budget.actions_used == 1


def test_notify_user_honours_an_explicit_service():
    agent, bridge = steward()
    agent.handle(env("notify_user", {"message": "hi",
                                     "service": "mobile_app_pixel"}))
    assert bridge.calls[0][:2] == ("notify", "mobile_app_pixel")


def test_notify_user_strips_control_characters():
    agent, bridge = steward()
    agent.handle(env("notify_user", {"message": "lineone\nline two"}))
    assert bridge.calls[0][2]["message"] == "line one line two"


def test_notify_user_caps_message_length():
    agent, bridge = steward()
    agent.handle(env("notify_user", {"message": "x" * 5000}))
    assert len(bridge.calls[0][2]["message"]) == 500


@pytest.mark.parametrize("service", ["../../auth", "notify/persistent", 42,
                                     "Persistent_Notification"])
def test_notify_user_rejects_a_bad_service_before_any_call(service):
    agent, bridge = steward()
    reply = agent.handle(env("notify_user", {"message": "hi",
                                             "service": service}))
    assert reply["ok"] is False
    assert bridge.calls == []


@pytest.mark.parametrize("message", [None, "", "   ", 42, ["hi"]])
def test_notify_user_rejects_a_bad_message_before_any_call(message):
    agent, bridge = steward()
    reply = agent.handle(env("notify_user", {"message": message}))
    assert reply["ok"] is False
    assert bridge.calls == []


# ── announce ────────────────────────────────────────────────────────

def test_announce_uses_assist_satellite_announce():
    bridge = StubBridge(states={"assist_satellite.voice": {"state": "idle"}})
    agent, _ = steward(bridge)
    e = env("announce", {"text": "Someone is at the shed",
                         "entity_id": "assist_satellite.voice"})
    reply = agent.handle(e)

    assert reply["ok"] is True
    assert reply["service"] == "assist_satellite.announce"
    assert bridge.calls == [
        ("assist_satellite", "announce",
         {"message": "Someone is at the shed",
          "entity_id": "assist_satellite.voice"})]
    assert e.budget.actions_used == 1


def test_announce_discovers_the_satellite_when_none_is_given(monkeypatch):
    bridge = StubBridge(states={"assist_satellite.found": {"state": "idle"}})
    monkeypatch.setattr(
        bridge, "list_devices",
        lambda domain=None, controllable_only=True: [
            StubDevice("assist_satellite.dead", "unavailable"),
            StubDevice("assist_satellite.found", "idle"),
        ])
    agent, _ = steward(bridge)
    reply = agent.handle(env("announce", {"text": "hello"}))

    assert reply["entity_id"] == "assist_satellite.found"
    assert bridge.calls[0][2]["entity_id"] == "assist_satellite.found"


def test_announce_without_a_satellite_is_refused_with_no_call():
    agent, bridge = steward()
    reply = agent.handle(env("announce", {"text": "hello"}))
    assert reply["ok"] is False
    assert "no assist_satellite" in reply["error"]
    assert bridge.calls == []


def test_announce_to_a_media_player_uses_music_assistant_with_a_url():
    bridge = StubBridge(states={"media_player.kitchen": {"state": "idle"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("announce", {
        "text": "dinner", "entity_id": "media_player.kitchen",
        "url": MEDIA_URL}))

    assert reply["ok"] is True
    assert bridge.calls == [
        ("music_assistant", "play_announcement",
         {"url": MEDIA_URL, "entity_id": "media_player.kitchen"})]


def test_announce_to_a_media_player_without_a_url_is_refused():
    bridge = StubBridge(states={"media_player.kitchen": {"state": "idle"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("announce", {"text": "dinner",
                                          "entity_id": "media_player.kitchen"}))
    assert reply["ok"] is False
    assert "url" in reply["error"]
    assert bridge.calls == []


def test_announce_to_an_unsupported_domain_is_refused():
    agent, bridge = steward()
    reply = agent.handle(env("announce", {"text": "hi",
                                          "entity_id": "light.kitchen"}))
    assert reply["ok"] is False
    assert bridge.calls == []


# ── announce must not be an unvalidated-URL SSRF primitive ──────────
#
# A url forwarded to HA verbatim is fetched by HA, so an unvalidated string is
# a server-side request forgery primitive with HA's credentials and network
# position. The assertion that matters in each case below is
# ``bridge.calls == []``.

def _announce_url(agent, url):
    return agent.handle(env("announce", {
        "text": "dinner", "entity_id": "media_player.kitchen", "url": url}))


def _media_steward(**kwargs):
    bridge = StubBridge(states={"media_player.kitchen": {"state": "idle"}})
    return steward(bridge, **kwargs)


HOSTILE_URLS = [
    # scheme confusion / local file read on the HA host
    "file:///etc/shadow",
    "file://localhost/etc/passwd",
    "javascript:alert(1)",
    "data:audio/mp3;base64,AAAA",
    "gopher://10.0.0.1:70/_stuff",
    "ftp://10.0.0.1/x.mp3",
    "//evil.example.com/x.mp3",          # scheme-relative, no scheme at all
    "/local/x.mp3",                       # relative, no host
    "http://",                            # no host
    # cloud metadata + link-local
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "https://169.254.170.2/v2/credentials",
    "http://[fe80::1]/x.mp3",
    # loopback, including the shorthand/obfuscated forms a permissive HTTP
    # client resolves to 127.0.0.1 but ``ipaddress`` refuses to parse
    "http://127.0.0.1:8123/api/",
    "http://127.1/x.mp3",
    "http://127.0.1/x.mp3",
    "http://2130706433/x.mp3",            # decimal
    "http://0x7f000001/x.mp3",            # hex
    "http://0177.0.0.1/x.mp3",            # octal
    "http://[::1]:8123/x.mp3",
    "http://localhost:8123/api/states",
    "http://localhost.localdomain/x.mp3",
    # other LAN / reserved / multicast / unspecified
    "http://192.168.50.5/tts/dinner.mp3",
    "http://192.168.50.140:8123/api/",
    "http://172.16.0.1/x.mp3",
    "http://0.0.0.0/x.mp3",
    "http://224.0.0.1/x.mp3",
    "http://255.255.255.255/x.mp3",
    "http://240.0.0.1/x.mp3",
    # intranet name shapes
    "http://nas/x.mp3",
    "http://printer.local/x.mp3",
    "http://vault.internal/x.mp3",
    "http://thing.home.arpa/x.mp3",
    "http://box.lan/x.mp3",
    # credentials in the URL
    "http://admin:hunter2@example.com/x.mp3",
    "http://admin@example.com/x.mp3",
    # header / request splitting and log injection
    "http://example.com/x.mp3\r\nHost: evil",
    "http://example.com/x\x00.mp3",
    "http://exa mple.com/x.mp3",
    # not a string / empty
    "",
    "   ",
]


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_hostile_announce_urls_make_zero_http_calls(url):
    agent, bridge = _media_steward()
    reply = _announce_url(agent, url)

    assert reply["ok"] is False, f"{url!r} was accepted"
    assert bridge.calls == [], f"{url!r} reached Home Assistant"


@pytest.mark.parametrize("url", [None, 12345, ["http://example.com/x.mp3"],
                                 {"url": "http://example.com/x.mp3"}, True])
def test_non_string_announce_urls_make_zero_http_calls(url):
    agent, bridge = _media_steward()
    reply = _announce_url(agent, url)
    assert reply["ok"] is False
    assert bridge.calls == []


def test_an_overlong_announce_url_is_refused():
    from puffpai.agents.steward import MAX_URL_LEN

    agent, bridge = _media_steward()
    reply = _announce_url(agent, "http://example.com/" + "a" * MAX_URL_LEN)

    assert reply["ok"] is False
    assert "exceeds" in reply["error"]
    assert bridge.calls == []
    # 5000-char strings used to pass straight through.
    assert len("http://example.com/" + "a" * MAX_URL_LEN) > MAX_URL_LEN


@pytest.mark.parametrize("url", [
    MEDIA_URL,                                       # the configured house host
    f"https://{MEDIA_HOST}/announcements/a.mp3",
    "http://example.com/x.mp3",                      # a public host
    "https://cdn.example.org/announce/a.mp3",
    "https://example.com.:8443/a.mp3",               # trailing-dot FQDN
    # A name whose labels are all hex digits is still a name — the
    # numeric-label rule must not swallow it.
    "http://abc.def/a.mp3",
])
def test_legitimate_announce_urls_still_work(url):
    agent, bridge = _media_steward()
    reply = _announce_url(agent, url)

    assert reply["ok"] is True, reply.get("error")
    assert bridge.calls[0][2]["url"] == url


def test_a_configured_house_host_is_allowed_even_on_a_private_address():
    """The HA/media host is exempt from the public-address rule, by design."""
    agent, bridge = _media_steward(announce_hosts=("192.168.50.140",))
    reply = _announce_url(agent, "http://192.168.50.140:8123/local/a.mp3")

    assert reply["ok"] is True, reply.get("error")
    assert bridge.calls[0][2]["url"] == "http://192.168.50.140:8123/local/a.mp3"


def test_the_announce_host_allowlist_is_read_from_the_environment(monkeypatch):
    from puffpai.agents.steward import ANNOUNCE_HOSTS_ENV

    monkeypatch.setenv(ANNOUNCE_HOSTS_ENV, "audio.house, 10.9.9.9")
    bridge = StubBridge(states={"media_player.kitchen": {"state": "idle"}})
    agent = StewardAgent(ha_bridge=bridge)          # no injected allow-list

    assert _announce_url(agent, "http://audio.house/a.mp3")["ok"] is True
    assert _announce_url(agent, "http://10.9.9.9/a.mp3")["ok"] is True
    assert _announce_url(agent, "http://10.9.9.10/a.mp3")["ok"] is False


# ── announce cannot reach an arbitrary service ──────────────────────

@pytest.mark.parametrize("domain,entity_id,service", [
    # Opens a live conversation on a house satellite.
    ("assist_satellite", "assist_satellite.voice", "start_conversation"),
    ("assist_satellite", "assist_satellite.voice", "turn_off"),
    # Plays arbitrary media, not an announcement.
    ("media_player", "media_player.kitchen", "play_media"),
    ("media_player", "media_player.kitchen", "transfer_queue"),
])
def test_announce_cannot_reach_an_arbitrary_service(domain, entity_id, service):
    bridge = StubBridge(states={entity_id: {"state": "idle"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("announce", {
        "text": "hi", "entity_id": entity_id, "service": service,
        "url": MEDIA_URL}))

    assert reply["ok"] is False
    assert "must be one of" in reply["error"]
    assert bridge.calls == []


@pytest.mark.parametrize("service", ["", "ANNOUNCE", "announce ", 7, True,
                                     ["announce"], "announce;turn_off"])
def test_announce_service_overrides_are_allowlisted_not_shape_checked(service):
    bridge = StubBridge(states={"assist_satellite.voice": {"state": "idle"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("announce", {
        "text": "hi", "entity_id": "assist_satellite.voice",
        "service": service}))

    assert reply["ok"] is False
    assert bridge.calls == []


def test_the_intended_announce_services_are_still_nameable():
    bridge = StubBridge(states={"assist_satellite.voice": {"state": "idle"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("announce", {
        "text": "hi", "entity_id": "assist_satellite.voice",
        "service": "announce"}))

    assert reply["ok"] is True
    assert bridge.calls == [("assist_satellite", "announce",
                             {"message": "hi",
                              "entity_id": "assist_satellite.voice"})]


def test_a_refused_announce_url_costs_no_budget():
    """Validation happens before metering, so a probe cannot drain the budget."""
    agent, bridge = _media_steward()
    e = env("announce", {"text": "dinner", "entity_id": "media_player.kitchen",
                         "url": "http://169.254.169.254/latest/meta-data/"},
            actions=3)
    agent.handle(e)

    assert e.budget.actions_used == 0
    assert bridge.calls == []


def test_announce_to_an_unavailable_satellite_reports_unavailable():
    bridge = StubBridge(
        states={"assist_satellite.voice": {"state": "unavailable"}})
    agent, _ = steward(bridge)
    e = env("announce", {"text": "hi", "entity_id": "assist_satellite.voice"})
    reply = agent.handle(e)

    assert reply["ok"] is False
    assert reply["unavailable"] is True
    assert reply["entity_id"] == "assist_satellite.voice"
    assert bridge.calls == []
    assert e.budget.actions_used == 0   # an unavailable device costs nothing


@pytest.mark.parametrize("entity_id", TRAVERSAL_IDS)
def test_announce_rejects_a_malformed_entity_id_before_any_call(entity_id):
    agent, bridge = steward()
    reply = agent.handle(env("announce", {"text": "hi",
                                          "entity_id": entity_id}))
    assert reply["ok"] is False
    assert bridge.calls == [] and bridge.image_calls == []


# ── camera_snapshot ─────────────────────────────────────────────────

def test_camera_snapshot_fetches_through_the_camera_proxy():
    bridge = StubBridge(states={"camera.shed": {"state": "idle"}})
    agent, _ = steward(bridge)
    e = env("camera_snapshot", {"entity_id": "camera.shed"},
            state_changing=False)
    reply = agent.handle(e)

    assert reply["ok"] is True
    assert reply["source"] == "camera_proxy"
    assert reply["content_type"] == "image/jpeg"
    assert reply["bytes"] == len(b"\xff\xd8jpegbytes")
    assert "image_b64" not in reply
    assert bridge.image_calls == ["camera.shed"]
    assert bridge.calls == []           # never HA's camera.snapshot service
    assert e.budget.actions_used == 1


def test_camera_snapshot_can_return_the_image():
    bridge = StubBridge(states={"camera.shed": {"state": "idle"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("camera_snapshot",
                             {"entity_id": "camera.shed", "include_image": True},
                             state_changing=False))
    assert base64.b64decode(reply["image_b64"]) == b"\xff\xd8jpegbytes"


def test_camera_snapshot_rejects_a_non_camera_entity():
    agent, bridge = steward()
    reply = agent.handle(env("camera_snapshot", {"entity_id": "light.kitchen"},
                             state_changing=False))
    assert reply["ok"] is False
    assert bridge.image_calls == []


def test_camera_snapshot_of_an_offline_camera_reports_unavailable():
    bridge = StubBridge(states={"camera.shed": {"state": "unavailable"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("camera_snapshot", {"entity_id": "camera.shed"},
                             state_changing=False))
    assert reply["ok"] is False and reply["unavailable"] is True
    assert bridge.image_calls == []


# ── set_climate ─────────────────────────────────────────────────────

def test_set_climate_sets_temperature():
    bridge = StubBridge(states={"climate.hall": {"state": "cool"}})
    agent, _ = steward(bridge)
    e = env("set_climate", {"entity_id": "climate.hall", "temperature": 72})
    reply = agent.handle(e)

    assert reply["ok"] is True
    assert bridge.calls == [
        ("climate", "set_temperature",
         {"temperature": 72.0, "entity_id": "climate.hall"})]
    assert e.budget.actions_used == 1


def test_set_climate_sets_mode_then_temperature():
    bridge = StubBridge(states={"climate.hall": {"state": "cool"}})
    agent, _ = steward(bridge)
    e = env("set_climate", {"entity_id": "climate.hall", "temperature": 68,
                            "hvac_mode": "cool"})
    reply = agent.handle(e)

    assert reply["services"] == ["climate.set_hvac_mode",
                                 "climate.set_temperature"]
    assert [c[1] for c in bridge.calls] == ["set_hvac_mode", "set_temperature"]
    assert bridge.calls[0][2] == {"hvac_mode": "cool",
                                  "entity_id": "climate.hall"}
    assert e.budget.actions_used == 2


def test_set_climate_mode_only_uses_set_hvac_mode():
    bridge = StubBridge(states={"climate.hall": {"state": "cool"}})
    agent, _ = steward(bridge)
    agent.handle(env("set_climate", {"entity_id": "climate.hall",
                                     "hvac_mode": "off"}))
    assert bridge.calls == [
        ("climate", "set_hvac_mode",
         {"hvac_mode": "off", "entity_id": "climate.hall"})]


@pytest.mark.parametrize("temperature", [49, 91, 300, -10, "72", True, None])
def test_set_climate_refuses_an_out_of_band_temperature(temperature):
    bridge = StubBridge(states={"climate.hall": {"state": "cool"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("set_climate", {"entity_id": "climate.hall",
                                             "temperature": temperature}))
    assert reply["ok"] is False
    assert bridge.calls == []


@pytest.mark.parametrize("mode", ["ludicrous", "COOL", 3, ""])
def test_set_climate_refuses_an_unknown_hvac_mode(mode):
    bridge = StubBridge(states={"climate.hall": {"state": "cool"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("set_climate", {"entity_id": "climate.hall",
                                             "hvac_mode": mode}))
    assert reply["ok"] is False
    assert bridge.calls == []


def test_set_climate_rejects_a_non_climate_entity():
    agent, bridge = steward()
    reply = agent.handle(env("set_climate", {"entity_id": "light.kitchen",
                                             "temperature": 70}))
    assert reply["ok"] is False
    assert bridge.calls == []


def test_set_climate_on_an_offline_thermostat_reports_unavailable():
    bridge = StubBridge(states={"climate.hall": {"state": "unavailable"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("set_climate", {"entity_id": "climate.hall",
                                             "temperature": 70}))
    assert reply["ok"] is False and reply["unavailable"] is True
    assert bridge.calls == []


def test_set_climate_budget_abort_stops_before_the_second_call():
    bridge = StubBridge(states={"climate.hall": {"state": "cool"}})
    agent, _ = steward(bridge)
    e = env("set_climate", {"entity_id": "climate.hall", "temperature": 70,
                            "hvac_mode": "heat"}, actions=1)
    with pytest.raises(BudgetExceeded):
        agent.handle(e)
    assert [c[1] for c in bridge.calls] == ["set_hvac_mode"]


# ── siren (always-confirm class) ────────────────────────────────────

def test_siren_is_declared_state_changing_and_confirm_required():
    from puffpai.agents.steward import CONFIRM_REQUIRED_INTENTS
    assert "siren" in STATE_CHANGING_INTENTS
    assert "siren" in CONFIRM_REQUIRED_INTENTS


def test_siren_without_confirmation_only_proposes():
    bridge = StubBridge(states={"siren.shed": {"state": "off"}})
    agent, _ = steward(bridge)
    e = env("siren", {"entity_id": "siren.shed"})
    reply = agent.handle(e)

    assert reply["ok"] is True
    assert reply["executed"] is False
    assert reply["requires_confirmation"] is True
    assert reply["proposal"] == {"service": "siren.turn_on",
                                 "entity_id": "siren.shed"}
    assert bridge.calls == []
    assert e.budget.actions_used == 0


def test_siren_confirmed_by_an_agent_alone_does_not_fire():
    """An agent can set confirmed=True; it cannot claim a human origin."""
    bridge = StubBridge(states={"siren.shed": {"state": "off"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("siren", {"entity_id": "siren.shed",
                                       "confirmed": True},
                             origin="concierge"))
    assert reply["executed"] is False
    assert bridge.calls == []


def test_siren_from_a_human_origin_without_the_flag_does_not_fire():
    bridge = StubBridge(states={"siren.shed": {"state": "off"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("siren", {"entity_id": "siren.shed"},
                             origin="human"))
    assert reply["executed"] is False
    assert bridge.calls == []


@pytest.mark.parametrize("confirmed", ["true", 1, "yes", None])
def test_siren_requires_a_real_boolean_confirmation(confirmed):
    bridge = StubBridge(states={"siren.shed": {"state": "off"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("siren", {"entity_id": "siren.shed",
                                       "confirmed": confirmed},
                             origin="human"))
    assert reply["executed"] is False
    assert bridge.calls == []


def test_siren_fires_on_a_human_confirmed_envelope():
    bridge = StubBridge(states={"siren.shed": {"state": "off"}})
    agent, _ = steward(bridge)
    e = env("siren", {"entity_id": "siren.shed", "confirmed": True},
            origin="human")
    reply = agent.handle(e)

    assert reply["ok"] is True and reply["executed"] is True
    assert bridge.calls == [("siren", "turn_on", {"entity_id": "siren.shed"})]
    assert e.budget.actions_used == 1


def test_siren_turn_off_is_supported():
    bridge = StubBridge(states={"siren.shed": {"state": "on"}})
    agent, _ = steward(bridge)
    agent.handle(env("siren", {"entity_id": "siren.shed", "action": "turn_off",
                               "confirmed": True}, origin="owner"))
    assert bridge.calls == [("siren", "turn_off", {"entity_id": "siren.shed"})]


@pytest.mark.parametrize("action", ["blare", "toggle", "", 1])
def test_siren_rejects_an_unknown_action(action):
    bridge = StubBridge(states={"siren.shed": {"state": "off"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("siren", {"entity_id": "siren.shed",
                                       "action": action, "confirmed": True},
                             origin="human"))
    assert reply["ok"] is False
    assert bridge.calls == []


def test_siren_rejects_a_non_siren_entity_even_when_confirmed():
    agent, bridge = steward()
    reply = agent.handle(env("siren", {"entity_id": "light.kitchen",
                                       "confirmed": True}, origin="human"))
    assert reply["ok"] is False
    assert bridge.calls == []


@pytest.mark.parametrize("entity_id", TRAVERSAL_IDS)
def test_siren_rejects_a_malformed_entity_id_before_any_call(entity_id):
    agent, bridge = steward()
    reply = agent.handle(env("siren", {"entity_id": entity_id,
                                       "confirmed": True}, origin="human"))
    assert reply["ok"] is False
    assert bridge.calls == []


def test_siren_on_an_offline_siren_reports_unavailable():
    bridge = StubBridge(states={"siren.shed": {"state": "unavailable"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("siren", {"entity_id": "siren.shed",
                                       "confirmed": True}, origin="human"))
    assert reply["ok"] is False and reply["unavailable"] is True
    assert bridge.calls == []


# ── list_add ────────────────────────────────────────────────────────

def test_list_add_defaults_to_the_shopping_list():
    agent, bridge = steward()
    e = env("list_add", {"item": "oat milk"})
    reply = agent.handle(e)

    assert reply["ok"] is True
    assert bridge.calls == [("shopping_list", "add_item", {"name": "oat milk"})]
    assert e.budget.actions_used == 1


def test_list_add_uses_todo_add_item_for_a_todo_entity():
    bridge = StubBridge(states={"todo.chores": {"state": "3"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("list_add", {"item": "water plants",
                                          "entity_id": "todo.chores"}))
    assert reply["service"] == "todo.add_item"
    assert bridge.calls == [
        ("todo", "add_item",
         {"item": "water plants", "entity_id": "todo.chores"})]


def test_list_add_todo_without_an_entity_is_refused():
    agent, bridge = steward()
    reply = agent.handle(env("list_add", {"item": "x", "list": "todo"}))
    assert reply["ok"] is False
    assert bridge.calls == []


def test_list_add_rejects_an_unknown_list():
    agent, bridge = steward()
    reply = agent.handle(env("list_add", {"item": "x", "list": "groceries"}))
    assert reply["ok"] is False
    assert bridge.calls == []


@pytest.mark.parametrize("item", [None, "", "  ", 42, {"a": 1}])
def test_list_add_rejects_a_bad_item_before_any_call(item):
    agent, bridge = steward()
    reply = agent.handle(env("list_add", {"item": item}))
    assert reply["ok"] is False
    assert bridge.calls == []


def test_list_add_caps_and_cleans_the_item_text():
    agent, bridge = steward()
    agent.handle(env("list_add", {"item": "milk \n" + "x" * 400}))
    name = bridge.calls[0][2]["name"]
    assert len(name) == 255
    assert name.startswith("milk xxx")
    assert chr(10) not in name and chr(9) not in name


def test_list_add_to_an_unavailable_todo_reports_unavailable():
    bridge = StubBridge(states={"todo.chores": {"state": "unavailable"}})
    agent, _ = steward(bridge)
    reply = agent.handle(env("list_add", {"item": "x",
                                          "entity_id": "todo.chores"}))
    assert reply["ok"] is False and reply["unavailable"] is True
    assert bridge.calls == []


# ── HA failures degrade, never crash ────────────────────────────────

def test_an_ha_error_becomes_a_handled_failure():
    class Broken(StubBridge):
        def call_service(self, *a, **kw):
            raise HABridgeError("Cannot connect to Home Assistant")

    agent, _ = steward(Broken())
    reply = agent.handle(env("notify_user", {"message": "hi"}))
    assert reply["ok"] is False
    assert "home assistant" in reply["error"]


def test_an_unknown_entity_becomes_a_handled_failure():
    class Missing(StubBridge):
        def get_entity_state(self, entity_id):
            raise HABridgeError("HA returned HTTP 404: ")

    agent, bridge = steward(Missing())
    reply = agent.handle(env("siren", {"entity_id": "siren.nope",
                                       "confirmed": True}, origin="human"))
    assert reply["ok"] is False
    assert bridge.calls == []
