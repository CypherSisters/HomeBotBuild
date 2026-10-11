"""One brain, one conversation.

The rule: voice, dashboard chat, Home Assistant's voice assistant and the
kiosk live transcript are the same brain and the same conversation. One shared
household thread (``home-owner``) until voices can be told apart; "New chat"
stays as a side topic.

Pinned here:
* one id: every surface that names no conversation lands in ``home-owner``;
* a "yes" or a PIN said on one surface answers the ask made on another, and a
  side-topic id does not;
* one save point tags each message's surface, caps the file, and writes NO
  trace of a vault or PIN turn;
* one memory: a typed turn is in the next spoken turn's prompt history;
* HA Assist (/v1) turns are saved and feed the live tile; the dashboard's own
  /v1 stream (``client_saves``) is not saved twice;
* test threads are hidden from /chat/history but kept.

Run: python -m pytest -q -p no:cacheprovider tests/test_one_conversation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

# Fixtures reused from the HA-management suites (pytest picks them up by name).
from test_ha_approval_scope import (  # noqa: E402,F401
    api, leaks, queue, hooks, _file, _status)

HOME = "home-owner"
SIDE = "0123456789abcdef0123456789abcdef"


def _h(api):
    return {"X-API-Key": api._API_KEY}


def _voice_body(text, history=()):
    """Exactly what the on-box voice unit posts (voice/service.brain_payload)."""
    from puffpai.voice import service
    msgs = list(history) + [{"role": "user", "content": text}]
    return service.brain_payload(msgs, truncated=False)


def _ha_body(text):
    """Home Assistant's OpenAI-compatible conversation agent."""
    return {"model": "puffpai",
            "messages": [{"role": "user", "content": text}]}


@pytest.fixture
def thread(api, monkeypatch):
    """In-memory history only: nothing reaches the real chat_history dir."""
    saved = []
    before = list(api._chat_history)
    api._chat_history.clear()
    monkeypatch.setattr(api, "_save_conversation",
                        lambda conv: saved.append(json.loads(json.dumps(conv))))
    monkeypatch.setattr(api._voice_full_tl, "text", None, raising=False)
    api._private_turns.clear()
    try:
        yield saved
    finally:
        api._chat_history[:] = before
        api._private_turns.clear()


@pytest.fixture
def brain(api, monkeypatch):
    """Deterministic replies on every route; records the history each got."""
    seen = {"voice": [], "chat": []}

    def voice_q(message, conversation_history=None, **kw):
        seen["voice"].append(list(conversation_history or []))
        api._voice_full_tl.text = None
        return "spoken: " + message

    def chat_q(message, conversation_history=None, **kw):
        seen["chat"].append(list(conversation_history or []))
        return {"response": "typed: " + message, "sources": [],
                "suggestions": []}

    def stream(message, history, persona, **kw):
        seen["chat"].append(list(history or []))
        yield "streamed: " + message

    monkeypatch.setattr(api, "_process_voice_query", voice_q)
    monkeypatch.setattr(api, "_process_chat_query", chat_q)
    monkeypatch.setattr(api, "_stream_ollama_tokens", stream)
    monkeypatch.setattr(api, "_try_ha_management_rules", lambda *a, **k: None)
    return seen


def _home(api):
    for conv in api._chat_history:
        if conv.get("id") == HOME:
            return conv
    return None


# ── 1. one id ───────────────────────────────────────────────────────────────

