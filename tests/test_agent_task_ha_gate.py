"""``POST /agents/task`` handlers actuate through the gate, or not at all.

The hole this file closes. The always-confirm gate lives inside
:class:`HABridge` so that nothing reaching Home Assistant can skip it. A second
HA client in ``api_server`` that builds its own authenticated
``POST {ha.url}/api/{endpoint}`` with the HA token performs **no classification
of the target**, so everything the gate does is simply absent: no
``touched_domains``, no ``enforce_confirmation``, no ``ConfirmationRequired``,
no ticket, no 409.

Six agent-task handlers actuate on Home Assistant, and three of them name the
always-confirm class:

* ``_bridge_good_morning``   -> ``light/turn_on``  ``{entity_id: all}``
* ``_bridge_leaving_home``   -> ``light/turn_off`` **and** ``lock/lock``  ``{entity_id: all}``
* ``_bridge_control_lights`` -> ``light/turn_off|turn_on`` ``{entity_id: all}``
* ``_sentinel_fix_security`` -> ``lock/lock``, ``cover/close_cover``,
  ``alarm_control_panel/alarm_arm_home``

All six are in ``_ACTION_HANDLERS``, and ``_match_task_action`` auto-routes free
text to them — ``"fix security"``, ``"lock all"``, ``"secure the house"`` and
``"remediate"`` all reach ``_sentinel_fix_security``. So, outside the gate, a
sentence submitted to ``POST /agents/task`` could lock every lock, close every cover and arm the alarm
panel with no ticket, no proposal and no 409.

``entity_id: "all"`` is the un-targeted broadcast
:func:`~puffpai.ha_bridge.touched_domains` was written to catch: the *service*
domain alone makes ``lock`` / ``cover`` / ``alarm_control_panel`` critical, with
no entity needed. Routing these calls through the bridge therefore refuses them,
so the handlers enumerate what they would touch and return a per-entity 409-shaped proposal, and the submitter completes
the action by minting one ticket per target and resubmitting with
``params.tickets``. There is deliberately no ticket for "all": a person
approving "lock the house" cannot see what "all" resolves to.

The bridge refuses the spelled-out broadcast itself, in every domain and with
no allow-list, so the light handlers enumerate too. The rule is uniform:
**every handler names what it touches, lights included**, the un-critical
domains because the gate does not accept "all" from anyone, the
safety-critical ones because naming is only half of it and each target
additionally needs its own ticket.

The worked examples are ``cover`` (a garage door) and ``camera`` (the
household's own witness). **Every entity id in this file is fabricated and
nothing here may reach a real Home Assistant.** The bridge is a real
``HABridge`` subclass whose ``_request`` — the only method that would open a
socket — is replaced by a recorder, so "the gate leaked" and "the test touched a
house" are the same observable event: an entry in ``bridge.wire``. Tests assert
on that list directly, so a pass is evidence that zero calls were made rather
than that a mock went unasserted.

Run: pytest tests/test_agent_task_ha_gate.py -q
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
    SAFETY_CRITICAL_DOMAINS,
    HABridge,
)

# Fabricated. None of these exist in any real house.
LOCK_FRONT = "lock.test_front_door"
LOCK_BACK = "lock.test_back_door"
COVER_GARAGE = "cover.test_garage_door"
ALARM = "alarm_control_panel.test_house"
CAMERA = "camera.test_hallway_cam"
LIGHT = "light.test_porch_light"


class WireTrap(HABridge):
    """A real :class:`HABridge` with the wire cut and a recorder in its place.

    Subclassing the real class rather than duck-typing it is the point: the gate
    under test is the real gate, in the real ``call_service``, with the real
    argument handling. Only ``_request`` is replaced.
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

    def calls_to(self, domain, service=None):
        prefix = (f"services/{domain}/{service}" if service
                  else f"services/{domain}/")
        return [c for c in self.service_calls if c[0].startswith(prefix)]


#: Everything needing remediation at once: two unlocked locks, an open garage
#: door and a disarmed panel. A camera is present but is never a target of these
#: handlers — it is here so a broadcast that escaped would have something extra
#: to hit.
STATES = {
    LOCK_FRONT: {"state": "unlocked", "attributes": {"friendly_name": "Front"}},
    LOCK_BACK: {"state": "unlocked", "attributes": {"friendly_name": "Back"}},
    COVER_GARAGE: {"state": "open", "attributes": {"friendly_name": "Garage"}},
    ALARM: {"state": "disarmed", "attributes": {"friendly_name": "House"}},
    CAMERA: {"state": "recording", "attributes": {}},
    LIGHT: {"state": "on", "attributes": {"friendly_name": "Porch"}},
}


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
def trapped(api_mod, monkeypatch):
    """``(mod, bridge)`` with every HA path in api_server pointed at a recorder.

    ``_get_ha_bridge`` is the *only* thing patched, which is itself part of the
    assertion: ``_ha_api``'s reads now go through the bridge too, so if any code
    path still held its own transport this fixture would not be able to trap it
    and the test would reach ``http://ha.invalid``.
    """
    bridge = WireTrap(states=dict(STATES))
    monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
    api_mod._ha_tickets.clear()
    yield api_mod, bridge
    api_mod._ha_tickets.clear()


