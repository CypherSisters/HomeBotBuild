"""HA management: letters-and-digits PINs, late holds and misheard PINs.

* letters-and-digits PINs in a sentence; the 10-minute late hold in EVERY
  conversation; 2-3 digit fragments joined within 60 s; numbers in an
  ordinary sentence are held but never spend a PIN attempt;
* the chat/voice surface is request-scoped (flask.g), /v1/chat/completions
  counts as voice, and nothing carries between turns on a reused thread.

Known limit (documented, not detected): a mishearing like "for eight to one".

Run: python -m pytest -q -p no:cacheprovider tests/test_ha_pin_privacy.py
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

from test_ha_approval_scope import (EPS, _body, _file, api, hooks,   # noqa: E402,F401
                                       leaks, queue, _status)

from puffpai import ha_provisioning as hp                              # noqa: E402

VOICE = [("VOICE", False)]
SECRETS = ("4821", "4-8-2-1", "four eight two one", "oh four two one",
           "a1b2c3", "1987", "1234 main")


def _send(api, ep, stream, msg, cid):
    if ep == "VOICE":
        return str(api._process_voice_query(msg, [], conversation_id=cid))
    return api.app.test_client().post(
        ep, json=_body(ep, msg, cid, stream),
        headers={"X-API-Key": api._API_KEY}).get_data(as_text=True)


def _leak(api, seen, out):
    blob = (" ".join(seen["llm"] + seen["saved"] + seen["memory"]) +
            json.dumps(api._chat_history) + out).lower()
    return [s for s in SECRETS if s in blob]


def _open(api):
    api._ha_pin_reset()
    api._chat_history.clear()
    api._ha_pin_slot_add("automation.puffpai_x", "c1",
                         {"entity_id": "automation.puffpai_x"})


@pytest.mark.parametrize("ep,stream", EPS + VOICE)
@pytest.mark.parametrize("msg,pin", [
    ("my pin is 4821", "4821"), ("pin: 4-8-2-1", "4821"),
    ("four eight two one", "4821"), ("oh four two one", "0421"),
    ("4821 please", "4821"), ("my pin is a1b2c3", "a1b2c3")])
def test_pin_shaped_messages_are_tried_in_the_asked_conversation(
        api, leaks, capsys, ep, stream, msg, pin):
    _open(api)
    _send(api, ep, stream, msg, "c1")
    assert leaks["applied"] == [("c1", pin)]
    assert _leak(api, leaks, capsys.readouterr().out) == []


@pytest.mark.parametrize("ep,stream", EPS + VOICE)
@pytest.mark.parametrize("msg", [
    "I was born in 1987, what happened that year?",
    "I live at 1234 main street, what's the weather"])
def test_numbers_in_a_sentence_are_held_without_an_attempt(
        api, leaks, capsys, ep, stream, msg):
    _open(api)
    out = _send(api, ep, stream, msg, "c1")
    assert "has numbers in it while I'm waiting for your PIN" in out
    assert leaks["applied"] == []
    assert api._ha_pin_slot_ids("c1") == ["automation.puffpai_x"]   # not spent
    assert _leak(api, leaks, capsys.readouterr().out) == []


@pytest.mark.parametrize("ep,stream", EPS + VOICE)
def test_split_pin_fragments_are_joined_and_never_leak(api, leaks, capsys, ep, stream):
    _open(api)
    first = _send(api, ep, stream, "48", "c1")
    assert "Send the rest of the PIN" in first
    _send(api, ep, stream, "21", "c1")
    assert leaks["applied"] == [("c1", "4821")]
    blob = " ".join(leaks["llm"] + leaks["saved"] + leaks["memory"]) + \
        json.dumps(api._chat_history)
    assert "48" not in blob and "21" not in blob


@pytest.mark.parametrize("ep,stream", EPS + VOICE)
@pytest.mark.parametrize("cid", ["c1", "c7"])
def test_the_late_hold_applies_in_every_conversation(api, leaks, capsys, ep,
                                                     stream, cid):
    _open(api)
    for slot in api._ha_pin_slots.values():
        slot["expires"] = time.monotonic() - 5
    out = _send(api, ep, stream, "4821", cid)
    assert "it looks like a PIN" in out
    assert leaks["applied"] == []
    assert _leak(api, leaks, capsys.readouterr().out) == []


@pytest.mark.parametrize("ep,stream", EPS + VOICE)
def test_a_year_in_another_conversation_is_held(api, leaks, capsys, ep, stream):
    _open(api)
    out = _send(api, ep, stream, "I was born in 1987, what happened that year?", "c3")
    assert "it looks like a PIN" in out and leaks["applied"] == []
    assert _leak(api, leaks, capsys.readouterr().out) == []


# ── surfaces (p12 rows E, G, H, I and order independence) ───────────

def _yes(api, ep, msg, cid, stream=False):
    if ep == "/chat/stream":
        body = {"message": msg, "conversation_id": cid}
    else:
        body = {"model": "puffpai", "stream": stream, "conversation_id": cid,
                "messages": [{"role": "user", "content": msg}]}
        if cid is None:
            body.pop("conversation_id")
    api.app.test_client().post(ep, json=body,
                               headers={"X-API-Key": api._API_KEY}).get_data()


def test_v1_counts_as_voice_and_chat_stream_as_chat(api, queue, hooks, leaks):
    _file(queue, "automation.puffpai_v1s", "V1", "ha_provision", "c5")
    _yes(api, "/v1/chat/completions", "yes", "c5", stream=True)
    _file(queue, "automation.puffpai_chat", "Chat", "ha_provision", "c8")
    _yes(api, "/chat/stream", "yes", "c8")
    _file(queue, "automation.puffpai_noconv", "NoConv", "ha_provision", None)
    _yes(api, "/v1/chat/completions", "yes", None)
    st = _status(queue)
    assert st["automation.puffpai_v1s"] == ("approved", "user_via_voice")
    assert st["automation.puffpai_chat"] == ("approved", "user_via_chat")
    assert st["automation.puffpai_noconv"] == ("approved", "user_via_voice")


def test_a_chat_request_never_marks_the_next_direct_voice_turn(api, queue, hooks, leaks):
    _file(queue, "automation.puffpai_c", "C", "ha_provision", "c8")
    _yes(api, "/chat/stream", "yes", "c8")
    _file(queue, "automation.puffpai_v", "V", "ha_provision", "cv")
    api._try_consent_approval("yes", "cv")          # same thread, no request
    assert _status(queue)["automation.puffpai_v"] == ("approved", "user_via_voice")


def test_no_denies_only_this_conversations_ask(api, queue, hooks, leaks):
    _file(queue, "automation.puffpai_d1", "D1", "ha_provision", "c1")
    _file(queue, "automation.puffpai_d2", "D2", "ha_provision", "c2")
    _yes(api, "/chat/stream", "no", "c1")
    st = _status(queue)
    assert st["automation.puffpai_d1"][0] == "denied"
    assert st["automation.puffpai_d2"][0] == "open"


# ── misheard spoken PINs ────────────────────────────────────────────
HOMOPHONE_SECRETS = ("for eight to one", "won to free ate", "4821", "1238")


@pytest.mark.parametrize("ep,stream", EPS + VOICE)
@pytest.mark.parametrize("msg,pin", [
    ("for eight to one", "4821"), ("For eight, to one.", "4821"),
    ("my pin is won to free ate", "1238"), ("oh for too won please", "0421"),
    ("fore ate two one", "4821")])
def test_misheard_digit_words_are_read_as_the_pin(
        api, leaks, capsys, ep, stream, msg, pin):
    _open(api)
    _send(api, ep, stream, msg, "c1")
    assert leaks["applied"] == [("c1", pin)]
    blob = (" ".join(leaks["llm"] + leaks["saved"] + leaks["memory"]) +
            json.dumps(api._chat_history) + capsys.readouterr().out).lower()
    assert not [s for s in HOMOPHONE_SECRETS if s in blob]


@pytest.mark.parametrize("ep,stream", EPS + VOICE)
def test_a_misheard_pin_in_the_late_hold_is_held_not_used(
        api, leaks, capsys, ep, stream):
    _open(api)
    for slot in api._ha_pin_slots.values():
        slot["expires"] = time.monotonic() - 5
    out = _send(api, ep, stream, "for eight to one", "c7")
    assert "it looks like a PIN" in out and leaks["applied"] == []
    assert "for eight to one" not in " ".join(leaks["llm"] + leaks["saved"])


@pytest.mark.parametrize("msg", [
    "I want to go for a walk", "two for one", "set it to 4 for now",
    "what is there to do for fun", "for eight", "go to bed"])
def test_ordinary_sentences_are_not_read_as_homophone_pins(api, msg):
    assert api._pin_shape(msg)[0] is None


def test_homophones_count_only_while_a_pin_matters(api, leaks):
    api._ha_pin_reset()
    assert api._ha_pin_intercept("for eight to one", "c1") is None


def test_the_voice_unit_is_told_to_withhold_its_log_during_an_ha_pin(
        api, leaks, monkeypatch):
    monkeypatch.setattr(api, "_process_voice_query",
                        lambda *a, **k: "ok")
    c = api.app.test_client()
    body = {"model": "puffpai", "stream": False,
            "messages": [{"role": "user", "content": "what time is it"}]}
    api._ha_pin_reset()
    r = c.post("/api/chat", json=body, headers={"X-API-Key": api._API_KEY})
    assert r.get_json().get("vault_challenge_pending") is False
    _open(api)
    r = c.post("/api/chat", json=body, headers={"X-API-Key": api._API_KEY})
    assert r.get_json().get("vault_challenge_pending") is True
    api._ha_pin_reset()
