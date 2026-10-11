"""Opening the pairing window through the household's own HA script.

Home Assistant registers ``zha.permit`` as an **admin-only** service, and this
appliance's HA user is deliberately not an administrator (least-privilege
hardening). The direct call therefore comes back 401 and the
pairing window never opens. HA's sanctioned pattern for a restricted user is
the one this fallback uses: the administrator writes a script whose action and
duration are fixed, and the restricted user only gets to *trigger* it.

What these tests are here to keep true, because the fallback is a new route to
an actuation that admits physical devices into the house network:

* the fallback is reached **only** after a 401/403 from ``zha.permit`` — a 404
  (no ZHA at all) or a transport error never fires a script;
* it names **only** the entity in ``ha.zigbee_permit_script`` /
  ``ha.zigbee_permit_close_script``. No caller argument, no model-written text,
  and no other script, can be named through this path;
* the opening direction still needs a real ``HUMAN_CONFIRMED``, and an
  unconfirmed call is refused **before any HA call at all** — including the
  script's;
* a model-written ``duration`` cannot steer it: on the script path the length
  is the script's, and PuffPAI passes no duration at all;
* the closing direction stays ungated, because a gate that can block the close
  is a gate that can hold a pairing window open.

**Nothing here may reach a real Home Assistant.** Every bridge is a recorder or
a landmine.

Run: pytest tests/test_zigbee_permit_script.py -q
"""

from __future__ import annotations

import logging

import pytest

from puffpai.ha_bridge import (
    HUMAN_CONFIRMED,
    ConfirmationRequired,
    HABridgeError,
    is_permit_close_script_call,
    permit_close_script_entity,
)
from puffpai import zigbee_manager as zm
from puffpai.zigbee_manager import (
    PERMIT_ACTION,
    SCRIPT_ACTION,
    ZigbeeManager,
    is_ha_auth_refusal,
    permit_script_entity,
    permit_script_seconds,
)

OPEN_SCRIPT = "script.puffpai_zigbee_permit"
CLOSE_SCRIPT = "script.puffpai_zigbee_permit_close"

# HA's own wording, as ``HABridge`` renders it. This same wording is read
# in ``zigbee_pairing._refusal_message``.
HA_401 = "HA returned HTTP 401: 401: Unauthorized"
HA_403 = "HA returned HTTP 403: 403: Forbidden"
HA_404 = "HA returned HTTP 404: 404: Not Found"


# ══════════════════════════════════════════════════════════════════════
#  Doubles
# ══════════════════════════════════════════════════════════════════════

class ScriptBridge:
    """Records service calls; refuses ``zha.permit`` the way a non-admin HA
    user is refused, and serves a state list the script may or may not be in."""

    ENFORCES_CONFIRMATION = True

    def __init__(self, *, permit_error: str | None = HA_401,
                 scripts=(OPEN_SCRIPT, CLOSE_SCRIPT), runs: bool = True):
        self.calls: list[tuple[str, str, object, dict]] = []
        self.permit_error = permit_error
        #: Does the script body actually run, or does HA just answer 200?
        #: HA answering 200 without the script running is a real failure mode
        #: (see tests/test_zigbee_permit_readback.py), so the honest default
        #: for a double is a script that really runs and advances
        #: last_triggered.
        self.runs = runs
        self._clock = 0
        self.states = [{"entity_id": e, "state": "off",
                        "attributes": {"last_triggered": None}}
                       for e in scripts]
        self.states.append({"entity_id": "light.hall", "state": "on"})

    def get_all_states(self, *, force: bool = False):
        return [dict(s) for s in self.states]

    def get_entity_state(self, entity_id):
        for s in self.states:
            if s.get("entity_id") == entity_id:
                return dict(s)
        return {}

    def call_service(self, domain, service, entity_id=None, **data):
        confirmation = data.pop("human_confirmed", None)
        self.calls.append((domain, service, entity_id, dict(data)))
        if (domain, service) == ("zha", "permit") and self.permit_error:
            raise HABridgeError(self.permit_error)
        if (domain, service) == ("script", "turn_on") and self.runs:
            self._clock += 1
            for s in self.states:
                if s.get("entity_id") == entity_id:
                    s["attributes"] = {
                        "last_triggered": f"2026-09-15T10:0{self._clock}:00+00:00"}
        self._last_confirmation = confirmation
        return []

    # -- readbacks the tests assert on --
    @property
    def script_calls(self):
        return [c for c in self.calls if c[0] == "script"]

    @property
    def permit_calls(self):
        return [c for c in self.calls if c[0] == "zha"]


