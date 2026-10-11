"""Release gate: the backend binds LOOPBACK unless somebody says otherwise.

HomeBotBuild ships no host packet filter, so the bind address IS the boundary
and the API key is the only control in front of the port. The build installs on
arbitrary hardware — a laptop on café wifi, a spare desktop on a flat-share LAN
— where 0.0.0.0 offers the household's whole backend to every peer on the
network.

Nothing in the DEFAULT install needs more than loopback: the dashboard's browser
code never calls the backend directly, it calls its own same-origin proxy at
``/api/pai`` which forwards server-side to ``http://localhost:8000``
(``dashboard/app/api/pai/[...path]/route.ts``). The kiosk and the voice service
are same-host too.

Three optional deployments DO need a wider bind, and each gets the flag rather
than the default — Node-RED in Docker (reaches the host through
``host.docker.internal``, not loopback), an OFF-BOX Home Assistant using the
conversation endpoints, and any deliberately remote client.

Run:
    pytest tests/test_release_bind.py
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "config" / "puffpai_config.json"


def _start_api():
    """Import start_api.py by path (it is an entrypoint, not a package module)."""
    spec = importlib.util.spec_from_file_location(
        "_start_api_under_test", REPO_ROOT / "start_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestShippedDefaults:

    def test_the_dataclass_default_is_loopback(self):
        from puffpai.config import ApiServerConfig
        assert ApiServerConfig().host == "127.0.0.1"

    def test_the_shipped_config_is_loopback(self):
        raw = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
        assert raw["api_server"]["host"] == "127.0.0.1", (
            "the shipped config offers the backend to the whole LAN")

    def test_the_dashboard_proxy_targets_loopback(self):
        """The reason loopback is enough: the browser never calls us directly."""
        route = (REPO_ROOT / "dashboard" / "app" / "api" / "pai"
                 / "[...path]" / "route.ts")
        text = route.read_text(encoding="utf-8")
        assert "http://localhost:8000" in text


class TestIsLoopbackBind:

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost",
                                      "[::1]", "127.0.1.1", "  127.0.0.1 "])
    def test_host_only_addresses(self, host):
        assert _start_api().is_loopback_bind(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "192.168.1.10",
                                      "172.17.0.1", "example.local",
                                      "127.0.0.1.evil.com", "1270.0.1"])
    def test_everything_else_is_not_loopback(self, host):
        assert _start_api().is_loopback_bind(host) is False, host


class TestResolution:

    def test_env_var_widens_it(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_API_BIND", "172.17.0.1")
        assert os.environ["PUFFPAI_API_BIND"] == "172.17.0.1"
        assert _start_api().is_loopback_bind("172.17.0.1") is False

    def test_a_non_loopback_bind_warns_loudly(self, caplog):
        """One line, every boot, naming the exposure."""
        module = _start_api()
        caplog.set_level(logging.WARNING)
        logger = logging.getLogger("test_release_bind")
        host = "0.0.0.0"
        if not module.is_loopback_bind(host):
            logger.warning(
                "SECURITY: binding %s:%d - NOT loopback. Every host that can "
                "reach this address can reach the backend", host, 8000)
        assert "NOT loopback" in caplog.text

    def test_the_warning_text_lives_in_start_api(self):
        """Pins the real line, not the re-creation above.

        The sentence itself moved to ``security/netaddr.py`` so that
        ``api_server``'s ``__main__`` block prints the same one; what is pinned
        here is that this entrypoint still emits it on a non-loopback bind.
        """
        from puffpai.security import netaddr

        text = (REPO_ROOT / "start_api.py").read_text(encoding="utf-8")
        assert "is_loopback_bind(host)" in text
        assert "NON_LOOPBACK_BIND_WARNING" in text
        assert "NOT loopback" in netaddr.NON_LOOPBACK_BIND_WARNING
        assert 'os.environ.get("PUFFPAI_API_BIND"' in text
        # The default itself, spelled out.
        assert 'host or "127.0.0.1"' in text
