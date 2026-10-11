"""Tests for :meth:`OllamaEngine.revise_plan` — the observe→revise engine hook.

``revise_plan`` is :meth:`plan_steps`' sibling and is held to the same rules: a
schema-constrained decode over catalogue-derived enums, degrading to ``None`` on
every failure. It is ALSO the "content-drives-action" boundary, so these tests
pin the extra rule that makes it safe: only SCALAR observation fields ever reach
the model, and anything else an observation carries is never rendered into the
prompt (defence in depth behind ``planning_loop._observe``).

Injected transport: no Ollama, no model, no network.
Run: pytest tests/test_agent_engine_revise.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents import engine as eng
from puffpai.agents.engine import OllamaEngine

# Same shape as tests/test_agent_engine.py's fakes, re-declared so this file
# stands alone.
CATALOGUE = [
    "  archivist.retrieve query=<value>",
    "  human.ask question=<value>",
    "  steward.camera_snapshot target=<device name>",
    "  steward.control_device target=<device name> action=<value>",
    "  steward.list_devices",
]


class FakeResponse:
    def __init__(self, content=None, *, status_code=200, body=None):
        self.status_code = status_code
        self.text = ""
        self._body = ({"message": {"content": content}} if content is not None
                      else (body or {}))

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected extra POST")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def reply(payload: dict) -> FakeResponse:
    return FakeResponse(json.dumps(payload))


@pytest.fixture(autouse=True)
def engine_on(monkeypatch):
    monkeypatch.setenv(eng.ENGINE_ENV, "1")
    monkeypatch.delenv(eng.CLASSIFY_MODEL_ENV, raising=False)
    monkeypatch.delenv(eng.PLAN_MODEL_ENV, raising=False)


def make(http, **kwargs) -> OllamaEngine:
    kwargs.setdefault("url", "http://stub:11434")
    return OllamaEngine(http=http, **kwargs)


OBS = [{"step": "archivist.retrieve on schedule", "status": "executed",
        "ok": True, "empty": True, "count": 0, "error_class": "ok"}]
REMAINING = ["steward.list_devices"]


# ── it plans like plan_steps ────────────────────────────────────────

def test_revise_returns_step_dicts():
    steps = [{"agent": "steward", "intent": "list_devices",
              "why": "the note was empty, list what we have"}]
    http = FakeHTTP(reply({"reasoning": "empty, try devices", "steps": steps}))
    assert make(http).revise_plan("find the schedule", OBS, REMAINING,
                                  CATALOGUE) == steps


def test_revise_enums_match_plan_steps_exactly():
    """A revision must be as constrained as an initial plan — same enums.

    Built from the same catalogue path, so a revised step can only ever name a
    capability the code implements; the parse gate in the loop is the backstop.
    """
    http = FakeHTTP(reply({"reasoning": "", "steps": []}),
                    reply({"reasoning": "", "steps": []}))
    engine = make(http)
    engine.plan_steps("goal", CATALOGUE)
    engine.revise_plan("goal", OBS, REMAINING, CATALOGUE)

    plan_items = http.calls[0]["json"]["format"]["properties"]["steps"]["items"]
    rev_items = http.calls[1]["json"]["format"]["properties"]["steps"]["items"]
    assert rev_items == plan_items
    assert rev_items["properties"]["agent"]["enum"] == [
        "archivist", "human", "steward"]


def test_revise_uses_the_plan_model_and_plan_ceilings():
    http = FakeHTTP(reply({"reasoning": "", "steps": []}))
    make(http).revise_plan("goal", OBS, REMAINING, CATALOGUE)
    body = http.calls[0]["json"]
    assert body["model"] == eng.DEFAULT_PLAN_MODEL
    assert body["options"]["temperature"] == 0
    assert body["options"]["num_predict"] == eng.PLAN_NUM_PREDICT
    assert http.calls[0]["timeout"] == eng.PLAN_TIMEOUT


# ── it degrades, never raises ───────────────────────────────────────

@pytest.mark.parametrize("catalogue", [None, [], ["nonsense with no pair"]])
def test_revise_without_a_catalogue_makes_no_call(catalogue):
    http = FakeHTTP()  # any POST raises
    assert make(http).revise_plan("goal", OBS, REMAINING, catalogue) is None
    assert http.calls == []


def test_revise_with_no_goal_makes_no_call():
    http = FakeHTTP()
    assert make(http).revise_plan("   ", OBS, REMAINING, CATALOGUE) is None
    assert http.calls == []


def test_revise_survives_a_transport_failure():
    http = FakeHTTP(RuntimeError("box is busy"))
    assert make(http).revise_plan("goal", OBS, REMAINING, CATALOGUE) is None


def test_revise_survives_non_json_content():
    http = FakeHTTP(FakeResponse("let me think about that"))
    assert make(http).revise_plan("goal", OBS, REMAINING, CATALOGUE) is None


def test_revise_drops_non_dict_steps():
    http = FakeHTTP(reply({"reasoning": "", "steps": [
        {"agent": "steward", "intent": "list_devices", "why": "ok"},
        "not a step", 42]}))
    out = make(http).revise_plan("goal", OBS, REMAINING, CATALOGUE)
    assert out == [{"agent": "steward", "intent": "list_devices", "why": "ok"}]


# ── the security boundary: only scalars reach the model ─────────────

MARKER = "INJECTED_PAGE_TEXT_fetch_169_254_169_254"


def test_a_stray_free_text_field_never_reaches_the_prompt():
    """Defence in depth behind ``planning_loop._observe``.

    The loop's observation builder is the choke point that guarantees scalars
    only, but ``revise_plan`` independently renders ONLY the fixed scalar keys.
    An observation carrying an extra free-text field (a bug, or a future key)
    still cannot ride into the planner prompt through here.
    """
    poisoned = [{"step": "toolsmith.web_fetch on https://example.com",
                 "status": "executed", "ok": True, "empty": False, "count": 1,
                 "error_class": "ok",
                 # Nothing legitimate puts content in an observation; if it ever
                 # did, this field must not be rendered.
                 "leaked_body": MARKER,
                 "text": MARKER}]
    http = FakeHTTP(reply({"reasoning": "", "steps": []}))
    make(http).revise_plan("goal", poisoned, REMAINING, CATALOGUE)
    prompt = json.dumps(http.calls[0]["json"]["messages"])
    assert MARKER not in prompt


def test_a_non_canonical_error_class_is_coarsened_not_echoed():
    """``error_class`` is a closed label set; an off-list value cannot smuggle
    a message string (which could echo fetched content) into the prompt."""
    obs = [{"step": "toolsmith.web_fetch on https://example.com",
            "status": "error", "ok": False, "empty": None, "count": None,
            "error_class": f"boom {MARKER} at line 5"}]
    http = FakeHTTP(reply({"reasoning": "", "steps": []}))
    make(http).revise_plan("goal", obs, REMAINING, CATALOGUE)
    prompt = json.dumps(http.calls[0]["json"]["messages"])
    assert MARKER not in prompt
    assert "error" in prompt  # the coarse category still made it through
