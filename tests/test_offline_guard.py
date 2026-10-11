"""Tests for offline guard decorators."""

import pytest
from unittest.mock import patch

from puffpai.offline_guard import (
    NetworkStatus, requires_internet, requires_local_service,
    get_network_status
)


class TestNetworkStatus:
    def test_unreachable_host(self):
        status = NetworkStatus()
        # Use a non-routable IP to test failure
        assert status.is_reachable("192.0.2.1", 9999, timeout=0.5) is False

    def test_cache_works(self):
        status = NetworkStatus()
        # First call
        status.is_reachable("192.0.2.1", 9999, timeout=0.5)
        # Second call should be cached (fast)
        result = status.is_reachable("192.0.2.1", 9999, timeout=0.5)
        assert result is False

    def test_clear_cache(self):
        status = NetworkStatus()
        status._cache[("test", 80)] = (True, 999999999999.0)
        status.clear_cache()
        assert len(status._cache) == 0


class TestRequiresInternet:
    @patch.object(NetworkStatus, 'has_internet', return_value=False)
    def test_returns_offline_message(self, mock_inet):
        @requires_internet
        def web_search(query):
            return f"results for {query}"

        result = web_search("test")
        assert "[Offline]" in result
        assert "web_search" in result

    @patch.object(NetworkStatus, 'has_internet', return_value=True)
    def test_runs_when_online(self, mock_inet):
        @requires_internet
        def web_search(query):
            return f"results for {query}"

        result = web_search("test")
        assert result == "results for test"


class TestRequiresLocalService:
    @patch.object(NetworkStatus, 'is_reachable', return_value=False)
    def test_returns_offline_when_unreachable(self, mock_reach):
        @requires_local_service("localhost", 11435, "Ollama")
        def query_llm(prompt):
            return "response"

        result = query_llm("hello")
        assert "[Offline]" in result
        assert "Ollama" in result

    @patch.object(NetworkStatus, 'is_reachable', return_value=True)
    def test_runs_when_service_available(self, mock_reach):
        @requires_local_service("localhost", 11435, "Ollama")
        def query_llm(prompt):
            return "response"

        result = query_llm("hello")
        assert result == "response"
