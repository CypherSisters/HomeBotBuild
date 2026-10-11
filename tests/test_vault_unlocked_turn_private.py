"""A vault answer from an ALREADY-UNLOCKED chat session must not be persisted.

THE BUG THESE TESTS LOCK SHUT
-----------------------------
``_mark_vault_turn_private`` / ``_consume_vault_turn_private`` are the module's
rule for "this turn never reaches the on-disk conversation history": the vault
challenge and the PIN follow-up set it, and every history-write site consumes it
(``/conversation``, ``/chat/send``, ``/chat/stream``, and the voice transcript
writer ``_record_voice_turn`` in ``/api/chat``).

``_maybe_vault_gated_answer`` set it nowhere. That function is the chat-side
vault gate, and its unlocked-session branch ("case 2a") returns text SYNTHESIZED
FROM DECRYPTED VAULT DOCUMENTS. Its PIN branch ("case 1") is worse: the user
half of that turn is the PIN itself. Both are reached from
``_stream_ollama_tokens``, which is what ``/chat/stream`` streams from — so the
answer, and the PIN, were written to ``_chat_history`` and saved to disk by
``_save_conversation``, in plaintext, and rendered in the dashboard transcript.

WHAT IS ASSERTED
  1. The gate marks the turn private on every branch that returns vault content
     or names the protected question (case 1, case 2a, case 2b, unpaired).
  2. Through the real ``/chat/stream`` endpoint: the unlocked answer is SERVED
     to the caller and NOT saved; so is the PIN turn and the challenge turn.
  3. An ordinary turn through the same endpoint IS still saved.
  4. The mark is consumed by the turn that set it, so it cannot suppress the
     next, ordinary turn's transcript.
  5. The suppression mechanism at the voice transcript writer still holds: a
     marked turn is not passed to ``_record_voice_turn``.

Flask test client only. No real vault is opened, no Ollama call is made, and
``_save_conversation`` is captured so nothing is written to the real store.
"""

import json
import time

import pytest

from puffpai import api_server

#: Shaped like the dashboard's derived device id (32 hex chars), so the request
#: is an IDENTIFIED caller and not the single-use anonymous key.
DEVICE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SESSION = "vault-session-abc"

PROTECTED = "what are my allergies"
ORDINARY = "what is the capital of France"
PIN = "135790"

#: What the vault path returns. A sentinel, so "was it persisted?" is a
#: substring check over everything the endpoint tried to save.
VAULT_SENTENCE = "Penicillin and sulfa, from the allergy card in your vault."
VAULT_DOC_TEXT = "Allergies: penicillin, sulfa. Patient DOB 1970-01-01."


@pytest.fixture(autouse=True)
def fresh_turn_privacy():
    """The flag is a consume-once thread-local; never inherit or leave one."""
    api_server._reset_vault_turn_privacy()
    yield
    api_server._reset_vault_turn_privacy()


@pytest.fixture
def vault_state():
    saved = dict(api_server._VAULT_CHAT_STATE)
    api_server._VAULT_CHAT_STATE.clear()
    try:
        yield api_server._VAULT_CHAT_STATE
    finally:
        api_server._VAULT_CHAT_STATE.clear()
        api_server._VAULT_CHAT_STATE.update(saved)


@pytest.fixture
def saved_turns(monkeypatch):
    """Everything the endpoint tried to write to the conversation store."""
    calls = []
    monkeypatch.setattr(api_server, "_save_conversation",
                        lambda conv: calls.append(conv))
    before = list(api_server._chat_history)
    try:
        yield calls
    finally:
        api_server._chat_history[:] = before


class _Vault:
    """Initialized, holding documents, and willing to unlock with one PIN."""

    is_initialized = True

    def __init__(self):
        self.validated = []

    def _validate_session(self, sid):
        self.validated.append(sid)
        if sid != SESSION:
            raise AssertionError("expired")

    def authenticate(self, pin):
        assert pin == PIN, "the gate must pass the PIN through unchanged"
        return type("S", (), {"session_id": SESSION})()

    def migrate_plaintext_files(self, sid):
        return None


@pytest.fixture
def vault(monkeypatch):
    v = _Vault()
    monkeypatch.setattr(api_server, "_get_vault", lambda: v)
    monkeypatch.setattr(api_server, "_count_vaulted_documents", lambda _v: 2)
    monkeypatch.setattr(
        api_server, "_load_vaulted_documents",
        lambda _v, sid: [{"filename": "doc_1", "text": VAULT_DOC_TEXT}])
    # Stands in for the local LLM read of the decrypted documents.
    monkeypatch.setattr(api_server, "_synthesize_from_vault",
                        lambda q, docs: VAULT_SENTENCE)
    return v


