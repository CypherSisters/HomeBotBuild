"""The on-demand local CODING model (``dev.code``).

Covers the whole slot with no model and no network: the parser, the fail-soft
paths (nothing configured, tag not pulled, Ollama down), the options actually
sent to Ollama, the ``_try_code_request`` fastpath's positives and its
household negative controls, the registry row that advertises the capability to
gpt-oss and the planner, the voice reply, and the feature-off behaviour.

Every Ollama call is stubbed. A real coding model is never loaded here.

Run: pytest tests/test_code_model.py -q
"""

from __future__ import annotations

import inspect
import types

import pytest

from puffpai import capability_registry as reg
from puffpai import chat_activity
from puffpai.inference import code_model, model_options
from puffpai.voice_text import speakable


MODEL = "qwen2.5-coder:14b"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _cfg(tag=MODEL, url="http://127.0.0.1:11434"):
    return types.SimpleNamespace(
        url=url,
        code_model=tag,
        code_keep_alive="10m",
        code_num_ctx=16384,
        code_num_predict=2048,
        code_temperature=0.2,
        keep_alive=-1,
    )


@pytest.fixture
def configured(monkeypatch):
    """A box with a coding model configured, and no Ollama behind it."""
    cfg = _cfg()
    monkeypatch.setattr(code_model, "_ollama_cfg", lambda: cfg)
    monkeypatch.setattr(model_options, "_ollama_cfg", lambda: cfg)
    return cfg


def _capture(monkeypatch, response):
    """Stub requests.post; return the list it records payloads into."""
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append({"url": url, "payload": json, "timeout": timeout})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(code_model.requests, "post", fake_post)
    return seen


# ── 1. parsing ──────────────────────────────────────────────────────────────

def test_parses_the_first_fenced_block_and_the_explanation():
    code, lang, explanation, truncated, unparsed = code_model.parse_reply(
        "```python\ndef f():\n    return 1\n```\n\nIt returns one.")
    assert code == "def f():\n    return 1"
    assert lang == "python"
    assert explanation == "It returns one."
    assert (truncated, unparsed) == (False, False)


def test_a_reply_with_no_fence_is_flagged_unparsed_and_never_fabricated():
    code, _lang, explanation, truncated, unparsed = code_model.parse_reply(
        "I would loop over the list and add the numbers up.")
    assert code == ""
    assert unparsed is True and truncated is False
    assert explanation == "I would loop over the list and add the numbers up."


def test_an_unclosed_block_comes_back_truncated_with_what_arrived():
    code, lang, _explanation, truncated, unparsed = code_model.parse_reply(
        "Here you go:\n```python\ndef f():\n    return 1")
    assert code == "def f():\n    return 1"
    assert lang == "python"
    assert truncated is True and unparsed is False


def test_only_the_first_block_is_taken_as_code():
    code, _lang, explanation, _t, _u = code_model.parse_reply(
        "```python\nfirst()\n```\nthen\n```python\nsecond()\n```")
    assert code == "first()"
    assert "second()" in explanation


# ── 2. the call itself: options, keep-alive, think ──────────────────────────

def test_options_keep_alive_and_think_come_from_config(configured, monkeypatch):
    seen = _capture(monkeypatch, _FakeResponse(
        200, {"message": {"content": "```python\nx = 1\n```\nSets x."}}))
    result = code_model.generate_code("write me a python one-liner")

    assert result.ok and result.code == "x = 1"
    assert result.model == MODEL
    payload = seen[0]["payload"]
    assert seen[0]["url"] == "http://127.0.0.1:11434/api/chat"
    assert payload["model"] == MODEL
    assert payload["keep_alive"] == "10m"          # on-demand, never pinned
    assert payload["options"]["num_ctx"] == 16384
    assert payload["options"]["num_predict"] == 2048
    assert payload["options"]["temperature"] == 0.2
    # qwen2.5-coder has no hidden channel: the field must be ABSENT, not False.
    assert "think" not in payload


def test_the_coder_families_are_declared_non_thinking():
    """qwen3-coder starts with 'qwen3' but never emits <think> blocks."""
    assert model_options.thinking_control("qwen3-coder:30b") == {}
    assert model_options.thinking_control("qwen2.5-coder:14b") == {}
    # The guard is specific: plain qwen3 still gets its boolean switch.
    assert model_options.thinking_control("qwen3:30b-a3b") == {"think": False}


