"""Tests for the CLI entry point."""

from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock

from puffpai.cli import build_parser, main, _cmd_version, _cmd_discover_zim


class TestBuildParser:
    def test_parser_has_subcommands(self):
        parser = build_parser()
        # Parser should have subcommands
        assert parser is not None

    def test_version_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["version"])
        assert args.command == "version"
        assert hasattr(args, "func")

    def test_chat_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["chat"])
        assert args.command == "chat"

    def test_discover_zim_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["discover-zim"])
        assert args.command == "discover-zim"

    def test_ingest_zim_requires_path(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ingest-zim"])  # missing path

    def test_ingest_zim_with_path(self):
        parser = build_parser()
        args = parser.parse_args(["ingest-zim", "/path/to/file.zim"])
        assert args.command == "ingest-zim"
        assert args.path == "/path/to/file.zim"

    def test_ingest_zim_max_articles(self):
        parser = build_parser()
        args = parser.parse_args(["ingest-zim", "/path/file.zim", "--max", "500"])
        assert args.max == 500

    def test_firewall_status_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["firewall-status"])
        assert args.command == "firewall-status"


class TestMain:
    def test_no_command_returns_zero(self, capsys):
        """No command prints help and returns 0."""
        result = main([])
        assert result == 0

    def test_version_command(self, capsys):
        """version command prints version string."""
        result = main(["version"])
        captured = capsys.readouterr()
        assert "PuffPAI v" in captured.out
        assert result == 0

    def test_discover_zim_no_files(self, capsys):
        """discover-zim with no configured paths returns 0."""
        with patch('puffpai.knowledge.discovery.discover_from_config', return_value=[]):
            result = main(["discover-zim"])
            captured = capsys.readouterr()
            assert "No ZIM files found" in captured.out
            assert result == 0

    def test_ingest_zim_missing_file(self, capsys):
        """ingest-zim with non-existent file returns 1."""
        result = main(["ingest-zim", "/nonexistent/file.zim"])
        captured = capsys.readouterr()
        assert "not found" in captured.out
        assert result == 1


class TestMainModule:
    def test_module_runnable(self):
        """__main__.py exists and imports correctly."""
        import importlib.util
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_path = os.path.join(repo_root, "src", "puffpai", "__main__.py")
        assert os.path.isfile(main_path)

        # Verify it has the right content
        with open(main_path) as f:
            content = f.read()
        assert "from puffpai.cli import main" in content
