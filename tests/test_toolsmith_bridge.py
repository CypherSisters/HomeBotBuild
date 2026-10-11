"""``registry_read`` — the bridge from a planned step to the legacy tool tier.

A generic dispatcher is only admissible because of what it refuses, so this file
is mostly refusals. Every fixture below is a REAL registry capability, not a
mock: the point is that the live registry's own declarations are what gate the
bridge, and a future edit to those declarations changes the behaviour tested
here (which is the intended coupling).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

import pytest

from puffpai import capability_registry as reg
from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.toolsmith import (
    BRIDGE_DENIED_CAPS,
    BRIDGE_MAX_OUTPUT,
    STATE_CHANGING_INTENTS,
    TOOL_INTENTS,
    ToolsmithAgent,
)


class FakeTool:
    """Stands in for a LangChain tool: a name and an ``invoke``."""

    def __init__(self, text="tool output", boom=False):
        self.text, self.boom, self.calls = text, boom, []

    def invoke(self, arg):
        self.calls.append(arg)
        if self.boom:
            raise RuntimeError("tool exploded")
        return self.text


def agent_with(**tools):
    return ToolsmithAgent(tool_source=dict(tools))


def env(tool, query=None, *, state_changing=False):
    payload = {"tool": tool}
    if query is not None:
        payload["query"] = query
    return Envelope(intent="registry_read", payload=payload,
                    budget=Budget(max_actions=4, max_seconds=30.0,
                                  allow_egress=False),
                    state_changing=state_changing, origin_agent="concierge")


# ── shape ───────────────────────────────────────────────────────────

def test_bridge_is_known_and_is_not_state_changing():
    """A state-changing bridge would reach an actuator without the Steward's
    entity resolution or Drawbridge's review."""
    assert "registry_read" in TOOL_INTENTS
    assert "registry_read" not in STATE_CHANGING_INTENTS


def test_the_registry_declares_the_bridge_itself():
    cap = reg.get("utility.registry_read")
    assert cap is not None and cap.agent == "toolsmith"
    assert cap.access == "read"


# ── it works ────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool_name,cap_id", [
    ("search_knowledge_base", "knowledge.library"),    # archivist, read
    ("search_my_documents", "knowledge.documents"),    # archivist, read
    ("auto_news_summary", "news.headlines"),           # majordomo, read
    ("auto_weather_forecast", "weather.current"),      # majordomo, read
])
def test_read_only_information_tools_are_reachable(tool_name, cap_id):
    fake = FakeTool("the answer")
    reply = agent_with(**{tool_name: fake}).handle(env(tool_name, "q"))
    assert reply["ok"] is True, reply
    assert reply["text"] == "the answer"
    assert reply["capability"] == cap_id
    assert reply["untrusted"] is True
    assert fake.calls == ["q"]


def test_a_missing_query_still_invokes_with_empty_string():
    fake = FakeTool()
    assert agent_with(auto_news_summary=fake).handle(
        env("auto_news_summary"))["ok"] is True
    assert fake.calls == [""]


# ── refusals: the whole safety argument ─────────────────────────────

def test_unknown_tool_never_reaches_the_process():
    """An invented name is refused before any live lookup."""
    a = agent_with(not_a_real_tool=FakeTool())
    reply = a.handle(env("not_a_real_tool"))
    assert reply["ok"] is False
    assert "registry" in reply["error"]


@pytest.mark.parametrize("tool_name", ["read_file", "workspace_exec",
                                       "app_trivia_question"])
def test_write_capable_tools_are_refused(tool_name):
    """utility.files / utility.exec / learning.trivia are read-write."""
    reply = agent_with(**{tool_name: FakeTool()}).handle(env(tool_name))
    assert reply["ok"] is False and reply["refused"] is True
    assert "read-only" in reply["error"] or "state-changer" in reply["error"]


def test_a_read_tool_outside_the_information_domain_is_refused():
    """cameras.alerts is READ but steward-owned — access alone is not enough."""
    assert reg.get("cameras.alerts").access == "read"
    reply = agent_with(camera_recent_alerts=FakeTool()).handle(
        env("camera_recent_alerts"))
    assert reply["ok"] is False and reply["refused"] is True
    assert "steward" in reply["error"]


@pytest.mark.parametrize("tool_name,cap_id", [
    ("vault_list", "vault.secrets"),
    ("vault_display_document", "vault.display"),
])
def test_the_vault_is_denied_by_id_not_by_access(tool_name, cap_id):
    """Both vault caps are READ and archivist-owned, so they would pass the
    access and domain gates. Denying by id is what actually stops them, and it
    keeps stopping them if someone later edits their access level."""
    cap = reg.get(cap_id)
    assert cap.access == "read" and cap.agent == "archivist"
    assert cap_id in BRIDGE_DENIED_CAPS
    reply = agent_with(**{tool_name: FakeTool()}).handle(env(tool_name))
    assert reply["ok"] is False and reply["refused"] is True
    assert "vault" in reply["error"]


def test_the_bridge_cannot_invoke_itself():
    reply = agent_with(registry_read=FakeTool()).handle(env("registry_read"))
    assert reply["ok"] is False and reply["refused"] is True
    assert "itself" in reply["error"]


@pytest.mark.parametrize("bad", [None, "", "   ", 7])
def test_tool_name_must_be_a_non_empty_string(bad):
    payload = {} if bad is None else {"tool": bad}
    reply = ToolsmithAgent(tool_source={}).handle(Envelope(
        intent="registry_read", payload=payload,
        budget=Budget(max_actions=2, max_seconds=30.0, allow_egress=False),
        state_changing=False, origin_agent="concierge"))
    assert reply["ok"] is False


def test_declared_but_not_live_is_reported_cleanly():
    reply = agent_with().handle(env("auto_news_summary"))
    assert reply["ok"] is False and "not live" in reply["error"]


# ── behaviour under failure ─────────────────────────────────────────

def test_a_failing_tool_is_a_result_not_an_exception():
    reply = agent_with(auto_news_summary=FakeTool(boom=True)).handle(
        env("auto_news_summary"))
    assert reply["ok"] is False and "failed" in reply["error"]


def test_output_is_capped():
    fake = FakeTool("x" * (BRIDGE_MAX_OUTPUT + 5000))
    reply = agent_with(auto_news_summary=fake).handle(env("auto_news_summary"))
    assert reply["truncated"] is True
    assert len(reply["text"]) == BRIDGE_MAX_OUTPUT


def test_every_denied_cap_is_actually_in_the_registry():
    """A typo in BRIDGE_DENIED_CAPS would silently deny nothing."""
    for cap_id in BRIDGE_DENIED_CAPS:
        assert reg.get(cap_id) is not None, cap_id
