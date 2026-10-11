"""The AGENT TOOL that reads a vault secret aloud must make its turn private.

THE HOLE THIS CLOSES
--------------------
``_mark_vault_turn_private`` / ``_consume_vault_turn_private`` is the rule for
"this turn never reaches the on-disk conversation history". Every path that
handles a vault secret in ``api_server`` sets it -- the chat gate, the PIN
follow-up, the show-a-document challenge.

``assistant_tools.vault_unlock_and_get`` set it NOWHERE. That tool takes
``'item label | PIN'``, verifies the PIN and returns ``"Your wifi password is:
<value>"`` -- and it is reached from the ordinary agent route, on every surface.
So a spoken or typed unlock ran with no mark at all: the user half (the PIN)
and the assistant half (the decrypted secret) were appended to ``_chat_history``
and written by ``_save_conversation`` / ``_record_voice_turn``, rendered in the
dashboard transcript, posted back by the browser to ``/chat/save``, and -- on
the spoken surface -- published to the wall panel at ``/voice/live``, because
``_live_turn_should_seal`` recognized none of the phrasings that carry a PIN
inside a sentence.

WHAT IS ASSERTED
  1. The tool marks the turn BEFORE it does anything, on a right PIN and on a
     wrong one (the wrong-PIN turn still has the PIN in the user half), and
     fails CLOSED when the mark cannot be taken.
  2. ``/api/chat`` (voice): the answer is SERVED, ``_record_voice_turn`` is
     never called, and the turn is registered so ``/chat/save`` refuses the
     save-back.
  3. The typed routes that reach the same tool -- ``/chat/send``,
     ``/chat/stream``, ``/v1/chat/completions``, ``/conversation`` -- persist
     nothing and register the turn.
  4. The live buffer: a mid-turn poll and the end-of-turn poll carry neither
     the PIN, nor the secret, nor the reasoning.
  5. The new seal rule catches spoken unlock phrasings and leaves ordinary
     numeric speech (timers, arithmetic, a 2024 budget) readable.
  6. An ordinary tool turn is still recorded and still shown.
  7. The mark is consume-once: it cannot suppress the next, ordinary turn.
  8. The journal: the legacy loop no longer prints the PIN or the secret.

No network, no real vault, no model. The vault is a fake directory; the "model"
is a stub that calls the REAL tool through the REAL boundary helper.
"""

import json
import os
from typing import Annotated, Sequence, TypedDict

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from puffpai import api_server
from puffpai import assistant_tools as rt

DEVICE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PIN = "4821"
WRONG_PIN = "9999"
SECRET = "sw0rdf1sh-XYZ"   # short on purpose: the secret scan
LABEL = "wifi password"
#: What the household says out loud. The PIN is IN the sentence.
UNLOCK_SAID = "what's my wifi password, the PIN is 4821"
WRONG_SAID = "what's my wifi password, the PIN is 9999"
ORDINARY = "what is the capital of France"
ORDINARY_REPLY = "Paris."


# -- fakes -------------------------------------------------------------------

class _FakeSession:
    session_id = "vault-session-xyz"


class _FakeAudit:
    def log(self, **kwargs):
        pass


class _FakeVault:
    """Initialized, one stored item, one PIN that works."""

    is_initialized = True

    def __init__(self, vault_dir):
        self._vault_dir = vault_dir
        self._audit = _FakeAudit()
        self.auth_calls = 0

    def authenticate(self, pin):
        self.auth_calls += 1
        if pin != PIN:
            from puffpai.security.vault import AuthenticationError
            raise AuthenticationError("bad pin")
        return _FakeSession()

    def read_secure_session(self, filename, session_id):
        assert session_id == _FakeSession.session_id
        return {"value": SECRET}

    def logout(self, session_id):
        pass


@pytest.fixture
def vault(tmp_path, monkeypatch):
    d = tmp_path / "vault_data"
    d.mkdir()
    (d / "wifi_password.json").write_text("{}", encoding="utf-8")
    # assistant_tools reaches the vault through api_server._get_vault, and
    # _vault_list_stems wants a pathlib directory it can list.
    v = _FakeVault(d)
    monkeypatch.setattr(api_server, "_get_vault", lambda: v)
    return v


