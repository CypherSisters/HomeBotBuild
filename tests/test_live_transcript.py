"""The live transcript: reasoning as a side channel, and the turn in flight.

Two things are pinned here.

First, that asking to SEE the reasoning changes nothing for anyone who did not
ask: the default payload carries no ``think: True``, and reasoning — when it is
asked for — travels beside the answer, never inside it. Thinking that reached
``full_response`` would reach the stored transcript, the PII pass and the
speaker, which is exactly the leak this shape exists to make impossible.

Second, that ``GET /voice/live`` reports the spoken turn while it is still
happening, carries the FULL reply (the dashboard keeps the essay; only the
speaker gets the two sentences), forgets a finished turn after 90 s, and sits
behind the same guard as ``/voice/state``.
"""

import importlib
import json
import os
import sys

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


@pytest.fixture(scope="module")
def api_app():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod.app, mod._API_KEY, mod


@pytest.fixture()
def client(api_app):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    mod._live_turn_clear()
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}, mod


class _FakeStream:
    """What ``requests`` hands back for a streaming /api/chat call."""

    def __init__(self, chunks, status_code=200):
        self.status_code = status_code
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self):
        for chunk in self._chunks:
            yield json.dumps(chunk).encode("utf-8")


def _content(text):
    return {"message": {"content": text}, "done": False}


def _thought(text):
    return {"message": {"content": "", "thinking": text}, "done": False}


@pytest.fixture()
def plain_llm_path(api_app, monkeypatch):
    """Strip the pre-LLM interceptors so the stream itself is under test."""
    _app, _key, mod = api_app
    monkeypatch.setattr(mod, "_maybe_vault_gated_answer", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_maybe_identity_answer", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_should_skip_rag", lambda *_a, **_k: True)
    return mod


# ── the default call is untouched ────────────────────────────────────────────

def test_the_default_stream_never_asks_the_model_to_think(plain_llm_path,
                                                          monkeypatch):
    mod = plain_llm_path
    seen = {}

    def _fake_chat(messages, **kwargs):
        seen.update(kwargs)
        return _FakeStream([_thought("secretly reasoning"), _content("Hi.")])

    monkeypatch.setattr(mod, "_ollama_chat", _fake_chat)
    out = list(mod._stream_ollama_tokens("hello", [], "persona"))

    assert seen.get("think") is None, "no caller asked to think"
    assert out == ["Hi."], "reasoning is not content, even unasked-for"


def test_the_payload_flips_only_the_family_s_own_lever(api_app, monkeypatch):
    """think=True must never invent a key a model would 400 on."""
    _app, _key, mod = api_app
    from puffpai.inference import model_options
    import requests

    sent = {}
    monkeypatch.setattr(requests, "post",
                        lambda url, json=None, **kw: sent.update(json) or MagicMock())

    mod._ollama_chat([{"role": "user", "content": "hi"}])
    assert sent.get("think") is not True

    default_frag = model_options.thinking_control(mod.OLLAMA_MODEL)
    sent.clear()
    mod._ollama_chat([{"role": "user", "content": "hi"}], think=True)
    if isinstance(default_frag.get("think"), bool):
        assert sent["think"] is True
    else:
        # An effort family keeps its effort; a model with no lever gets no key.
        assert sent.get("think") == default_frag.get("think")


# ── opt in, and reasoning arrives beside the answer ──────────────────────────

def test_thinking_arrives_on_the_side_channel_in_order(plain_llm_path,
                                                       monkeypatch):
    mod = plain_llm_path
    monkeypatch.setattr(mod, "_ollama_chat", lambda messages, **kw: _FakeStream([
        _thought("The user said hello. "),
        _thought("A greeting will do."),
        _content("Hello "),
        _content("there."),
    ]))

    out = list(mod._stream_ollama_tokens("hello", [], "persona", think=True))

    thinking = [p.text for p in out if isinstance(p, mod._ThinkingChunk)]
    content = [p for p in out if not isinstance(p, mod._ThinkingChunk)]
    assert thinking == ["The user said hello. ", "A greeting will do."]
    assert content == ["Hello ", "there."]
    assert "".join(content) == "Hello there."
    # Not a str: a caller that forgot to filter fails loudly, never leaks.
    assert not isinstance(out[0], str)


