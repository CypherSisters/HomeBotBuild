"""a stop from ANY surface silences the appliance radio.

The owner's report: a station she started by voice kept playing
and "I couldn't pause it from the dashboard or in the media page."

Cause: play went through `radio_player.play` - the server-side `puffpai-radio`
mpv container - but every browser-side stop only queued a command for the KIOSK
player (`app_data.mediahub_radio_request`), which is a different player on a
different machine. Nothing in that path touched the container.

Fix: `mediahub_radio_request` is the ONE function every surface's stop passes
through (voice `media_hub.tool_radio_stop`, the LLM tool, and
`POST /apps/mediahub/action` from the dashboard/media page), so the appliance
kill lives there. `pause` is accepted as a synonym: a live stream has no pause,
and "pause" from a browser means "stop making sound".

Real `AppData` on a temp dir; `radio_player` is mocked - no docker is ever run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

_STATION = {"stationuuid": "u-1", "name": "KEXP",
            "url_resolved": "https://example.invalid/kexp.mp3"}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


@pytest.fixture()
def stops(monkeypatch):
    """Counts calls to the server-side player's stop, and never runs docker."""
    from puffpai import radio_player
    calls = []
    monkeypatch.setattr(radio_player, "stop", lambda: calls.append(1) or True)
    monkeypatch.setattr(radio_player, "is_playing", lambda: False)
    return calls


# -- the choke point ---------------------------------------------------------

def test_stop_kills_the_server_side_player_exactly_once(app, stops):
    app.mediahub_radio_request("stop")
    assert len(stops) == 1


def test_pause_is_accepted_and_also_kills_the_player(app, stops):
    command = app.mediahub_radio_request("pause")
    assert len(stops) == 1
    # The kiosk bridge understands play/stop only, so pause queues a stop.
    assert command["action"] == "stop"


def test_play_never_touches_the_server_side_stop(app, stops):
    app.mediahub_radio_request("play", station=_STATION)
    assert stops == []


def test_a_stop_still_queues_the_browser_command(app, stops):
    command = app.mediahub_radio_request("stop")
    assert command["station"] is None
    assert app.get_data("mediahub")["radio_command"]["id"] == command["id"]


def test_the_appliance_row_reads_idle_after_a_stop(app, stops):
    app.mediahub_report_radio("playing", station=_STATION, source="appliance")
    app.mediahub_radio_request("stop")

    radio = app.get_data("mediahub")["radio"]
    assert radio["status"] == "idle"
    assert radio["station"] is None
    assert radio["source"] == "appliance"


def test_repeated_stops_are_idempotent(app, stops):
    for _ in range(3):
        app.mediahub_radio_request("stop")
    assert len(stops) == 3          # one docker rm -f each, all harmless
    assert app.get_data("mediahub")["radio"]["status"] == "idle"


def test_a_still_unknown_action_is_rejected(app, stops):
    with pytest.raises(ValueError):
        app.mediahub_radio_request("skip")
    assert stops == []


# -- no player / no docker: a no-op, never an error --------------------------

def test_a_player_that_cannot_run_is_a_no_op(app, monkeypatch):
    """Docker missing, image absent, container already gone - radio_player.stop
    swallows it and returns False. The stop must still succeed."""
    from puffpai import radio_player
    monkeypatch.setattr(radio_player, "stop", lambda: False)

    command = app.mediahub_radio_request("stop")
    assert command["action"] == "stop"
    assert app.get_data("mediahub")["radio"]["status"] == "idle"


def test_a_raising_player_does_not_break_the_stop(app, monkeypatch):
    from puffpai import radio_player

    def boom():
        raise RuntimeError("docker daemon is not running")

    monkeypatch.setattr(radio_player, "stop", boom)

    command = app.mediahub_radio_request("stop")
    assert command["action"] == "stop"
    assert app.get_data("mediahub")["radio"]["status"] == "idle"


# -- the voice path still wins ----------------------------------------------

def test_the_voice_stop_tool_still_stops_and_reports_idle(app, stops):
    from puffpai import media_hub as mh

    app.mediahub_report_radio("playing", station=_STATION, source="appliance")
    out = mh.tool_radio_stop(app)

    assert "Stopping internet radio" in out
    assert app.get_data("mediahub")["radio"]["status"] == "idle"
    # Its own explicit kill plus the choke point's. Both are `docker rm -f`.
    assert len(stops) == 2


# -- the honesty check -------------------------------------------------------

def test_a_running_container_with_a_silent_mixer_is_logged(app, monkeypatch,
                                                           caplog):
    from puffpai import media_hub as mh, radio_player
    monkeypatch.setattr(radio_player, "is_playing", lambda: True)
    monkeypatch.setattr("puffpai.now_playing._pulse_sink_input_count",
                        lambda: 0)

    with caplog.at_level("WARNING"):
        mh.radio_truth_check(app)

    assert any("alive and silent" in r.getMessage() for r in caplog.records)


def test_state_that_outlived_the_container_is_logged(app, monkeypatch, caplog):
    from puffpai import media_hub as mh, radio_player
    monkeypatch.setattr(radio_player, "is_playing", lambda: False)
    app.mediahub_report_radio("playing", station=_STATION, source="appliance")

    with caplog.at_level("WARNING"):
        mh.radio_truth_check(app)

    assert any("still says" in r.getMessage() for r in caplog.records)


def test_agreement_is_silent(app, monkeypatch, caplog):
    from puffpai import media_hub as mh, radio_player
    monkeypatch.setattr(radio_player, "is_playing", lambda: True)
    monkeypatch.setattr("puffpai.now_playing._pulse_sink_input_count",
                        lambda: 2)

    with caplog.at_level("WARNING"):
        mh.radio_truth_check(app)

    assert [r for r in caplog.records if "radio honesty" in r.getMessage()] == []


def test_an_unreadable_mixer_is_not_a_mismatch(app, monkeypatch, caplog):
    """pactl hidden by ProtectHome returns None - unknown is not a lie."""
    from puffpai import media_hub as mh, radio_player
    monkeypatch.setattr(radio_player, "is_playing", lambda: True)
    monkeypatch.setattr("puffpai.now_playing._pulse_sink_input_count",
                        lambda: None)

    with caplog.at_level("WARNING"):
        mh.radio_truth_check(app)

    assert [r for r in caplog.records if "radio honesty" in r.getMessage()] == []


# -- the dashboard surface, end to end through the route ---------------------

@pytest.fixture(scope="module")
def api():
    from unittest.mock import MagicMock
    from tests.modpatch import patched_modules

    src = str(REPO_ROOT / "src" / "puffpai")
    if src not in sys.path:
        sys.path.insert(0, src)
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod


@pytest.mark.parametrize("requested", ["stop", "pause"])
def test_the_dashboard_button_stops_the_appliance(api, app, stops, monkeypatch,
                                                  requested):
    monkeypatch.setattr(api, "_get_app_data", lambda: app)
    app.mediahub_report_radio("playing", station=_STATION, source="appliance")

    with api.app.test_client() as c:
        response = c.post("/apps/mediahub/action",
                          json={"action": "mediahub_radio_request",
                                "params": {"action": requested}},
                          headers={"X-API-Key": api._API_KEY})

    assert response.status_code == 200
    assert len(stops) == 1
    assert app.get_data("mediahub")["radio"]["status"] == "idle"
