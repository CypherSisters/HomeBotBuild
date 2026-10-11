"""Admin-direct Zigbee permit and the honest close report.

A run of script.puffpai_zigbee_permit proves only that the script STARTED
(last_triggered), not that the radio opened; the close report must count
devices, not entities, and reach the chat. Pinned here:

* admin_permit_join sends exactly ONE fixed call (POST services/zha/permit,
  {"duration": n}, 0..254) and only with the in-process HUMAN_CONFIRMED and an
  unlocked vault; the token never reaches a log;
* no tool, agent or HTTP route can reach it: only zigbee_pairing calls it;
* an approved pairing opens through it when the vault is open ("open", HA
  accepted) and falls back to the script when locked ("started", radio not
  confirmed);
* the close report counts devices and is posted to the chat thread.

Run: python -m pytest -q -p no:cacheprovider tests/test_zigbee_admin_permit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from puffpai import ha_provisioning as hp          # noqa: E402
from puffpai import zigbee_pairing as zp           # noqa: E402

ADMIN = "ADMIN-TOKEN-" + "f" * 40


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _Session:
    calls: list = []
    status = 200

    def __init__(self):
        self.headers = {}
        _Session.calls.append(("NEW", None, None))

    def post(self, url, json=None, **kw):
        _Session.calls.append(("POST", url, json))
        return _Resp(_Session.status)

    def get(self, url, **kw):
        _Session.calls.append(("GET", url, None))
        return _Resp(404)

    def close(self):
        pass


@pytest.fixture
def http(monkeypatch):
    import requests
    _Session.calls = []
    _Session.status = 200
    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setattr(hp, "_ha_base_url", lambda: "http://ha.test")
    monkeypatch.setattr(hp, "enabled", lambda: True)
    return _Session


def _vault(monkeypatch, unlocked=True):
    from puffpai.security import ha_token_source as hts
    monkeypatch.setattr(
        hts, "resolve_ha_admin_token",
        lambda session_id=None: hts.Resolution(
            ADMIN if unlocked else None,
            hts.SOURCE_VAULT_SESSION if unlocked else None, "x"))


def _hc():
    from puffpai.ha_bridge import HUMAN_CONFIRMED
    return HUMAN_CONFIRMED


# ── the gates ───────────────────────────────────────────────────────

@pytest.mark.parametrize("confirmation", [False, True, "HUMAN_CONFIRMED", None, 1])
def test_no_in_process_confirmation_no_call(http, monkeypatch, confirmation):
    _vault(monkeypatch)
    out = hp.admin_permit_join(120, human_confirmed=confirmation)
    assert out["ok"] is False and out["attempted"] is False
    assert http.calls == []


@pytest.mark.parametrize("duration", [-1, 255, 1000, True, "120", 1.5, None])
def test_duration_is_bounded(http, monkeypatch, duration):
    _vault(monkeypatch)
    out = hp.admin_permit_join(duration, human_confirmed=_hc())
    assert out["attempted"] is False and http.calls == []


def test_locked_vault_sends_nothing(http, monkeypatch):
    _vault(monkeypatch, unlocked=False)
    out = hp.admin_permit_join(120, human_confirmed=_hc())
    assert out == {"ok": False, "attempted": False, "vault_locked": True,
                   "duration": 120, "error": "elevated credential unavailable"}
    assert http.calls == []


def test_one_fixed_call_and_the_token_never_logged(http, monkeypatch, caplog, capsys):
    _vault(monkeypatch)
    with caplog.at_level("DEBUG"):
        out = hp.admin_permit_join(120, human_confirmed=_hc())
    assert out["ok"] is True and out["attempted"] is True
    posts = [c for c in http.calls if c[0] == "POST"]
    assert posts == [("POST", "http://ha.test/api/services/zha/permit",
                      {"duration": 120})]
    assert ADMIN not in caplog.text and ADMIN not in capsys.readouterr().out


def test_ha_refusal_is_reported_as_attempted(http, monkeypatch):
    _vault(monkeypatch)
    http.status = 401
    out = hp.admin_permit_join(120, human_confirmed=_hc())
    assert out["ok"] is False and out["attempted"] is True
    assert out["error"] == "HTTP 401"


def test_only_zigbee_pairing_can_reach_the_admin_permit():
    callers = []
    for path in (ROOT / "src" / "puffpai").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "admin_permit_join(" in text:
            callers.append(path.name)
    assert sorted(callers) == ["ha_provisioning.py", "zigbee_pairing.py"]


# ── complete_approved_pairing ───────────────────────────────────────

class _Manager:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def open_permit_join(self, seconds, human_confirmed=None):
        self.calls += 1
        return dict(self.result)


@pytest.fixture
def pairing(monkeypatch):
    zp._session = None
    zp._spent_approvals.clear()
    monkeypatch.setattr(zp, "_recent_approval", lambda request=None: {
        "resolved_at": "2026-10-09T05:00:00+00:00", "resolved_by": "user_via_ui",
        "context": {"duration": 120}})
    monkeypatch.setattr(zp, "_entity_snapshot", lambda bridge: {})
    monkeypatch.setattr(zp, "_watch", lambda *a, **k: None)
    monkeypatch.setattr(zp, "_log", lambda *a, **k: None)
    yield
    zp._session = None
    zp._spent_approvals.clear()


def test_unlocked_vault_opens_admin_direct_and_says_open(pairing, monkeypatch):
    sent = []
    monkeypatch.setattr(hp, "admin_permit_join",
                        lambda d, **kw: sent.append((d, kw.get("human_confirmed") is _hc()))
                        or {"ok": True, "attempted": True})
    manager = _Manager({"success": True})
    monkeypatch.setattr(zp, "_resolve_actuators", lambda a: (manager, object()))
    out = zp.complete_approved_pairing(object())
    assert sent == [(120, True)]
    assert manager.calls == 0
    assert out["duration_source"] == "admin_direct" and out["status"] == "open"
    assert "Home Assistant accepted it" in out["message"]


def test_locked_vault_falls_back_to_the_script_and_says_started(pairing, monkeypatch):
    monkeypatch.setattr(hp, "admin_permit_join",
                        lambda d, **kw: {"ok": False, "attempted": False,
                                         "vault_locked": True})
    manager = _Manager({"success": True, "duration": 120,
                        "duration_source": "ha_script",
                        "script_entity": "script.puffpai_zigbee_permit",
                        "verified": True})
    monkeypatch.setattr(zp, "_resolve_actuators", lambda a: (manager, object()))
    out = zp.complete_approved_pairing(object())
    assert manager.calls == 1
    assert out["status"] == "started"
    assert out["message"].startswith(
        "I started your pairing script (script.puffpai_zigbee_permit).")
    assert "can't confirm the radio opened" in out["message"]
    assert "is open" not in out["message"]


# ── the close report ────────────────────────────────────────────────

def test_devices_not_entities():
    entities = {
        "light.living_room_sofa_1": "Sofa 1",
        "button.living_room_sofa_1_identify": "Sofa 1 Identify",
        "number.living_room_sofa_1_on_level": "Sofa 1 On level",
        "light.living_room_sofa_2": "Sofa 2",
        "select.living_room_sofa_2_power_on_behavior": "Sofa 2 Power-on behavior",
    }
    assert zp.group_devices(entities) == ["Sofa 1", "Sofa 2"]
    twins = {"light.third_reality_inc_3rcb01057z": "Third Reality 3RCB01057Z",
             "light.third_reality_inc_3rcb01057z_2": "Third Reality 3RCB01057Z",
             "button.third_reality_inc_3rcb01057z_identify": "Third Reality 3RCB01057Z Identify"}
    assert len(zp.group_devices(twins)) == 2


def test_close_report_goes_to_the_chat_thread_and_offers_a_reopen(monkeypatch):
    from puffpai import announcer
    spoken, posted = [], []
    monkeypatch.setattr(announcer, "announce",
                        lambda text, **kw: spoken.append(text) or {"ok": True})
    zp.set_thread_poster(lambda text, conv=None: posted.append(text) or True)
    zp.clear_reopen_offer()
    try:
        zp._announce_result(["Sofa 1", "Sofa 2"], {"light.living_room_sofa_1"})
        assert posted[-1] == spoken[-1] == (
            "The pairing window closed. 2 new devices joined: Sofa 1 and "
            "Sofa 2. I've asked on the dashboard whether I should manage them.")
        assert not zp.reopen_offer_active()
        zp._announce_result([], set(), {"opened_via": "ha_script"})
        assert "nothing new joined" in posted[-1]
        assert "only know the script started" in posted[-1]
        assert zp.reopen_offer_active()
    finally:
        zp.set_thread_poster(None)
        zp.clear_reopen_offer()
