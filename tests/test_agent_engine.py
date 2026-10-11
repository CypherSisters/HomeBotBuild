"""Tests for the Concierge engine — schema-constrained local inference.

Every test runs with an injected transport: no Ollama, no model, no network.
That is the point — the paths that must hold are the failure paths (the box is
busy, the model is gone, a runtime ignores the schema), and those are exactly
the ones a live smoke test cannot reach on demand.

Run: pytest tests/test_agent_engine.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents import engine as eng
from puffpai.agents.engine import NO_INTENT, OllamaEngine, build_engine

MENU = ["camera_snapshot", "control_device", "device_status", "list_devices"]
CATALOGUE = [
    "  archivist.retrieve query=<value>",
    "  human.ask question=<value>",
    "  steward.camera_snapshot target=<device name>",
    "  steward.control_device target=<device name> action=<value>",
    "  steward.siren target=<device name> [needs household confirmation — can only be proposed]",
]


# ── fakes ───────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, content=None, *, status_code=200, body=None, raw=None):
        self.status_code = status_code
        self.text = raw if raw is not None else ""
        if body is not None:
            self._body = body
        elif content is not None:
            self._body = {"message": {"content": content}}
        else:
            self._body = {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Records requests; replies from a queue (or one canned response)."""

    def __init__(self, *responses, get_response=None):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.get_calls: list[str] = []
        self._get_response = get_response

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected extra POST")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url, timeout=None):
        self.get_calls.append(url)
        if isinstance(self._get_response, Exception):
            raise self._get_response
        return self._get_response


def reply(payload: dict) -> FakeResponse:
    return FakeResponse(json.dumps(payload))


@pytest.fixture(autouse=True)
def engine_on(monkeypatch):
    """Kill-switch on, model tags pinned — env must not leak between tests."""
    monkeypatch.setenv(eng.ENGINE_ENV, "1")
    monkeypatch.delenv(eng.CLASSIFY_MODEL_ENV, raising=False)
    monkeypatch.delenv(eng.PLAN_MODEL_ENV, raising=False)


def make(http, **kwargs) -> OllamaEngine:
    kwargs.setdefault("url", "http://stub:11434")
    return OllamaEngine(http=http, **kwargs)


# ── classify_intent ─────────────────────────────────────────────────

def test_classify_returns_the_chosen_intent():
    http = FakeHTTP(reply({"intent": "camera_snapshot"}))
    assert make(http).classify_intent("grab a photo of the shed", MENU) == \
        "camera_snapshot"


def test_classify_constrains_the_menu_with_an_enum():
    http = FakeHTTP(reply({"intent": "device_status"}))
    make(http).classify_intent("is the porch light on", MENU)

    schema = http.calls[0]["json"]["format"]
    assert schema["properties"]["intent"]["enum"] == sorted(MENU) + [NO_INTENT]
    assert schema["required"] == ["intent"]


def test_classify_none_means_not_house_work():
    """The escape hatch: without it the enum hijacks every knowledge question."""
    http = FakeHTTP(reply({"intent": NO_INTENT}))
    assert make(http).classify_intent("what's the capital of France", MENU) is None


def test_classify_rejects_an_off_menu_answer():
    """Defence in depth — the enum should make this unreachable."""
    http = FakeHTTP(reply({"intent": "launch_missiles"}))
    assert make(http).classify_intent("do the thing", MENU) is None


def test_classify_survives_a_transport_failure():
    http = FakeHTTP(RuntimeError("connection refused"))
    assert make(http).classify_intent("is the porch light on", MENU) is None


def test_classify_survives_non_json_content():
    http = FakeHTTP(FakeResponse("I think you want the camera!"))
    assert make(http).classify_intent("photo of the shed", MENU) is None


def test_classify_survives_a_missing_message_body():
    http = FakeHTTP(FakeResponse(body={"done": True}))
    assert make(http).classify_intent("photo of the shed", MENU) is None


