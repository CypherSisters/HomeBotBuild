"""A PIN must not reach the journal, whatever words carried it there.

THE HOLE THIS CLOSES
--------------------
Two halves of one decision had drifted apart.

``_live_turn_should_seal`` decides, before the wall panel's buffer opens,
that a turn publishes nothing. ``_log_safe`` decides what a route's log line
may print. They are the same question asked by two surfaces -- and ``_log_safe``
was consulting exactly ONE of ``_live_turn_should_seal``'s five conditions.
So a turn the wall refused to show was written out in full to journald and to
the rotating ``puffpai.log``, which is durable:

  * ``4821`` -- a bare PIN answering an armed vault challenge. This is the
    LIVE path: the protected question arms ``pending_query``, the reply is
    printed by the route's log line BEFORE the vault gate ever sees it.
  * ``passport | 4821``, ``birth certificate | 4821`` -- sealed by the
    document classifier, printed by the log.

And a second class that neither half caught, because the unlock tool's own
``label | PIN`` format puts an ordinary English word on the label side:

  * ``wifi | 4821``, ``bank account | 4821``, ``garage code | 4821`` --
    a PIN on the wall panel AND in the journal.
  * word order: ``the password is 4821`` sealed, ``the password it is 4821``
    did not, and neither did "the password for the router, it's 4821".

WHAT IS ASSERTED
  1. AGREEMENT, as a property over a corpus: for every phrasing, if
     ``_live_turn_should_seal`` is true then ``_log_safe`` prints no part of
     the number -- and the phrasings that must stay readable still are.
  2. The LIVE path: an armed challenge, then "4821", on each of the four
     routes that log user text. The PIN appears in NO captured output.
  3. STRUCTURAL: an utterance that dodges every pattern ("get wifi 4821")
     running the REAL vault tool through the REAL boundary. The early log line
     no longer carries text at all, so there is nothing for a pattern to miss.
  4. An ordinary turn still logs its route AND its text, exactly once.
  5. A turn that raises logs no user text.

No network, no real vault, no model.

NOT PROVEN HERE: nothing in this file runs on real hardware, and the fake
agent invokes the vault tool on the REQUEST thread. The one cross-thread
assertion below runs the boundary call on a genuine pool thread through
``contextvars.copy_context().run`` -- which is how LangChain's
``run_in_executor`` dispatches a langgraph ToolNode body -- and that is the
only threading claim made.
"""

import contextvars
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from puffpai import api_server
from puffpai import dashboard_display as dd
from puffpai import assistant_tools as rt

DEVICE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PIN = "4821"
SECRET = "sw0rdf1sh-XYZ"
LABEL = "wifi password"
#: The utterance that dodges every pattern: no vault word, no secret noun,
#: no separator, no cue. Only the tool boundary knows what this turn was.
DODGES_EVERY_PATTERN = "get wifi 4821"
PROTECTED_ASK = "what are my allergies"
ORDINARY = "what is the capital of France"
ORDINARY_REPLY = "Paris is the capital."


# ── the corpus ──────────────────────────────────────────────────────────────
#
# Every phrasing a review found printed in full, plus the shapes around
# them. A phrase is in SEALED because a PIN can be read out of it.
SEALED = (
    # sealed by the panel, printed by the journal
    "4821",
    "4 8 2 1",
    "four eight two one",
    "birth certificate | 4821",
    "passport | 4821",
    "social security card | 4821",
    "show me my passport 4821",
    # the tool's own format with an everyday label
    "wifi | 4821",
    "bank account | 4821",
    "garage code | 4821",
    "safe | 4821",
    "safe|four eight two one",
    "vault | 4 8 2 1",
    # word order
    "the password is 4821",
    "the password it is 4821",
    "the password for the router, it's 4821",
    "the password for the router, its 4821",
    "4821 is my password",
    "password: 4821",
    "wifi password = 4821",
    # already sealed before this change; they must stay sealed
    "router password | 4821",
    "router password|4821",
    "wifi password 4821",
    "read router password 4821",
    "show wifi password 4821 now",
    "wifi password please, 4 8 2 1",
    "my pin is 4821",
    "unlock the vault 4821",
    "passcode 4821",
)

