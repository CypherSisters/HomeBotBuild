"""The permit window is only "open" once the HA script says it ran.

Home Assistant answering ``200`` to ``script.turn_on`` proves one thing: HA
accepted the call. It does not prove the script body (``zha.permit``) ran, a
script that failed to load answers 200 and does nothing. Without a readback,
PuffPAI could announce an open pairing window, the
owner could power-cycle bulbs at it, and nothing would ever join because
``script.puffpai_zigbee_permit`` was never triggered at all.

So the open direction now reads the entity back through the same everyday
(non-admin) bridge the call went through, and refuses to claim a window unless
``attributes.last_triggered`` moved (or the script is caught mid-run). These
tests pin that read-back, both directions:

* it moved → success, ``verified: True``, and the stamp travels with the result;
* it never moved → the ordinary refusal shape with ``reason ==
  "script_not_triggered"``, **no watcher**, and plain words for the owner;
* a null-before → non-null-after counts (a script triggered for the first time);
* ``attributes.current >= 1`` / ``state == "on"`` counts (a long script caught
  while still running, before ``last_triggered`` propagates).

Nothing here reaches a real Home Assistant.

Run: pytest tests/test_zigbee_permit_readback.py -q
"""

from __future__ import annotations

import pytest

from puffpai.ha_bridge import HUMAN_CONFIRMED, HABridgeError
from puffpai import zigbee_manager as zm
from puffpai import zigbee_pairing as zp
from puffpai.zigbee_manager import (
    DURATION_SOURCE_SCRIPT,
    SCRIPT_NOT_TRIGGERED,
    SCRIPT_NOT_TRIGGERED_MESSAGE,
    ZigbeeManager,
)

OPEN_SCRIPT = "script.puffpai_zigbee_permit"
HA_401 = "HA returned HTTP 401: 401: Unauthorized"


class ReadbackBridge:
    """A non-admin HA: refuses ``zha.permit``, serves the script's state.

    ``behaviour`` decides what the script does when triggered, which is the
    only axis these tests care about.
    """

    ENFORCES_CONFIRMATION = True

    def __init__(self, *, behaviour: str = "advances",
                 before: str | None = "2026-09-15T09:00:00+00:00"):
        self.behaviour = behaviour
        self.calls: list[tuple[str, str, object]] = []
        self.reads = 0
        self.state = {"entity_id": OPEN_SCRIPT, "state": "off",
                      "attributes": {"last_triggered": before}}

    def get_all_states(self, *, force: bool = False):
        return [dict(self.state)]

    def get_entity_state(self, entity_id):
        self.reads += 1
        return dict(self.state) if entity_id == OPEN_SCRIPT else {}

    def call_service(self, domain, service, entity_id=None, **data):
        data.pop("human_confirmed", None)
        self.calls.append((domain, service, entity_id))
        if (domain, service) == ("zha", "permit"):
            raise HABridgeError(HA_401)
        if (domain, service) == ("script", "turn_on"):
            if self.behaviour == "advances":
                self.state["attributes"] = {
                    "last_triggered": "2026-09-15T10:00:00+00:00"}
            elif self.behaviour == "running":
                self.state = {"entity_id": OPEN_SCRIPT, "state": "on",
                              "attributes": {"current": 1,
                                             "last_triggered": None}}
            # "silent": HA said 200 and the script never ran.
        return []


@pytest.fixture(autouse=True)
def zha_installed(monkeypatch):
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", lambda self: True)


@pytest.fixture(autouse=True)
def scripts_configured(monkeypatch):
    monkeypatch.setattr(zm, "permit_script_entity",
                        lambda *, closing: "" if closing else OPEN_SCRIPT)
    monkeypatch.setattr(zm, "permit_script_seconds", lambda: 120)


def _manager(bridge):
    mgr = ZigbeeManager(url="http://ha.invalid", token="t", bridge=bridge)
    mgr._readback_sleep = lambda seconds: None     # no real waiting in tests
    return mgr


# ══════════════════════════════════════════════════════════════════════
#  (a) it advanced → verified
# ══════════════════════════════════════════════════════════════════════

def test_advanced_last_triggered_opens_a_verified_window():
    bridge = ReadbackBridge(behaviour="advances")
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert result["duration_source"] == DURATION_SOURCE_SCRIPT
    assert result["verified"] is True
    assert result["last_triggered"] == "2026-09-15T10:00:00+00:00"
    assert ("script", "turn_on", OPEN_SCRIPT) in bridge.calls


def test_the_stamp_is_read_before_the_call_so_a_stale_value_is_not_proof():
    """A script whose last_triggered already had a value, and never moves,
    must not be mistaken for one that just ran."""
    bridge = ReadbackBridge(behaviour="silent",
                            before="2026-09-14T22:15:00+00:00")
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is False
    assert result["reason"] == SCRIPT_NOT_TRIGGERED


# ══════════════════════════════════════════════════════════════════════
#  (b) it never advanced → refusal, and no watcher
# ══════════════════════════════════════════════════════════════════════

def test_silent_script_is_a_refusal_not_an_open_window():
    bridge = ReadbackBridge(behaviour="silent")
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is False
    assert result["reason"] == SCRIPT_NOT_TRIGGERED
    assert result["duration_source"] is None
    assert result["script_entity"] == OPEN_SCRIPT
    assert SCRIPT_NOT_TRIGGERED in result["error"]
    assert bridge.reads >= zm.PERMIT_READBACK_ATTEMPTS


