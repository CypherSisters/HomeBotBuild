"""Tests for VoicePipeline.process_single_test() and voice integration."""

import pytest
from unittest.mock import MagicMock

from puffpai.voice.pipeline import VoicePipeline
from puffpai.voice.stt import StubSTT
from puffpai.voice.tts import StubTTS


class TestProcessSingleTest:
    """VoicePipeline.process_single_test() method."""

    def test_with_text_input(self):
        """Providing text skips STT and goes straight to agent."""
        responses = []
        pipeline = VoicePipeline(
            agent_fn=lambda t: f"Reply: {t}",
            tts=StubTTS(),
        )

        result = pipeline.process_single_test(text="Hello PuffPAI")
        assert result['stt_ok'] is True
        assert result['transcription'] == "Hello PuffPAI"
        assert result['agent_ok'] is True
        assert result['response'] == "Reply: Hello PuffPAI"
        assert result['error'] is None

    def test_with_audio_input(self):
        """Providing audio runs through STT then agent."""
        mock_stt = MagicMock()
        mock_stt.transcribe.return_value = "test transcription"

        mock_tts = MagicMock()
        mock_tts.speak = MagicMock()

        pipeline = VoicePipeline(
            stt=mock_stt, tts=mock_tts,
            agent_fn=lambda t: f"Reply: {t}",
        )

        result = pipeline.process_single_test(audio=b'\x00\x00' * 100)
        assert result['stt_ok'] is True
        assert result['transcription'] == "test transcription"
        assert result['agent_ok'] is True
        assert result['response'] == "Reply: test transcription"
        assert result['tts_ok'] is True
        mock_tts.speak.assert_called_once()

    def test_empty_transcription_returns_error(self):
        """Empty STT result reports an error."""
        mock_stt = MagicMock()
        mock_stt.transcribe.return_value = ""

        pipeline = VoicePipeline(stt=mock_stt, tts=StubTTS())
        result = pipeline.process_single_test(audio=b'\x00\x00' * 100)

        assert result['stt_ok'] is False
        assert result['agent_ok'] is False
        assert result['error'] == 'Empty transcription'

    def test_no_input_returns_error(self):
        """Calling with no text or audio returns error."""
        pipeline = VoicePipeline()
        result = pipeline.process_single_test()
        assert result['error'] is not None
        assert 'Must provide' in result['error']

    def test_agent_exception_captured(self):
        """Agent exceptions are captured in the error field."""
        def failing_agent(text):
            raise RuntimeError("Agent crashed")

        pipeline = VoicePipeline(agent_fn=failing_agent, tts=StubTTS())
        result = pipeline.process_single_test(text="hello")
        assert result['error'] is not None
        assert 'Agent crashed' in result['error']
