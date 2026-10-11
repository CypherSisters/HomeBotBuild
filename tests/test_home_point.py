"""The owner's home point: set once on the box, never shipped, never logged.

Pinned here:

* the overlay file: write / read / clear, atomic (tmp + os.replace), 0600,
  under ``$PUFFPAI_DATA_DIR/config/`` and git-ignored when it falls back into
  the repo;
* validation at the trust boundary: range, 0/0, NaN/inf, strings, junk, and
  the "lat, lon" string a phone maps app copies;
* ``/api/home-point``: GET never returns the numbers, POST/DELETE return the
  GET shape, every method needs the API key;
* the numbers never reach a log or an error message, on success or failure;
* a fresh config with no overlay is unset EVERYWHERE: no point, no town, the
  weather card says so.

Every coordinate here is synthetic (open ocean). No real point is tracked.
"""

import json
import logging
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.config as config_mod  # noqa: E402
from puffpai import dashboard_widgets as dw  # noqa: E402
from puffpai import home_point as hp  # noqa: E402
from puffpai import household_geo as geo  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

#: Synthetic point. Distinctive digits so a leak into any log is findable.
LAT, LON = 12.3456, -45.6789
LAT_S, LON_S = "12.3456", "45.6789"


def _leaked(text: str) -> bool:
    return LAT_S in text or LON_S in text


@pytest.fixture(autouse=True)
def overlay(tmp_path):
    """Every test gets its own overlay path; nothing touches a real one."""
    path = tmp_path / "data" / "config" / "home_point.json"
    hp.set_path_override(str(path))
    geo.reset_cache()
    dw._env_cache.clear()
    yield path
    hp.set_path_override(None)
    geo.reset_cache()
    dw._env_cache.clear()


@pytest.fixture
def all_logs(caplog):
    """Capture every logger at DEBUG, including the 'puffpai' tree even when
    a logging setup has turned its propagation off."""
    caplog.set_level(logging.DEBUG)
    pl = logging.getLogger("puffpai")
    pl.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        pl.removeHandler(caplog.handler)


def _repo_with_config(tmp_path, **raw):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True, exist_ok=True)
    # The directory _build_config reads for this root (a test-isolated copy
    # when $PUFFPAI_CONFIG_DIR is set, else <root>/config).
    d = Path(config_mod._config_dir_for(str(root)))
    d.mkdir(parents=True, exist_ok=True)
    (d / "puffpai_config.json").write_text(json.dumps(raw))
    return str(root)


# ── the overlay file ────────────────────────────────────────────────────────

def test_write_read_clear_round_trip(overlay):
    assert hp.read() is None
    set_at = hp.write(LAT, LON)
    got = hp.read()
    assert (got["latitude"], got["longitude"]) == (LAT, LON)
    assert got["set_at"] == set_at
    assert hp.clear() is True
    assert hp.read() is None
    assert not overlay.exists()
    assert hp.clear() is False


def test_write_is_atomic_and_owner_only(overlay, monkeypatch):
    replaced, chmods = [], []
    real_replace, real_chmod = os.replace, os.chmod

    def _replace(src, dst):
        replaced.append((src, dst))
        assert Path(src).parent == Path(dst).parent  # same filesystem
        return real_replace(src, dst)

    def _chmod(p, mode):
        chmods.append(mode)
        return real_chmod(p, mode)

    monkeypatch.setattr(hp.os, "replace", _replace)
    monkeypatch.setattr(hp.os, "chmod", _chmod)
    hp.write(LAT, LON)
    assert len(replaced) == 1 and replaced[0][1] == str(overlay)
    assert chmods == [0o600]
    assert [p.name for p in overlay.parent.iterdir()] == ["home_point.json"]
    if os.name == "posix":
        assert (overlay.stat().st_mode & 0o777) == 0o600


def test_failed_write_leaves_the_old_point_and_no_temp_file(overlay, monkeypatch):
    hp.write(LAT, LON)
    before = overlay.read_text()

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(hp.os, "replace", _boom)
    with pytest.raises(OSError):
        hp.write(10.0, 20.0)
    assert overlay.read_text() == before
    assert [p.name for p in overlay.parent.iterdir()] == ["home_point.json"]


def test_unreadable_overlay_is_unset_and_logged_without_content(overlay, all_logs):
    overlay.parent.mkdir(parents=True)
    overlay.write_text('{"kind": "home_point", "latitude": %s, "longitude": '
                       % LAT_S)  # truncated JSON
    assert hp.read() is None
    overlay.write_text(json.dumps({"kind": "home_point", "latitude": 95.0,
                                   "longitude": LON}))
    assert hp.read() is None
    assert not _leaked(all_logs.text)


