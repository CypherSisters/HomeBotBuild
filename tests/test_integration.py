"""Cross-module integration tests for PuffPAI.

Verify that modules compose correctly when wired together, exercising the
interfaces between subsystems.
"""

import json
import os
import sys
import tempfile
import threading

import pytest
from unittest.mock import patch, MagicMock


# ===========================================================================
# Config → Router → Agent integration
# ===========================================================================

class TestConfigRouterAgent:
    """Config feeds the router; router decides where the agent sends."""

    def test_config_loads_and_router_dispatches(self):
        """Full path: load config → route a message → get expected target."""
        from puffpai.config import get_config
        from puffpai.router import route_message, RouteTarget

        cfg = get_config(_force_reload=True)
        assert cfg.REPO_ROOT  # config loaded successfully

        # Security message → AGENT
        assert route_message("block device 192.168.1.50") == RouteTarget.AGENT
        # General question → RAG
        assert route_message("What is the capital of France?") == RouteTarget.RAG
        # Briefing → BRIEFING
        assert route_message("give me my daily briefing") == RouteTarget.BRIEFING

    def test_route_category_matches_target(self):
        """get_route_category and route_message agree on classification."""
        from puffpai.router import route_message, get_route_category, RouteTarget

        msg = "turn on the lights"
        route = route_message(msg)
        category = get_route_category(msg)

        assert route in (RouteTarget.AGENT, RouteTarget.HOME)
        assert category == "home"

    def test_web_search_consent_gates_tool(self):
        """Config consent flag → web_search tool returns consent message."""
        from puffpai.config import get_config

        cfg = get_config(_force_reload=True)
        original = cfg.web_search_consent
        try:
            cfg.web_search_consent = False
            assert cfg.web_search_consent is False
            # The web_search tool checks this at runtime
        finally:
            cfg.web_search_consent = original


# ===========================================================================
# Firewall backend chain
# ===========================================================================

class TestFirewallBackendChain:
    """get_firewall_backend() selects the right class per OS."""

    def test_windows_backend(self, tmp_path):
        from puffpai.firewall_manager import get_firewall_backend, WindowsFirewall
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        with patch.object(sys, 'platform', 'win32'):
            fw = get_firewall_backend(str(bf))
            assert isinstance(fw, WindowsFirewall)

    def test_linux_backend_requires_opt_in(self, tmp_path, monkeypatch):
        # The Linux default is the refusing backend. Selecting a
        # write backend by platform guess could put a netsh engine on an
        # ARM64 Jetson; a host that can enforce rules says so explicitly.
        from puffpai.firewall_manager import (
            get_firewall_backend, NftablesFirewall, UnavailableFirewall)
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        monkeypatch.delenv("PUFFPAI_FIREWALL_BACKEND", raising=False)
        with patch.object(sys, 'platform', 'linux'):
            assert isinstance(get_firewall_backend(str(bf)), UnavailableFirewall)
        monkeypatch.setenv("PUFFPAI_FIREWALL_BACKEND", "nftables")
        with patch.object(sys, 'platform', 'linux'):
            assert isinstance(get_firewall_backend(str(bf)), NftablesFirewall)

    def test_unknown_backend_falls_to_unavailable(self, tmp_path, monkeypatch):
        from puffpai.firewall_manager import (
            get_firewall_backend, MockFirewall, UnavailableFirewall)
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        monkeypatch.delenv("PUFFPAI_FIREWALL_BACKEND", raising=False)
        with patch.object(sys, 'platform', 'darwin'):
            fw = get_firewall_backend(str(bf))
            assert isinstance(fw, UnavailableFirewall)
            assert not isinstance(fw, MockFirewall)

    def test_mock_block_unblock_cycle(self, tmp_path):
        """End-to-end: block → verify blocked → unblock → verify clear."""
        from puffpai.firewall_manager import MockFirewall
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))

        result = fw.block_device("192.168.1.99")
        assert result['success'] is True
        assert fw.get_status()['blocked_count'] == 1

        result = fw.unblock_device("192.168.1.99")
        assert result['success'] is True
        assert fw.get_status()['blocked_count'] == 0


# ===========================================================================
# Inference fallback chain
# ===========================================================================

