"""Tests for the ZIM discovery module."""

import os
import struct
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from puffpai.knowledge.discovery import (
    discover_zim_files,
    get_zim_metadata,
    discover_from_config,
    _looks_like_zim,
    _ZIM_MAGIC,
)


# ── Helper: create a minimal fake ZIM file ────────────────────────────────

def _make_fake_zim(dirpath: str, filename: str = "test.zim",
                   entry_count: int = 100, cluster_count: int = 10) -> str:
    """Create an 80-byte fake ZIM header file for testing."""
    filepath = os.path.join(dirpath, filename)
    header = bytearray(80)
    # Magic number at offset 0
    struct.pack_into('<I', header, 0, _ZIM_MAGIC)
    # Major/minor version at offset 4
    struct.pack_into('<HH', header, 4, 5, 0)
    # UUID at offset 8 (16 bytes)
    header[8:24] = b'\xaa' * 16
    # entry_count, cluster_count at offset 24
    struct.pack_into('<II', header, 24, entry_count, cluster_count)
    # url_ptr_pos, title_ptr_pos at offset 32
    struct.pack_into('<QQ', header, 32, 80, 80)
    # cluster_ptr_pos, mime_list_pos at offset 48
    struct.pack_into('<QQ', header, 48, 80, 80)
    # main_page, layout_page at offset 64
    struct.pack_into('<II', header, 64, 0xFFFFFFFF, 0xFFFFFFFF)
    # checksum_pos at offset 72
    struct.pack_into('<Q', header, 72, 80)

    with open(filepath, 'wb') as f:
        f.write(header)
    return filepath


# ── discover_zim_files tests ──────────────────────────────────────────────

class TestDiscoverZimFiles:
    def test_finds_zim_in_directory(self, tmp_path):
        _make_fake_zim(str(tmp_path), "wikipedia.zim")
        _make_fake_zim(str(tmp_path), "wiktionary.zim")
        result = discover_zim_files(zim_dirs=[str(tmp_path)])
        assert len(result) == 2
        assert all(p.endswith('.zim') for p in result)

    def test_explicit_file_paths(self, tmp_path):
        zim_path = _make_fake_zim(str(tmp_path), "explicit.zim")
        result = discover_zim_files(zim_files=[zim_path])
        assert len(result) == 1
        assert result[0] == os.path.abspath(zim_path)

    def test_ignores_non_zim_files(self, tmp_path):
        _make_fake_zim(str(tmp_path), "real.zim")
        # Create a .txt file that's not ZIM
        txt_path = os.path.join(str(tmp_path), "notes.txt")
        with open(txt_path, 'w') as f:
            f.write("Not a ZIM file")
        result = discover_zim_files(zim_dirs=[str(tmp_path)])
        assert len(result) == 1

    def test_ignores_fake_zim_extension(self, tmp_path):
        """A .zim file without valid magic should be skipped."""
        fake = os.path.join(str(tmp_path), "fake.zim")
        with open(fake, 'wb') as f:
            f.write(b'\x00' * 80)  # invalid magic
        result = discover_zim_files(zim_dirs=[str(tmp_path)])
        assert len(result) == 0

    def test_deduplicates_across_dirs_and_files(self, tmp_path):
        zim_path = _make_fake_zim(str(tmp_path), "shared.zim")
        result = discover_zim_files(
            zim_dirs=[str(tmp_path)],
            zim_files=[zim_path],
        )
        assert len(result) == 1

    def test_handles_missing_directory(self):
        result = discover_zim_files(zim_dirs=["/nonexistent/path/xyz123"])
        assert result == []

    def test_handles_empty_inputs(self):
        result = discover_zim_files()
        assert result == []

    def test_returns_sorted_paths(self, tmp_path):
        _make_fake_zim(str(tmp_path), "zoo.zim")
        _make_fake_zim(str(tmp_path), "alpha.zim")
        _make_fake_zim(str(tmp_path), "mid.zim")
        result = discover_zim_files(zim_dirs=[str(tmp_path)])
        assert result == sorted(result)


# ── _looks_like_zim tests ────────────────────────────────────────────────

class TestLooksLikeZim:
    def test_valid_zim_header(self, tmp_path):
        path = _make_fake_zim(str(tmp_path))
        assert _looks_like_zim(path) is True

    def test_invalid_magic(self, tmp_path):
        path = os.path.join(str(tmp_path), "bad.zim")
        with open(path, 'wb') as f:
            f.write(struct.pack('<I', 0xDEADBEEF))
        assert _looks_like_zim(path) is False

    def test_too_small_file(self, tmp_path):
        path = os.path.join(str(tmp_path), "tiny.zim")
        with open(path, 'wb') as f:
            f.write(b'\x00\x01')
        assert _looks_like_zim(path) is False

    def test_nonexistent_file(self):
        assert _looks_like_zim("/nonexistent/file.zim") is False


# ── get_zim_metadata tests ───────────────────────────────────────────────

class TestGetZimMetadata:
    def test_extracts_header_fields(self, tmp_path):
        path = _make_fake_zim(str(tmp_path), entry_count=500, cluster_count=25)
        meta = get_zim_metadata(path)
        assert meta["entry_count"] == 500
        assert meta["cluster_count"] == 25
        assert meta["major_version"] == 5
        assert meta["minor_version"] == 0
        assert meta["filename"] == "test.zim"
        assert meta["filepath"] == os.path.abspath(path)
        assert meta["file_size_bytes"] == 80
        assert len(meta["uuid_hex"]) == 32  # 16 bytes as hex

    def test_raises_for_missing_file(self):
        with pytest.raises(FileNotFoundError):
            get_zim_metadata("/nonexistent/file.zim")

    def test_raises_for_invalid_magic(self, tmp_path):
        path = os.path.join(str(tmp_path), "bad.zim")
        with open(path, 'wb') as f:
            f.write(b'\x00' * 80)
        with pytest.raises(ValueError, match="Not a ZIM file"):
            get_zim_metadata(path)

    def test_raises_for_truncated_file(self, tmp_path):
        path = os.path.join(str(tmp_path), "truncated.zim")
        with open(path, 'wb') as f:
            f.write(struct.pack('<I', _ZIM_MAGIC))  # only 4 bytes
        with pytest.raises(ValueError, match="too small"):
            get_zim_metadata(path)


# ── discover_from_config tests ───────────────────────────────────────────

class TestDiscoverFromConfig:
    def test_uses_config_paths(self, tmp_path):
        _make_fake_zim(str(tmp_path), "config.zim")

        mock_config = MagicMock()
        mock_config.paths.zim_dirs = [str(tmp_path)]
        mock_config.paths.zim_files = []

        with patch('puffpai.knowledge.discovery.get_config',
                   create=True) as mock_get:
            # Patch the import inside discover_from_config
            with patch.dict('sys.modules', {}):
                # We need to patch at the function level
                pass

        # Simpler approach: patch the config import inside the function
        import puffpai.knowledge.discovery as disc_mod
        original = disc_mod.discover_from_config

        with patch('puffpai.config.get_config') as mock_get:
            mock_get.return_value = mock_config
            result = disc_mod.discover_from_config()
            assert len(result) == 1
            assert result[0].endswith("config.zim")

    def test_returns_empty_on_config_error(self):
        """Gracefully returns [] if config loading fails."""
        with patch('puffpai.config.get_config',
                   side_effect=RuntimeError("No config")):
            from puffpai.knowledge.discovery import discover_from_config
            result = discover_from_config()
            assert result == []