#: The other half of the bargain: a log an operator can still read.
OPEN = (
    "what's the password policy for 2024",
    "password managers, are they safe in 2026",
    "remind me to change the password in 30 days",
    "is the password policy from 2024 still valid",
    "tell me about password hygiene",
    "my password manager is asking for an update",
    "is my login page down",
    "login page for my 401k",
    "what is the area code for 512",
    "zip code 78701",
    "set a timer for 10 minutes",
    "what's 1234 plus 4321",
    "call the 2024 budget",
    "play the 1984 album",
    "what year was 1984 published",
    "search for 500 mile hikes",
    "set the thermostat to 68",
    "unlock the front door",
    "play jazz",
    "turn on the kitchen lights",
    "turn the living room lamp off",
    "add milk to the shopping list",
    "how many days until christmas",
    "what's the weather tomorrow",
    "remind me at 7 to call mum",
    "read me the news",
    "how do I reset the router",
    "what's on my calendar for the 12th",
    "what is the capital of France",
    "how do I reset the wifi password",
)

_DIGIT_RUN = re.compile(r"\d{3,}")


# ── fakes ───────────────────────────────────────────────────────────────────

class _FakeSession:
    session_id = "vault-session-xyz"


class _FakeAudit:
    def log(self, **kwargs):
        pass


class _FakeVault:
    is_initialized = True

    def __init__(self, vault_dir):
        self._vault_dir = vault_dir
        self.vault_dir = vault_dir
        self._audit = _FakeAudit()

    def authenticate(self, pin):
        if pin != PIN:
            from puffpai.security.vault import AuthenticationError
            raise AuthenticationError("bad pin")
        return _FakeSession()

    def read_secure_session(self, filename, session_id):
        return {"value": SECRET}

    def migrate_plaintext_files(self, session_id):
        return None

    def logout(self, session_id):
        pass


@pytest.fixture
def vault(tmp_path, monkeypatch):
    d = tmp_path / "vault_data"
    d.mkdir()
    (d / "wifi_password.json").write_text("{}", encoding="utf-8")
    # The gate only challenges when there is something to gate on: it counts
    # doc_* entries without decrypting them.
    (d / "doc_allergies.json").write_text("{}", encoding="utf-8")
    v = _FakeVault(d)
    monkeypatch.setattr(api_server, "_get_vault", lambda: v)
    return v


@pytest.fixture(scope="module")
def unlock_tool():
    """The REAL tool object, built once (AssistantTools is expensive)."""
    return rt.AssistantTools().tool_dict["vault_unlock_and_get"]


@pytest.fixture(autouse=True)
def fresh_rate_limit(unlock_tool):
    """The tool's own 5-per-300s lockout is a closure shared by the module."""
    seen, queue = set(), [getattr(unlock_tool, "func", None)]
    while queue:
        obj = queue.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        for cell in (getattr(obj, "__closure__", None) or ()):
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if (isinstance(val, dict) and "lock_until" in val
                    and "attempts" in val):
                val["attempts"].clear()
                val["lock_until"] = 0.0
            elif callable(val):
                queue.append(val)
    yield


@pytest.fixture(autouse=True)
def fresh_turn_state():
    api_server._reset_vault_turn_privacy()
    api_server._live_turn_clear()
    api_server._VAULT_CHAT_STATE.clear()
    dd.clear_pending_vault()
    saved = dict(api_server._private_turns)
    api_server._private_turns.clear()
    yield
    api_server._reset_vault_turn_privacy()
    api_server._live_turn_clear()
    api_server._VAULT_CHAT_STATE.clear()
    dd.clear_pending_vault()
    api_server._private_turns.clear()
    api_server._private_turns.update(saved)


