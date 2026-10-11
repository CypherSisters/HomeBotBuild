"""Tests for voice/ pipeline — wake word, STT, TTS, pipeline.

VoskSTT, PiperTTS, audio capture and threading.
"""

import struct
import pytest
from unittest.mock import patch, MagicMock

from puffpai.voice.wake_word import WakeWordDetector
from puffpai.voice.stt import STTEngine, StubSTT
from puffpai.voice.tts import TTSEngine, StubTTS
from puffpai.voice.pipeline import VoicePipeline, compute_rms_energy


# ── Wake Word ─────────────────────────────────────────────────────────────

class TestWakeWordDetector:
    def test_model_file_graceful_missing(self, tmp_path):
        """Detector should not crash when model file doesn't exist."""
        det = WakeWordDetector(model_path=tmp_path / "nonexistent.tflite")
        assert det.is_loaded is False

    def test_detect_returns_false_when_not_loaded(self, tmp_path):
        det = WakeWordDetector(model_path=tmp_path / "missing.tflite")
        assert det.detect(b"\x00" * 1600) is False

    def test_threshold_default(self):
        det = WakeWordDetector(model_path="/tmp/nonexistent.tflite")
        assert det.threshold == 0.5


# ── StubSTT ───────────────────────────────────────────────────────────────

class TestStubSTT:
    def test_transcribe_returns_empty(self):
        stt = StubSTT()
        assert stt.transcribe(b"\x00" * 100) == ""

    def test_not_available(self):
        stt = StubSTT()
        assert stt.is_available() is False


# ── VoskSTT ───────────────────────────────────────────────────────────────

class TestVoskSTT:
    def test_import_without_vosk(self):
        """VoskSTT can be imported even if vosk is not installed."""
        from puffpai.voice.stt import VoskSTT
        assert issubclass(VoskSTT, STTEngine)

    def test_not_available_without_vosk(self):
        with patch('puffpai.voice.stt._HAS_VOSK', False):
            from puffpai.voice.stt import VoskSTT
            stt = VoskSTT("/nonexistent/model")
            assert stt.is_available() is False

    def test_not_available_missing_model(self):
        with patch('puffpai.voice.stt._HAS_VOSK', True):
            from puffpai.voice.stt import VoskSTT
            stt = VoskSTT("/nonexistent/model")
            assert stt.is_available() is False

    def test_transcribe_returns_empty_when_unavailable(self):
        with patch('puffpai.voice.stt._HAS_VOSK', False):
            from puffpai.voice.stt import VoskSTT
            stt = VoskSTT("/nonexistent")
            assert stt.transcribe(b"\x00" * 100) == ""

    def test_transcribe_with_mocked_vosk(self, tmp_path):
        model_dir = tmp_path / "vosk-model"
        model_dir.mkdir()

        mock_model = MagicMock()
        mock_recognizer = MagicMock()
        mock_recognizer.FinalResult.return_value = '{"text": "hello world"}'

        mock_vosk = MagicMock()
        mock_vosk.Model.return_value = mock_model
        mock_vosk.KaldiRecognizer.return_value = mock_recognizer

        with patch('puffpai.voice.stt._HAS_VOSK', True), \
             patch('puffpai.voice.stt.vosk', mock_vosk):
            from puffpai.voice.stt import VoskSTT
            stt = VoskSTT(str(model_dir))
            assert stt.is_available() is True
            result = stt.transcribe(b"\x00" * 3200)
            assert result == "hello world"

    def test_transcribe_exception_returns_empty(self, tmp_path):
        model_dir = tmp_path / "vosk-model"
        model_dir.mkdir()

        mock_model = MagicMock()
        mock_recognizer = MagicMock()
        mock_recognizer.FinalResult.side_effect = RuntimeError("decode error")

        mock_vosk = MagicMock()
        mock_vosk.Model.return_value = mock_model
        mock_vosk.KaldiRecognizer.return_value = mock_recognizer

        with patch('puffpai.voice.stt._HAS_VOSK', True), \
             patch('puffpai.voice.stt.vosk', mock_vosk):
            from puffpai.voice.stt import VoskSTT
            stt = VoskSTT(str(model_dir))
            assert stt.transcribe(b"\x00" * 100) == ""