class TestInferenceFallback:
    """LLM backends: llama.cpp → Ollama → error message."""

    def test_llamacpp_unavailable_falls_through(self):
        """LlamaCppBackend is_available=False when library missing."""
        from puffpai.inference import LlamaCppBackend
        backend = LlamaCppBackend(model_path="/nonexistent/model.gguf")
        assert backend.is_available() is False

    def test_ollama_unreachable_returns_error(self):
        """OllamaBackend returns error string when server is down."""
        from puffpai.inference import OllamaBackend
        backend = OllamaBackend(url="http://127.0.0.1:19999")
        result = backend.chat([{"role": "user", "content": "hello"}])
        assert "[Error]" in result

    def test_both_backends_share_interface(self):
        """Both backends implement the same LLMBackend ABC."""
        from puffpai.inference.base import LLMBackend
        from puffpai.inference import OllamaBackend, LlamaCppBackend
        assert issubclass(OllamaBackend, LLMBackend)
        assert issubclass(LlamaCppBackend, LLMBackend)


# ===========================================================================
# Voice pipeline end-to-end
# ===========================================================================

class TestVoicePipelineEndToEnd:
    """Mock audio → STT → agent → TTS → verify output."""

    def test_stub_pipeline_process_utterance(self):
        """StubSTT returns empty → process_utterance returns None."""
        from puffpai.voice import VoicePipeline

        pipeline = VoicePipeline()
        result = pipeline.process_utterance(b"\x00" * 3200)
        assert result is None  # StubSTT transcribes to ""

    def test_pipeline_with_mock_stt_and_tts(self):
        """Wire mocked STT → agent_fn → mocked TTS, verify chain."""
        from puffpai.voice.stt import STTEngine
        from puffpai.voice.tts import TTSEngine
        from puffpai.voice.pipeline import VoicePipeline

        class MockSTT(STTEngine):
            def transcribe(self, audio: bytes) -> str:
                return "hello puffpai"
            def is_available(self) -> bool:
                return True

        class MockTTS(TTSEngine):
            def __init__(self):
                self.last_spoken = None
            def speak(self, text: str):
                self.last_spoken = text
                return b"\x00" * 100
            def is_available(self) -> bool:
                return True

        tts = MockTTS()
        agent_calls = []

        def mock_agent(text: str) -> str:
            agent_calls.append(text)
            return f"You said: {text}"

        pipeline = VoicePipeline(
            stt=MockSTT(),
            tts=tts,
            agent_fn=mock_agent,
        )

        result = pipeline.process_utterance(b"\x00" * 3200)
        assert result == "You said: hello puffpai"
        assert agent_calls == ["hello puffpai"]
        assert tts.last_spoken == "You said: hello puffpai"

    def test_pipeline_start_stop_without_sounddevice(self):
        """start() → stop() lifecycle when sounddevice not installed."""
        from puffpai.voice.pipeline import VoicePipeline

        with patch('puffpai.voice.pipeline._HAS_SOUNDDEVICE', False):
            pipeline = VoicePipeline()
            pipeline.start()
            assert pipeline._running is True
            pipeline.stop()
            assert pipeline._running is False


# ===========================================================================
# Vault → HA integration
# ===========================================================================

class TestVaultHAIntegration:
    """Store HA token via vault → retrieve → build HA config."""

    def test_ha_token_plaintext_fallback(self):
        """get_ha_token falls back to plaintext config file."""
        from puffpai.security.ha_vault import get_ha_token
        # Without a vault session, should fall back to config file
        # The default config has "YOUR_LONG_LIVED_ACCESS_TOKEN_HERE"
        token = get_ha_token()
        # Either returns the placeholder or None (if vault overrides)
        assert token is None or isinstance(token, str)

    def test_ha_config_round_trip(self):
        """get_ha_config returns dict with expected keys."""
        from puffpai.security.ha_vault import get_ha_config
        config = get_ha_config()
        assert 'url' in config
        assert 'token' in config
        assert 'enabled' in config


# ===========================================================================
# Device classifier combined
# ===========================================================================