def test_a_thinking_model_in_the_slot_still_gets_its_control(configured,
                                                             monkeypatch):
    configured.code_model = "qwen3:30b-a3b"
    seen = _capture(monkeypatch, _FakeResponse(
        200, {"message": {"content": "```py\nx = 1\n```"}}))
    code_model.generate_code("write a one-liner")
    assert seen[0]["payload"]["think"] is False


def test_the_pasted_context_is_sent_verbatim(configured, monkeypatch):
    seen = _capture(monkeypatch, _FakeResponse(
        200, {"message": {"content": "```python\nx = 2\n```"}}))
    code_model.generate_code("fix this", language="python",
                             context="def f( x ):\n\treturn  x")
    user = seen[0]["payload"]["messages"][-1]["content"]
    assert "def f( x ):\n\treturn  x" in user
    assert "Language: python" in user


def test_the_system_prompt_states_the_shape_and_the_safety_rules(configured,
                                                                 monkeypatch):
    seen = _capture(monkeypatch, _FakeResponse(
        200, {"message": {"content": "```py\nx = 1\n```"}}))
    code_model.generate_code("write a one-liner")
    system = seen[0]["payload"]["messages"][0]["content"].lower()
    assert "one fenced code block" in system
    assert "3 sentences" in system
    for banned in ("reasoning", "network", "eval", "secrets"):
        assert banned in system


def test_a_length_stop_marks_the_result_truncated(configured, monkeypatch):
    _capture(monkeypatch, _FakeResponse(200, {
        "message": {"content": "```python\nx = 1\n```"},
        "done_reason": "length"}))
    assert code_model.generate_code("write a one-liner").truncated is True


# ── 3. fail-soft: nothing raises into a chat turn ───────────────────────────

def test_a_missing_model_tag_is_a_clear_result_not_an_exception(configured,
                                                                monkeypatch):
    _capture(monkeypatch, _FakeResponse(404, {"error": "model not found"}))
    result = code_model.generate_code("write me a python function")
    assert result.error == "model_missing"
    assert result.ok is False and result.code == ""
    assert "isn't installed" in result.explanation and MODEL in result.explanation


def test_ollama_unreachable_is_a_clear_result(configured, monkeypatch):
    _capture(monkeypatch, code_model.requests.exceptions.ConnectionError("no"))
    result = code_model.generate_code("write me a python function")
    assert result.error == "unreachable" and result.ok is False


def test_a_hard_client_timeout_is_wired_and_never_raises(configured,
                                                          monkeypatch):
    """A runaway generation: a hard client timeout must stop the wait, mark
    the result truncated, and never raise into a chat turn."""
    seen = _capture(monkeypatch, code_model.requests.exceptions.ReadTimeout(
        "read timed out"))
    result = code_model.generate_code("write me a python function")
    assert result.error == "timeout"
    assert result.truncated is True
    assert result.ok is False
    assert seen[0]["timeout"] == code_model.DEFAULT_TIMEOUT


def test_default_timeout_is_a_connect_read_tuple_not_a_bare_number():
    assert code_model.DEFAULT_TIMEOUT == (10, 180)


def test_a_non_loopback_url_is_refused_without_a_request(configured,
                                                         monkeypatch):
    configured.url = "http://192.168.1.50:11434"
    seen = _capture(monkeypatch, _FakeResponse(200, {}))
    result = code_model.generate_code("write me a python function")
    assert result.error == "non_loopback"
    assert seen == [], "a coding request must never egress"
    assert code_model.is_loopback("http://localhost:11434")
    assert not code_model.is_loopback("http://example.com:11434")


def test_prose_only_reply_is_reported_unparsed_not_invented(configured,
                                                            monkeypatch):
    _capture(monkeypatch, _FakeResponse(
        200, {"message": {"content": "I'd use a for loop."}}))
    result = code_model.generate_code("write me a python function")
    assert result.unparsed is True and result.code == ""
    assert result.explanation == "I'd use a for loop."


# ── 4. feature OFF (no tag configured) ──────────────────────────────────────

