"""The turn-scoped thread-locals must not survive the request that armed them.

WHAT THIS CLOSES
----------------
``turn_writes`` and ``turn_health`` each ship a ``begin_turn`` / ``end_turn``
pair. ``begin_turn`` is called from ``_prepare_user_message`` (the four typed
surfaces) and from the voice route. Without an ``end_turn`` in the request path,
a finished turn stays armed on a reused Flask worker thread: ``active=True``,
the compiled health pattern still loaded, ``kept=True`` still answering yes.

The dangerous direction is ``health_was_stored``. Any persistence path that
runs WITHOUT a preceding ``begin_turn`` inherits the previous turn's "yes, this
turn kept a health term", and ``_pii_types_not_stored_this_turn`` then drops
``health.condition`` from the honesty notice — PuffPAI staying silent about
something it really did discard. ``/chat/save`` is the reachable path with no
``begin_turn`` of its own.

The guard is one ``before_request`` / ``teardown_request`` pair
(``puffpai.security.turn_scope``), registered ahead of the rate limiter and the
API-key guard so no handler and no short-circuited 401/429 can observe a stale
record, and firing in teardown even when the handler raised.

Fake data only, per the standing rules. No household names anywhere.
"""

import json
import os
import sys

import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from puffpai import app_data as app_data_mod  # noqa: E402
from puffpai.security import turn_health, turn_scope, turn_writes  # noqa: E402

#: The sentence a turn that kept nothing must still carry.
NOT_KEPT = "did not keep it"

#: Structurally valid, not a published fake, so the quality gate really vaults
#: it on the flag-ON path. Same constant as tests/test_scrub_and_vault.py.
GOOD_SSN = "123-45-6788"

#: A medication name that is obviously not a real drug. The med fastpath stores
#: whatever the household says; nothing here depends on it being a real one.
FAKE_MED = "testfakemed"

MODEL_REPLY = "Medication added."

#: A condition the free-text health detector recognises behind a disclosure cue
#: ("I have..."), so ``split_for_persistence`` arms the turn record with it.
#: Generic, not a household detail.
HEALTH_CONDITION = "asthma"

#: The message that reaches the MODEL STUB. Verified below by
#: ``test_the_streaming_probe_message_really_takes_the_streaming_branch``:
#: ``_detect_route`` says ``rag`` and ``_try_med_fastpath`` says ``None``, which
#: is the only combination that puts ``/chat/stream`` on its REAL streaming
#: branch — the one whose persistence runs inside the SSE generator, after
#: ``teardown_request``. ``add <med> to my meds`` does NOT: the med fastpath
#: fires, the turn is served by ``generate_fallback``, and that branch persists
#: from inside the view while the turn record is still open. A test built on it
#: exercises the live record, never the capture, and passes with the capture
#: deleted.
STREAMING_HEALTH_MESSAGE = f"I have {HEALTH_CONDITION} what should I know"

#: What the stubbed model streams back. It echoes the condition with NO
#: disclosure cue, so the free-text pass inside
#: ``_scrub_assistant_for_persistence`` does not fire on it and the captured
#: turn redactor is the ONLY thing that can remove the word — which is what
#: makes the negative control below meaningful.
HEALTH_REPLY = ("Asthma is usually managed with a daily controller inhaler "
                "and a rescue inhaler kept nearby.")


def _clear_every_app_data_dir_cache(monkeypatch):
    """Reset the resolved app-data dir on EVERY copy of the module.

    ``assistant_tools`` does ``from app_data import AppData`` — the TOP-LEVEL
    module, a different object from ``puffpai.app_data`` with its own
    ``_data_dir_cache``. Clearing only the package one leaves every ``_try_*``
    fastpath write pointed at an earlier test's tmp dir. Do not delete either
    branch.
    """
    for name, mod in list(sys.modules.items()):
        if name in ("app_data", "puffpai.app_data") and mod is not None:
            monkeypatch.setattr(mod, "_data_dir_cache", None, raising=False)


