"""Tests for avatar_generator.py — PIL-primary avatar generation."""

import pytest
from PIL import Image

from puffpai.avatar_generator import (
    AvatarGenerator, generate_avatar_pil, get_initials,
)


class TestGetInitials:
    def test_two_words(self):
        assert get_initials("John Smith") == "JS"

    def test_single_word(self):
        assert get_initials("Puff") == "P"

    def test_empty(self):
        assert get_initials("") == "?"

    def test_three_words(self):
        assert get_initials("John Michael Smith") == "JS"

    def test_whitespace_only(self):
        assert get_initials("   ") == "?"


class TestGenerateAvatarPIL:
    def test_returns_rgba_image(self):
        img = generate_avatar_pil("Test", size=100)
        assert isinstance(img, Image.Image)
        assert img.mode == 'RGBA'
        assert img.size == (100, 100)

    def test_deterministic(self):
        """Same name always gives the same avatar."""
        img1 = generate_avatar_pil("Puff", size=64)
        img2 = generate_avatar_pil("Puff", size=64)
        assert list(img1.getdata()) == list(img2.getdata())

    def test_different_names_different_colors(self):
        """Different names should (usually) give different avatars."""
        img1 = generate_avatar_pil("Alice", size=64)
        img2 = generate_avatar_pil("Bob", size=64)
        # At minimum, the pixel data should differ
        assert list(img1.getdata()) != list(img2.getdata())


class TestAvatarGenerator:
    def test_generate_pil_default(self, tmp_path, monkeypatch):
        """Default generation uses PIL (no Node.js)."""
        monkeypatch.setattr('puffpai.avatar_generator._CACHE_DIR', str(tmp_path))
        gen = AvatarGenerator()
        img = gen.generate("Test", size=64)
        assert isinstance(img, Image.Image)
        assert img.size == (64, 64)

    def test_caching(self, tmp_path, monkeypatch):
        """Second call should load from cache."""
        monkeypatch.setattr('puffpai.avatar_generator._CACHE_DIR', str(tmp_path))
        gen = AvatarGenerator()
        img1 = gen.generate("Test", size=64)
        img2 = gen.generate("Test", size=64)
        # Both should succeed
        assert img1.size == img2.size

    def test_clear_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr('puffpai.avatar_generator._CACHE_DIR', str(tmp_path))
        gen = AvatarGenerator()
        gen.generate("Test", size=64)
        # Cache should have at least 1 PNG
        assert any(f.name.endswith('.png') for f in tmp_path.iterdir())
        gen.clear_cache()
        assert not any(f.name.endswith('.png') for f in (tmp_path).iterdir())
