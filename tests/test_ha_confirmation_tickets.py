"""The HTTP half of the always-confirm gate: refusal shape, and the way out.

The gate lives inside :class:`~puffpai.ha_bridge.HABridge`, so nothing reaching
Home Assistant can skip it. That needs two things at the HTTP layer, and this
file pins both:

1. **The refusal is a 409, not a 502.** ``ConfirmationRequired`` subclasses
   ``HABridgeError``, so without its own branch it falls through
   ``_ha_error_response``'s generic tail and is reported as "the upstream
   broke". A refusal to blind the house's only camera is the system working
   correctly; calling it an upstream fault invites a retry loop. The 409
   carries the proposal verbatim.

2. **A person needs a way to say yes.** The API key authenticates a
   *client*, and the appliance's own agent stack holds the same key — a
   prompt-injected LLM presents byte-identical credentials to a human at the
   dashboard. The confirmation-ticket flow adds something a single injected tool
   call cannot produce: a fresh, server-generated, unguessable value obtained in
   a *previous* request and presented back bound to the same action.

The worked example throughout is ``camera.turn_off`` — turning off the only
witness the house has — with ``cover`` (a garage door) as the physical case.
**Every entity id in this file is fabricated and nothing here may reach a real
Home Assistant.** The bridge is a real ``HABridge`` subclass whose ``_request``
— the one method that would open a socket — is replaced by a recorder, so "the
gate leaked" and "the test touched a house" are the same observable event: an
entry in ``bridge.wire``. Tests assert on that list directly, so a passing test
is evidence that zero calls were made, not merely that a mock was not asserted
against.

Run: pytest tests/test_ha_confirmation_tickets.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from tests.modpatch import patched_modules

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai.ha_bridge import (  # noqa: E402
    CONFIRMATION_KEYWORD,
    HUMAN_CONFIRMED,
    HABridge,
)

# Fabricated entities. None of these exist in any real house.
CAMERA = "camera.test_hallway_cam"
CAMERA_2 = "camera.test_porch_cam"
COVER = "cover.test_garage_door"
LIGHT = "light.test_porch_light"
SCENE_PLAIN = "scene.test_movie_night"
SCENE_CRITICAL = "scene.test_lockdown"


# ══════════════════════════════════════════════════════════════════════
#  A bridge that cannot reach a real house
# ══════════════════════════════════════════════════════════════════════

class WireTrap(HABridge):
    """A real :class:`HABridge` with the wire cut and a recorder in its place.

    Subclassing the real class rather than duck-typing it is the point: the gate
    under test is the real gate, in the real methods, with the real argument
    handling. Only ``_request`` is replaced.
    """

    def __init__(self, states=None):
        # Skip HABridge.__init__: it reads config and builds a requests.Session.
        self.wire: list = []
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
            return _Resp([{"entity_id": eid, **state}
                          for eid, state in self._states.items()])
        if endpoint.startswith("states/"):
            return _Resp(self._states.get(endpoint.split("/", 1)[1], {}))
        return _Resp([])

    @property
    def service_calls(self):
        """Only the calls that would actuate — POSTs to ``services/…``."""
        return [(e, m, d) for e, m, d in self.wire
                if e.startswith("services/") and m == "POST"]

    @property
    def config_writes(self):
        """POSTs to the config API — deferred actuation (automations)."""
        return [(e, m, d) for e, m, d in self.wire
                if e.startswith("config/") and m == "POST"]


def scene_state(members):
    return {"state": "on", "attributes": {"entity_id": list(members)}}


DEFAULT_STATES = {
    CAMERA: {"state": "recording", "attributes": {}},
    CAMERA_2: {"state": "recording", "attributes": {}},
    COVER: {"state": "closed", "attributes": {}},
    LIGHT: {"state": "off", "attributes": {}},
    SCENE_PLAIN: scene_state([LIGHT]),
    SCENE_CRITICAL: scene_state([LIGHT, COVER]),
}


# ══════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def api_mod():
    """Import api_server once, with the firewall backend mocked out."""
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        mod = importlib.import_module("puffpai.api_server")
    mod.app.config["TESTING"] = True
    return mod


@pytest.fixture
def wired(api_mod, monkeypatch):
    """A test client whose ``/ha/*`` routes talk to a trapped bridge.

    Yields ``(client, headers, bridge, mod)``. The ticket store is emptied
    before and after every test so no ticket leaks between cases — which also
    stands in for the restart guarantee: the store is a plain in-memory list and
    emptying it is the whole of "the tickets are gone".
    """
    bridge = WireTrap(states=dict(DEFAULT_STATES))
    monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
    api_mod._rate_limit_store.clear()
    api_mod._ha_tickets.clear()
    headers = {"X-API-Key": api_mod._API_KEY}
    with api_mod.app.test_client() as client:
        yield client, headers, bridge, api_mod
    api_mod._ha_tickets.clear()


@pytest.fixture
def consent_allowed(monkeypatch):
    """Neutralise the *other* gate so this file tests only the confirmation one.

    ``/ha/automations`` and the agent creators run the Sentinel per-device
    consent gate first, which denies unapproved entities with **403** before the
    confirmation gate is ever reached. That ordering is correct — an unapproved
    device is a harder no than an unconfirmed one — but it would mask what this
    file is about, and every entity here is fabricated so none of them is
    approved. Stubbed to "allow" so the assertions below are about the
    confirmation gate and nothing else.
    """
    from puffpai.security import consent_gate
    allow = {"action": "allow", "binding": False, "reasons": [],
             "targets": [], "automation_name": ""}
    monkeypatch.setattr(consent_gate, "gate_targets",
                        lambda *a, **k: dict(allow))
    monkeypatch.setattr(consent_gate, "sentinel_review",
                        lambda *a, **k: dict(allow))
    return allow


@pytest.fixture(autouse=True)
def _review_log_in_tmp(monkeypatch, tmp_path):
    """gate_targets / sentinel_review audit every verdict to
    sentinel_reviews.jsonl (consent_gate.REVIEW_LOG) — the real data dir on a
    dev checkout. ``consent_allowed`` stubs the gate where these tests reach
    it, but isolation must not depend on that: pin the log to tmp."""
    import puffpai.security.consent_gate as cg
    monkeypatch.setattr(cg, "REVIEW_LOG", str(tmp_path / "reviews.jsonl"))


def mint(client, headers, *, entity_id, action, kind="control"):
    """Mint a ticket and return the parsed body."""
    resp = client.post("/ha/confirm", headers=headers,
                       json={"kind": kind, "entity_id": entity_id,
                             "action": action})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


# ══════════════════════════════════════════════════════════════════════
#  1. The refusal: 409, a proposal, and nothing on the wire
# ══════════════════════════════════════════════════════════════════════

class TestRefusalShape:

    def test_camera_turn_off_refused_with_409_and_proposal(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off"})

        assert resp.status_code == 409, (
            "a refusal to blind the house's only witness is the system working; "
            "502 would report it as an upstream fault and invite a retry")
        body = resp.get_json()
        assert body["needs_confirmation"] is True
        assert body["success"] is False
        assert body["confirm_endpoint"] == "/ha/confirm"

        proposal = body["proposal"]
        assert proposal["entity_id"] == CAMERA
        assert proposal["service"] == "camera.turn_off"
        assert proposal["domains"] == ["camera"]
        assert body["message"]

        # The whole point: nothing was sent.
        assert bridge.wire == [], "a refused actuation reached Home Assistant"

    def test_refusal_is_not_reported_as_upstream_failure(self, wired):
        client, headers, _, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": COVER, "action": "turn_on"})
        assert resp.status_code not in (400, 500, 502, 503, 504)
        assert resp.status_code == 409

    def test_confirmation_branch_wins_over_string_classifier(self, api_mod):
        """Type first, string-matching never.

        ``ConfirmationRequired`` subclasses ``HABridgeError`` and its message is
        assembled partly from caller-supplied identifiers, so a message that
        happens to start with "invalid " (or contain "connect") must not be able
        to steer the refusal into a 400 or a 502.
        """
        from puffpai.ha_bridge import ConfirmationRequired
        exc = ConfirmationRequired(
            {"service": "camera.turn_off", "entity_id": CAMERA,
             "domains": ["camera"]},
            "invalid entity could not connect and timed out")
        with api_mod.app.test_request_context("/ha/control"):
            resp, code = api_mod._ha_error_response(exc)
        assert code == 409
        assert resp.get_json()["needs_confirmation"] is True

    def test_plain_bridge_errors_keep_their_old_codes(self, api_mod):
        from puffpai.ha_bridge import HABridgeError
        with api_mod.app.test_request_context("/ha/control"):
            _, invalid = api_mod._ha_error_response(HABridgeError("invalid x"))
            _, timeout = api_mod._ha_error_response(HABridgeError("timed out"))
            _, other = api_mod._ha_error_response(HABridgeError("boom"))
        assert (invalid, timeout, other) == (400, 504, 502)


# ══════════════════════════════════════════════════════════════════════
#  2. Minting is inert
# ══════════════════════════════════════════════════════════════════════

class TestMint:

    def test_mint_does_not_actuate_or_even_read(self, wired):
        client, headers, bridge, _ = wired
        body = mint(client, headers, entity_id=CAMERA, action="turn_off")
        assert body["ticket"]
        assert body["single_use"] is True
        assert body["expires_in"] == 90
        assert bridge.wire == [], "minting a ticket contacted Home Assistant"

    def test_mint_echoes_the_exact_proposal_it_binds(self, wired):
        client, headers, _, _ = wired
        body = mint(client, headers, entity_id=CAMERA, action="TURN_OFF")
        proposal = body["proposal"]
        assert proposal["entity_id"] == CAMERA
        assert proposal["action"] == "turn_off"
        assert proposal["domains"] == ["camera"]
        assert proposal["safety_critical"] is True
        assert proposal["kind"] == "control"

    def test_ticket_is_long_and_unguessable(self, wired):
        client, headers, _, _ = wired
        seen = set()
        for _ in range(5):
            body = mint(client, headers, entity_id=CAMERA, action="turn_off")
            assert len(body["ticket"]) >= 32
            seen.add(body["ticket"])
        assert len(seen) == 5, "tickets repeated — they are not random"

    def test_mint_requires_an_explicit_action(self, wired):
        client, headers, _, _ = wired
        resp = client.post("/ha/confirm", headers=headers,
                           json={"entity_id": CAMERA})
        assert resp.status_code == 400

    def test_mint_rejects_an_unknown_kind(self, wired):
        client, headers, _, _ = wired
        resp = client.post("/ha/confirm", headers=headers,
                           json={"kind": "anything", "entity_id": CAMERA,
                                 "action": "turn_off"})
        assert resp.status_code == 400

    def test_mint_needs_the_api_key(self, wired):
        client, _, _, _ = wired
        resp = client.post("/ha/confirm",
                           json={"entity_id": CAMERA, "action": "turn_off"})
        assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════
#  3. A valid ticket authorises exactly its own action
# ══════════════════════════════════════════════════════════════════════

class TestRedemption:

    def test_valid_ticket_permits_its_own_action(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 "ticket": ticket})
        assert resp.status_code == 200, resp.get_json()
        assert len(bridge.service_calls) == 1
        endpoint, _method, data = bridge.service_calls[0]
        assert endpoint == "services/camera/turn_off"
        assert data["entity_id"] == CAMERA

    def test_redeemed_ticket_never_reaches_home_assistant(self, wired):
        """The ticket and the gate parameter are ours; HA must not see them."""
        client, headers, bridge, _ = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        client.post("/ha/control", headers=headers,
                    json={"entity_id": CAMERA, "action": "turn_off",
                          "ticket": ticket})
        _endpoint, _method, data = bridge.service_calls[0]
        assert "ticket" not in data
        assert CONFIRMATION_KEYWORD not in data
        assert ticket not in str(data)

    def test_ticket_for_action_a_is_rejected_for_action_b(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_on",
                                 "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_ticket_for_one_camera_does_not_authorise_another(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA_2, "action": "turn_off",
                                 "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_mismatched_ticket_is_burned_not_returned(self, wired):
        """Presenting a ticket against the wrong action spends it.

        Fail closed: a valid ticket aimed at an action it was not minted for is
        misuse, not a typo. The holder starts over in front of the person.
        """
        client, headers, bridge, _ = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        client.post("/ha/control", headers=headers,
                    json={"entity_id": CAMERA, "action": "turn_on",
                          "ticket": ticket})
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_replay_is_rejected(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        first = client.post("/ha/control", headers=headers,
                            json={"entity_id": CAMERA, "action": "turn_off",
                                  "ticket": ticket})
        second = client.post("/ha/control", headers=headers,
                             json={"entity_id": CAMERA, "action": "turn_off",
                                   "ticket": ticket})
        assert first.status_code == 200
        assert second.status_code == 409
        assert len(bridge.service_calls) == 1, "the replay actuated"

    def test_expired_ticket_is_rejected(self, wired):
        client, headers, bridge, mod = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        # Age the ticket past its deadline without sleeping.
        mod._ha_tickets[-1]["expires_at"] = time.monotonic() - 1
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_expiry_verdict_is_distinguishable(self, wired):
        _client, _headers, _bridge, mod = wired
        secret, record = mod._ha_mint_ticket("control", CAMERA, "turn_off")
        record["expires_at"] = time.monotonic() - 1
        ok, verdict = mod._ha_redeem_ticket(
            secret, kind="control", entity_id=CAMERA, action="turn_off")
        assert (ok, verdict) == (False, "expired")

    @pytest.mark.parametrize("forged", [
        "not-a-real-ticket",
        "",
        "A" * 43,
        "../../etc/passwd",
        "<script>alert(1)</script>",
    ])
    def test_forged_or_garbage_tickets_are_rejected(self, wired, forged):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 "ticket": forged})
        assert resp.status_code == 409
        assert bridge.wire == []

    @pytest.mark.parametrize("junk", [True, 1, None, {"a": 1}, ["x"]])
    def test_non_string_tickets_are_rejected(self, wired, junk):
        """A ticket must be a string. ``true`` is not a ticket."""
        client, headers, bridge, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 "ticket": junk})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_ticket_does_not_survive_a_restart(self, wired):
        """The store is in memory only; there is no persistence path.

        Clearing the module-level list is the whole of what a restart does to a
        ticket, so this is the property, not an approximation of it.
        """
        client, headers, bridge, mod = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        mod._ha_tickets.clear()          # stand-in for process restart
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.wire == []


# ══════════════════════════════════════════════════════════════════════
#  4. The splat regression — the specific line that made this necessary
# ══════════════════════════════════════════════════════════════════════

class TestSplatRegression:
    """``/ha/control`` splats unrecognised body keys into ``control_device``.

    That means a body key named after the gate parameter *binds to the gate
    parameter*. ha_bridge already refuses it by object identity; api_server must
    also never forward it. Two independent barriers, because the day someone
    changes one of them the other has to still be there.
    """

    @pytest.mark.parametrize("key", [
        "human_confirmed", "confirmed", "confirm", "confirmation",
        "Human_Confirmed", "HUMAN_CONFIRMED", "human_confirm", "is_human",
        "force",
    ])
    def test_confirmation_keys_in_the_body_never_open_the_gate(self, wired, key):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 key: True})
        assert resp.status_code == 409, f"{key!r} in the body opened the gate"
        assert bridge.wire == []

    def test_the_string_HUMAN_CONFIRMED_is_not_the_token(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA, "action": "turn_off",
                                 "human_confirmed": "<HUMAN_CONFIRMED>"})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_reserved_keys_never_reach_home_assistant_as_service_data(self, wired):
        """Even on a benign device the keys are dropped, not forwarded."""
        client, headers, bridge, _ = wired
        client.post("/ha/control", headers=headers,
                    json={"entity_id": LIGHT, "action": "turn_on",
                          "human_confirmed": True, "confirmed": True,
                          "ticket": "x", "brightness_pct": 40})
        _endpoint, _method, data = bridge.service_calls[0]
        assert data["brightness_pct"] == 40, "legitimate service data was lost"
        for key in ("human_confirmed", "confirmed", "ticket"):
            assert key not in data

    def test_strip_helper_drops_every_spelling(self, api_mod):
        stripped = api_mod._ha_strip_reserved(
            {"entity_id": CAMERA, "action": "turn_off", "human_confirmed": True,
             " Confirmed ": 1, "TICKET": "x", "brightness_pct": 40},
            also=("entity_id", "action"))
        assert stripped == {"brightness_pct": 40}

    def test_the_bridges_keyword_is_in_the_reserved_set(self, api_mod):
        """Drift alarm.

        If ha_bridge renames its confirmation parameter, this fails rather than
        api_server silently starting to forward the new name into the splat.
        """
        assert CONFIRMATION_KEYWORD in api_mod._HA_RESERVED_BODY_KEYS

    def test_only_the_token_object_is_accepted(self, api_mod):
        """The value api_server passes on success is the token itself."""
        from puffpai.ha_bridge import is_human_confirmation
        assert is_human_confirmation(HUMAN_CONFIRMED)
        assert not is_human_confirmation(True)
        assert not is_human_confirmation("<HUMAN_CONFIRMED>")


# ══════════════════════════════════════════════════════════════════════
#  5. Everything that must NOT have changed
# ══════════════════════════════════════════════════════════════════════

class TestUnaffected:

    def test_ordinary_light_needs_no_ticket(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/control", headers=headers,
                           json={"entity_id": LIGHT, "action": "turn_on"})
        assert resp.status_code == 200
        assert len(bridge.service_calls) == 1

    def test_light_service_data_still_passes_through(self, wired):
        client, headers, bridge, _ = wired
        client.post("/ha/control", headers=headers,
                    json={"entity_id": LIGHT, "action": "turn_on",
                          "brightness_pct": 55})
        _e, _m, data = bridge.service_calls[0]
        assert data["brightness_pct"] == 55

    def test_plain_scene_needs_no_ticket(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post(f"/ha/scenes/{SCENE_PLAIN}/activate", headers=headers)
        assert resp.status_code == 200
        assert bridge.service_calls

    def test_non_critical_service_call_unaffected(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/service/light/turn_on", headers=headers,
                           json={"entity_id": LIGHT})
        assert resp.status_code == 200
        assert len(bridge.service_calls) == 1

    def test_reads_are_unaffected(self, wired):
        client, headers, bridge, _ = wired
        assert client.get("/ha/entities", headers=headers).status_code == 200
        assert client.get(f"/ha/entity/{CAMERA}",
                          headers=headers).status_code == 200
        assert client.get("/ha/devices", headers=headers).status_code == 200
        assert bridge.service_calls == [], "a read actuated something"

    def test_reading_a_camera_image_is_not_gated(self, wired):
        """Looking through the camera is not the same as switching it off."""
        client, headers, _, _ = wired
        assert client.get(f"/ha/camera/{CAMERA}",
                          headers=headers).status_code == 200


# ══════════════════════════════════════════════════════════════════════
#  6. The other actuating routes
# ══════════════════════════════════════════════════════════════════════

class TestServiceRoute:

    def test_camera_turn_off_via_service_route_refused(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/service/camera/turn_off", headers=headers,
                           json={"entity_id": CAMERA})
        assert resp.status_code == 409
        assert resp.get_json()["needs_confirmation"] is True
        assert bridge.wire == []

    def test_service_ticket_permits_that_service(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="service", entity_id=CAMERA,
                      action="camera.turn_off")["ticket"]
        resp = client.post("/ha/service/camera/turn_off", headers=headers,
                           json={"entity_id": CAMERA, "ticket": ticket})
        assert resp.status_code == 200, resp.get_json()
        assert len(bridge.service_calls) == 1

    def test_a_control_ticket_does_not_redeem_on_the_service_route(self, wired):
        """Consent is bound to the route family the person was shown."""
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="control", entity_id=CAMERA,
                      action="turn_off")["ticket"]
        resp = client.post("/ha/service/camera/turn_off", headers=headers,
                           json={"entity_id": CAMERA, "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_service_ticket_does_not_authorise_a_different_service(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="service", entity_id=CAMERA,
                      action="camera.turn_off")["ticket"]
        resp = client.post("/ha/service/camera/turn_on", headers=headers,
                           json={"entity_id": CAMERA, "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.wire == []

    def test_confirmation_key_in_service_body_is_stripped(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/service/camera/turn_off", headers=headers,
                           json={"entity_id": CAMERA, "human_confirmed": True})
        assert resp.status_code == 409
        assert bridge.wire == []


class TestSceneRoute:

    def test_scene_containing_a_cover_is_refused(self, wired):
        client, headers, bridge, _ = wired
        resp = client.post(f"/ha/scenes/{SCENE_CRITICAL}/activate",
                           headers=headers)
        assert resp.status_code == 409
        assert resp.get_json()["proposal"]["domains"] == ["cover"]
        assert bridge.service_calls == []

    def test_scene_ticket_permits_that_scene(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="scene",
                      entity_id=SCENE_CRITICAL, action="activate")["ticket"]
        resp = client.post(f"/ha/scenes/{SCENE_CRITICAL}/activate",
                           headers=headers, json={"ticket": ticket})
        assert resp.status_code == 200, resp.get_json()
        assert len(bridge.service_calls) == 1

    def test_scene_ticket_does_not_authorise_another_scene(self, wired):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="scene",
                      entity_id=SCENE_PLAIN, action="activate")["ticket"]
        resp = client.post(f"/ha/scenes/{SCENE_CRITICAL}/activate",
                           headers=headers, json={"ticket": ticket})
        assert resp.status_code == 409
        assert bridge.service_calls == []


class TestAutomationRoute:
    """Writing an instruction to actuate later is gated like actuating."""

    CONFIG = {
        "alias": "Test blind the hallway at midnight",
        "trigger": [{"platform": "time", "at": "00:00:00"}],
        "action": [{"service": "camera.turn_off",
                    "target": {"entity_id": CAMERA}}],
    }

    def test_safety_critical_automation_is_refused_with_409(self, wired, consent_allowed):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/automations", headers=headers,
                           json=dict(self.CONFIG))
        assert resp.status_code == 409, resp.get_json()
        body = resp.get_json()
        assert body["needs_confirmation"] is True
        assert body["proposal"]["entity_id"] == self.CONFIG["alias"]
        assert "camera" in body["proposal"]["domains"]
        assert bridge.config_writes == [], "the automation was written anyway"

    def test_automation_ticket_permits_that_automation(self, wired, consent_allowed):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="automation",
                      entity_id=self.CONFIG["alias"], action="create")["ticket"]
        resp = client.post("/ha/automations", headers=headers,
                           json={**self.CONFIG, "ticket": ticket})
        assert resp.status_code == 201, resp.get_json()
        assert len(bridge.config_writes) == 1

    def test_the_ticket_is_not_written_into_the_stored_automation(self, wired, consent_allowed):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="automation",
                      entity_id=self.CONFIG["alias"], action="create")["ticket"]
        client.post("/ha/automations", headers=headers,
                    json={**self.CONFIG, "ticket": ticket,
                          "human_confirmed": True})
        _endpoint, _method, data = bridge.config_writes[0]
        assert "ticket" not in data
        assert "human_confirmed" not in data
        assert ticket not in str(data)

    def test_automation_ticket_for_a_different_alias_is_rejected(self, wired, consent_allowed):
        client, headers, bridge, _ = wired
        ticket = mint(client, headers, kind="automation",
                      entity_id="Something else entirely",
                      action="create")["ticket"]
        resp = client.post("/ha/automations", headers=headers,
                           json={**self.CONFIG, "ticket": ticket})
        assert resp.status_code == 409
        assert bridge.config_writes == []

    def test_confirmation_key_in_automation_body_does_not_open_the_gate(self, wired, consent_allowed):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/automations", headers=headers,
                           json={**self.CONFIG, "human_confirmed": True})
        assert resp.status_code == 409
        assert bridge.config_writes == []

    def test_benign_automation_still_creates(self, wired, consent_allowed):
        client, headers, bridge, _ = wired
        resp = client.post("/ha/automations", headers=headers, json={
            "alias": "Test porch light at dusk",
            "trigger": [{"platform": "sun", "event": "sunset"}],
            "action": [{"service": "light.turn_on",
                        "target": {"entity_id": LIGHT}}],
        })
        assert resp.status_code == 201, resp.get_json()
        assert len(bridge.config_writes) == 1


# ══════════════════════════════════════════════════════════════════════
#  7. The agent-side creators refuse legibly, not as a crash
# ══════════════════════════════════════════════════════════════════════

class TestAgentCreatorsRefuseLegibly:
    """An agent must not be able to confirm itself.

    Both cortex/bridge creators call ``create_automation`` with no ticket by
    design. What changed is that the refusal is now reported in the same
    409/proposal shape instead of a truncated exception string, so the dashboard
    can offer the confirm flow rather than showing a breakage.
    """

    def test_bridge_creator_reports_needs_confirmation(self, api_mod, monkeypatch,
                                                       consent_allowed):
        bridge = WireTrap(states=dict(DEFAULT_STATES))
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_create_automations({"suggestions": [{
            "name": "Blind the hallway",
            "trigger": "midnight",
            "action": "turn off",
            "entities": f"{CAMERA} -> {CAMERA}",
        }]})
        errors = out["result"]["errors"]
        assert errors, out
        assert out["needs_confirmation"] is True
        entry = errors[0]
        assert entry["needs_confirmation"] is True
        assert entry["http_status"] == 409
        assert "camera" in entry["proposal"]["domains"]
        assert bridge.config_writes == []

    def test_build_ha_automation_reports_needs_confirmation(self, api_mod,
                                                            monkeypatch,
                                                            consent_allowed):
        bridge = WireTrap(states=dict(DEFAULT_STATES))
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_build_ha_automation({
            "name": "Blind the hallway",
            "trigger_type": "time",
            "trigger_value": "00:00:00",
            "action_type": "turn_off",
            "action_entity_domain": "camera",
            "action_entity_hint": "camera.*",
        })
        assert out["success"] is False
        assert out["needs_confirmation"] is True
        assert out["result"]["http_status"] == 409
        assert "camera" in out["result"]["proposal"]["domains"]
        assert bridge.config_writes == []


# ══════════════════════════════════════════════════════════════════════
#  8. Store hygiene
# ══════════════════════════════════════════════════════════════════════

class TestStoreHygiene:

    def test_store_is_bounded(self, wired):
        """Minting is authenticated, so an unbounded store is a memory DoS."""
        client, headers, _, mod = wired
        for i in range(mod._HA_TICKET_MAX * 3):
            mint(client, headers, entity_id=f"camera.test_fake_{i}",
                 action="turn_off")
        assert len(mod._ha_tickets) <= mod._HA_TICKET_MAX

    def test_eviction_drops_the_oldest_not_the_newest(self, wired):
        client, headers, bridge, mod = wired
        first = mint(client, headers, entity_id=CAMERA,
                     action="turn_off")["ticket"]
        for i in range(mod._HA_TICKET_MAX + 1):
            mint(client, headers, entity_id=f"camera.test_fake_{i}",
                 action="turn_off")
        newest = mint(client, headers, entity_id=CAMERA_2,
                      action="turn_off")["ticket"]

        evicted = client.post("/ha/control", headers=headers,
                              json={"entity_id": CAMERA, "action": "turn_off",
                                    "ticket": first})
        assert evicted.status_code == 409
        kept = client.post("/ha/control", headers=headers,
                           json={"entity_id": CAMERA_2, "action": "turn_off",
                                 "ticket": newest})
        assert kept.status_code == 200

    def test_no_ticket_secret_is_stored(self, wired):
        """Only a digest is kept, so the store is not a place to harvest from."""
        client, headers, _, mod = wired
        ticket = mint(client, headers,
                      entity_id=CAMERA, action="turn_off")["ticket"]
        assert ticket not in repr(mod._ha_tickets)
        for record in mod._ha_tickets:
            assert ticket not in str(record.values())

    def test_ticket_secret_is_not_logged(self, wired, caplog):
        client, headers, _, _ = wired
        with caplog.at_level("INFO"):
            ticket = mint(client, headers,
                          entity_id=CAMERA, action="turn_off")["ticket"]
            client.post("/ha/control", headers=headers,
                        json={"entity_id": CAMERA, "action": "turn_off",
                              "ticket": ticket})
        assert ticket not in caplog.text
        assert "ha-confirm mint" in caplog.text
        assert "ha-confirm redeem" in caplog.text

    def test_binding_has_no_wildcard_or_prefix_match(self, api_mod):
        b = api_mod._ha_binding
        assert b("control", CAMERA, "turn_off") == b("control", CAMERA.upper(),
                                                     " TURN_OFF ")
        assert b("control", CAMERA, "turn_off") != b("control", CAMERA,
                                                     "turn_off_all")
        assert b("control", CAMERA, "turn_off") != b("service", CAMERA,
                                                     "turn_off")
        assert b("control", "camera.", "turn_off") != b("control", CAMERA,
                                                        "turn_off")
