"""The agent stops cleanly instead of hitting the recursion limit.

A multi-leg request ("Look up how long canned beans last, then check if any
of our supplies expire this month") can have every leg succeed and then the
model keeps re-issuing tool calls. The duplicate guard blocks the repeats,
but the graph still needs one more agent round to answer, and without a
forced stop the recursion limit fires first ("[Agent] Recursion limit hit"):
LLM rounds wasted and the user gets the going-in-circles apology instead of
the answer the tools had already gathered.

What src/puffpai/agent_graph.py does now, pinned here:

  * TWO CONSECUTIVE tool rounds consisting only of duplicate calls set
    _force_final — the next agent round synthesizes the final answer from
    the real tool results (no tools bound) and the graph ends, never the
    recursion limit. A single all-duplicate round is treated as a
    mid-task stutter: the blocked notices go back and the model gets one
    chance to finish any remaining leg;
  * spending the whole max_steps distinct-call budget forces the same stop;
  * a transient "[Progress check]" system note sits directly under the
    newest tool results each round, restating RULE 2 where the model can
    actually see it, so a well-behaved model answers one round earlier.

No live LLM: the tool-bound LLM is a scripted stand-in driving the real
compiled StateGraph, and synthesis goes through an injected fake backend.

Run: pytest tests/test_agent_stop_regressions.py -q
"""

