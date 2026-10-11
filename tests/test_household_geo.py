"""The house's position: config coordinates, one geocode, or honestly unknown.

``config.location`` is a place NAME. If the alerts path fell back to
built-in coordinates, the NWS query would run for one place while the reply
said "No active severe weather alerts for <their city>".

Pinned here:

* ``latitude``/``longitude`` are real config fields, validated on load.
* Set → used as-is, and NOTHING crosses the geocode door.
* Unset + geocode door open → looked up once, cached, reused.
* Unset + geocode door shut → ``(None, None)``; the alerts reply is the
  location-unset state and never an all-clear; the weather card reports
  ``unset`` and fetches nothing.
* The weather card and the alerts ask about the SAME point.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.config as config_mod  # noqa: E402
import puffpai.security.egress_doors as ed  # noqa: E402
from puffpai import dashboard_widgets as dw  # noqa: E402
from puffpai import household_geo as geo  # noqa: E402
from puffpai import weather_alerts as wa  # noqa: E402

NOON = datetime(2026, 9, 9, 12, 34)
#: Synthetic, in open ocean: no tracked file carries a real home point.
HOUSE = (12.5, -34.25)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    # No owner home point leaks in from wherever the suite runs.
    from puffpai import home_point as hp
    hp.set_path_override(str(tmp_path / "no-owner" / "home_point.json"))
    geo.reset_cache()
    dw._env_cache.clear()
    yield
    hp.set_path_override(None)
    geo.reset_cache()
    dw._env_cache.clear()


@pytest.fixture
def cfg(monkeypatch):
    """Install a config with chosen place/coordinates; returns a setter:
    ``cfg(location, lat, lon)``. Built on the real repo config so code that
    reads other sections (models, api_server, ...) keeps working."""
    import dataclasses
    base = config_mod._build_config()
    holder = {}

    def _set(location="Denver, CO", lat=None, lon=None):
        holder["cfg"] = dataclasses.replace(base, location=location,
                                            latitude=lat, longitude=lon)
        return holder["cfg"]

    monkeypatch.setattr(config_mod, "get_config", lambda **k: holder["cfg"])
    _set()
    return _set


@pytest.fixture
def geocode_calls(monkeypatch):
    """Record geocode-door traffic; ``calls[0]["mode"]`` picks the behaviour."""
    calls = []
    mode = {"mode": "ok"}

    def _egress_get(door_id, url, **kw):
        calls.append({"door": door_id, "url": url, **kw})
        if mode["mode"] == "shut":
            raise ed.EgressBlocked(door_id, "flag off",
                                   "The 'geocode' door is off.")
        if mode["mode"] == "down":
            raise ConnectionError("no internet")
        if mode["mode"] == "empty":
            return _Resp([])
        return _Resp([{"lat": str(HOUSE[0]), "lon": str(HOUSE[1])}])

    monkeypatch.setattr(ed, "egress_get", _egress_get)
    calls_obj = SimpleNamespace(calls=calls, mode=mode)
    return calls_obj


# ── config fields ───────────────────────────────────────────────────────────

def _build(tmp_path, **raw):
    # The directory _build_config reads for this root (a test-isolated copy
    # when $PUFFPAI_CONFIG_DIR is set, else <root>/config).
    d = Path(config_mod._config_dir_for(str(tmp_path)))
    d.mkdir(parents=True, exist_ok=True)
    (d / "puffpai_config.json").write_text(json.dumps(raw))
    return config_mod._build_config(str(tmp_path))


def test_config_has_optional_coordinates_defaulting_to_none(tmp_path):
    cfg = _build(tmp_path, location="Denver, CO")
    assert cfg.location == "Denver, CO"
    assert cfg.latitude is None and cfg.longitude is None


def test_config_parses_coordinates_as_floats(tmp_path):
    cfg = _build(tmp_path, latitude="12.5", longitude=-34.25)
    assert cfg.latitude == pytest.approx(12.5)
    assert cfg.longitude == pytest.approx(-34.25)
    assert isinstance(cfg.latitude, float)


@pytest.mark.parametrize("lat,lon", [
    ("north", 20.0),        # not a number
    (95.0, 20.0),           # latitude out of range
    (10.0, 181.0),          # longitude out of range
    (10.0, None),           # half a position
    (None, 20.0),
    (True, 20.0),           # bool is not a coordinate
])
def test_unusable_coordinates_load_as_unset_not_as_a_crash(tmp_path, lat, lon):
    cfg = _build(tmp_path, latitude=lat, longitude=lon)
    assert cfg.latitude is None and cfg.longitude is None


def _shipped_config():
    return json.loads((Path(__file__).resolve().parents[1] / "config"
                       / "puffpai_config.json").read_text(encoding="utf-8"))


_NOT_A_CHECKOUT = not (Path(__file__).resolve().parents[1] / ".git").exists()


@pytest.mark.skipif(_NOT_A_CHECKOUT, reason="repo checkout only (no .git here; installed tree)")
def test_shipped_config_carries_no_coordinates():
    """No build ships a home point: the owner sets it once, on the box, and it
    lives only in the box's data dir (``puffpai.home_point``)."""
    raw = _shipped_config()
    assert raw.get("latitude") is None
    assert raw.get("longitude") is None


