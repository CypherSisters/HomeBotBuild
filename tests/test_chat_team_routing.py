"""Tests for the product team as the chat front door (api_server).

Before this, every chat path (/chat/send, /chat/stream, /api/chat,
/v1/chat/completions) funnelled through ``_detect_route`` and the "agent"
branch called the LEGACY ``PuffPAIAgent``, so the six-agent team never saw a
command typed into the dashboard. These tests pin the new predicate, the
kill-switch, and — most importantly — that the legacy path still answers
everything the team does not claim.

Run: pytest tests/test_chat_team_routing.py -q
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tests.modpatch import patched_modules


@pytest.fixture(scope="module")
def api():
    """Import the api_server under its *package* name.

    Deliberately ``puffpai.api_server`` rather than a bare ``api_server``: the
    bare name resolves through whatever ``src/puffpai`` directory happens to be
    first on sys.path, which in a whole-suite run can be a different checkout.
    """
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        mod = importlib.import_module("puffpai.api_server")
        assert hasattr(mod, "_CHAT_VIA_TEAM_ENV"), "stale api_server on sys.path"
        yield mod


@pytest.fixture
def legacy(api, monkeypatch):
    """Stub the whole legacy pipeline and record what it was asked to do."""
    calls: dict[str, list] = {"agent": [], "rag": [], "library": [], "direct": []}

    class Tools:
        def run(self, message, history):
            calls["agent"].append(message)
            return "LEGACY-AGENT"

        def daily_briefing(self):
            return "LEGACY-BRIEFING"

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: Tools())
    monkeypatch.setattr(api, "_try_direct_handle",
                        lambda msg: calls["direct"].append(msg))
    monkeypatch.setattr(api, "_snapshot_counts", lambda msg: {})
    monkeypatch.setattr(api, "_verify_and_fix", lambda msg, resp, before: resp)
    monkeypatch.setattr(api, "_load_persona", lambda: "persona")

    def rag(message, history, persona, enriched=False, **kwargs):
        calls["rag"].append(message)
        return {'response': "LEGACY-RAG", 'sources': [], 'suggestions': []}

    monkeypatch.setattr(api, "_query_ollama_direct", rag)
    monkeypatch.setattr(api, "_get_indexed_libraries_direct",
                        lambda: calls["library"].append(True) or "LEGACY-LIBRARY")
    return calls


def _concierge_reply(**over):
    reply = {
        "ok": True, "handled": True, "reply": "Captured a snapshot from the camera.",
        "intent": "camera_snapshot", "targets": [],
        "delegated_to": "steward", "delegate_reply": {"ok": True, "bytes": 42},
    }
    reply.update(over)
    return reply


ACTION = "take a snapshot from the back door camera"
KNOWLEDGE = "why is the sky blue"


# ── the predicate ───────────────────────────────────────────────────

def test_action_utterance_goes_to_the_team(api, legacy, monkeypatch):
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: _concierge_reply())

    assert api._detect_route(ACTION) == "agent"
    out = api._process_chat_query(ACTION)

    assert out['response'] == "Captured a snapshot from the camera."
    assert out['team']['intent'] == "camera_snapshot"
    assert legacy["agent"] == [] and legacy["direct"] == []


def test_kill_switch_restores_the_legacy_path(api, legacy, monkeypatch):
    monkeypatch.setenv(api._CHAT_VIA_TEAM_ENV, "0")
    called = []
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: called.append(text))

    out = api._process_chat_query(ACTION)

    assert out['response'] == "LEGACY-AGENT"
    assert called == []
    assert legacy["agent"] == [ACTION]


@pytest.mark.parametrize("kill_switch", ["1", "0"])
def test_knowledge_questions_still_go_to_rag(api, legacy, monkeypatch, kill_switch):
    monkeypatch.setenv(api._CHAT_VIA_TEAM_ENV, kill_switch)
    called = []
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: called.append(text))

    assert api._detect_route(KNOWLEDGE) == "rag"
    out = api._process_chat_query(KNOWLEDGE)

    assert out['response'] == "LEGACY-RAG"
    assert called == []


def test_library_route_never_reaches_the_team(api, legacy, monkeypatch):
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    called = []
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: called.append(text))

    msg = "what libraries do you have indexed"
    assert api._detect_route(msg) == "library"
    assert api._process_chat_query(msg)['response'] == "LEGACY-LIBRARY"
    assert called == []


# ── multi-step house requests reach the team ────────────────────────
#
# Live on the Orin these came back from RAG as "I'm not able to read the
# temperature sensor or inventory devices directly from here", with ZIM
# knowledge sources attached — the Concierge was never asked. The sources are
# the diagnostic tell: a degraded HOUSE answer that cites the library is a
# routing miss, not an agent failure, so each case asserts them empty.

MULTI_STEP_HOUSE = [
    "check the temperature in the living room and then tell me "
    "what devices are here",
    "read the living room temperature and then show me every device",
    "list my devices and then check the living room temperature",
    "what is the temperature in the living room and then list my devices",
    "what devices are here",
]


#: "the living room temperature" has a deterministic
#: Home Assistant sensor reader, the way "which lights are on" already has one.
#: A clause that reader claims never reaches the Concierge — which is the same
#: direction of travel this test defends, one layer earlier: the property here
#: is that a HOUSE question is answered from the HOUSE, never from the library.
_LIVING_ROOM_STATES = [
    {"entity_id": "sensor.living_room_temperature", "state": "72",
     "attributes": {"friendly_name": "Living Room Temperature",
                    "device_class": "temperature",
                    "unit_of_measurement": "°F"}},
]


class _StubBridge:
    def get_all_states(self):
        return list(_LIVING_ROOM_STATES)


@pytest.mark.parametrize("msg", MULTI_STEP_HOUSE)
def test_house_requests_reach_the_team_not_the_library(api, legacy, monkeypatch,
                                                       msg):
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    from puffpai.agents import household_reads as _hr
    monkeypatch.setattr(_hr, "_ha_bridge", lambda bridge=None: _StubBridge())
    seen = []

    def concierge(text, requester="owner", **kw):
        seen.append(text)
        return _concierge_reply(reply="The living room is 72 degrees. "
                                     "I found 111 devices.",
                                intent="plan")

    monkeypatch.setattr(api, "_route_via_concierge", concierge)

    assert api._detect_route(msg) == "agent", msg
    out = api._process_chat_query(msg)

    # The property this test defends is WHERE the request goes: the Concierge,
    # never RAG. A SEQUENCED request reaches it one clause at a time,
    # because handing the planner a whole sequenced
    # utterance is what let it answer "your pantry is currently empty" for a
    # pantry holding one item. Every clause still reaches the team, in the
    # order asked, and the reply still comes from the team.
    from puffpai import compound_turns as ct
    clauses = ct.split_clauses(msg) or [msg]
    # A clause with its own deterministic household reader is answered from the
    # live store and never delegated; every other clause still reaches the
    # Concierge, in the order asked.
    assert seen == [c for c in clauses if _hr.match(c) is None], msg
    assert "72" in out['response']
    assert out['sources'] == [], "a house answer citing the library is the bug"
    assert legacy["rag"] == []
    if any(_hr.match(c) == "room_temperature" for c in clauses):
        assert "Living Room Temperature" in out['response'], (
            "the room-temperature clause must be answered from the sensor")


# ── fallback ────────────────────────────────────────────────────────

def test_team_disabled_or_bus_missing_falls_back(api, legacy, monkeypatch):
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    monkeypatch.setattr(api, "_get_agent_team", lambda: None)

    out = api._process_chat_query(ACTION)
    assert out['response'] == "LEGACY-AGENT"


def test_concierge_error_falls_back(api, legacy, monkeypatch):
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)

    def boom(text, requester="owner", **kw):
        raise RuntimeError("bus exploded")

    monkeypatch.setattr(api, "_route_via_concierge", boom)
    assert api._process_chat_query(ACTION)['response'] == "LEGACY-AGENT"


@pytest.mark.parametrize("reply", [
    None,
    {"ok": False},
    _concierge_reply(handled=False),
    _concierge_reply(reply=None),
])
def test_unhandled_concierge_replies_fall_back(api, legacy, monkeypatch, reply):
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: reply)
    assert api._process_chat_query(ACTION)['response'] == "LEGACY-AGENT"


# ── the voice/stream helper inherits the same behaviour ─────────────

def test_voice_path_uses_the_team_too(api, legacy, monkeypatch):
    """/api/chat and the non-RAG branch of /chat/stream go through here."""
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: _concierge_reply())

    out = api._process_voice_query(ACTION)
    assert "Captured a snapshot" in out
    assert legacy["agent"] == []


def test_voice_path_falls_back_when_unhandled(api, legacy, monkeypatch):
    monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: _concierge_reply(handled=False))
    assert "LEGACY-AGENT" in api._process_voice_query(ACTION)


# ── kill-switch parsing ─────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, True), ("1", True), ("true", True), ("ON", True),
    ("0", False), ("false", False), ("no", False), ("", False),
])
def test_kill_switch_parsing(api, monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(api._CHAT_VIA_TEAM_ENV, raising=False)
    else:
        monkeypatch.setenv(api._CHAT_VIA_TEAM_ENV, value)
    assert api._chat_via_team_enabled() is expected


# ── clock questions never reach a model ─────────────────────────────

def test_clock_questions_answer_from_the_box_clock(api, legacy):
    out = api._process_chat_query("What time is it right now?")
    assert out['response'].startswith("It's ")
    assert legacy["agent"] == [] and legacy["rag"] == []


@pytest.mark.parametrize("msg", [
    "what time is the meeting tomorrow",
    "set a timer for 10 minutes",
    "cancel the three minute timer",
])
def test_clock_regex_keeps_off_other_time_phrases(api, msg):
    assert api._try_time_answer(msg) is None


@pytest.mark.parametrize("msg", [
    "do you have a dashboard I can look at?",
    "tell me about your agent team",
    "which model are you running?",
])
def test_self_referential_questions_are_persona_turns(api, msg):
    assert api._detect_route(msg) == "rag"


def test_self_reference_does_not_steal_house_turns(api):
    assert api._detect_route("turn off the porch light") == "agent"
    # House nouns are not self-descriptions — the hook must not match them.
    # ("list YOUR devices" routes rag today, but that's a pre-existing router
    # keyword gap — "my devices" matches, "your devices" doesn't — not this
    # hook's doing.)
    assert not api._SELF_REFERENCE_RE.search("list your devices")
    assert api._detect_route("list my devices") == "agent"
    # The launch-verb guard skips the hook; "open your dashboard" keeps
    # whatever route the funnel always gave it (it predates this hook).
    assert api._SELF_REFERENCE_RE.search("open your dashboard")
    import re as _re
    assert _re.search(r"\b(?:open|launch|start|show|pull up)\b",
                      "open your dashboard")


# ── the pre-team instant layer: weather + timers (live regressions) ──

class FakeTool:
    def __init__(self, result):
        self.result, self.calls = result, []

    def invoke(self, arg):
        self.calls.append(arg)
        return self.result


def _instant_tools(api, monkeypatch, **tools):
    class T:
        tool_dict = dict(tools)

        def run(self, message, history):
            return "LEGACY-AGENT"
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: T())
    monkeypatch.setattr(api, "_summarize_tool_result",
                        lambda msg, raw: raw)
    return T.tool_dict


def test_outdoor_temperature_is_weather_not_a_house_sensor(api, legacy, monkeypatch):
    """Live: 'what's the temperature outside?' answered with INDOOR sensors."""
    tools = _instant_tools(api, monkeypatch,
                           auto_weather_forecast=FakeTool("SUNNY-99"))
    out = api._process_chat_query("what is the temperature outside?")
    assert out['response'] == "SUNNY-99"
    assert tools['auto_weather_forecast'].calls == ['']
    assert legacy["agent"] == [] and legacy["rag"] == []


