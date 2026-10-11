"""
Conditional voice model tests.

These tests only run when real voice models are present on disk.
Download models with: python scripts/download_models.py --vosk --piper

Skipped automatically when models are absent (CI-safe).
"""

from __future__ import annotations

import os
import struct
import pytest

# ── Model paths ──────────────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(REPO_ROOT, "models")

VOSK_MODEL_DIR = os.path.join(MODELS_DIR, "vosk-model-small-en-us")
PIPER_MODEL_PATH = os.path.join(MODELS_DIR, "en_US-lessac-medium.onnx")

_HAS_VOSK_MODEL = os.path.isdir(VOSK_MODEL_DIR) and os.listdir(VOSK_MODEL_DIR) != []
_HAS_PIPER_MODEL = os.path.isfile(PIPER_MODEL_PATH)

try:
    import vosk
    _HAS_VOSK = True
except ImportError:
    _HAS_VOSK = False

try:
    import piper
    _HAS_PIPER = True
except ImportError:
    _HAS_PIPER = False


# ── Vosk STT Tests ───────────────────────────────────────────────────────────

@pytest.mark.skipif(
    not (_HAS_VOSK and _HAS_VOSK_MODEL),
    reason="Vosk library or model not available",
)
class TestVoskReal:
    """Real Vosk model tests (conditional)."""

    def test_vosk_model_loads(self):
        """Vosk model directory loads successfully."""
        model = vosk.Model(VOSK_MODEL_DIR)
        assert model is not None

    def test_vosk_recognizer_creates(self):
        """A recognizer can be created with the loaded model."""
        model = vosk.Model(VOSK_MODEL_DIR)
        rec = vosk.KaldiRecognizer(model, 16000)
        assert rec is not None

    def test_vosk_silence_transcription(self):
        """Transcribing silence produces empty or minimal result."""
        model = vosk.Model(VOSK_MODEL_DIR)
        rec = vosk.KaldiRecognizer(model, 16000)

        # Feed 1 second of silence (16-bit PCM, 16kHz)
        silence = b'\x00\x00' * 16000
        rec.AcceptWaveform(silence)
        result = rec.FinalResult()

        # Should be valid JSON with empty or minimal text
        import json
        parsed = json.loads(result)
        assert "text" in parsed
        # Silence should produce empty or near-empty transcription
        assert len(parsed["text"]) < 50


# ── Piper TTS Tests ──────────────────────────────────────────────────────────

@pytest.mark.skipif(
    not (_HAS_PIPER and _HAS_PIPER_MODEL),
    reason="Piper library or model not available",
)
class TestPiperReal:
    """Real Piper model tests (conditional)."""

    def test_piper_generates_audio(self):
        """Piper model produces audio bytes for a short phrase."""
        from piper import PiperVoice

        voice = PiperVoice.load(PIPER_MODEL_PATH)
        # synthesize_stream_raw returns a generator of audio chunks
        audio_chunks = list(voice.synthesize_stream_raw("Hello"))
        assert len(audio_chunks) > 0
        total_bytes = sum(len(c) for c in audio_chunks)
        assert total_bytes > 100  # should produce substantial audio


# ── Download Script Tests (always run) ───────────────────────────────────────

class TestDownloadScript:
    """Test the download script's helper functions and structure."""

    def test_script_is_importable(self):
        """download_models.py can be imported without side effects."""
        import importlib.util
        script_path = os.path.join(REPO_ROOT, "scripts", "download_models.py")
        spec = importlib.util.spec_from_file_location("download_models", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "download_vosk")
        assert hasattr(mod, "download_piper")
        assert hasattr(mod, "download_llm")
        assert hasattr(mod, "main")

    def test_models_dir_is_correct(self):
        """MODELS_DIR points to the repo's models/ directory."""
        import importlib.util
        script_path = os.path.join(REPO_ROOT, "scripts", "download_models.py")
        spec = importlib.util.spec_from_file_location("download_models", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        expected = os.path.join(REPO_ROOT, "models")
        assert mod.MODELS_DIR == expected

    def test_model_urls_are_https(self):
        """All download URLs use HTTPS."""
        import importlib.util
        script_path = os.path.join(REPO_ROOT, "scripts", "download_models.py")
        spec = importlib.util.spec_from_file_location("download_models", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for model_def in [mod.VOSK_MODEL, mod.PIPER_MODEL, mod.PIPER_CONFIG, mod.LLM_MODEL]:
            assert model_def["url"].startswith("https://"), \
                f"URL not HTTPS: {model_def['url']}"

    def test_download_file_skips_existing(self, tmp_path):
        """_download_file returns True and skips if file already exists."""
        import importlib.util
        script_path = os.path.join(REPO_ROOT, "scripts", "download_models.py")
        spec = importlib.util.spec_from_file_location("download_models", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        existing = os.path.join(str(tmp_path), "existing.bin")
        with open(existing, 'wb') as f:
            f.write(b"data")

        result = mod._download_file(
            "https://example.com/fake", existing, "test file")
        assert result is True  # skipped, returned True