def test_one_id_on_every_surface(api, monkeypatch):
    ids = []
    monkeypatch.setattr(api, "_ha_pin_intercept",
                        lambda text, cid=None, consume=True: ids.append(cid) or "x")
    c = api.app.test_client()
    c.post("/api/chat", json=_voice_body("hello"), headers=_h(api))
    c.post("/v1/chat/completions", json=_ha_body("hello"), headers=_h(api))
    c.post("/conversation", json={"text": "hello"}, headers=_h(api))
    c.post("/chat/stream", json={"message": "hello"}, headers=_h(api))
    c.post("/chat/send", json={"message": "hello"}, headers=_h(api))
    assert ids == [HOME] * 5
    # HA's own session id (`user`) is not a conversation of its own any more.
    ids.clear()
    c.post("/v1/chat/completions",
           json=dict(_ha_body("hello"), user="01HA-SESSION"), headers=_h(api))
    assert ids == [HOME]
    # An explicit id (a "New chat" side topic) is still honoured.
    ids.clear()
    c.post("/chat/stream", json={"message": "hi", "conversation_id": SIDE},
           headers=_h(api))
    c.post("/api/chat", json=dict(_voice_body("hi"), conversation_id=SIDE),
           headers=_h(api))
    assert ids == [SIDE, SIDE]
    assert api._ha_pin_key(None) == HOME


# ── 2. pending asks follow the household across surfaces ────────────────────

def test_pin_asked_in_chat_is_answered_by_voice(api, leaks):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    c = api.app.test_client()
    r = c.post("/api/chat", json=_voice_body("4821"), headers=_h(api))
    assert r.status_code == 200 and r.get_json()["private"] is True
    assert leaks["applied"] == [(HOME, "4821")]
    assert leaks["saved"] == [] and leaks["llm"] == []


def test_pin_asked_by_voice_is_answered_in_chat(api, leaks):
    # The voice path files its ask with no id of its own: the household.
    api._ha_pin_slot_add("automation.puffpai_x", None,
                         {"entity_id": "automation.puffpai_x"})
    c = api.app.test_client()
    r = c.post("/chat/stream", json={"message": "my pin is 4821",
                                     "conversation_id": HOME}, headers=_h(api))
    assert r.status_code == 200
    assert leaks["applied"] == [(HOME, "4821")]
    assert leaks["saved"] == [] and leaks["llm"] == []


def test_a_side_topic_does_not_answer_the_household_pin(api, leaks):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    c = api.app.test_client()
    r = c.post("/chat/send", json={"message": "4821", "conversation_id": SIDE},
               headers=_h(api))
    assert leaks["applied"] == []
    assert "PIN" in r.get_json()["response"]     # held back, not used
    assert leaks["saved"] == [] and leaks["llm"] == []


def test_yes_by_voice_answers_an_ask_made_in_chat(api, queue, hooks, thread):
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision", HOME)
    c = api.app.test_client()
    r = c.post("/api/chat", json=_voice_body("yes"), headers=_h(api))
    assert r.status_code == 200
    assert hooks == ["automation.puffpai_a"]
    assert _status(queue)["automation.puffpai_a"][1] == "user_via_voice"


def test_yes_typed_answers_an_ask_made_by_voice(api, queue, hooks, thread,
                                                monkeypatch):
    monkeypatch.setattr(api, "_query_ollama_direct", lambda *a, **k: "ok")
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision",
          api._ha_pin_key(None))
    c = api.app.test_client()
    r = c.post("/chat/send", json={"message": "yes"}, headers=_h(api))
    assert r.status_code == 200
    assert hooks == ["automation.puffpai_a"]
    assert _status(queue)["automation.puffpai_a"][1] == "user_via_chat"


def test_a_side_topic_yes_does_not_answer_the_household_ask(api, queue, hooks):
    _file(queue, "automation.puffpai_a", "Automation A", "ha_provision", HOME)
    assert api._try_consent_approval("yes", SIDE) is None
    assert hooks == []


# ── 3. one save point ───────────────────────────────────────────────────────

