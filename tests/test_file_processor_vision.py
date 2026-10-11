"""
Tests for the vision-model escalation wired into file_processor._extract_image.

A fake ``vision_fn`` stands in for the Ollama call; pytesseract is monkeypatched
so we control the "OCR" result without needing tesseract installed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

pytest.importorskip("PIL")

from puffpai import file_processor as fp  # noqa: E402


def _make_png(tmp_path):
    from PIL import Image
    p = tmp_path / "photo.png"
    Image.new("RGB", (48, 24), (255, 255, 255)).save(p)
    return str(p)


def _fake_pytesseract(monkeypatch, ocr_text):
    """Force pytesseract.image_to_string to return a fixed string."""
    import types
    fake = types.ModuleType("pytesseract")
    fake.image_to_string = lambda img, **kw: ocr_text
    monkeypatch.setitem(sys.modules, "pytesseract", fake)


def test_weak_ocr_escalates_and_uses_vision(tmp_path, monkeypatch):
    img = _make_png(tmp_path)
    _fake_pytesseract(monkeypatch, "x")  # sparse -> weak

    rich = "Soccer practice Tuesday 4pm at City Field\nDentist Wed 9am"
    calls = []

    def vision_fn(path):
        calls.append(path)
        return rich

    text, meta = fp._extract_image(img, vision_fn=vision_fn,
                                   vision_min_chars=120)

    assert calls == [img]
    assert text == rich
    assert meta["extractor"] == "qwen_vl"
    assert meta["vision_used"] is True
    assert meta["tesseract_chars"] == 1


def test_rich_ocr_does_not_escalate(tmp_path, monkeypatch):
    img = _make_png(tmp_path)
    good = ("Weekly schedule for the household this week\n"
            "Monday 9am standup meeting in the kitchen\n"
            "Tuesday 2pm design review with the team\n"
            "Wednesday lunch with Sam at the cafe downtown\n"
            "Thursday evening gym session then groceries\n"
            "Friday deploy the release and celebrate after")
    assert len(good.strip()) >= 120  # guard: must be non-weak by length
    _fake_pytesseract(monkeypatch, good)

    called = []

    def vision_fn(path):
        called.append(path)
        return "SHOULD NOT BE USED " * 20

    text, meta = fp._extract_image(img, vision_fn=vision_fn,
                                   vision_min_chars=120)

    assert called == []
    assert text == good.strip()
    assert meta["extractor"] == "tesseract_ocr"
    assert meta["vision_used"] is False


def test_no_vision_fn_is_unchanged_behavior(tmp_path, monkeypatch):
    img = _make_png(tmp_path)
    _fake_pytesseract(monkeypatch, "x")  # weak, but no vision_fn to escalate to

    text, meta = fp._extract_image(img)  # vision_fn defaults to None

    assert text == "x"
    assert meta["extractor"] == "tesseract_ocr"
    assert meta["vision_used"] is False
    assert "tesseract_chars" not in meta


def test_primary_mode_high_min_chars_forces_escalation(tmp_path, monkeypatch):
    """A huge vision_min_chars ('primary' mode) escalates even rich OCR."""
    img = _make_png(tmp_path)
    good = "This is perfectly clean OCR text that is well over the default."
    _fake_pytesseract(monkeypatch, good)

    vlm = good + " Plus extra detail only the vision model saw."

    text, meta = fp._extract_image(img, vision_fn=lambda p: vlm,
                                   vision_min_chars=10_000_000)

    assert text == vlm
    assert meta["vision_used"] is True


def test_vision_fn_failure_falls_back_to_ocr(tmp_path, monkeypatch):
    img = _make_png(tmp_path)
    _fake_pytesseract(monkeypatch, "x")

    def vision_fn(path):
        raise RuntimeError("ollama down")

    text, meta = fp._extract_image(img, vision_fn=vision_fn)

    assert text == "x"  # kept the OCR text; no raise
    assert meta["extractor"] == "tesseract_ocr"
    assert meta["vision_used"] is False


def test_vision_not_richer_keeps_ocr(tmp_path, monkeypatch):
    img = _make_png(tmp_path)
    _fake_pytesseract(monkeypatch, "x")

    # weak OCR triggers escalation, but VLM returns nothing longer -> keep OCR
    text, meta = fp._extract_image(img, vision_fn=lambda p: "")

    assert text == "x"
    assert meta["vision_used"] is False


class TestWeakOcrHeuristic:
    def test_short_is_weak(self):
        assert fp._ocr_text_is_weak("hi", 120) is True

    def test_long_clean_prose_is_not_weak(self):
        s = "The quick brown fox jumps over the lazy dog. " * 5
        assert fp._ocr_text_is_weak(s, 120) is False

    def test_garbage_symbols_are_weak(self):
        s = "|_ ~ \\ // '' `` |||  ---  ~~~  \\\\  //// " * 5
        assert fp._ocr_text_is_weak(s, 5) is True

    def test_empty_is_weak(self):
        assert fp._ocr_text_is_weak("", 120) is True
        assert fp._ocr_text_is_weak("   \n  ", 120) is True