def test_a_refused_think_flag_still_answers(plain_llm_path, monkeypatch):
    mod = plain_llm_path
    calls = []

    def _fake_chat(messages, **kwargs):
        calls.append(kwargs.get("think"))
        if kwargs.get("think"):
            return _FakeStream([], status_code=400)
        return _FakeStream([_content("Answered anyway.")])

    monkeypatch.setattr(mod, "_ollama_chat", _fake_chat)
    assert list(mod._stream_ollama_tokens("hi", [], "p", think=True)) == \
        ["Answered anyway."]
    assert calls == [True, None]


# ── /chat/stream ─────────────────────────────────────────────────────────────

def _sse_events(response):
    return [json.loads(line[6:]) for line in
            response.get_data(as_text=True).splitlines()
            if line.startswith("data: ")]


@pytest.fixture()
def stream_route(client, monkeypatch):
    c, headers, mod = client
    monkeypatch.setattr(mod, "_maybe_vault_gated_answer", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_maybe_identity_answer", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_should_skip_rag", lambda *_a, **_k: True)
    monkeypatch.setattr(mod, "_detect_route", lambda *_a, **_k: "rag")
    monkeypatch.setattr(mod, "_try_med_fastpath", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_try_calendar_recurring", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_try_calendar_delete", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_save_conversation", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_ollama_chat", lambda messages, **kw: _FakeStream([
        _thought("Weighing it up."),
        _content("Two "),
        _content("sentences."),
    ]))
    return c, headers, mod


def test_show_thinking_adds_events_and_leaves_the_answer_alone(stream_route):
    c, headers, _mod = stream_route
    resp = c.post("/chat/stream",
                  json={"message": "what is a vector", "show_thinking": True},
                  headers=headers)
    events = _sse_events(resp)

    thinking = [e["thinking"] for e in events if "thinking" in e]
    assert thinking == ["Weighing it up."]
    assert all(e["delta"] == "" for e in events if "thinking" in e)

    final = events[-1]
    assert final["done"] is True
    assert final["full_response"] == "Two sentences."
    assert "Weighing it up." not in final["full_response"]


def test_without_the_flag_the_stream_is_exactly_what_it_was(stream_route):
    c, headers, _mod = stream_route
    resp = c.post("/chat/stream", json={"message": "what is a vector"},
                  headers=headers)
    events = _sse_events(resp)

    assert not [e for e in events if "thinking" in e]
    assert [e["delta"] for e in events if not e["done"]] == ["Two ", "sentences."]
    assert events[-1]["full_response"] == "Two sentences."


# ── the live turn buffer ─────────────────────────────────────────────────────

LONG = ("A vector is a quantity with both magnitude and direction. "
        "It is written as an arrow in space. "
        "Vectors add head to tail.")


def test_the_buffer_holds_the_turn_then_forgets_it(client):
    _c, _headers, mod = client
    mod._live_turn_begin("what is a vector")

    live = mod._live_turn_snapshot()
    assert live["active"] is True and live["done"] is False
    assert live["heard"] == "what is a vector"
    assert live["turn_id"] and live["updated_at"]

    mod._live_turn_append_thinking("Recalling the definition.")
    mod._live_turn_append_reply("A vector is ")
    assert mod._live_turn_snapshot()["reply"] == "A vector is "

    # The FULL answer, not the two sentences the speaker got.
    mod._live_turn_finish(LONG)
    done = mod._live_turn_snapshot()
    assert done["done"] is True
    assert done["reply"] == LONG
    assert "Vectors add head to tail" in done["reply"]
    assert mod.VOICE_TRUNCATION_TAIL_SCREEN.strip() not in done["reply"]
    assert done["thinking"] == "Recalling the definition."

    # Readable for 90 s, then gone — including the text, not just the flag.
    import time as _time
    still = mod._live_turn_snapshot(now=_time.time() + mod._LIVE_TURN_TTL - 1)
    assert still["active"] is True and still["reply"] == LONG
    stale = mod._live_turn_snapshot(now=_time.time() + mod._LIVE_TURN_TTL + 1)
    assert stale["active"] is False
    assert stale["turn_id"] is None and stale["reply"] == "" \
        and stale["thinking"] == ""