def test_overlay_lives_under_the_data_dir_config(monkeypatch, tmp_path):
    hp.set_path_override(None)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    assert Path(hp.overlay_path()) == tmp_path / "state" / "config" / "home_point.json"


@pytest.mark.skipif(not (REPO / ".git").exists(),
                    reason="repo checkout only (no .git here; installed tree)")
def test_dev_fallback_path_is_git_ignored(monkeypatch):
    hp.set_path_override(None)
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    path = Path(hp.overlay_path())
    if REPO not in path.parents:
        # This build keeps state in the per-user data dir, outside the tree,
        # so there is nothing for git to ignore.
        from puffpai import platform_paths
        assert Path(platform_paths.user_data_dir()) in path.parents
        return
    rel = path.relative_to(REPO).as_posix()
    try:
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", rel],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git not available")
    assert r.returncode == 0, rel
    # Wherever it lands in the tree, the basename is ignored too.
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q",
                        "config/home_point.json"], capture_output=True, timeout=20)
    assert r.returncode == 0


# ── validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("12.3456, -45.6789", (LAT, LON)),
    ("12.3456,-45.6789", (LAT, LON)),
    ("  12.3456 ,  -45.6789  ", (LAT, LON)),
    ("12.3456°, -45.6789°", (LAT, LON)),
    ("+12.5, 20", (12.5, 20.0)),
    ("-90, 180", (-90.0, 180.0)),
])
def test_phone_copy_strings_are_accepted(text, expected):
    assert hp.parse_body({"point": text}) == pytest.approx(expected)


@pytest.mark.parametrize("body", [
    {"point": "91, 20"},                     # latitude out of range
    {"point": "10, -181"},                   # longitude out of range
    {"point": "0, 0"},                       # Null Island
    {"point": "0.0,0.000"},
    {"point": "home"},                       # junk
    {"point": "12.3456 -45.6789"},           # no comma
    {"point": "12.3456, -45.6789, 3"},       # three numbers
    {"point": "N 12.3456, W 45.6789"},       # letters
    {"point": "1e1, 2e1"},                   # exponent form
    {"point": "nan, nan"},
    {"point": "12.3456, " + "9" * 80},       # over the length cap
    {"point": 12.3456},                      # not a string
    {"latitude": "12.3456", "longitude": "-45.6789"},  # strings, not numbers
    {"latitude": float("nan"), "longitude": 20.0},
    {"latitude": 10.0, "longitude": float("inf")},
    {"latitude": True, "longitude": 20.0},
    {"latitude": 10 ** 400, "longitude": 20.0},  # too big for a float
    {"latitude": 0, "longitude": 0},
    {"latitude": -90.5, "longitude": 20.0},
    {"latitude": 10.0},                      # half a point
    {"latitude": 10.0, "longitude": 20.0, "point": "10, 20"},  # both shapes
    {"latitude": 10.0, "longitude": 20.0, "note": "x"},        # unknown key
    {},
    [10.0, 20.0],
    None,
    "10, 20",
])
def test_bad_bodies_are_refused_with_a_fixed_message(body):
    with pytest.raises(hp.HomePointError) as ei:
        hp.parse_body(body)
    assert str(ei.value) in {hp.ERR_SHAPE, hp.ERR_NUMBER, hp.ERR_POINT,
                             hp.ERR_RANGE, hp.ERR_ZERO}


def test_write_revalidates_even_without_parse_body(overlay):
    for lat, lon in ((0, 0), (91, 0), (math.nan, 1.0), ("1", 2)):
        with pytest.raises(hp.HomePointError):
            hp.write(lat, lon)
    assert not overlay.exists()


# ── config layering ─────────────────────────────────────────────────────────

def test_owner_overlay_is_layered_over_the_config_file(tmp_path):
    root = _repo_with_config(tmp_path, location="Testville", latitude=1.5,
                             longitude=2.5)
    assert (config_mod._build_config(root).latitude,
            config_mod._build_config(root).longitude) == (1.5, 2.5)
    assert hp.status(config_mod._build_config(root))["source"] == "config"
    hp.write(LAT, LON)
    cfg = config_mod._build_config(root)
    assert (cfg.latitude, cfg.longitude) == (LAT, LON)
    st = hp.status(cfg)
    assert st["set"] is True and st["source"] == "owner" and st["set_at"]