class ExplodingBridge(ScriptBridge):
    """Any service call is a bug on a path that must refuse first."""

    def call_service(self, domain, service, entity_id=None, **data):
        raise AssertionError(
            f"{domain}.{service} on {entity_id} was reached on a call that "
            f"must have been refused before touching Home Assistant")


@pytest.fixture(autouse=True)
def zha_installed(monkeypatch):
    """ZHA exists (so the permit call is attempted) without a WebSocket."""
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", lambda self: True)


@pytest.fixture
def scripts_configured(monkeypatch):
    """Config names both scripts, with a 120 s fixed window."""
    monkeypatch.setattr(zm, "permit_script_entity",
                        lambda *, closing: CLOSE_SCRIPT if closing else OPEN_SCRIPT)
    monkeypatch.setattr(zm, "permit_script_seconds", lambda: 120)


@pytest.fixture
def no_scripts(monkeypatch):
    """Config names no script at all — the documented way to disable this."""
    monkeypatch.setattr(zm, "permit_script_entity", lambda *, closing: "")


def make_manager(bridge):
    return ZigbeeManager(url="http://ha.invalid", token="not-a-real-token",
                         bridge=bridge)


# ══════════════════════════════════════════════════════════════════════
#  1. 401 + a script that exists → the script is triggered
# ══════════════════════════════════════════════════════════════════════

def test_401_falls_back_to_the_configured_script(scripts_configured):
    bridge = ScriptBridge()
    result = make_manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert result["duration_source"] == "ha_script"
    assert result["script_entity"] == OPEN_SCRIPT
    # The window is the script's, not the caller's 60.
    assert result["duration"] == 120
    assert bridge.script_calls == [("script", "turn_on", OPEN_SCRIPT, {})], (
        "the fallback must call script.turn_on on exactly the configured "
        f"entity, with no other service data: {bridge.script_calls}")


def test_403_is_treated_the_same_as_401(scripts_configured):
    bridge = ScriptBridge(permit_error=HA_403)
    result = make_manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)
    assert result["duration_source"] == "ha_script"
    assert bridge.script_calls[0][2] == OPEN_SCRIPT


def test_the_direct_service_is_still_tried_first(scripts_configured):
    """The fallback is a fallback. A promoted HA user must keep working."""
    bridge = ScriptBridge()
    make_manager(bridge).open_permit_join(60, human_confirmed=HUMAN_CONFIRMED)
    assert bridge.calls[0][:2] == ("zha", "permit")


# ══════════════════════════════════════════════════════════════════════
#  2. The confirmation is still required, before anything
# ══════════════════════════════════════════════════════════════════════

def test_unconfirmed_open_is_refused_before_any_ha_call(scripts_configured):
    """No token, no window — and not one HTTP call on the way to finding out,
    the script's included."""
    bridge = ExplodingBridge()
    with pytest.raises(ConfirmationRequired):
        make_manager(bridge).open_permit_join(60)
    assert bridge.calls == []


def test_a_bare_true_is_not_a_confirmation(scripts_configured):
    bridge = ExplodingBridge()
    with pytest.raises(ConfirmationRequired):
        make_manager(bridge).open_permit_join(60, human_confirmed=True)
    assert bridge.calls == []


