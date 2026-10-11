"""Every chat turn credits the agent(s) that handled it — and leaks nothing.

The Agent Team page must not show roster agents as never engaged when they
have been. ``GET /agents/status`` derives those rows from
``agent-activity.jsonl``, and the whole CHAT dispatch path, the ``_try_*``
fastpaths, the agent graph, the planner, must write to it, not leave it
populated only by the announcer, consent, Node-RED, the bridge and the
legacy task queue.

:mod:`puffpai.chat_activity` closes that gap with one post-route hook in
``_process_chat_query``. These tests pin the two things that make the fix
acceptable:

* ATTRIBUTION — the owner comes from the capability registry, never from a
  guess: a fastpath credits its capability's ``agent=``, a graph turn credits
  each agent whose tool actually RAN (once each), and a turn with no capability
  behind it credits the Concierge that routed it.
* PRIVACY — an activity event is a household-visible, on-disk, indefinitely
  retained record, so it carries no content at all.
  ``TestPrivacy::test_no_utterance_reply_or_conversation_id_reaches_the_feed``
  drives a turn whose utterance AND reply both contain a fake SSN, with a
  conversation id set, and asserts none of the three is anywhere in the bytes
  that were written.

Plus the two fail-soft properties: the flag off writes nothing, and a writer
that raises cannot change the reply.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                                   # noqa: E402
from puffpai import chat_activity                                # noqa: E402
from puffpai.security import consent_requests as cr              # noqa: E402


PII = "my SSN is 000-12-3456"
PII_REPLY = "Noted that your SSN is 000-12-3456, Alex."
CONVERSATION_ID = "conv-7f3a9c21-secret"


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_turn_stack():
    """No turn scope may leak between tests."""
    chat_activity._LOCAL.stack = []
    yield
    chat_activity._LOCAL.stack = []


@pytest.fixture
def feed(tmp_path, monkeypatch):
    """The activity feed, redirected at the writer both halves already share."""
    path = tmp_path / "agent-activity.jsonl"
    monkeypatch.setattr(cr, "ACTIVITY_LOG", str(path))

    class _Feed:
        path = None

        def raw(self):
            return path.read_text(encoding="utf-8") if path.exists() else ""

        def entries(self):
            return [json.loads(line) for line in self.raw().splitlines() if line.strip()]

        def turns(self):
            return [e for e in self.entries() if e.get("event") == "turn_handled"]

        def by_agent(self):
            return {e["agent"]: e for e in self.turns()}

    _Feed.path = path
    return _Feed()


@pytest.fixture
def chat(monkeypatch):
    """``_process_chat_query`` with everything but the routing stubbed out.

    Returns a callable ``chat(message, route=..., instant=..., **kw)``. The
    dispatcher is replaced by a stub so a test can decide which fastpath wins
    without depending on the matching behaviour of the other 39.
    """
    monkeypatch.setattr(api_server, "_load_persona", lambda: "persona")
    monkeypatch.setattr(api_server, "_augment_persona_with_memory",
                        lambda persona, user_id: persona)
    monkeypatch.setattr(api_server, "_remember_for_user", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_requester_for", lambda user_id: "owner")
    monkeypatch.setattr(api_server, "_try_compound_request",
                        lambda *a, **k: None)

    def _run(message, *, route="rag", instant=None, **kwargs):
        monkeypatch.setattr(api_server, "_detect_route", lambda *a, **k: route)
        monkeypatch.setattr(api_server, "_try_instant_answer",
                            instant or (lambda *a, **k: None))
        return api_server._process_chat_query(
            message, conversation_id=CONVERSATION_ID, **kwargs)

    return _run


@pytest.fixture
def voice(monkeypatch):
    """``_process_voice_query`` with everything but the routing stubbed out.

    Same shape as the ``chat`` fixture above, for the voice path: voice must
    have a turn scope like chat does, so voice turns write to the feed too.
    """
    monkeypatch.setattr(api_server, "_load_persona", lambda: "persona")
    monkeypatch.setattr(api_server, "_augment_persona_with_memory",
                        lambda persona, user_id: persona)
    monkeypatch.setattr(api_server, "_remember_for_user", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_requester_for", lambda user_id: "owner")
    monkeypatch.setattr(api_server, "_try_compound_request",
                        lambda *a, **k: None)

    def _run(message, *, route="rag", instant=None, **kwargs):
        monkeypatch.setattr(api_server, "_detect_route", lambda *a, **k: route)
        monkeypatch.setattr(api_server, "_try_instant_answer",
                            instant or (lambda *a, **k: None))
        return api_server._process_voice_query(
            message, conversation_id=CONVERSATION_ID, **kwargs)

    return _run


def _fastpath(name, reply):
    """A stub fastpath wrapped by the real probe the install hook applies."""
    return chat_activity.probe(name, lambda *a, **k: reply)


class _GraphTools:
    """Stands in for the agent-graph object ``_get_assistant_tools()`` returns."""

    def __init__(self, reply, executed):
        self._reply = reply
        self._executed = list(executed)

    def run(self, query, history):
        return self._reply

    def executed_tool_names(self):
        out = []
        for name in self._executed:
            if name not in out:
                out.append(name)
        return out


@pytest.fixture
def graph_turn(monkeypatch):
    """Drive the ``route == "agent"`` branch straight onto the graph."""
    monkeypatch.setattr(api_server, "_try_knowledge_search", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_try_team_chat", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_try_direct_handle", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "_snapshot_counts", lambda *a, **k: {})
    monkeypatch.setattr(api_server, "_verify_and_fix",
                        lambda msg, response, before: response)

    def _install(reply, executed):
        monkeypatch.setattr(api_server, "_get_assistant_tools",
                            lambda: _GraphTools(reply, executed))

    return _install


# ── the install hook ─────────────────────────────────────────────────────────

class TestProbeInstall:

    def test_every_dispatcher_fastpath_is_probed(self):
        """The one hook really did cover the whole chain, not a subset."""
        names = _dispatcher_fastpath_names()
        assert len(names) > 30, f"only found {len(names)} fastpaths; parse broke"
        unprobed = [
            n for n in names
            if not hasattr(getattr(api_server, n, None),
                           "__puffpai_activity_probe__")
        ]
        assert unprobed == [], f"fastpaths with no activity probe: {unprobed}"

    def test_every_fastpath_resolves_to_a_named_owner(self):
        """No fastpath is silently unaccounted for: mapped, or listed as a TODO."""
        names = set(_dispatcher_fastpath_names())
        known = (set(chat_activity.FASTPATH_CAPABILITY)
                 | set(chat_activity.UNMAPPED_FASTPATHS)
                 | chat_activity._CHITCHAT_FASTPATHS)
        assert names - known == set(), (
            "these fastpaths are neither mapped to a capability nor listed in "
            f"UNMAPPED_FASTPATHS: {sorted(names - known)}")

    def test_mapped_capabilities_all_exist_in_the_registry(self):
        """The map never invents a capability id the registry does not have."""
        from puffpai import capability_registry as registry
        real = {c.id for c in registry.CAPABILITIES}
        bogus = sorted(set(chat_activity.FASTPATH_CAPABILITY.values()) - real)
        assert bogus == [], f"no such capability rows: {bogus}"

    def test_probe_is_transparent_to_source_introspection(self):
        """tests/test_instant_answer_precedence.py reads the dispatcher's source."""
        import inspect
        src = inspect.getsource(api_server._try_weather_answer)
        assert "def _try_weather_answer" in src


