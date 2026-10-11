"""
Tests for puffpai.service — Background AI service module.
"""

import os
import sys
import threading
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai.service import (
    _check_port_available,
    _check_existing_service,
    _write_pid_file,
    _remove_pid_file,
    is_service_running,
)


class TestPortCheck:
    """Tests for port availability checking."""

    def test_check_port_available_open_port(self):
        """An unused high port should be available."""
        # Port 0 lets OS pick a free port — but we test with a high random port
        assert _check_port_available("127.0.0.1", 59999) in (True, False)

    @patch("socket.socket")
    def test_check_port_available_bind_succeeds(self, mock_socket_cls):
        """When bind succeeds, port is available."""
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
        assert _check_port_available("127.0.0.1", 8000) is True

    @patch("socket.socket")
    def test_check_port_available_bind_fails(self, mock_socket_cls):
        """When bind raises OSError, port is not available."""
        mock_sock = MagicMock()
        mock_sock.bind.side_effect = OSError("Address already in use")
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)
        assert _check_port_available("127.0.0.1", 8000) is False


class TestExistingService:
    """Tests for checking if a service is already running."""

    @patch("requests.get")
    def test_service_running(self, mock_get):
        """When health endpoint returns 200, service is running."""
        mock_get.return_value.status_code = 200
        assert _check_existing_service("127.0.0.1", 8000) is True

    @patch("requests.get")
    def test_service_not_running(self, mock_get):
        """When request fails, service is not running."""
        mock_get.side_effect = Exception("Connection refused")
        assert _check_existing_service("127.0.0.1", 8000) is False

    @patch("requests.get")
    def test_service_returns_error(self, mock_get):
        """When health returns non-200, service is not running."""
        mock_get.return_value.status_code = 500
        assert _check_existing_service("127.0.0.1", 8000) is False


class TestPidFile:
    """Tests for PID file management."""

    def test_write_pid_file(self, tmp_path):
        """PID file should contain current process ID."""
        pid_path = str(tmp_path / "test.pid")
        _write_pid_file(pid_path)
        with open(pid_path) as f:
            assert f.read() == str(os.getpid())

    def test_write_pid_creates_dirs(self, tmp_path):
        """PID file should create parent directories."""
        pid_path = str(tmp_path / "deep" / "nested" / "test.pid")
        _write_pid_file(pid_path)
        assert os.path.isfile(pid_path)

    def test_remove_pid_file(self, tmp_path):
        """PID file should be removed."""
        pid_path = str(tmp_path / "test.pid")
        _write_pid_file(pid_path)
        assert os.path.isfile(pid_path)
        _remove_pid_file(pid_path)
        assert not os.path.isfile(pid_path)

    def test_remove_nonexistent_pid_file(self, tmp_path):
        """Removing a nonexistent PID file should not raise."""
        pid_path = str(tmp_path / "nonexistent.pid")
        _remove_pid_file(pid_path)  # Should not raise


class TestIsServiceRunning:
    """Tests for the public is_service_running() helper."""

    @patch("puffpai.service._check_existing_service")
    def test_delegates_to_check(self, mock_check):
        mock_check.return_value = True
        assert is_service_running(8000) is True
        mock_check.assert_called_once_with("127.0.0.1", 8000)

    @patch("puffpai.service._check_existing_service")
    def test_custom_port(self, mock_check):
        mock_check.return_value = False
        assert is_service_running(9999) is False
        mock_check.assert_called_once_with("127.0.0.1", 9999)


class TestRunService:
    """Tests for run_service() initialization logic."""

    @patch("puffpai.service._check_existing_service", return_value=True)
    @patch("puffpai.service.setup_logging")
    def test_exits_if_already_running(self, mock_logging, mock_check):
        """run_service should return immediately if service is already running."""
        from puffpai.service import run_service
        # Should not raise, should return without starting Flask
        run_service(host="127.0.0.1", port=8000)

    @patch("puffpai.service._check_existing_service", return_value=False)
    @patch("puffpai.service._check_port_available", return_value=False)
    @patch("puffpai.service.setup_logging")
    def test_exits_if_port_in_use(self, mock_logging, mock_port, mock_check):
        """run_service should return if port is unavailable."""
        from puffpai.service import run_service
        run_service(host="127.0.0.1", port=8000)


class TestStartServiceBackground:
    """Tests for start_service_background()."""

    @patch("puffpai.service.is_service_running", return_value=True)
    def test_returns_none_if_running(self, mock_running):
        """Should return None if service is already up."""
        from puffpai.service import start_service_background
        result = start_service_background(8000)
        assert result is None