@pytest.mark.parametrize("msg", [
    "can you tell me the temperature outside, please?",
    "is it going to rain tomorrow",
    "how hot is it out there",
    "what's the weather like",
])
def test_weather_phrasings_hit_the_fastpath(api, legacy, monkeypatch, msg):
    _instant_tools(api, monkeypatch, auto_weather_forecast=FakeTool("WX"))
    assert api._process_chat_query(msg)['response'] == "WX"


@pytest.mark.parametrize("msg", [
    "what is the temperature in the living room?",
    "how warm is it in here",
    "what's the humidity in the basement",
])
def test_room_questions_never_hit_the_weather_fastpath(api, msg):
    assert api._try_weather_answer(msg) is None


def test_set_a_timer_is_a_countdown_not_a_thermostat(api, legacy, monkeypatch):
    """Live: the set_climate rule ate 'set a timer for 10 minutes'."""
    tools = _instant_tools(api, monkeypatch,
                           app_set_timer=FakeTool("Timer set for 10 minutes."))
    out = api._process_chat_query("set a timer for 10 minutes")
    assert out['response'] == "Timer set for 10 minutes."
    # Labelled by duration so "cancel the ten minute timer" can find it.
    assert tools['app_set_timer'].calls == [
        {"description": "10 minute", "minutes": 10, "hours": 0, "seconds": 0}]
    assert legacy["agent"] == []


@pytest.mark.parametrize("msg,field,n", [
    ("set a timer for ten minutes", "minutes", 10),
    ("start a timer for two hours", "hours", 2),
    ("set the oven timer for forty-five minutes", "minutes", 45),
    ("set a timer for 90 seconds", "seconds", 90),
])
def test_timer_durations_parse_including_word_numbers(api, monkeypatch, msg, field, n):
    tools = _instant_tools(api, monkeypatch, app_set_timer=FakeTool("ok"))
    api._process_chat_query(msg)
    assert tools['app_set_timer'].calls[0][field] == n


def test_a_timer_without_a_duration_falls_through(api, monkeypatch):
    _instant_tools(api, monkeypatch, app_set_timer=FakeTool("ok"))
    assert api._try_timer_set("set a timer") is None


def test_cancel_timer_is_intercepted_before_the_team(api, legacy, monkeypatch):
    tools = _instant_tools(api, monkeypatch,
                           app_delete_alarm=FakeTool("Cancelled."),
                           app_dismiss_alarm=FakeTool("Dismissed."))
    out = api._process_chat_query("cancel the ten minute timer")
    assert out['response'] == "Cancelled."
    assert tools['app_delete_alarm'].calls == ["ten minute"]
    assert legacy["agent"] == []