def test_the_save_point_tags_each_surface(api, thread, brain, monkeypatch):
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "rag")
    c = api.app.test_client()
    c.post("/api/chat", json=_voice_body("one"), headers=_h(api))
    c.post("/v1/chat/completions", json=_ha_body("two"), headers=_h(api))
    c.post("/chat/send", json={"message": "three"}, headers=_h(api))
    c.post("/chat/stream", json={"message": "four", "surface": "talk"},
           headers=_h(api)).get_data()
    c.post("/chat/save", json={"user_message": "five",
                               "assistant_message": "ok five"},
           headers=_h(api))
    msgs = _home(api)["messages"]
    assert [(m["role"], m["content"], m["surface"]) for m in msgs][::2] == [
        ("user", "one", "voice"), ("user", "two", "ha"),
        ("user", "three", "chat"), ("user", "four", "talk"),
        ("user", "five", "chat")]
    assert [m["surface"] for m in msgs[1::2]] == [
        "voice", "ha", "chat", "talk", "chat"]
    # A surface a client may not claim falls back to chat.
    api._append_thread_turn(HOME, "owner", "kiosk-root", "six", "ok")
    assert _home(api)["messages"][-1]["surface"] == "chat"
    # The household thread is the most recent conversation.
    api._append_thread_turn(SIDE, "owner", "chat", "side", "ok")
    api._append_thread_turn(HOME, "owner", "voice", "back", "ok")
    assert api._chat_history[-1]["id"] == HOME


def test_the_thread_file_is_capped(api, thread, monkeypatch):
    monkeypatch.setattr(api, "_THREAD_MAX_MESSAGES", 10)
    for i in range(8):
        api._append_thread_turn(HOME, "owner", "chat", f"q{i}", f"a{i}")
    msgs = _home(api)["messages"]
    assert len(msgs) == 10 and msgs[0]["content"] == "q3"
    assert thread[-1]["messages"] == msgs


def test_the_household_thread_is_never_evicted(api, thread, monkeypatch):
    monkeypatch.setattr(api, "_MAX_CONVERSATIONS", 3)
    api._append_thread_turn(HOME, "owner", "voice", "hi", "hello")
    for i in range(5):
        with api._chat_history_lock:
            api._chat_history.insert(len(api._chat_history),
                                     {"id": f"{i:032x}", "messages": []})
            api._trim_chat_history()
    assert [c["id"] for c in api._chat_history].count(HOME) == 1
    assert len(api._chat_history) == 3


def test_a_private_turn_leaves_no_trace_at_the_save_point(api, thread):
    api._register_private_turn("what is my wifi password")
    assert api._append_thread_turn(HOME, "owner", "voice",
                                   "what is my wifi password", "hunter2") is False
    assert api._append_thread_turn(HOME, "owner", "chat", "q", "a",
                                   private=True) is False
    # A PIN-shaped line while an HA PIN is being waited for.
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    try:
        assert api._append_thread_turn(HOME, "owner", "ha", "4821", "ok") is False
    finally:
        api._ha_pin_reset()
    assert _home(api) is None and thread == []


@pytest.mark.parametrize("route", ["/api/chat", "/v1/chat/completions",
                                   "/chat/send", "/chat/stream"])
def test_a_vault_turn_leaves_no_trace_on_any_route(api, thread, brain,
                                                   monkeypatch, route):
    secret = "the wifi password is hunter2"

    def marks(message, conversation_history=None, **kw):
        api._mark_vault_turn_private()
        return secret

    monkeypatch.setattr(api, "_process_voice_query", marks)
    monkeypatch.setattr(api, "_process_chat_query",
                        lambda m, h=None, **k: (api._mark_vault_turn_private()
                                                or {"response": secret,
                                                    "sources": [],
                                                    "suggestions": []}))
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "agent")
    c = api.app.test_client()
    body = {"/api/chat": _voice_body("read me the wifi"),
            "/v1/chat/completions": _ha_body("read me the wifi"),
            "/chat/send": {"message": "read me the wifi"},
            "/chat/stream": {"message": "read me the wifi"}}[route]
    r = c.post(route, json=body, headers=_h(api))
    assert r.status_code == 200
    r.get_data()
    assert _home(api) is None and thread == []
    live = api._live_turn_snapshot()
    assert secret not in json.dumps(live) and "wifi" not in live["heard"]
    # …and the client's save-back is refused too.
    c.post("/chat/save", json={"user_message": "read me the wifi",
                               "assistant_message": secret}, headers=_h(api))
    assert _home(api) is None and thread == []


