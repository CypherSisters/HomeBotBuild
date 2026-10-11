"""Tests for puffpai.app_data — app launcher helpers (get_app_paths, open_app)."""

import os
import pytest
from unittest.mock import patch, MagicMock

from puffpai.app_data import get_app_paths, open_app, _APP_FILES, _APPS_DIR


class TestAppFiles:
    """_APP_FILES mapping validation."""

    def test_app_files_has_five_entries(self):
        assert len(_APP_FILES) == 5

    def test_all_ids_present(self):
        for app_id in ("alarm", "calendar", "freshtable", "fitfam", "trivia"):
            assert app_id in _APP_FILES

    def test_all_filenames_are_html(self):
        for filename in _APP_FILES.values():
            assert filename.endswith(".html")


class TestGetAppPaths:
    """get_app_paths() discovery logic."""

    def test_returns_dict(self):
        result = get_app_paths()
        assert isinstance(result, dict)

    def test_found_paths_are_absolute(self):
        """Every returned path is absolute and exists."""
        for app_id, path in get_app_paths().items():
            assert os.path.isabs(path)
            assert os.path.isfile(path)

    def test_only_existing_files_returned(self, tmp_path):
        """Only apps whose HTML file exists are returned."""
        with patch("puffpai.app_data._APPS_DIR", str(tmp_path)):
            # Create only one app file
            (tmp_path / "alarm.html").write_text("<html></html>")

            result = get_app_paths()
            assert "alarm" in result
            assert "calendar" not in result


class TestOpenApp:
    """open_app() browser launching."""

    def test_unknown_app_returns_false(self):
        assert open_app("nonexistent_app") is False

    def test_missing_file_returns_false(self):
        with patch("puffpai.app_data._APPS_DIR", "/nonexistent/dir"):
            assert open_app("alarm") is False

    def test_existing_file_opens_browser(self, tmp_path):
        with patch("puffpai.app_data._APPS_DIR", str(tmp_path)):
            (tmp_path / "alarm.html").write_text("<html></html>")
            with patch("webbrowser.open") as mock_open:
                result = open_app("alarm")
                assert result is True
                mock_open.assert_called_once()
                call_arg = mock_open.call_args[0][0]
                assert "alarm.html" in call_arg

    def test_real_apps_directory_exists(self):
        """Verify the Applications directory exists on this machine."""
        assert os.path.isdir(_APPS_DIR), (
            f"Applications directory not found at {_APPS_DIR}")