@pytest.fixture(scope="module")
def unlock_tool():
    """The REAL tool object, built once (AssistantTools is expensive)."""
    return rt.AssistantTools().tool_dict["vault_unlock_and_get"]


@pytest.fixture(autouse=True)
def fresh_rate_limit(unlock_tool):
    """The tool's own 5-per-300s lockout is a closure shared by the whole
    module (AssistantTools is built once), so without this the sixth test is
    answered "too many unlock attempts" instead of by the vault."""
    seen, queue = set(), [getattr(unlock_tool, "func", None)]
    while queue:                      # the dict lives in _vault_rl_check's
        obj = queue.pop()             # closure, one level in from the tool
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        for cell in (getattr(obj, "__closure__", None) or ()):
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, dict) and "lock_until" in val                     and "attempts" in val:
                val["attempts"].clear()
                val["lock_until"] = 0.0
            elif callable(val):
                queue.append(val)
    yield


@pytest.fixture(autouse=True)
def fresh_turn_privacy():
    api_server._reset_vault_turn_privacy()
    api_server._live_turn_clear()
    yield
    api_server._reset_vault_turn_privacy()
    api_server._live_turn_clear()


@pytest.fixture(autouse=True)
def fresh_registry():
    saved = dict(api_server._private_turns)
    api_server._private_turns.clear()
    yield api_server._private_turns
    api_server._private_turns.clear()
    api_server._private_turns.update(saved)


@pytest.fixture
def saved_turns(monkeypatch):
    calls = []
    monkeypatch.setattr(api_server, "_save_conversation",
                        lambda conv: calls.append(conv))
    before = list(api_server._chat_history)
    try:
        yield calls
    finally:
        api_server._chat_history[:] = before


@pytest.fixture
def voice_turns(monkeypatch):
    calls = []
    monkeypatch.setattr(api_server, "_record_voice_turn",
                        lambda *a, **k: calls.append((a, k)))
    return calls


class _FakeAgent:
    """Stands in for the model + agent loop.

    It does what the real loop does at the only point that matters here: it
    announces the tool call at the shared boundary and then invokes the REAL
    ``vault_unlock_and_get``. ``seen`` photographs the live buffer while the
    turn is still open -- what a concurrent /voice/live poll would have read.
    """

    def __init__(self, tool, mod, seen=None, arg=None):
        self._tool = tool
        self._mod = mod
        self._seen = seen
        self._arg = arg
        self.ran = []

    def _snap(self):
        if self._seen is not None:
            self._seen.append(self._mod._live_turn_snapshot())

    def run(self, query, conversation_history=None, **kwargs):
        self._snap()                                   # just after begin
        self._mod._live_turn_append_thinking(
            "They said " + str(query) + ". I will unlock the vault. ")
        rt.note_vault_tool_calls(["vault_unlock_and_get"])
        arg = self._arg if self._arg is not None else _arg_for(query)
        reply = self._tool.invoke(arg)
        self.ran.append(arg)
        self._mod._live_turn_append_reply(reply)
        self._snap()                                   # mid-turn
        return reply

    def executed_tool_names(self):
        return ["vault_unlock_and_get"]

    def daily_briefing(self, *a, **k):                 # pragma: no cover
        return ""


class _OrdinaryAgent:
    """An agent turn that runs a NON-vault tool."""

    def __init__(self, mod, seen=None):
        self._mod = mod
        self._seen = seen

    def run(self, query, conversation_history=None, **kwargs):
        if self._seen is not None:
            self._seen.append(self._mod._live_turn_snapshot())
        rt.note_vault_tool_calls(["auto_weather_forecast"])
        self._mod._live_turn_append_reply(ORDINARY_REPLY)
        if self._seen is not None:
            self._seen.append(self._mod._live_turn_snapshot())
        return ORDINARY_REPLY

    def executed_tool_names(self):
        return ["auto_weather_forecast"]

    def daily_briefing(self, *a, **k):                 # pragma: no cover
        return ""