@pytest.mark.parametrize("route", ["/api/chat", "/v1/chat/completions",
                                   "/chat/send", "/chat/stream",
                                   "/conversation"])
def test_an_ha_pin_turn_leaves_no_trace_on_any_route(api, leaks, thread,
                                                     route):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    c = api.app.test_client()
    body = {"/api/chat": _voice_body("4821"),
            "/v1/chat/completions": _ha_body("4821"),
            "/chat/send": {"message": "4821"},
            "/chat/stream": {"message": "4821"},
            "/conversation": {"text": "4821"}}[route]
    c.post(route, json=body, headers=_h(api)).get_data()
    assert leaks["applied"] == [(HOME, "4821")]
    assert leaks["saved"] == [] and _home(api) is None
    assert "4821" not in json.dumps(api._live_turn_snapshot())


# ── 4. one memory ───────────────────────────────────────────────────────────

def test_a_typed_turn_informs_the_next_spoken_one(api, thread, brain):
    c = api.app.test_client()
    c.post("/chat/send", json={"message": "my sister is called Ada"},
           headers=_h(api))
    # The voice unit sends only its own (empty) buffer.
    c.post("/api/chat", json=_voice_body("what is she called"),
           headers=_h(api))
    assert brain["voice"][-1] == [{"user": "my sister is called Ada",
                                   "assistant": "typed: my sister is called Ada"}]
    # …and a spoken turn informs the next typed one, and HA's.
    c.post("/chat/send", json={"message": "and her age"}, headers=_h(api))
    assert brain["chat"][-1][-1]["user"] == "what is she called"
    c.post("/v1/chat/completions", json=_ha_body("say it again"),
           headers=_h(api))
    assert brain["chat"][-1][-1]["user"] == "and her age"


def test_the_voice_buffer_adds_only_what_the_thread_lacks(api, thread, brain):
    api._append_thread_turn(HOME, "owner", "voice", "hi", "hello there")
    c = api.app.test_client()
    # The voice unit's buffer repeats the stored pair (spoken form) and holds
    # one the thread never had.
    hist = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "older reply"}]
    c.post("/api/chat", json=_voice_body("next", hist), headers=_h(api))
    assert [p["user"] for p in brain["voice"][-1]] == ["hi", "older"]


def test_a_side_topic_does_not_feed_the_household_memory(api, thread, brain):
    c = api.app.test_client()
    c.post("/chat/send", json={"message": "side secret plan",
                               "conversation_id": SIDE}, headers=_h(api))
    c.post("/api/chat", json=_voice_body("anything new"), headers=_h(api))
    assert brain["voice"][-1] == []


# ── 5. HA Assist (/v1) ──────────────────────────────────────────────────────

def test_v1_ha_turn_is_saved_and_feeds_the_live_tile(api, thread, brain):
    c = api.app.test_client()
    r = c.post("/v1/chat/completions", json=_ha_body("turn on the porch"),
               headers=_h(api))
    assert r.status_code == 200
    msgs = _home(api)["messages"]
    assert [(m["content"], m["surface"]) for m in msgs] == [
        ("turn on the porch", "ha"), ("typed: turn on the porch", "ha")]
    live = api._live_turn_snapshot()
    assert live["heard"] == "turn on the porch"
    assert live["reply"] == "typed: turn on the porch"


def test_v1_dashboard_stream_is_not_saved_twice(api, thread, brain,
                                                monkeypatch):
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "rag")
    c = api.app.test_client()
    c.post("/v1/chat/completions",
           json={"model": "puffpai", "stream": True, "client_saves": True,
                 "conversation_id": HOME,
                 "messages": [{"role": "user", "content": "hello"}]},
           headers=_h(api)).get_data()
    assert _home(api) is None and thread == []


