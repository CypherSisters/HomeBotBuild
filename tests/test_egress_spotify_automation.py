"""Spotify and the automation engine go out only through their Bridge switches.

Two outbound paths used to skip the registry:

* **Spotify control** (``puffpai.spotify_control``) reached
  ``accounts.spotify.com`` and ``api.spotify.com`` whenever the Spotify app was
  set up. Every call now goes through ``egress_request("spotify", ...)``, so
  with the switch off nothing is looked up or dialled and the caller gets the
  sentence that names the switch. The ``spotify`` entry is enforced by the
  registry and writable from Settings.
* **The automation engine** (``puffpai.automation_client``) starts Node-RED
  flows that call ``api.weather.gov`` and an RSS feed. ``WEBHOOK_SWITCHES``
  maps each such webhook to its switches, and the client, the
  ``POST /automation/trigger`` route and the assistant's automation helper
  refuse to start one while a switch is off. The flows' own check is covered
  in ``test_egress_nodered_flows.py``.
"""

import importlib
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.modpatch import patched_modules

ROOT = Path(__file__).resolve().parents[1]
_src_dir = str(ROOT / "src" / "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

SPOTIFY_HINT = ("Spotify is switched off. To use it, open Settings and switch "
                "on 'Spotify' in the Bridge section.")


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A throwaway config, overlay and ledger with every switch off."""
    from puffpai.security import egress_doors, egress_ledger

    cfg = tmp_path / "puffpai_config.json"
    cfg.write_text(json.dumps({"identity": {"name": "TestBot"}}), encoding="utf-8")
    overlay = tmp_path / "state" / "egress_doors.json"
    ledger = tmp_path / "egress.jsonl"
    monkeypatch.setattr(egress_doors, "_config_path", lambda: str(cfg))
    monkeypatch.setattr(egress_doors, "_ledger_path",
                        lambda: str(tmp_path / "egress_doors_snapshot.json"))
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    egress_doors.set_overlay_override(str(overlay))
    egress_ledger.set_path_override(str(ledger))
    egress_doors.reset_cache()
    try:
        yield SimpleNamespace(overlay=overlay, ledger=ledger)
    finally:
        egress_ledger.set_path_override(None)
        egress_doors.set_overlay_override(None)
        egress_doors.reset_cache()


def _refusals(ledger: Path, door_id: str) -> int:
    if not ledger.exists():
        return 0
    rows = [json.loads(line) for line in
            ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    return sum(1 for r in rows
               if r.get("door") == door_id and r.get("result") == "refused")


class _Dialled(AssertionError):
    """Raised if anything tries to look a name up or open a connection."""


@pytest.fixture()
def no_wire(monkeypatch):
    """Every way to reach the network raises, so 'refused' means 'never tried'."""
    import requests
    from puffpai.security import egress_doors

    def boom(*_a, **_kw):
        raise _Dialled("the network was touched with the switch off")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(egress_doors, "_pinned_session", boom)
    monkeypatch.setattr(egress_doors, "_RESOLVER_OVERRIDE", boom)
    monkeypatch.setattr(requests, "request", boom)
    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    return boom


@pytest.fixture()
def spotify(tmp_path):
    """A controller with an app and a refresh token stored, and a fresh access
    token cached, so a playback call would go straight to the Web API."""
    import time
    from puffpai import spotify_control as sc

    ctl = sc.SpotifyControl(sc.SpotifyStore(str(tmp_path / "spotify_oauth.json")))
    ctl._store.set_client("dummy_client_id_0123", "fake-confid-NEVER-SURFACE")
    ctl._store.set_refresh_token("fake-refresh-NEVER-SURFACE")
    ctl._access_token = "fake-access"
    ctl._access_expiry = time.time() + 3600
    return ctl


# ── the Spotify entry ────────────────────────────────────────────────────────

class TestSpotifyEntry:
    def test_it_is_enforced_by_the_registry(self):
        from puffpai.security import egress_doors
        d = egress_doors.door("spotify")
        assert d.enforcement == "registry"
        assert d.hosts == ("accounts.spotify.com", "api.spotify.com")
        assert d.default is False
        assert d.settings_hint == SPOTIFY_HINT

    def test_it_says_the_librespot_containers_are_outside_it(self):
        from puffpai.security import egress_doors
        notes = egress_doors.door("spotify").notes
        assert "librespot" in notes
        assert "docker-compose.spotify.yml" in notes
        assert "docker-compose.spotify-voice.yml" in notes
        assert "cannot stop them" in notes
        assert "stopping the container is the switch" in notes

    def test_status_reports_it_enforced_and_writable(self, sandbox):
        from puffpai.security import egress_doors
        row = [r for r in egress_doors.door_status() if r["id"] == "spotify"][0]
        assert row["enforced"] is True
        assert row["writable"] is True
        assert row["open"] is False
        assert row["enforcement_note"] == egress_doors.ENFORCEMENT_NOTES["registry"]

    def test_settings_can_open_and_close_it(self, sandbox):
        from puffpai.security import egress_admin, egress_doors
        assert egress_admin.set_door_open("spotify", True)["open"] is True
        assert egress_doors.egress_allowed("spotify")[0] is True
        assert egress_admin.set_door_open("spotify", False)["open"] is False
        assert egress_doors.egress_allowed("spotify")[0] is False


# ── Spotify with the switch off ──────────────────────────────────────────────

class TestSpotifySwitchedOff:
    @pytest.mark.parametrize("tool", ["tool_spotify_pause",
                                      "tool_spotify_resume",
                                      "tool_spotify_next"])
    def test_a_playback_tool_refuses_without_touching_the_network(
            self, sandbox, no_wire, spotify, tool):
        from puffpai import spotify_control as sc
        before = _refusals(sandbox.ledger, "spotify")
        assert getattr(sc, tool)(control=spotify) == SPOTIFY_HINT
        assert _refusals(sandbox.ledger, "spotify") == before + 1

    def test_play_refuses_without_touching_the_network(self, sandbox, no_wire,
                                                       spotify):
        from puffpai import spotify_control as sc
        assert sc.tool_spotify_play("jazz", control=spotify) == SPOTIFY_HINT

    def test_the_status_tool_names_the_switch(self, sandbox, no_wire, spotify):
        from puffpai import spotify_control as sc
        assert sc.tool_spotify_status(control=spotify) == SPOTIFY_HINT

    def test_status_says_why_and_records_no_attempt(self, sandbox, no_wire,
                                                    spotify):
        """The dashboard polls status; a poll is not an attempt to go out."""
        before = _refusals(sandbox.ledger, "spotify")
        st = spotify.status()
        assert st["configured"] is True
        assert st["connected"] is False
        assert st["bridge_open"] is False
        assert st["bridge_message"] == SPOTIFY_HINT
        assert _refusals(sandbox.ledger, "spotify") == before

    def test_a_web_api_call_is_refused_before_the_wire(self, sandbox, no_wire,
                                                       spotify):
        from puffpai import spotify_control as sc
        with pytest.raises(sc.SpotifyRefused) as info:
            spotify._api("GET", "/me/player/devices")
        assert str(info.value) == SPOTIFY_HINT

    def test_a_token_refresh_is_refused_before_the_wire(self, sandbox, no_wire,
                                                        spotify):
        from puffpai import spotify_control as sc
        spotify._access_token = None
        with pytest.raises(sc.SpotifyRefused):
            spotify._valid_access_token()

    def test_connecting_the_account_keeps_the_pasted_state(self, sandbox,
                                                           no_wire, spotify):
        """The switch is asked before the single-use state is spent, so the
        same pasted URL works once the switch is on."""
        from puffpai import spotify_control as sc
        state = spotify._new_state()
        with pytest.raises(sc.SpotifyRefused):
            spotify.exchange_code("a-code", state)
        assert state in spotify._pending_states

    def test_the_exchange_route_answers_the_sentence(self, sandbox, no_wire,
                                                     spotify, monkeypatch):
        from puffpai import spotify_control as sc
        monkeypatch.setattr(sc, "get_control", lambda: spotify)
        mod, key = _api_module()
        with mod.app.test_client() as c:
            resp = c.post("/api/spotify/exchange", headers={"X-API-Key": key},
                          json={"code": "a-code", "state": spotify._new_state()})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == SPOTIFY_HINT


# ── Spotify with the switch on ───────────────────────────────────────────────

class _Session:
    def __init__(self, calls):
        self.calls = calls

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return SimpleNamespace(status_code=204, content=b"", headers={})

    def close(self):
        return None


class TestSpotifySwitchedOn:
    def test_calls_go_through_the_registry_wrapper(self, sandbox, spotify,
                                                   monkeypatch):
        from puffpai.security import egress_doors
        calls = []
        monkeypatch.setenv("PUFFPAI_EGRESS_SPOTIFY", "1")
        monkeypatch.setattr(egress_doors, "_pinned_session",
                            lambda _door: _Session(calls))
        spotify.pause(device_id="PP1")
        assert calls == [("PUT", "https://api.spotify.com/v1/me/player/pause")]

    def test_a_host_the_switch_does_not_name_is_refused(self, sandbox, spotify,
                                                        monkeypatch, no_wire):
        from puffpai import spotify_control as sc
        monkeypatch.setenv("PUFFPAI_EGRESS_SPOTIFY", "1")
        with pytest.raises(sc.SpotifyRefused):
            spotify._api("GET", "https://elsewhere.example.org/v1/me")


# ── the automation engine ────────────────────────────────────────────────────

def _engine_cfg():
    names = ("block_device", "network_scan", "weather_alerts", "puffpai_voice",
             "daily_news", "calendar_sync", "meal_planner", "grocery_list",
             "daily_briefing", "daily_weather", "media_now_playing",
             "health_check", "good_morning", "leaving_home", "security_check")
    hooks = SimpleNamespace(**{n: "/webhook/" + n.replace("_", "-")
                               for n in names})
    return SimpleNamespace(nodered=SimpleNamespace(
        url="http://localhost:1880", timeout=5, retries=1, health_path="/",
        webhooks=hooks))


@pytest.fixture()
def engine(monkeypatch):
    """An AutomationClient whose transport records calls instead of dialling."""
    from puffpai import automation_client as ac
    monkeypatch.setattr(ac, "get_config", _engine_cfg)
    post = MagicMock()
    post.return_value = SimpleNamespace(status_code=200,
                                        json=lambda: {"ok": True}, text="")
    monkeypatch.setattr(ac.requests, "post", post)
    return ac, post


def _hint(door_id):
    from puffpai.security import egress_doors
    return egress_doors.door(door_id).settings_hint


class TestAutomationSwitches:
    def test_the_table_is_exact(self):
        from puffpai.automation_client import KNOWN_WEBHOOKS, WEBHOOK_SWITCHES
        assert WEBHOOK_SWITCHES == {
            "daily_weather": ("weather",),
            "weather_alerts": ("weather",),
            "daily_news": ("news_rss",),
            "daily_briefing": ("weather", "news_rss"),
        }
        assert set(WEBHOOK_SWITCHES) <= KNOWN_WEBHOOKS

    @pytest.mark.parametrize("webhook, door_id", [
        ("daily_weather", "weather"),
        ("weather_alerts", "weather"),
        ("daily_news", "news_rss"),
    ])
    def test_a_flow_is_not_started_with_its_switch_off(self, sandbox, engine,
                                                       webhook, door_id):
        ac, post = engine
        before = _refusals(sandbox.ledger, door_id)
        ok, data = ac.AutomationClient().call(webhook, {})
        assert ok is False
        assert data == _hint(door_id)
        post.assert_not_called()
        assert _refusals(sandbox.ledger, door_id) == before + 1

    def test_the_briefing_needs_both_switches(self, sandbox, engine,
                                              monkeypatch):
        ac, post = engine
        monkeypatch.setenv("PUFFPAI_EGRESS_WEATHER", "1")
        ok, data = ac.AutomationClient().call("daily_briefing", {})
        assert ok is False
        assert data == _hint("news_rss")
        post.assert_not_called()

    def test_a_flow_starts_once_its_switches_are_on(self, sandbox, engine,
                                                    monkeypatch):
        ac, post = engine
        monkeypatch.setenv("PUFFPAI_EGRESS_WEATHER", "1")
        monkeypatch.setenv("PUFFPAI_EGRESS_NEWS_RSS", "1")
        ok, _data = ac.AutomationClient().call("daily_briefing", {})
        assert ok is True
        post.assert_called_once()

    @pytest.mark.parametrize("webhook", ["block_device", "health_check",
                                         "calendar_sync", "good_morning"])
    def test_a_house_only_flow_is_unaffected(self, sandbox, engine, webhook):
        ac, post = engine
        ok, _data = ac.AutomationClient().call(webhook, {})
        assert ok is True
        post.assert_called_once()

    def test_a_broken_registry_counts_as_off(self, sandbox, engine,
                                             monkeypatch):
        from puffpai.security import egress_doors
        ac, post = engine

        def boom(_door_id):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(egress_doors, "require_open", boom)
        ok, data = ac.AutomationClient().call("daily_news", {})
        assert ok is False
        assert "'news_rss' switch could not be read" in data
        post.assert_not_called()

    def test_the_assistant_helper_asks_before_it_calls(self):
        """``_automation_call`` in assistant_tools answers the sentence itself
        rather than wrapping it in "<name> unavailable: ...". It is a closure
        inside ``create_tools``, so the order is checked in the source."""
        text = (ROOT / "src" / "puffpai" / "assistant_tools.py").read_text(
            encoding="utf-8")
        start = text.index("def _automation_call(")
        body = text[start:start + 1200]
        assert body.index("switch_refusal(webhook_name)") < body.index(
            "client.call(webhook_name, payload)")


def _api_module():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = sys.modules["api_server"]
        else:
            mod = importlib.import_module("api_server")
    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    return mod, mod._API_KEY


class TestTriggerRoute:
    def test_it_refuses_with_403_and_names_the_switch(self, sandbox):
        mod, key = _api_module()
        with patch("puffpai.automation_client.AutomationClient.call") as call:
            with mod.app.test_client() as c:
                resp = c.post("/automation/trigger/daily_weather",
                              headers={"X-API-Key": key}, json={})
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["success"] is False
        assert body["reason"] == "bridge_closed"
        assert body["switches"] == ["weather"]
        assert body["error"] == _hint("weather")
        call.assert_not_called()

    def test_an_open_switch_reaches_the_engine(self, sandbox, monkeypatch):
        monkeypatch.setenv("PUFFPAI_EGRESS_WEATHER", "1")
        mod, key = _api_module()
        with patch("puffpai.automation_client.AutomationClient.call",
                   return_value=(True, {"ok": 1})) as call:
            with mod.app.test_client() as c:
                resp = c.post("/automation/trigger/daily_weather",
                              headers={"X-API-Key": key}, json={})
        assert resp.status_code == 200
        call.assert_called_once()

    def test_an_unknown_webhook_is_still_400(self, sandbox):
        mod, key = _api_module()
        with mod.app.test_client() as c:
            resp = c.post("/automation/trigger/not_a_flow",
                          headers={"X-API-Key": key}, json={})
        assert resp.status_code == 400