def test_thinking_is_capped_at_the_tail(client):
    _c, _headers, mod = client
    mod._live_turn_begin("go on then")
    mod._live_turn_append_thinking("x" * (mod._LIVE_TURN_THINKING_MAX + 500))
    mod._live_turn_append_thinking("THE END")

    held = mod._live_turn_snapshot()["thinking"]
    assert len(held) == mod._LIVE_TURN_THINKING_MAX
    assert held.endswith("THE END"), "the tail is the part worth keeping"


def test_a_buffer_failure_can_never_break_a_turn(client, monkeypatch):
    """Every mutator swallows its own failure — a display panel is not worth
    a dropped answer."""
    _c, _headers, mod = client
    broken = MagicMock()
    broken.__enter__ = MagicMock(side_effect=RuntimeError("lock is gone"))
    monkeypatch.setattr(mod, "_live_turn_lock", broken)

    mod._live_turn_begin("hello")
    mod._live_turn_append_thinking("...")
    mod._live_turn_append_reply("...")
    mod._live_turn_finish("done")
    mod._live_turn_clear()


def test_nothing_is_live_before_the_first_turn(client):
    _c, _headers, mod = client
    empty = mod._live_turn_snapshot()
    assert empty["active"] is False
    assert empty["turn_id"] is None and empty["heard"] == ""


# ── the route ────────────────────────────────────────────────────────────────

def test_voice_live_serves_the_buffer(client):
    c, headers, mod = client
    mod._live_turn_begin("is the porch light on")
    mod._live_turn_finish("Yes, the porch light is on.")

    body = c.get("/voice/live", headers=headers).get_json()
    assert body["active"] is True
    assert body["heard"] == "is the porch light on"
    assert body["reply"] == "Yes, the porch light is on."
    assert set(body) == {"active", "turn_id", "heard", "thinking", "reply",
                         "done", "updated_at"}


def test_voice_live_is_gated_exactly_like_voice_state(client):
    c, _headers, _mod = client
    assert (c.get("/voice/live").status_code
            == c.get("/voice/state").status_code)
    assert c.get("/voice/live").status_code in (401, 403)


# ── a spoken secret never enters the buffer ──────────────────────────────────
# The vault challenges the owner to SAY a PIN. For that one turn the recognized
# speech IS the secret, and /voice/live is polled about once a second by a wall
# display. Clearing the turn at the END of the turn is too late.

PIN = "4281"


@pytest.fixture()
def voice_turn(client, monkeypatch):
    """A /api/chat voice turn with everything but the live buffer stubbed."""
    c, headers, mod = client
    monkeypatch.setattr(mod, "_record_voice_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_pii_deterministic_reply", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "_apply_pii_honesty", lambda text, *_a, **_k: text)
    monkeypatch.setattr(mod._voice_full_tl, "text", None, raising=False)
    return c, headers, mod


def _say(c, headers, text):
    return c.post("/api/chat", headers=headers,
                  json={"messages": [{"role": "user", "content": text}]})


def _watcher(mod, seen, reply):
    """Stands in for the model: streams, and photographs the buffer while the
    turn is still open — what a concurrent /voice/live poll would have read."""
    def _run(message, *_a, **_k):
        seen.append(mod._live_turn_snapshot())            # just after begin
        mod._live_turn_append_thinking(f"They said {message}. ")
        mod._live_turn_append_reply(reply)
        seen.append(mod._live_turn_snapshot())            # mid-turn
        return reply
    return _run