@pytest.mark.parametrize("text,choices", [
    ("", MENU), ("   ", MENU), ("real text", []), ("real text", None),
])
def test_classify_makes_no_call_without_usable_input(text, choices):
    http = FakeHTTP()  # any POST raises
    assert make(http).classify_intent(text, choices) is None
    assert http.calls == []


def test_classify_uses_the_small_model_and_tight_ceilings():
    http = FakeHTTP(reply({"intent": "list_devices"}))
    make(http).classify_intent("what devices are there", MENU)

    body = http.calls[0]["json"]
    assert body["model"] == eng.DEFAULT_CLASSIFY_MODEL
    assert body["stream"] is False
    assert body["options"]["temperature"] == 0
    assert body["options"]["num_predict"] == eng.CLASSIFY_NUM_PREDICT
    assert http.calls[0]["timeout"] == eng.CLASSIFY_TIMEOUT


def test_model_slots_come_from_env(monkeypatch):
    monkeypatch.setenv(eng.CLASSIFY_MODEL_ENV, "tiny:1b")
    monkeypatch.setenv(eng.PLAN_MODEL_ENV, "big:70b")
    http = FakeHTTP(reply({"intent": "list_devices"}))
    make(http).classify_intent("what devices", MENU)
    assert http.calls[0]["json"]["model"] == "tiny:1b"


# ── plan_steps ──────────────────────────────────────────────────────

def test_plan_returns_step_dicts():
    steps = [{"agent": "steward", "intent": "camera_snapshot",
              "target": "shed", "why": "see who is there"}]
    http = FakeHTTP(reply({"reasoning": "look first", "steps": steps}))
    assert make(http).plan_steps("check the shed", CATALOGUE) == steps


def test_plan_enums_are_built_from_the_catalogue():
    """A capability the code does not implement must be undecodable."""
    http = FakeHTTP(reply({"reasoning": "", "steps": []}))
    make(http).plan_steps("do something", CATALOGUE)

    item = http.calls[0]["json"]["format"]["properties"]["steps"]["items"]
    assert item["properties"]["agent"]["enum"] == ["archivist", "human", "steward"]
    assert item["properties"]["intent"]["enum"] == [
        "ask", "camera_snapshot", "control_device", "retrieve", "siren"]
    assert item["required"] == ["agent", "intent", "why"]


def test_plan_reasons_before_it_commits():
    """Schema property order is emission order — the constraint-tax mitigation."""
    http = FakeHTTP(reply({"reasoning": "", "steps": []}))
    make(http).plan_steps("check the shed", CATALOGUE)

    props = http.calls[0]["json"]["format"]["properties"]
    assert list(props) == ["reasoning", "steps"]


def test_plan_is_capped_and_uses_the_plan_model():
    http = FakeHTTP(reply({"reasoning": "", "steps": []}))
    make(http).plan_steps("check the shed", CATALOGUE)

    body = http.calls[0]["json"]
    assert body["model"] == eng.DEFAULT_PLAN_MODEL
    assert body["format"]["properties"]["steps"]["maxItems"] == 6
    assert http.calls[0]["timeout"] == eng.PLAN_TIMEOUT


def test_plan_drops_non_dict_steps():
    http = FakeHTTP(reply({"reasoning": "x", "steps": [
        {"agent": "steward", "intent": "camera_snapshot", "why": "y"},
        "not a step", 42, None]}))
    out = make(http).plan_steps("check the shed", CATALOGUE)
    assert out == [{"agent": "steward", "intent": "camera_snapshot", "why": "y"}]


def test_plan_rejects_a_non_list_steps_field():
    http = FakeHTTP(reply({"reasoning": "x", "steps": "camera_snapshot"}))
    assert make(http).plan_steps("check the shed", CATALOGUE) is None


@pytest.mark.parametrize("goal,catalogue", [
    ("", CATALOGUE), ("   ", CATALOGUE),
    ("real goal", []), ("real goal", ["no pairs here"]), ("real goal", None),
])
def test_plan_makes_no_call_without_a_goal_or_catalogue(goal, catalogue):
    http = FakeHTTP()
    assert make(http).plan_steps(goal, catalogue) is None
    assert http.calls == []