def test_fresh_config_without_overlay_is_unset_everywhere(tmp_path, monkeypatch):
    """No town, no point, no city default anywhere a user would see one."""
    import puffpai.api_server as api
    root = _repo_with_config(tmp_path)  # an empty config file
    cfg = config_mod._build_config(root)
    assert cfg.location == ""
    assert cfg.latitude is None and cfg.longitude is None
    assert config_mod.PuffPAIConfig().location == ""  # the dataclass default
    monkeypatch.setattr(config_mod, "get_config", lambda **k: cfg)

    assert geo.household_coordinates() == (None, None)
    assert dw.fetch_environment() == {"unset": geo.UNSET_MESSAGE}
    assert hp.status() == {"set": False, "set_at": None, "source": None}
    assert api._home_city() == ""

    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: (_ for _ in ()).throw(RuntimeError("no HA")))
    with api.app.test_request_context("/dashboard/weather"):
        payload = api.dashboard_weather().get_json()
    assert payload["location"] == ""
    assert payload["unset"] == geo.UNSET_MESSAGE

    reply = api._air_quality_answer("how is the air outside")
    assert geo.UNSET_MESSAGE in reply

    with api.app.test_request_context("/automation/weather"):
        resp, code = api.automation_weather()
    assert code == 400 and resp.get_json()["success"] is False


# ── the API ─────────────────────────────────────────────────────────────────

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """The real app, with a real (tmp-built) config singleton so the route's
    refresh is exercised end to end."""
    import puffpai.api_server as api
    root = _repo_with_config(tmp_path, location="Testville")
    monkeypatch.setattr(config_mod, "_instance", config_mod._build_config(root))
    api._rate_limit_store.clear()
    api.app.config["TESTING"] = True
    with api.app.test_client() as c:
        yield c, {"X-API-Key": api._API_KEY}, api


def test_routes_are_behind_the_api_key(api_client):
    c, key, api = api_client
    from puffpai.security import endpoint_gate
    for ep in ("home_point_get_route", "home_point_set_route",
               "home_point_clear_route"):
        assert ep not in api._PUBLIC_ENDPOINTS
        assert ep not in endpoint_gate.LOCAL_ONLY_ENDPOINTS
    body = {"point": "12.3456, -45.6789"}
    assert c.get("/api/home-point").status_code == 401
    assert c.post("/api/home-point", json=body).status_code == 401
    assert c.delete("/api/home-point").status_code == 401
    bad = {"X-API-Key": "not-the-key"}
    assert c.get("/api/home-point", headers=bad).status_code == 401
    assert c.post("/api/home-point", json=body, headers=bad).status_code == 401
    assert hp.read() is None  # the refused POST wrote nothing


def test_post_get_delete_never_return_the_numbers(api_client):
    c, key, api = api_client
    r = c.get("/api/home-point", headers=key)
    assert r.status_code == 200
    assert r.get_json() == {"set": False, "set_at": None, "source": None}

    r = c.post("/api/home-point", json={"point": "12.3456, -45.6789"},
               headers=key)
    assert r.status_code == 200, r.get_data(as_text=True)
    st = r.get_json()
    assert set(st) == {"set", "set_at", "source"}
    assert st["set"] is True and st["source"] == "owner" and st["set_at"]
    assert not _leaked(r.get_data(as_text=True))

    r = c.get("/api/home-point", headers=key)
    assert r.get_json() == st
    assert not _leaked(r.get_data(as_text=True))

    r = c.post("/api/home-point", json={"latitude": 10.5, "longitude": 20.5},
               headers=key)
    assert r.status_code == 200 and r.get_json()["set"] is True

    r = c.delete("/api/home-point", headers=key)
    assert r.status_code == 200
    assert r.get_json() == {"set": False, "set_at": None, "source": None}
    assert hp.read() is None


def test_saved_point_takes_effect_without_a_restart(api_client):
    c, key, api = api_client
    c.post("/api/home-point", json={"latitude": LAT, "longitude": LON},
           headers=key)
    assert config_mod.get_config().latitude == LAT
    assert geo.household_coordinates() == (LAT, LON)
    c.delete("/api/home-point", headers=key)
    assert config_mod.get_config().latitude is None


def test_values_never_reach_logs_on_success_or_error(api_client, all_logs, capsys):
    c, key, api = api_client
    r = c.post("/api/home-point", json={"point": "12.3456, -45.6789"},
               headers=key)
    assert r.status_code == 200
    c.get("/api/home-point", headers=key)
    # Validation errors: out of range, junk, strings. None may echo input.
    for body in ({"point": "112.3456, -45.6789"},
                 {"point": "12.3456 -45.6789 junk"},
                 {"latitude": "12.3456", "longitude": "-45.6789"},
                 {"latitude": 12.3456, "longitude": -45.6789, "x": 1}):
        r = c.post("/api/home-point", json=body, headers=key)
        assert r.status_code == 400
        assert not _leaked(r.get_data(as_text=True)), body
    r = c.post("/api/home-point", data="{not json 12.3456",
               content_type="application/json", headers=key)
    assert r.status_code == 400 and not _leaked(r.get_data(as_text=True))
    c.delete("/api/home-point", headers=key)
    out = capsys.readouterr()
    assert not _leaked(all_logs.text)
    assert not _leaked(out.out + out.err)


