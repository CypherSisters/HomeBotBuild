""""what's the air quality outside today" must answer.

Without an air-quality phrasing on the weather path:

    "what's the air quality outside today"
      -> ``Route: agent`` -> the model asks the user for a city/ZIP.
    "what's the temperature outside" (the very next turn)
      -> weather fastpath -> "Current weather in <city>...
         Air quality (AQI): 41 - Good"

The number is already in the payload, for the configured ``location``. Only
the PHRASING has to reach the weather path. These tests pin: the phrasings
reach it, the reply LEADS with the reading and stays speakable, no model is
consulted, no location is ever asked for, the weather answers are unchanged,
and the four "air" appliances keep their own paths.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server as api  # noqa: E402


ENV = {
    "high": 98, "low": 79,
    "us_aqi": 41, "aqi_label": "Good", "pm2_5": 6.2,
    "air_breakdown": {"pollen": {"level": 1, "label": "Moderate",
                                 "detail": "ragweed season"}},
}

#: Every way the box could reveal it went to a model or to the tool wrapper.
_FORBIDDEN_LOCATION_WORDS = (
    "what city", "which city", "your city", "zip", "postal",
    "where do you live", "where are you located", "your location",
    "tell me your", "provide your",
)


@pytest.fixture
def air(monkeypatch):
    """Patch the payload source + forbid every model/tool call."""
    from puffpai import dashboard_widgets as dwid
    calls = {"env": 0, "llm": 0, "tool": 0}

    def _env(*a, **k):
        calls["env"] += 1
        return dict(ENV)

    def _no_llm(*a, **k):
        calls["llm"] += 1
        raise AssertionError("the air-quality answer must not call the LLM")

    def _no_tools(*a, **k):
        calls["tool"] += 1
        raise AssertionError("the air-quality answer must not invoke tools")

    monkeypatch.setattr(dwid, "fetch_environment", _env)
    monkeypatch.setattr(api, "_summarize_tool_result", _no_llm)
    monkeypatch.setattr(api, "_get_assistant_tools", _no_tools)
    monkeypatch.setattr(api, "_home_city", lambda: "the default city")
    return calls


def _assert_clean(reply):
    assert reply, "the fastpath declined the turn"
    low = reply.lower()
    for word in _FORBIDDEN_LOCATION_WORDS:
        assert word not in low, f"asked for a location: {reply!r}"
    assert "?" not in reply, f"answered with a question: {reply!r}"


# ── 1. the phrasings reach the weather path ────────────────────────────────

POSITIVE = [
    "what's the air quality outside today",
    "how's the air outside",
    "hows the air outside",            # ASR drops the apostrophe
    "what is the aqi",
    "is the air quality bad today",
    "how bad is the pollution outside",
    "is it smoggy out there today",
    "is the air safe to breathe outside",
    "what's the air index right now",
]


@pytest.mark.parametrize("said", POSITIVE)
def test_air_quality_phrasings_are_answered_with_the_reading(said, air):
    reply = api._try_weather_answer(said)
    _assert_clean(reply)
    assert reply.startswith("Air quality outside is 41"), reply
    assert "good" in reply.lower()
    assert "the default city" in reply
    assert air["llm"] == 0 and air["tool"] == 0
    assert air["env"] == 1


def test_the_answer_is_short_enough_to_speak(air):
    reply = api._try_weather_answer("what's the air quality outside today")
    assert len(reply) <= 140, reply


def test_a_moderate_reading_adds_exactly_one_clause_of_advice(air,
                                                              monkeypatch):
    from puffpai import dashboard_widgets as dwid
    monkeypatch.setattr(dwid, "fetch_environment",
                        lambda *a, **k: dict(ENV, us_aqi=88,
                                             aqi_label="Moderate"))
    reply = api._try_weather_answer("how's the air outside")
    assert reply.startswith("Air quality outside is 88, moderate,")
    assert "sensitive" in reply.lower()


def test_good_air_gets_no_caveat(air):
    reply = api._try_weather_answer("how's the air outside")
    assert "sensitive" not in reply.lower()
    assert "limit" not in reply.lower()


def test_a_pollen_question_adds_the_pollen_risk(air):
    reply = api._try_weather_answer("is the pollen bad outside today")
    _assert_clean(reply)
    assert "Air quality outside is 41" in reply
    assert "pollen risk is moderate" in reply.lower()
    assert "ragweed" in reply.lower()


def test_pollen_detail_is_absent_when_the_payload_has_none(air, monkeypatch):
    from puffpai import dashboard_widgets as dwid
    monkeypatch.setattr(dwid, "fetch_environment",
                        lambda *a, **k: {"us_aqi": 41, "aqi_label": "Good"})
    reply = api._try_weather_answer("is the pollen bad outside today")
    _assert_clean(reply)
    assert "pollen" not in reply.lower()


# ── 2. no reading -> say so, never ask where the user lives ────────────────

def test_a_closed_door_is_stated_honestly(air, monkeypatch):
    from puffpai import dashboard_widgets as dwid
    monkeypatch.setattr(
        dwid, "fetch_environment",
        lambda *a, **k: {"blocked": "The air-quality door is closed."})
    reply = api._try_weather_answer("what's the air quality outside today")
    _assert_clean(reply)
    low = reply.lower()
    assert "air-quality feed is off" in low
    assert "41" not in reply
    assert "the default city" in reply


def test_a_missing_aqi_field_is_stated_honestly(air, monkeypatch):
    from puffpai import dashboard_widgets as dwid
    monkeypatch.setattr(dwid, "fetch_environment",
                        lambda *a, **k: {"high": 98, "low": 79})
    reply = api._try_weather_answer("how's the air outside")
    _assert_clean(reply)
    assert "don't have an air-quality reading" in reply.lower()


def test_a_broken_payload_source_still_answers_honestly(air, monkeypatch):
    from puffpai import dashboard_widgets as dwid

    def _boom(*a, **k):
        raise RuntimeError("open-meteo down")

    monkeypatch.setattr(dwid, "fetch_environment", _boom)
    reply = api._try_weather_answer("what's the air quality outside today")
    _assert_clean(reply)
    assert "don't have an air-quality reading" in reply.lower()


# ── 3. the weather answers are unchanged ───────────────────────────────────

WEATHER = [
    # "do I need a jacket today" lives in the attire test below: it still
    # reaches the forecast, but the reply is a direct yes/no rather than the
    # summarised snapshot.
    "will it rain today",
    "what's the temperature outside",
    "what is the weather like right now",
    "is it hot out",
]


@pytest.mark.parametrize("said", WEATHER)
def test_weather_questions_still_get_the_full_forecast_answer(said,
                                                              monkeypatch):
    """These must go through the tool + summarizer, NOT the air-quality line."""
    seen = {}

    class _Tool:
        def invoke(self, arg):
            seen["invoked"] = True
            return "Weather for Springfield, IL: currently 92F; air quality AQI 41 (Good)."

    class _Tools:
        tool_dict = {"auto_weather_forecast": _Tool()}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    monkeypatch.setattr(api, "_summarize_tool_result",
                        lambda q, raw: f"SUMMARY::{raw}")
    reply = api._try_weather_answer(said)
    assert seen.get("invoked"), f"{said!r} does not reach the weather tool"
    assert reply.startswith("SUMMARY::")
    assert "AQI 41" in reply, "the full weather answer must keep its AQI clause"
    assert not reply.startswith("Air quality outside is")


# ── 4. negative controls ───────────────────────────────────────────────────

NEGATIVE = [
    "is the air conditioner on",
    "turn on the air purifier",
    "how do I purify water",
    "what's in the air fryer",
]


@pytest.mark.parametrize("said", NEGATIVE)
def test_air_appliances_and_water_keep_their_own_paths(said, air):
    assert api._try_weather_answer(said) is None
    assert air["env"] == 0


def test_indoor_air_questions_are_not_the_outdoor_forecast(air):
    assert api._try_weather_answer("how's the air quality in the bedroom") is None


# ── 5. the agent-graph fallback binds the tool ─────────────────────────────

def test_the_weather_tool_description_covers_air_quality():
    src = Path(__file__).resolve().parents[1] / "src/puffpai/assistant_tools.py"
    text = src.read_text(encoding="utf-8")
    start = text.index("def auto_weather_forecast")
    doc = text[start:text.index('"""', text.index('"""', start) + 3)].lower()
    for word in ("air quality", "aqi", "pollution", "pollen"):
        assert word in doc, f"{word!r} missing from the tool description"
    assert "never ask" in doc


def test_the_weather_tool_defaults_to_the_configured_location():
    src = (Path(__file__).resolve().parents[1]
           / "src/puffpai/assistant_tools.py").read_text(encoding="utf-8")
    assert 'def auto_weather_forecast(location: str = "") -> str:' in src


def test_the_registry_row_carries_the_air_quality_keywords():
    from puffpai import capability_registry as reg
    row = next(c for c in reg.CAPABILITIES if c.id == "weather.current")
    joined = " ".join(row.keywords).lower()
    for word in ("air quality", "aqi", "pollution", "smog", "pollen"):
        assert word in joined, f"{word!r} missing from weather.current keywords"


def test_the_agent_prompt_lists_air_quality_for_the_weather_tool():
    src = (Path(__file__).resolve().parents[1]
           / "src/puffpai/agent_graph.py").read_text(encoding="utf-8")
    start = src.index("auto_weather_forecast (only if user said")
    window = src[start:start + 400].lower()
    assert "air quality" in window and "aqi" in window
    assert "never ask the user for a city" in window


# ── 6. "look on your dashboard" ────────────────────────────────────────────

DASHBOARD = [
    "you can look on your dashboard, it has the air quality readings",
    "check the dashboard for the air quality",
    "it's on your dashboard — the air quality",
]


@pytest.mark.parametrize("said", DASHBOARD)
def test_a_dashboard_reference_is_answered_from_the_same_data(said, air):
    reply = api._try_weather_answer(said)
    _assert_clean(reply)
    low = reply.lower()
    assert "can't see your screen" in low, reply
    assert "same live data" in low
    assert "41" in reply and "good" in low


def test_a_non_air_dashboard_sentence_is_still_declined(air):
    assert api._try_weather_answer("add a weather widget to my dashboard") is None
    assert api._try_weather_answer("show me the dashboard") is None
    assert air["env"] == 0


# ── 7. no built-in location, no guessed pollen ─────────────────────────────

def test_no_coordinates_means_no_request_and_no_numbers(monkeypatch):
    from puffpai import dashboard_widgets as dwid
    from puffpai import weather_alerts as wa
    import puffpai.security.egress_doors as ed

    def _no_egress(*a, **k):  # pragma: no cover - fails the test if reached
        raise AssertionError("no coordinates must mean no weather request")

    from puffpai import household_geo as geo
    monkeypatch.setattr(wa, "household_coordinates", lambda: (None, None))
    monkeypatch.setattr(geo, "household_coordinates", lambda: (None, None))
    monkeypatch.setattr(ed, "egress_get", _no_egress)
    # No request and no numbers: only the plain "not set" message.
    assert dwid.fetch_environment() == {"unset": geo.UNSET_MESSAGE}


def test_pollen_is_absent_without_a_measurement():
    from puffpai import dashboard_widgets as dwid
    b = dwid._air_breakdown({"us_aqi": 30, "pm10": 10}, 50, 0)
    assert "pollen" not in b
    assert b["asthma"]["level"] == 0


def test_measured_pollen_is_reported():
    from puffpai import dashboard_widgets as dwid
    b = dwid._air_breakdown({"grass_pollen": 90}, None, None)
    assert b["pollen"]["level"] == 2
    assert b["pollen"]["detail"] == "grass 90 grains/m3"