def test_plan_survives_a_timeout():
    http = FakeHTTP(TimeoutError("read timed out"))
    assert make(http).plan_steps("check the shed", CATALOGUE) is None


# ── the think probe ─────────────────────────────────────────────────

def test_think_is_suppressed_by_default():
    http = FakeHTTP(reply({"intent": "list_devices"}))
    make(http).classify_intent("what devices", MENU)
    assert http.calls[0]["json"]["think"] is False


def test_a_model_that_rejects_think_is_retried_and_memoised():
    http = FakeHTTP(
        FakeResponse(status_code=400,
                     body={"error": "\"tiny\" does not support thinking"}),
        reply({"intent": "list_devices"}),
        reply({"intent": "device_status"}),
    )
    engine = make(http)
    assert engine.classify_intent("what devices", MENU) == "list_devices"
    assert "think" not in http.calls[1]["json"], "retry must drop the field"

    # Second call: the memo means no wasted round-trip.
    assert engine.classify_intent("is it on", MENU) == "device_status"
    assert len(http.calls) == 3
    assert "think" not in http.calls[2]["json"]


def test_an_unrelated_400_is_not_retried():
    http = FakeHTTP(FakeResponse(status_code=400,
                                 body={"error": "model not found"}))
    assert make(http).classify_intent("what devices", MENU) is None
    assert len(http.calls) == 1


def test_a_400_with_an_unreadable_body_is_not_retried():
    http = FakeHTTP(FakeResponse(status_code=500, body=None, raw="upstream boom"))
    assert make(http).classify_intent("what devices", MENU) is None
    assert len(http.calls) == 1


# ── run(), health(), kill-switch ────────────────────────────────────

def test_run_is_deliberately_not_a_chat_engine():
    """Defined, not omitted: the call site would otherwise log a fault a turn."""
    http = FakeHTTP()
    assert make(http).run("hello", None) is None
    assert http.calls == []


def test_health_reports_model_presence():
    """Slots are pinned distinct here: the defaults share a tag, which would
    make this pass without proving the two are reported independently."""
    http = FakeHTTP(get_response=FakeResponse(body={"models": [
        {"name": "present:7b"}]}))
    health = make(http, classify_model="present:7b",
                  plan_model="absent:70b").health()
    assert health["ok"] is True
    assert health["classify_model_present"] is True
    assert health["plan_model_present"] is False


def test_health_reports_an_unreachable_runtime():
    http = FakeHTTP(get_response=RuntimeError("connection refused"))
    health = make(http).health()
    assert health["ok"] is False
    assert "connection refused" in health["error"]


def test_kill_switch_stops_every_model_call(monkeypatch):
    monkeypatch.setenv(eng.ENGINE_ENV, "0")
    http = FakeHTTP()
    engine = make(http)
    assert engine.classify_intent("is the porch light on", MENU) is None
    assert engine.plan_steps("check the shed", CATALOGUE) is None
    assert http.calls == []
    assert build_engine() is None


def test_build_engine_makes_no_network_call():
    http = FakeHTTP()
    assert isinstance(build_engine(http=http, url="http://stub:11434"),
                      OllamaEngine)
    assert http.calls == [] and http.get_calls == []


# ── wiring: build_team gives the Concierge its brain ────────────────

def test_build_team_attaches_an_engine_by_default(monkeypatch):
    monkeypatch.setenv(eng.ENGINE_ENV, "1")
    from puffpai.agents import build_team
    concierge = build_team().get("concierge")
    assert concierge.health()["engine_attached"] is True
    assert concierge.health()["engine_models"]["classify"]


def test_build_team_honours_an_explicit_none(monkeypatch):
    """A test (or a deployment) can still ask for the routing-only Concierge."""
    monkeypatch.setenv(eng.ENGINE_ENV, "1")
    from puffpai.agents import build_team
    bus = build_team(engines={"concierge_engine": None})
    assert bus.get("concierge").health()["engine_attached"] is False