def test_a_pending_pin_turn_is_never_readable(voice_turn, monkeypatch):
    c, headers, mod = voice_turn
    from puffpai import dashboard_display as dd
    dd.set_pending_vault("my birth certificate")
    seen = []
    monkeypatch.setattr(mod, "_process_voice_query",
                        _watcher(mod, seen, "🔓 Vault unlocked. Here it is."))
    try:
        assert _say(c, headers, PIN).status_code == 200
        after = c.get("/voice/live", headers=headers).get_json()
    finally:
        dd.clear_pending_vault()

    assert len(seen) == 2, "the turn actually ran"
    for snap in seen + [after]:
        assert PIN not in json.dumps(snap)
        assert snap["heard"] == "" and snap["thinking"] == "" \
            and snap["reply"] == ""
    assert after["active"] is False and after["turn_id"] is None


def test_the_pending_check_does_not_consume_the_slot(client):
    """The PIN-capture fastpath must still find the slot it needs."""
    _c, _headers, mod = client
    from puffpai import dashboard_display as dd
    dd.set_pending_vault("my passport")
    try:
        assert mod._live_turn_should_seal(PIN) is True
        # The pending slot alone seals it — an alphanumeric secret that no
        # shape heuristic would catch is withheld because a PIN was asked for.
        assert mod._live_turn_pin_shaped("hunter2x") is False
        assert mod._live_turn_should_seal("hunter2x") is True
        assert dd.pending_vault_active() is True
        assert dd.take_pending_vault() == "my passport", "read, never taken"
    finally:
        dd.clear_pending_vault()


def test_pin_shaped_words_are_withheld_with_nothing_pending(voice_turn,
                                                            monkeypatch):
    c, headers, mod = voice_turn
    from puffpai import dashboard_display as dd
    dd.clear_pending_vault()
    seen = []
    monkeypatch.setattr(mod, "_process_voice_query",
                        _watcher(mod, seen, "I'm not sure what that means."))
    assert _say(c, headers, "four two eight one").status_code == 200

    assert seen[0]["heard"] == "", "spoken digits are not published"
    assert "four two eight one" not in json.dumps(seen)
    assert c.get("/voice/live", headers=headers).get_json()["active"] is False


def test_pin_shape_leaves_ordinary_speech_alone(client):
    _c, _headers, mod = client
    for said in ("1234", "4281", "one two three four", "my pin is 4 2 8 1",
                 "4-2-8-1"):
        assert mod._live_turn_pin_shaped(said) is True, said
    for said in ("is the porch light on", "what is a vector", "set a timer",
                 "play KEXP", "seven", "turn the kitchen lights off"):
        assert mod._live_turn_pin_shaped(said) is False, said


def test_a_sealed_turn_publishes_no_reply_and_no_thinking(client):
    _c, _headers, mod = client
    mod._live_turn_begin("what is a vector")
    mod._live_turn_finish("A quantity with direction.")

    mod._live_turn_begin(PIN, sealed=True)
    opened = mod._live_turn_snapshot()
    assert opened["heard"] == "" and opened["reply"] == "", \
        "a sealed turn also drops what the last turn left readable"

    mod._live_turn_append_thinking(f"The PIN is {PIN}.")
    mod._live_turn_append_reply("Your passport number is C0293118.")
    mid = mod._live_turn_snapshot()
    assert mid["thinking"] == "" and mid["reply"] == ""

    mod._live_turn_finish("Your passport number is C0293118.")
    end = mod._live_turn_snapshot()
    assert end["active"] is False and end["turn_id"] is None
    blob = json.dumps([opened, mid, end])
    assert PIN not in blob and "C0293118" not in blob


def test_a_protected_topic_turn_is_sealed(client):
    _c, _headers, mod = client
    assert mod._live_turn_should_seal("read me my medical history") is True
    assert mod._live_turn_should_seal("is the porch light on") is False