class _FakeVaultAgent:
    """The model + agent loop, reduced to the one thing that matters here:
    it announces the tool call at the shared boundary and invokes the REAL
    ``vault_unlock_and_get``."""

    def __init__(self, tool):
        self._tool = tool

    def run(self, query, conversation_history=None, **kwargs):
        rt.note_vault_tool_calls(["vault_unlock_and_get"])
        return self._tool.invoke(LABEL + " | " + PIN)

    def executed_tool_names(self):
        return ["vault_unlock_and_get"]

    def daily_briefing(self, *a, **k):          # pragma: no cover
        return ""


class _PlainAgent:
    def run(self, query, conversation_history=None, **kwargs):
        return ORDINARY_REPLY

    def executed_tool_names(self):
        return ["auto_weather_forecast"]

    def daily_briefing(self, *a, **k):          # pragma: no cover
        return ""


@pytest.fixture
def agent_route(monkeypatch):
    """Force both pipelines onto the plain agent route, stub its neighbours."""
    monkeypatch.setattr(api_server, "_detect_route", lambda *a, **k: "agent")
    for name in ("_try_compound_request", "_try_instant_answer",
                 "_try_knowledge_search", "_try_team_chat",
                 "_try_direct_handle", "_try_composed_plan",
                 "_try_capability_gap", "_try_med_fastpath",
                 "_pii_deterministic_reply"):
        monkeypatch.setattr(api_server, name, lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_verify_and_fix",
                        lambda _q, response, *a, **k: response)
    monkeypatch.setattr(api_server, "_snapshot_counts", lambda *a, **k: {})
    monkeypatch.setattr(api_server, "_get_rag_context", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_apply_pii_honesty",
                        lambda text, *a, **k: text)
    monkeypatch.setattr(api_server, "_record_voice_turn",
                        lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_save_conversation", lambda conv: None)


@pytest.fixture
def client(monkeypatch):
    api_server.app.config.update(TESTING=True)
    api_server._rate_limit_store.clear()
    with api_server.app.test_client() as c:
        yield c


def _headers():
    return {"X-API-Key": api_server._API_KEY, "X-Puff-Device-Id": DEVICE}


def _post(client, route, text):
    if route == "/chat/send":
        return client.post(route, headers=_headers(),
                           json={"message": text,
                                 "conversation_id": "conv-log"})
    if route == "/chat/stream":
        return client.post(route, headers=_headers(),
                           json={"message": text,
                                 "conversation_id": "conv-log"})
    return client.post(route, headers=_headers(),
                       json={"messages": [{"role": "user", "content": text}]})


ROUTES = ("/chat/send", "/chat/stream", "/v1/chat/completions", "/api/chat")


def _captured(capsys, caplog):
    """Everything an operator would find written down for this turn."""
    return capsys.readouterr().out + "\n" + caplog.text


def _arm_the_challenge(client):
    """The real arming path: a protected question through the vault gate."""
    resp = client.post("/v1/chat/completions", headers=_headers(),
                       json={"messages": [{"role": "user",
                                           "content": PROTECTED_ASK}],
                             "stream": False})
    assert resp.status_code == 200
    return resp


# ═══════════════════════════════════════════════════════════════════════════
#  1. AGREEMENT — one source of truth
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("said", SEALED)
def test_a_sealed_phrasing_is_also_redacted(said):
    """The property: seal(text) implies the log prints none of the number."""
    assert api_server._live_turn_should_seal(said) is True, said
    out = api_server._log_safe(said)
    assert PIN not in out, out
    for run in _DIGIT_RUN.findall(said):
        assert run not in out, out
    # And nothing DERIVED from the text either: no length, no hash. A 4-digit
    # PIN is small enough that a hash of it is the PIN.
    assert out == "[redacted: private turn]", out


@pytest.mark.parametrize("said", OPEN)
def test_an_ordinary_phrasing_stays_readable(said):
    assert api_server._live_turn_should_seal(said) is False, said
    out = api_server._log_safe(said)
    assert "redacted: private turn" not in out, out


def test_seal_and_redact_agree_across_the_whole_corpus():
    """Stated once as the property itself, over both halves at once."""
    for said in SEALED + OPEN:
        sealed = api_server._live_turn_should_seal(said)
        redacted = api_server._log_safe(said) == "[redacted: private turn]"
        assert sealed == redacted, said


def test_the_new_predicates_cover_the_two_missed_classes():
    assert api_server._live_turn_pipe_pin("wifi | 4821") is True
    assert api_server._live_turn_pipe_pin("safe|four eight two one") is True
    assert api_server._live_turn_pipe_pin("shopping | milk") is False
    assert api_server._live_turn_secret_value_cue(
        "the password for the router, it's 4821") is True
    assert api_server._live_turn_secret_value_cue(
        "is the password policy from 2024 still valid") is False


def test_a_pending_challenge_redacts_every_line(vault):
    """_live_turn_secret_pending() is one of the seal conditions, so while a
    challenge is open EVERY line through _log_safe is redacted -- not only the
    PIN turn. Deliberate: the reply can arrive on any surface."""
    assert api_server._log_safe(ORDINARY).startswith("what is the capital")
    dd.set_pending_vault("my passport")
    try:
        assert api_server._log_safe(ORDINARY) == "[redacted: private turn]"
    finally:
        dd.clear_pending_vault()
    assert api_server._log_safe(ORDINARY).startswith("what is the capital")


def test_log_safe_fails_closed():
    """An unreadable seal decision redacts; it does not print."""
    import unittest.mock as mock
    with mock.patch.object(api_server, "_live_turn_should_seal",
                           side_effect=RuntimeError("boom")):
        assert api_server._log_safe(ORDINARY) == "[redacted]"


# ═══════════════════════════════════════════════════════════════════════════
#  2. THE LIVE PATH — an armed challenge, then a bare PIN
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("route", ROUTES)
def test_a_bare_pin_answering_a_challenge_is_never_written_down(
        route, client, vault, agent_route, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _PlainAgent())
    _arm_the_challenge(client)
    assert any(s.get("pending_query")
               for s in api_server._VAULT_CHAT_STATE.values()), \
        "the challenge must actually be armed, or this proves nothing"
    capsys.readouterr()
    caplog.clear()

    resp = _post(client, route, PIN)
    assert resp.status_code == 200
    resp.get_data()                      # consume any streamed body

    written = _captured(capsys, caplog)
    assert PIN not in written, written


def test_the_protected_question_itself_is_not_written_down(
        client, vault, capsys, caplog):
    """The challenge turn names what was asked for; it is a durable record
    that it was asked."""
    caplog.set_level(logging.DEBUG)
    _arm_the_challenge(client)
    written = _captured(capsys, caplog)
    assert PROTECTED_ASK not in written, written


# ═══════════════════════════════════════════════════════════════════════════
#  3. STRUCTURAL — the utterance that dodges every pattern
# ═══════════════════════════════════════════════════════════════════════════

def test_the_dodging_utterance_is_still_a_pattern_miss():
    """Honest about what layer A does NOT do: no vault word, no secret noun,
    no separator, no cue. Only the tool boundary knows. The wall panel's
    pre-open seal therefore still shows it for the ~700 ms until the tool
    clears the turn -- that residual is not closed here."""
    assert api_server._live_turn_should_seal(DODGES_EVERY_PATTERN) is False


@pytest.mark.parametrize("route", ROUTES)
def test_a_vault_tool_turn_writes_no_text_even_when_no_pattern_fires(
        route, client, vault, unlock_tool, agent_route, monkeypatch,
        capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeVaultAgent(unlock_tool))
    capsys.readouterr()
    caplog.clear()

    resp = _post(client, route, DODGES_EVERY_PATTERN)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert SECRET in body, "the household is still answered"

    written = _captured(capsys, caplog)
    assert PIN not in written, written
    assert SECRET not in written, written
    assert "[private turn]" in written, written


def test_a_mark_taken_on_a_pool_thread_reaches_the_flush():
    """The tool body runs on a langgraph ToolNode POOL thread in production.

    LangChain dispatches it through ``run_in_executor``, i.e. a worker thread
    running a COPY of this context -- so the ContextVar lookup finds the SAME
    holder object, and a mark set there is visible to the flush here. That is
    what this asserts, on a real second thread; it is not a claim about any
    hardware.
    """
    api_server._begin_turn_privacy()
    api_server._log_turn_text("[Voice] Route: agent", "[Voice] Turn text:",
                              DODGES_EVERY_PATTERN)
    ctx = contextvars.copy_context()

    def _on_pool_thread():
        return api_server.note_vault_secret_tool("vault_unlock_and_get")

    with ThreadPoolExecutor(max_workers=1) as pool:
        marked = pool.submit(ctx.run, _on_pool_thread).result()
    assert marked is True, "the boundary must take the mark on a pool thread"

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        api_server._flush_turn_log(api_server._consume_vault_turn_private())
    out = buf.getvalue()
    assert "[private turn]" in out, out
    assert PIN not in out, out


# ═══════════════════════════════════════════════════════════════════════════
#  4. THE LOG STAYS USEFUL
# ═══════════════════════════════════════════════════════════════════════════

def test_an_ordinary_turn_logs_its_route_and_its_text_exactly_once(
        client, agent_route, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _PlainAgent())
    capsys.readouterr()

    resp = client.post("/chat/send", headers=_headers(),
                       json={"message": ORDINARY, "conversation_id": "c-ord"})
    assert resp.status_code == 200

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    route_lines = [ln for ln in lines if ln.startswith("[Chat] Route: ")]
    text_lines = [ln for ln in lines if ORDINARY in ln]
    assert len(route_lines) == 1, lines
    assert route_lines[0] == "[Chat] Route: agent", route_lines
    assert len(text_lines) == 1, lines
    assert text_lines[0].startswith("[Chat] Turn text: "), text_lines


def test_the_early_route_line_carries_no_text_at_all(
        client, agent_route, monkeypatch, capsys):
    """The point of layer B: the line printed BEFORE the turn's privacy is
    known has nothing in it to leak."""
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _PlainAgent())
    printed = []
    real = api_server._log_turn_text

    def _spy(now_line, later_prefix, text):
        printed.append(now_line)
        return real(now_line, later_prefix, text)

    monkeypatch.setattr(api_server, "_log_turn_text", _spy)
    client.post("/chat/send", headers=_headers(),
                json={"message": DODGES_EVERY_PATTERN,
                      "conversation_id": "c-early"})
    capsys.readouterr()
    assert printed, "the route line must still be printed"
    for line in printed:
        assert PIN not in line, line
        assert "wifi" not in line, line