@pytest.fixture(autouse=True)
def _review_log_in_tmp(monkeypatch, tmp_path):
    """The automation-authoring handlers audit every gate verdict to
    sentinel_reviews.jsonl (consent_gate.REVIEW_LOG) — the real data dir on a
    dev checkout. Today the tests here fail their reads before the gate runs;
    pin the log to tmp so a live-read case cannot leave residue either."""
    import puffpai.security.consent_gate as cg
    monkeypatch.setattr(cg, "REVIEW_LOG", str(tmp_path / "reviews.jsonl"))


def mint(mod, entity_id, action):
    """Mint a real ticket the way ``POST /ha/confirm`` does, for *one* target."""
    secret, _record = mod._ha_mint_ticket("service", entity_id, action)
    return secret


# ══════════════════════════════════════════════════════════════════════
#  1. The bypass itself is gone
# ══════════════════════════════════════════════════════════════════════

class TestHaApiIsReadOnly:
    """``_ha_api`` was a live POST bypass. It refuses to write at all now."""

    @pytest.mark.parametrize("endpoint", [
        "services/lock/lock",
        "services/cover/close_cover",
        "services/alarm_control_panel/alarm_arm_home",
        "services/camera/turn_off",
    ])
    def test_post_is_refused_and_nothing_reaches_ha(self, trapped, endpoint):
        mod, bridge = trapped
        ok, detail = mod._ha_api(endpoint, method="POST",
                                 payload={"entity_id": "all"})
        assert ok is False
        assert "read-only" in detail
        # The refusal is *before* the transport, not a failed request.
        assert bridge.wire == []

    def test_the_refusal_names_the_way_through(self, trapped):
        """A dead end teaches nothing; this one names the gated alternative."""
        mod, _ = trapped
        _ok, detail = mod._ha_api("services/lock/lock", method="POST")
        assert "call_service" in detail

    def test_reads_still_work_and_go_through_the_bridge(self, trapped):
        mod, bridge = trapped
        err, states = mod._ha_states_read()
        assert err is None
        assert {e["entity_id"] for e in states} == set(STATES)
        assert bridge.wire == [("states", "GET", None)]
        assert bridge.service_calls == []


# ══════════════════════════════════════════════════════════════════════
#  2. The lights routines are unchanged — light is not safety-critical
# ══════════════════════════════════════════════════════════════════════

class TestLightRoutinesStillWork:
    """The gate must not break what it was never meant to stop.

    ``light`` is deliberately absent from ``SAFETY_CRITICAL_DOMAINS``, so these
    three call sites keep working with no confirmation. A gate that refused the
    morning routine is a gate the household would switch off.

    Under the broadcast rule the routines name every ``light.*`` entity instead
    of posting ``entity_id: "all"``. A named list of un-critical entities is
    ordinary traffic (it meets neither the untargeted rule nor the broadcast
    one), so the household sees
    the same lights come on, over a call that says which ones.
    """

    def test_light_is_not_in_the_always_confirm_class(self):
        assert "light" not in SAFETY_CRITICAL_DOMAINS

    def test_good_morning_still_turns_the_lights_on(self, trapped):
        mod, bridge = trapped
        out = mod._bridge_good_morning()
        assert out["success"] is True
        assert "lights_error" not in out["result"]
        assert bridge.calls_to("light", "turn_on") == [
            ("services/light/turn_on", "POST", {"entity_id": [LIGHT]})]

    def test_an_empty_house_makes_no_light_call_at_all(self, api_mod,
                                                       monkeypatch):
        """No lights to name means no call, not a call that names nothing.

        An empty entity list is the *untargeted* form, which HA and the gate
        both read as the whole ``light`` domain. The guard skips the wire
        instead of letting a house with no lights become a house-wide sweep.
        """
        lightless = {eid: state for eid, state in STATES.items()
                     if not eid.startswith("light.")}
        bridge = WireTrap(states=lightless)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_good_morning()
        assert bridge.service_calls == []
        assert "lights_error" not in out["result"]

    def test_every_light_is_named_and_sorted(self, api_mod, monkeypatch):
        """The wire carries the enumeration itself, in a deterministic order."""
        desk = "light.test_desk_lamp"
        states = {
            LIGHT: {"state": "off", "attributes": {"friendly_name": "Porch"}},
            desk: {"state": "off", "attributes": {"friendly_name": "Desk"}},
            CAMERA: {"state": "recording", "attributes": {}},
        }
        bridge = WireTrap(states=states)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        api_mod._bridge_good_morning()
        calls = bridge.calls_to("light", "turn_on")
        assert len(calls) == 1
        assert calls[0][2] == {"entity_id": sorted([LIGHT, desk])}
        # Nothing that is not a light was swept up with them.
        assert CAMERA not in calls[0][2]["entity_id"]

    def test_no_call_carries_the_broadcast_spelling(self, trapped):
        """Pins that ``entity_id: "all"`` never comes back to these routines."""
        mod, bridge = trapped
        mod._bridge_good_morning()
        mod._bridge_control_lights()
        assert bridge.service_calls
        for _endpoint, _method, data in bridge.service_calls:
            target = (data or {}).get("entity_id")
            assert target != "all"
            assert "all" not in (target if isinstance(target, list) else [])

    def test_control_lights_turns_off_when_most_are_on(self, trapped):
        mod, bridge = trapped
        out = mod._bridge_control_lights()
        assert out["result"]["action"] == "turned off"
        assert bridge.calls_to("light", "turn_off") == [
            ("services/light/turn_off", "POST", {"entity_id": [LIGHT]})]

    def test_a_transport_failure_is_reported_not_swallowed(self, api_mod,
                                                           monkeypatch):
        """The old code discarded the result, so a failure read as success."""
        from puffpai.ha_bridge import HABridgeError

        bridge = WireTrap(states=dict(STATES))

        def boom(endpoint, method="GET", data=None, timeout=None, stream=False):
            if method == "POST":
                raise HABridgeError("Cannot connect to Home Assistant")
            return WireTrap._request(bridge, endpoint, method, data)

        monkeypatch.setattr(bridge, "_request", boom)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_good_morning()
        assert "Cannot connect" in out["result"]["lights_error"]

    def test_an_unreadable_state_list_is_an_error_not_zero_lights(
            self, api_mod, monkeypatch):
        """Enumeration failing must not read as "no lights to turn on".

        The enumeration is what makes this call nameable, so a failure to
        enumerate is a failure to actuate. Collapsing it to an empty list would
        take the "nothing to do" branch and report success — the same
        failure-reads-as-success shape the routine above pins for the POST.
        """
        from puffpai.ha_bridge import HABridgeError

        bridge = WireTrap(states=dict(STATES))

        def dark(endpoint, method="GET", data=None, timeout=None, stream=False):
            raise HABridgeError("Cannot connect to Home Assistant")

        monkeypatch.setattr(bridge, "_request", dark)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_good_morning()
        assert "Cannot connect" in out["result"]["lights_error"]
        assert bridge.wire == []