@pytest.fixture(autouse=True)
def _no_turn_leaks_out_of_this_file():
    """Both records are thread-local and pytest runs on one thread."""
    turn_scope.reset_all()
    yield
    turn_scope.reset_all()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A real Flask test client on a throwaway data dir, flag ON.

    Flag ON is the live appliance's state (``PUFFPAI_SCRUB_AND_VAULT=1`` in
    ``/etc/puffpai/api.env``) and the only one where the health record is armed
    at all. The go-live interlock needs BOTH the flag and an enrolled
    PIN-recovery code, so one is minted here and the assert proves the branch
    under test is really the flag-ON one.

    The test client issues every request on the CALLING thread, which is exactly
    the reused-worker-thread situation this file is about: turn N and turn N+1
    share a thread, so a record left armed by N is visible to N+1.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    _clear_every_app_data_dir_cache(monkeypatch)

    import puffpai.api_server as api_server
    from puffpai.security import scrub_and_vault as sav
    monkeypatch.setattr(api_server, "_app_data_instance", None)
    monkeypatch.setattr(api_server, "_assistant_tools_instance", None)
    sav.invalidate_recovery_code_cache()

    from puffpai.security.vault import Vault
    monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", "1")
    vault = Vault(vault_dir=str(tmp_path / "vault"))
    vault.initialize("vault-pin-1234")
    session = vault.authenticate("vault-pin-1234")
    vault.generate_recovery_code(session.session_id)
    monkeypatch.setattr(api_server, "_get_vault", lambda: vault)
    assert sav.scrub_and_vault_active(vault=vault) is True

    # Every model sink stubbed: these tests must never reach a backend.
    monkeypatch.setattr(api_server, "_query_ollama_direct",
                        lambda *a, **k: MODEL_REPLY)
    monkeypatch.setattr(api_server, "_stream_ollama_tokens",
                        lambda *a, **k: iter([MODEL_REPLY]))
    monkeypatch.setattr(api_server, "_process_chat_query",
                        lambda m, h=None, **k: {
                            'response': api_server._try_instant_answer(m)
                                        or MODEL_REPLY,
                            'sources': [], 'suggestions': []})
    monkeypatch.setattr(api_server, "_process_voice_query",
                        lambda m, h=None, **k:
                        api_server._try_instant_answer(m) or MODEL_REPLY)
    api_server._get_assistant_tools()

    history = list(api_server._chat_history)
    api_server._chat_history.clear()
    client = api_server.app.test_client()
    headers = {"X-API-Key": api_server._API_KEY}
    yield api_server, client, headers, tmp_path
    api_server._chat_history.clear()
    api_server._chat_history.extend(history)
    sav.invalidate_recovery_code_cache()