@pytest.mark.skipif(_NOT_A_CHECKOUT, reason="repo checkout only (no .git here; installed tree)")
def test_the_code_has_no_built_in_coordinates_any_more():
    assert not hasattr(dw, "_DEFAULT_LAT")
    assert not hasattr(dw, "_DEFAULT_LON")
    # ...and neither does the shipped config: built from the real repo with no
    # owner overlay, the house's position is unknown.
    assert geo.valid_pair(_shipped_config().get("latitude"),
                          _shipped_config().get("longitude")) is None
    built = config_mod._build_config()
    assert built.latitude is None and built.longitude is None


# ── set ─────────────────────────────────────────────────────────────────────

def test_set_coordinates_are_used_and_the_geocode_door_is_never_touched(
        cfg, geocode_calls):
    cfg("Denver, CO", 12.5, -34.25)
    assert geo.household_coordinates() == pytest.approx(HOUSE)
    assert geo.resolve()["source"] == geo.SOURCE_CONFIG
    assert geocode_calls.calls == []


def test_alerts_use_the_configured_coordinates(cfg, geocode_calls, monkeypatch):
    cfg("Denver, CO", 12.5, -34.25)
    monkeypatch.setattr(wa, "_alerts_from_automation",
                        lambda lat, lon, loc: (wa.STATE_SOURCE_DOWN, [], "down"))
    seen = {}

    def _nws(door_id, url, **kw):
        seen.update(door=door_id, params=kw.get("params"))
        return _Resp({"features": []})

    monkeypatch.setattr(ed, "egress_get", _nws)
    status = wa.alert_status()
    assert status["state"] == wa.STATE_NONE
    assert status["location"] == "Denver, CO"
    assert seen["door"] == "weather"
    assert seen["params"]["point"] == "12.5,-34.25"
    reply = wa.format_alert_reply(status, now=NOON)
    assert "No active severe weather alerts for Denver, CO" in reply


# ── unset, door shut ────────────────────────────────────────────────────────

def test_unset_with_geocode_door_shut_is_unknown(cfg, geocode_calls):
    cfg("Denver, CO", None, None)
    geocode_calls.mode["mode"] = "shut"
    assert geo.household_coordinates() == (None, None)
    assert geocode_calls.calls and geocode_calls.calls[0]["door"] == "geocode"


def test_unset_with_door_shut_makes_the_alerts_reply_honest(cfg, geocode_calls):
    """No coordinates must mean NO all-clear for anybody's city, and no
    NWS query for a built-in place."""
    cfg("Denver, CO", None, None)
    geocode_calls.mode["mode"] = "shut"
    status = wa.alert_status()
    assert status["state"] == wa.STATE_LOCATION_UNSET
    assert [c["door"] for c in geocode_calls.calls] == ["geocode"]  # no NWS
    reply = wa.format_alert_reply(status, now=NOON)
    low = reply.lower()
    assert "home point" in low and "settings" in low
    assert "no active severe weather alerts" not in low


def test_shut_door_is_retried_next_time_because_the_owner_may_open_it(
        cfg, geocode_calls):
    cfg("Denver, CO", None, None)
    geocode_calls.mode["mode"] = "shut"
    assert geo.household_coordinates() == (None, None)
    geocode_calls.mode["mode"] = "ok"
    assert geo.household_coordinates() == pytest.approx(HOUSE)


def test_empty_location_and_no_coordinates_asks_nobody(cfg, geocode_calls):
    cfg("", None, None)
    assert geo.household_coordinates() == (None, None)
    assert geocode_calls.calls == []
    assert wa.alert_status()["state"] == wa.STATE_LOCATION_UNSET


# ── unset, door open ────────────────────────────────────────────────────────

def test_unset_with_door_open_geocodes_once_and_caches(cfg, geocode_calls):
    cfg("Denver, CO", None, None)
    assert geo.household_coordinates() == pytest.approx(HOUSE)
    assert geo.household_coordinates() == pytest.approx(HOUSE)
    assert wa.household_coordinates() == pytest.approx(HOUSE)
    assert len(geocode_calls.calls) == 1
    call = geocode_calls.calls[0]
    assert call["door"] == "geocode"
    assert call["params"]["q"] == "Denver, CO"
    assert geo.resolve()["source"] == geo.SOURCE_GEOCODE