def test_silent_script_warns_with_both_timestamps(caplog):
    caplog.set_level("WARNING")
    _manager(ReadbackBridge(behaviour="silent",
                            before="2026-09-14T22:15:00+00:00")
             ).open_permit_join(60, human_confirmed=HUMAN_CONFIRMED)

    warned = " ".join(r.getMessage() for r in caplog.records
                      if r.levelname == "WARNING")
    assert "2026-09-14T22:15:00+00:00" in warned
    assert "never ran" in warned


def test_a_silent_script_starts_no_watcher_and_reaches_the_owner(monkeypatch):
    """End to end through the pairing module: a window that never opened must
    not start the watcher thread, and must be named in plain words."""
    bridge = ReadbackBridge(behaviour="silent")
    mgr = _manager(bridge)

    started: list[object] = []

    class _NoThread:
        def __init__(self, *a, **kw):
            started.append(kw)

        def start(self):
            raise AssertionError("the watcher started on a window that never "
                                 "opened")

    monkeypatch.setattr(zp.threading, "Thread", _NoThread)
    monkeypatch.setattr(zp, "_resolve_actuators", lambda actuator: (mgr, bridge))
    monkeypatch.setattr(zp, "_recent_approval",
                        lambda request: {"resolved_at": "2026-09-15T10:00:00Z",
                                         "resolved_by": "alex",
                                         "context": {"duration": 60}})
    logged: list[tuple] = []
    monkeypatch.setattr(zp, "_log",
                        lambda ev, msg, status, detail=None:
                        logged.append((ev, detail)))

    result = zp.complete_approved_pairing()

    assert result["ok"] is False and result["opened"] is False
    assert result["reason"] == SCRIPT_NOT_TRIGGERED
    assert result["message"] == SCRIPT_NOT_TRIGGERED_MESSAGE
    assert started == []
    assert [ev for ev, _ in logged] == ["pairing_window_refused"]
    assert logged[0][1]["reason"] == SCRIPT_NOT_TRIGGERED


def test_status_prose_surfaces_the_reason():
    """The spoken "pairing status" answer reads the last outcome, so it has to
    name this one too rather than calling it an unknown HA error."""
    outcome = {"event": "pairing_window_refused",
               "timestamp": "2026-09-15T10:02:00Z",
               "result": {"error": f"{SCRIPT_NOT_TRIGGERED}: "
                                   f"{SCRIPT_NOT_TRIGGERED_MESSAGE}",
                          "reason": SCRIPT_NOT_TRIGGERED}}
    prose = zp._status_prose(False, 0, False, [], None, outcome)

    assert "never actually opened" in prose
    assert "last-triggered time didn't change" in prose
    assert "administrator" not in prose


# ══════════════════════════════════════════════════════════════════════
#  (c) null before → non-null after counts
# ══════════════════════════════════════════════════════════════════════

def test_first_ever_trigger_counts():
    bridge = ReadbackBridge(behaviour="advances", before=None)
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert result["verified"] is True
    assert result["last_triggered"] == "2026-09-15T10:00:00+00:00"


# ══════════════════════════════════════════════════════════════════════
#  (d) caught mid-run counts
# ══════════════════════════════════════════════════════════════════════

def test_script_caught_running_counts_as_proof():
    bridge = ReadbackBridge(behaviour="running", before=None)
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert result["verified"] is True
    assert result["duration_source"] == DURATION_SOURCE_SCRIPT


def test_all_reads_raising_opens_unverified(caplog):
    """Every read-back attempt raising is not proof the script never ran —
    it's an inability to check. That degrades to unverified rather than
    refusing a window that may well be open."""
    caplog.set_level("WARNING")

    class Blind(ReadbackBridge):
        def get_entity_state(self, entity_id):
            self.reads += 1
            raise HABridgeError("HA returned HTTP 500")

    bridge = Blind(behaviour="advances")
    # It still has to *exist* to be called at all; the existence check reads
    # the state list, which works.
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert result["verified"] is False
    assert result["readback"] == "unreadable"
    assert bridge.reads >= zm.PERMIT_READBACK_ATTEMPTS
    warned = " ".join(r.getMessage() for r in caplog.records
                      if r.levelname == "WARNING")
    assert "read-back unavailable" in warned
    assert "proceeding unverified" in warned


def test_all_reads_returning_none_opens_unverified():
    """Same as an all-raising read-back: a bridge that hands back ``None``
    every attempt gives no proof either way, so this also degrades to
    unverified instead of refusing."""
    class Blank(ReadbackBridge):
        def get_entity_state(self, entity_id):
            self.reads += 1
            return None

    bridge = Blank(behaviour="advances")
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert result["verified"] is False
    assert result["readback"] == "unreadable"
    assert bridge.reads >= zm.PERMIT_READBACK_ATTEMPTS


def test_readable_but_unchanged_still_refuses():
    """A readable entity whose last_triggered never moves (and never shows
    as running) is unchanged behaviour: it refuses, same as the silent-script
    case above, because we *did* check and it said no."""
    bridge = ReadbackBridge(behaviour="silent")
    result = _manager(bridge).open_permit_join(
        60, human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is False
    assert result["reason"] == SCRIPT_NOT_TRIGGERED


# ══════════════════════════════════════════════════════════════════════
#  the close direction is untouched
# ══════════════════════════════════════════════════════════════════════

def test_closing_never_waits_on_a_read_back(monkeypatch):
    """Closing must stay fast and ungated: no poll, no refusal path."""
    monkeypatch.setattr(zm, "permit_script_entity",
                        lambda *, closing: OPEN_SCRIPT)
    bridge = ReadbackBridge(behaviour="silent")
    mgr = _manager(bridge)
    mgr._readback_sleep = lambda seconds: (_ for _ in ()).throw(
        AssertionError("the close direction polled"))

    result = mgr.close_permit_join()

    assert result["success"] is True
    assert result["closed"] is True
