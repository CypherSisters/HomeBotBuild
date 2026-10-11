"""Trivia: no stage directions for the player, and an honest generator.

Stage direction: a rendered question must not end with the literal sentence
"Ask the user to pick A, B, C, or D. Do NOT reveal the answer yet, wait for
their response." That is the tool talking to the orchestrating model, not to
the person playing. Neither source emits it, and it is scrubbed on the way
out so a question already persisted in a household's `sessions[].pending`
cannot leak it either.

Generator: "play trivia about space" must produce a space question or say why
not. When ollama answers `{"error": "model 'x' not found"}` with no `message`
key, `resp.json.get("message", {}).get("content", "")` turns that into an
EMPTY STRING, indistinguishable, to the caller, from "the model wrote
something I could not parse". The tool checks the error envelope, the HTTP
status and an empty/thinking-only content, and returns a reason instead of "".

No model is called here: the HTTP layer is stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from puffpai import trivia_game  # noqa: E402

STAGE = "Ask the user to pick A, B, C, or D"
NO_REVEAL = "Do NOT reveal the answer yet"


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
    """Stub the HTTP layer. Nothing in this file reaches a model."""
    import requests

    box = {"body": {}, "status": 200}

    def _post(url, **kwargs):
        return _Response(box["body"], box["status"])

    monkeypatch.setattr(requests, "post", _post)
    return box


# ── the stage direction ─────────────────────────────────────────────────────

def test_the_bank_tool_emits_no_stage_direction(tools):
    for _ in range(12):
        out = tools["app_trivia_question"].invoke("history")
        assert STAGE not in out
        assert NO_REVEAL not in out
        assert "[answer=" in out, "the key must still reach the grader"


def test_the_bank_tool_output_is_exactly_header_question_options_key(tools):
    out = tools["app_trivia_question"].invoke("history")
    lines = out.split("\n")
    assert len(lines) == 4, out
    assert lines[0].startswith("[Trivia — ")
    assert lines[2].startswith("(A) ")
    assert lines[3].startswith("[answer=") and lines[3].endswith("]")


def test_the_generator_emits_no_stage_direction(tools, post):
    post["body"] = {"message": {"content": (
        "QUESTION: What colour is the sky on Mars at noon?\n"
        "A) Blue\nB) Butterscotch\nC) Green\nD) Violet\n"
        "ANSWER: B\nFACT: Dust scatters red light.")}}
    out = tools["app_trivia_generate"].invoke(
        {"topic": "space", "difficulty": "medium", "grade_level": ""})
    assert STAGE not in out
    assert NO_REVEAL not in out
    assert "[answer=B|fact=" in out


def test_a_rendered_question_never_carries_the_stage_direction(tools):
    raw = tools["app_trivia_question"].invoke("history")
    question = trivia_game.parse_question(raw)
    assert question is not None
    assert STAGE not in question["prompt"]
    assert "[answer" not in question["prompt"].lower()


def test_the_scrub_removes_it_from_a_question_persisted_by_the_old_code():
    """A household's stored `pending` still holds the old envelope."""
    old = ("[Trivia — History]\n"
           "What year did the Titanic sink?\n"
           "(A) 1910  (B) 1912  (C) 1914  (D) 1916\n\n"
           "Ask the user to pick A, B, C, or D. "
           "Do NOT reveal the answer yet — wait for their response. "
           "[answer=B]")
    cleaned = trivia_game.strip_answer_marker(old)
    assert STAGE not in cleaned
    assert NO_REVEAL not in cleaned
    assert "[answer" not in cleaned.lower()
    assert "What year did the Titanic sink?" in cleaned
    assert "(B) 1912" in cleaned


def test_the_scrub_leaves_an_ordinary_reply_untouched():
    plain = "Correct! Diamond. 1 out of 2 right so far."
    assert trivia_game.strip_answer_marker(plain) == plain


def test_the_api_reply_guards_strip_the_stage_direction_too():
    from puffpai import api_server
    leaked = ("What year did the Titanic sink? (A) 1910  (B) 1912 "
              "Ask the user to pick A, B, C, or D. "
              "Do NOT reveal the answer yet — wait for their response. "
              "[answer=B]")
    for guard in (api_server._strip_trivia_answer,
                  api_server._guard_chat_reply,
                  api_server._clean_response):
        cleaned = guard(leaked)
        assert STAGE not in cleaned, guard.__name__
        assert "[answer" not in cleaned.lower(), guard.__name__