def test_geocode_cache_is_keyed_by_the_place_name(cfg, geocode_calls):
    cfg("Denver, CO", None, None)
    geo.household_coordinates()
    cfg("Boise, ID", None, None)
    geo.household_coordinates()
    assert [c["params"]["q"] for c in geocode_calls.calls] == ["Denver, CO",
                                                               "Boise, ID"]


@pytest.mark.parametrize("mode", ["down", "empty"])
def test_failed_lookup_is_unknown_and_not_hammered(cfg, geocode_calls, mode):
    cfg("Denver, CO", None, None)
    geocode_calls.mode["mode"] = mode
    assert geo.household_coordinates() == (None, None)
    assert geo.household_coordinates() == (None, None)
    assert len(geocode_calls.calls) == 1


# ── the weather card shares the resolver ────────────────────────────────────

def test_weather_card_fetches_the_household_point(cfg, monkeypatch):
    cfg("Denver, CO", 12.5, -34.25)
    seen = []

    def _egress_get(door_id, url, **kw):
        seen.append((door_id, kw.get("params", {})))
        if door_id == "weather":
            return _Resp({"daily": {"temperature_2m_max": [70],
                                    "temperature_2m_min": [50]},
                          "current": {}})
        return _Resp({"current": {"us_aqi": 20}})

    monkeypatch.setattr(ed, "egress_get", _egress_get)
    out = dw.fetch_environment()
    assert out["high"] == 70 and out["us_aqi"] == 20
    assert "unset" not in out
    for _door, params in seen:
        assert (params["latitude"], params["longitude"]) == HOUSE


def test_weather_card_reports_unset_and_fetches_nothing(cfg, geocode_calls):
    cfg("Denver, CO", None, None)
    geocode_calls.mode["mode"] = "shut"
    out = dw.fetch_environment()
    assert out == {"unset": geo.UNSET_MESSAGE}
    assert [c["door"] for c in geocode_calls.calls] == ["geocode"]
    assert dw._env_cache == {}  # setting coordinates must take effect at once


def test_card_and_alerts_resolve_through_one_function(cfg, monkeypatch):
    cfg("Denver, CO", 12.5, -34.25)
    asked = []

    def _egress_get(door_id, url, **kw):
        p = kw.get("params", {})
        if "point" in p:
            asked.append(tuple(float(x) for x in p["point"].split(",")))
            return _Resp({"features": []})
        asked.append((p["latitude"], p["longitude"]))
        return _Resp({"daily": {"temperature_2m_max": [1],
                                "temperature_2m_min": [0]}, "current": {}})

    monkeypatch.setattr(ed, "egress_get", _egress_get)
    monkeypatch.setattr(wa, "_alerts_from_automation",
                        lambda lat, lon, loc: (wa.STATE_SOURCE_DOWN, [], "down"))
    dw.fetch_environment()
    wa.alert_status()
    assert len(set(asked)) == 1, asked


def test_alerts_reply_is_the_one_shared_unset_sentence():
    reply = wa.format_alert_reply(
        {"state": wa.STATE_LOCATION_UNSET, "location": "Denver, CO",
         "alerts": [], "source": "", "detail": ""}, now=NOON)
    assert geo.UNSET_MESSAGE in reply


def test_resolver_reads_the_config_module_that_is_actually_installed(monkeypatch):
    """``from puffpai import config`` reads the
    ATTRIBUTE on the package, not ``sys.modules["puffpai.config"]``.

    A ``patched_modules`` swap elsewhere in the suite (tests/modpatch.py) can
    leave those two pointing at different module objects, and this resolver
    was then reading a different config than the rest of the process. Every
    test in this file passed alone and 19 failed in the full suite. The fix
    is the submodule import form; this test pins it.
    """
    import types

    import puffpai

    stub = types.ModuleType("puffpai.config")
    stub.get_config = lambda **k: SimpleNamespace(
        location="WRONG: read via the package attribute",
        latitude=None, longitude=None)
    monkeypatch.setattr(puffpai, "config", stub, raising=False)
    monkeypatch.setattr(config_mod, "get_config", lambda **k: SimpleNamespace(
        location="Denver, CO", latitude=None, longitude=None))

    assert puffpai.config is not config_mod          # the divergence exists
    assert geo.household_location() == "Denver, CO"  # and is not what we read


def test_unset_message_names_where_the_values_actually_live():
    """The one real control is the Home point card in Settings."""
    low = geo.UNSET_MESSAGE.lower()
    assert "home point" in low and "settings" in low
    assert "puffpai_config.json" not in low
    assert "\u2014" not in geo.UNSET_MESSAGE  # no em dashes in what users read