def test_an_ordinary_turn_still_shows_what_was_heard(voice_turn, monkeypatch):
    c, headers, mod = voice_turn
    seen = []
    monkeypatch.setattr(mod, "_process_voice_query",
                        _watcher(mod, seen, "Yes, the porch light is on."))
    assert _say(c, headers, "is the porch light on").status_code == 200

    assert seen[0]["heard"] == "is the porch light on"
    assert seen[0]["active"] is True and seen[0]["done"] is False
    assert seen[1]["reply"] == "Yes, the porch light is on."
    live = c.get("/voice/live", headers=headers).get_json()
    assert live["active"] is True and live["done"] is True
    assert live["reply"] == "Yes, the porch light is on."


# ── one turn is current; writes belong to whoever opened theirs ──────────────
# Two /api/chat calls overlap (the wall and a phone). The buffer holds ONE
# turn, so without ownership request A's writes land under request B's heard —
# and A may be the sealed PIN turn.

def test_overlapping_requests_do_not_write_to_each_other(client):
    import threading
    _c, _headers, mod = client
    opened, replaced = threading.Event(), threading.Event()

    def request_a():
        mod._live_turn_begin(PIN, sealed=True)   # A's thread holds A's token
        opened.set()
        replaced.wait(5)
        mod._live_turn_append_thinking(f"They said the PIN {PIN}. ")
        mod._live_turn_append_reply("Your passport number is C0293118.")
        mod._live_turn_finish("ANSWER TO A")

    thread = threading.Thread(target=request_a)
    thread.start()
    assert opened.wait(5)
    token_b = mod._live_turn_begin("is the porch light on")   # B replaces A
    replaced.set()
    thread.join(5)

    snap = mod._live_turn_snapshot()
    assert snap["heard"] == "is the porch light on", "B still owns the buffer"
    assert snap["thinking"] == "" and snap["reply"] == ""
    assert snap["done"] is False, "A's finish must not close B's turn"
    # turn_id is random hex and can contain the PIN's digits by chance
    # (~0.4% of runs), which is a flake, not a leak: exclude it.
    blob = json.dumps({k: v for k, v in snap.items() if k != "turn_id"})
    assert PIN not in blob and "C0293118" not in blob and "ANSWER TO A" not in blob

    mod._live_turn_finish("Yes, it's on.", token=token_b)
    assert mod._live_turn_snapshot()["reply"] == "Yes, it's on."


def test_a_replaced_turns_answer_never_lands_under_the_new_one(client):
    _c, _headers, mod = client
    token_a = mod._live_turn_begin("read me the letter")
    mod._live_turn_begin("what is a vector")          # B replaces A

    mod._live_turn_append_reply("Dear Alex, ", token=token_a)
    mod._live_turn_finish("ANSWER TO A", token=token_a)

    snap = mod._live_turn_snapshot()
    assert snap["heard"] == "what is a vector"
    assert snap["reply"] == "" and snap["done"] is False
    assert "ANSWER TO A" not in json.dumps(snap)


def test_a_write_with_no_token_is_refused(client):
    """A caller that never opened a turn cannot write into somebody else's."""
    import threading
    _c, _headers, mod = client
    mod._live_turn_begin("what is a vector")

    def stranger():                      # its own thread-local: no token
        mod._live_turn_append_thinking("overheard reasoning")
        mod._live_turn_append_reply("overheard answer")
        mod._live_turn_finish("overheard answer")

    thread = threading.Thread(target=stranger)
    thread.start()
    thread.join(5)

    snap = mod._live_turn_snapshot()
    assert snap["thinking"] == "" and snap["reply"] == ""
    assert snap["done"] is False and snap["heard"] == "what is a vector"
    assert "overheard" not in json.dumps(snap)


