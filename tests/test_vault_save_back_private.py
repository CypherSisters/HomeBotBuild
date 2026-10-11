"""The CLIENT must not be able to re-persist a turn the backend suppressed.

THE HOLE THESE TESTS LOCK SHUT
------------------------------
``_mark_vault_turn_private`` / ``_consume_vault_turn_private`` keep a vault turn
out of the on-disk history at every SERVER write site (see
tests/test_vault_unlocked_turn_private.py). ``/chat/save`` is not one of them.
It is a SECOND request, posted by the dashboard Chat page after every streamed
turn (``saveChat(convId, text, streamedContent)``), carrying whatever
``user_message`` / ``assistant_message`` the browser chose. The thread-local
belongs to a different request, so it is meaningless there — and the route
appended the pair with no privacy check at all.

Result, on the surface the household actually uses: the PIN they typed and the
answer read out of the decrypted vault were written to the transcript by the
client, in plaintext, moments after the backend refused to write them.

THE FIX, IN TWO INDEPENDENT HALVES
  1. A private-turn REGISTRY. A turn found private at a serving route's
     recording site is remembered as a salted digest of its user half, with a
     TTL and a size bound; ``/chat/save`` refuses a save-back that matches.
  2. The standing CLASSIFIERS, via ``_live_turn_should_seal``: PIN-shaped, a
     pending vault challenge, a vaulted document type, or a protected-topic
     question is refused whether or not the registry has heard of it. This
     covers a restart, an evicted entry, and a client that invents a turn.
Either half refusing is enough, and any exception in either refuses.

AND A WIRE CONTRACT so the client can stop posting in the first place: the
terminal SSE event carries ``private`` (top level on ``/chat/stream``, inside
``puffpai_meta`` on ``/v1/chat/completions``), and ``/chat/send`` returns it in
its JSON. Always present on those exits, so a client can tell "not private"
from "backend too old to say".

NOTE ON WHICH ROUTE THE CHAT PAGE STREAMS FROM. It is
``/v1/chat/completions`` — ``streamMessage`` defaults to ``route: 'openai'``
and that route is sent NO conversation id — which is exactly why the registry
is keyed on the message digest alone and never on the conversation id.

Flask test client only. No real vault, no Ollama call, no network, and
``_save_conversation`` is captured so nothing reaches the real store.
"""

import hashlib
import json
import time
import uuid

import pytest

from puffpai import api_server

#: Shaped like the dashboard's derived device id (32 hex chars), so the request
#: is an IDENTIFIED caller and not the single-use anonymous key.
DEVICE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SESSION = "vault-session-abc"

PROTECTED = "what are my allergies"
ORDINARY = "what is the capital of France"
PIN = "135790"

#: A private turn the CLASSIFIERS would wave through: not PIN-shaped, not a
#: protected topic, not a vaulted document type. Only the registry can refuse
#: its save-back, so every registry assertion below uses it — a test built on
#: ``PROTECTED`` or ``PIN`` would pass with the registry deleted. Routed "rag"
#: (asserted below), which is the branch that reaches the vault gate.
REGISTRY_ONLY = "tell me about the blue folder"

VAULT_SENTENCE = "Penicillin and sulfa, from the allergy card in your vault."
VAULT_DOC_TEXT = "Allergies: penicillin, sulfa. Patient DOB 1970-01-01."


# ── fixtures (conventions from tests/test_vault_unlocked_turn_private.py) ────

@pytest.fixture(autouse=True)
def fresh_turn_privacy():
    """The flag is a consume-once thread-local; never inherit or leave one."""
    api_server._reset_vault_turn_privacy()
    yield
    api_server._reset_vault_turn_privacy()


@pytest.fixture(autouse=True)
def fresh_registry():
    """The registry is process-global; no test may inherit or leak an entry."""
    with api_server._private_turn_lock:
        saved = dict(api_server._private_turns)
        api_server._private_turns.clear()
    try:
        yield api_server._private_turns
    finally:
        with api_server._private_turn_lock:
            api_server._private_turns.clear()
            api_server._private_turns.update(saved)


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
    """Everything any endpoint tried to write to the conversation store.

    ``_save_conversation`` is the ONLY path from chat history to disk, so
    capturing it is both the assertion and the guarantee that this test file
    writes nothing.
    """
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
    monkeypatch.setattr(api_server, "_synthesize_from_vault",
                        lambda q, docs: VAULT_SENTENCE)
    return v