def _dispatcher_fastpath_names():
    import ast
    import inspect
    import re

    src = inspect.getsource(api_server._try_instant_answer)
    src = re.sub(r"^ +", "", src, count=1) if src.startswith(" ") else src
    out = []
    for node in ast.walk(ast.parse(src).body[0]):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None)
        if name and name.startswith("_try_") and name not in out:
            out.append(name)
    return out


# ── attribution ──────────────────────────────────────────────────────────────

class TestAttribution:

    def test_fastpath_turn_logs_the_registry_owner(self, chat, feed):
        """weather.current is the Majordomo's row, so the Majordomo gets the line."""
        reply = chat("what is the weather",
                     instant=_fastpath("_try_weather_answer", "72 and clear"))

        assert reply["response"] == "72 and clear"
        turns = feed.turns()
        assert len(turns) == 1
        entry = turns[0]
        assert entry["agent"] == "majordomo"
        assert entry["event"] == "turn_handled"
        assert entry["status"] == "completed"
        assert entry["task"] == "weather.current via fastpath"
        assert entry["result"]["capability_id"] == "weather.current"
        assert entry["result"]["source"] == "fastpath"
        assert entry["result"]["route"] == "rag"
        assert isinstance(entry["result"]["ms"], int)

    def test_unmapped_fastpath_falls_to_the_concierge(self, chat, feed):
        """No registry row -> the Concierge, never a guessed owner."""
        chat("what time is it",
             instant=_fastpath("_try_time_answer", "It's 4:15."))

        entry = feed.turns()[0]
        assert entry["agent"] == "concierge"
        assert entry["result"]["capability_id"] is None
        assert entry["task"] == "general Q&A via fastpath"

    def test_chitchat_is_the_concierge_with_its_own_source(self, chat, feed):
        chat("hello there", instant=_fastpath("_try_chitchat", "Hi!"))

        entry = feed.turns()[0]
        assert entry["agent"] == "concierge"
        assert entry["result"]["source"] == "chitchat"
        assert entry["task"] == "general Q&A via chitchat"

    def test_agent_graph_turn_logs_each_invoked_tools_owner(
            self, chat, feed, graph_turn):
        """Two Steward tools and one Majordomo tool -> two lines, not three."""
        graph_turn("Done.", ["ha_control_device", "ha_list_devices",
                             "auto_weather_forecast"])
        reply = chat("turn on the lamp and tell me the forecast", route="agent")

        assert reply["response"] == "Done."
        by_agent = feed.by_agent()
        assert set(by_agent) == {"steward", "majordomo"}
        assert len(feed.turns()) == 2, "one event per (turn, agent)"
        assert by_agent["steward"]["result"]["source"] == "agent_graph"
        # First claim wins, so the Steward's line names the first tool's row.
        assert by_agent["steward"]["result"]["capability_id"] == "home.control"
        assert by_agent["majordomo"]["result"]["capability_id"] == "weather.current"
        assert by_agent["majordomo"]["task"] == "weather.current via agent graph"

    def test_repeated_tool_for_one_agent_writes_one_line(
            self, chat, feed, graph_turn):
        graph_turn("Done.", ["ha_control_device", "ha_control_device",
                             "ha_list_devices"])
        chat("turn everything on", route="agent")

        assert [e["agent"] for e in feed.turns()] == ["steward"]

    def test_graph_turn_with_no_tools_still_credits_someone(
            self, chat, feed, graph_turn):
        graph_turn("I'm not sure.", [])
        chat("something vague", route="agent")

        entry = feed.turns()[0]
        assert entry["agent"] == "concierge"
        assert entry["result"]["source"] == "agent_graph"

    def test_rag_turn_logs_the_concierge(self, chat, feed, monkeypatch):
        monkeypatch.setattr(
            api_server, "_query_ollama_direct",
            lambda *a, **k: {"response": "Because of tides.",
                             "sources": [], "suggestions": []})
        reply = chat("why is the sea salty")

        assert reply["response"] == "Because of tides."
        entry = feed.turns()[0]
        assert entry["agent"] == "concierge"
        assert entry["result"]["source"] == "rag"
        assert entry["result"]["capability_id"] is None
        assert entry["task"] == "general Q&A via rag"

    def test_library_route_credits_the_archivist(self, chat, feed, monkeypatch):
        monkeypatch.setattr(api_server, "_get_indexed_libraries_direct",
                            lambda: "Three libraries are indexed.")
        chat("what libraries do you have", route="library")

        entry = feed.turns()[0]
        assert entry["agent"] == "archivist"
        assert entry["result"]["capability_id"] == "knowledge.library"

    def test_legacy_rows_are_untouched(self, chat, feed, monkeypatch):
        """No turn ever writes as cortex/bridge/sentinel-the-legacy-row."""
        monkeypatch.setattr(
            api_server, "_query_ollama_direct",
            lambda *a, **k: {"response": "ok", "sources": [], "suggestions": []})
        chat("anything")

        assert all(e["event"] == "turn_handled" for e in feed.entries())
        assert "cortex" not in {e["agent"] for e in feed.entries()}


