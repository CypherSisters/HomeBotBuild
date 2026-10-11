"""Tests for LlamaCppBackend — mocked llama-cpp-python."""

import os
import pytest
from unittest.mock import patch, MagicMock

from puffpai.inference.base import LLMBackend


# ---------------------------------------------------------------------------
# Helpers — mock the llama_cpp module so tests run without the real library
# ---------------------------------------------------------------------------

def _make_mock_llama():
    """Create a mock Llama class that returns canned responses."""
    mock_llm = MagicMock()
    mock_llm.create_chat_completion.return_value = {
        "choices": [{"message": {"content": "Hello from llama.cpp!"}}]
    }
    mock_llm.create_embedding.return_value = {
        "data": [{"embedding": [0.1, 0.2, 0.3]}]
    }
    return mock_llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLlamaCppImport:
    """Test graceful handling when llama-cpp-python is not installed."""

    def test_import_succeeds(self):
        """Module imports without error even if llama_cpp missing."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        assert LlamaCppBackend is not None

    def test_is_llm_backend_subclass(self):
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        assert issubclass(LlamaCppBackend, LLMBackend)


class TestLlamaCppNoLibrary:
    """Tests when llama-cpp-python is NOT installed."""

    def test_not_available_without_library(self, tmp_path):
        """Backend reports unavailable when library missing."""
        model_file = tmp_path / "test.gguf"
        model_file.write_bytes(b"fake")

        with patch('puffpai.inference.llamacpp_backend._HAS_LLAMA_CPP', False):
            from puffpai.inference.llamacpp_backend import LlamaCppBackend
            backend = LlamaCppBackend(str(model_file))
            assert backend.is_available() is False

    def test_chat_returns_error_without_library(self, tmp_path):
        model_file = tmp_path / "test.gguf"
        model_file.write_bytes(b"fake")

        with patch('puffpai.inference.llamacpp_backend._HAS_LLAMA_CPP', False):
            from puffpai.inference.llamacpp_backend import LlamaCppBackend
            backend = LlamaCppBackend(str(model_file))
            result = backend.chat([{"role": "user", "content": "hi"}])
            assert "[Error]" in result
            assert "not installed" in result

    def test_embed_returns_empty_without_library(self, tmp_path):
        model_file = tmp_path / "test.gguf"
        model_file.write_bytes(b"fake")

        with patch('puffpai.inference.llamacpp_backend._HAS_LLAMA_CPP', False):
            from puffpai.inference.llamacpp_backend import LlamaCppBackend
            backend = LlamaCppBackend(str(model_file))
            assert backend.embed("hello") == []


class TestLlamaCppMissingModel:
    """Tests when library is available but model file doesn't exist."""

    def test_not_available_missing_model(self):
        with patch('puffpai.inference.llamacpp_backend._HAS_LLAMA_CPP', True):
            from puffpai.inference.llamacpp_backend import LlamaCppBackend
            backend = LlamaCppBackend("/nonexistent/model.gguf")
            assert backend.is_available() is False

    def test_chat_error_missing_model(self):
        with patch('puffpai.inference.llamacpp_backend._HAS_LLAMA_CPP', True):
            from puffpai.inference.llamacpp_backend import LlamaCppBackend
            backend = LlamaCppBackend("/nonexistent/model.gguf")
            result = backend.chat([{"role": "user", "content": "hi"}])
            assert "[Error]" in result
            assert "not found" in result


class TestLlamaCppWithMockedLibrary:
    """Tests with mocked llama_cpp.Llama — simulates loaded model."""

    def _make_backend(self, tmp_path, mock_llm=None):
        """Helper to create a backend with mocked Llama."""
        model_file = tmp_path / "test.gguf"
        model_file.write_bytes(b"fake-gguf")

        if mock_llm is None:
            mock_llm = _make_mock_llama()

        with patch('puffpai.inference.llamacpp_backend._HAS_LLAMA_CPP', True), \
             patch('puffpai.inference.llamacpp_backend.Llama', return_value=mock_llm):
            from puffpai.inference.llamacpp_backend import LlamaCppBackend
            backend = LlamaCppBackend(str(model_file), n_ctx=4096, n_gpu_layers=8)
        return backend, mock_llm

    def test_is_available(self, tmp_path):
        backend, _ = self._make_backend(tmp_path)
        assert backend.is_available() is True

    def test_model_name_returns_filename(self, tmp_path):
        backend, _ = self._make_backend(tmp_path)
        assert backend.model_name == "test.gguf"

    def test_chat_returns_response(self, tmp_path):
        backend, _ = self._make_backend(tmp_path)
        result = backend.chat([{"role": "user", "content": "hello"}])
        assert result == "Hello from llama.cpp!"

    def test_chat_passes_parameters(self, tmp_path):
        backend, mock_llm = self._make_backend(tmp_path)
        messages = [{"role": "user", "content": "test"}]
        backend.chat(messages, temperature=0.5, max_tokens=512)

        mock_llm.create_chat_completion.assert_called_once_with(
            messages=messages, temperature=0.5, max_tokens=512,
        )

    def test_chat_handles_exception(self, tmp_path):
        mock_llm = _make_mock_llama()
        mock_llm.create_chat_completion.side_effect = RuntimeError("OOM")
        backend, _ = self._make_backend(tmp_path, mock_llm)

        result = backend.chat([{"role": "user", "content": "hi"}])
        assert "[Error]" in result
        assert "OOM" in result

    def test_embed_returns_vector(self, tmp_path):
        backend, _ = self._make_backend(tmp_path)
        result = backend.embed("hello world")
        assert result == [0.1, 0.2, 0.3]

    def test_embed_handles_exception(self, tmp_path):
        mock_llm = _make_mock_llama()
        mock_llm.create_embedding.side_effect = RuntimeError("embed error")
        backend, _ = self._make_backend(tmp_path, mock_llm)

        result = backend.embed("hello")
        assert result == []

    def test_embed_handles_empty_data(self, tmp_path):
        mock_llm = _make_mock_llama()
        mock_llm.create_embedding.return_value = {"data": []}
        backend, _ = self._make_backend(tmp_path, mock_llm)

        result = backend.embed("hello")
        assert result == []
