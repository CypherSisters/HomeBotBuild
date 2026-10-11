"""HA management: PINs inside sentences, and one conversation for id-less callers.

1. A PIN said INSIDE a sentence ("my pin is 4821", "pin 4821") or as words
   ("four eight two one") while any HA PIN slot is open never reaches a model,
   history, memory or a log, on all six entry points and on the in-process
   voice path. In the asked conversation it is used; elsewhere, or late after
   the slot closed, it is held back with one plain sentence.
2. One "yes" answers only the request Puff asked about most recently IN THAT
   CONVERSATION. Several offered together need "yes to both" or a name. The
   real surface (chat or voice) is recorded.

Run: python -m pytest -q -p no:cacheprovider tests/test_ha_approval_scope.py
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_ha_automation_builder import LIVE, LIVE_NO_DUPS, TURN_1   # noqa: E402

from puffpai import ha_automation_builder as hab        # noqa: E402
from puffpai import ha_provisioning as hp               # noqa: E402


class _Bridge:
    def get_all_states(self, force=False):
        return LIVE


@pytest.fixture
def api(monkeypatch):
    from puffpai import api_server as a

    class _Tools:
        _ha_bridge = _Bridge()
        _llm_chat = None

        def run(self, *args, **kw):
            raise AssertionError("the tool agent must not run")

    monkeypatch.setattr(a, "_get_assistant_tools", lambda: _Tools())
    hab.clear_draft()
    a._ha_pin_reset()
    yield a
    hab.clear_draft()
    a._ha_pin_reset()


@pytest.fixture
def leaks(api, monkeypatch):
    seen = {"llm": [], "saved": [], "memory": [], "applied": []}

    def fake_stream(msg, *a, **k):
        seen["llm"].append(str(msg))
        yield "ok"

    monkeypatch.setattr(api, "_stream_ollama_tokens", fake_stream)
    monkeypatch.setattr(api, "_query_ollama_direct",
                        lambda *x, **k: seen["llm"].append(json.dumps(x, default=str)) or "ok")
    monkeypatch.setattr(api, "_save_conversation",
                        lambda conv: seen["saved"].append(json.dumps(conv.get("messages", []))))
    monkeypatch.setattr(api, "_remember_for_user",
                        lambda uid, text: seen["memory"].append(text))
    monkeypatch.setattr(api, "_apply_ha_changes_with_pin",
                        lambda cid, pin: seen["applied"].append((cid, pin)) or "applied")
    return seen


def _body(ep, msg, cid, stream=False):
    if ep in ("/chat/stream", "/chat/send"):
        return {"message": msg, "conversation_id": cid}
    if ep == "/conversation":
        d = {"text": msg}
        if cid:
            d["conversation_id"] = cid
        return d
    d = {"model": "puffpai", "stream": stream,
         "messages": [{"role": "user", "content": msg}]}
    if cid:
        d["conversation_id"] = cid
    return d


EPS = [("/chat/stream", False), ("/chat/send", False), ("/conversation", False),
       ("/v1/chat/completions", True), ("/v1/chat/completions", False),
       ("/api/chat", False)]
OWN = ["4821", "4 8 2 1", "4821.", "my pin is 4821", "pin 4821",
       "it's 4821 thanks", "four eight two one", "four-eight-two-one"]
SECRETS = ("4821", "4 8 2 1", "eight two", "eight-two")


def _leaked(seen, chat_history, out):
    blob = " ".join(seen["llm"] + seen["saved"] + seen["memory"]) + \
        json.dumps(chat_history) + out
    return [s for s in SECRETS if s in blob]


@pytest.mark.parametrize("ep,stream", EPS)
@pytest.mark.parametrize("msg", OWN)
def test_pin_in_a_sentence_or_as_words_is_used_in_its_own_conversation(
        api, leaks, capsys, ep, stream, msg):
    api._chat_history.clear()
    api._ha_pin_slot_add("automation.puffpai_x", "c1", {"entity_id": "automation.puffpai_x"})
    client = api.app.test_client()
    r = client.post(ep, json=_body(ep, msg, "c1", stream),
                    headers={"X-API-Key": api._API_KEY})
    assert r.status_code == 200
    assert leaks["applied"] == [("c1", "4821")]
    assert _leaked(leaks, api._chat_history, capsys.readouterr().out) == []


@pytest.mark.parametrize("ep,stream", EPS)
@pytest.mark.parametrize("msg,cid", [("4821", "c2"), ("my pin is 4821", "c2"),
                                     ("four eight two one", None)])
def test_pin_outside_the_asked_conversation_is_held(api, leaks, capsys, ep,
                                                     stream, msg, cid):
    api._chat_history.clear()
    api._ha_pin_slot_add("automation.puffpai_x", "c1", {"entity_id": "automation.puffpai_x"})
    client = api.app.test_client()
    text = client.post(ep, json=_body(ep, msg, cid, stream),
                       headers={"X-API-Key": api._API_KEY}).get_data(as_text=True)
    assert "it looks like a PIN" in text
    assert leaks["applied"] == []
    assert _leaked(leaks, api._chat_history, capsys.readouterr().out) == []


def test_a_late_pin_after_the_slot_expired_is_held(api, leaks, capsys):
    api._chat_history.clear()
    api._ha_pin_slot_add("automation.puffpai_x", "c1", {"entity_id": "automation.puffpai_x"})
    for slot in api._ha_pin_slots.values():
        slot["expires"] = time.monotonic() - 1
    client = api.app.test_client()
    text = client.post("/chat/stream", json={"message": "my pin is 4821",
                                             "conversation_id": "c1"},
                       headers={"X-API-Key": api._API_KEY}).get_data(as_text=True)
    assert api.HA_PIN_HELD in text
    assert leaks["applied"] == []
    assert _leaked(leaks, api._chat_history, capsys.readouterr().out) == []


def test_in_process_voice_path_catches_a_spoken_pin(api, leaks, capsys):
    api._ha_pin_slot_add("automation.puffpai_x", "c1", {"entity_id": "automation.puffpai_x"})
    reply = api._process_voice_query("four eight two one", [], conversation_id="c1")
    assert reply == "applied" and leaks["applied"] == [("c1", "4821")]
    api._ha_pin_slot_add("automation.puffpai_x", "c1", {"entity_id": "automation.puffpai_x"})
    held = api._process_voice_query("my pin is 4821", [], conversation_id="elsewhere")
    assert held == api.HA_PIN_HELD
    assert leaks["memory"] == [] and leaks["llm"] == []
    assert _leaked(leaks, [], capsys.readouterr().out) == []


def test_conversation_route_keeps_a_stable_id(api, leaks, monkeypatch):
    ids = []
    monkeypatch.setattr(api, "_ha_pin_intercept",
                        lambda text, cid=None, consume=True: ids.append(cid) or "x")
    client = api.app.test_client()
    for _ in range(2):
        client.post("/conversation", json={"text": "hello"},
                    headers={"X-API-Key": api._API_KEY})
    # One conversation: an id-less caller is
    # in the household thread, not a per-client hash.
    assert ids[0] == ids[1] == "home-owner"


# ── 2. one "yes", one ask, one conversation ─────────────────────────

@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import puffpai.agent_activity as aa
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    hp.reset_state()
    yield cr
    hp.reset_state()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()


def _file(cr, eid, label, action, conv, batch=None, age=0.0):
    cr.create_request(entity_id=eid, label=label, action=action, reason="r",
                      requested_by="steward",
                      context={"conversation_id": conv, "batch": batch})
    items = cr._load()
    when = datetime.now(timezone.utc) - timedelta(seconds=age)
    for req in items:
        if req.get("entity_id") == eid:
            req["created_at"] = when.isoformat()
    cr._save(items)


@pytest.fixture
def hooks(monkeypatch):
    from puffpai import zigbee_pairing as zp
    done = []
    monkeypatch.setattr(hp, "apply_approved_change",
                        lambda **k: done.append(k["request"]["entity_id"])
                        or {"message": "made " + k["request"]["entity_id"]})
    monkeypatch.setattr(zp, "complete_approved_pairing",
                        lambda *a, **k: done.append("zha.pairing_window")
                        or {"message": "window open"})
    return done


def _status(cr):
    return {r["entity_id"]: (r["status"], r.get("resolved_by")) for r in cr._load()}


def test_yes_answers_only_the_newest_ask_in_this_conversation(api, queue, hooks):
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision", "cA", age=120)
    _file(queue, "automation.puffpai_b", "Automation B", "ha_provision", "cB", age=60)
    _file(queue, "zha.pairing_window", "Zigbee pairing", "zigbee_pair", "cA", age=10)
    api._try_consent_approval("yes", "cA")
    st = _status(queue)
    assert hooks == ["zha.pairing_window"]
    assert st["automation.puffpai_a"][0] == "open"
    assert st["automation.puffpai_b"][0] == "open"
    api._try_consent_approval("yes", "cB")
    assert hooks == ["zha.pairing_window", "automation.puffpai_b"]
    assert _status(queue)["automation.puffpai_a"][0] == "open"


def test_a_yes_in_a_conversation_with_nothing_asked_approves_nothing(api, queue, hooks):
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision", "cA")
    assert api._try_consent_approval("yes", "cZ") is None
    assert hooks == [] and _status(queue)["automation.puffpai_a"][0] == "open"


def test_a_stale_ask_is_not_answered_by_a_bare_yes(api, queue, hooks):
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision", "cA", age=900)
    assert api._try_consent_approval("yes", "cA") is None
    assert hooks == []


def test_a_batch_needs_yes_to_both_or_a_name(api, queue, hooks):
    _file(queue, "automation.puffpai_sofa_off", "Home Assistant automation: sofa off",
          "ha_provision", "cA", batch="b1", age=5)
    _file(queue, "automation.puffpai_porch_on", "Home Assistant automation: porch on",
          "ha_provision", "cA", batch="b1", age=5)
    reply = api._try_consent_approval("yes", "cA")
    assert reply.startswith("Which one should I approve:") and "yes to both" in reply
    assert hooks == []
    api._try_consent_approval("yes the porch one", "cA")
    assert hooks == ["automation.puffpai_porch_on"]
    _file(queue, "automation.puffpai_x1", "X1", "ha_provision", "cA", batch="b2")
    _file(queue, "automation.puffpai_x2", "X2", "ha_provision", "cA", batch="b2")
    api._try_consent_approval("yes to both", "cA")
    assert set(hooks[1:]) == {"automation.puffpai_x1", "automation.puffpai_x2"}
    assert _status(queue)["automation.puffpai_sofa_off"][0] == "open"


def test_the_real_surface_is_recorded(api, queue, hooks):
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision", "cA")
    with api.app.test_request_context():
        api._set_turn_surface("chat")
        api._try_consent_approval("yes", "cA")
    assert _status(queue)["automation.puffpai_a"] == ("approved", "user_via_chat")
    _file(queue, "automation.puffpai_b", "Automation B", "ha_provision", "cV")
    api._try_consent_approval("yes", "cV")          # direct voice path
    assert _status(queue)["automation.puffpai_b"] == ("approved", "user_via_voice")


def test_typed_yes_on_the_stream_is_chat_and_scoped(api, queue, hooks, leaks):
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision", "s1", age=30)
    _file(queue, "automation.puffpai_b", "Automation B", "ha_provision", "s2", age=20)
    _file(queue, "zha.pairing_window", "Zigbee pairing", "zigbee_pair", "s2", age=10)
    api.app.test_client().post("/chat/stream", json={"message": "yes",
                                                     "conversation_id": "s1"},
                               headers={"X-API-Key": api._API_KEY}).get_data()
    st = _status(queue)
    assert hooks == ["automation.puffpai_a"]
    assert st["automation.puffpai_a"] == ("approved", "user_via_chat")
    assert st["automation.puffpai_b"][0] == "open"
    assert st["zha.pairing_window"][0] == "open"


def test_two_rules_ask_for_yes_to_both(monkeypatch):
    calls = []
    monkeypatch.setattr(hp, "propose_ha_change",
                        lambda *a, **k: calls.append(k.get("batch")) or {"proposed": True})
    hab.clear_draft()
    reply = hab.handle_turn(TURN_1, LIVE_NO_DUPS, conversation_id="cA")
    assert reply.endswith(hab.APPROVE_BOTH_TAIL)
    assert len(calls) == 2 and calls[0] and calls[0] == calls[1]
