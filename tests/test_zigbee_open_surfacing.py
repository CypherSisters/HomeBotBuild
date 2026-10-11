"""HA refuses to open the Zigbee pairing window — surfaced, not swallowed.

If an owner approves a pairing request and nothing
visibly happens, that is a failure: ``zha.permit`` is admin-only in Home
Assistant while the PuffPAI HA user is not an administrator, so the refusal
comes back as an HTTP 401 from ``open_permit_join``. Returning only a
generic ``message`` with no log line would give the owner no way to tell "I
denied it" from "PuffPAI is broken" from "you need to fix Home Assistant".

This file locks in three things:

1. ``complete_approved_pairing`` logs a WARNING and returns a plain-English,
   actionable message when HA refuses with 401/403 (not-an-admin) or with
   400/404/"not found" (no ZHA integration).
2. ``pairing_status`` (the read the owner's follow-up question resolves to)
   surfaces the last refusal — "Approved at HH:MM, but Home Assistant
   refused..." — so asking "did the pairing window open?" gets a real answer.
3. The success path is unchanged.

Run: py -3.14 -m pytest tests/test_zigbee_open_surfacing.py -q
"""

from __future__ import annotations

import importlib
import logging

import pytest

from tests.test_zigbee_pairing import StubActuator, _approve, _drain


class RefusingActuator(StubActuator):
    """A permit-join call that fails the way a non-admin HA user fails."""

    def __init__(self, error: str, states=None):
        super().__init__(states)
        self._error = error

    def open_permit_join(self, duration=60, ieee=None, *, human_confirmed=False):
        self.opens.append((duration, human_confirmed))
        return {"success": False, "duration": duration, "ieee": None,
                "closed": False, "error": self._error}


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

    yield zp, cr, tmp_path

    zp._session = None
    zp._spent_approvals.clear()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()


def test_a_401_refusal_is_logged_and_named_as_an_admin_problem(paired, caplog):
    zp, cr, _ = paired
    actuator = RefusingActuator("HA returned HTTP 401: 401: Unauthorized")

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    with caplog.at_level(logging.WARNING, logger="puffpai.zigbee_pairing"):
        out = zp.complete_approved_pairing(actuator)

    assert out["ok"] is False
    assert out["opened"] is False
    assert "administrator" in out["message"].lower()
    assert "Settings" in out["message"]
    assert any("refused to open the window" in r.message for r in caplog.records)


def test_a_403_refusal_is_also_named_as_an_admin_problem(paired):
    zp, cr, _ = paired
    actuator = RefusingActuator("HA returned HTTP 403: Forbidden")

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    out = zp.complete_approved_pairing(actuator)

    assert out["ok"] is False
    assert "administrator" in out["message"].lower()


def test_a_400_or_missing_zha_is_named_as_no_integration(paired):
    zp, cr, _ = paired
    actuator = RefusingActuator("HA returned HTTP 400: entity not found")

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    out = zp.complete_approved_pairing(actuator)

    assert out["ok"] is False
    assert "no zigbee integration" in out["message"].lower() \
        or "zha" in out["message"].lower()


def test_an_unrecognised_error_keeps_the_raw_text(paired):
    zp, cr, _ = paired
    actuator = RefusingActuator("HA returned HTTP 500: internal error")

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    out = zp.complete_approved_pairing(actuator)

    assert out["ok"] is False
    assert "HTTP 500" in out["message"]


def test_a_refusal_does_not_start_a_watcher_or_spend_the_window(paired):
    """A refused open must not look like a successful one to anything else."""
    zp, cr, _ = paired
    actuator = RefusingActuator("HA returned HTTP 401: 401: Unauthorized")

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    out = zp.complete_approved_pairing(actuator)

    assert out["opened"] is False
    status = zp.pairing_status()
    assert status["window_open"] is False


def test_the_follow_up_status_surfaces_the_refusal(paired):
    zp, cr, _ = paired
    actuator = RefusingActuator("HA returned HTTP 401: 401: Unauthorized")

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    zp.complete_approved_pairing(actuator)

    status = zp.pairing_status()
    assert status["last_outcome"] is not None
    assert status["last_outcome"]["event"] == "pairing_window_refused"
    assert "Approved at" in status["message"]
    assert "refused" in status["message"].lower()
    assert "administrator" in status["message"].lower()


def test_the_success_path_is_unchanged(paired):
    zp, cr, _ = paired
    actuator = StubActuator()

    zp.request_pairing("pair my new bulbs", duration=1)
    _approve(cr, zp)
    out = zp.complete_approved_pairing(actuator)

    assert out["ok"] is True
    assert out["opened"] is True
    assert len(actuator.opens) == 1

    status = zp.pairing_status()
    assert status["last_outcome"] is not None
    assert status["last_outcome"]["event"] == "pairing_window_opened"

    _drain(zp)
