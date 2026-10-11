"""Tests for the STT confidence gate + config-driven engine selection.

These cover the pure scoring logic (no faster-whisper/vosk needed), the Vosk
word-confidence path, WhisperSTT graceful-degrade, and the create_stt factory
fallback chain. The heavy engines are never imported here — everything is either
a pure function or a mock, so it runs on any dev box.
"""

from unittest.mock import patch, MagicMock

from puffpai.voice.stt import (
    STTEngine, StubSTT, VoskSTT, WhisperSTT,
    TranscriptionResult, evaluate_whisper, evaluate_vosk, create_stt,
)


# ── TranscriptionResult ─────────────────────────────────────────────────────
class TestTranscriptionResult:
    def test_defaults(self):
        r = TranscriptionResult()
        assert r.text == "" and r.low_confidence is False and r.n_segments == 0

    def test_truthiness_follows_text(self):
        assert bool(TranscriptionResult(text="hello")) is True
        assert bool(TranscriptionResult(text="   ")) is False


# ── evaluate_whisper (the whisper gate) ─────────────────────────────────────
class TestEvaluateWhisper:
    KW = dict(min_avg_logprob=-1.0, max_no_speech_prob=0.6)

    def test_real_speech_passes(self):
        # Measured real-speech range on-box: logprob ≈ -0.35, no_speech ≈ 0.01.
        r = evaluate_whisper("what's the weather like today",
                             [-0.35], [0.01], **self.KW)
        assert r.low_confidence is False
        assert r.text == "what's the weather like today"
        assert r.avg_logprob == -0.35 and r.no_speech_prob == 0.01

    def test_low_logprob_flagged(self):
        r = evaluate_whisper("in 37", [-1.4], [0.05], **self.KW)
        assert r.low_confidence is True
        assert "avg_logprob" in r.reason

    def test_high_no_speech_flagged(self):
        r = evaluate_whisper("uh", [-0.5], [0.9], **self.KW)
        assert r.low_confidence is True
        assert "no_speech_prob" in r.reason

    def test_empty_flagged(self):
        r = evaluate_whisper("", [], [], **self.KW)
        assert r.low_confidence is True and "empty" in r.reason

    def test_multi_segment_uses_mean_logprob_and_max_no_speech(self):
        # mean(-0.4,-0.6) = -0.5 (ok); max(0.1,0.2)=0.2 (ok) → not flagged
        r = evaluate_whisper("hello there friend", [-0.4, -0.6], [0.1, 0.2], **self.KW)
        assert r.low_confidence is False
        assert r.avg_logprob == -0.5 and r.no_speech_prob == 0.2
        assert r.n_segments == 2

    def test_threshold_is_configurable(self):
        # A stricter bar flags speech that the default would accept.
        r = evaluate_whisper("play some music", [-0.7], [0.01],
                             min_avg_logprob=-0.5, max_no_speech_prob=0.6)
        assert r.low_confidence is True


# ── evaluate_vosk (the vosk gate) ───────────────────────────────────────────
class TestEvaluateVosk:
    def test_confident_words_pass(self):
        r = evaluate_vosk("turn off the lights", [0.98, 0.95, 0.99, 0.92],
                          min_word_confidence=0.5)
        assert r.low_confidence is False
        assert r.word_confidence == 0.96

    def test_low_conf_flagged(self):
        r = evaluate_vosk("the a", [0.2, 0.3], min_word_confidence=0.5)
        assert r.low_confidence is True and "word_conf" in r.reason

    def test_empty_flagged(self):
        r = evaluate_vosk("", [], min_word_confidence=0.5)
        assert r.low_confidence is True and "empty" in r.reason