def test_the_token_is_passed_down_to_the_script_call(scripts_configured):
    """The script call goes through ``call_service`` carrying the token: the
    bridge cannot read a non-admin's stored script config, so it fails closed
    with ``force=True`` and only a real confirmation lifts it."""
    seen = {}

    class Recorder(ScriptBridge):
        def call_service(self, domain, service, entity_id=None, **data):
            if domain == "script":
                seen["confirmation"] = data.get("human_confirmed")
            return super().call_service(domain, service, entity_id, **data)

    make_manager(Recorder()).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)
    assert seen["confirmation"] is HUMAN_CONFIRMED


def test_the_script_call_is_gated_on_its_own_name(scripts_configured):
    """The second verdict names ``script.turn_on`` and the script entity, so a
    proposal a person sees says what would actually be triggered."""
    mgr = make_manager(ScriptBridge())
    with pytest.raises(ConfirmationRequired) as caught:
        mgr._permit_via_script(ScriptBridge(), closing=False,
                               human_confirmed=False, error=HA_401)
    proposal = caught.value.proposal
    assert proposal["service"] == SCRIPT_ACTION
    assert proposal["entity_id"] == OPEN_SCRIPT


# ══════════════════════════════════════════════════════════════════════
#  3. Never widen: no script, no window
# ══════════════════════════════════════════════════════════════════════

def test_no_script_configured_returns_the_original_refusal(no_scripts):
    bridge = ScriptBridge()
    result = make_manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)
    assert result["success"] is False
    assert "401" in result["error"]
    assert result["duration_source"] is None
    assert bridge.script_calls == []


def test_a_script_absent_from_ha_is_never_called(scripts_configured, caplog):
    """Do not POST a service call naming an entity that does not exist."""
    bridge = ScriptBridge(scripts=())          # HA has no such script
    with caplog.at_level(logging.WARNING):
        result = make_manager(bridge).open_permit_join(
            60, human_confirmed=HUMAN_CONFIRMED)
    assert result["success"] is False and "401" in result["error"]
    assert bridge.script_calls == []
    assert OPEN_SCRIPT in caplog.text


def test_an_unreadable_state_list_fails_closed(scripts_configured):
    class NoStates(ScriptBridge):
        def get_all_states(self, *, force: bool = False):
            raise HABridgeError("HA returned HTTP 500")

    bridge = NoStates()
    result = make_manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)
    assert result["success"] is False
    assert bridge.script_calls == []


def test_a_non_permission_error_never_fires_a_script(scripts_configured):
    """404 means there is no ZHA to permit-join at all. Triggering a script
    there would be guessing."""
    bridge = ScriptBridge(permit_error=HA_404)
    result = make_manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)
    assert result["success"] is False and "404" in result["error"]
    assert bridge.script_calls == []


def test_ha_auth_refusal_reads_only_permission_errors():
    assert is_ha_auth_refusal(HA_401) and is_ha_auth_refusal(HA_403)
    assert is_ha_auth_refusal("403: Forbidden") is True
    assert is_ha_auth_refusal(HA_404) is False
    assert is_ha_auth_refusal("connection refused") is False


def test_a_non_script_entity_in_config_disables_the_fallback(monkeypatch):
    """Only a ``script.*`` entity may be triggered this way."""
    class Cfg:
        zigbee_permit_script = "light.hall"
        zigbee_permit_close_script = ""
    monkeypatch.setattr(zm, "get_config", lambda: type("C", (), {"ha": Cfg})())
    assert permit_script_entity(closing=False) == ""
    assert permit_script_entity(closing=True) == ""


def test_the_script_window_length_is_bounded(monkeypatch):
    class Cfg:
        zigbee_permit_script_seconds = 99999
    monkeypatch.setattr(zm, "get_config", lambda: type("C", (), {"ha": Cfg})())
    assert permit_script_seconds() == 120


# ══════════════════════════════════════════════════════════════════════
#  4. A healthy zha.permit never involves a script
# ══════════════════════════════════════════════════════════════════════