def _send(client, headers, message):
    r = client.post("/chat/send", json={"message": message}, headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return (r.get_json() or {}).get("response", "")


def _persisted(api_server):
    out = []
    for conv in api_server._chat_history:
        for msg in conv.get("messages", []):
            out.append((msg["role"], msg["content"]))
    return out


def _meds(tmp_path):
    path = tmp_path / "state" / "app_data" / "calendar.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [m.get("name") for m in doc.get("meds", [])]


def _stub_the_streaming_model(api_server, monkeypatch):
    """Make the token stream carry the health word, in more than one chunk.

    The ``wired`` fixture's default stream stub answers ``MODEL_REPLY``, which
    contains no health term at all — nothing for the redactor to remove, so it
    cannot show whether the redactor ran.
    """
    def _tokens(*_a, **_k):
        mid = len(HEALTH_REPLY) // 2
        return iter([HEALTH_REPLY[:mid], HEALTH_REPLY[mid:]])

    monkeypatch.setattr(api_server, "_stream_ollama_tokens", _tokens)


def _spy_on_capture_redactor(monkeypatch, replacement=None):
    """Count calls to ``turn_health.capture_redactor`` and return the counter.

    ``chat_stream`` does ``from puffpai.security import turn_health as _th``
    INSIDE the view, so the name is resolved off the module object at call
    time and patching the module attribute is seen by the code under test.

    ``replacement`` swaps what the capture returns while still counting the
    call; ``lambda: None`` reproduces the PRE-FIX code path exactly, because
    ``_scrub_assistant_for_persistence`` then falls back to reading the live
    ``turn_health`` record — which teardown has already closed by the time the
    generator runs.
    """
    calls = []
    real = turn_health.capture_redactor

    def _counting_capture():
        calls.append(True)
        return real() if replacement is None else replacement()

    monkeypatch.setattr(turn_health, "capture_redactor", _counting_capture)
    return calls


def _stream_and_hold(client, headers, message):
    """POST to ``/chat/stream`` WITHOUT consuming the SSE body.

    Flask pops the request context — and with it runs ``teardown_request`` —
    as soon as the view has returned its ``Response``; a streaming response's
    iterable is not touched until something reads it. ``buffered=False`` keeps
    the werkzeug test client lazy, so the caller decides when the generator
    runs. That ordering IS the thing under test.
    """
    return client.post("/chat/stream", headers=headers,
                       json={"message": message}, buffered=False)


# ══════════════════════════════════════════════════════════════════════════
# 1 - the record does not outlive the request that armed it
# ══════════════════════════════════════════════════════════════════════════

def test_a_med_turn_leaves_no_armed_record_behind(wired):
    """The defect itself: this request must not return with ``active=True``
    and ``kept=True`` still set on the serving thread."""
    api_server, client, headers, tmp_path = wired
    reply = _send(client, headers, f"add {FAKE_MED} to my meds")

    assert _meds(tmp_path) == [FAKE_MED], _meds(tmp_path)
    assert NOT_KEPT not in reply, reply
    #...and the thread is clean the instant the request is over.
    assert turn_health.turn_active() is False
    assert turn_health.health_was_stored() is False
    assert turn_writes.turn_active() is False
    assert turn_writes.stored_classes() == frozenset()


def test_the_second_turn_on_the_same_thread_gets_its_own_true_notice(wired):
    """Two sequential requests, one thread, first one stores a medication.

    The second turn keeps nothing, so its notice must be present AND true —
    ``calendar`` must show it wrote nothing.
    """
    api_server, client, headers, tmp_path = wired

    first = _send(client, headers, f"add {FAKE_MED} to my meds")
    assert _meds(tmp_path) == [FAKE_MED]
    assert NOT_KEPT not in first, first

    second = _send(client, headers, "I have asthma")
    # Nothing new was written: the notice below is a true statement.
    assert _meds(tmp_path) == [FAKE_MED], _meds(tmp_path)
    assert NOT_KEPT in second, second
    assert MODEL_REPLY in second, second


def test_a_turn_that_stores_nothing_still_leaves_a_clean_thread(wired):
    api_server, client, headers, tmp_path = wired
    _send(client, headers, "I have asthma")
    assert turn_health.turn_active() is False
    assert turn_writes.turn_active() is False


# ══════════════════════════════════════════════════════════════════════════
# 2 - /chat/save runs with a fresh, empty turn
# ══════════════════════════════════════════════════════════════════════════

def test_chat_save_never_sees_a_previous_turns_record(wired):
    """``/chat/save`` calls no ``begin_turn`` of its own, so without the
    teardown it would inherit whatever the last chat turn on this thread left
    armed, including a ``kept=True`` that can suppress an honesty notice.

    The probe is registered AFTER ``turn_scope``'s hook, and Flask runs
    ``before_request`` functions in registration order, so what it records is
    the state the route handler is about to see.
    """
    api_server, client, headers, tmp_path = wired
    seen = []

    def _probe():
        seen.append((turn_health.turn_active(),
                     turn_health.health_was_stored(),
                     turn_writes.turn_active(),
                     set(turn_writes.stored_classes())))
        return None

    _send(client, headers, f"add {FAKE_MED} to my meds")
    # Re-arm by hand as well, so the test states the guarantee rather than
    # relying on the previous request having left something behind.
    turn_writes.begin_turn()
    turn_health.begin_turn()
    turn_health.arm([FAKE_MED])
    turn_health.note_write("calendar", {"meds": [{"name": FAKE_MED}]})
    assert turn_health.health_was_stored() is True

    api_server.app.before_request_funcs.setdefault(None, []).append(_probe)
    try:
        r = client.post("/chat/save", headers=headers, json={
            "user_message": "what time is the school concert",
            "assistant_message": "Seven, in the main hall.",
        })
    finally:
        api_server.app.before_request_funcs[None].remove(_probe)

    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert seen, "probe never ran - hook registration changed"
    active_h, kept, active_w, classes = seen[0]
    assert kept is False, "a stale 'stored' flag reached /chat/save"
    assert active_h is False and active_w is False
    assert classes == set()


def test_chat_save_scrubs_the_user_half(wired):
    """``/chat/save`` scrubs the user half, not only the assistant half.

    Appending ``user_message`` raw would let a client that streamed a turn and
    saved it back write the household's SSN into the transcript in plaintext.

    The probe does not say "my ssn is...": ``/chat/save`` REFUSES a save-back
    whose user half is a protected-topic question outright, so that wording is
    not scrubbed-and-stored, it is not stored at all, the strictly stronger
    outcome, pinned by the test below. What is pinned HERE is the scrub itself,
    on a message the privacy gate passes through.
    """
    api_server, client, headers, tmp_path = wired
    _send(client, headers, f"add {FAKE_MED} to my meds")

    r = client.post("/chat/save", headers=headers, json={
        "user_message": f"the reference is {GOOD_SSN} and I take {FAKE_MED}",
        "assistant_message": "Noted.",
    })
    assert r.status_code == 200, r.get_data(as_text=True)[:200]

    users = [c for role, c in _persisted(api_server) if role == "user"]
    saved = users[-1]
    assert "reference" in saved, "the save-back was refused, not scrubbed"
    assert GOOD_SSN not in saved, saved
    assert FAKE_MED.lower() not in saved.lower(), saved


def test_chat_save_refuses_a_protected_topic_save_back(wired):
    """The client cannot put back what the serving route refused to keep.

    ``/chat/save`` is the one persistence path whose CONTENT is chosen by the
    browser, so it applies the standing vault classifiers to the posted user
    half and writes nothing when they fire. Full coverage of the rule (the
    private-turn registry, the PIN case, the SSE ``private`` contract) lives in
    tests/test_vault_save_back_private.py; this pins the interaction with the
    scrub path in this file.
    """
    api_server, client, headers, tmp_path = wired
    _send(client, headers, f"add {FAKE_MED} to my meds")
    before = len(_persisted(api_server))

    r = client.post("/chat/save", headers=headers, json={
        "user_message": f"my ssn is {GOOD_SSN} and I take {FAKE_MED}",
        "assistant_message": "Noted.",
    })
    # The refusal is indistinguishable from a write, so the client neither
    # retries nor learns which of its turns the appliance found interesting.
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert (r.get_json() or {}).get("success") is True

    assert len(_persisted(api_server)) == before, "the pair reached the store"
    blob = json.dumps(_persisted(api_server))
    assert GOOD_SSN not in blob and "Noted." not in blob
    # The whole transcript, both halves, on both turns.
    everything = " ".join(c for _role, c in _persisted(api_server))
    assert GOOD_SSN not in everything, everything


# ══════════════════════════════════════════════════════════════════════════
# 3 - teardown fires on the paths that do not return normally
# ══════════════════════════════════════════════════════════════════════════

def _app_with_turn_scope():
    """A throwaway Flask app carrying only the hooks under test."""
    app = Flask(__name__)
    turn_scope.install(app)

    @app.route("/arm-then-raise", methods=["POST"])
    def _arm_then_raise():
        turn_writes.begin_turn()
        turn_health.begin_turn()
        turn_health.arm([FAKE_MED])
        turn_health.note_write("calendar", {"meds": [{"name": FAKE_MED}]})
        raise RuntimeError("handler blew up mid-turn")

    @app.route("/arm-then-return", methods=["POST"])
    def _arm_then_return():
        turn_writes.begin_turn()
        turn_health.begin_turn()
        turn_health.arm([FAKE_MED])
        return "ok"

    return app


def test_teardown_clears_the_record_when_the_handler_raises():
    app = _app_with_turn_scope()
    client = app.test_client()
    r = client.post("/arm-then-raise")
    assert r.status_code == 500
    assert turn_health.turn_active() is False
    assert turn_health.health_was_stored() is False
    assert turn_writes.turn_active() is False


def test_teardown_clears_the_record_on_a_normal_return():
    app = _app_with_turn_scope()
    client = app.test_client()
    assert client.post("/arm-then-return").status_code == 200
    assert turn_health.turn_active() is False
    assert turn_writes.turn_active() is False


def test_before_request_clears_a_record_left_by_something_else():
    """Entry reset, not just exit: a record armed outside any request (a
    background writer, a crashed process reusing the thread) must not be
    visible to the handler."""
    app = _app_with_turn_scope()
    seen = []

    @app.route("/observe")
    def _observe():
        seen.append((turn_health.turn_active(),
                     turn_health.health_was_stored(),
                     turn_writes.turn_active()))
        return "ok"

    turn_writes.begin_turn()
    turn_health.begin_turn()
    turn_health.arm([FAKE_MED])
    turn_health.note_write("calendar", {"meds": [{"name": FAKE_MED}]})
    assert turn_health.health_was_stored() is True

    assert app.test_client().get("/observe").status_code == 200
    assert seen == [(False, False, False)], seen


def test_the_reset_hook_is_registered_before_the_rate_limiter():
    """Ordering is the whole guarantee for a request that never reaches a
    handler: Flask runs ``before_request`` functions in registration order and
    stops at the first one that returns a response, so a 429 or a 401 would
    skip a reset registered behind them."""
    import puffpai.api_server as api_server
    names = [getattr(f, "__name__", "")
             for f in api_server.app.before_request_funcs.get(None, [])]
    assert names[0] == "_before_request", names
    assert "_rate_limit" in names and "_check_api_key" in names
    assert names.index("_before_request") < names.index("_rate_limit")
    assert names.index("_rate_limit") < names.index("_check_api_key")


# ══════════════════════════════════════════════════════════════════════════
# 4 - the streaming surface keeps its persistence scrub after teardown
# ══════════════════════════════════════════════════════════════════════════

def test_the_streaming_probe_message_really_takes_the_streaming_branch(wired):
    """Guard on the guard.

    Everything below depends on ``STREAMING_HEALTH_MESSAGE`` reaching the model
    stub. ``/chat/stream`` only does that when the route is ``rag`` AND the
    medication fastpath declines; either one flipping silently reroutes the
    streaming tests onto ``generate_fallback``, which persists from inside the
    VIEW while the turn record is still open, and they would go on passing
    while testing nothing about the capture. So assert the precondition
    directly rather than inferring it.
    """
    api_server, _client, _headers, _tmp_path = wired
    assert api_server._detect_route(STREAMING_HEALTH_MESSAGE) == "rag"
    assert api_server._try_med_fastpath(STREAMING_HEALTH_MESSAGE) is None
    #...and the obvious "add <med>" phrasing does NOT: it is served by the
    # fallback branch, which is why it can never exercise the capture.
    assert api_server._try_med_fastpath(f"add {FAKE_MED} to my meds") is not None


def test_chat_stream_still_redacts_the_stored_reply_after_teardown(
        wired, monkeypatch):
    """``/chat/stream`` persists from INSIDE the SSE generator, which Flask
    consumes after the view returned — i.e. after ``teardown_request`` closed
    the record. ``turn_health.capture_redactor`` is taken on the request
    thread so the stored transcript keeps the treatment it had before.

    Sending ``add <med> to my meds`` would not test this: the medication
    fastpath claims it, so the turn never leaves the in-view
    ``generate_fallback`` branch, ``capture_redactor`` is called ZERO times and
    the test passes with the capture deleted, a false green. This test sends a
    message that reaches the model stub, holds the SSE body until AFTER
    teardown, and spies on the capture so "the capture ran" is asserted rather
    than assumed.

    Scope note: this asserts the ASSISTANT half, which is what the captured
    redactor governs. The USER half is produced by ``_prepare_user_message``
    inside the view, before any teardown, and the capture does not touch it;
    see ``test_the_user_half_of_a_streamed_turn_is_unchanged``.
    """
    api_server, client, headers, tmp_path = wired
    _stub_the_streaming_model(api_server, monkeypatch)
    calls = _spy_on_capture_redactor(monkeypatch)

    r = _stream_and_hold(client, headers, STREAMING_HEALTH_MESSAGE)

    # The view has returned, so teardown has already closed the record...
    assert turn_health.turn_active() is False
    assert turn_writes.turn_active() is False
    #...and the generator has not run yet, so nothing is persisted. If this
    # ever fails, the response stopped being lazy and the ordering this whole
    # test is about is no longer being exercised.
    assert not _persisted(api_server), _persisted(api_server)

    body = r.get_data(as_text=True)  # <-- the SSE generator runs HERE

    assert calls, "capture_redactor was never called - the fix did not run"
    # The household still reads the condition by name in the live reply.
    assert HEALTH_CONDITION in body.lower(), body[:300]
    # Conversation memory does not keep it on the assistant side.
    assistants = [c for role, c in _persisted(api_server) if role == "assistant"]
    assert assistants, "the streaming turn persisted nothing at all"
    stored = " ".join(assistants)
    assert HEALTH_CONDITION not in stored.lower(), stored
    assert "[REDACTED-HEALTH]" in stored, stored


def test_the_stored_stream_reply_is_raw_without_the_captured_redactor(
        wired, monkeypatch):
    """The negative control for the test above: behaviour without the capture.

    ``capture_redactor`` is made to hand back nothing, so
    ``_scrub_assistant_for_persistence`` takes its ``turn_redactor is None``
    branch and calls ``turn_health.redact_turn_terms`` against the LIVE record —
    which ``turn_scope``'s ``teardown_request`` has closed by the time the
    generator runs. The condition therefore lands in conversation memory in the clear.

    Two things follow, and neither is provable from the passing test alone:
    the capture is load-bearing (delete it and the assertion above fails), and
    the record really is closed before the generator runs (otherwise the live
    read would still have redacted).
    """
    api_server, client, headers, tmp_path = wired
    _stub_the_streaming_model(api_server, monkeypatch)
    calls = _spy_on_capture_redactor(monkeypatch, replacement=lambda: None)

    r = _stream_and_hold(client, headers, STREAMING_HEALTH_MESSAGE)
    r.get_data(as_text=True)

    assert calls, "capture_redactor was never called - wrong branch again"
    assistants = [c for role, c in _persisted(api_server) if role == "assistant"]
    assert assistants, "the streaming turn persisted nothing at all"
    stored = " ".join(assistants)
    assert HEALTH_CONDITION in stored.lower(), stored


def test_the_streaming_and_nonstreaming_surfaces_store_the_same_reply(
        wired, monkeypatch):
    """Parity. The captured redactor must give ``/chat/stream`` exactly what
    the live record gives ``/chat/send``; a divergence between the two is how a
    teardown-shaped regression would surface.

    A fastpath phrasing would put BOTH surfaces on a branch that reads the
    live record, so the comparison would never involve the capture. Both
    surfaces are given the identical reply text and the streaming half really
    streams.
    """
    api_server, client, headers, tmp_path = wired
    _stub_the_streaming_model(api_server, monkeypatch)
    # /chat/send's sink, answering the same words the stream stub emits, so any
    # difference in the stored strings is the persistence scrub and nothing
    # else.
    monkeypatch.setattr(api_server, "_process_chat_query",
                        lambda m, h=None, **k: {'response': HEALTH_REPLY,
                                                'sources': [],
                                                'suggestions': []})
    calls = _spy_on_capture_redactor(monkeypatch)

    _send(client, headers, STREAMING_HEALTH_MESSAGE)
    sent = [c for role, c in _persisted(api_server) if role == "assistant"][-1]
    api_server._chat_history.clear()

    r = _stream_and_hold(client, headers, STREAMING_HEALTH_MESSAGE)
    assert not _persisted(api_server), _persisted(api_server)
    r.get_data(as_text=True)
    streamed = [c for role, c in _persisted(api_server)
                if role == "assistant"][-1]

    # /chat/send reads the live record and never captures; the one call is the
    # streaming half's.
    assert len(calls) >= 1, "capture_redactor was never called"
    assert "[REDACTED-HEALTH]" in sent, sent
    assert HEALTH_CONDITION not in sent.lower(), sent
    assert streamed == sent, (streamed, sent)


def test_the_user_half_of_a_streamed_turn_is_unchanged(wired):
    """Records a known boundary that the turn lifecycle does not move, so a
    reader does not mistake it for something the lifecycle causes.

    ``_prepare_user_message`` scrubs the user half with the free-text health
    detector only. That detector redacts the disclosure phrase ("my meds") but
    does not recognise an arbitrary token as a drug name, so an unrecognised
    medication name persists on the USER side while the ASSISTANT side — which
    additionally screens against the stored medication list — does not. Closing
    that asymmetry means running ``redact_known_medications`` over the user
    half too; it is a detector-coverage change, not a turn-lifecycle one.
    """
    api_server, client, headers, tmp_path = wired
    client.post("/chat/stream", headers=headers,
                json={"message": f"add {FAKE_MED} to my meds"})

    users = [c for role, c in _persisted(api_server) if role == "user"]
    assert users, "the streaming turn persisted no user half"
    # The disclosure phrase IS removed - the detector fired, as designed.
    assert "[REDACTED-HEALTH]" in users[-1], users[-1]


def test_capture_redactor_is_identity_outside_a_turn():
    turn_scope.reset_all()
    redact = turn_health.capture_redactor()
    assert redact(f"{FAKE_MED} at eight") == f"{FAKE_MED} at eight"


def test_capture_redactor_keeps_working_after_the_turn_closed():
    turn_health.begin_turn()
    turn_health.arm([FAKE_MED])
    redact = turn_health.capture_redactor()
    turn_scope.reset_all()
    assert turn_health.redact_turn_terms(f"{FAKE_MED} at eight") == \
        f"{FAKE_MED} at eight"
    assert redact(f"{FAKE_MED} at eight") == "[REDACTED-HEALTH] at eight"


def test_reset_all_is_silent_when_a_record_module_cannot_be_cleared(monkeypatch):
    """One failing record may not leave a sibling armed, and nothing may raise
    out of a teardown hook."""
    turn_writes.begin_turn()
    turn_health.begin_turn()
    turn_health.arm([FAKE_MED])

    def _boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(turn_writes, "end_turn", _boom)
    turn_scope.reset_all()
    assert turn_health.turn_active() is False