# ── privacy ──────────────────────────────────────────────────────────────────

class TestVoiceTurnScope:
    """The voice path must have its own turn scope, same as chat."""

    def test_voice_turn_logs_exactly_one_event_per_agent(self, voice, feed):
        reply = voice(
            "what's the weather",
            instant=_fastpath("_try_weather_answer", "72 and clear"))
        assert reply == "72 and clear"

        turns = feed.turns()
        agents = [t["agent"] for t in turns]
        assert len(agents) == len(set(agents)), (
            "exactly one event per (turn, agent), no duplicates")
        assert turns, "the voice turn must have written at least one event"

    def test_nested_voice_scope_is_a_no_op(self, voice, feed, monkeypatch):
        """A compound voice turn re-enters ``_process_voice_query`` for each
        clause; the inner call must JOIN the outer scope, not open (and
        write) a second one."""
        def _compound(message, run_clause, **kwargs):
            # Simulate the compound-request path invoking the clause callback,
            # which re-enters _process_voice_query recursively.
            run_clause("inner clause")
            return "outer response"

        monkeypatch.setattr(api_server, "_try_compound_request", _compound)
        reply = voice(
            "do two things",
            instant=_fastpath("_try_weather_answer", "72 and clear"))
        assert reply == "outer response"

        # Only ONE write reached the feed for the whole compound turn — the
        # inner recursive call joined the outer scope rather than writing its
        # own line and leaving the outer scope orphaned (or double-writing).
        assert len(feed.turns()) == 1