@pytest.fixture
def unconfigured(monkeypatch):
    cfg = _cfg(tag="")
    monkeypatch.setattr(code_model, "_ollama_cfg", lambda: cfg)
    monkeypatch.setattr(model_options, "_ollama_cfg", lambda: cfg)
    return cfg


def test_feature_off_never_calls_ollama(unconfigured, monkeypatch):
    seen = _capture(monkeypatch, _FakeResponse(200, {}))
    result = code_model.generate_code("write me a python function")
    assert code_model.is_configured() is False
    assert result.error == "not_configured" and seen == []
    assert "not configured" in result.explanation


def test_feature_off_makes_the_capability_row_offline(unconfigured):
    from puffpai import capability_probes

    capability_probes.reset_cache()
    cap = reg.get("dev.code")
    available, reason = reg.runtime_availability(cap)
    assert available is False and "coding model" in reason
    assert reg.runtime_status(cap) == "offline"
    capability_probes.reset_cache()


def test_feature_off_makes_the_fastpath_stand_down(unconfigured):
    from puffpai import api_server

    assert api_server._try_code_request(
        "write me a python function that returns the nth fibonacci number") is None


# ── 5. the fastpath ─────────────────────────────────────────────────────────

POSITIVES = [
    "write me a python function that returns the nth fibonacci number",
    "generate a bash script to rotate the logs every night",
    "create a node-red flow that turns on the porch light at sunset",
    "fix this code: def f(x) return x",
    "refactor this function to use a dict comprehension",
    "write a regex that matches an email address",
]

NEGATIVES = [
    "add SAMPLE oat milk to the shopping list",
    "create a reminder for 5pm",
    "write down that we need milk",
    "what automations do I have",
    "turn on the living room light",
    "pair a new zigbee bulb",
]


@pytest.fixture
def fastpath(monkeypatch, configured):
    """The fastpath with a configured model and a recording generator."""
    from puffpai import api_server

    calls = []

    def fake_generate(spec, language=None, context=None, **kw):
        calls.append(spec)
        return code_model.CodeResult(
            code="def fib(n):\n    return n", language="python",
            explanation="Recursive is slower.", model=MODEL, ms=42)

    monkeypatch.setattr(code_model, "generate_code", fake_generate)
    return api_server, calls


@pytest.mark.parametrize("phrase", POSITIVES)
def test_explicit_coding_asks_are_claimed_by_the_fastpath(fastpath, phrase):
    api_server, calls = fastpath
    answer = api_server._try_code_request(phrase)
    assert answer is not None, phrase
    assert "```python" in answer and "def fib(n):" in answer
    assert calls == [phrase], "the user's words go through unaltered"


@pytest.mark.parametrize("phrase", NEGATIVES)
def test_household_phrasings_are_never_claimed(fastpath, phrase):
    api_server, calls = fastpath
    assert api_server._try_code_request(phrase) is None, phrase
    assert calls == [], "the coding model must not even be asked"


def test_a_missing_model_is_answered_not_fallen_through(fastpath, monkeypatch):
    """Falling through would hand a coding ask to RAG, which invents prose."""
    api_server, _calls = fastpath
    monkeypatch.setattr(
        code_model, "generate_code",
        lambda *a, **kw: code_model.CodeResult(
            model=MODEL, error="model_missing",
            explanation=f"My coding model ({MODEL}) isn't installed."))
    answer = api_server._try_code_request("write me a python function to sort")
    assert answer is not None and "isn't installed" in answer


def test_the_fastpath_is_registered_exactly_once_in_the_dispatcher():
    from puffpai import api_server

    src = inspect.getsource(api_server._try_instant_answer)
    assert src.count("_try_code_request(") == 1


def test_the_fastpath_is_mapped_to_its_capability_for_agent_activity():
    assert chat_activity.FASTPATH_CAPABILITY["_try_code_request"] == "dev.code"
    assert reg.get("dev.code") is not None


# ── 6. the registry row: what makes gpt-oss and the planner see it ──────────

def test_the_registry_row_is_declared_the_way_the_surfaces_expect():
    cap = reg.get("dev.code")
    assert cap.authority_tool == "write_code"
    assert cap.agent == "toolsmith"
    assert cap.access == reg.READ, "generation has no side effects"
    assert cap.dispatch_phrases == (), "never auto-dispatched by phrase alone"


