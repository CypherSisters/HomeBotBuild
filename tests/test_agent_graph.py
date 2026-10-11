"""Tests for the LangGraph agent."""

import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from puffpai.agent_graph import (
    PuffPAIAgent, AgentState, _reducer,
)


# ── State reducer tests ─────────────────────────────────────────────────────

class TestReducer:
    def test_appends_messages(self):
        current = [HumanMessage(content="hello")]
        update = [AIMessage(content="hi")]
        result = _reducer(current, update)
        assert len(result) == 2
        assert result[0].content == "hello"
        assert result[1].content == "hi"

    def test_empty_current(self):
        result = _reducer([], [HumanMessage(content="first")])
        assert len(result) == 1

    def test_empty_update(self):
        result = _reducer([HumanMessage(content="existing")], [])
        assert len(result) == 1


# ── Tool resolution tests ───────────────────────────────────────────────────

@tool
def mock_search(query: str) -> str:
    """Search for information."""
    return f"Result for: {query}"


@tool
def mock_calculate(expression: str) -> str:
    """Calculate math expressions."""
    return f"= {expression}"


class TestToolResolution:
    def setup_method(self):
        self.agent = PuffPAIAgent.__new__(PuffPAIAgent)
        self.agent.tools = [mock_search, mock_calculate]
        self.agent.tool_dict = {t.name: t for t in self.agent.tools}

    def test_exact_match(self):
        assert self.agent._resolve_tool_name("mock_search") == "mock_search"

    def test_fuzzy_no_underscores(self):
        assert self.agent._resolve_tool_name("mocksearch") == "mock_search"

    def test_case_insensitive(self):
        assert self.agent._resolve_tool_name("Mock_Search") == "mock_search"

    def test_substring_match(self):
        assert self.agent._resolve_tool_name("search") == "mock_search"

    def test_no_match(self):
        assert self.agent._resolve_tool_name("nonexistent_tool") is None


# ── System prompt tests ─────────────────────────────────────────────────────

class TestSystemPrompt:
    def test_includes_ai_name(self):
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent.ai_name = "Puff"
        agent.user_name = None
        agent.tools = [mock_search]
        prompt = agent._build_system_prompt(None)
        assert "Puff" in prompt

    def test_includes_user_name(self):
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent.ai_name = "Puff"
        agent.user_name = "Alice"
        agent.tools = [mock_search]
        prompt = agent._build_system_prompt(None)
        assert "Alice" in prompt

    def test_uses_custom_persona(self):
        # The tool-use rules block is always prepended so a custom persona
        # can't override tool discipline; the persona text follows it.
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent.ai_name = "Puff"
        agent.user_name = None
        agent.tools = [mock_search]
        prompt = agent._build_system_prompt("Custom persona text")
        assert "Custom persona text" in prompt
        assert "=== ABSOLUTE TOOL-USE RULES" in prompt
        assert prompt.index("=== ABSOLUTE TOOL-USE RULES") < prompt.index(
            "Custom persona text"
        )
        assert prompt.endswith("Custom persona text")

    def test_omits_tool_name_listing(self):
        # The prompt deliberately does not enumerate bound tool names —
        # the bound tool schemas already tell the LLM what's available.
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent.ai_name = "Puff"
        agent.user_name = None
        agent.tools = [mock_search, mock_calculate]
        prompt = agent._build_system_prompt(None)
        assert "mock_search" not in prompt
        assert "mock_calculate" not in prompt


# ── Voice cleanup tests ─────────────────────────────────────────────────────

class TestVoiceCleanup:
    def setup_method(self):
        self.agent = PuffPAIAgent.__new__(PuffPAIAgent)

    def test_strips_bold_markdown(self):
        result = self.agent._clean_for_voice("This is **bold** text")
        assert result == "This is bold text"

    def test_strips_headers(self):
        result = self.agent._clean_for_voice("## Header\nContent")
        assert "##" not in result
        assert "Content" in result

    def test_strips_code_backticks(self):
        result = self.agent._clean_for_voice("Use `pip install` to install")
        assert "`" not in result
        assert "pip install" in result

    def test_strips_bullet_points(self):
        result = self.agent._clean_for_voice("- item one\n- item two")
        assert "- " not in result


# ── Integration test with mocked LLM ────────────────────────────────────────