def test_a_turn_that_raises_logs_no_user_text(
        client, agent_route, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(api_server, "_process_chat_query", _boom)
    capsys.readouterr()
    caplog.clear()

    resp = client.post("/chat/send", headers=_headers(),
                       json={"message": DODGES_EVERY_PATTERN,
                             "conversation_id": "c-raise"})
    assert resp.status_code == 500
    written = _captured(capsys, caplog)
    assert DODGES_EVERY_PATTERN not in written, written
    assert PIN not in written, written


def test_a_streamed_turn_flushes_from_inside_its_generator(
        client, monkeypatch, capsys, caplog):
    """Flask runs teardown_request BEFORE the WSGI server iterates a streamed
    response, so the fallback would otherwise eat the line. The streaming mark
    hands it to the generator instead -- and the fallback never prints text
    under any circumstances, so the two cannot race into a leak."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api_server, "_detect_route", lambda *a, **k: "rag")
    for name in ("_try_med_fastpath", "_try_calendar_recurring",
                 "_try_calendar_delete", "_try_media_bypass_rules",
                 "_pii_deterministic_reply"):
        monkeypatch.setattr(api_server, name, lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_augment_persona_with_memory",
                        lambda persona, user_id: persona)
    monkeypatch.setattr(api_server, "_remember_for_user",
                        lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_stream_ollama_tokens",
                        lambda *a, **k: iter(["ok"]))
    capsys.readouterr()

    resp = client.post("/v1/chat/completions", headers=_headers(),
                       json={"messages": [{"role": "user",
                                           "content": ORDINARY}],
                             "model": "puffpai", "stream": True})
    assert resp.status_code == 200
    resp.get_data(as_text=True)          # iterate the generator

    out = capsys.readouterr().out
    assert "[Voice API] /v1/chat/completions" in out, out
    assert ORDINARY in out, "a non-private streamed turn keeps its text"
    assert "text withheld" not in out, out


# ═════════════════════════════════════════════════════════════════════════
#  5. H1 — the journal withholds on SHAPE, so no word list has to win
# ═════════════════════════════════════════════════════════════════════════
#
# A pattern-miss utterance that never reaches a vault tool leaves the turn
# NOT private, so the end-of-turn flush printed it through _log_safe -- which
# only sealed. Every phrasing below was found by stepping around the word
# list one more time. The log stops playing.

#: Withheld from the journal. The PANEL decision is NOT changed for these.
PIN_SHAPED = (
    "get wifi 4821",
    "pw 4821",
    "passwd is 4821",
    "key in 4821",
    "enter 4821 to open the safe",
    "4821 | wifi",
    "four eight two one | wifi",
    "wifi \uff5c 4821",                  # FULLWIDTH VERTICAL LINE
    "wifi \u00a6 4821",                  # BROKEN BAR
    "forty eight twenty one",
    "double four double eight",
    "\u0664\u0668\u0662\u0661",             # Arabic-Indic digits
    "\uff14\uff18\uff12\uff11",             # fullwidth digits
    "4821" * 5000,                      # 20 000 digits, capped at 80 before
)

#: The accepted cost, stated as a test so it cannot drift silently. These
#: keep their wall-panel line; they lose their journal text.
INNOCENT_BUT_WITHHELD = (
    "what's 1234 plus 4321",
    "call the 2024 budget",
    "what year was 1984 published",
    "zip code 78701",
    "what is the area code for 512",
    "what's the password policy for 2024",
    "password managers, are they safe in 2026",
    "search for 500 mile hikes",
    "login page for my 401k",
)

#: And the other half: a log an operator can still read.
STILL_LOGGED = (
    "set a timer for 10 minutes",
    "add 2 pounds of flour",
    "remind me to change the password in 30 days",
    "what's on my calendar for the 12th",
    "set the thermostat to 68",
    "remind me at 7 to call mum",
    "play jazz",
    "unlock the front door",
    "what is the capital of France",
    "turn on the kitchen lights",
    "is my login page down",
    "how do I reset the router",
)


@pytest.mark.parametrize("said", PIN_SHAPED)
def test_a_pin_shape_is_never_written_down_whatever_words_carry_it(said):
    out = api_server._log_safe(said)
    assert PIN not in out, out
    assert "forty" not in out and "four" not in out, out
    assert "\u0664" not in out and "\uff14" not in out, out
    assert out in (api_server._LOG_WITHHELD_DIGITS,
                   "[redacted: private turn]"), out


@pytest.mark.parametrize("said", INNOCENT_BUT_WITHHELD)
def test_an_innocent_number_keeps_the_panel_and_loses_the_log_text(said):
    """The accepted cost. The panel decision is deliberately NOT widened:
    sealing every utterance with a number in it would blank the wall for
    "set the thermostat to 68"."""
    assert api_server._live_turn_should_seal(said) is False, said
    assert api_server._log_safe(said) == api_server._LOG_WITHHELD_DIGITS


@pytest.mark.parametrize("said", STILL_LOGGED)
def test_the_log_is_still_worth_reading(said):
    out = api_server._log_safe(said)
    assert "withheld" not in out, out
    assert "redacted" not in out, out


def test_the_shape_test_reads_the_full_text_not_the_capped_slice():
    """A PIN 200 characters in is still a PIN, and the 80-character cap would
    have hidden it from the check but not from the line."""
    buried = ("tell me a long story about nothing in particular " * 6
              + " and the code is 4821")
    assert len(buried) > 80
    assert api_server._log_text_has_pin_shape(buried) is True
    assert PIN not in api_server._log_safe(buried)


def test_the_shape_test_is_unicode_aware():
    assert api_server._log_text_has_pin_shape("\u0664\u0668\u0662") is True
    assert api_server._log_text_has_pin_shape("\uff14\uff18\uff12") is True
    assert api_server._log_text_has_pin_shape("abc") is False
    assert api_server._log_text_has_pin_shape("") is False
    assert api_server._log_text_has_pin_shape(None) is False


def test_the_shape_test_fails_closed():
    import unittest.mock as mock
    with mock.patch.object(api_server, "_log_text_has_pin_shape",
                           side_effect=RuntimeError("boom")):
        assert api_server._log_safe("get wifi 4821") == "[redacted]"


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("said", ("get wifi 4821", "pw 4821", "4821 | wifi",
                                  "forty eight twenty one"))
def test_no_vault_tool_no_challenge_and_still_no_pin_in_the_journal(
        said, route, client, agent_route, monkeypatch, capsys, caplog):
    """The H1 path end to end: nothing marks this turn private -- no vault
    tool fires, no challenge is armed -- so the flush takes the NOT-private
    branch and prints through _log_safe. That is the line that used to carry
    the PIN."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _PlainAgent())
    capsys.readouterr()
    caplog.clear()

    resp = _post(client, route, said)
    assert resp.status_code == 200
    resp.get_data()

    written = _captured(capsys, caplog)
    assert api_server._consume_vault_turn_private() is False, \
        "this turn must NOT be private, or the test proves nothing"
    if said == "forty eight twenty one":
        assert "forty eight twenty one" not in written, written
    else:
        assert PIN not in written, written
    assert "Route:" in written or "/api/chat" in written or \
        "/v1/chat/completions" in written, "the route line still prints"


# ═════════════════════════════════════════════════════════════════════════
#  6. H2/H3 — two older lines that could carry the same secret
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tool", ("web_search", "app_set_timer",
                                  "vault_unlock_and_get"))
def test_the_legacy_decision_line_redacts_input_for_every_tool(tool):
    """The MODEL writes that tool name. A decision naming a tool that never
    touches the vault can still put the PIN in INPUT."""
    decision = "TOOL: %s\nINPUT: %s | %s\n" % (tool, LABEL, PIN)
    out = rt._redact_tool_decision(decision)
    assert PIN not in out, out
    assert LABEL not in out, out
    assert tool in out, "the tool NAME is the point of the line"


def test_the_reasoning_leak_warning_does_not_log_the_reply(caplog):
    """It logged 100 raw characters of the ASSISTANT half. On a turn answered
    out of an unlocked vault those characters are the decrypted secret."""
    caplog.set_level(logging.DEBUG)
    leaky = ("Your wifi password is: " + SECRET
             + ". The user's request needs to be clarified.")
    out = api_server._guard_chat_reply(leaky, [])
    assert SECRET not in out, "the reply is suppressed"
    assert SECRET not in caplog.text, caplog.text
    assert "marker" in caplog.text, caplog.text