class TestPrivacy:

    def test_no_utterance_reply_or_conversation_id_reaches_the_feed(
            self, chat, feed):
        """The non-negotiable rule, proved on the bytes that were written."""
        reply = chat(PII, instant=_fastpath("_try_weather_answer", PII_REPLY))
        assert reply["response"] == PII_REPLY  # the reply itself is unchanged

        raw = feed.raw()
        assert raw.strip(), "nothing was written; the assertions below are vacuous"
        for forbidden in ("000-12-3456", "SSN", "ssn", PII, PII_REPLY,
                          CONVERSATION_ID, "conv-", "Alex"):
            assert forbidden not in raw, f"{forbidden!r} leaked into the feed"

    def test_no_pii_reaches_the_feed_via_voice(self, voice, feed):
        """The same rule, driven through the voice path."""
        reply = voice(PII, instant=_fastpath("_try_weather_answer", PII_REPLY))
        assert PII_REPLY in reply  # voice-cleaned, but the reply text survives

        raw = feed.raw()
        assert raw.strip(), "nothing was written; the assertions below are vacuous"
        for forbidden in ("000-12-3456", "SSN", "ssn", PII, PII_REPLY,
                          CONVERSATION_ID, "conv-", "Alex"):
            assert forbidden not in raw, f"{forbidden!r} leaked into the feed"

    def test_the_event_carries_only_the_allowed_keys(self, chat, feed):
        chat(PII, instant=_fastpath("_try_weather_answer", PII_REPLY))

        entry = feed.turns()[0]
        assert set(entry) == {"agent", "event", "task", "status", "timestamp",
                              "result"}
        assert set(entry["result"]) == {"route", "capability_id", "ms", "source"}

    def test_graph_tool_results_never_reach_the_feed(
            self, chat, feed, graph_turn, monkeypatch):
        """The graph's own record holds result snippets; only NAMES are read."""
        tools = _GraphTools(PII_REPLY, [])
        tools._executed = ["ha_control_device"]
        monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: tools)
        graph_turn(PII_REPLY, ["ha_control_device"])
        monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: tools)
        chat(PII, route="agent")

        raw = feed.raw()
        assert "000-12-3456" not in raw and PII_REPLY not in raw

    def test_the_real_graph_accessor_drops_result_snippets(self):
        """puffpai.agent_graph exposes names only — the source of that promise."""
        from puffpai.agent_graph import PuffPAIAgent

        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent._executed = [("ha_control_device", PII_REPLY),
                           ("ha_control_device", "again"),
                           ("auto_weather_forecast", "72 and clear")]
        assert agent.executed_tool_names() == ["ha_control_device",
                                               "auto_weather_forecast"]