# ══════════════════════════════════════════════════════════════════════
#  3. leaving-home: the lock broadcast is refused, the lights still happen
# ══════════════════════════════════════════════════════════════════════

class TestLeavingHome:

    def test_no_lock_is_touched_without_a_confirmation(self, trapped):
        mod, bridge = trapped
        out = mod._bridge_leaving_home()
        assert out["success"] is False
        assert out["needs_confirmation"] is True
        assert bridge.calls_to("lock") == []
        # …and the part it may do alone still happened.
        assert bridge.calls_to("light", "turn_off")

    def test_every_unlocked_lock_is_named_with_its_own_proposal(self, trapped):
        mod, _ = trapped
        pending = mod._bridge_leaving_home()["result"]["needs_confirmation"]
        assert {p["entity"] for p in pending} == {LOCK_FRONT, LOCK_BACK}
        for item in pending:
            assert item["http_status"] == 409
            assert item["needs_confirmation"] is True
            assert item["proposal"]["domains"] == ["lock"]
            # The way out is spelled out per target, not per routine.
            assert "/ha/confirm" in item["next_step"]
            assert item["entity"] in item["next_step"]

    def test_the_security_line_no_longer_claims_the_house_is_armed(self, trapped):
        """It never armed anything. Saying "armed" is a false assurance."""
        mod, bridge = trapped
        out = mod._bridge_leaving_home()
        assert out["result"]["security"] != "armed"
        assert bridge.calls_to("alarm_control_panel") == []

    def test_a_ticket_per_lock_completes_the_routine(self, trapped):
        mod, bridge = trapped
        tickets = {eid: mint(mod, eid, "lock.lock")
                   for eid in (LOCK_FRONT, LOCK_BACK)}
        out = mod._bridge_leaving_home({"tickets": tickets})
        assert out["success"] is True
        assert out["needs_confirmation"] is False
        assert {c[2]["entity_id"] for c in bridge.calls_to("lock", "lock")} == {
            LOCK_FRONT, LOCK_BACK}

    def test_one_ticket_authorises_only_its_own_lock(self, trapped):
        """The unit of consent is one action on one target."""
        mod, bridge = trapped
        out = mod._bridge_leaving_home(
            {"tickets": {LOCK_FRONT: mint(mod, LOCK_FRONT, "lock.lock")}})
        assert [c[2]["entity_id"] for c in bridge.calls_to("lock", "lock")] == [
            LOCK_FRONT]
        assert [p["entity"] for p in out["result"]["needs_confirmation"]] == [
            LOCK_BACK]


# ══════════════════════════════════════════════════════════════════════
#  4. fix-security: reachable from free text, so it must be gated
# ══════════════════════════════════════════════════════════════════════