# ── 6. Recent hides test threads ────────────────────────────────────────────

def test_test_threads_are_hidden_from_history_but_kept(api, thread):
    ids = [HOME, "voice-20261008", SIDE,
           "8f14e45f-ceea-467a-9575-0e1d8e0f7a3b",
           "media-smoke", "pin-smoke"]
    for cid in ids:
        api._chat_history.append({"id": cid, "messages": [],
                                  "created_at": "2026-10-09T00:00:00+00:00"})
    c = api.app.test_client()
    body = c.get("/chat/history", headers=_h(api)).get_json()
    listed = {conv["id"] for conv in body["conversations"]}
    assert listed == set(ids[:4])
    assert body["household_id"] == HOME
    everything = c.get("/chat/history?all=1", headers=_h(api)).get_json()
    assert {conv["id"] for conv in everything["conversations"]} == set(ids)


def test_the_chat_page_poll_reads_one_thread_since_a_timestamp(api, thread):
    api._append_thread_turn(HOME, "owner", "chat", "typed", "ok")
    first = _home(api)["messages"][-1]["timestamp"]
    c = api.app.test_client()
    whole = c.get(f"/chat/history?c={HOME}", headers=_h(api)).get_json()
    assert [m["content"] for m in whole["conversation"]["messages"]] == [
        "typed", "ok"]
    from urllib.parse import quote
    assert c.get(f"/chat/history?c={HOME}&since={first}",
                 headers=_h(api)).get_json()["conversation"]["messages"] == []
    nothing = c.get(f"/chat/history?c={HOME}&since={quote(first)}",
                    headers=_h(api)).get_json()
    assert nothing["conversation"]["messages"] == []
    missing = c.get("/chat/history?c=nope", headers=_h(api)).get_json()
    assert missing["conversation"] is None and missing["household_id"] == HOME


# ── /chat/save gets the turn-time PIN hold ─────────────────────────────────

@pytest.mark.parametrize("conv", [None, HOME, SIDE])
def test_chat_save_refuses_a_pin_sentence_while_a_pin_is_awaited(
        api, thread, brain, conv):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    c = api.app.test_client()
    try:
        body = {"user_message": "the code is 9876 thanks",
                "assistant_message": "noted"}
        if conv:
            body["conversation_id"] = conv
        r = c.post("/chat/save", json=body, headers=_h(api))
        assert r.status_code == 200 and r.get_json()["success"] is True
        assert thread == [] and _home(api) is None
    finally:
        api._ha_pin_reset()
    # …and so it never feeds the next voice prompt.
    c.post("/api/chat", json=_voice_body("what did I say"), headers=_h(api))
    assert "9876" not in json.dumps(brain["voice"][-1])


def test_chat_save_refuses_a_pin_inside_the_hold_after_the_slot_closed(
        api, thread):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    api._ha_pin_slot_clear(HOME)        # closed: the 10-minute hold runs
    c = api.app.test_client()
    try:
        for text in ("the code is 9876 thanks", "9876", "nine eight seven six"):
            c.post("/chat/save", json={"user_message": text,
                                       "assistant_message": "ok"},
                   headers=_h(api))
        assert thread == [] and _home(api) is None
        # An ordinary sentence in the hold is still stored.
        c.post("/chat/save", json={"user_message": "turn the porch light on",
                                   "assistant_message": "done"},
               headers=_h(api))
        assert len(thread) == 1
        assert "9876" not in json.dumps(thread)
    finally:
        api._ha_pin_reset()


def test_chat_save_stores_numbers_when_no_pin_is_awaited(api, thread):
    api._ha_pin_reset()
    c = api.app.test_client()
    c.post("/chat/save", json={"user_message": "the code is 9876 thanks",
                               "assistant_message": "ok"}, headers=_h(api))
    assert len(thread) == 1