def test_an_abandoned_turn_goes_stale_and_is_wiped(client):
    import time as _time
    _c, _headers, mod = client
    mod._live_turn_begin("read me the letter")
    mod._live_turn_append_reply("Dear Alex, ")

    held = mod._live_turn_snapshot(now=_time.time() + mod._LIVE_TURN_MAX_OPEN - 1)
    assert held["active"] is True, "a turn in flight is not a stale turn"

    gone = mod._live_turn_snapshot(now=_time.time() + mod._LIVE_TURN_MAX_OPEN + 1)
    assert gone["active"] is False and gone["turn_id"] is None
    assert gone["heard"] == "" and gone["reply"] == ""
    # Forgotten, not hidden: the next read finds nothing left to serve.
    now_snap = mod._live_turn_snapshot()
    assert now_snap["active"] is False and now_snap["heard"] == ""


def test_an_exception_in_the_turn_leaves_nothing_readable(voice_turn,
                                                          monkeypatch):
    c, headers, mod = voice_turn

    seen = []

    def _boom(*_a, **_k):
        seen.append(mod._live_turn_snapshot())   # the turn WAS open and readable
        raise RuntimeError("the model died mid-answer")

    monkeypatch.setattr(mod, "_process_voice_query", _boom)
    try:
        resp = _say(c, headers, "read me the letter")
        assert resp.status_code >= 500
    except RuntimeError:
        pass  # TESTING=True re-raises instead of returning 500

    assert seen and seen[0]["heard"] == "read me the letter"
    live = c.get("/voice/live", headers=headers).get_json()
    assert live["active"] is False and live["turn_id"] is None
    assert live["heard"] == "" and live["reply"] == ""


def test_thinking_is_asked_for_only_by_the_caller_that_can_show_it(client,
                                                                   monkeypatch):
    """PUFF_VOICE_THINK is the voice panel's lever, not a global one."""
    _c, _headers, mod = client
    monkeypatch.setenv("PUFF_VOICE_THINK", "1")
    seen = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"content": "Hi.", "thinking": "reasoning"}}

    def _fake_chat(messages, **kwargs):
        seen.append(kwargs.get("think"))
        return _Resp()

    monkeypatch.setattr(mod, "_ollama_chat", _fake_chat)

    # Typed chat / the briefing: no live turn of their own, no reasoning asked
    # for, and nothing of theirs in the buffer.
    mod._live_turn_clear()
    assert mod._live_turn_owner_token() is None
    mod._query_ollama_direct("hi", [], "persona")
    assert seen and seen[-1] is None, "no panel, no think flag"

    # A sealed spoken turn owns the buffer but may show nothing either.
    mod._live_turn_begin("4281", sealed=True)
    assert mod._live_turn_owner_token() is None
    mod._query_ollama_direct("hi", [], "persona")
    assert seen[-1] is None
    assert mod._live_turn_snapshot()["thinking"] == ""

    # The spoken turn that does have somewhere to put it.
    mod._live_turn_begin("what is a vector")
    assert mod._live_turn_owner_token() is not None
    mod._query_ollama_direct("hi", [], "persona")
    assert seen[-1] is True
    assert mod._live_turn_snapshot()["thinking"] == "reasoning"


def test_the_openai_stream_stays_thinking_free(stream_route, monkeypatch):
    """HA and the dashboard read /v1/chat/completions — the flag is not theirs."""
    c, headers, mod = stream_route
    monkeypatch.setenv("PUFF_VOICE_THINK", "1")
    monkeypatch.setattr(mod, "_ollama_chat", lambda messages, **kw: _FakeStream([
        _thought("secretly reasoning"),
        _content("Hi."),
    ]))
    resp = c.post("/v1/chat/completions", headers=headers,
                  json={"messages": [{"role": "user",
                                      "content": "what is a vector"}],
                        "model": "puffpai", "stream": True})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Hi." in body, "the stream really came from this model call"
    assert "secretly reasoning" not in body
    assert "thinking" not in body


def test_voice_thinking_is_off_unless_the_owner_turns_it_on(client,
                                                            monkeypatch):
    _c, _headers, mod = client
    monkeypatch.delenv("PUFF_VOICE_THINK", raising=False)
    assert mod._voice_thinking_enabled() is False
    monkeypatch.setenv("PUFF_VOICE_THINK", "1")
    assert mod._voice_thinking_enabled() is True