@pytest.fixture
def unlocked(vault_state, vault):
    """A live 300-second unlock, keyed under both the identified device key and
    ``unknown`` (a streamed response is consumed after the request context is
    torn down, so the gate may see either)."""
    for key in (f"device:{DEVICE}", "unknown"):
        vault_state[key] = {"vault_session": SESSION,
                            "unlocked_at": time.time()}
    return vault_state


@pytest.fixture
def client(monkeypatch):
    api_server.app.config.update(TESTING=True)
    api_server._rate_limit_store.clear()
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


# ── wire helpers ────────────────────────────────────────────────────────────

def _stream(client, message, conversation_id="conv-under-test"):
    resp = client.post("/chat/stream",
                       json={"message": message,
                             "conversation_id": conversation_id},
                       headers=_headers())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_data(as_text=True)


def _openai_stream(client, message):
    """The route the dashboard Chat page really streams from: no conversation
    id goes out, and the page posts one it minted itself to /chat/save."""
    resp = client.post("/v1/chat/completions", headers=_headers(),
                       json={"model": "puffpai", "stream": True,
                             "messages": [{"role": "user",
                                           "content": message}]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_data(as_text=True)


def _events(body):
    out = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


def _terminal(body):
    """The event that ends a /chat/stream response."""
    finals = [ev for ev in _events(body) if ev.get("done") is True]
    assert len(finals) == 1, body
    return finals[0]


def _final_meta(body):
    """The ``puffpai_meta`` block of a /v1/chat/completions response."""
    metas = [ev["puffpai_meta"] for ev in _events(body) if "puffpai_meta" in ev]
    assert len(metas) == 1, body
    return metas[0]


def _save_back(client, message, answer, conversation_id="conv-under-test"):
    return client.post("/chat/save", headers=_headers(),
                       json={"conversation_id": conversation_id,
                             "user_message": message,
                             "assistant_message": answer})


def _ok_shape(resp, conversation_id="conv-under-test"):
    """The route's ORDINARY success shape: a refusal must be indistinguishable
    from a write, or the client retries / shows an error / learns which of its
    turns the appliance found interesting."""
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json() == {"success": True,
                               "conversation_id": conversation_id}


def _persisted_text(saved_turns):
    return json.dumps(saved_turns)


# ══════════════════════════════════════════════════════════════════════════
#  1 - the leak itself: stream privately, then post the turn back
# ══════════════════════════════════════════════════════════════════════════

def test_the_unlocked_vault_answer_save_back_writes_nothing(client, unlocked,
                                                            saved_turns):
    body = _stream(client, PROTECTED)
    assert VAULT_SENTENCE in body, "the caller must still get their answer"
    assert saved_turns == [], "the serving route already refused"

    _ok_shape(_save_back(client, PROTECTED, VAULT_SENTENCE))

    assert saved_turns == [], "the client put the vault answer back on disk"
    stored = _persisted_text(saved_turns)
    assert VAULT_SENTENCE not in stored and VAULT_DOC_TEXT not in stored


def test_the_pin_turn_save_back_writes_nothing(client, vault_state, vault,
                                               saved_turns):
    """The user half of this turn IS the PIN."""
    for key in (f"device:{DEVICE}", "unknown"):
        vault_state[key] = {"pending_query": PROTECTED,
                            "pending_since": time.time()}
    body = _stream(client, PIN)
    assert VAULT_SENTENCE in body
    assert saved_turns == []

    _ok_shape(_save_back(client, PIN, VAULT_SENTENCE))

    assert saved_turns == []
    assert PIN not in _persisted_text(saved_turns)


def test_the_dashboard_chat_pages_own_route_is_covered(client, unlocked,
                                                       saved_turns):
    """/v1/chat/completions persists nothing itself, which is why it was left
    alone — but it IS the route the Chat page streams from, and that page's
    save-back is a persistence path for it. Streamed here, posted back with a
    browser-minted conversation id, exactly as the page does it."""
    body = _openai_stream(client, PROTECTED)
    assert VAULT_SENTENCE in body
    assert _final_meta(body)["private"] is True
    assert saved_turns == []

    _ok_shape(_save_back(client, PROTECTED, VAULT_SENTENCE,
                         conversation_id="conv-minted-by-the-browser"),
              conversation_id="conv-minted-by-the-browser")

    assert saved_turns == []


def test_a_brand_new_conversation_id_does_not_get_round_it(client, unlocked,
                                                           saved_turns):
    """The registry is keyed on the message, not the conversation: a client
    that invents a fresh id for the save-back is refused just the same."""
    _stream(client, PROTECTED, conversation_id="conv-A")
    fresh = f"conv-{uuid.uuid4().hex}"
    _ok_shape(_save_back(client, PROTECTED, VAULT_SENTENCE,
                         conversation_id=fresh), conversation_id=fresh)
    assert saved_turns == []


def test_a_private_turn_the_classifiers_would_miss_is_still_refused(
        client, vault_state, vault, saved_turns, monkeypatch):
    """The registry on its own. ``REGISTRY_ONLY`` is PIN-innocent, topic-
    innocent and document-innocent, so nothing but the registry can refuse it
    — this test fails if the registry is removed and the classifiers are left."""
    assert api_server._live_turn_should_seal(REGISTRY_ONLY) is False, \
        "pick a message the classifiers really do wave through"
    assert api_server._detect_route(REGISTRY_ONLY) == "rag", \
        "only the rag branch reaches the vault gate this test patches"

    def _marks_and_answers(user_message):
        if user_message == REGISTRY_ONLY:
            api_server._mark_vault_turn_private()
            return VAULT_SENTENCE
        return None

    monkeypatch.setattr(api_server, "_maybe_vault_gated_answer",
                        _marks_and_answers)
    body = _stream(client, REGISTRY_ONLY)
    assert VAULT_SENTENCE in body
    assert _terminal(body)["private"] is True
    assert saved_turns == []

    _ok_shape(_save_back(client, REGISTRY_ONLY, VAULT_SENTENCE))
    assert saved_turns == []


# ══════════════════════════════════════════════════════════════════════════
#  2 - and an ordinary turn is still written, exactly as before
# ══════════════════════════════════════════════════════════════════════════

def test_an_ordinary_save_back_still_persists(client, vault_state, vault,
                                              saved_turns):
    _ok_shape(_save_back(client, ORDINARY, "Paris."))
    assert len(saved_turns) == 1, "normal chat must keep its transcript"
    stored = _persisted_text(saved_turns)
    assert ORDINARY in stored and "Paris." in stored


def test_an_ordinary_streamed_turn_is_still_saved_back(client, vault_state,
                                                       vault, saved_turns):
    body = _stream(client, ORDINARY)
    assert "Paris." in body
    assert len(saved_turns) == 1, "the serving route wrote its own copy"

    _ok_shape(_save_back(client, ORDINARY, "Paris."))
    assert len(saved_turns) == 2, "the save-back must not be swallowed"


def test_the_registry_does_not_suppress_the_next_ordinary_turn(client,
                                                               unlocked,
                                                               saved_turns):
    """A registered private turn must refuse ITS message and nothing else."""
    _stream(client, PROTECTED)
    _ok_shape(_save_back(client, PROTECTED, VAULT_SENTENCE))
    assert saved_turns == []

    _ok_shape(_save_back(client, ORDINARY, "Paris."))
    assert len(saved_turns) == 1


# ══════════════════════════════════════════════════════════════════════════
#  3 - the wire contract: `private` on the terminal event
# ══════════════════════════════════════════════════════════════════════════

def test_the_terminal_event_declares_a_private_stream_turn(client, unlocked):
    assert _terminal(_stream(client, PROTECTED))["private"] is True


def test_the_terminal_event_declares_a_private_pin_turn(client, vault_state,
                                                        vault):
    for key in (f"device:{DEVICE}", "unknown"):
        vault_state[key] = {"pending_query": PROTECTED,
                            "pending_since": time.time()}
    assert _terminal(_stream(client, PIN))["private"] is True


def test_an_ordinary_terminal_event_says_private_false(client, vault_state,
                                                       vault):
    """False, not absent: a client can then tell "not private" from "backend
    too old to say", and fail closed on the second."""
    term = _terminal(_stream(client, ORDINARY))
    assert term["private"] is False
    assert term["done"] is True and "full_response" in term


def test_the_openai_route_declares_privacy_in_puffpai_meta(client, unlocked,
                                                           vault_state):
    assert _final_meta(_openai_stream(client, PROTECTED))["private"] is True


def test_the_openai_route_says_false_for_an_ordinary_turn(client, vault_state,
                                                          vault):
    assert _final_meta(_openai_stream(client, ORDINARY))["private"] is False


def test_chat_send_reports_privacy_in_its_json(client, vault_state, vault,
                                               saved_turns, monkeypatch):
    """The non-streaming surface. Its vault answers arrive through the
    fastpaths inside ``_process_chat_query`` rather than through
    ``_maybe_vault_gated_answer`` (which only the streaming routes consult), so
    the mark is injected there — same technique as the voice-path test in
    tests/test_vault_unlocked_turn_private.py."""
    def _marks_and_answers(*a, **k):
        api_server._mark_vault_turn_private()
        return {'response': VAULT_SENTENCE, 'sources': [], 'suggestions': []}

    monkeypatch.setattr(api_server, "_process_chat_query", _marks_and_answers)
    resp = client.post("/chat/send", headers=_headers(),
                       json={"message": REGISTRY_ONLY,
                             "conversation_id": "conv-under-test"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["private"] is True
    assert saved_turns == [], "the route must not have written it either"

    # ...and the save-back of that same turn is refused by the registry alone.
    _ok_shape(_save_back(client, REGISTRY_ONLY, VAULT_SENTENCE))
    assert saved_turns == []

    monkeypatch.setattr(
        api_server, "_process_chat_query",
        lambda *a, **k: {'response': "Paris.", 'sources': [],
                         'suggestions': []})
    resp = client.post("/chat/send", headers=_headers(),
                       json={"message": ORDINARY,
                             "conversation_id": "conv-under-test"})
    assert resp.get_json()["private"] is False
    assert len(saved_turns) == 1, "an ordinary send still keeps its transcript"


def test_the_flag_is_consumed_once_and_the_event_still_gets_it(client,
                                                               unlocked,
                                                               saved_turns):
    """Emitting `private` must not consume the flag out from under the write
    site, nor the other way round: one consume, one boolean, both readers."""
    body = _stream(client, PROTECTED)
    assert _terminal(body)["private"] is True   # the event saw it
    assert saved_turns == []                    # ...and so did the write site


# ══════════════════════════════════════════════════════════════════════════
#  4 - the registry: what it holds, how long, how many
# ══════════════════════════════════════════════════════════════════════════

def test_the_registry_never_holds_the_plaintext(fresh_registry):
    """The user half of a PIN turn is the PIN. A plain digest of six digits is
    brute-forced instantly, so the salt is the only thing making the stored
    value useless to whoever reads this process's memory or a heap dump."""
    api_server._register_private_turn(PIN)
    keys = list(fresh_registry)
    assert len(keys) == 1
    assert PIN not in json.dumps(keys)
    assert hashlib.sha256(PIN.encode()).hexdigest() not in keys, \
        "unsalted digest: a six-digit PIN would be recovered in milliseconds"


def test_the_registry_matches_across_whitespace_and_case(fresh_registry):
    api_server._register_private_turn("  What Are My Allergies  ")
    assert api_server._is_registered_private_turn("what are my allergies")


def test_the_registry_forgets_a_turn_after_the_ttl(fresh_registry, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(api_server.time, "time", lambda: clock[0])

    api_server._register_private_turn(REGISTRY_ONLY)
    assert api_server._is_registered_private_turn(REGISTRY_ONLY) is True

    clock[0] += api_server._PRIVATE_TURN_TTL - 1
    assert api_server._is_registered_private_turn(REGISTRY_ONLY) is True

    clock[0] += 2
    assert api_server._is_registered_private_turn(REGISTRY_ONLY) is False
    assert fresh_registry == {}, "an expired entry is dropped, not kept"


def test_the_registry_is_bounded_and_evicts_the_oldest(fresh_registry):
    over = api_server._PRIVATE_TURN_MAX + 40
    for i in range(over):
        api_server._register_private_turn(f"private turn number {i}")

    assert len(fresh_registry) == api_server._PRIVATE_TURN_MAX
    assert api_server._is_registered_private_turn("private turn number 0") \
        is False, "the oldest must be the one evicted"
    assert api_server._is_registered_private_turn(
        f"private turn number {over - 1}") is True


def test_a_save_back_does_not_consume_the_registration(client, unlocked,
                                                       saved_turns):
    """A browser can post the same turn twice (a retry, a second tab). Every
    copy is refused, not just the first."""
    _stream(client, PROTECTED)
    for _ in range(3):
        _ok_shape(_save_back(client, PROTECTED, VAULT_SENTENCE))
    assert saved_turns == []


def test_registering_an_empty_message_is_a_no_op(fresh_registry):
    api_server._register_private_turn("")
    api_server._register_private_turn("   ")
    api_server._register_private_turn(None)
    assert fresh_registry == {}


# ══════════════════════════════════════════════════════════════════════════
#  5 - fail closed
# ══════════════════════════════════════════════════════════════════════════

def test_a_registry_failure_refuses_the_save(client, vault_state, vault,
                                             saved_turns, monkeypatch):
    def _boom(_message):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(api_server, "_is_registered_private_turn", _boom)
    _ok_shape(_save_back(client, ORDINARY, "Paris."))
    assert saved_turns == [], "a broken privacy check must not open the write"


def test_a_classifier_failure_refuses_the_save(client, vault_state, vault,
                                               saved_turns, monkeypatch):
    def _boom(_message):
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(api_server, "_is_registered_private_turn",
                        lambda _m: False)
    monkeypatch.setattr(api_server, "_live_turn_should_seal", _boom)
    _ok_shape(_save_back(client, ORDINARY, "Paris."))
    assert saved_turns == []


def test_a_registry_write_failure_never_breaks_the_turn(client, unlocked,
                                                        saved_turns,
                                                        monkeypatch):
    """The registration is best-effort: a serving route must answer even if the
    registry is broken. The classifiers still refuse the save-back."""
    def _boom(_message):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(api_server, "_private_turn_key", _boom)
    body = _stream(client, PROTECTED)
    assert VAULT_SENTENCE in body, "the turn still answers"
    assert _terminal(body)["private"] is True
    assert saved_turns == []


# ══════════════════════════════════════════════════════════════════════════
#  6 - defense in depth: refused with NO registry entry at all
# ══════════════════════════════════════════════════════════════════════════

def test_a_pin_shaped_save_back_is_refused_with_an_empty_registry(
        client, vault_state, vault, saved_turns, fresh_registry):
    """Restart between the stream and the save-back, an evicted entry, or a
    client posting a turn that was never served: the PIN must not land."""
    assert fresh_registry == {}
    _ok_shape(_save_back(client, PIN, VAULT_SENTENCE))
    assert saved_turns == []
    assert PIN not in _persisted_text(saved_turns)


def test_a_protected_question_save_back_is_refused_with_an_empty_registry(
        client, vault_state, vault, saved_turns, fresh_registry):
    assert fresh_registry == {}
    _ok_shape(_save_back(client, PROTECTED, VAULT_SENTENCE))
    assert saved_turns == []


def test_a_vaulted_document_request_save_back_is_refused(client, vault_state,
                                                         vault, saved_turns):
    """"Pull up the passport" names a vaulted document TYPE without naming an
    owner, so the protected-topic test misses it; classify_source does not."""
    _ok_shape(_save_back(client, "pull up the passport",
                         "Here is the passport."))
    assert saved_turns == []


def test_a_save_back_during_a_pending_challenge_is_refused(client, vault_state,
                                                           vault, saved_turns):
    """While a challenge is armed, the next thing typed may BE the secret."""
    vault_state[f"device:{DEVICE}"] = {"pending_query": PROTECTED,
                                       "pending_since": time.time()}
    _ok_shape(_save_back(client, "24 68 02", "Unlocked."))
    assert saved_turns == []


def test_the_defense_does_not_swallow_ordinary_traffic(client, vault_state,
                                                       vault, saved_turns):
    """The other half: refusing everything would be data loss, not privacy."""
    for message in (ORDINARY, "show me my calendar", "what is on tonight",
                    "put the dashboard up", REGISTRY_ONLY):
        saved_turns.clear()
        _ok_shape(_save_back(client, message, "Here you go."))
        assert len(saved_turns) == 1, message