class TestSentinelFixSecurity:

    @pytest.mark.parametrize("text", [
        "fix security", "lock all", "secure the house", "lock everything",
        "remediate",
    ])
    def test_free_text_reaches_this_handler(self, api_mod, text):
        """Why the gate has to be here: no ticket can travel with a sentence."""
        _agent, handler = api_mod._match_task_action(text)
        assert handler == "sentinel_fix_security"
        assert api_mod._ACTION_HANDLERS[handler] is api_mod._sentinel_fix_security

    def test_every_domain_it_actuates_is_always_confirm(self):
        assert {"lock", "cover", "alarm_control_panel"} <= SAFETY_CRITICAL_DOMAINS

    def test_nothing_is_actuated_without_tickets(self, trapped):
        mod, bridge = trapped
        out = mod._sentinel_fix_security(
            {"actions": ["lock_all", "close_doors", "arm_alarm"]})
        assert out["success"] is False
        assert out["needs_confirmation"] is True
        assert out["result"]["fixes_applied"] == 0
        assert out["result"]["fixes_needing_confirmation"] == 4
        assert bridge.service_calls == []

    def test_each_target_gets_its_own_proposal(self, trapped):
        mod, _ = trapped
        pending = mod._sentinel_fix_security(
            {"actions": ["lock_all", "close_doors", "arm_alarm"]},
        )["result"]["needs_confirmation"]
        by_entity = {p["entity"]: p for p in pending}
        assert set(by_entity) == {LOCK_FRONT, LOCK_BACK, COVER_GARAGE, ALARM}
        assert by_entity[COVER_GARAGE]["action"] == "cover.close_cover"
        assert by_entity[COVER_GARAGE]["proposal"]["domains"] == ["cover"]
        assert by_entity[ALARM]["action"] == "alarm_control_panel.alarm_arm_home"

    def test_the_garage_door_closes_only_with_its_own_ticket(self, trapped):
        """The worked example: a cover is a physical thing that can trap a body."""
        mod, bridge = trapped
        ticket = mint(mod, COVER_GARAGE, "cover.close_cover")
        out = mod._sentinel_fix_security(
            {"actions": ["close_doors"], "tickets": {COVER_GARAGE: ticket}})
        assert out["success"] is True
        assert out["result"]["details"] == [
            {"entity": COVER_GARAGE, "name": "Garage", "action": "closed"}]
        assert bridge.calls_to("cover", "close_cover") == [
            ("services/cover/close_cover", "POST", {"entity_id": COVER_GARAGE})]

    def test_a_ticket_for_one_domain_does_not_open_another(self, trapped):
        """A cover ticket is not a lock ticket, even in the same submission."""
        mod, bridge = trapped
        ticket = mint(mod, COVER_GARAGE, "cover.close_cover")
        out = mod._sentinel_fix_security({
            "actions": ["lock_all", "close_doors"],
            # The same value offered against every entity in the routine.
            "tickets": {LOCK_FRONT: ticket, LOCK_BACK: ticket,
                        COVER_GARAGE: ticket},
        })
        assert bridge.calls_to("lock") == []
        # The cover is refused too, and that is the stricter half of the rule:
        # a mismatched presentation *burns* the ticket before its binding is
        # checked, so spraying one ticket at four targets spends it on the first
        # and authorises none — including the one it was actually minted for.
        assert bridge.calls_to("cover") == []
        assert {p["entity"] for p in out["result"]["needs_confirmation"]} == {
            LOCK_FRONT, LOCK_BACK, COVER_GARAGE}
        assert out["result"]["fixes_applied"] == 0

    def test_a_spent_ticket_cannot_be_replayed(self, trapped):
        mod, bridge = trapped
        ticket = mint(mod, COVER_GARAGE, "cover.close_cover")
        first = mod._sentinel_fix_security(
            {"actions": ["close_doors"], "tickets": {COVER_GARAGE: ticket}})
        assert first["success"] is True
        second = mod._sentinel_fix_security(
            {"actions": ["close_doors"], "tickets": {COVER_GARAGE: ticket}})
        assert second["needs_confirmation"] is True
        assert len(bridge.calls_to("cover", "close_cover")) == 1

    @pytest.mark.parametrize("forged", [True, "true", 1, "not-a-ticket", ""])
    def test_a_value_that_is_not_a_ticket_confirms_nothing(self, trapped, forged):
        """A JSON body can supply values; it cannot supply the token object."""
        mod, bridge = trapped
        out = mod._sentinel_fix_security(
            {"actions": ["close_doors"], "tickets": {COVER_GARAGE: forged}})
        assert out["needs_confirmation"] is True
        assert bridge.service_calls == []

    def test_default_actions_still_exclude_the_alarm_panel(self, trapped):
        mod, _ = trapped
        pending = mod._sentinel_fix_security({})["result"]["needs_confirmation"]
        assert ALARM not in {p["entity"] for p in pending}

    def test_a_confirmed_call_that_fails_upstream_is_reported_not_raised(
            self, api_mod, monkeypatch):
        """A confirmed actuation can still fail. That is an error, not a crash.

        Distinct from a refusal: the person said yes and the house did not
        answer. It belongs in ``errors``, and the handler must keep going —
        one unreachable lock must not abandon the rest of the remediation.
        """
        from puffpai.ha_bridge import HABridgeError

        bridge = WireTrap(states=dict(STATES))
        real_request = bridge._request

        def flaky(endpoint, method="GET", data=None, timeout=None, stream=False):
            if endpoint.startswith("services/cover/"):
                raise HABridgeError("HA returned HTTP 500: boom")
            return real_request(endpoint, method, data, timeout, stream)

        monkeypatch.setattr(bridge, "_request", flaky)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        api_mod._ha_tickets.clear()
        out = api_mod._sentinel_fix_security({
            "actions": ["close_doors"],
            "tickets": {COVER_GARAGE: mint(api_mod, COVER_GARAGE,
                                           "cover.close_cover")}})
        api_mod._ha_tickets.clear()
        assert out["result"]["fixes_failed"] == 1
        assert out["result"]["errors"][0]["entity"] == COVER_GARAGE
        assert "boom" in out["result"]["errors"][0]["error"]
        # A genuine upstream failure is not a missing confirmation.
        assert out["needs_confirmation"] is False


