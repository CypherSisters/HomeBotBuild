"""The severe-weather answer must state ONE thing, from the source.

The failure mode:

    "any severe weather alerts"
      -> "No severe weather alerts are currently active in Denver, CO.
          The system couldn't retrieve any alerts because it doesn't have
          your location coordinates.... Add or confirm your coordinates in
          Settings..."

    "whats the weather like right now" (same session, moments earlier)
      -> real conditions for the default city: 92F, 52% humidity,
         high 98/low 79, AQI 37

A definite all-clear AND a "the check could not run" in one reply, with the
second claim independently false. It happens when ``egress_doors`` reads
``"geocode": false`` while ``"weather": true``: a nominatim step refuses
(honestly) and an LLM summarizer prepends a fabricated all-clear.

Every state below is pinned separately, because the defect is two of them
appearing at once.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import weather_alerts as wa  # noqa: E402

#: The primary prompt.
PROMPT = "any severe weather alerts"

NOON = datetime(2026, 9, 9, 12, 34)

#: Phrases that must never appear next to a definite answer.
_CANT_CHECK_WORDS = ("couldn't", "could not", "unable", "don't have",
                     "doesn't have", "no coordinates", "coordinates in")
_ALL_CLEAR_WORDS = ("no active severe weather alerts", "no severe weather "
                    "alerts are currently active")


def _has_all_clear(text):
    low = text.lower()
    return any(w in low for w in _ALL_CLEAR_WORDS)


def _has_cant_check(text):
    low = text.lower()
    return any(w in low for w in _CANT_CHECK_WORDS)


# ── the four states, each stated exactly once ───────────────────────────────

def test_no_alerts_is_a_clean_negative_naming_time_and_source():
    reply = wa.format_alert_reply(
        {"state": wa.STATE_NONE, "location": "Springfield, IL",
         "alerts": [], "source": wa.NWS_SOURCE, "detail": ""}, now=NOON)
    assert "No active severe weather alerts for Springfield, IL" in reply
    assert "12:34 PM" in reply
    assert wa.NWS_SOURCE in reply
    # The defect: this reply also claimed the check could not run.
    assert not _has_cant_check(reply), reply


def test_active_alerts_are_listed_from_the_data():
    reply = wa.format_alert_reply(
        {"state": wa.STATE_ACTIVE, "location": "Springfield, IL",
         "alerts": [{"event": "Heat Advisory", "severity": "Moderate",
                     "headline": "Heat Advisory until 8 PM CDT"},
                    {"event": "Flood Watch", "severity": "Severe",
                     "headline": "Flood Watch through Wednesday evening"}],
         "source": wa.NWS_SOURCE, "detail": ""}, now=NOON)
    assert "Heat Advisory" in reply and "Flood Watch" in reply
    assert "Moderate" in reply and "Severe" in reply
    assert "2 active severe weather alerts" in reply
    assert not _has_all_clear(reply), reply


def test_source_down_never_says_no_alerts():
    reply = wa.format_alert_reply(
        {"state": wa.STATE_SOURCE_DOWN, "location": "Springfield, IL",
         "alerts": [], "source": wa.NWS_SOURCE, "detail": "timeout"}, now=NOON)
    assert _has_cant_check(reply)
    assert not _has_all_clear(reply), reply


def test_location_unset_says_only_that():
    reply = wa.format_alert_reply(
        {"state": wa.STATE_LOCATION_UNSET, "location": "",
         "alerts": [], "source": "", "detail": ""}, now=NOON)
    assert "location" in reply.lower()
    assert not _has_all_clear(reply), reply


def test_shut_door_is_reported_as_a_door_not_as_an_all_clear():
    reply = wa.format_alert_reply(
        {"state": wa.STATE_DOOR_CLOSED, "location": "Springfield, IL",
         "alerts": [], "source": wa.NWS_SOURCE,
         "detail": "Weather needs the 'weather' egress door, which is off."},
        now=NOON)
    assert "[Egress door closed]" in reply
    assert not _has_all_clear(reply), reply


# ── the fetch itself ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


@pytest.fixture
def no_automation(monkeypatch):
    """The Node-RED flow is silent, so the NWS is the source under test."""
    monkeypatch.setattr(wa, "_alerts_from_automation",
                        lambda lat, lon, loc: (wa.STATE_SOURCE_DOWN, [], "down"))


def test_nws_empty_feature_list_is_none_not_source_down(monkeypatch,
                                                        no_automation):
    import puffpai.security.egress_doors as ed
    monkeypatch.setattr(ed, "egress_get",
                        lambda *a, **k: _Resp({"features": []}))
    status = wa.alert_status(location="Springfield, IL", lat=39.8, lon=-89.6)
    assert status["state"] == wa.STATE_NONE
    assert status["source"] == wa.NWS_SOURCE


def test_nws_features_become_structured_alerts(monkeypatch, no_automation):
    import puffpai.security.egress_doors as ed
    payload = {"features": [{"properties": {
        "event": "Severe Thunderstorm Warning", "severity": "Severe",
        "headline": "Severe Thunderstorm Warning until 7:15 PM CDT"}}]}
    monkeypatch.setattr(ed, "egress_get", lambda *a, **k: _Resp(payload))
    status = wa.alert_status(location="Springfield, IL", lat=39.8, lon=-89.6)
    assert status["state"] == wa.STATE_ACTIVE
    assert status["alerts"][0]["event"] == "Severe Thunderstorm Warning"


def test_shut_weather_door_reports_door_closed(monkeypatch, no_automation):
    import puffpai.security.egress_doors as ed

    def _blocked(*a, **k):
        raise ed.EgressBlocked("weather", "flag off",
                               "The 'weather' door is off.")

    monkeypatch.setattr(ed, "egress_get", _blocked)
    status = wa.alert_status(location="Springfield, IL", lat=39.8, lon=-89.6)
    assert status["state"] == wa.STATE_DOOR_CLOSED
    assert not _has_all_clear(wa.format_alert_reply(status, now=NOON))


def test_unknown_coordinates_is_the_only_location_claim(monkeypatch):
    monkeypatch.setattr(wa, "household_coordinates", lambda: (None, None))
    status = wa.alert_status(location="")
    assert status["state"] == wa.STATE_LOCATION_UNSET


def test_configured_coordinates_resolve_without_the_geocode_door(monkeypatch):
    """The coordinates the weather card uses come from config, so a box with
    them set never says "it doesn't have your location coordinates"."""
    import types
    import puffpai.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_config", lambda: types.SimpleNamespace(
        latitude=39.8, longitude=-89.6, location="Springfield, IL"))
    assert wa.household_coordinates() == (39.8, -89.6)

def test_a_saved_home_point_resolves_without_the_geocode_switch(
        tmp_path, monkeypatch):
    """The coordinates the weather card fetches with need no geocode switch.

    Once the owner has saved a home point, the coordinates the weather card
    fetches with are on the box, no geocode switch required: the owner overlay
    (``puffpai.home_point``) is layered over the config at load, and
    ``puffpai.household_geo`` is the one resolver both surfaces use. No build
    ships a point, so this is set up here with a synthetic one.
    """
    import puffpai.config as config_mod
    from puffpai import household_geo as geo
    from puffpai import home_point as hp
    overlay = tmp_path / "config" / "home_point.json"
    hp.set_path_override(str(overlay))
    try:
        hp.write(10.0, 20.0)
        built = config_mod._build_config()
        monkeypatch.setattr(config_mod, "get_config", lambda **k: built)
        geo.reset_cache()
        lat, lon = wa.household_coordinates()
        assert (lat, lon) == (10.0, 20.0)
    finally:
        hp.set_path_override(None)
        geo.reset_cache()


def test_there_are_no_built_in_coordinates(monkeypatch):
    import types
    import puffpai.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_config", lambda: types.SimpleNamespace(
        latitude=None, longitude=None, location=""))
    assert wa.household_coordinates() == (None, None)
    assert wa.alert_status()["state"] == wa.STATE_LOCATION_UNSET


# ── the fastpath wiring ─────────────────────────────────────────────────────

def test_fastpath_returns_the_deterministic_reply_not_llm_prose(monkeypatch):
    import puffpai.api_server as api

    monkeypatch.setattr(wa, "alert_status", lambda **k: {
        "state": wa.STATE_NONE, "location": "Springfield, IL", "alerts": [],
        "source": wa.NWS_SOURCE, "detail": ""})

    def _no_llm(*a, **k):  # pragma: no cover - fails the test if reached
        raise AssertionError("the alerts path must not call the summarizer")

    monkeypatch.setattr(api, "_summarize_tool_result", _no_llm)
    reply = api._try_weather_alerts(PROMPT)
    assert reply is not None
    assert "No active severe weather alerts for Springfield, IL" in reply
    assert not _has_cant_check(reply), reply


@pytest.mark.parametrize("phrase", [
    "any severe weather alerts",
    "are there any weather warnings",
    "is there a tornado warning",
    "any storm advisories today",
])
def test_alert_phrasings_still_claim_the_path(monkeypatch, phrase):
    import puffpai.api_server as api
    monkeypatch.setattr(wa, "alert_status", lambda **k: {
        "state": wa.STATE_NONE, "location": "Springfield, IL", "alerts": [],
        "source": wa.NWS_SOURCE, "detail": ""})
    assert api._try_weather_alerts(phrase) is not None


@pytest.mark.parametrize("phrase", [
    "set an alert for my meeting",
    "whats in the news alerts",
])
def test_non_weather_alert_phrasings_are_declined(phrase):
    import puffpai.api_server as api
    assert api._try_weather_alerts(phrase) is None