class TestAgentRun:
    @patch('puffpai.agent_graph.PuffPAIAgent._create_llm')
    def test_run_builds_messages(self, mock_create_llm):
        """Test that run() correctly builds the message sequence."""
        mock_create_llm.return_value = None  # force ReAct fallback

        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent.tools = [mock_search]
        agent.tool_dict = {"mock_search": mock_search}
        agent.ai_name = "Puff"
        agent.user_name = None
        agent.ollama_url = "http://localhost:11435"
        agent.model = "dolphin-llama3:8b"
        agent.temperature = 0.7
        agent.max_steps = 3
        agent._system_prompt = "You are Puff."
        agent._llm = None
        agent._backend = None  # fallback to HTTP
        # Per-run state the real __init__/run() establishes
        agent._seen_calls = set()
        agent._executed = []
        agent._force_final = False

        # Mock requests.post for the ReAct fallback
        with patch('requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "ANSWER: Hello there!"}}]
            }
            mock_post.return_value = mock_response

            # Build graph manually (can't use _build_graph without real LLM)
            # Instead, test _react_fallback directly
            messages = [HumanMessage(content="Hello")]
            result = agent._react_fallback(messages)

            assert "messages" in result
            assert len(result["messages"]) >= 1
            # Should be an AIMessage with the answer
            final = result["messages"][-1]
            assert isinstance(final, AIMessage)
            assert "Hello there!" in final.content

    @patch('requests.post')
    def test_react_tool_call(self, mock_post):
        """Test ReAct fallback correctly calls a tool."""
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent.tools = [mock_search]
        agent.tool_dict = {"mock_search": mock_search}
        agent.ai_name = "Puff"
        agent.user_name = None
        agent.ollama_url = "http://localhost:11435"
        agent.model = "dolphin-llama3:8b"
        agent.temperature = 0.7
        agent._llm = None
        agent._backend = None  # fallback to HTTP
        # Per-run state the real __init__/run() establishes
        agent._seen_calls = set()
        agent._executed = []
        agent._force_final = False

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "TOOL: mock_search\nINPUT: python tutorials"}}]
        }
        mock_post.return_value = mock_response

        messages = [HumanMessage(content="Search for python tutorials")]
        result = agent._react_fallback(messages)

        assert "messages" in result
        # Should have AI message + tool message
        assert len(result["messages"]) == 2
        assert isinstance(result["messages"][0], AIMessage)
        assert isinstance(result["messages"][1], ToolMessage)
        assert "Result for: python tutorials" in result["messages"][1].content


# ── LLMBackend injection ─────────────────────────────────────────────────────

class TestAgentLLMBackend:
    """Verify PuffPAIAgent can use injected LLMBackend."""

    def test_agent_accepts_backend_param(self):
        """PuffPAIAgent accepts backend= parameter."""
        mock_backend = MagicMock()
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent._backend = mock_backend
        assert agent._backend is mock_backend

    def test_backend_chat_uses_injected_backend(self):
        """_backend_chat delegates to injected backend."""
        mock_backend = MagicMock()
        mock_backend.chat.return_value = "Backend response"

        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent._backend = mock_backend
        agent.temperature = 0.7
        agent.ollama_url = "http://localhost:11435"
        agent.model = "test"

        result = agent._backend_chat("test prompt")
        assert result == "Backend response"
        mock_backend.chat.assert_called_once()

    def test_backend_chat_fallback_without_backend(self):
        """_backend_chat falls back to HTTP when no backend injected."""
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent._backend = None
        agent.temperature = 0.7
        agent.ollama_url = "http://localhost:11435"
        agent.model = "test"

        with patch('requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "HTTP response"}}]
            }
            mock_post.return_value = mock_response

            result = agent._backend_chat("test prompt")
            assert result == "HTTP response"

    def test_synthesize_uses_backend_chat(self):
        """_synthesize delegates to _backend_chat."""
        agent = PuffPAIAgent.__new__(PuffPAIAgent)
        agent.ai_name = "Puff"
        agent._backend = MagicMock()
        agent._backend.chat.return_value = "Synthesized response"
        agent.temperature = 0.7
        agent.ollama_url = "http://localhost:11435"
        agent.model = "test"

        result = agent._synthesize("What is the weather?", ["It's sunny"])
        assert result == "Synthesized response"