# ── StubTTS ───────────────────────────────────────────────────────────────

class TestStubTTS:
    def test_speak_returns_none(self):
        tts = StubTTS()
        assert tts.speak("hello") is None

    def test_not_available(self):
        tts = StubTTS()
        assert tts.is_available() is False


# ── PiperTTS ──────────────────────────────────────────────────────────────

class TestPiperTTS:
    def test_import_without_piper(self):
        from puffpai.voice.tts import PiperTTS
        assert issubclass(PiperTTS, TTSEngine)

    def test_not_available_without_piper(self):
        with patch('puffpai.voice.tts._HAS_PIPER', False):
            from puffpai.voice.tts import PiperTTS
            tts = PiperTTS("/nonexistent/model.onnx")
            assert tts.is_available() is False

    def test_not_available_missing_model(self):
        with patch('puffpai.voice.tts._HAS_PIPER', True):
            from puffpai.voice.tts import PiperTTS
            tts = PiperTTS("/nonexistent/model.onnx")
            assert tts.is_available() is False

    def test_speak_returns_none_when_unavailable(self):
        with patch('puffpai.voice.tts._HAS_PIPER', False):
            from puffpai.voice.tts import PiperTTS
            tts = PiperTTS("/nonexistent")
            assert tts.speak("hello") is None

    def test_speak_with_mocked_piper(self, tmp_path):
        model_file = tmp_path / "voice.onnx"
        model_file.write_bytes(b"fake-onnx")

        mock_voice = MagicMock()
        mock_voice.synthesize_stream_raw.return_value = [b"\x00" * 100, b"\x01" * 50]

        mock_piper = MagicMock()
        mock_piper.PiperVoice.load.return_value = mock_voice

        with patch('puffpai.voice.tts._HAS_PIPER', True), \
             patch('puffpai.voice.tts.piper', mock_piper):
            from puffpai.voice.tts import PiperTTS
            tts = PiperTTS(str(model_file))
            assert tts.is_available() is True
            result = tts.speak("hello world")
            assert result == b"\x00" * 100 + b"\x01" * 50

    def test_speak_exception_returns_none(self, tmp_path):
        model_file = tmp_path / "voice.onnx"
        model_file.write_bytes(b"fake")

        mock_voice = MagicMock()
        mock_voice.synthesize_stream_raw.side_effect = RuntimeError("synth error")

        mock_piper = MagicMock()
        mock_piper.PiperVoice.load.return_value = mock_voice

        with patch('puffpai.voice.tts._HAS_PIPER', True), \
             patch('puffpai.voice.tts.piper', mock_piper):
            from puffpai.voice.tts import PiperTTS
            tts = PiperTTS(str(model_file))
            assert tts.speak("hello") is None


# ── RMS Energy / VAD ──────────────────────────────────────────────────────

class TestRMSEnergy:
    def test_silence_is_zero(self):
        silence = b"\x00" * 3200  # 1600 samples of 0
        assert compute_rms_energy(silence) == 0.0

    def test_loud_audio_high_energy(self):
        # Max amplitude 16-bit audio
        loud = struct.pack('<' + 'h' * 100, *([32767] * 100))
        energy = compute_rms_energy(loud)
        assert energy > 30000

    def test_empty_bytes_zero(self):
        assert compute_rms_energy(b"") == 0.0

    def test_single_byte_zero(self):
        assert compute_rms_energy(b"\x00") == 0.0


# ── VoicePipeline ─────────────────────────────────────────────────────────