def _arg_for(query):
    """What the model is told to pass: 'item label | PIN'."""
    pin = WRONG_PIN if WRONG_PIN in (query or "") else PIN
    return LABEL + " | " + pin


@pytest.fixture
def agent_route(monkeypatch):
    """Force both pipelines onto the plain agent route and stub its neighbours."""
    monkeypatch.setattr(api_server, "_detect_route", lambda *a, **k: "agent")
    for name in ("_try_compound_request", "_try_instant_answer",
                 "_try_knowledge_search", "_try_team_chat",
                 "_try_direct_handle", "_try_composed_plan",
                 "_try_capability_gap", "_try_med_fastpath",
                 "_maybe_vault_gated_answer", "_pii_deterministic_reply"):
        monkeypatch.setattr(api_server, name, lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_verify_and_fix",
                        lambda _q, response, *a, **k: response)
    monkeypatch.setattr(api_server, "_snapshot_counts", lambda *a, **k: {})
    monkeypatch.setattr(api_server, "_get_rag_context", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_apply_pii_honesty",
                        lambda text, *a, **k: text)


@pytest.fixture
def client(monkeypatch):
    api_server.app.config.update(TESTING=True)
    api_server._rate_limit_store.clear()
    with api_server.app.test_client() as c:
        yield c


def _headers():
    return {"X-API-Key": api_server._API_KEY, "X-Puff-Device-Id": DEVICE}


def _say(client, text):
    return client.post("/api/chat", headers=_headers(),
                       json={"messages": [{"role": "user", "content": text}]})


def _blob(obj):
    return json.dumps(obj, default=str)


def _leaks(obj):
    """Does anything here carry the PIN or the secret?"""
    text = _blob(obj)
    return SECRET in text or PIN in text


# -- 1. the tool marks, before it does anything ------------------------------

def test_a_successful_unlock_marks_the_turn_private(vault, unlock_tool):
    out = unlock_tool.invoke(LABEL + " | " + PIN)
    assert SECRET in out, "the household must still get their secret"
    assert api_server._consume_vault_turn_private() is True


def test_a_wrong_pin_marks_the_turn_private_too(vault, unlock_tool):
    """The attempt failed; the user half of the turn is still the PIN."""
    out = unlock_tool.invoke(LABEL + " | " + WRONG_PIN)
    assert SECRET not in out
    assert api_server._consume_vault_turn_private() is True


def test_a_malformed_call_marks_the_turn_private(vault, unlock_tool):
    """No pipe, no PIN parsed -- but the words still came from an unlock ask."""
    unlock_tool.invoke("wifi password")
    assert api_server._consume_vault_turn_private() is True


def test_the_tool_fails_closed_when_it_cannot_mark(vault, unlock_tool,
                                                   monkeypatch):
    """No suppression, no secret. The one honest answer left."""
    monkeypatch.setattr(api_server, "note_vault_secret_tool",
                        lambda *_a, **_k: False)
    out = unlock_tool.invoke(LABEL + " | " + PIN)
    assert SECRET not in out
    assert "isn't answering" in out


def test_the_document_display_tool_marks_too():
    tool = rt.AssistantTools().tool_dict["vault_display_document"]
    from puffpai import dashboard_display as dd
    try:
        out = tool.invoke("my birth certificate")
        assert "PIN" in out or "pin" in out
        assert api_server._consume_vault_turn_private() is True
    finally:
        dd.clear_pending_vault()


def test_the_boundary_helper_only_fires_for_vault_tools():
    rt.note_vault_tool_calls(["auto_weather_forecast", "app_set_timer"])
    assert api_server._consume_vault_turn_private() is False
    rt.note_vault_tool_calls(["auto_weather_forecast", "vault_unlock_and_get"])
    assert api_server._consume_vault_turn_private() is True