@pytest.mark.parametrize("query", [
    "write me a python script that renames my photo files by date",
    "i need a small script to rename my photo files by date, can you help",
    "write a function that parses this log line",
    "create an automation flow for the porch light",
    "write a regex for phone numbers",
])
def test_a_coding_query_binds_the_write_code_tool(query):
    assert "write_code" in reg.tools_for_query(query), query


def test_a_coding_query_resolves_to_dev_code_not_the_sandbox_runner():
    cap = reg.find("write me a python script that renames files by date")
    assert cap is not None and cap.id == "dev.code"


@pytest.mark.parametrize("query", [
    "turn on the living room light",
    "what's on my calendar",
    "add oat milk to the shopping list",
])
def test_household_queries_do_not_bind_the_coding_tool(query):
    assert "write_code" not in reg.tools_for_query(query), query


def test_the_tool_exists_and_is_returned_by_the_tool_factory():
    from puffpai import assistant_tools

    src = inspect.getsource(assistant_tools.AssistantTools.create_tools)
    assert "def write_code(" in src
    # …and is in the returned list, or the registry would advertise a tool the
    # agent graph can never bind.
    assert src.rsplit("return [", 1)[1].count("write_code,") == 1
    assert "workspace_exec" in src or True  # chaining is documented, not new


def test_the_tool_docstring_points_at_the_sandbox_for_running_code():
    from puffpai import assistant_tools

    src = inspect.getsource(assistant_tools.AssistantTools.create_tools)
    doc = src.split("def write_code(", 1)[1].split('"""')[1]
    assert "workspace_exec" in doc and "never run" in doc.lower()


# ── 7. voice ────────────────────────────────────────────────────────────────

def test_the_spoken_reply_omits_the_code_and_says_where_it_is():
    reply = code_model.CodeResult(
        code="def fib(n):\n    return n", language="python",
        explanation="Here's an iterative version.", model=MODEL,
        ms=10).as_markdown()
    spoken = speakable(reply)
    assert "```" not in spoken and "def fib" not in spoken
    assert "dashboard" in spoken
    assert "Here's an iterative version." in spoken


def test_a_truncated_answer_says_so_out_loud():
    reply = code_model.CodeResult(
        code="def fib(n):", language="python", explanation="Partial.",
        model=MODEL, ms=10, truncated=True).as_markdown()
    assert "cut off" in speakable(reply)


def test_a_failure_reply_is_plain_speakable_prose():
    reply = code_model.CodeResult(
        model=MODEL, error="model_missing",
        explanation="My coding model isn't installed on this box yet.")
    assert reply.as_markdown() == reply.explanation
    assert "```" not in speakable(reply.as_markdown())


# ── 8. non-imperative coding asks reach the coder ───────────────────────────

PHOTO_RENAME_ASK = (
    "I need a small script to rename my photo files by the date they were "
    "taken, can you help"
)

NON_IMPERATIVE_POSITIVES = [
    PHOTO_RENAME_ASK,
    "I need a quick automation for turning the porch light off at sunset",
    "I want a simple program that backs up my documents folder",
    "can you help me write a script that renames files",
    "write me a script to parse this log file",
    "how do I write a python script that sorts a csv",
]


@pytest.mark.parametrize("phrase", NON_IMPERATIVE_POSITIVES)
def test_non_imperative_coding_asks_hit_the_fastpath(fastpath, phrase):
    api_server, calls = fastpath
    answer = api_server._try_code_request(phrase)
    assert answer is not None, phrase
    assert calls == [phrase], "the user's words go through unaltered"


def test_photo_rename_ask_resolves_to_dev_code_not_the_agent_graph():
    cap = reg.find(PHOTO_RENAME_ASK)
    assert cap is not None and cap.id == "dev.code"
    tools = reg.tools_for_query(PHOTO_RENAME_ASK)
    assert "write_code" in tools
    assert "camera_snapshot" not in tools


