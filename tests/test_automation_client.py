"""Tests for PuffPAI AutomationClient — Node-RED webhook communication layer.

Validates webhook-name validation, retry logic, error handling, config-driven
webhook URLs, and engine config loading, all without requiring a live
automation engine.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from puffpai.automation_client import AutomationClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _webhooks():
    return SimpleNamespace(
        block_device="/webhook/block-device",
        network_scan="/webhook/network-scan",
        weather_alerts="/webhook/weather-alerts",
        puffpai_voice="/webhook/puffpai_voice",
        # Flows that exist in nodered_flows/flows.json and must be present in
        # the backend's webhook map.
        daily_weather="/webhook/daily-weather",
        media_now_playing="/webhook/media-now-playing",
    )


@pytest.fixture
def mock_cfg():
    """Patch get_config to return test values (canonical `nodered` section)."""
    cfg = SimpleNamespace(
        nodered=SimpleNamespace(
            url="http://localhost:1880",
            timeout=5,
            retries=2,
            health_path="/",
            webhooks=_webhooks(),
        ),
    )
    with patch("puffpai.automation_client.get_config", return_value=cfg):
        yield cfg


@pytest.fixture
def client(mock_cfg, monkeypatch):
    # weather_alerts reaches api.weather.gov, so the client refuses to start it
    # while the Bridge's `weather` switch is off. These tests are about the
    # transport, so the switch is on; the refusal has its own tests in
    # test_egress_spotify_automation.py.
    monkeypatch.setenv("PUFFPAI_EGRESS_WEATHER", "1")
    return AutomationClient(max_retries=2)


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

class TestWebhookUrl:
    def test_builds_block_device_url(self, client):
        assert client.webhook_url("block_device") == "http://localhost:1880/webhook/block-device"

    def test_builds_weather_alerts_url(self, client):
        assert client.webhook_url("weather_alerts") == "http://localhost:1880/webhook/weather-alerts"

    def test_builds_network_scan_url(self, client):
        assert client.webhook_url("network_scan") == "http://localhost:1880/webhook/network-scan"

    def test_builds_daily_weather_url(self, client):
        """flows.json serves /webhook/daily-weather; the backend must know it."""
        assert client.webhook_url("daily_weather") == "http://localhost:1880/webhook/daily-weather"

    def test_builds_media_now_playing_url(self, client):
        """flows.json serves /webhook/media-now-playing; the backend must know it."""
        assert (client.webhook_url("media_now_playing")
                == "http://localhost:1880/webhook/media-now-playing")

    def test_unknown_webhook_raises(self, client):
        with pytest.raises(ValueError, match="Unknown webhook"):
            client.webhook_url("nonexistent")

    def test_path_traversal_rejected(self, client):
        """The allow-list is the only way to reach a path on the engine."""
        with pytest.raises(ValueError, match="Unknown webhook"):
            client.webhook_url("../../settings")

    def test_call_rejects_unknown_name_before_any_request(self, client):
        with patch("puffpai.automation_client.requests.post") as mock_post:
            with pytest.raises(ValueError, match="Unknown webhook"):
                client.call("not_a_webhook", {})
            mock_post.assert_not_called()

    def test_all_known_names_resolve_against_real_config(self):
        """Every name in the allow-list must exist on the real config object."""
        from puffpai.automation_client import KNOWN_WEBHOOKS
        from puffpai.config import AutomationWebhooks
        for name in KNOWN_WEBHOOKS:
            assert getattr(AutomationWebhooks(), name).startswith("/webhook/")

    def test_shipped_config_webhooks_match_the_dataclass(self):
        """The shipped JSON and the dataclass must not drift apart.

        The config loader silently drops keys that are not dataclass fields, so
        a typo in puffpai_config.json would fall back to the default without any
        error. Both new flow names must be present in both places.
        """
        import json
        from pathlib import Path
        from puffpai.automation_client import KNOWN_WEBHOOKS
        from puffpai.config import AutomationWebhooks

        cfg_path = Path(__file__).resolve().parents[1] / "config" / "puffpai_config.json"
        shipped = json.loads(cfg_path.read_text(encoding="utf-8"))["nodered"]["webhooks"]
        fields = set(AutomationWebhooks.__dataclass_fields__)

        assert not set(shipped) - fields, "puffpai_config.json has webhook keys the dataclass lacks"
        assert KNOWN_WEBHOOKS <= set(shipped), "allow-listed webhooks missing from puffpai_config.json"
        for name in ("daily_weather", "media_now_playing"):
            assert name in shipped
            assert shipped[name] == getattr(AutomationWebhooks(), name)


# ---------------------------------------------------------------------------
# Successful calls
# ---------------------------------------------------------------------------

class TestCallSuccess:
    @patch("puffpai.automation_client.requests.post")
    def test_post_returns_json(self, mock_post, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"alert_count": 2, "alerts": []}
        mock_post.return_value = mock_resp

        ok, data = client.call("weather_alerts", {"lat": 39.8, "lon": -89.6})
        assert ok is True
        assert data["alert_count"] == 2
        mock_post.assert_called_once()

    @patch("puffpai.automation_client.requests.post")
    def test_post_with_non_json_response(self, mock_post, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not JSON")
        mock_resp.text = "OK"
        mock_post.return_value = mock_resp

        ok, data = client.call("block_device", {"action": "block", "ip": "10.0.0.5"})
        assert ok is True
        assert data == {"raw": "OK"}

    @patch("puffpai.automation_client.requests.get")
    def test_get_method(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        mock_get.return_value = mock_resp

        ok, data = client.call("network_scan", {"q": "test"}, method="GET")
        assert ok is True
        mock_get.assert_called_once()


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetry:
    @patch("puffpai.automation_client.time.sleep")
    @patch("puffpai.automation_client.requests.post")
    def test_retries_on_connection_error(self, mock_post, mock_sleep, client):
        import requests as _req
        mock_post.side_effect = _req.exceptions.ConnectionError("refused")

        ok, msg = client.call("block_device", {"action": "block", "ip": "10.0.0.1"})
        assert ok is False
        assert "unreachable" in msg
        assert mock_post.call_count == 2  # max_retries=2

    @patch("puffpai.automation_client.time.sleep")
    @patch("puffpai.automation_client.requests.post")
    def test_retries_on_timeout(self, mock_post, mock_sleep, client):
        import requests as _req
        mock_post.side_effect = _req.exceptions.Timeout("timed out")

        ok, msg = client.call("weather_alerts", {"lat": 0, "lon": 0})
        assert ok is False
        assert "timeout" in msg.lower()
        assert mock_post.call_count == 2

    @patch("puffpai.automation_client.time.sleep")
    @patch("puffpai.automation_client.requests.post")
    def test_succeeds_on_second_attempt(self, mock_post, mock_sleep, client):
        import requests as _req

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True}

        mock_post.side_effect = [
            _req.exceptions.ConnectionError("refused"),
            mock_resp,
        ]

        ok, data = client.call("block_device", {"action": "block", "ip": "10.0.0.1"})
        assert ok is True
        assert data["success"] is True
        assert mock_post.call_count == 2

    @patch("puffpai.automation_client.time.sleep")
    @patch("puffpai.automation_client.requests.post")
    def test_retries_on_server_error(self, mock_post, mock_sleep, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        ok, msg = client.call("network_scan", {"devices": []})
        assert ok is False
        assert "500" in msg

    @patch("puffpai.automation_client.time.sleep")
    @patch("puffpai.automation_client.requests.post")
    def test_retry_count_defaults_to_config(self, mock_post, mock_sleep, mock_cfg):
        """max_retries falls back to nodered.retries when not passed."""
        import requests as _req
        mock_cfg.nodered.retries = 3
        mock_post.side_effect = _req.exceptions.ConnectionError("refused")

        ok, _ = AutomationClient().call("network_scan", {})
        assert ok is False
        assert mock_post.call_count == 3

    @patch("puffpai.automation_client.time.sleep")
    @patch("puffpai.automation_client.requests.post")
    def test_backoff_grows_between_attempts(self, mock_post, mock_sleep, mock_cfg):
        import requests as _req
        mock_cfg.nodered.retries = 3
        mock_post.side_effect = _req.exceptions.ConnectionError("refused")

        AutomationClient().call("network_scan", {})
        # Sleeps only between attempts (2 sleeps for 3 attempts), 0.5s * attempt.
        assert [c.args[0] for c in mock_sleep.call_args_list] == [0.5, 1.0]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestReachable:
    def test_health_url_is_base_plus_health_path(self, client):
        assert client.health_url() == "http://localhost:1880/"

    def test_health_path_is_configurable(self, mock_cfg):
        mock_cfg.nodered.health_path = "/healthz"
        assert AutomationClient().health_url() == "http://localhost:1880/healthz"

    @patch("puffpai.automation_client.requests.get")
    def test_reachable_when_server_up(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200)
        assert client.is_reachable() is True

    @patch("puffpai.automation_client.requests.get")
    def test_not_reachable_on_non_200(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=503)
        assert client.is_reachable() is False

    @patch("puffpai.automation_client.requests.get")
    def test_unreachable_when_server_down(self, mock_get, client):
        import requests as _req
        mock_get.side_effect = _req.exceptions.ConnectionError("refused")
        assert client.is_reachable() is False


# ---------------------------------------------------------------------------
# Custom base URL
# ---------------------------------------------------------------------------

class TestCustomConfig:
    def test_custom_base_url(self, mock_cfg):
        client = AutomationClient(base_url="http://192.168.1.100:1880")
        assert client.webhook_url("block_device") == "http://192.168.1.100:1880/webhook/block-device"

    def test_trailing_slash_stripped(self, mock_cfg):
        client = AutomationClient(base_url="http://myhost:1880/")
        assert client.webhook_url("weather_alerts") == "http://myhost:1880/webhook/weather-alerts"

    def test_base_url_property(self, client):
        assert client.base_url == "http://localhost:1880"


# ---------------------------------------------------------------------------
# Engine config
# ---------------------------------------------------------------------------

class TestEngineConfig:
    def test_no_engine_configured_raises(self):
        with patch("puffpai.automation_client.get_config",
                   return_value=SimpleNamespace(nodered=None)):
            with pytest.raises(ValueError, match="No automation engine"):
                AutomationClient()

    def test_config_loader_reads_nodered_section(self, tmp_path):
        import json
        from puffpai.config import _build_config

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "puffpai_config.json").write_text(json.dumps({
            "nodered": {"url": "http://10.0.0.5:1880", "retries": 4},
        }), encoding="utf-8")

        cfg = _build_config(str(tmp_path))
        assert cfg.nodered.url == "http://10.0.0.5:1880"
        assert cfg.nodered.retries == 4

    def test_config_accepts_base_url_alias(self, tmp_path):
        import json
        from puffpai.config import _build_config

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "puffpai_config.json").write_text(json.dumps({
            "nodered": {"base_url": "http://10.0.0.9:1880"},
        }), encoding="utf-8")

        assert _build_config(str(tmp_path)).nodered.url == "http://10.0.0.9:1880"