class TestVoicePipeline:
    def test_pipeline_not_ready_with_stubs(self):
        pipe = VoicePipeline()
        assert pipe.is_ready is False

    def test_process_utterance_with_mock_stt(self):
        """Mock STT returns text → agent echoes → TTS speaks."""
        mock_stt = MagicMock()
        mock_stt.transcribe.return_value = "turn on the lights"
        mock_stt.is_available.return_value = True

        mock_tts = MagicMock()
        mock_tts.is_available.return_value = True

        agent_calls = []
        def mock_agent(text):
            agent_calls.append(text)
            return "Lights turned on!"

        pipe = VoicePipeline(stt=mock_stt, tts=mock_tts, agent_fn=mock_agent)
        assert pipe.is_ready is True

        result = pipe.process_utterance(b"\x00" * 100)
        assert result == "Lights turned on!"
        assert agent_calls == ["turn on the lights"]
        mock_tts.speak.assert_called_once_with("Lights turned on!")

    def test_process_empty_transcription_returns_none(self):
        mock_stt = MagicMock()
        mock_stt.transcribe.return_value = "  "

        pipe = VoicePipeline(stt=mock_stt)
        result = pipe.process_utterance(b"\x00" * 100)
        assert result is None

    def test_start_stop_without_sounddevice(self):
        """Pipeline starts/stops gracefully when sounddevice missing."""
        with patch('puffpai.voice.pipeline._HAS_SOUNDDEVICE', False):
            pipe = VoicePipeline()
            pipe.start()
            assert pipe._running is True
            pipe.stop()
            assert pipe._running is False

    def test_start_with_sounddevice_creates_thread(self):
        """Pipeline creates a daemon thread when sounddevice is available."""
        mock_sd = MagicMock()
        # Make sd.rec block briefly then return silence
        mock_sd.rec.return_value = MagicMock(tobytes=lambda: b"\x00" * 3200)

        with patch('puffpai.voice.pipeline._HAS_SOUNDDEVICE', True), \
             patch('puffpai.voice.pipeline.sd', mock_sd):
            pipe = VoicePipeline()
            pipe.start()
            assert pipe._running is True
            assert pipe._thread is not None
            assert pipe._thread.daemon is True
            pipe.stop()
            assert pipe._running is False

    def test_vad_threshold_configurable(self):
        pipe = VoicePipeline(vad_threshold=200)
        assert pipe.vad_threshold == 200


# ── Config-wired voice model paths ────────────────────────────────────────

class TestVoiceConfigPaths:
    """Verify voice modules read default paths from ModelConfig."""

    def test_vosk_stt_uses_config_path(self, tmp_path):
        """VoskSTT reads from config when no explicit path given."""
        expected = str(tmp_path / "custom_vosk")
        with patch('puffpai.config.get_voice_models',
                   return_value={"vosk_model_dir": expected}), \
             patch('puffpai.voice.stt._HAS_VOSK', False):
            from puffpai.voice.stt import VoskSTT
            stt = VoskSTT()  # No explicit path
            assert stt._model_path == expected

    def test_piper_tts_uses_config_path(self, tmp_path):
        """PiperTTS reads from config when no explicit path given."""
        expected = str(tmp_path / "custom_piper.onnx")
        with patch('puffpai.config.get_voice_models',
                   return_value={"piper_model_path": expected}), \
             patch('puffpai.voice.tts._HAS_PIPER', False):
            from puffpai.voice.tts import PiperTTS
            tts = PiperTTS()  # No explicit path
            assert tts._model_path == expected

    def test_wake_word_uses_config_path(self, tmp_path):
        """WakeWordDetector reads from config when no explicit path given."""
        expected = tmp_path / "custom_wakeword.tflite"
        with patch('puffpai.config.get_voice_models',
                   return_value={"wake_word_model_path": str(expected)}):
            det = WakeWordDetector()  # No explicit path
            assert det.model_path == expected

    def test_voice_explicit_path_overrides_config(self, tmp_path):
        """Explicit path always overrides config-derived path."""
        explicit = str(tmp_path / "my_model")
        with patch('puffpai.voice.stt._HAS_VOSK', False):
            from puffpai.voice.stt import VoskSTT
            stt = VoskSTT(model_path=explicit)
            assert stt._model_path == explicit