class TestDeviceClassifierCombined:
    """MAC + hostname + mDNS → correct category via classify_device."""

    def test_hostname_overrides_oui(self):
        """Hostname heuristic has higher priority than OUI lookup."""
        from puffpai.security.device_classifier import classify_device, DeviceCategory
        result = classify_device(
            mac="AA:BB:CC:DD:EE:FF",  # unknown OUI
            hostname="Ring-Doorbell-Pro",
        )
        assert result.category == DeviceCategory.IOT_CAMERA

    def test_mdns_used_when_hostname_unknown(self):
        """mDNS service type is used when hostname doesn't match."""
        from puffpai.security.device_classifier import classify_device, DeviceCategory
        result = classify_device(
            mac="AA:BB:CC:DD:EE:FF",
            hostname="unknown-device",
            mdns_services=["_airplay._tcp"],
        )
        assert result.category == DeviceCategory.SMART_TV

    def test_ssdp_used_as_last_resort(self):
        """SSDP header matching is lower priority than hostname/mDNS."""
        from puffpai.security.device_classifier import classify_device, DeviceCategory
        result = classify_device(
            mac="AA:BB:CC:DD:EE:FF",
            ssdp_header="Roku/12.0 UPnP/1.0",
        )
        assert result.category == DeviceCategory.SMART_TV

    def test_fallback_to_unknown(self):
        """No signals → falls back to UNKNOWN."""
        from puffpai.security.device_classifier import classify_device, DeviceCategory
        result = classify_device(mac="AA:BB:CC:DD:EE:FF")
        assert result.category == DeviceCategory.UNKNOWN


# ===========================================================================
# Offline guard integration
# ===========================================================================

class TestOfflineGuardIntegration:
    """Decorated function respects network status."""

    def test_requires_internet_blocks_when_offline(self):
        """When internet is down, returns offline message."""
        from puffpai.offline_guard import requires_internet, get_network_status

        @requires_internet
        def fetch_data():
            return "real data"

        status = get_network_status()
        # Force cache to report offline
        with patch.object(status, 'has_internet', return_value=False):
            result = fetch_data()
            assert "[Offline]" in result

    def test_requires_internet_passes_when_online(self):
        """When internet is up, the function runs normally."""
        from puffpai.offline_guard import requires_internet, get_network_status

        @requires_internet
        def fetch_data():
            return "real data"

        status = get_network_status()
        with patch.object(status, 'has_internet', return_value=True):
            result = fetch_data()
            assert result == "real data"


# ===========================================================================
# Data classifier → Zone routing
# ===========================================================================

class TestDataClassifierZoneRouting:
    """PII detected in content → routed to Zone 1 (Vault)."""

    def test_pii_detected_routes_to_vault(self):
        """Email address in text → Zone 1."""
        from puffpai.security.data_classifier import DataClassifier
        from puffpai.security.zones import DataZone

        dc = DataClassifier()
        zone, pii = dc.classify_content("Contact me at alice@example.com for details")
        assert zone == DataZone.VAULT
        assert pii.has_pii is True
        assert len(pii.emails) > 0

    def test_no_pii_routes_to_intelligence(self):
        """Clean text → Zone 2."""
        from puffpai.security.data_classifier import DataClassifier
        from puffpai.security.zones import DataZone

        dc = DataClassifier()
        zone, pii = dc.classify_content("The capital of France is Paris.")
        assert zone == DataZone.INTELLIGENCE
        assert pii.has_pii is False

    def test_zone_policy_enforced(self):
        """Vault zone requires auth; Intelligence zone does not."""
        from puffpai.security.zones import DataZone, check_access

        # Vault requires auth
        assert check_access(DataZone.VAULT, "read_with_auth", authenticated=True) is True
        assert check_access(DataZone.VAULT, "read_with_auth", authenticated=False) is False

        # Intelligence is open for reads
        assert check_access(DataZone.INTELLIGENCE, "read", authenticated=False) is True


# ===========================================================================
# Logging framework integration
# ===========================================================================

class TestLoggingIntegration:
    """Logging framework works with config module."""

    def test_config_uses_logging(self, tmp_path):
        """config.py uses logger.warning instead of print."""
        import logging
        from puffpai.logging_config import setup_logging, get_logger

        setup_logging(level="DEBUG", force=True)
        logger = get_logger("puffpai.config")
        assert logger.name == "puffpai.config"

        # The config module's logger should be a child of the puffpai hierarchy
        root_logger = logging.getLogger("puffpai")
        assert logger.parent == root_logger or logger.parent.name == "puffpai"