def test_a_successful_permit_call_never_touches_a_script(scripts_configured):
    bridge = ScriptBridge(permit_error=None)
    result = make_manager(bridge).open_permit_join(
        45, human_confirmed=HUMAN_CONFIRMED)
    assert result["success"] is True
    assert result["duration"] == 45
    assert result["duration_source"] == PERMIT_ACTION
    assert result["script_entity"] is None
    assert bridge.script_calls == []


# ══════════════════════════════════════════════════════════════════════
#  5. A model-written duration cannot steer the script path
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("duration", [1, 60, 254])
def test_no_duration_reaches_the_script_call(scripts_configured, duration):
    """Whatever number the caller asked for, the script call carries none: the
    length is the admin's, and the appliance cannot lengthen it."""
    bridge = ScriptBridge()
    result = make_manager(bridge).open_permit_join(
        duration, human_confirmed=HUMAN_CONFIRMED)
    assert bridge.script_calls == [("script", "turn_on", OPEN_SCRIPT, {})]
    assert result["duration"] == 120


@pytest.mark.parametrize("duration", ["120; script.unlock", -1, 999, None])
def test_a_malformed_duration_is_refused_before_the_script(scripts_configured,
                                                           duration):
    bridge = ExplodingBridge()
    with pytest.raises(HABridgeError):
        make_manager(bridge).open_permit_join(
            duration, human_confirmed=HUMAN_CONFIRMED)
    assert bridge.calls == []


def test_duration_zero_is_the_close_not_an_open(scripts_configured):
    """``open_permit_join(0)`` is ZHA's close, and it must fall back to the
    *close* script — never to the one that opens a window."""
    bridge = ScriptBridge()
    result = make_manager(bridge).open_permit_join(0)
    assert result["script_entity"] == CLOSE_SCRIPT
    assert bridge.script_calls == [("script", "turn_on", CLOSE_SCRIPT, {})]


# ══════════════════════════════════════════════════════════════════════
#  6. The close
# ══════════════════════════════════════════════════════════════════════

def test_close_falls_back_to_the_close_script_ungated(scripts_configured):
    bridge = ScriptBridge()
    result = make_manager(bridge).close_permit_join()
    assert result["success"] is True
    assert result["closed"] is True
    assert result["duration"] == 0
    assert bridge.script_calls == [("script", "turn_on", CLOSE_SCRIPT, {})]


def test_the_close_presents_no_confirmation_token(scripts_configured):
    """The close must not launder a ``HUMAN_CONFIRMED`` nobody gave."""
    seen = {}

    class Recorder(ScriptBridge):
        def call_service(self, domain, service, entity_id=None, **data):
            if domain == "script":
                seen["confirmation"] = data.get("human_confirmed", "absent")
            return super().call_service(domain, service, entity_id, **data)

    make_manager(Recorder()).close_permit_join()
    assert seen["confirmation"] == "absent"


def test_a_refused_close_leaves_the_window_to_its_own_timeout(
        scripts_configured, caplog):
    class Refuser(ScriptBridge):
        def call_service(self, domain, service, entity_id=None, **data):
            if domain == "script":
                raise ConfirmationRequired({"service": SCRIPT_ACTION,
                                            "entity_id": entity_id,
                                            "domains": []},
                                           "cannot read this script's steps")
            return super().call_service(domain, service, entity_id, **data)

    with caplog.at_level(logging.WARNING):
        result = make_manager(Refuser()).close_permit_join()
    assert result["success"] is False
    assert "own timeout" in caplog.text


def test_no_close_script_configured_warns_and_does_not_raise(no_scripts,
                                                             caplog):
    bridge = ScriptBridge()
    with caplog.at_level(logging.WARNING):
        result = make_manager(bridge).close_permit_join()
    assert result["success"] is False
    assert bridge.script_calls == []


