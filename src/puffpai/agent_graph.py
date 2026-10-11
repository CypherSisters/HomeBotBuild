"""
PuffPAI LangGraph Agent

Replaces the hand-rolled regex-based agent loop in assistant_tools.py with a
proper LangGraph StateGraph + ToolNode pipeline.

Architecture:
  START -> agent (LLM decides) -> tools_or_end
    if tool_calls -> tool_node (execute) -> agent (loop)
    if no tool_calls -> END

The agent uses Ollama's OpenAI-compatible chat/completions endpoint with
function calling. If function calling is unavailable (older Ollama), it
falls back to a ReAct-style prompt-based tool selection.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import logging
import re
import os
from typing import Annotated, TypedDict, Sequence, Optional
from dataclasses import dataclass

from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage,
)
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition

logger = logging.getLogger(__name__)


# ── Agent State ─────────────────────────────────────────────────────────────

def _reducer(current: Sequence[BaseMessage],
             update: Sequence[BaseMessage]) -> Sequence[BaseMessage]:
    """Append new messages to the existing sequence."""
    return list(current) + list(update)


class AgentState(TypedDict):
    """State that flows through the agent graph."""
    messages: Annotated[Sequence[BaseMessage], _reducer]


# ── Agent Node (LLM call with tool binding) ─────────────────────────────────

class PuffPAIAgent:
    """LangGraph-based agent for PuffPAI.

    Usage:
        from puffpai.agent_graph import PuffPAIAgent

        agent = PuffPAIAgent(
            tools=assistant_tools_instance.tools,
            ai_name="PuffPAI",
            ollama_url="http://localhost:11434",
            model="gemma3:4b",
        )

        response = agent.run("Turn on the living room lights")
    """

    def __init__(self, tools: list, ai_name: str = "PuffPAI",
                 user_name: str = None,
                 ollama_url: str = None,
                 model: str = None,
                 temperature: float = None,
                 persona: str = None,
                 max_steps: int = 5,
                 backend=None):
        # Pull defaults from central config if not provided
        if ollama_url is None or model is None or temperature is None:
            try:
                from puffpai.config import get_config
                _cfg = get_config()
                if ollama_url is None:
                    ollama_url = _cfg.ollama.url
                if model is None:
                    model = _cfg.ollama.model
                if temperature is None:
                    temperature = _cfg.ollama.temperature
            except Exception:
                # Hard fallback if config unavailable (stock Ollama port)
                ollama_url = ollama_url or "http://localhost:11434"
                model = model or "gemma3:4b"
                temperature = temperature if temperature is not None else 0.6
        self.tools = tools
        self.tool_dict = {t.name: t for t in tools}
        self.ai_name = ai_name
        self.user_name = user_name
        self.ollama_url = ollama_url
        self.model = model
        self.temperature = temperature
        self.max_steps = max_steps
        self._backend = backend  # LLMBackend from config.get_llm_backend()

        # Per-run duplicate-call guard + executed-tool log. Small models
        # loop on the same tool call; without this, every loop iteration
        # re-executes the side effect (duplicate tasks/timers) until the
        # recursion limit kills the run.
        self._seen_calls = set()
        self._executed = []
        # Set when the model has demonstrably finished (two consecutive
        # rounds of only repeats, or the per-turn call budget spent): the
        # next agent round must synthesize a final answer, not see tools.
        self._force_final = False
        self._dup_rounds = 0  # consecutive all-duplicate tool rounds

        # Build system prompt
        self._system_prompt = self._build_system_prompt(persona)

        # Try to use LangChain's ChatOllama with tool binding
        self._llm = self._create_llm()

        # Build the graph
        self._graph = self._build_graph()

    def _backend_chat(self, prompt: str) -> str:
        """Send a prompt to the LLM backend (llama.cpp or Ollama).

        Falls back to direct HTTP if no backend was injected.
        """
        import time as _time
        _t0 = _time.time()
        try:
            if self._backend is not None:
                return self._backend.chat(
                    [{"role": "user", "content": prompt}],
                    temperature=self.temperature,
                )
            # Legacy fallback: direct HTTP to Ollama
            import requests as _requests
            try:
                resp = _requests.post(
                    f"{self.ollama_url}/v1/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": self.temperature,
                        # Cap generation: agent decision rounds don't need
                        # more, and unbounded output was a latency tax
                        "max_tokens": 512,
                    },
                    timeout=60,
                )
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                return f"[Error] LLM request failed: {e}"
        finally:
            # Latency observability: every LLM round is timed so
            # slow turns can be attributed (prefill vs rounds vs tools)
            print(f"[Agent] LLM round took {_time.time() - _t0:.1f}s "
                  f"(prompt {len(prompt):,} chars)")

    def _create_llm(self):
        """Create an LLM with tool-calling capability.

        On Python 3.14+, langchain_core's Pydantic V1 shim warns and
        ``bind_tools`` silently returns a wrapper that does NOT convert
        Ollama tool-call responses back into structured ``tool_calls``
        fields. The model outputs raw JSON into content, the graph's
        ``tools_condition`` sees no tool_calls, and the run ends with
        raw function-call JSON leaking into the user's chat.

        Detect that environment and force the ReAct text-prompt fallback,
        which parses ``TOOL:``/``INPUT:`` markers ourselves and is robust
        to langchain/pydantic version drift.
        """
        import sys
        if sys.version_info >= (3, 14):
            print("[Agent] Python 3.14+ detected — "
                  "bypassing bind_tools (pydantic v1 shim is broken). "
                  "Using ReAct text-prompt tool routing.")
            return None
        try:
            from langchain_ollama import ChatOllama
            from puffpai.inference import model_options
            # Pass the model options, not just model/base_url/temperature.
            # With no num_ctx, ollama sizes the runner from the model's own
            # trained context ("requested context size too large for model
            # num_ctx=262144 n_ctx_train=131072") and starts a second
            # llama-server (for gpt-oss:20b, at -c 131072 for a short
            # prompt). A different context size is a different runner, so
            # every resident model is evicted to make room and the turn pays
            # a cold reload of many seconds. The options come from the SAME
            # config every other call path honours
            # (puffpai.inference.model_options) so the agent path cannot
            # disagree with them.
            llm = ChatOllama(
                model=self.model,
                base_url=self.ollama_url,
                temperature=self.temperature,
                **model_options.langchain_chat_kwargs(),
            )
            # Bind tools for function calling
            return llm.bind_tools(self.tools)
        except (ImportError, Exception) as e:
            print(f"[Agent] ChatOllama with tools failed: {e}")
            print("[Agent] Falling back to ReAct prompt-based tool selection")
            return None

    def _build_system_prompt(self, persona: str = None) -> str:
        """Build the system prompt for the agent.

        If a persona is provided, prepend the agent rules so the persona's
        voice/personality is preserved while tool-use rules still apply.
        """
        from datetime import datetime, timedelta
        user_ctx = f" You are chatting with {self.user_name}." if self.user_name else ""

        today = datetime.now().strftime('%Y-%m-%d')
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        day_name = datetime.now().strftime('%A')

        agent_rules = (
            f"=== ABSOLUTE TOOL-USE RULES (HIGHEST PRIORITY) ===\n"
            f"Today is {day_name}, {today}. Tomorrow is {tomorrow}.\n\n"
            f"RULE 1 — ONE ACTION = ONE TOOL CALL:\n"
            f"  When the user asks for ONE thing, call EXACTLY ONE tool, "
            f"  ONE TIME, then respond. Never call multiple tools in parallel.\n"
            f"  Never call additional 'helpful' tools the user did not ask for.\n\n"
            f"RULE 1B — COMPOUND REQUESTS WITH EXPLICIT 'BUT' / 'AND' / ALSO:\n"
            f"  If the user describes TWO distinct linked actions (e.g.\n"
            f"  'set a recurring event for X at 3pm BUT remind me to leave by 2:30pm'),\n"
            f"  you should call BOTH tools, one after the other, then stop.\n"
            f"  Example: 'pick up Riley every weekday at 3pm but remind me to leave by 2:30pm'\n"
            f"    Step 1: app_add_event(title='Pick up Riley from school', date='today at 3pm',\n"
            f"            recurrence='weekdays')\n"
            f"    Step 2: app_set_alarm(description='Leave to pick up Riley', time='2:30 PM')\n"
            f"    Then stop and confirm both actions.\n"
            f"  Recognize phrases like 'but also', 'and remind me', 'plus', 'as well as'.\n\n"
            f"RULE 2 — STOP IMMEDIATELY AFTER ALL REQUESTED ACTIONS COMPLETE:\n"
            f"  Once every action the user explicitly asked for has succeeded, your\n"
            f"  next response must be natural-language confirmation only — NO\n"
            f"  additional tool calls. Do not 'open the app' or 'fetch related info'.\n\n"
            f"RULE 3 — NEVER CALL THESE UNLESS EXPLICITLY ASKED:\n"
            f"  - auto_daily_briefing  (only if user said 'briefing', 'brief me', 'morning update')\n"
            f"  - auto_weather_forecast (only if user said 'weather', 'temperature', 'forecast',\n"
            f"    'air quality', 'AQI', 'pollution', 'smog' or 'pollen' — it already knows the home\n"
            f"    location, so NEVER ask the user for a city or ZIP)\n"
            f"  - auto_news_summary    (only if user said 'news', 'headlines')\n"
            f"  - app_log_workout      (only if user said 'log workout', 'I worked out')\n"
            f"  - app_create_recipe    (only if user said 'create recipe', 'add recipe')\n\n"
            f"RULE 4 — PICK THE EXACT TOOL FOR THE JOB:\n"
            f"  - 'set a timer' → app_set_timer (ONLY)\n"
            f"  - 'set an alarm' → app_set_alarm (ONLY)\n"
            f"  - RELATIVE requests ('in 45 minutes', 'in 2 hours', 'remind me in 10 min')\n"
            f"    → app_set_timer with that duration, OR app_set_alarm(in_minutes=N).\n"
            f"    NEVER invent a clock time for a relative request.\n"
            f"  - 'add a task/reminder/to-do' → app_add_task (ONLY)\n"
            f"  - 'add an event/appointment/meeting' → app_add_event (ONLY)\n"
            f"  - 'add to shopping/buy' → app_add_shopping (ONLY)\n"
            f"  - 'I bought / I have / pantry' → app_add_pantry (ONLY)\n"
            f"  - 'how many days of food/water do we have', 'do we have\n"
            f"    enough food' → prep_days_remaining (ONLY)\n"
            f"  - 'turn on/off light' → ha_control_device (ONLY)\n"
            f"  - 'dim/brighten light to N%' → ha_set_light (ONLY)\n"
            f"  - 'set thermostat to N' → ha_set_temperature (ONLY)\n"
            f"  - math/arithmetic ('what's 847 times 62', '15% of 80') → calculate (ONLY).\n"
            f"    Pass a pure arithmetic expression: calculate(expression='847*62').\n"
            f"    NEVER use search_knowledge_base for arithmetic — it cannot compute.\n"
            f"  - factual/how-to question (general) → search_knowledge_base (ONLY)\n"
            f"  - 'list my libraries' → list_indexed_libraries (ONLY)\n"
            f"  - 'play trivia', 'quiz me' → app_trivia_question (ONLY)\n"
            f"  - trivia ABOUT A NAMED TOPIC ('trivia about space',\n"
            f"    'quiz me on the Civil War') → app_trivia_generate (ONLY).\n"
            f"    NEVER answer a named topic with app_trivia_question.\n"
            f"  - NEVER print an [answer=...] tag: it is the answer key.\n"
            f"  - ONE tool per request: a timer request never needs calendar\n"
            f"    tools. Never call tools the user did not ask for.\n"
            f"  - If the user NAMES the item ('called morning workout',\n"
            f"    'for the pizza') pass that name as the description.\n\n"
            f"RULE 4B — USER'S PERSONAL/UPLOADED CONTENT:\n"
            f"  When the user references something THEY uploaded or that\n"
            f"  belongs to THEM personally (mom's recipe, my study notes,\n"
            f"  the school flyer I sent you, my history textbook, my excel\n"
            f"  schedule, etc.), you MUST call search_my_documents FIRST\n"
            f"  before answering. NEVER hallucinate content from their\n"
            f"  personal files. If search_my_documents returns no results,\n"
            f"  tell the user the document isn't indexed yet and ask them\n"
            f"  to upload it via the Documents page.\n"
            f"  Phrases that should trigger search_my_documents:\n"
            f"    - 'from my mom/dad/grandma/grandpa/family'\n"
            f"    - 'I uploaded', 'in my file', 'that document', 'my notes'\n"
            f"    - 'my recipe for', 'my schedule', 'my homework'\n"
            f"    - 'find that flyer', 'what was on the [thing I sent]'\n\n"
            f"RULE 4C — VERBATIM TRANSCRIPTION OF DOCUMENT CONTENT:\n"
            f"  When you quote or list content from a user's uploaded\n"
            f"  document (especially recipes from images, handwritten\n"
            f"  notes, or scanned PDFs), you MUST treat the document as\n"
            f"  the source of truth. Specifically:\n"
            f"    - NEVER 'fix' or 'normalize' ingredient quantities. If\n"
            f"      the OCR text says '1 tap brown sugar' do not write\n"
            f"      '1 cup brown sugar' — write what the document says.\n"
            f"    - If a quantity looks garbled (e.g. '34 cups flour'),\n"
            f"      write what's there but flag it: '3-1/2 cups flour\n"
            f"      (OCR may be off — please verify against the original)'.\n"
            f"    - NEVER add ingredients that aren't in the search snippet.\n"
            f"      If the snippet mentions 7 ingredients, list 7. Not 9.\n"
            f"    - NEVER add cooking instructions that aren't literally\n"
            f"      in the snippet. If the user uploaded a photo with\n"
            f"      only ingredients, just list ingredients.\n"
            f"    - When the user asks for the FULL recipe and the\n"
            f"      snippet is partial, call fetch_my_document to get\n"
            f"      the complete text, don't fill in the gaps yourself.\n"
            f"    - ALWAYS tell the user this content came from their\n"
            f"      OCR'd image and may have transcription errors.\n\n"
            f"RULE 5 — IF A TOOL FAILS, DO NOT KEEP RETRYING:\n"
            f"  Try a different tool ONCE, or apologize to the user. "
            f"  Maximum 3 tool calls total per conversation turn.\n\n"
            f"RULE 6 — NEVER INVENT TOOL NAMES.\n"
            f"  Only call tools that are in your bound tool list.\n\n"
            f"RULE 7 — FRESH DATA BEATS MEMORY:\n"
            f"  If a tool result in THIS turn conflicts with anything said\n"
            f"  earlier in the conversation, the fresh tool result is the ONLY\n"
            f"  truth. NEVER reuse numbers (temperatures, prices, counts,\n"
            f"  times) from earlier turns — always re-read them from the\n"
            f"  current tool result. Earlier answers may be outdated or wrong.\n\n"
            f"=== END TOOL-USE RULES ===\n\n"
            f"--- PERSONA ---\n"
        )

        if persona:
            return agent_rules + persona

        # No persona provided — return just the agent rules + minimal identity.
        # We do NOT list every tool name here — the bound tool schemas already
        # tell the LLM what's available. Listing 80 names just bloats context.
        return (
            f"You are {self.ai_name}, a privacy-first AI assistant.{user_ctx}\n\n"
            + agent_rules.replace("--- PERSONA ---\n", "")
        )

    def _build_graph(self) -> StateGraph:
        """Build the LangGraph StateGraph."""
        graph = StateGraph(AgentState)

        # Add nodes — tools go through the dedupe wrapper so a looping
        # model can't execute the same side effect twice in one run.
        self._tool_node = ToolNode(self.tools)
        graph.add_node("agent", self._agent_node)
        graph.add_node("tools", self._tools_node)

        # Add edges
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent",
            tools_condition,  # routes to "tools" if tool_calls, else END
        )
        graph.add_edge("tools", "agent")  # after tools, go back to agent

        return graph.compile()

    @staticmethod
    def _call_key(name, args) -> tuple:
        """Stable identity for a tool call (name + normalized args)."""
        try:
            args_key = json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            args_key = str(args)
        return (name, args_key)

    def _tools_node(self, state: AgentState) -> dict:
        """ToolNode wrapper: executes fresh calls, blocks exact repeats.

        A repeated (name, args) pair within one run gets a ToolMessage
        telling the model the work is already done and to answer now —
        instead of re-running the side effect (which is how 'Wake up at
        3:27 AM' ended up in the calendar four times).
        """
        messages = list(state["messages"])
        last = messages[-1]
        calls = list(getattr(last, "tool_calls", None) or [])

        # Vault boundary: a call whose INPUT is a PIN or whose RESULT is
        # decrypted vault content makes this whole turn unpersistable, and the
        # decision is taken BEFORE the call. The tool body marks for itself,
        # but two paths never reach it -- an argument-schema error whose
        # message echoes the arguments, and a duplicate answered below from the
        # stored prior result -- so the boundary marks for ALL calls, fresh or
        # repeated.
        privacy_refusal = ("I can't do that right now: the guard that keeps a "
                           "vault secret out of the transcript isn't "
                           "answering, so nothing was unlocked.")
        try:
            from puffpai.assistant_tools import (VAULT_PRIVACY_REFUSAL,
                                             note_vault_tool_calls)
            privacy_refusal = VAULT_PRIVACY_REFUSAL
            privacy_ok = bool(note_vault_tool_calls(
                [tc.get("name") for tc in calls]))
        except Exception as exc:
            logger.error("vault privacy boundary failed at tool node: %s", exc)
            privacy_ok = False
        if not privacy_ok:
            # FAIL CLOSED, and this is the only place on this path that CAN.
            # ToolNode runs each tool body on a pool thread, so by the time the
            # tool could refuse for itself the request thread has already moved
            # on. No confirmed mark, no tool call: the model gets a refusal to
            # relay instead of a secret nobody can take back off the disk.
            return {"messages": [
                ToolMessage(content=privacy_refusal,
                            tool_call_id=(tc.get("id")
                                          or f"refused_{tc.get('name')}"),
                            name=tc.get("name") or "unknown")
                for tc in calls]}

        # Observability: the native bind_tools path is otherwise silent about
        # WHICH tool fired (only the ReAct fallback prints "Using tool:"), so a
        # working call can look like it answered from memory in the logs. This
        # guarded print surfaces the tool name(s). It can never affect the run.
        try:
            if calls:
                print("[Agent] tool call: "
                      + ", ".join(str(tc.get("name")) for tc in calls),
                      flush=True)
        except Exception:
            pass

        fresh, dup_msgs = [], []
        for tc in calls:
            key = self._call_key(tc.get("name"), tc.get("args"))
            if key in self._seen_calls:
                dup_msgs.append(ToolMessage(
                    content=(f"DUPLICATE BLOCKED: {tc.get('name')} already "
                             "ran with this exact input this turn; its "
                             "result stands. Do NOT repeat it — finish any "
                             "remaining requested action, or give the user "
                             "your final answer now."),
                    tool_call_id=tc.get("id") or f"dup_{tc.get('name')}",
                    name=tc.get("name") or "unknown",
                ))
            else:
                self._seen_calls.add(key)
                fresh.append(tc)

        out = []
        if fresh:
            if len(fresh) != len(calls):
                try:
                    last = last.model_copy(update={"tool_calls": fresh})
                except AttributeError:  # pydantic v1 fallback
                    last = last.copy(update={"tool_calls": fresh})
            result = self._tool_node.invoke(
                {**state, "messages": messages[:-1] + [last]})
            tool_msgs = result.get("messages", []) if isinstance(result, dict) else []
            for tm in tool_msgs:
                if isinstance(tm, ToolMessage):
                    self._executed.append(
                        (tm.name, str(tm.content)[:160]))
            out.extend(tool_msgs)
        out.extend(dup_msgs)

        # Benchmark 2.8: after all legs succeeded the model kept issuing
        # repeats until the recursion limit killed the run. One round of
        # ONLY repeats may be a mid-task stutter — the blocked notices go
        # back and the model gets one chance to finish a remaining leg.
        # A second in a row means it's stuck, and a run that has spent
        # its whole call budget is done whether the model agrees or not.
        # Either way, the next agent round must produce the answer.
        if calls and not fresh:
            self._dup_rounds += 1
        elif fresh:
            self._dup_rounds = 0
        if self._dup_rounds >= 2 or len(self._seen_calls) >= self.max_steps:
            self._force_final = True
        return {"messages": out}

    def _completed_reminder(self) -> str:
        """RULE 2 restated next to the newest tool results.

        The rule at the top of the system prompt stops registering once
        several ToolMessages sit between it and the model's next token —
        the 2.8 benchmark run ignored it after three successful legs.
        """
        done = ", ".join(name for name, _res in self._executed)
        return (
            f"[Progress check] Tools already run this turn: {done}. "
            f"Every result above is final — never call a tool again with "
            f"the same input. If these results cover everything the user "
            f"asked for, give your final answer in plain language NOW, "
            f"with no further tool calls."
        )

    def executed_tool_names(self) -> list:
        """Tool NAMES invoked during the last :meth:`run`, in order, deduped.

        The Agent Team activity feed attributes a graph turn to each agent whose
        tool actually ran, and this is the only thing it is allowed to see:
        ``self._executed`` also holds a 160-char slice of each tool's RESULT,
        which is reply content and must never reach an on-disk activity event.
        Reset per run alongside ``_executed`` itself.
        """
        out = []
        for name, _res in (self._executed or []):
            if name not in out:
                out.append(name)
        return out

    def _final_answer(self, messages: list) -> str:
        """One synthesis round over the real tool results, no tools bound.

        The results come from the state's ToolMessages (full text), not
        from self._executed (truncated to 160 chars for log/summary use).
        """
        query = next((m.content for m in reversed(messages)
                      if isinstance(m, HumanMessage)), "")
        results = [f"{m.name}: {m.content}" for m in messages
                   if isinstance(m, ToolMessage)
                   and not str(m.content).startswith("DUPLICATE BLOCKED")]
        if not results:
            results = [f"{name}: {res}" for name, res in self._executed]
        answer = self._synthesize(query, results)
        if not answer or answer.startswith("[Error]"):
            answer = "Here's what I found: " + " ".join(results)
        return self._guard_web_denial(answer, messages)

    # ── web-search honesty ─────────────────────────────────────────────
    #
    # The Toolsmith can search, the egress ledger can record real results
    # inside the budget, and the synthesis step can still reply "I can't
    # access the internet right now." The search is not the problem;
    # discarding its results is.
    # Two additions, both here, both leaving every gate alone:
    #   * results are LABELLED as data before the model sees them, so they
    #     read as evidence and not as noise (and so a snippet saying "ignore
    #     previous instructions" arrives fenced);
    #   * a reply that denies internet access while a successful web_search
    #     result sits in the same turn is DISCARDED and replaced with the
    #     results, because it is a false statement about something the ledger
    #     can prove happened.

    @staticmethod
    def _web_search_results_text(messages: list) -> str:
        """The successful web_search tool output in this turn, or ``""``.

        Failures — a shut door, an offline probe, a timeout, an empty result
        set — deliberately do NOT count: if the search did not succeed, a
        reply saying the appliance could not go online may well be true, and
        overriding it would trade one dishonest answer for another.
        """
        try:
            from puffpai import web_search_reply as _wsr
        except Exception:  # pragma: no cover - import guard
            return ""
        chunks = []
        for msg in messages or []:
            if not isinstance(msg, ToolMessage):
                continue
            content = str(getattr(msg, "content", "") or "")
            if _wsr.looks_like_web_results(content):
                chunks.append(content)
        return "\n\n".join(chunks)

    def _guard_web_denial(self, reply: str, messages: list) -> str:
        """Replace a false "I'm offline" answer with the results it ignored."""
        try:
            from puffpai import web_search_reply as _wsr
            evidence = self._web_search_results_text(messages)
            if not evidence or not _wsr.denies_internet(reply):
                return reply
            rendered = _wsr.render_from_tool_text(evidence)
            if not rendered:
                return reply
            print("[Agent] Reply denied internet access while real web_search "
                  "results were in the turn — returning the results instead",
                  flush=True)
            return rendered
        except Exception:  # pragma: no cover - never break a reply
            return reply

    def _agent_node(self, state: AgentState) -> dict:
        """The agent node: calls LLM with conversation + tool results."""
        messages = list(state["messages"])

        # Ensure system prompt is first
        if not messages or not isinstance(messages[0], SystemMessage):
            messages.insert(0, SystemMessage(content=self._system_prompt))

        # Deterministic stop (benchmark 2.8): every distinct call the
        # model asked for has a result, so another tool round can only
        # circle. Answer from what was gathered and end the graph.
        if self._force_final:
            return {"messages": [AIMessage(content=self._final_answer(messages))]}

        if self._llm is not None:
            # Transient reminder — appended to this invocation only,
            # never stored in graph state, so it always sits directly
            # under the newest tool results instead of piling up.
            if self._executed and isinstance(messages[-1], ToolMessage):
                messages.append(
                    SystemMessage(content=self._completed_reminder()))
            try:
                # LangChain tool-calling path
                import time as _time
                _t0 = _time.time()
                response = self._llm.invoke(messages)
                print(f"[Agent] LLM round (bind_tools) took "
                      f"{_time.time() - _t0:.1f}s")
                return {"messages": [response]}
            except Exception as e:
                err = str(e)
                if ("does not support tools" in err
                        or "status code: 400" in err):
                    # Model lacks native tool-calling — permanently
                    # switch this instance to ReAct prompt-based routing.
                    print(f"[Agent] Model lacks native tool support, "
                          f"switching to ReAct: {err[:80]}")
                    self._llm = None
                    return self._react_fallback(messages)
                raise  # propagate unexpected errors
        else:
            # Fallback: ReAct prompt-based path
            return self._react_fallback(messages)

    def _react_fallback(self, messages: list) -> dict:
        """Fallback agent that uses ReAct-style prompting for tool use.

        Used when ChatOllama with function calling isn't available.
        """
        import requests

        # Build the prompt with tool descriptions
        tool_desc = "\n".join(
            f"- {t.name}: {t.description}" for t in self.tools
        )
        tool_names = ", ".join(t.name for t in self.tools)

        # Gather conversation so far
        conv_parts = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                # The transient tool-round reminder is superseded by the
                # done-note below; dumping both would double the guidance.
                if "[Progress check]" in msg.content:
                    continue
                conv_parts.append(f"System: {msg.content}")
            elif isinstance(msg, HumanMessage):
                conv_parts.append(f"User: {msg.content}")
            elif isinstance(msg, AIMessage):
                conv_parts.append(f"Assistant: {msg.content}")
            elif isinstance(msg, ToolMessage):
                conv_parts.append(f"Tool Result ({msg.name}): {msg.content}")

        conversation = "\n".join(conv_parts)

        from datetime import datetime as _dt, timedelta as _td
        _today = _dt.now().strftime('%Y-%m-%d')
        _tomorrow = (_dt.now() + _td(days=1)).strftime('%Y-%m-%d')

        # Same salience fix as the tool-calling path: restate "you are
        # done, answer" next to the decision point once work has run.
        done_note = ""
        if self._executed:
            done_lines = "\n".join(
                f"- {name}: {res}" for name, res in self._executed)
            done_note = (
                f"\nAlready completed this turn (these results are final, "
                f"never repeat them):\n{done_lines}\n"
                f"If they cover everything the user asked for, respond "
                f"with ANSWER: now.\n")

        react_prompt = f"""{conversation}
{done_note}

Today is {_dt.now().strftime('%A')}, {_today}. Tomorrow is {_tomorrow}.

Available tools:
{tool_desc}

VALID TOOL NAMES: {tool_names}

For calendar/task tools, use PIPE-separated values. Example:
TOOL: app_add_task
INPUT: Buy milk|{_tomorrow}|family|all|medium

IMPORTANT: If the user mentions a TIME for an event, you MUST include it!
Use 'next monday at 3pm' or ISO with time like '{_tomorrow}T15:00'.
Example: TOOL: app_add_event / INPUT: Dentist|next friday at 2pm|medical|dad|high

IMPORTANT: For FOOD/GROCERY items, use app_add_pantry (items at home) or app_add_shopping (items to buy). Do NOT use app_add_task for food.
For trivia, use app_trivia_question — or app_trivia_generate when the user names
a topic ("trivia about space"). Do NOT reveal the answer, and NEVER print the
[answer=...] tag.

If you need a tool, respond EXACTLY:
TOOL: tool_name
INPUT: tool_input

If you have enough info to answer, respond EXACTLY:
ANSWER: your final response

Your decision:"""

        # Call LLM via backend or direct HTTP
        try:
            decision = self._backend_chat(react_prompt)
        except Exception as e:
            return {"messages": [AIMessage(
                content=f"I'm having trouble reaching the AI model: {e}")]}

        # Parse TOOL: / INPUT: or ANSWER:
        if "TOOL:" in decision:
            tool_match = re.search(r'TOOL:\s*(\w+)', decision)
            input_match = re.search(r'INPUT:\s*(.*?)(?:\n|$)', decision, re.DOTALL)

            if tool_match:
                tool_name = tool_match.group(1)
                tool_input = input_match.group(1).strip() if input_match else ""

                # Resolve tool name (exact or fuzzy)
                resolved = self._resolve_tool_name(tool_name)
                if resolved:
                    # Same vault boundary as _tools_node, before the duplicate
                    # guard -- which can answer from a stored prior result.
                    privacy_refusal = (
                        "I can't do that right now: the guard that keeps a "
                        "vault secret out of the transcript isn't answering, "
                        "so nothing was unlocked.")
                    try:
                        from puffpai.assistant_tools import (
                            VAULT_PRIVACY_REFUSAL, note_vault_tool_calls)
                        privacy_refusal = VAULT_PRIVACY_REFUSAL
                        privacy_ok = bool(note_vault_tool_calls([resolved]))
                    except Exception as exc:
                        logger.error(
                            "vault privacy boundary failed in ReAct "
                            "fallback: %s", exc)
                        privacy_ok = False
                    if not privacy_ok:
                        return {"messages": [
                            AIMessage(content=privacy_refusal)]}
                    # Duplicate guard — same rationale as _tools_node
                    key = self._call_key(resolved, (tool_input or "").strip())
                    if key in self._seen_calls:
                        prior = next(
                            (res for name, res in reversed(self._executed)
                             if name == resolved), None)
                        summary = (f"Already done — {prior}" if prior
                                   else "That step is already done.")
                        return {"messages": [AIMessage(content=summary)]}
                    try:
                        result = self.tool_dict[resolved].invoke(tool_input)
                        self._seen_calls.add(key)
                        self._executed.append((resolved, str(result)[:160]))
                        # Return as AI message with tool call + tool result
                        ai_msg = AIMessage(
                            content=f"Using tool: {resolved}",
                            tool_calls=[{
                                "name": resolved,
                                "args": {"input": tool_input},
                                "id": f"call_{resolved}",
                            }],
                        )
                        tool_msg = ToolMessage(
                            content=str(result),
                            tool_call_id=f"call_{resolved}",
                            name=resolved,
                        )
                        return {"messages": [ai_msg, tool_msg]}
                    except Exception as e:
                        return {"messages": [AIMessage(
                            content=f"Tool {resolved} failed: {e}")]}
                else:
                    return {"messages": [AIMessage(
                        content=f"Tool '{tool_name}' not found. "
                                f"Available: {tool_names}")]}

        # ANSWER or plain response
        answer = decision
        if "ANSWER:" in decision:
            answer = decision.split("ANSWER:", 1)[1].strip()

        return {"messages": [AIMessage(content=answer)]}

    def _resolve_tool_name(self, name: str) -> Optional[str]:
        """Resolve a tool name with exact match, then fuzzy fallback."""
        if name in self.tool_dict:
            return name

        normalized = name.lower().replace('_', '')
        for valid_name in self.tool_dict:
            if valid_name.lower().replace('_', '') == normalized:
                return valid_name

        # Substring fallback
        for valid_name in self.tool_dict:
            vn = valid_name.replace('_', '')
            if normalized in vn or vn in normalized:
                return valid_name

        return None

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self, query: str, conversation_history: list = None) -> str:
        """Run the agent on a user query.

        This is a drop-in replacement for AssistantTools.run().

        Args:
            query: The user's message.
            conversation_history: List of {"user": ..., "assistant": ...} dicts.

        Returns:
            The agent's final text response.
        """
        messages = []

        # Fresh duplicate-guard state for this run
        self._seen_calls = set()
        self._executed = []
        self._force_final = False
        self._dup_rounds = 0

        # Add conversation history as messages
        if conversation_history:
            recent = conversation_history[-5:]
            for msg in recent:
                messages.append(HumanMessage(content=msg['user']))
                asst = msg['assistant']
                limit = 800 if msg == recent[-1] else 400
                truncated = asst[:limit] + "..." if len(asst) > limit else asst
                messages.append(AIMessage(content=truncated))

        # Add current query
        messages.append(HumanMessage(content=query))

        # Run the graph with step limit. max_steps tool rounds cost two
        # graph steps each (agent + tools); the +4 leaves room for two
        # all-duplicate rounds (stutter + stuck) and the forced-synthesis
        # round after them. With the _force_final backstop, circling ends
        # in synthesis before this limit can fire — it remains as a
        # fail-fast guard only.
        config = {"recursion_limit": max(8, 2 * self.max_steps + 4)}

        try:
            result = self._graph.invoke(
                {"messages": messages},
                config=config,
            )

            # Extract the final AI message
            final_messages = result.get("messages", [])
            for msg in reversed(final_messages):
                if isinstance(msg, AIMessage) and msg.content:
                    # Skip messages that are just "Using tool: ..."
                    if not msg.content.startswith("Using tool:"):
                        return self._guard_web_denial(
                            self._clean_for_voice(msg.content),
                            final_messages)

            # Fallback: synthesize from tool results
            tool_results = [
                msg.content for msg in final_messages
                if isinstance(msg, ToolMessage)
            ]
            if tool_results:
                return self._guard_web_denial(
                    self._synthesize(query, tool_results), final_messages)

            return "I wasn't able to determine the right approach. Could you rephrase?"

        except Exception as e:
            err = str(e)

            # Recursion limit: the model went in circles. Thanks to the
            # duplicate guard nothing ran twice — report what DID happen
            # in plain speech instead of surfacing a jargon error, and do
            # NOT fall through to the legacy loop (double execution).
            if ('recursion limit' in err.lower()
                    or type(e).__name__ == 'GraphRecursionError'):
                print(f"[Agent] Recursion limit hit: {err[:120]}")
                if self._executed:
                    done = "; ".join(
                        res for _name, res in self._executed[-3:])
                    return self._clean_for_voice(
                        f"Here's what I completed: {done}. I started going "
                        f"in circles after that, so I stopped rather than "
                        f"repeat anything. If something's still missing, "
                        f"ask me for just that piece.")
                return ("That request sent me in circles, so I stopped "
                        "before doing anything twice. Could you say it "
                        "again as one specific instruction?")

            print(f"[Agent] Graph execution error: {e}")
            # Re-raise model-compatibility errors so the caller
            # (AssistantTools.run) can fall back to the legacy loop.
            if ("does not support tools" in err
                    or "status code: 400" in err):
                raise
            return f"I ran into an issue processing that: {e}"

    def _synthesize(self, query: str, tool_results: list) -> str:
        """Synthesize tool results into a natural spoken response."""
        raw_text = "\n".join(tool_results)
        results_text = raw_text
        # Web search results are labelled as DATA before the model reads them
        # — and the label carries the fact the model keeps forgetting: the
        # search already happened, in this turn, successfully. Without it the
        # box produced "I can't access the internet right now" over a real
        # 19 KB result set. This changes what the model is
        # TOLD, never what it is allowed to reach.
        data_note = ""
        try:
            from puffpai import web_search_reply as _wsr
            if _wsr.looks_like_web_results(results_text):
                results_text = _wsr.label_as_data(results_text)
                data_note = _wsr.DATA_NOTE + "\n\n"
        except Exception:  # pragma: no cover - never break synthesis
            data_note = ""
        prompt = (
            f"You are {self.ai_name}, a helpful AI assistant. "
            f"Present these results clearly.\n\n"
            f"{data_note}"
            f"FORMATTING: Default to natural conversational sentences. "
            f"However, if the user explicitly asks for specific formatting "
            f"(bullet points, numbered lists, alphabetical order, etc.), "
            f"use that format.\n\n"
            f"User question: {query}\n\n"
            f"Tool results:\n{results_text}\n\n"
            f"Your spoken response:"
        )

        try:
            answer = self._backend_chat(prompt)
        except Exception:
            # The model is unreachable; hand back the results themselves —
            # unfenced, because this string goes to a person, not a model.
            return raw_text
        # The same guard as the graph's return paths, applied here too: this
        # method is also called directly (run()'s no-AIMessage fallback), and
        # a denial written over real results is false wherever it is written.
        try:
            from puffpai import web_search_reply as _wsr
            if _wsr.looks_like_web_results(raw_text) and _wsr.denies_internet(answer):
                rendered = _wsr.render_from_tool_text(raw_text)
                if rendered:
                    return rendered
        except Exception:  # pragma: no cover - never break synthesis
            pass
        return answer

    def _clean_for_voice(self, text: str) -> str:
        """Light cleanup for voice-friendly output."""
        # Remove markdown artifacts
        text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
        text = re.sub(r'#{1,6}\s*', '', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.MULTILINE)
        return text.strip()