# ── fail-soft ────────────────────────────────────────────────────────────────

class TestFailSoft:

    def test_flag_off_writes_nothing(self, chat, feed, monkeypatch):
        monkeypatch.setattr(chat_activity, "enabled", lambda: False)
        reply = chat("what is the weather",
                     instant=_fastpath("_try_weather_answer", "72 and clear"))

        assert reply["response"] == "72 and clear"
        assert feed.raw() == ""

    def test_flag_defaults_to_true(self):
        from puffpai.config import AgentsConfig
        assert AgentsConfig().activity_from_chat is True

    def test_writer_exception_does_not_change_the_reply(
            self, chat, feed, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("feed is on fire")

        monkeypatch.setattr(cr, "log_agent_activity", _boom)
        reply = chat("what is the weather",
                     instant=_fastpath("_try_weather_answer", "72 and clear"))

        assert reply == {"response": "72 and clear", "sources": [],
                         "suggestions": []}
        assert feed.raw() == ""

    def test_broken_registry_does_not_break_the_turn(
            self, chat, feed, monkeypatch):
        monkeypatch.setattr(chat_activity, "_ownership",
                            lambda: (_ for _ in ()).throw(RuntimeError("nope")))
        reply = chat("what is the weather",
                     instant=_fastpath("_try_weather_answer", "72 and clear"))

        assert reply["response"] == "72 and clear"

    def test_a_failing_turn_still_records_and_still_reports_the_error(
            self, chat, feed, monkeypatch):
        def _explode(*a, **k):
            raise RuntimeError("model down")

        monkeypatch.setattr(api_server, "_query_ollama_direct", _explode)
        reply = chat("why is the sea salty")

        assert "Sorry, something went wrong" in reply["response"]
        assert feed.turns(), "the turn happened; the feed should say so"

    def test_recording_outside_a_turn_is_a_no_op(self, feed):
        chat_activity.record_fastpath("_try_weather_answer")
        chat_activity.record_tools(["ha_control_device"])
        chat_activity.record_capability("weather.current")
        assert feed.raw() == ""

    def test_nested_clauses_share_one_turn(self, chat, feed, monkeypatch):
        """A compound turn's clauses re-enter the pipeline; one turn, one write."""
        monkeypatch.setattr(
            api_server, "_query_ollama_direct",
            lambda *a, **k: {"response": "ok", "sources": [], "suggestions": []})

        chat_activity.begin_turn()
        try:
            api_server._process_chat_query("inner clause")
            assert feed.raw() == "", "an inner clause must not write on its own"
            chat_activity.record_capability("weather.current")
        finally:
            chat_activity.end_turn("rag")

        assert [e["agent"] for e in feed.turns()] == ["majordomo"]