def test_oversized_body_is_refused_before_parsing(api_client):
    c, key, api = api_client
    big = json.dumps({"point": "1, 2", "pad": "x" * 4000})
    r = c.post("/api/home-point", data=big, content_type="application/json",
               headers=key)
    assert r.status_code == 413
    assert hp.read() is None


# ── the point never leaves through a tool or a route ───────────────────────

@pytest.fixture
def testville_with_point(tmp_path, monkeypatch):
    """Town "Testville", the owner's point saved, no stale prep inventory."""
    import puffpai.emergency_prep as ep
    hp.write(LAT, LON)
    cfg = config_mod._build_config(_repo_with_config(tmp_path, location="Testville"))
    assert (cfg.latitude, cfg.longitude) == (LAT, LON)
    monkeypatch.setattr(config_mod, "get_config", lambda **k: cfg)
    monkeypatch.setattr(ep, "_INVENTORY_FILE",
                        str(tmp_path / "missing" / "emergency_inventory.json"))
    return cfg


def test_prep_alerts_tool_never_echoes_the_point(testville_with_point,
                                                 monkeypatch, all_logs, capsys):
    """Automation down + NWS unreachable must not return "Coordinates:
    <lat>, <lon>" to the model: the agent loop prints every tool result to
    stdout."""
    import puffpai.security.egress_doors as ed
    from puffpai.automation_client import AutomationClient
    from puffpai.assistant_tools import AssistantTools
    sent = []

    def _call(self, name, payload, **k):
        sent.append(payload)
        return False, "down"

    def _egress_get(door_id, url, **kw):
        sent.append(kw.get("params"))
        raise ConnectionError("no internet")

    monkeypatch.setattr(AutomationClient, "call", _call)
    monkeypatch.setattr(ed, "egress_get", _egress_get)
    result = AssistantTools().tool_dict["prep_weather_alerts"].invoke("false")
    # The point really was used, for the alert sources only...
    assert sent[0]["lat"] == LAT and sent[0]["lon"] == LON
    # ...and is in neither the tool result nor any captured output.
    assert not _leaked(result), result
    assert "your saved home point" in result
    assert "Testville" in result
    out = capsys.readouterr()
    assert not _leaked(all_logs.text)
    assert not _leaked(out.out + out.err)


def test_automation_weather_sends_the_point_for_the_home_town_only(
        testville_with_point, monkeypatch):
    import puffpai.api_server as api
    from puffpai.automation_client import AutomationClient
    sent = []

    def _call(self, name, payload, **k):
        sent.append(payload)
        # Even if the flow ever echoed the point, the route must not return it.
        return True, {"alert_count": 0, "location": payload["location"],
                      "lat": payload.get("lat"), "lon": payload.get("lon")}

    monkeypatch.setattr(AutomationClient, "call", _call)
    with api.app.test_request_context("/automation/weather"):
        body = api.automation_weather().get_json()
    assert sent[-1] == {"location": "Testville", "lat": LAT, "lon": LON}
    assert not _leaked(json.dumps(body))
    assert body["data"]["alert_count"] == 0

    # Another place: today's location-only behaviour, no point attached.
    with api.app.test_request_context("/automation/weather?location=Elsewhere"):
        api.automation_weather()
    assert sent[-1] == {"location": "Elsewhere"}


def test_automation_weather_without_a_point_sends_only_the_town(tmp_path, monkeypatch):
    import puffpai.api_server as api
    from puffpai.automation_client import AutomationClient
    cfg = config_mod._build_config(_repo_with_config(tmp_path, location="Testville"))
    monkeypatch.setattr(config_mod, "get_config", lambda **k: cfg)
    sent = []
    monkeypatch.setattr(AutomationClient, "call",
                        lambda self, n, p, **k: (sent.append(p), (True, {}))[1])
    with api.app.test_request_context("/automation/weather"):
        api.automation_weather()
    assert sent == [{"location": "Testville"}]


def test_user_facing_replies_carry_no_em_dash(tmp_path, monkeypatch):
    import puffpai.api_server as api
    monkeypatch.setattr(dw, "fetch_environment",
                        lambda *a, **k: {"us_aqi": 42, "aqi_label": "Good"})
    monkeypatch.setattr(api, "_home_city", lambda: "Testville")
    assert "\u2014" not in api._air_quality_answer("how is the air")
    monkeypatch.setattr(dw, "fetch_environment", lambda *a, **k: {"blocked": "x"})
    assert "\u2014" not in api._air_quality_answer("how is the air")
    monkeypatch.setattr(dw, "fetch_environment", lambda *a, **k: {})
    assert "\u2014" not in api._air_quality_answer("how is the air")