def test_build_team_is_engine_less_when_the_switch_is_off(monkeypatch):
    monkeypatch.setenv(eng.ENGINE_ENV, "0")
    from puffpai.agents import build_team
    assert build_team().get("concierge").health()["engine_attached"] is False


# ── integration: the engine actually changes Concierge routing ──────
#
# These use phrasings the deterministic resolver genuinely misses today (all
# four fall through with reason="no rule matched"), including the exact
# question benchmark 8.6 scored as a FAIL — it answered with a generic
# 111-device list because the miss path defaults to list_devices.

class StubEngine:
    """Engine-shaped double: records the menu, returns a canned choice."""

    def __init__(self, choice):
        self.choice = choice
        self.seen: list[tuple[str, list]] = []

    def classify_intent(self, text, choices):
        self.seen.append((text, list(choices)))
        return self.choice

    def run(self, text, conversation_history=None):
        return None


MISSED = "is the back door camera seeing motion"


def test_the_resolver_really_does_miss_these(monkeypatch):
    """Guard the premise: if a rule starts matching, these tests must be re-aimed."""
    from puffpai.agents.concierge import resolve_intent
    for text in (MISSED, "would you mind checking how stuffy it feels in here",
                 "i want eyes on the back door"):
        assert resolve_intent(text).reason == "no rule matched"


def test_engine_refines_a_missed_request_to_a_real_intent():
    from puffpai.agents.concierge import ConciergeAgent
    engine = StubEngine("device_status")
    resolved = ConciergeAgent(engine=engine)._resolve(MISSED)

    assert resolved.intent == "device_status"
    # The reason still carries the "no rule matched" prefix on purpose: a
    # refinement is a guess about a turn no rule claimed, and
    # _delegate_to_steward's non-home guard keys off that prefix. See
    # tests/test_misroutes.py.
    assert resolved.reason == "no rule matched (engine refined)"
    assert resolved.needs_entity is True


def test_engine_is_offered_only_the_intents_the_steward_implements():
    from puffpai.agents.concierge import RESOLVABLE_INTENTS, ConciergeAgent
    engine = StubEngine("device_status")
    ConciergeAgent(engine=engine)._resolve(MISSED)

    _text, menu = engine.seen[0]
    assert set(menu) == set(RESOLVABLE_INTENTS)


def test_engine_declining_leaves_the_message_to_the_knowledge_path():
    """'none' must not become a house intent — that hijack is the whole risk."""
    from puffpai.agents.concierge import ConciergeAgent
    resolved = ConciergeAgent(engine=StubEngine(None))._resolve(
        "how do I purify water")
    assert resolved.reason == "no rule matched"


def test_engine_is_not_consulted_when_a_rule_already_matched():
    """The fast path stays fast: a matched rule never pays for inference."""
    from puffpai.agents.concierge import ConciergeAgent, resolve_intent
    text = "turn on the kitchen light"
    assert resolve_intent(text).reason != "no rule matched"

    engine = StubEngine("siren")
    resolved = ConciergeAgent(engine=engine)._resolve(text)
    assert engine.seen == []
    assert resolved.intent != "siren"


def test_a_broken_engine_degrades_to_the_deterministic_answer():
    from puffpai.agents.concierge import ConciergeAgent

    class Exploding:
        def classify_intent(self, text, choices):
            raise RuntimeError("ollama is on fire")

    resolved = ConciergeAgent(engine=Exploding())._resolve(MISSED)
    assert resolved.reason == "no rule matched"


def test_classify_enum_never_repeats_the_sentinel():
    """A caller that already lists 'none' must not produce a malformed schema."""
    http = FakeHTTP(reply({"intent": "list_devices"}))
    make(http).classify_intent("what devices", MENU + [NO_INTENT])

    enum = http.calls[0]["json"]["format"]["properties"]["intent"]["enum"]
    assert enum.count(NO_INTENT) == 1
    assert enum == sorted(MENU) + [NO_INTENT]