@pytest.fixture
def unlocked(vault_state, vault):
    """A live 300-second unlock. Keyed under BOTH the identified device key and
    ``unknown``: a streaming response is consumed after the request context is
    torn down, so the gate may see either key depending on the surface."""
    for key in (f"device:{DEVICE}", "unknown"):
        vault_state[key] = {"vault_session": SESSION,
                            "unlocked_at": time.time()}
    return vault_state


@pytest.fixture
def client(monkeypatch):
    api_server.app.config.update(TESTING=True)
    api_server._rate_limit_store.clear()
    # No knowledge-base read and no Ollama call from a test.
    monkeypatch.setattr(api_server, "_get_rag_context", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_ollama_chat", _fake_ollama_stream)
    with api_server.app.test_client() as c:
        yield c


def _fake_ollama_stream(messages, **kwargs):
    lines = [json.dumps({"message": {"content": "Paris."}}).encode(),
             json.dumps({"done": True}).encode()]
    return type("R", (), {"raise_for_status": lambda self: None,
                          "iter_lines": lambda self: iter(lines)})()


def _headers():
    return {"X-API-Key": api_server._API_KEY, "X-Puff-Device-Id": DEVICE}


def _stream(client, message):
    resp = client.post("/chat/stream", json={"message": message,
                                             "conversation_id": "conv-under-test"},
                       headers=_headers())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_data(as_text=True)


def _persisted_text(saved_turns):
    return json.dumps(saved_turns)


# ── 1. the gate itself marks, on every branch that says something vault-ish ──

def _gate(message):
    with api_server.app.test_request_context(
            "/chat/stream", method="POST",
            headers={"X-Puff-Device-Id": DEVICE},
            environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        return api_server._maybe_vault_gated_answer(message)


def test_the_unlocked_session_answer_is_marked_private(unlocked):
    """Case 2a: the branch that returns decrypted vault content with no PIN."""
    assert _gate(PROTECTED) == VAULT_SENTENCE
    assert api_server._consume_vault_turn_private() is True


def test_the_pin_reply_is_marked_private(vault_state, vault):
    """Case 1: the user half of this turn is the PIN."""
    vault_state[f"device:{DEVICE}"] = {"pending_query": PROTECTED,
                                       "pending_since": time.time()}
    answer = _gate(PIN)
    assert VAULT_SENTENCE in answer
    assert api_server._consume_vault_turn_private() is True


def test_the_challenge_turn_is_marked_private(vault_state, vault):
    """Case 2b: no vault content, but a durable record that it was asked for."""
    answer = _gate(PROTECTED)
    assert "PIN" in answer
    assert api_server._consume_vault_turn_private() is True


def test_an_ordinary_question_is_not_marked(vault_state, vault):
    assert _gate(ORDINARY) is None
    assert api_server._consume_vault_turn_private() is False


# ── 2. through the real endpoint: served to the caller, saved nowhere ────────

def test_an_unlocked_vault_answer_is_served_but_never_saved(client, unlocked,
                                                            saved_turns):
    body = _stream(client, PROTECTED)

    assert VAULT_SENTENCE in body, "the caller must still get their answer"
    assert saved_turns == [], "a vault answer must not reach the store"
    assert VAULT_SENTENCE not in _persisted_text(saved_turns)
    assert VAULT_DOC_TEXT not in _persisted_text(saved_turns)


def test_the_pin_turn_is_never_saved(client, vault_state, vault, saved_turns):
    """The PIN is the user content of this turn; nothing about it persists."""
    for key in (f"device:{DEVICE}", "unknown"):
        vault_state[key] = {"pending_query": PROTECTED,
                            "pending_since": time.time()}
    body = _stream(client, PIN)

    assert VAULT_SENTENCE in body
    assert saved_turns == []
    assert PIN not in _persisted_text(saved_turns)


def test_the_challenge_turn_is_never_saved(client, vault_state, vault,
                                           saved_turns):
    body = _stream(client, PROTECTED)

    assert "PIN" in body
    assert saved_turns == []


# ── 3. …and an ordinary turn is still recorded, exactly as before ────────────

def test_an_ordinary_turn_is_still_saved(client, vault_state, vault,
                                         saved_turns):
    body = _stream(client, ORDINARY)

    assert "Paris." in body
    assert len(saved_turns) == 1, "normal chat must keep its transcript"
    stored = _persisted_text(saved_turns)
    assert ORDINARY in stored and "Paris." in stored


def test_the_mark_does_not_leak_into_the_next_ordinary_turn(client, unlocked,
                                                            saved_turns):
    """Consume-once, proven end to end: the vault turn takes the flag with it."""
    _stream(client, PROTECTED)
    assert saved_turns == []

    _stream(client, ORDINARY)
    assert len(saved_turns) == 1, "the next turn must not stay suppressed"
    stored = _persisted_text(saved_turns)
    assert ORDINARY in stored
    assert VAULT_SENTENCE not in stored


# ── 4. the voice transcript writer obeys the same flag ──────────────────────
# NOTE for the next reader: on this branch ``/api/chat`` does NOT reach
# ``_maybe_vault_gated_answer`` (its vault surfaces are ``_try_show`` and
# ``_try_vault_pin_followup``, which both mark). What is pinned here is the
# SUPPRESSION MECHANISM at the voice write site, so a future vault answer added
# to the spoken path inherits a proven "marked -> not recorded".

def test_a_marked_voice_turn_is_not_passed_to_the_transcript_writer(
        client, monkeypatch):
    recorded = []
    monkeypatch.setattr(api_server, "_record_voice_turn",
                        lambda *a, **k: recorded.append(a))

    def _marks_the_turn(message, history=None, **kwargs):
        api_server._mark_vault_turn_private()
        return VAULT_SENTENCE

    monkeypatch.setattr(api_server, "_process_voice_query", _marks_the_turn)
    resp = client.post("/api/chat",
                       json={"messages": [{"role": "user",
                                           "content": PROTECTED}]},
                       headers=_headers())
    assert resp.status_code == 200
    assert recorded == [], "a marked turn must not reach the transcript"

    # …and the same call without the mark records normally.
    monkeypatch.setattr(api_server, "_process_voice_query",
                        lambda message, history=None, **kwargs: "It is sunny.")
    resp = client.post("/api/chat",
                       json={"messages": [{"role": "user",
                                           "content": ORDINARY}]},
                       headers=_headers())
    assert resp.status_code == 200
    assert len(recorded) == 1


# ── 5. the live transcript buffer ────────────
# ORDERING IS THE WHOLE POINT HERE. The private-turn mark is consumed at the END of
# a turn, but the live buffer is written at the START of one: _live_turn_begin
# publishes `heard` at the top of /api/chat, and a wall display polls
# /voice/live about once a second while she answers. A consume-at-the-end flag
# cannot protect a buffer that was already read.
#
# So the live transcript takes its own decision BEFORE the buffer opens
# (_live_turn_should_seal). What is pinned below is the SEAM between the two
# features: every branch the mark makes private is also a branch the live
# buffer seals, the seal holds MID-turn and not just at the end, and the typed paths
# that actually reach the vault gate never open a live turn at all.


@pytest.fixture(autouse=True)
def fresh_live_turn():
    api_server._live_turn_clear()
    yield
    api_server._live_turn_clear()


@pytest.fixture
def voice_turn(client, monkeypatch):
    """A /api/chat turn with everything but the live buffer stubbed out."""
    monkeypatch.setattr(api_server, "_record_voice_turn", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_pii_deterministic_reply",
                        lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_apply_pii_honesty",
                        lambda text, *a, **k: text)
    monkeypatch.setattr(api_server._voice_full_tl, "text", None, raising=False)
    return client


def _watcher(seen, reply):
    """Stands in for the pipeline and photographs the buffer MID-turn — what a
    /voice/live poll landing while she is still answering would have read."""
    def _run(message, *a, **k):
        seen.append(api_server._live_turn_snapshot())      # just after begin
        api_server._live_turn_append_thinking("They asked: %s. " % message)
        api_server._live_turn_append_reply(reply)
        seen.append(api_server._live_turn_snapshot())      # mid-turn
        return reply
    return _run


def _say(client, text):
    return client.post("/api/chat", headers=_headers(),
                       json={"messages": [{"role": "user", "content": text}]})


def _live(client):
    return client.get("/voice/live", headers=_headers()).get_json()


def test_the_typed_vault_paths_never_open_a_live_turn(client, unlocked,
                                                      saved_turns):
    """/chat/stream serves vault contents but has no live turn to put them in:
    the buffer's only opener is /api/chat, and a write without that turn's
    token is refused."""
    body = _stream(client, PROTECTED)
    assert VAULT_SENTENCE in body, "the caller still got their answer"

    snap = _live(client)
    assert snap["active"] is False and snap["turn_id"] is None
    blob = json.dumps(snap)
    assert VAULT_SENTENCE not in blob and VAULT_DOC_TEXT not in blob
    assert PROTECTED not in blob


def test_a_protected_question_is_never_published_to_the_wall(voice_turn,
                                                             monkeypatch):
    """Case 2a's utterance and answer, on the spoken surface: sealed at the
    open, still sealed mid-turn, and nothing readable afterwards."""
    seen = []
    monkeypatch.setattr(api_server, "_process_voice_query",
                        _watcher(seen, VAULT_SENTENCE))
    assert _say(voice_turn, PROTECTED).status_code == 200

    assert len(seen) == 2, "the turn actually ran"
    for snap in seen + [_live(voice_turn)]:
        blob = json.dumps(snap)
        assert snap["heard"] == "", "the question names what is in the vault"
        assert snap["reply"] == "" and snap["thinking"] == ""
        assert VAULT_SENTENCE not in blob and PROTECTED not in blob


def test_a_pin_spoken_while_the_chat_gate_waits_is_never_published(
        voice_turn, vault_state, monkeypatch):
    """Case 1 on the spoken surface. The chat gate's own pending slot arms the
    seal, and reading it must not consume it — the PIN fastpath still needs
    it."""
    key = f"device:{DEVICE}"
    vault_state[key] = {"pending_query": PROTECTED,
                        "pending_since": time.time()}
    seen = []
    monkeypatch.setattr(api_server, "_process_voice_query",
                        _watcher(seen, VAULT_SENTENCE))
    assert _say(voice_turn, PIN).status_code == 200

    for snap in seen + [_live(voice_turn)]:
        blob = json.dumps(snap)
        assert PIN not in blob and VAULT_SENTENCE not in blob
        assert snap["heard"] == "" and snap["reply"] == ""
    assert vault_state[key]["pending_query"] == PROTECTED, "slot not consumed"


def test_a_vault_document_request_is_sealed_before_the_buffer_opens():
    """"Pull up the passport" names a vaulted document TYPE but no owner, so
    the protected-query test does not catch it. It is answered by the display
    challenge, which marks the turn private — far too late for a buffer that
    published `heard` at the top of the turn."""
    for said in ("pull up the passport", "show me the birth certificate",
                 "show me the bank statement", "pull up the tax return"):
        assert api_server._live_turn_should_seal(said) is True, said


def test_the_document_seal_does_not_swallow_ordinary_show_requests():
    """The other half: sealing everything would be a dark panel, not privacy."""
    for said in ("show me my calendar", "show me the weather",
                 "put the dashboard up", "what is the capital of France"):
        assert api_server._live_turn_should_seal(said) is False, said


def test_an_ordinary_voice_turn_is_still_published(voice_turn, monkeypatch):
    """The control: an ordinary spoken turn reaches the wall exactly as before,
    so none of the above is privacy bought with a blank panel."""
    seen = []
    monkeypatch.setattr(api_server, "_process_voice_query",
                        _watcher(seen, "Paris is the capital of France."))
    assert _say(voice_turn, ORDINARY).status_code == 200

    assert seen[0]["heard"] == ORDINARY and seen[0]["active"] is True
    assert seen[1]["reply"] == "Paris is the capital of France."
    live = _live(voice_turn)
    assert live["active"] is True and live["done"] is True
    assert live["reply"] == "Paris is the capital of France."


# ── 6. the route that persists nothing still cleans up after itself ─────────

def test_the_completions_route_leaves_no_mark_behind(client, monkeypatch):
    """/v1/chat/completions has three exits — the identity early return, the
    real-stream generator, and the plain one below them — and writes no
    conversation history on any of them. A mark it left set could therefore
    only ever suppress the NEXT turn's transcript, on a surface that does
    persist. This pins the last exit, the one reached through the router."""
    def _marks_and_answers(*a, **k):
        api_server._mark_vault_turn_private()
        return {'response': 'Paris.', 'sources': [], 'suggestions': []}

    monkeypatch.setattr(api_server, "_process_chat_query", _marks_and_answers)
    resp = client.post("/v1/chat/completions", headers=_headers(),
                       json={"messages": [{"role": "user",
                                           "content": ORDINARY}]})
    assert resp.status_code == 200
    assert api_server._consume_vault_turn_private() is False, \
        "the mark must not survive a route that never persisted anything"