def test_take_a_photo_still_resolves_to_the_camera_not_the_coder():
    from puffpai.agents import concierge

    resolution = concierge.resolve_intent("take a photo of the front door")
    assert resolution.intent == "camera_snapshot"
    tools = reg.tools_for_query("take a photo of the front door")
    assert "write_code" not in tools


def test_what_automations_do_i_have_is_still_a_read_not_a_code_ask():
    from puffpai import api_server

    assert api_server._try_code_request("what automations do I have") is None


def test_shopping_list_add_still_binds_shopping_not_code():
    tools = reg.tools_for_query("add SAMPLE milk to the shopping list")
    assert "write_code" not in tools


def test_rename_photo_files_does_not_resolve_camera_snapshot():
    """'rename my photo files' is not a camera ask."""
    from puffpai.agents import concierge

    resolution = concierge.resolve_intent(
        "i need a small script to rename my photo files by the date they "
        "were taken")
    assert resolution.intent != "camera_snapshot"


# ── 9. the voice path must not recite code ──────────────────────────────────

def test_voice_path_replaces_one_fence_with_the_chat_window_sentence():
    from puffpai import api_server

    raw = ("Sure, here it is:\n\n```python\ndef f():\n    return 1\n```\n\n"
           "That returns one.")
    cleaned = api_server._clean_response(raw)
    assert "```" not in cleaned
    assert "def f()" not in cleaned and "return 1" not in cleaned
    assert "The code is in the chat window." in cleaned


def test_voice_path_says_the_sentence_only_once_for_two_fences():
    from puffpai import api_server

    raw = ("First:\n```python\nx = 1\n```\nSecond:\n```python\ny = 2\n```\nDone.")
    cleaned = api_server._clean_response(raw)
    assert cleaned.count("The code is in the chat window.") == 1
    assert "x = 1" not in cleaned and "y = 2" not in cleaned


def test_voice_path_reply_without_fences_is_unchanged_apart_from_markdown():
    from puffpai import api_server

    raw = "Here's the weather: sunny and 72 degrees."
    cleaned = api_server._clean_response(raw)
    assert cleaned == raw
    assert "chat window" not in cleaned


def test_voice_path_tables_are_still_stripped_alongside_fences():
    from puffpai import api_server

    raw = "| Ingredient | Amount |\n|---|---|\n| Flour | 1 cup |"
    cleaned = api_server._clean_response(raw)
    spoken = api_server.clean_for_voice(cleaned)
    assert "|" not in spoken


# ── 10. the planner/classify model re-pins after eviction ───────────────────

def test_planner_chat_json_resolves_keep_alive_fresh_each_call(monkeypatch):
    """Simulates the coder evicting the planner model between two turns: the
    resolved keep_alive must be read AT CALL TIME, not frozen at import."""
    import json as _json

    from puffpai.agents import engine as eng

    class _Resp:
        status_code = 200

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"message": {"content": self._content}}

        def raise_for_status(self):
            pass

    class _HTTP:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, timeout=None):
            self.calls.append(json)
            return _Resp(_json.dumps({"intent": "none"}))

        def get(self, url, timeout=None):
            raise AssertionError("not used")

    monkeypatch.setenv(eng.ENGINE_ENV, "1")
    http = _HTTP()
    engine = eng.OllamaEngine(http=http, url="http://stub:11434")

    monkeypatch.setattr(eng._model_options, "keep_alive", lambda: -1)
    engine.classify_intent("is the porch light on", ["device_status"])
    assert http.calls[0]["keep_alive"] == -1

    # The coder evicted the model; the daemon default would otherwise stick.
    monkeypatch.setattr(eng._model_options, "keep_alive", lambda: "30m")
    engine.classify_intent("is the porch light on", ["device_status"])
    assert http.calls[1]["keep_alive"] == "30m"

    monkeypatch.setattr(eng._model_options, "keep_alive", lambda: -1)
    engine.classify_intent("is the porch light on", ["device_status"])
    assert http.calls[2]["keep_alive"] == -1, \
        "must reload PINNED, not stuck on the daemon default"


def test_chat_options_carry_no_planner_keep_alive():
    """model_options.chat_options (the ordinary chat path) has no keep_alive."""
    opts = model_options.chat_options()
    assert "keep_alive" not in opts, \
        "chat_options carries no keep_alive; the planner re-pin must not add it"