import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from langchain_core.messages import (  # noqa: E402
    AIMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.tools import tool  # noqa: E402


QUERY = ("Look up how long canned beans last, then check if any of our "
         "supplies expire this month")

# Deliberately longer than the 160-char _executed log truncation: the
# synthesis prompt must carry the FULL ToolMessage text, not the log line.
BEANS = ("Canned beans last 2-5 years past the printed date when stored "
         "cool and dry; low-acid canned goods keep their quality longest, "
         "and USDA guidance says they remain safe well beyond the date as "
         "long as the can is intact and undamaged.")
assert len(BEANS) > 160


def _make_tools(counter):
    """The three benchmark-2.8 legs, each counting its executions."""

    @tool
    def search_knowledge_base(query: str) -> str:
        """Search the offline knowledge base."""
        counter['search_knowledge_base'] += 1
        return BEANS

    @tool
    def web_search(query: str) -> str:
        """Search the web."""
        counter['web_search'] += 1
        return "Consensus across sources: 2-5 years for canned beans."

    @tool
    def prep_check_expiration(dummy: str = "") -> str:
        """Check emergency supplies for upcoming expirations."""
        counter['prep_check_expiration'] += 1
        return ("1 item expires this month: water purification tablets "
                "(08/2026).")

    return [search_knowledge_base, web_search, prep_check_expiration]


def _leg(name, args, call_id):
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": call_id}])


def _script_2_8():
    return [
        _leg("search_knowledge_base",
             {"query": "canned beans shelf life"}, "c1"),
        _leg("web_search",
             {"query": "how long do canned beans last"}, "c2"),
        _leg("prep_check_expiration", {"dummy": ""}, "c3"),
    ]


def _circle(n):
    """The benchmark's circling: leg 1 again, under a fresh call id.

    The fresh id is the point — the duplicate guard keys on name+args and
    a new id must not defeat it.
    """
    return _leg("search_knowledge_base",
                {"query": "canned beans shelf life"}, f"loop_{n}")


class ScriptedLLM:
    """Stands in for the tool-bound ChatOllama: replays a decision script.

    When the script runs out, repeat_factory(n) supplies the next response
    (the circling model); with no factory, being consulted again is a
    test failure.
    """

    def __init__(self, script, repeat_factory=None):
        self._script = list(script)
        self._repeat_factory = repeat_factory
        self._n = 0
        self.rounds = []  # snapshot of the messages seen by each invoke

    def invoke(self, messages):
        self.rounds.append(list(messages))
        if self._script:
            return self._script.pop(0)
        if self._repeat_factory is not None:
            self._n += 1
            return self._repeat_factory(self._n)
        raise AssertionError("LLM consulted after its script ended")


class FakeBackend:
    """LLMBackend stand-in capturing every synthesis prompt."""

    def __init__(self, reply="Beans keep 2-5 years, and your water "
                             "purification tablets expire this month."):
        self.prompts = []
        self.reply = reply

    def chat(self, messages, temperature=None):
        self.prompts.append(messages[0]["content"])
        return self.reply


def _build_agent(script, repeat_factory=None, backend=None, max_steps=5):
    from puffpai.agent_graph import PuffPAIAgent
    counter = Counter()
    agent = PuffPAIAgent(
        tools=_make_tools(counter),
        ollama_url="http://ollama.invalid:11434",
        model="scripted",
        temperature=0.0,
        max_steps=max_steps,
        backend=backend,
    )
    # The graph reads self._llm at call time, so the stand-in slots in
    # after __init__ regardless of what _create_llm decided locally.
    agent._llm = ScriptedLLM(script, repeat_factory)
    return agent, agent._llm, counter


def _progress_notes(messages):
    return [m for m in messages if isinstance(m, SystemMessage)
            and "[Progress check]" in m.content]


# ═══════════════════════════════════════════════════════════════════════════
#  The benchmark shape: three legs, then a model that circles forever
# ═══════════════════════════════════════════════════════════════════════════

class TestBenchmark28StopsCleanly:

    @pytest.fixture
    def ran(self):
        backend = FakeBackend()
        agent, llm, counter = _build_agent(
            _script_2_8(), repeat_factory=_circle, backend=backend)
        result = agent.run(QUERY)
        return result, agent, llm, counter, backend

    def test_answer_is_the_synthesis_not_the_circles_apology(self, ran):
        result, _agent, _llm, _counter, backend = ran
        assert result == backend.reply
        assert "circles" not in result.lower()

    def test_each_leg_executed_exactly_once(self, ran):
        _result, _agent, _llm, counter, _backend = ran
        assert counter == {"search_knowledge_base": 1, "web_search": 1,
                           "prep_check_expiration": 1}

    def test_llm_rounds_stop_at_the_second_consecutive_duplicate(self, ran):
        """3 legs + 2 circling decisions (the first duplicate round is a
        forgiven stutter, the second proves the model is stuck); the
        forced answer round goes through the backend, so a model that
        would circle forever is consulted exactly 5 times."""
        _result, _agent, llm, _counter, _backend = ran
        assert len(llm.rounds) == 5

    def test_synthesis_sees_query_and_full_results(self, ran):
        _result, _agent, _llm, _counter, backend = ran
        assert len(backend.prompts) == 1
        prompt = backend.prompts[0]
        assert QUERY in prompt
        assert BEANS in prompt  # full text, not the 160-char log line
        assert "water purification tablets" in prompt
        assert "Consensus across sources" in prompt
        assert "DUPLICATE BLOCKED" not in prompt

    def test_progress_note_sits_under_newest_results_each_round(self, ran):
        _result, _agent, llm, _counter, _backend = ran
        assert _progress_notes(llm.rounds[0]) == []
        for later_round in llm.rounds[1:]:
            notes = _progress_notes(later_round)
            # Transient: exactly one note, never an accumulated pile.
            assert len(notes) == 1
            assert later_round[-1] is notes[0]
            assert "search_knowledge_base" in notes[0].content
        assert "web_search" in _progress_notes(llm.rounds[2])[0].content

    def test_recursion_limit_never_fires(self, capsys):
        agent, _llm, _counter = _build_agent(
            _script_2_8(), repeat_factory=_circle, backend=FakeBackend())
        agent.run(QUERY)
        assert "Recursion limit hit" not in capsys.readouterr().out

    def test_a_second_run_on_the_same_agent_starts_fresh(self, ran):
        """run() must reset the forced stop along with the dup guard."""
        _result, agent, llm, counter, backend = ran
        llm._script = [_leg("prep_check_expiration", {"dummy": ""}, "r1"),
                       AIMessage(content="Tablets expire this month.")]
        llm._repeat_factory = None
        assert agent.run("anything expiring?") == "Tablets expire this month."
        assert counter["prep_check_expiration"] == 2
        assert len(backend.prompts) == 1  # no new forced synthesis


# ═══════════════════════════════════════════════════════════════════════════
#  A round that mixes one repeat with genuinely new work must not stop
# ═══════════════════════════════════════════════════════════════════════════

class TestMixedRoundStillProgresses:

    def test_partial_duplicate_does_not_force_the_stop(self):
        backend = FakeBackend()
        script = [
            _leg("search_knowledge_base", {"query": "beans"}, "m1"),
            AIMessage(content="", tool_calls=[
                {"name": "search_knowledge_base", "args": {"query": "beans"},
                 "id": "m2"},
                {"name": "web_search", "args": {"query": "beans"},
                 "id": "m3"},
            ]),
            AIMessage(content="Beans last two to five years."),
        ]
        agent, _llm, counter = _build_agent(script, backend=backend)
        result = agent.run("how long do beans last?")
        assert result == "Beans last two to five years."
        assert counter["search_knowledge_base"] == 1
        assert counter["web_search"] == 1
        assert backend.prompts == []  # the model answered on its own

    def test_single_stutter_gets_one_recovery_chance(self):
        """One all-duplicate round mid-chain must NOT end the turn, the
        remaining leg still runs, rather than forcing synthesis on the
        first duplicate round."""
        backend = FakeBackend()
        script = [
            _leg("search_knowledge_base", {"query": "beans"}, "t1"),
            _leg("search_knowledge_base", {"query": "beans"}, "t2"),
            _leg("web_search", {"query": "beans"}, "t3"),
            AIMessage(content="Recovered and finished."),
        ]
        agent, _llm, counter = _build_agent(script, backend=backend)
        result = agent.run("look up beans, then search the web")
        assert result == "Recovered and finished."
        assert counter["search_knowledge_base"] == 1
        assert counter["web_search"] == 1
        assert backend.prompts == []

    def test_non_consecutive_stutters_do_not_force_the_stop(self):
        """A fresh round resets the stuck counter: stutter, progress,
        stutter again is clumsy but still the model's own turn to end."""
        backend = FakeBackend()
        script = [
            _leg("search_knowledge_base", {"query": "beans"}, "n1"),
            _leg("search_knowledge_base", {"query": "beans"}, "n2"),
            _leg("web_search", {"query": "beans"}, "n3"),
            _leg("web_search", {"query": "beans"}, "n4"),
            AIMessage(content="Clumsy but done."),
        ]
        agent, _llm, counter = _build_agent(script, backend=backend)
        result = agent.run("look up beans, then search the web")
        assert result == "Clumsy but done."
        assert counter["search_knowledge_base"] == 1
        assert counter["web_search"] == 1
        assert backend.prompts == []


# ═══════════════════════════════════════════════════════════════════════════
#  The distinct-call budget (max_steps) is the other deterministic stop
# ═══════════════════════════════════════════════════════════════════════════

class TestCallBudgetForcesFinal:

    def test_budget_spent_means_synthesis_not_a_sixth_round(self):
        backend = FakeBackend(reply="Budget spent; here is everything.")
        script = [_leg("search_knowledge_base", {"query": f"q{i}"}, f"b{i}")
                  for i in range(5)]
        agent, llm, counter = _build_agent(script, backend=backend)
        result = agent.run("research five things")
        assert result == "Budget spent; here is everything."
        assert counter["search_knowledge_base"] == 5
        assert len(llm.rounds) == 5  # no sixth decision round
        assert len(backend.prompts) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  The ordinary single-action turn is untouched
# ═══════════════════════════════════════════════════════════════════════════

class TestSingleActionUnaffected:

    def test_one_leg_then_answer_never_reaches_forced_synthesis(self):
        backend = FakeBackend()
        script = [
            _leg("prep_check_expiration", {"dummy": ""}, "s1"),
            AIMessage(content="Your tablets expire this month."),
        ]
        agent, llm, counter = _build_agent(script, backend=backend)
        result = agent.run("anything expiring?")
        assert result == "Your tablets expire this month."
        assert counter["prep_check_expiration"] == 1
        assert backend.prompts == []
        assert _progress_notes(llm.rounds[0]) == []
        assert len(_progress_notes(llm.rounds[1])) == 1


# ═══════════════════════════════════════════════════════════════════════════
#  ReAct text-prompt path: the completed-work note reaches the prompt
# ═══════════════════════════════════════════════════════════════════════════

class TestReactPathCarriesTheNote:
    """The graph ends a ReAct round at the ToolMessage (tools_condition
    inspects only the last message), so the note is pinned by driving
    _react_fallback directly, the way a looping wiring would."""

    def test_second_decision_prompt_lists_completed_work(self):
        prompts = []

        class ReactBackend:
            def chat(self, messages, temperature=None):
                prompts.append(messages[0]["content"])
                if len(prompts) == 1:
                    return "TOOL: search_knowledge_base\nINPUT: beans"
                return "ANSWER: Beans last years."

        from puffpai.agent_graph import PuffPAIAgent
        counter = Counter()
        agent = PuffPAIAgent(
            tools=_make_tools(counter),
            ollama_url="http://ollama.invalid:11434",
            model="scripted", temperature=0.0, backend=ReactBackend())
        agent._llm = None  # force the ReAct text-prompt path
        agent._seen_calls, agent._executed = set(), []

        first = agent._react_fallback([HumanMessage(content="beans?")])
        assert counter["search_knowledge_base"] == 1
        assert "Already completed this turn" not in prompts[0]

        second = agent._react_fallback(
            [HumanMessage(content="beans?")] + list(first["messages"]))
        assert second["messages"][-1].content == "Beans last years."
        assert "Already completed this turn" in prompts[1]
        assert "search_knowledge_base" in prompts[1]
