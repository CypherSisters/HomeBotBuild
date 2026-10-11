"""Tests for PuffPAI logging configuration."""

import logging
import os
import pytest

from puffpai.logging_config import setup_logging, get_logger, _configured


class TestSetupLogging:
    def setup_method(self):
        """Reset state before each test."""
        import puffpai.logging_config as lc
        lc._configured = False
        # Clean up any existing handlers
        root = logging.getLogger("puffpai")
        root.handlers.clear()

    def test_returns_puffpai_logger(self):
        logger = setup_logging()
        assert logger.name == "puffpai"

    def test_sets_level(self):
        logger = setup_logging("DEBUG")
        assert logger.level == logging.DEBUG

    def test_adds_console_handler(self):
        logger = setup_logging()
        assert len(logger.handlers) >= 1
        assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)

    def test_file_handler_created(self, tmp_path):
        log_file = tmp_path / "test.log"
        logger = setup_logging("INFO", log_file=str(log_file))
        from logging.handlers import RotatingFileHandler
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)

    def test_file_handler_writes(self, tmp_path):
        log_file = tmp_path / "test.log"
        logger = setup_logging("INFO", log_file=str(log_file))
        logger.info("test message 12345")
        # Flush handlers
        for h in logger.handlers:
            h.flush()
        content = log_file.read_text()
        assert "test message 12345" in content

    def test_idempotent_without_force(self):
        setup_logging("INFO")
        logger = setup_logging("DEBUG")  # Should NOT reconfigure
        # Level stays at INFO because second call was skipped
        assert logger.level == logging.INFO

    def test_force_reconfigures(self):
        setup_logging("INFO")
        logger = setup_logging("DEBUG", force=True)
        assert logger.level == logging.DEBUG


class TestGetLogger:
    def test_returns_child_logger(self):
        logger = get_logger("security.vault")
        assert logger.name == "puffpai.security.vault"

    def test_already_prefixed(self):
        logger = get_logger("puffpai.inference")
        assert logger.name == "puffpai.inference"


class TestRedactSecrets:
    """``redact_secrets`` is the guard on INFO/DEBUG logging going live.

    The cases below are the shapes credentials actually arrive in on this
    appliance: a requests exception carrying the Pi-hole v5 ``?auth=`` query
    param, an Authorization header, a Jellyfin ``Token="..."`` header, and a
    dict/JSON dump of the vault's service-secret names.
    """

    @pytest.mark.parametrize("text,secret", [
        ("/admin/api.php?summary=&auth=9f86d081884c7d659a2feaa0",
         "9f86d081884c7d659a2feaa0"),
        ('Client="PuffPAI", Token="jf_live_key_abc123"', "jf_live_key_abc123"),
        ("{'Authorization': 'Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghij.sig'}",
         "eyJhbGciOiJIUzI1NiJ9"),
        ("https://admin:hunter2@pi.hole/x", "hunter2"),
        ("?access_token=abc123def456ghi", "abc123def456ghi"),
        ('{"password": "p@ssw0rd"}', "p@ssw0rd"),
        ("{'api_key': 'sk-live-999', 'model': 'qwen3'}", "sk-live-999"),
        ('{"HA_TOKEN": "eyJhb.longlived.xyz"}', "eyJhb.longlived.xyz"),
        ('{"PUFFPAI_API_KEY": "deadbeef00"}', "deadbeef00"),
        ('{"NODERED_API_TOKEN": "nrt_secret"}', "nrt_secret"),
        ("?ha_token=abcdef123456", "abcdef123456"),
    ])
    def test_masks_credential(self, text, secret):
        from puffpai.logging_config import redact_secrets
        out = redact_secrets(text)
        assert secret not in out
        assert "<redacted>" in out

    @pytest.mark.parametrize("text", [
        "Basic authentication failed",
        "monkey=banana turkey=leg",
        "Max retries exceeded with url: /api/stats/summary",
        "{'model': 'qwen3:30b', 'url': 'http://localhost:11434'}",
        "engine ok in 1.23s (qwen3:30b-a3b)",
        "dispatching task 7: alarm.set -> steward",
    ])
    def test_leaves_diagnostics_alone(self, text):
        """Over-redaction defeats the point of turning INFO on."""
        from puffpai.logging_config import redact_secrets
        assert redact_secrets(text) == text

    def test_requests_exception_loses_the_query_credential(self):
        """The live shape: requests stringifies the full URL, params included."""
        import requests
        from puffpai.logging_config import redact_secrets
        with pytest.raises(requests.RequestException) as excinfo:
            requests.get("http://127.0.0.1:9/admin/api.php",
                         params={"summary": "", "auth": "SUPERSECRETTOKEN"},
                         timeout=1)
        raw = str(excinfo.value)
        assert "SUPERSECRETTOKEN" in raw, "premise changed: no credential in exc"
        assert "SUPERSECRETTOKEN" not in redact_secrets(excinfo.value)

    def test_accepts_non_string(self):
        from puffpai.logging_config import redact_secrets
        assert redact_secrets(ValueError("?auth=abc123")) == "?auth=<redacted>"