# ══════════════════════════════════════════════════════════════════════
#  5. A failed state read is an error, never an empty house
# ══════════════════════════════════════════════════════════════════════

def _dead(*_a, **_k):
    """A transport that never answers. Every read raises."""
    from puffpai.ha_bridge import HABridgeError
    raise HABridgeError("Cannot connect to Home Assistant")


def _nth_states_get_dies(bridge, n):
    """Wrap ``bridge._request`` so the *n*-th ``states`` GET raises.

    The partial-failure window is what makes these bugs dangerous rather than
    merely wrong: one poll fails, everything around it succeeds, and the routine
    carries on with a picture of the house it never actually read.
    """
    from puffpai.ha_bridge import HABridgeError
    seen = {"n": 0}
    real = bridge._request

    def wrapped(endpoint, method="GET", data=None, timeout=None, stream=False):
        if endpoint == "states" and method == "GET":
            seen["n"] += 1
            if seen["n"] == n:
                raise HABridgeError("Cannot connect to Home Assistant")
        return real(endpoint, method, data, timeout, stream)

    return wrapped


class TestFailedReadsFailClosed:
    """A failed state read is an error, never an empty house.

    The old ``_ha_all_states`` collapsed an unreachable Home Assistant to
    ``[]``, so every caller that let the *emptiness* or the *content* of that
    list decide something turned a failed read into a confident wrong answer: a
    direction to actuate, an "all locked" report, a count of lights, an
    automation built against no entities, a consent audit with nothing pending.
    Each site below now reads through ``_ha_states_read`` and refuses.

    As everywhere in this file the bridge is a real ``HABridge`` whose
    ``_request`` is a recorder, so ``bridge.wire``/``bridge.service_calls`` being
    empty is evidence that nothing was sent — not that a mock went unasserted.
    """

    # ── the isinstance branch of the reference implementation ──

    def test_a_non_list_states_payload_enumerates_no_lights(self, api_mod,
                                                            monkeypatch):
        """Transport fine, payload wrong. ``{"message": "API running."}`` is
        what HA's API root answers, and a misrouted read returning it must not
        iterate a dict's keys as if they were entities — nor take the
        "no lights, nothing to do" branch, which returns success.
        """
        bridge = WireTrap(states=dict(STATES))

        def wrong_shape(endpoint, method="GET", data=None, timeout=None,
                        stream=False):
            bridge.wire.append((endpoint, method, data))

            class _Resp:
                def json(self):
                    return {"message": "API running."}

            return _Resp()

        monkeypatch.setattr(bridge, "_request", wrong_shape)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        error = api_mod._agent_task_lights("turn_on")
        assert error and "unexpected state list" in error
        assert bridge.service_calls == []

    # ── the flagship: a direction must not come from a failed read ──

    def test_control_lights_does_not_guess_a_direction_from_a_failed_read(
            self, api_mod, monkeypatch):
        """The dangerous shape, exactly: one poll fails, the actuation works.

        ``_bridge_control_lights`` decides *which way* to go from the state
        list. Collapsed to ``[]`` that read as "0 of 0 lights on", which chose
        ``turn_on`` — and the retry inside ``_agent_task_lights`` then succeeded,
        so every light in the house came on because a single poll failed. There
        is no safe default here: not knowing the current state means not knowing
        the direction, so the routine must refuse.
        """
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request", _nth_states_get_dies(bridge, 1))
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_control_lights()
        assert out["success"] is False
        assert "Cannot connect" in out["result"]["error"]
        assert bridge.service_calls == []

    # ── the false assurance at the front door ──

    def test_leaving_home_does_not_report_all_locked_from_a_failed_read(
            self, api_mod, monkeypatch):
        """The locks are enumerated from the state list, so a failed read
        enumerated none — and "nothing to lock" was rendered as ``all locked``
        with ``success: True``, told to someone on their way out.
        """
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request", _dead)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_leaving_home()
        assert out["success"] is False
        error = out["result"]["error"]
        assert "could not enumerate locks" in error
        assert "Cannot connect" in error
        assert out["result"].get("locks") != "all locked"
        assert bridge.service_calls == []

    # ── fix-security: refuse legibly rather than crash ──

    def test_fix_security_refuses_a_non_list_payload(self, api_mod, monkeypatch):
        """Transport ok, payload a dict. The old ``if not states`` let a dict
        through as truthy and the remediation loop raised ``TypeError`` on
        ``entity['entity_id']``. A refusal a household can read is the floor.
        """
        bridge = WireTrap(states=dict(STATES))

        def wrong_shape(endpoint, method="GET", data=None, timeout=None,
                        stream=False):
            bridge.wire.append((endpoint, method, data))

            class _Resp:
                def json(self):
                    return {"message": "API running."}

            return _Resp()

        monkeypatch.setattr(bridge, "_request", wrong_shape)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._sentinel_fix_security({"actions": ["lock_all"]})
        assert out["success"] is False
        assert "unexpected state list" in out["result"]["error"]
        assert bridge.service_calls == []

    # ── automations built from a house nobody could read ──

    def test_create_automations_writes_nothing_on_a_dead_read(self, api_mod,
                                                              monkeypatch):
        """A failed read did not stop creation; it changed what was created.

        Every suggestion's resolved entities silently became the manual-trigger
        stub, and the automation was still POSTed to HA — content decided by
        failed I/O. An empty ``bridge.wire`` is the assertion: no POST, so no
        stub automation exists.
        """
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request", _dead)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_create_automations({"suggestions": [{
            "name": "X",
            "trigger": "motion",
            "action": "turn on",
            "entities": "light.* -> light.*",
        }]})
        assert out["success"] is False
        assert "could not read the entity list" in out["result"]["error"]
        assert bridge.wire == []

    def test_build_ha_automation_writes_nothing_on_a_dead_read(self, api_mod,
                                                               monkeypatch):
        """Same hazard one layer down, with an amplifier: this builder's "if
        still nothing, grab all entities of the action domain" fallback turns an
        unresolvable hint into a domain-wide target.
        """
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request", _dead)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_build_ha_automation({
            "name": "X",
            "trigger_type": "time",
            "trigger_value": "23:00",
            "action_type": "turn_off",
            "action_entity_domain": "light",
            "action_entity_hint": "light.*",
        })
        assert out["success"] is False
        assert "could not read the entity list" in out["result"]["error"]
        assert bridge.wire == []

    def test_parse_and_create_automation_stops_before_the_model(self, api_mod,
                                                                monkeypatch):
        """The prompt tells the model the listed entities are the only ones that
        exist. A failed read told it "(no entities found)" and then trusted the
        hints it invented. Refusing early also skips a pointless Ollama call —
        pinned here, because reaching the model at all would mean the read
        result was never checked.
        """
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request", _dead)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        # The handler logs 'task_analysing' before the read; tests must not
        # append to the real activity log.
        monkeypatch.setattr(api_mod, "_append_jsonl", lambda *a, **k: None)

        def no_ollama(*_a, **_k):
            raise AssertionError("must not reach Ollama on a failed state read")

        monkeypatch.setattr("requests.post", no_ollama)
        out = api_mod._cortex_parse_and_create_automation(
            {"_task_text": "turn off the porch light at 11pm"})
        assert out["success"] is False
        assert "could not read the entity list" in out["result"]["error"]
        assert bridge.wire == []

    # ── a read-back that failed is not a count, and not an all-clear ──

    def test_good_morning_reports_the_read_back_failure_not_a_count(
            self, api_mod, monkeypatch):
        """The lights went on, then the read-back failed — and the routine said
        "0 lights turned on" about a house it had just lit. The actuation half
        still stands (``success`` stays true); the reporting half must say it
        does not know.
        """
        bridge = WireTrap(states=dict(STATES))
        # First GET (the enumeration) and the POST succeed; the read-back dies.
        monkeypatch.setattr(bridge, "_request", _nth_states_get_dies(bridge, 2))
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._bridge_good_morning()
        assert bridge.calls_to("light", "turn_on")
        result = out["result"]
        assert result["state_read_error"]
        assert "Cannot connect" in result["state_read_error"]
        assert result["lights"] == "unknown: the state list could not be read"
        assert "lights turned on" not in result["lights"]

    def test_pending_approvals_does_not_report_a_false_all_clear(self, api_mod,
                                                                 monkeypatch):
        """A consent audit answering "0 pending" because the read failed is an
        all-clear nobody earned, on the surface that decides what may actuate.
        """
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request", _dead)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = api_mod._sentinel_pending_approvals()
        assert out["success"] is False
        assert "could not read the entity list" in out["result"]["error"]
        assert "pending_count" not in out["result"]
        assert bridge.wire == []