# ── the generator says why, and never returns "" ────────────────────────────

def test_an_ollama_error_envelope_is_a_reason_not_an_empty_string(tools, post):
    """A 404 with {"error": "model... not found"}."""
    post["body"] = {"error": "model 'test-fake-model:20b' not found"}
    post["status"] = 404
    out = tools["app_trivia_generate"].invoke(
        {"topic": "space", "difficulty": "medium", "grade_level": ""})
    assert out, "an empty string is what hid this for a month"
    assert "couldn't generate a question" in out
    assert "not found" in out


def test_an_http_error_with_no_error_key_still_says_so(tools, post):
    post["body"] = {"message": {"content": ""}}
    post["status"] = 500
    out = tools["app_trivia_generate"].invoke({"topic": "space"})
    assert out and "couldn't generate a question" in out


def test_a_thinking_only_answer_says_which_failure_it_was(tools, post):
    post["body"] = {"message": {"content": "",
                                "thinking": "Let me consider Mars..."}}
    out = tools["app_trivia_generate"].invoke({"topic": "space"})
    assert "still thinking" in out


def test_an_empty_answer_says_the_model_returned_nothing(tools, post):
    post["body"] = {"message": {"content": "   "}}
    out = tools["app_trivia_generate"].invoke({"topic": "space"})
    assert "returned nothing" in out


def test_unparseable_prose_is_not_handed_back_as_a_question(tools, post):
    post["body"] = {"message": {"content": (
        "Sure! Here are some thoughts about space before I write anything.")}}
    out = tools["app_trivia_generate"].invoke({"topic": "space"})
    assert "did not answer in the question format" in out
    assert "Sure! Here are some thoughts" not in out


def test_no_failure_mode_returns_an_empty_string(tools, post):
    bodies = [{"error": "boom"}, {"message": {}}, {"message": {"content": ""}},
              {"message": {"content": "waffle"}}, [], "nonsense"]
    for body in bodies:
        post["body"] = body
        post["status"] = 200
        out = tools["app_trivia_generate"].invoke({"topic": "space"})
        assert out and out.strip(), body


def test_a_transport_failure_still_names_itself(tools, post):
    post["body"] = ValueError("not json")
    out = tools["app_trivia_generate"].invoke({"topic": "space"})
    assert "couldn't generate a question" in out


def test_a_good_answer_still_parses_end_to_end(tools, post):
    post["body"] = {"message": {"content": (
        "QUESTION: Which planet has the tallest volcano?\n"
        "A) Earth\nB) Venus\nC) Mars\nD) Mercury\n"
        "ANSWER: C\nFACT: Olympus Mons is about 22 km high.")}}
    raw = tools["app_trivia_generate"].invoke({"topic": "space"})
    question = trivia_game.parse_question(raw)
    assert question is not None
    assert question["answer"] == "C"
    assert "Olympus Mons" in question["fact"]
    assert STAGE not in question["prompt"]


# ── the fallback says it fell back ──────────────────────────────────────────

def test_the_bank_fallback_names_the_topic_it_could_not_make(tools, post):
    """A bank question under a request for a space one must say so, not swap
    silently."""
    post["body"] = {"error": "model not found"}
    post["status"] = 404

    def _bank(category="random"):
        return tools["app_trivia_question"].invoke("history")

    def _generate(topic, difficulty="medium", audience=""):
        return tools["app_trivia_generate"].invoke(
            {"topic": topic, "difficulty": difficulty,
             "grade_level": audience})

    class _Store:
        def __init__(self):
            self.data = {"results": [], "sessions": [], "players": {}}

        def load(self):
            return self.data

        def save(self, data):
            self.data = data
            return True

    reply = trivia_game.handle("play trivia about space", ask_bank=_bank,
                               ask_generate=_generate, store=_Store())
    assert reply
    assert "couldn't make a space question" in reply
    assert "from the built-in bank" in reply
    assert STAGE not in reply
    assert "[answer" not in reply.lower()