# ── VoskSTT confidence path (backward-compatible) ───────────────────────────
class TestVoskConfidencePath:
    def _mock_vosk(self, final_json):
        rec = MagicMock()
        rec.FinalResult.return_value = final_json
        v = MagicMock()
        v.Model.return_value = MagicMock()
        v.KaldiRecognizer.return_value = rec
        return v

    def test_transcribe_result_reads_word_conf(self, tmp_path):
        model_dir = tmp_path / "vosk-model"; model_dir.mkdir()
        final = '{"text": "the a", "result": [{"word":"the","conf":0.2},{"word":"a","conf":0.3}]}'
        with patch('puffpai.voice.stt._HAS_VOSK', True), \
             patch('puffpai.voice.stt.vosk', self._mock_vosk(final)):
            stt = VoskSTT(str(model_dir), min_word_confidence=0.5)
            res = stt.transcribe_result(b"\x00" * 3200)
            assert res.text == "the a"
            assert res.low_confidence is True
            assert res.word_confidence == 0.25
            # transcribe() still returns a bare string (backward compat)
            assert stt.transcribe(b"\x00" * 3200) == "the a"

    def test_confident_transcript_not_flagged(self, tmp_path):
        model_dir = tmp_path / "vosk-model"; model_dir.mkdir()
        final = '{"text": "turn on the lights", "result": [{"word":"turn","conf":0.99},{"word":"on","conf":0.98},{"word":"the","conf":0.97},{"word":"lights","conf":0.99}]}'
        with patch('puffpai.voice.stt._HAS_VOSK', True), \
             patch('puffpai.voice.stt.vosk', self._mock_vosk(final)):
            stt = VoskSTT(str(model_dir))
            res = stt.transcribe_result(b"\x00" * 3200)
            assert res.low_confidence is False and res.text == "turn on the lights"


# ── WhisperSTT graceful degrade ─────────────────────────────────────────────
class TestWhisperGraceful:
    def test_unavailable_without_faster_whisper(self):
        with patch('puffpai.voice.stt._HAS_WHISPER', False), \
             patch('puffpai.voice.stt._WhisperModel', None), \
             patch('puffpai.voice.stt._stt_config', return_value={}):
            stt = WhisperSTT()
            assert stt.is_available() is False
            assert stt.transcribe(b"\x00" * 100) == ""
            r = stt.transcribe_result(b"\x00" * 100)
            assert r.low_confidence is True

    def test_is_subclass(self):
        assert issubclass(WhisperSTT, STTEngine)


# ── create_stt factory fallback chain ───────────────────────────────────────
class TestCreateStt:
    def test_returns_whisper_when_available(self):
        fake = MagicMock(); fake.is_available.return_value = True
        with patch('puffpai.voice.stt._stt_config', return_value={"engine": "whisper"}), \
             patch('puffpai.voice.stt.WhisperSTT', return_value=fake) as W:
            got = create_stt()
            assert got is fake and W.called

    def test_falls_back_to_vosk_when_whisper_unavailable(self):
        bad = MagicMock(); bad.is_available.return_value = False
        good = MagicMock(); good.is_available.return_value = True
        with patch('puffpai.voice.stt._stt_config', return_value={"engine": "whisper", "vosk_model_dir": "/m"}), \
             patch('puffpai.voice.stt.WhisperSTT', return_value=bad), \
             patch('puffpai.voice.stt.VoskSTT', return_value=good) as V:
            got = create_stt()
            assert got is good
            V.assert_called_once_with(model_path="/m")

    def test_falls_back_to_stub_when_all_unavailable(self):
        bad = MagicMock(); bad.is_available.return_value = False
        with patch('puffpai.voice.stt._stt_config', return_value={"engine": "whisper"}), \
             patch('puffpai.voice.stt.WhisperSTT', return_value=bad), \
             patch('puffpai.voice.stt.VoskSTT', return_value=bad):
            got = create_stt()
            assert isinstance(got, StubSTT)

    def test_explicit_vosk_engine_skips_whisper(self):
        good = MagicMock(); good.is_available.return_value = True
        with patch('puffpai.voice.stt._stt_config', return_value={"vosk_model_dir": "/m"}), \
             patch('puffpai.voice.stt.WhisperSTT') as W, \
             patch('puffpai.voice.stt.VoskSTT', return_value=good):
            got = create_stt(engine="vosk")
            assert got is good and not W.called


# ── Default transcribe_result on a no-confidence engine ─────────────────────
class TestDefaultResult:
    def test_stub_default_result_flags_empty(self):
        r = StubSTT().transcribe_result(b"\x00" * 100)
        assert r.text == "" and r.low_confidence is True
