"""Hidden reasoning is not one switch, and the trivia writer's budget must fit
the reasoning a harmony model does anyway.

Why a single boolean is not enough:

* A trivia writer with a 300-token budget can spend all of it on hidden
  reasoning, leaving no question, so the bank fallback answers instead.
* Sending ``"think": False`` on every raw ``/api/chat`` payload does not stop
  that on every model: the journal still shows ``eval time = 15265.26 ms /
  300 tokens`` -- exactly the cap, no natural stop -- while a plain weather
  answer on the same model stops naturally at 180. ``gpt-oss:20b`` is a
  harmony-format model whose ``think`` parameter is a reasoning EFFORT LEVEL
  (``"low"``/``"medium"``/``"high"``); a boolean is ignored outright.

So the assertions here are PER FAMILY, and they cover the budget and the
one journal line a field failure has to leave behind.

No model is called: the HTTP layer is stubbed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


GOOD_QUESTION = (
    "QUESTION: What is the largest planet?\n"
    "A) Earth\nB) Jupiter\nC) Mars\nD) Venus\n"
    "ANSWER: B\nFACT: Jupiter is a gas giant.")


@pytest.fixture(scope="module")
def tools():
    from puffpai.api_server import _get_assistant_tools
    return _get_assistant_tools().tool_dict


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture()
def post(monkeypatch):
    """Stub the HTTP layer and record every payload it was called with."""
    import requests

    box = {"body": {"message": {"content": ""}}, "status": 200, "calls": []}

    def _post(url, **kwargs):
        box["calls"].append(kwargs.get("json"))
        return _Response(box["body"], box["status"])

    monkeypatch.setattr(requests, "post", _post)
    return box


@pytest.fixture()
def chat_model(monkeypatch):
    """Set the configured chat model for one test, everywhere it is read."""
    from puffpai.config import get_config
    from puffpai import api_server

    def _set(name):
        monkeypatch.setattr(get_config().ollama, "model", name)
        monkeypatch.setattr(api_server, "OLLAMA_MODEL", name)
        return name

    return _set


# ── the resolver itself ─────────────────────────────────────────────────────

def test_thinking_control_is_a_boolean_for_qwen3():
    from puffpai.inference import model_options
    assert model_options.thinking_control("qwen3:30b-a3b") == {"think": False}


def test_thinking_control_is_an_effort_level_for_gpt_oss():
    """A boolean is IGNORED by the harmony family -- that is the whole bug."""
    from puffpai.inference import model_options
    frag = model_options.thinking_control("gpt-oss:20b")
    assert frag == {"think": "low"}
    assert frag["think"] is not False


def test_thinking_control_sends_no_key_for_a_non_reasoning_model():
    from puffpai.inference import model_options
    assert model_options.thinking_control("llama3.1") == {}
    assert model_options.thinking_control("llama3.1:8b-instruct-q4_0") == {}
    # An unknown tag gets the same conservative treatment: a model with no
    # thinking capability answers 400 when the key is present.
    assert model_options.thinking_control("some-new-model:7b") == {}
    assert model_options.thinking_control("") == {}


def test_only_a_family_that_reasons_regardless_gets_a_raised_budget():
    from puffpai.inference import model_options
    assert model_options.structured_num_predict("gpt-oss:20b", 300) == 1024
    assert model_options.structured_num_predict("qwen3:30b-a3b", 300) == 300
    assert model_options.structured_num_predict("llama3.1", 300) == 300
    # A caller that already asks for more than the floor keeps its own number.
    assert model_options.structured_num_predict("gpt-oss:20b", 4096) == 4096


# ── the trivia writer's payload, per family ─────────────────────────────────

def test_generator_payload_for_gpt_oss_uses_low_effort_and_a_raised_budget(
        tools, post, chat_model):
    chat_model("gpt-oss:20b")
    post["body"] = {"message": {"content": GOOD_QUESTION}}
    out = tools["app_trivia_generate"].invoke(
        {"topic": "space", "difficulty": "medium", "grade_level": ""})

    assert len(post["calls"]) == 1
    payload = post["calls"][0]
    assert payload["think"] == "low"
    assert payload["options"]["num_predict"] == 1024
    assert "[answer=B|fact=" in out


def test_generator_payload_for_qwen3_keeps_the_boolean_and_the_tight_budget(
        tools, post, chat_model):
    chat_model("qwen3:30b-a3b")
    post["body"] = {"message": {"content": GOOD_QUESTION}}
    tools["app_trivia_generate"].invoke({"topic": "space"})

    payload = post["calls"][0]
    assert payload["think"] is False
    assert payload["options"]["num_predict"] == 300


def test_generator_payload_for_a_non_reasoning_model_has_no_think_key(
        tools, post, chat_model):
    chat_model("llama3.1")
    post["body"] = {"message": {"content": GOOD_QUESTION}}
    tools["app_trivia_generate"].invoke({"topic": "space"})

    payload = post["calls"][0]
    assert "think" not in payload
    assert payload["options"]["num_predict"] == 300


# ── mocked ollama envelopes ─────────────────────────────────────────────────

def test_a_reasoning_exhausted_reply_falls_back_and_leaves_one_journal_line(
        tools, post, chat_model, caplog):
    """The real exhausted shape: empty content, a full hidden channel, the cap.

    ``trivia_game.parse_question`` throws the reason string away, so without
    this log line a field failure is indistinguishable from any other.
    """
    chat_model("gpt-oss:20b")
    post["body"] = {
        "message": {"content": "", "thinking": "We need a space question..."},
        "eval_count": 300,
        "done_reason": "length",
    }
    with caplog.at_level(logging.WARNING, logger="puffpai.assistant_tools"):
        out = tools["app_trivia_generate"].invoke({"topic": "space"})

    # Honest fallback wording, unchanged.
    assert "couldn't generate a question" in out
    assert "still thinking when it ran out of room" in out
    # The hidden channel is never shown to a player.
    assert "We need a space question" not in out

    lines = [r.getMessage() for r in caplog.records
             if "trivia_generate_failed" in r.getMessage()]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "reason=empty_content_reasoning_exhausted" in line
    assert "model=gpt-oss:20b" in line
    assert "eval_count=300" in line


def test_a_normal_reply_returns_a_question_and_logs_nothing(
        tools, post, chat_model, caplog):
    chat_model("gpt-oss:20b")
    post["body"] = {"message": {"content": GOOD_QUESTION}, "eval_count": 141}
    with caplog.at_level(logging.WARNING, logger="puffpai.assistant_tools"):
        out = tools["app_trivia_generate"].invoke({"topic": "space"})

    assert "What is the largest planet?" in out
    assert "[answer=B|fact=" in out
    assert not [r for r in caplog.records
                if "trivia_generate_failed" in r.getMessage()]


def test_an_empty_reply_still_names_the_topic_in_the_fallback(tools, post):
    """The honest fallback holds under the per-family resolver."""
    post["body"] = {"message": {"content": ""}}
    out = tools["app_trivia_generate"].invoke({"topic": "space"})
    assert out and out.strip()
    assert "couldn't generate a question" in out
    assert "returned nothing" in out


def test_a_generated_question_still_parses_through_the_game(tools, post,
                                                            chat_model):
    """End of the chain: the writer's envelope must satisfy parse_question,
    which is what decides whether the bank fallback fires."""
    from puffpai import trivia_game
    chat_model("gpt-oss:20b")
    post["body"] = {"message": {"content": GOOD_QUESTION}}
    raw = tools["app_trivia_generate"].invoke({"topic": "space"})
    question = trivia_game.parse_question(raw)
    assert question is not None
    assert question["answer"] == "B"


# ── every other raw call to the MAIN chat model uses the same resolver ──────
# (vision.py is exempt: it targets a vision model, not the reasoning-budget
# failure mode the resolver addresses.)

def test_ollama_chat_helper_asks_gpt_oss_for_low_effort(post, chat_model):
    from puffpai import api_server
    chat_model("gpt-oss:20b")
    api_server._ollama_chat([{"role": "user", "content": "hi"}])
    assert len(post["calls"]) == 1
    assert post["calls"][0]["think"] == "low"


def test_ollama_chat_helper_sends_the_boolean_to_qwen3(post, chat_model):
    from puffpai import api_server
    chat_model("qwen3:30b-a3b")
    api_server._ollama_chat([{"role": "user", "content": "hi"}])
    assert post["calls"][0]["think"] is False


def test_ollama_chat_helper_sends_no_key_to_a_non_reasoning_model(
        post, chat_model):
    from puffpai import api_server
    chat_model("llama3.1")
    api_server._ollama_chat([{"role": "user", "content": "hi"}])
    assert "think" not in post["calls"][0]


def test_summarize_tool_result_uses_the_family_resolver(post, chat_model):
    from puffpai import api_server
    chat_model("gpt-oss:20b")
    post["body"] = {"message": {"content": "a summary"}}
    api_server._summarize_tool_result("what happened?", "raw tool output")
    assert len(post["calls"]) == 1
    assert post["calls"][0]["think"] == "low"


def test_ingestion_llm_chat_uses_the_family_resolver(post, chat_model):
    from puffpai import ingestion
    chat_model("qwen3:30b-a3b")
    post["body"] = {"message": {"content": "classified"}}
    ingestion._llm_chat("classify this")
    assert len(post["calls"]) == 1
    assert post["calls"][0]["think"] is False


def test_llm_chat_json_targets_its_own_model_family(post):
    """``_llm_chat_json`` names its model explicitly (qwen3 for structured
    output), so the fragment must follow THAT name, not the chat model."""
    from puffpai.assistant_tools import AssistantTools

    tools_obj = AssistantTools.__new__(AssistantTools)
    tools_obj._backend = None
    post["body"] = {"message": {"content": "{}"}}
    tools_obj._llm_chat_json("give me json", model="qwen3:30b-a3b")
    assert post["calls"], "no request was made"
    assert post["calls"][0]["think"] is False
    assert post["calls"][0]["model"] == "qwen3:30b-a3b"