# ══════════════════════════════════════════════════════════════════════
#  5b. A malformed element is dropped, never crashed on
# ══════════════════════════════════════════════════════════════════════

def _garbage_states(bridge, payload):
    """Wrap ``bridge._request`` so the ``states`` GET answers *payload* raw.

    Everything else still records to ``bridge.wire`` and answers ``[]``, the
    same shape the real recorder gives unknown endpoints, so actuation stays
    observable.
    """
    def wrapped(endpoint, method="GET", data=None, timeout=None, stream=False):
        bridge.wire.append((endpoint, method, data))

        class _Resp:
            def json(self):
                if endpoint == "states" and method == "GET":
                    return payload
                return []

        return _Resp()

    return wrapped


#: Shapes that have never come out of HA's ``/api/states``: a transport or
#: proxy mangling elements while the list itself still parses. The dict without
#: an ``entity_id`` is the sly one — it survives the element filter (it *is* a
#: dict) and must fall out of every predicate instead.
GARBAGE = ["garbage", None, 42, ["nested"], {"message": "API running."}]


def _house_with_garbage(states=None):
    """The real house's entity dicts with the garbage shapes interleaved."""
    entities = [{"entity_id": eid, **state}
                for eid, state in (states or STATES).items()]
    return list(GARBAGE) + entities