def test_both_agent_loops_announce_at_the_boundary():
    """The pre-invoke mark, where a schema error or a blocked duplicate lives.

    Source-level, deliberately: driving a real LangGraph run needs a model.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "puffpai", "agent_graph.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("note_vault_tool_calls") >= 2, \
        "the tool node AND the ReAct fallback must both mark"
    node = src.split("def _tools_node", 1)[1].split("\n    def ", 1)[0]
    assert "note_vault_tool_calls" in node
    assert node.index("note_vault_tool_calls") < node.index("_tool_node.invoke")


# -- 2. /api/chat: served, never recorded, registered ------------------------

def test_the_spoken_unlock_is_served_but_not_recorded(client, vault,
                                                      unlock_tool, agent_route,
                                                      voice_turns, monkeypatch):
    agent = _FakeAgent(unlock_tool, api_server)
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: agent)

    resp = _say(client, UNLOCK_SAID)
    assert resp.status_code == 200
    assert SECRET in resp.get_json()["message"]["content"]
    assert agent.ran == [LABEL + " | " + PIN], "the real tool ran"
    assert voice_turns == [], "nothing may reach the voice transcript"
    assert api_server._save_back_should_refuse(UNLOCK_SAID) is True


def test_a_wrong_spoken_pin_is_not_recorded_either(client, vault, unlock_tool,
                                                   agent_route, voice_turns,
                                                   monkeypatch):
    agent = _FakeAgent(unlock_tool, api_server)
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: agent)

    assert _say(client, WRONG_SAID).status_code == 200
    assert voice_turns == []
    assert api_server._save_back_should_refuse(WRONG_SAID) is True


def test_an_ordinary_spoken_tool_turn_is_still_recorded(client, agent_route,
                                                        voice_turns,
                                                        monkeypatch):
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _OrdinaryAgent(api_server))

    assert _say(client, ORDINARY).status_code == 200
    assert len(voice_turns) == 1, "an ordinary turn keeps its transcript"
    assert ORDINARY in _blob(voice_turns)


def test_the_mark_does_not_leak_into_the_next_ordinary_turn(
        client, vault, unlock_tool, agent_route, voice_turns, monkeypatch):
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server))
    assert _say(client, UNLOCK_SAID).status_code == 200
    assert voice_turns == []

    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _OrdinaryAgent(api_server))
    assert _say(client, ORDINARY).status_code == 200
    assert len(voice_turns) == 1, "the next turn must not stay suppressed"
    assert not _leaks(voice_turns)


# -- 3. the typed routes -----------------------------------------------------

def _assert_not_persisted(saved_turns, message):
    assert saved_turns == [], "a vault-tool turn must not reach the store"
    assert api_server._is_registered_private_turn(message) is True, \
        "the save-back registry must refuse the client's copy"


def test_chat_send_does_not_persist_a_vault_tool_turn(client, vault,
                                                      unlock_tool, agent_route,
                                                      saved_turns, monkeypatch):
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server))
    resp = client.post("/chat/send", headers=_headers(),
                       json={"message": UNLOCK_SAID,
                             "conversation_id": "conv-send"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert SECRET in body["response"], "the caller is still answered"
    assert body["private"] is True, "the client is told not to post it back"
    _assert_not_persisted(saved_turns, UNLOCK_SAID)


def test_chat_stream_does_not_persist_a_vault_tool_turn(client, vault,
                                                        unlock_tool,
                                                        agent_route,
                                                        saved_turns,
                                                        monkeypatch):
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server))
    resp = client.post("/chat/stream", headers=_headers(),
                       json={"message": UNLOCK_SAID,
                             "conversation_id": "conv-stream"})
    assert resp.status_code == 200
    assert SECRET in resp.get_data(as_text=True)
    _assert_not_persisted(saved_turns, UNLOCK_SAID)


def test_openai_completions_does_not_persist_a_vault_tool_turn(
        client, vault, unlock_tool, agent_route, saved_turns, monkeypatch):
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server))
    resp = client.post("/v1/chat/completions", headers=_headers(),
                       json={"messages": [{"role": "user",
                                           "content": UNLOCK_SAID}],
                             "stream": False})
    assert resp.status_code == 200
    _assert_not_persisted(saved_turns, UNLOCK_SAID)


def test_conversation_does_not_persist_and_now_registers(client, vault,
                                                         unlock_tool,
                                                         agent_route,
                                                         saved_turns,
                                                         monkeypatch):
    """/conversation consumed the mark but never registered the turn."""
    agent = _FakeAgent(unlock_tool, api_server)
    monkeypatch.setattr(rt, "AssistantTools", lambda *a, **k: agent)
    resp = client.post("/conversation", headers=_headers(),
                       json={"text": UNLOCK_SAID,
                             "conversation_id": "conv-voice"})
    assert resp.status_code == 200
    assert SECRET in resp.get_json()["response"]
    _assert_not_persisted(saved_turns, UNLOCK_SAID)


def test_chat_save_refuses_the_save_back(client, vault, unlock_tool,
                                         agent_route, saved_turns,
                                         monkeypatch):
    """End to end: serve on one route, then re-persist from the browser."""
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server))
    client.post("/chat/send", headers=_headers(),
                json={"message": UNLOCK_SAID, "conversation_id": "conv-save"})
    saved_turns.clear()

    resp = client.post("/chat/save", headers=_headers(),
                       json={"conversation_id": "conv-save",
                             "user_message": UNLOCK_SAID,
                             "assistant_message":
                                 "Your wifi password is: " + SECRET})
    assert resp.status_code == 200
    assert saved_turns == [], "the client's copy must be refused too"


# -- 4. the live buffer ------------------------------------------------------

def test_the_wall_never_sees_the_pin_or_the_secret(client, vault, unlock_tool,
                                                   agent_route, voice_turns,
                                                   monkeypatch):
    seen = []
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server, seen))

    assert _say(client, UNLOCK_SAID).status_code == 200
    after = client.get("/voice/live", headers=_headers()).get_json()

    assert len(seen) == 2, "the turn actually ran"
    for snap in seen + [after]:
        assert not _leaks(snap)
        assert snap["heard"] == "" and snap["thinking"] == "" \
            and snap["reply"] == ""
    assert after["active"] is False and after["turn_id"] is None


def test_an_unsealed_vault_turn_is_cleared_at_the_tool_boundary(
        client, vault, unlock_tool, agent_route, voice_turns, monkeypatch):
    """Belt and braces: even if the seal had missed the phrasing, the boundary
    wipes the buffer the moment the tool is announced -- not at end of turn."""
    monkeypatch.setattr(api_server, "_live_turn_should_seal",
                        lambda _heard: False)
    seen = []
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server, seen))

    assert _say(client, UNLOCK_SAID).status_code == 200
    after = client.get("/voice/live", headers=_headers()).get_json()

    assert seen[0]["heard"] == UNLOCK_SAID, "unsealed: the turn did open"
    assert not _leaks(seen[1]), "the boundary cleared it mid-turn"
    assert seen[1]["reply"] == "" and seen[1]["thinking"] == ""
    assert after["active"] is False
    assert not _leaks(after)


def test_an_ordinary_tool_turn_still_shows_on_the_wall(client, agent_route,
                                                       voice_turns,
                                                       monkeypatch):
    seen = []
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _OrdinaryAgent(api_server, seen))

    assert _say(client, ORDINARY).status_code == 200
    after = client.get("/voice/live", headers=_headers()).get_json()

    assert seen[0]["heard"] == ORDINARY
    assert after["active"] is True and after["reply"] == ORDINARY_REPLY


# -- 5. the seal rule itself -------------------------------------------------

SEALED_PHRASINGS = (
    "what's my wifi password, the PIN is 4821",
    "unlock my wifi password, PIN one two three four",
    "read me my router password, pin 4821",
    "unlock the vault and tell me my bank login, my pin is four eight two one",
    "open the vault, code 9 9 3 1, and read my email password",
    "tell me the netflix password pin 1234",
    # A secret noun with no possessive, but the digits are DICTATED.
    "wifi password please, 4 8 2 1",
    "my passcode is 4821 show the router login",
    # Tens and teens: "forty eight twenty one" IS 4821 said out loud.
    "the pin is forty eight twenty one for my wifi password",
    "my pin is forty-eight twenty-one, read the wifi password",
    "unlock the vault, pin double four triple nine",
    "pin oh nine three one, read my router password",
    # The tool's OWN documented input format, typed or spoken back. No
    # possessive, no dictated run: only the PIN's proximity to the noun.
    "router password | 4821",
    "router password|4821",
    "wifi password 4821",
    "read router password 4821",
    "show wifi password 4821 now",
    # Vault vocabulary with numbers said the way people say them.
    "pin double four two one",
    "my pin is nineteen eighty four",
    "pincode 4 8 2 1",
    "pin is twelve thirty four",
    "pin oh nine nine nine",
    "passphrase it is 1 2 3 4 5",
    "pin 4821 please",
)

ORDINARY_PHRASINGS = (
    "set a timer for 10 minutes",
    "what's 1234 plus 4321",
    "play jazz",
    "call the 2024 budget",
    "turn on the porch light",
    "add 2 pounds of flour to the shopping list",
    # A secret noun with neither a possessive nor dictated digits.
    "what's the password policy for 2024",
    # "code" is ordinary English and is no longer unlock vocabulary.
    "what is the area code for 512",
    "zip code 78701",
    # Unlock vocabulary with nothing PIN-shaped anywhere near it.
    "unlock the front door",
    "remind me at 10 past 4 about the 2024 taxes",
    # A secret noun with a number three or more words away, or too short to
    # be a PIN. These are the phrasings the proximity rule must not eat.
    "login page for my 401k",
    "password managers, are they safe in 2026",
    "remind me to change the password in 30 days",
    "how many secrets are in my vault",
    "zip code for 78701",
)


@pytest.mark.parametrize("said", SEALED_PHRASINGS)
def test_a_spoken_unlock_is_sealed_before_the_buffer_opens(said):
    assert api_server._live_turn_spoken_unlock(said) is True
    assert api_server._live_turn_should_seal(said) is True


@pytest.mark.parametrize("said", ORDINARY_PHRASINGS)
def test_ordinary_numeric_speech_is_not_sealed(said):
    assert api_server._live_turn_spoken_unlock(said) is False
    assert api_server._live_turn_should_seal(said) is False


def test_both_halves_are_required():
    """Unlock words alone, or digits alone, are ordinary English."""
    assert api_server._live_turn_spoken_unlock("what's my wifi password") is False
    assert api_server._live_turn_spoken_unlock("the answer is 4821") is False


# -- 6. the REAL tool-run boundary (langgraph runs bodies on a pool thread) --
#
# The correction this section exists for: langgraph's ToolNode executes every
# tool body through ``get_executor_for_config(config).map(...)``, i.e. on a
# thread-pool thread. A threading.local mark made inside the tool is invisible
# to the request thread that consumes it, so the in-tool mark alone was
# decorative on the primary agent path and the boundary hook -- whose failure
# used to be swallowed -- was the only thing standing between a spoken PIN and
# the transcript. These tests drive the real node through a real compiled
# graph (a bare ToolNode.invoke has no langgraph runtime and raises).


class _GraphState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


def _tool_call_msg(arg):
    return AIMessage(content="", tool_calls=[{
        "name": "vault_unlock_and_get",
        "args": {"item_and_pin": arg},
        "id": "call_1"}])


def _bare_agent(tool):
    """A PuffPAIAgent with just the state _tools_node touches (no model)."""
    from puffpai.agent_graph import PuffPAIAgent
    agent = object.__new__(PuffPAIAgent)
    agent._tool_node = ToolNode([tool])
    agent.tool_dict = {tool.name: tool}
    agent._seen_calls = set()
    agent._executed = []
    agent._dup_rounds = 0
    agent._force_final = False
    agent.max_steps = 5
    return agent


def _run_graph(node_fn, message):
    """Drive a node the way production does: through a compiled graph, which
    is what supplies the langgraph runtime a bare ToolNode.invoke lacks."""
    graph = StateGraph(_GraphState)
    graph.add_node("tools", node_fn)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile().invoke({"messages": [message]})


def _run_node(node_fn, arg):
    return _run_graph(node_fn, _tool_call_msg(arg))


def _tools_node_of(agent):
    from puffpai.agent_graph import PuffPAIAgent
    return lambda state: PuffPAIAgent._tools_node(agent, state)


def test_the_real_tool_node_marks_the_invoking_thread(vault, unlock_tool):
    api_server._reset_vault_turn_privacy()
    api_server._live_turn_begin("read me my router password, pin 4821")

    out = _run_node(_tools_node_of(_bare_agent(unlock_tool)),
                    LABEL + " | " + PIN)

    assert SECRET in out["messages"][-1].content, "the household is answered"
    assert api_server._consume_vault_turn_private() is True, \
        "the mark must reach the thread that records the turn"
    snap = api_server._live_turn_snapshot()
    assert snap["active"] is False and not _leaks(snap), \
        "the wall buffer is cleared at the boundary, not at end of turn"


def test_the_tool_body_alone_marks_across_the_pool_thread(vault, unlock_tool):
    """Bypass the boundary hook entirely: the ContextVar layer must carry it."""
    api_server._reset_vault_turn_privacy()

    out = _run_node(ToolNode([unlock_tool]), LABEL + " | " + PIN)

    assert SECRET in out["messages"][-1].content
    assert getattr(api_server._turn_privacy, "vault_private", False) is False, \
        "the body ran on a pool thread: the thread-local could never see it"
    assert api_server._consume_vault_turn_private() is True, \
        "...but the turn's holder did"


def test_the_boundary_refuses_to_invoke_when_the_mark_is_refused(
        vault, unlock_tool, monkeypatch):
    """ONLY the boundary fails -- the tool body would happily mark and answer.

    That isolation is the point: the boundary is the last code on the request
    thread, and before this change its verdict was ignored."""
    monkeypatch.setattr(rt, "note_vault_tool_calls", lambda _names: False)

    out = _run_node(_tools_node_of(_bare_agent(unlock_tool)),
                    LABEL + " | " + PIN)

    content = out["messages"][-1].content
    assert SECRET not in content and PIN not in content
    assert "isn't answering" in content
    assert vault.auth_calls == 0, "the tool body must never have run"


def test_the_boundary_refuses_to_invoke_when_the_hook_raises(
        vault, unlock_tool, monkeypatch):
    """A raising boundary used to be logged and stepped over."""
    def _boom(*_a, **_k):
        raise RuntimeError("guard down")

    monkeypatch.setattr(rt, "note_vault_tool_calls", _boom)

    out = _run_node(_tools_node_of(_bare_agent(unlock_tool)),
                    LABEL + " | " + PIN)

    content = out["messages"][-1].content
    assert SECRET not in content and PIN not in content
    assert vault.auth_calls == 0


def test_an_ordinary_tool_is_unaffected_by_the_boundary(unlock_tool):
    """The refusal is for vault tools only; everything else runs as before."""
    from langchain_core.tools import tool as _tool_decorator

    @_tool_decorator
    def echo_tool(text: str = "") -> str:
        """Echo."""
        return "echoed " + text

    msg = AIMessage(content="", tool_calls=[
        {"name": "echo_tool", "args": {"text": "hi"}, "id": "c1"}])
    out = _run_graph(_tools_node_of(_bare_agent(echo_tool)), msg)

    assert "echoed hi" in out["messages"][-1].content
    assert api_server._consume_vault_turn_private() is False


def test_a_tool_outside_any_turn_refuses(vault, unlock_tool):
    """No holder: nothing can suppress this turn, so nothing is read out."""
    api_server._turn_privacy_ctx.set(None)
    api_server._turn_privacy.vault_private = False

    out = unlock_tool.invoke(LABEL + " | " + PIN)

    assert SECRET not in out and "isn't answering" in out
    assert vault.auth_calls == 0


def test_every_request_gets_a_fresh_holder(client):
    api_server._begin_turn_privacy()
    mine = api_server._turn_privacy_holder()
    mine.private = True

    client.get("/voice/live", headers=_headers())

    current = api_server._turn_privacy_holder()
    assert current is not mine, "before_request installs a holder per request"
    assert current.private is False


def test_the_holder_does_not_leak_into_the_next_request(client, vault,
                                                        unlock_tool,
                                                        agent_route,
                                                        saved_turns,
                                                        monkeypatch):
    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _FakeAgent(unlock_tool, api_server))
    client.post("/chat/send", headers=_headers(),
                json={"message": UNLOCK_SAID, "conversation_id": "conv-leak"})
    assert saved_turns == []

    monkeypatch.setattr(api_server, "_get_assistant_tools",
                        lambda: _OrdinaryAgent(api_server))
    client.post("/chat/send", headers=_headers(),
                json={"message": ORDINARY, "conversation_id": "conv-leak"})
    assert len(saved_turns) == 1, \
        "the next request on this thread must not inherit the mark"
    assert not _leaks(saved_turns)


def test_the_hook_is_found_when_the_server_runs_as_main(monkeypatch):
    """``python src/puffpai/api_server.py`` registers it as __main__ only."""
    import sys as _sys
    import types

    calls = []
    fake_main = types.ModuleType("fake_main")
    fake_main.note_vault_secret_tool = (
        lambda name="": (calls.append(name), True)[1])
    monkeypatch.delitem(_sys.modules, "puffpai.api_server")
    monkeypatch.setitem(_sys.modules, "__main__", fake_main)

    assert rt.mark_vault_tool_turn_private("vault_unlock_and_get") is True
    assert calls == ["vault_unlock_and_get"]


# -- 7. the journal ----------------------------------------------------------

def test_the_route_log_line_does_not_print_the_pin():
    assert PIN not in api_server._log_safe(UNLOCK_SAID)
    assert api_server._log_safe(ORDINARY).startswith("what is the capital")


@pytest.mark.parametrize("said", (
    "router password | 4821",
    "router password|4821",
    "wifi password 4821",
    "read router password 4821",
    "show wifi password 4821 now",
))
def test_the_documented_tool_format_never_reaches_the_journal(said):
    """`[Voice] Route: ...` and `[Chat] Route: ...` are durable log lines and
    they print the utterance. This IS the PIN, in the shape the tool asks for
    it."""
    assert api_server._live_turn_should_seal(said) is True
    assert PIN not in api_server._log_safe(said)


@pytest.mark.parametrize("said,keep", (
    ("remind me to change the password in 30 days", "30"),
    ("set a timer for 10 minutes", "10"),
    ("add 2 pounds of flour", "2"),
))
def test_ordinary_numeric_speech_still_reaches_the_journal(said, keep):
    """The other half of the bargain: the log stays useful."""
    assert keep in api_server._log_safe(said)


@pytest.mark.parametrize("said", (
    "what's the password policy for 2024",
    "what is the area code for 512",
    "zip code 78701",
    "password managers, are they safe in 2026",
))
def test_three_digit_innocents_keep_the_panel_but_lose_the_log_text(said):
    """See tests/test_vault_log_privacy.py. A PIN-shaped run is
    withheld from the LOG whatever words surround it, because "get wifi 4821"
    proves the word list can always be stepped around and the journal is
    durable. The PANEL decision is untouched -- these still publish."""
    assert api_server._live_turn_should_seal(said) is False
    assert api_server._log_safe(said) == api_server._LOG_WITHHELD_DIGITS


def test_the_legacy_loop_does_not_print_the_pin():
    decision = "TOOL: vault_unlock_and_get\nINPUT: " + LABEL + " | " + PIN + "\n"
    redacted = rt._redact_tool_decision(decision)
    assert PIN not in redacted
    assert "vault_unlock_and_get" in redacted, "the tool NAME is the point"


def test_an_ordinary_decision_keeps_its_tool_name_but_not_its_input():
    """INPUT is redacted for EVERY tool, not only the
    vault ones. The model writes the tool name, so a decision naming
    app_set_timer can still carry the PIN in its INPUT."""
    decision = "TOOL: app_set_timer\nINPUT: 10 minutes\n"
    out = rt._redact_tool_decision(decision)
    assert "app_set_timer" in out, "the tool NAME is the point of the line"
    assert "10 minutes" not in out
    assert "[protected]" in out