# ── concurrency, logging and the reply paths ───────────────────────────────

def test_cold_cache_under_concurrency_geocodes_exactly_once(cfg, monkeypatch):
    """The card, the forecast tool and the alerts reply resolve independently
    under the threaded API server; a cold cache must still mean ONE outbound
    request carrying the place name."""
    import threading
    import time as _time
    cfg("Denver, CO", None, None)
    calls = []

    def _slow_egress_get(door_id, url, **kw):
        calls.append(door_id)
        _time.sleep(0.05)
        return _Resp([{"lat": str(HOUSE[0]), "lon": str(HOUSE[1])}])

    monkeypatch.setattr(ed, "egress_get", _slow_egress_get)
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(geo.household_coordinates()))
        for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, calls
    assert all(r == pytest.approx(HOUSE) for r in results)


def test_zero_zero_is_unset_everywhere(tmp_path, cfg):
    assert geo.valid_pair(0, 0) is None
    built = _build(tmp_path, latitude=0, longitude=0)
    assert built.latitude is None and built.longitude is None
    cfg("", 0.0, 0.0)
    assert geo.household_coordinates() == (None, None)


def test_dashboard_weather_endpoint_names_the_configured_city_and_unset(
        cfg, geocode_calls, monkeypatch):
    import puffpai.api_server as api
    cfg("Denver, CO", None, None)
    geocode_calls.mode["mode"] = "shut"
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: (_ for _ in ()).throw(RuntimeError("no HA")))
    with api.app.test_request_context("/dashboard/weather"):
        payload = api.dashboard_weather().get_json()
    assert payload["location"] == "Denver, CO"
    assert payload["unset"] == geo.UNSET_MESSAGE
    assert payload.get("high") is None


@pytest.fixture
def blank_inventory(tmp_path, monkeypatch):
    """An inventory file that names no place of its own."""
    import puffpai.emergency_prep as ep
    path = tmp_path / "emergency_inventory.json"
    path.write_text(json.dumps({"supplies": {}, "family_size": 4,
                                "location": "",
                                "last_updated": None}))
    monkeypatch.setattr(ep, "_INVENTORY_FILE", str(path))
    return path


def test_blank_inventory_does_not_shadow_the_configured_city(cfg, blank_inventory):
    import puffpai.emergency_prep as ep
    cfg("Denver, CO", None, None)
    assert ep.EmergencyPrep().effective_location() == "Denver, CO"
    # The owner's explicit choice still wins over config.
    prep = ep.EmergencyPrep()
    prep.inventory["location"] = "Boise, ID"
    assert prep.effective_location() == "Boise, ID"
    # And a box configured for a town keeps that town.
    cfg("Boise, ID", None, None)
    assert ep.EmergencyPrep().effective_location() == "Boise, ID"


def test_fresh_inventory_takes_the_configured_city(cfg, tmp_path, monkeypatch):
    import puffpai.emergency_prep as ep
    monkeypatch.setattr(ep, "_INVENTORY_FILE",
                        str(tmp_path / "missing" / "emergency_inventory.json"))
    cfg("Denver, CO", None, None)
    assert ep.EmergencyPrep().inventory["location"] == "Denver, CO"


def test_prep_alerts_tool_uses_household_coordinates_not_the_geocode_door(
        cfg, blank_inventory, monkeypatch):
    """The LLM tool path checks the same point as the card and the reply,
    even when the inventory names no place of its own."""
    from puffpai.automation_client import AutomationClient
    from puffpai.assistant_tools import AssistantTools
    cfg("Denver, CO", 12.5, -34.25)
    monkeypatch.setattr(AutomationClient, "call",
                        lambda self, *a, **k: (False, "down"))
    seen = []

    def _egress_get(door_id, url, **kw):
        seen.append((door_id, kw.get("params", {})))
        assert door_id == "weather", door_id
        return _Resp({"features": []})

    monkeypatch.setattr(ed, "egress_get", _egress_get)
    reply = AssistantTools().tool_dict["prep_weather_alerts"].invoke("false")
    assert seen == [("weather", {"point": "12.5,-34.25"})]
    assert "Denver, CO" in reply


def test_forecast_tool_reports_unset_instead_of_a_cache_failure(
        cfg, geocode_calls):
    from puffpai.assistant_tools import AssistantTools
    cfg("Denver, CO", None, None)
    geocode_calls.mode["mode"] = "shut"
    reply = AssistantTools().tool_dict["auto_weather_forecast"].invoke("")
    assert geo.UNSET_MESSAGE in reply
    assert "Denver, CO" in reply
    assert "unavailable right now" not in reply