# ══════════════════════════════════════════════════════════════════════
#  7. The bridge's own exemption is exactly one entity wide
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def close_script_configured(monkeypatch):
    monkeypatch.setattr("puffpai.ha_bridge.permit_close_script_entity",
                        lambda: CLOSE_SCRIPT)


def test_the_exemption_matches_only_the_configured_close_script(
        close_script_configured):
    assert is_permit_close_script_call("script", "turn_on", CLOSE_SCRIPT, {})
    # …and nothing else.
    assert not is_permit_close_script_call("script", "turn_on", OPEN_SCRIPT, {})
    assert not is_permit_close_script_call("script", "turn_on",
                                           "script.unlock_everything", {})
    assert not is_permit_close_script_call("script", "turn_on",
                                           [CLOSE_SCRIPT, "script.other"], {})
    assert not is_permit_close_script_call("script", "toggle", CLOSE_SCRIPT, {})
    assert not is_permit_close_script_call("automation", "turn_on",
                                           CLOSE_SCRIPT, {})
    assert not is_permit_close_script_call("script", "turn_on", None, {})


def test_the_exemption_refuses_extra_service_data(close_script_configured):
    """``variables:`` is how a script is told what to do at call time."""
    assert not is_permit_close_script_call(
        "script", "turn_on", CLOSE_SCRIPT, {"variables": {"duration": 254}})


def test_an_empty_config_key_disables_the_exemption(monkeypatch):
    monkeypatch.setattr("puffpai.ha_bridge.permit_close_script_entity",
                        lambda: "")
    assert not is_permit_close_script_call("script", "turn_on", CLOSE_SCRIPT, {})


def test_permit_close_script_entity_survives_unreadable_config(monkeypatch):
    import puffpai.config as cfgmod

    def boom():
        raise RuntimeError("config is gone")

    monkeypatch.setattr(cfgmod, "get_config", boom)
    assert permit_close_script_entity() == ""


# ══════════════════════════════════════════════════════════════════════
#  8. The pairing flow end to end: consent → script → audit trail
# ══════════════════════════════════════════════════════════════════════

import importlib  # noqa: E402  (kept with the block that uses it)

from tests.test_zigbee_pairing import StubActuator, _approve, _drain  # noqa: E402


class ScriptActuator(StubActuator):
    """A manager whose direct ``zha.permit`` is 401'd and whose fallback is the
    household script — the shape ``ZigbeeManager`` returns on that path."""

    #: The script's fixed window. Small here only so the bounded watcher
    #: finishes inside a test; on the box it is the owner's 120 s.
    WINDOW_SECONDS = 3

    def __init__(self, states=None, *, close_ok=True):
        super().__init__(states)
        self.close_ok = close_ok

    def open_permit_join(self, duration=60, ieee=None, *, human_confirmed=False):
        self.opens.append((duration, human_confirmed))
        return {"success": True, "duration": self.WINDOW_SECONDS, "ieee": None,
                "closed": False, "error": None,
                "duration_source": "ha_script", "script_entity": OPEN_SCRIPT}

    def close_permit_join(self, ieee=None):
        self.closes += 1
        if not self.close_ok:
            return {"success": False, "closed": True,
                    "error": HA_401, "duration_source": None,
                    "script_entity": None}
        return {"success": True, "closed": True, "error": None,
                "duration_source": "ha_script", "script_entity": CLOSE_SCRIPT}


