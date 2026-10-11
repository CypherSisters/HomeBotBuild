"""Approved-with-the-vault-locked finishes on the next unlock.

An automation approved with the dashboard button while the vault is locked
cannot run at once (the elevated credential is not available); it must not
vanish silently either. Pinned here:

* the button response says ``needs_pin`` with a plain message, and the record
  waits (30 min, that record only) in ``ha_pending_apply``;
* a PIN typed on the card applies THAT record; three wrong PINs void it;
* the next FULL vault unlock applies only approved, waiting records, each once,
  with the allowlist re-validated at apply time, and posts the result;
* after 30 minutes the record expires with a plain note and is voided;
* the vault fires unlock events only after a full unlock, and the HA PIN flow
  suppresses its own;
* Puff's next chat or voice turn mentions the waiting approval once.

Run: python -m pytest -q -p no:cacheprovider tests/test_ha_approve_finish.py
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from puffpai import ha_pending_apply as hpa          # noqa: E402
from puffpai import ha_provisioning as hp            # noqa: E402

ADMIN = "ADMIN-TOKEN-" + "z" * 40
PAYLOAD = {"alias": "PuffPAI Sofa 1 Sofa 2 off after midnight no motion",
           "trigger": [{"platform": "time", "at": "00:00:00"}],
           "action": [{"service": "light.turn_off",
                       "target": {"entity_id": ["light.living_room_sofa_1"]}}]}
OID = "puffpai_sofa_1_sofa_2_off_after_midnight_no_motion"
EID = "automation." + OID


class _Session:
    posts: list = []

    def __init__(self):
        self.headers = {}

    def get(self, url, **kw):
        class R:
            status_code, content = 404, b""
        return R()

    def post(self, url, json=None, **kw):
        _Session.posts.append(url)

        class R:
            status_code, content, text = 200, b"", ""
        return R()

    def close(self):
        pass


class _Bridge:
    def get_all_states(self, force=False):
        made = any("config/automation/config" in u for u in _Session.posts)
        return [{"entity_id": EID, "attributes": {}}] if made else []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFFPAI_HA_PROVISIONING", "1")
    import puffpai.agent_activity as aa
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    hp.reset_state()
    hpa.reset()
    monkeypatch.setattr(hp, "_ha_base_url", lambda: "http://ha.test")
    monkeypatch.setattr(hp, "VERIFY_ATTEMPTS", 1)
    monkeypatch.setattr(hp, "VERIFY_DELAY_SECONDS", 0.0)
    import requests
    _Session.posts = []
    monkeypatch.setattr(requests, "Session", _Session)
    posted = []
    monkeypatch.setattr(hpa, "_poster", lambda text, conv=None: posted.append((text, conv)))
    monkeypatch.setattr(hpa, "_bridge_getter", lambda: _Bridge())
    unlocked = {"sid": None}
    from puffpai.security import ha_token_source as hts
    monkeypatch.setattr(hts, "resolve_ha_admin_token", lambda session_id=None:
                        hts.Resolution(ADMIN, hts.SOURCE_VAULT_SESSION, "u")
                        if session_id and session_id == unlocked["sid"]
                        else hts.Resolution(None, None, hts.ADMIN_LOCKED_DETAIL))
    yield cr, posted, unlocked
    hpa.reset()
    hp.reset_state()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()


@pytest.fixture
def api(env, monkeypatch):
    from puffpai import api_server as a

    class _Tools:
        _ha_bridge = _Bridge()
        _llm_chat = None

    monkeypatch.setattr(a, "_get_assistant_tools", lambda: _Tools())
    a._ha_pin_reset()
    yield a
    a._ha_pin_reset()


def _propose(conv="c-owner"):
    hp.propose_ha_change("automation", OID, PAYLOAD,
                         "Every night after midnight ...", conversation_id=conv)


#: The paired device that taps Approve (the dashboard proxy asserts it from
#: the verified pairing cookie; the test client is a loopback peer).
DEVICE = "kiosk-tablet-1"
APPROVER = f"device:{DEVICE}"


def _button(api, body, device=DEVICE):
    headers = {"X-API-Key": api._API_KEY}
    if device:
        headers["X-Puff-Device-Id"] = device
    return api.app.test_client().post(
        f"/sentinel/consent/requests/{EID}", json=body,
        headers=headers).get_json()


def _unlocked_by(sid, requester):
    from puffpai.security import vault as vmod
    vmod._UNLOCK_REQUESTERS[sid] = requester


def _writes():
    return [u for u in _Session.posts if "config/automation/config" in u]


# ── 1. the button response and the waiting state ────────────────────

def test_button_approval_with_a_locked_vault_says_needs_pin_and_waits(api, env):
    _propose()
    out = _button(api, {"approve": True})
    assert out["needs_pin"] is True
    assert "is waiting for your vault PIN" in out["message"]
    assert "30 minutes" in out["message"]
    assert [e["entity_id"] for e in hpa.list_waiting()] == [EID]
    listed = api.app.test_client().get(
        "/sentinel/consent/requests", headers={"X-API-Key": api._API_KEY}).get_json()
    assert listed["waiting_pin"][0]["entity_id"] == EID
    assert 0 < listed["waiting_pin"][0]["expires_in"] <= hpa.WAIT_SECONDS
    assert _writes() == []


def test_the_next_unlock_applies_it_exactly_once_and_posts_the_result(api, env):
    cr, posted, unlocked = env
    _propose()
    _button(api, {"approve": True})
    unlocked["sid"] = "sid-1"
    msgs = hpa.apply_waiting("sid-1", requester=APPROVER)
    assert len(_writes()) == 1
    assert msgs == [f"Done: '{PAYLOAD['alias']}' is in Home Assistant and turned on."]
    assert posted[-1] == (msgs[0], "c-owner")
    assert hpa.apply_waiting("sid-1", requester=APPROVER) == []
    assert len(_writes()) == 1
    _unlocked_by("sid-1", APPROVER)
    hpa.on_unlock("sid-1")
    assert len(_writes()) == 1


def test_concurrent_unlocks_still_apply_once(api, env):
    cr, posted, unlocked = env
    _propose()
    _button(api, {"approve": True})
    unlocked["sid"] = "sid-c"
    _unlocked_by("sid-c", APPROVER)
    threads = [threading.Thread(target=hpa.on_unlock, args=("sid-c",)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(_writes()) == 1


def test_a_locked_session_applies_nothing_and_keeps_waiting(api, env):
    _propose()
    _button(api, {"approve": True})
    assert hpa.apply_waiting("not-unlocked") == []
    assert [e["entity_id"] for e in hpa.list_waiting()] == [EID]


def test_only_approved_records_are_ever_applied(env):
    cr, posted, unlocked = env
    _propose()                                     # open, never approved
    req = cr.list_open()[0]
    assert hpa.add_waiting(req) is None            # cannot even enter
    hpa._save([{"entity_id": EID, "resolved_at": "forged", "name": "x",
                "conversation_id": None, "waiting_since": time.time(),
                "mentioned": False}])
    unlocked["sid"] = "sid-2"
    assert hpa.apply_waiting("sid-2") == [] and _writes() == []


def test_the_allowlist_is_re_validated_at_apply_time(api, env):
    cr, posted, unlocked = env
    _propose()
    _button(api, {"approve": True})
    items = cr._load()
    for r in items:
        if r["entity_id"] == EID:
            r["context"]["payload"]["action"] = [
                {"service": "lock.unlock", "target": {"entity_id": ["lock.front_door"]}}]
    cr._save(items)
    unlocked["sid"] = "sid-3"
    msgs = hpa.apply_waiting("sid-3", requester=APPROVER)
    assert _writes() == [] and "can't apply" in msgs[0]


def test_it_expires_after_30_minutes_with_a_plain_note(api, env):
    cr, posted, unlocked = env
    _propose()
    out = _button(api, {"approve": True})
    entries = hpa._load()
    entries[0]["waiting_since"] = time.time() - hpa.WAIT_SECONDS - 5
    hpa._save(entries)
    assert hpa.list_waiting() == []
    assert "expired before the vault was unlocked" in posted[-1][0]
    assert out["request"]["resolved_at"] in hp._spent_approvals
    unlocked["sid"] = "sid-4"
    assert hpa.apply_waiting("sid-4") == [] and _writes() == []


# ── 2. the PIN on the card ──────────────────────────────────────────

class _Vault:
    is_initialized = True

    def __init__(self, good="482913"):
        self.good, self.logged_out, self.notify = good, [], []

    def authenticate_mfa(self, pin, totp_code=None, *, notify=True):
        from puffpai.security.vault import AuthenticationError
        self.notify.append(notify)
        if pin != self.good:
            raise AuthenticationError("bad")

        class S:
            session_id = "sid-card"
        return S()

    def logout(self, sid):
        self.logged_out.append(sid)


def test_a_pin_on_the_card_applies_that_record_once(api, env, monkeypatch, caplog, capsys):
    cr, posted, unlocked = env
    vault = _Vault()
    monkeypatch.setattr(api, "_get_vault", lambda: vault)
    monkeypatch.setattr(api, "_vault_display_rl_check", lambda: (True, 0))
    monkeypatch.setattr(api, "_vault_display_rl_note", lambda: None)
    _propose()
    assert _button(api, {"approve": True})["needs_pin"] is True
    unlocked["sid"] = "sid-card"
    with caplog.at_level("DEBUG"):
        out = _button(api, {"approve": True, "pin": "482913"})
    assert out["needs_pin"] is False
    assert "is in Home Assistant and turned on" in out["message"]
    assert len(_writes()) == 1 and vault.logged_out == ["sid-card"]
    assert vault.notify == [False]            # the PIN flow fires no unlock event
    assert "482913" not in caplog.text + capsys.readouterr().out
    assert hpa.list_waiting() == []


def test_three_wrong_pins_on_the_card_void_it(api, env, monkeypatch):
    vault = _Vault()
    monkeypatch.setattr(api, "_get_vault", lambda: vault)
    monkeypatch.setattr(api, "_vault_display_rl_check", lambda: (True, 0))
    monkeypatch.setattr(api, "_vault_display_rl_note", lambda: None)
    _propose()
    _button(api, {"approve": True})
    assert "Try once more" in _button(api, {"approve": True, "pin": "000000"})["message"]
    assert "Try once more" in _button(api, {"approve": True, "pin": "111111"})["message"]
    last = _button(api, {"approve": True, "pin": "222222"})
    assert "haven't made the change" in last["message"]
    assert hpa.list_waiting() == [] and _writes() == []


# ── 3. vault unlock events ──────────────────────────────────────────

def test_unlock_events_fire_only_after_a_full_unlock(tmp_path, monkeypatch):
    from puffpai.security import vault as vmod
    seen = []
    done = threading.Event()

    def listener(sid):
        seen.append(sid)
        done.set()

    monkeypatch.setattr(vmod, "_UNLOCK_LISTENERS", [listener])
    v = vmod.Vault(vault_dir=tmp_path / "vault")
    v.initialize("8241963570")
    seen.clear()
    done.clear()
    s1 = v.authenticate("8241963570", notify=False)
    time.sleep(0.2)
    assert seen == []
    s2 = v.authenticate_mfa("8241963570")
    assert done.wait(2) and seen == [s2.session_id]
    seen.clear()
    monkeypatch.setattr(v, "required_factors", lambda: ["pin", "totp"])
    s3 = v.authenticate("8241963570")              # PIN-only while TOTP enrolled
    time.sleep(0.2)
    assert seen == []                           # not a full unlock
    with pytest.raises(vmod.AuthenticationError):
        v.authenticate_mfa("8241963570")            # missing TOTP
    time.sleep(0.2)
    assert seen == []


# ── 4. the one-time chat / voice mention ────────────────────────────

def test_next_chat_and_voice_turn_mention_it_once(api, env, monkeypatch):
    _propose()
    _button(api, {"approve": True})
    monkeypatch.setattr(api, "_process_chat_query_unwrapped",
                        lambda *a, **k: {"response": "Hi."})
    monkeypatch.setattr(api, "_process_voice_query_unwrapped", lambda *a, **k: "Hi.")
    first = api._process_voice_query("hello")
    assert first.startswith(f"Your approval for '{PAYLOAD['alias']}' is waiting "
                            f"for your vault PIN.")
    assert api._process_voice_query("hello") == "Hi."
    assert api._process_chat_query("hello")["response"] == "Hi."


def test_a_chat_yes_keeps_the_existing_flow_and_is_not_mentioned_twice(api, env, monkeypatch):
    """"yes" + the PIN in chat creates it, as before."""
    cr, posted, unlocked = env
    vault = _Vault()
    monkeypatch.setattr(api, "_get_vault", lambda: vault)
    monkeypatch.setattr(api, "_vault_display_rl_check", lambda: (True, 0))
    monkeypatch.setattr(api, "_vault_display_rl_note", lambda: None)
    _propose(conv="t-chat")
    first = api._try_consent_approval("yes", "t-chat")
    assert first.startswith("Approved.") and api.HA_PIN_ASK in first
    assert hpa.take_unmentioned_notice() is None        # already asked in chat
    unlocked["sid"] = "sid-card"
    reply = api._try_instant_answer("482913", conversation_id="t-chat")
    assert "is in Home Assistant and turned on" in reply
    assert len(_writes()) == 1 and hpa.list_waiting() == []


# ── used approvals survive a restart ──────────────────────────────

def test_a_used_approval_planted_back_after_a_restart_never_re_applies(api, env):
    cr, posted, unlocked = env
    _propose()
    _button(api, {"approve": True})
    planted = [dict(e) for e in hpa._load()]
    unlocked["sid"] = "sid-r"
    hpa.apply_waiting("sid-r", requester=APPROVER)
    assert len(_writes()) == 1
    stamp = planted[0]["resolved_at"]
    assert stamp in json.loads(open(hpa._used_path(), encoding="utf-8").read())
    # Restart: the in-memory spent set is gone; the waiting entry is planted.
    hp._spent_approvals.clear()
    planted[0]["waiting_since"] = time.time()
    hpa._save(planted)
    assert hpa.apply_waiting("sid-r", requester=APPROVER) == []
    assert len(_writes()) == 1
    # …and the direct path refuses it too.
    out = hp.apply_approved_change(request=hpa._approved_record(planted[0]),
                                   bridge=_Bridge(),
                                   human_confirmed=__import__(
                                       "puffpai.ha_bridge", fromlist=["x"]
                                   ).HUMAN_CONFIRMED,
                                   vault_session_id="sid-r",
                                   freshness_seconds=hpa.WAIT_SECONDS)
    assert out.get("ok") is False and out.get("created") is False
    assert len(_writes()) == 1


def test_the_used_ledger_is_capped_and_ages_out(env, monkeypatch):
    monkeypatch.setattr(hpa, "USED_MAX", 3)
    for i in range(5):
        hpa.note_used(f"stamp-{i}")
    used = json.loads(open(hpa._used_path(), encoding="utf-8").read())
    assert sorted(used) == ["stamp-2", "stamp-3", "stamp-4"]
    assert hpa.was_used("stamp-4") and not hpa.was_used("stamp-0")
    later = time.time() + hpa.USED_TTL_SECONDS + 5
    monkeypatch.setattr(hpa, "_now", lambda: later)
    assert not hpa.was_used("stamp-4")


def test_an_unreadable_used_ledger_fails_closed(env):
    with open(hpa._used_path(), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert hpa.was_used("anything") is True
    assert hp.approval_spent("anything") is True


def test_a_corrupt_ledger_is_kept_aside_and_never_shrinks(env, monkeypatch):
    import glob
    import os
    from datetime import datetime, timezone

    def iso(offset):
        return datetime.fromtimestamp(time.time() + offset,
                                      timezone.utc).isoformat()

    path = hpa._used_path()
    before = iso(-600)                           # spent before the damage
    hpa.note_used(before)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"' + before + '": 17')        # truncated write
    corrupt = open(path, encoding="utf-8").read()
    # A new spend must not rewrite the file with only itself.
    hpa.note_used("2026-10-10T06:05:00+00:00")
    aside = glob.glob(path + ".corrupt-*")
    assert len(aside) == 1
    assert open(aside[0], encoding="utf-8").read() == corrupt
    # Every approval made before the quarantine is refused (fail closed),
    # including ones the corrupt file may have held and a restart forgot.
    hp._spent_approvals.clear()
    assert hpa.was_used(before) and hp.approval_spent(before)
    assert hpa.was_used(iso(-3600))
    assert hpa.was_used("not-a-timestamp")
    # An approval made after the quarantine is judged by the new ledger.
    fresh = iso(60)
    assert not hpa.was_used(fresh)
    hpa.note_used(fresh)
    assert hpa.was_used(fresh)
    # The marker survives further writes and the cap.
    monkeypatch.setattr(hpa, "USED_MAX", 1)
    hpa.note_used("2026-10-10T07:00:00+00:00")
    assert hpa.QUARANTINE_KEY in json.loads(open(path, encoding="utf-8").read())
    assert len(glob.glob(path + ".corrupt-*")) == 1


def test_a_corrupt_ledger_that_cannot_be_moved_keeps_refusing(env, monkeypatch):
    path = hpa._used_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("[]")                                # not an object
    import os

    def no_replace(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(os, "replace", no_replace)
    hpa.note_used("2026-10-10T06:05:00+00:00")
    assert open(path, encoding="utf-8").read() == "[]"   # never overwritten
    assert hpa.was_used("2030-01-01T00:00:00+00:00") is True


# ── only the approving device, or that ask's PIN ──────────────────

def test_an_unlock_on_a_different_device_does_not_finish_it(api, env):
    cr, posted, unlocked = env
    _propose()
    _button(api, {"approve": True})
    unlocked["sid"] = "sid-other"
    _unlocked_by("sid-other", "device:phone-of-someone-else")
    hpa.on_unlock("sid-other")
    assert _writes() == []
    assert [e["entity_id"] for e in hpa.list_waiting()] == [EID]
    notes = [t for t, _c in posted if "different device" in t]
    assert len(notes) == 1 and posted[-1][1] == "c-owner"
    # Mentioned once, not on every later unlock.
    hpa.on_unlock("sid-other")
    assert len([t for t, _c in posted if "different device" in t]) == 1
    # The approving device's own unlock still finishes it.
    unlocked["sid"] = "sid-mine"
    _unlocked_by("sid-mine", APPROVER)
    hpa.on_unlock("sid-mine")
    assert len(_writes()) == 1 and hpa.list_waiting() == []


def test_an_anonymous_approval_is_finished_by_no_unlock(api, env):
    cr, posted, unlocked = env
    _propose()
    _button(api, {"approve": True}, device=None)     # anonymous surface
    assert hpa._load()[0]["approver"] is None
    unlocked["sid"] = "sid-a"
    for who in (None, "anon:x", APPROVER):
        _unlocked_by("sid-a", who)
        hpa.on_unlock("sid-a")
    assert _writes() == [] and [e["entity_id"] for e in hpa.list_waiting()] == [EID]


def test_no_requester_and_no_named_ask_applies_nothing(api, env):
    cr, posted, unlocked = env
    _propose()
    _button(api, {"approve": True})
    unlocked["sid"] = "sid-n"
    assert hpa.apply_waiting("sid-n") == [] and _writes() == []


def test_the_waiting_file_holds_no_raw_device_id(api, env):
    _propose()
    _button(api, {"approve": True})
    raw = open(hpa._path(), encoding="utf-8").read()
    assert DEVICE not in raw and hpa._load()[0]["approver"]


def test_the_vault_records_who_fired_the_unlock(tmp_path, monkeypatch):
    from puffpai.security import vault as vmod
    monkeypatch.setattr(vmod, "_UNLOCK_LISTENERS", [])
    monkeypatch.setattr(vmod, "_unlock_requester_provider", lambda: APPROVER)
    v = vmod.Vault(vault_dir=tmp_path / "vault")
    v.initialize("8241963570")
    s1 = v.authenticate("8241963570")
    assert vmod.unlock_requester(s1.session_id) == APPROVER


def test_a_chat_pin_for_one_ask_does_not_finish_another_threads_ask(
        api, env, monkeypatch):
    cr, posted, unlocked = env
    vault = _Vault()
    monkeypatch.setattr(api, "_get_vault", lambda: vault)
    monkeypatch.setattr(api, "_vault_display_rl_check", lambda: (True, 0))
    monkeypatch.setattr(api, "_vault_display_rl_note", lambda: None)
    _propose(conv="c-owner")
    _button(api, {"approve": True})                # waits, thread c-owner
    unlocked["sid"] = "sid-card"
    api._ha_pin_slot_add("automation.puffpai_other", "c-guest",
                         {"entity_id": "automation.puffpai_other"})
    api._apply_ha_changes_with_pin("c-guest", "482913")
    assert _writes() == []
    assert [e["entity_id"] for e in hpa.list_waiting()] == [EID]
    # The PIN in the ask's own thread finishes it.
    api._ha_pin_slot_add(EID, "c-owner", hpa._approved_record(hpa._load()[0]))
    api._apply_ha_changes_with_pin("c-owner", "482913")
    assert len(_writes()) == 1 and hpa.list_waiting() == []