class TestMalformedElementsAreDropped:
    """``(True, ["garbage", ...])`` — a readable list, not entirely of dicts.

    The list-level ``isinstance`` check accepts this payload, and the handlers
    then index ``e['entity_id']`` in a loop, so one malformed element raised
    ``AttributeError``/``TypeError`` mid-handler. Crash-closed is still closed —
    nothing actuated — but the ``/agents/task`` boundary rendered it as a 500
    with no legible refusal, and ``_cortex_parse_and_create_automation``
    propagated the exception to non-route callers. ``_ha_states_read`` drops
    what is not a dict and keeps what is, which these tests pin from both
    sides: the garbage changes nothing, and the real entities are handled
    exactly as if it had never been there.
    """

    @pytest.fixture
    def garbaged(self, api_mod, monkeypatch):
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request",
                            _garbage_states(bridge, _house_with_garbage()))
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        api_mod._ha_tickets.clear()
        yield api_mod, bridge
        api_mod._ha_tickets.clear()

    def test_good_morning_still_lights_the_real_house(self, garbaged):
        mod, bridge = garbaged
        out = mod._bridge_good_morning()
        assert out["success"] is True
        assert "lights_error" not in out["result"]
        assert "state_read_error" not in out["result"]
        assert out["result"]["lights"] == "1 lights turned on"
        assert bridge.calls_to("light", "turn_on") == [
            ("services/light/turn_on", "POST", {"entity_id": [LIGHT]})]

    def test_leaving_home_still_names_each_real_lock(self, garbaged):
        mod, bridge = garbaged
        out = mod._bridge_leaving_home()
        assert out["success"] is False
        assert out["needs_confirmation"] is True
        assert {p["entity"] for p in out["result"]["needs_confirmation"]} == {
            LOCK_FRONT, LOCK_BACK}
        assert bridge.calls_to("lock") == []

    def test_control_lights_counts_only_real_entities(self, garbaged):
        mod, bridge = garbaged
        out = mod._bridge_control_lights()
        assert out["success"] is True
        assert out["result"]["lights_before"] == "1/1 on"
        assert bridge.calls_to("light", "turn_off") == [
            ("services/light/turn_off", "POST", {"entity_id": [LIGHT]})]

    def test_fix_security_still_refuses_per_real_target(self, garbaged):
        mod, bridge = garbaged
        out = mod._sentinel_fix_security({"actions": ["lock_all"]})
        assert out["needs_confirmation"] is True
        assert {p["entity"] for p in out["result"]["needs_confirmation"]} == {
            LOCK_FRONT, LOCK_BACK}
        assert bridge.calls_to("lock") == []

    def test_security_audit_counts_only_real_entities(self, garbaged):
        mod, _bridge = garbaged
        out = mod._sentinel_security_audit()
        assert out["success"] is True
        assert out["result"]["locks"] == "0/2 locked"

    def test_create_automations_resolves_the_real_light(self, garbaged,
                                                        monkeypatch):
        mod, _bridge = garbaged
        monkeypatch.delenv("PUFFPAI_ENFORCE_CONSENT", raising=False)
        out = mod._bridge_create_automations({"suggestions": [{
            "name": "X",
            "trigger": "motion",
            "action": "turn on",
            "entities": "light.* -> light.*",
        }]})
        assert out["result"]["created"] == 1
        assert out["result"]["details"][0]["actions"] == 1

    def test_build_ha_automation_targets_the_real_light(self, garbaged,
                                                        monkeypatch):
        mod, _bridge = garbaged
        monkeypatch.delenv("PUFFPAI_ENFORCE_CONSENT", raising=False)
        out = mod._bridge_build_ha_automation({
            "name": "X",
            "trigger_type": "time",
            "trigger_value": "23:00",
            "action_type": "turn_off",
            "action_entity_domain": "light",
            "action_entity_hint": "light.*",
        })
        assert out["success"] is True
        assert [a["target"]["entity_id"]
                for a in out["result"]["config"]["action"]] == [LIGHT]

    def test_parse_and_create_prompts_with_real_entities_only(
            self, garbaged, monkeypatch):
        """The old crash never got past the entity gathering; the handler must
        now reach the model step, with a prompt naming only real entities."""
        mod, _bridge = garbaged
        monkeypatch.setattr(mod, "_append_jsonl", lambda *a, **k: None)
        seen = {}

        def capture(messages, **_kwargs):
            seen["prompt"] = messages[0]["content"]
            raise RuntimeError("no model in tests")

        monkeypatch.setattr(mod, "_ollama_chat", capture)
        out = mod._cortex_parse_and_create_automation(
            {"_task_text": "turn off the porch light at 11pm"})
        assert out["success"] is False
        assert "Failed to parse Ollama response" in out["result"]["error"]
        assert LIGHT in seen["prompt"]
        assert "garbage" not in seen["prompt"]

    def test_pending_approvals_audits_the_real_entities(self, garbaged,
                                                        monkeypatch):
        mod, _bridge = garbaged
        import puffpai.security.consent_gate as cg

        class _AllPending:
            def pending(self, ids):
                return list(ids)

            def status(self, _entity_id):
                return "pending"

        monkeypatch.setattr(cg, "_store", _AllPending())
        out = mod._sentinel_pending_approvals()
        assert out["success"] is True
        assert out["result"]["total_entities"] == len(STATES)
        assert sorted(out["result"]["pending_sample"]) == sorted(STATES)