@pytest.fixture
def paired(tmp_path, monkeypatch):
    """Same isolation as tests/test_zigbee_pairing.py's ``paired`` fixture."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

    import puffpai.agent_activity as aa
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr
    import puffpai.zigbee_pairing as zp

    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()
    zp._session = None
    zp._spent_approvals.clear()

    monkeypatch.setattr(zp, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(zp, "GRACE_SECONDS", 1)
    monkeypatch.setattr(zp, "QUIET_CLOSE_SECONDS", 0.2)
    monkeypatch.setattr(zp, "EARLY_CLOSE_MIN_REMAINING", 0.05)

    yield zp, cr, tmp_path

    zp._session = None
    zp._spent_approvals.clear()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()


def test_the_consent_request_says_the_script_fixes_the_length(paired,
                                                              scripts_configured):
    zp, cr, _ = paired
    out = zp.request_pairing("pair my new bulbs", duration=60)
    reason = out["request"]["reason"]
    assert OPEN_SCRIPT in reason
    assert "not an administrator" in reason


def test_no_script_configured_leaves_the_consent_wording_alone(paired,
                                                              no_scripts):
    zp, cr, _ = paired
    out = zp.request_pairing("pair my new bulbs", duration=60)
    assert "script." not in out["request"]["reason"]


def test_the_window_opened_via_script_is_said_so_in_the_audit_trail(
        paired, scripts_configured, caplog):
    zp, cr, _ = paired
    actuator = ScriptActuator()

    zp.request_pairing("pair my new bulbs", duration=60)
    _approve(cr, zp)
    with caplog.at_level(logging.INFO, logger="puffpai.zigbee_pairing"):
        out = zp.complete_approved_pairing(actuator)
    _drain(zp)

    assert out["ok"] is True and out["opened"] is True
    # The script's fixed window, not the 60 s the request carried.
    assert out["window_seconds"] == ScriptActuator.WINDOW_SECONDS != 60
    assert out["duration_source"] == "ha_script"
    assert out["script_entity"] == OPEN_SCRIPT
    # A script run is reported as STARTED, never as
    # an open window, because last_triggered cannot prove the radio opened.
    assert "started your pairing script" in out["message"]
    assert "can't confirm the radio opened" in out["message"]
    assert out["status"] == "started"
    assert "STARTED" in caplog.text
    assert "radio state is not confirmed" in caplog.text

    # The durable audit trail the owner reads back, not just the log line.
    import json

    import puffpai.agent_activity as aa
    with open(aa.get_activity_log_path(), encoding="utf-8") as handle:
        feed = [json.loads(line) for line in handle if line.strip()]
    opened = [e for e in feed if e.get("event") == "pairing_window_opened"]
    assert opened, "no pairing_window_opened event was filed"
    latest = opened[-1]
    assert "via HA script" in latest["task"]
    assert (latest.get("result") or {}).get("script_entity") == OPEN_SCRIPT
    assert (latest.get("result") or {}).get("duration_source") == "ha_script"


def test_the_watchers_early_close_rides_the_same_fallback(paired,
                                                          scripts_configured):
    """The watcher calls ``close_permit_join``; with ``zha.permit`` admin-only
    that reaches HA as the close script. The watcher must treat its *result* as
    the truth, not the absence of an exception."""
    zp, cr, _ = paired
    actuator = ScriptActuator()

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)
    actuator.add({"entity_id": "light.new_bulb",
                  "attributes": {"friendly_name": "New Bulb"}, "state": "on"})
    _drain(zp)

    assert actuator.closes == 1, (
        "the watcher's early close must reach close_permit_join, which is "
        "where the close-script fallback lives")
    report = zp.read_report() or {}
    assert report.get("closed_early") is True


def test_a_close_that_did_not_take_is_not_reported_as_closed(
        paired, scripts_configured, caplog):
    zp, cr, _ = paired
    actuator = ScriptActuator(close_ok=False)

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    with caplog.at_level(logging.WARNING, logger="puffpai.zigbee_pairing"):
        zp.complete_approved_pairing(actuator)
        actuator.add({"entity_id": "light.new_bulb",
                      "attributes": {"friendly_name": "New Bulb"},
                      "state": "on"})
        _drain(zp)

    report = zp.read_report() or {}
    assert report.get("closed_early") is False, (
        "a close HA refused must not be reported as a closed window")
    assert actuator.closes == zp.MAX_EARLY_CLOSE_ATTEMPTS, (
        "a close HA keeps refusing must be retried, but capped")
    assert "own timeout" in caplog.text