# ══════════════════════════════════════════════════════════════════════
#  5c. Reporting reads say which empty answer they mean
# ══════════════════════════════════════════════════════════════════════

REPORTING_HANDLERS = [
    "_cortex_device_analytics",
    "_cortex_suggest_automations",
    "_cortex_environment_summary",
    "_bridge_discover_devices",
    "_sentinel_security_audit",
    "_sentinel_network_inventory",
]


class TestReportingReadsAreHonest:
    """The six pure-reporting handlers said ``Cannot reach HA`` about *any*
    empty answer — a dead transport and a genuinely empty house rendered the
    same. They read through ``_ha_states_read`` now: a failed read refuses with
    the transport's own words attached, a readable empty house reports its
    zeros, and a garbage element is dropped like everywhere else.
    """

    @pytest.mark.parametrize("handler", REPORTING_HANDLERS)
    def test_a_dead_read_reports_the_failure_itself(self, api_mod, monkeypatch,
                                                    handler):
        bridge = WireTrap(states=dict(STATES))
        monkeypatch.setattr(bridge, "_request", _dead)
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = getattr(api_mod, handler)()
        assert out["success"] is False
        assert "Cannot reach HA" in out["result"]["error"]
        # The transport's detail travels with the refusal instead of being
        # flattened into a bare slogan.
        assert "Cannot connect" in out["result"]["error"]

    @pytest.mark.parametrize("handler", REPORTING_HANDLERS)
    def test_an_empty_house_is_a_report_not_an_error(self, api_mod, monkeypatch,
                                                     handler):
        bridge = WireTrap(states={})
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = getattr(api_mod, handler)()
        assert out["success"] is True

    @pytest.mark.parametrize("handler", REPORTING_HANDLERS)
    def test_a_garbage_only_payload_reads_as_empty(self, api_mod, monkeypatch,
                                                   handler):
        bridge = WireTrap(states={})
        monkeypatch.setattr(bridge, "_request",
                            _garbage_states(bridge, list(GARBAGE)))
        monkeypatch.setattr(api_mod, "_get_ha_bridge", lambda: bridge)
        out = getattr(api_mod, handler)()
        assert out["success"] is True
        assert bridge.service_calls == []


# ══════════════════════════════════════════════════════════════════════
#  6. End to end over the route that was actually exposed
# ══════════════════════════════════════════════════════════════════════

class TestOverTheRoute:
    """``POST /agents/task`` with free text — the path an injection would take."""

    @pytest.fixture
    def client(self, api_mod, trapped, monkeypatch):
        mod, bridge = trapped
        mod._rate_limit_store.clear()
        # The concierge/bus and the activity log are not what this file tests.
        monkeypatch.setattr(mod, "_route_via_concierge", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_append_jsonl", lambda *a, **k: None)
        with mod.app.test_client() as c:
            yield c, {"X-API-Key": mod._API_KEY}, bridge

    def test_a_sentence_cannot_secure_the_house(self, client):
        c, headers, bridge = client
        resp = c.post("/agents/task", headers=headers,
                      json={"task": "secure the house"})
        assert resp.status_code == 200
        handler_out = resp.get_json()["task"]["result"]
        assert handler_out["needs_confirmation"] is True
        assert handler_out["success"] is False
        assert bridge.service_calls == []

    def test_the_response_carries_the_confirm_instructions(self, client):
        c, headers, bridge = client
        body = c.post("/agents/task", headers=headers,
                      json={"task": "lock all"}).get_json()
        pending = body["task"]["result"]["result"]["needs_confirmation"]
        assert pending
        assert all("/ha/confirm" in p["next_step"] for p in pending)
        assert bridge.service_calls == []

    def test_the_two_step_flow_completes_over_http(self, client):
        c, headers, bridge = client
        minted = c.post("/ha/confirm", headers=headers, json={
            "kind": "service", "entity_id": COVER_GARAGE,
            "action": "cover.close_cover"})
        assert minted.status_code == 201, minted.get_json()
        ticket = minted.get_json()["ticket"]
        # Minting must not have touched the house — not even a read.
        assert bridge.wire == []
        done = c.post("/agents/task", headers=headers, json={
            "task": "fix security",
            "params": {"actions": ["close_doors"],
                       "tickets": {COVER_GARAGE: ticket}}})
        assert done.get_json()["task"]["result"]["success"] is True
        assert bridge.calls_to("cover", "close_cover") == [
            ("services/cover/close_cover", "POST", {"entity_id": COVER_GARAGE})]

    def test_a_confirmed_flag_in_the_body_is_not_a_confirmation(self, client):
        """The obvious forgery: say yes in the same request that asks."""
        c, headers, bridge = client
        body = c.post("/agents/task", headers=headers, json={
            "task": "secure the house",
            "params": {"actions": ["lock_all"], "confirmed": True,
                       "human_confirmed": True, "force": True},
        }).get_json()
        assert body["task"]["result"]["needs_confirmation"] is True
        assert bridge.service_calls == []
