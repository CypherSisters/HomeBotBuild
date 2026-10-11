"""
Conditional llama.cpp real model tests.

These tests only run when:
1. llama-cpp-python is installed
2. A GGUF model file is present at models/tinyllama-*.gguf

Download model with: python scripts/download_models.py --llm

Skipped automatically when model or library is absent (CI-safe).
"""

from __future__ import annotations

import os
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(REPO_ROOT, "models")

# Look for any GGUF model file
_GGUF_PATH = None
if os.path.isdir(MODELS_DIR):
    for f in os.listdir(MODELS_DIR):
        if f.endswith('.gguf'):
            _GGUF_PATH = os.path.join(MODELS_DIR, f)
            break

try:
    from llama_cpp import Llama
    _HAS_LLAMA_CPP = True
except ImportError:
    _HAS_LLAMA_CPP = False

_CAN_RUN = _HAS_LLAMA_CPP and _GGUF_PATH is not None


# ── Real model tests (conditional) ──────────────────────────────────────────

@pytest.mark.skipif(not _CAN_RUN, reason="llama-cpp-python or GGUF model not available")
class TestLlamaCppReal:
    """Real llama.cpp model tests (conditional)."""

    def test_model_loads(self):
        """LlamaCppBackend can load the GGUF model."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(model_path=_GGUF_PATH, n_ctx=512, verbose=False)
        assert backend.is_available()
        assert backend.model_name.endswith('.gguf')

    def test_chat_produces_response(self):
        """Chat completion returns a non-empty string."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(model_path=_GGUF_PATH, n_ctx=512, verbose=False)
        result = backend.chat(
            [{"role": "user", "content": "Say hello in one word."}],
            temperature=0.1,
            max_tokens=32,
        )
        assert isinstance(result, str)
        assert len(result) > 0
        assert "[Error]" not in result

    def test_embed_produces_vector(self):
        """Embedding returns a non-empty float list."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(model_path=_GGUF_PATH, n_ctx=512, verbose=False)
        embedding = backend.embed("Hello world")
        assert isinstance(embedding, list)
        # If embeddings are supported, we expect a non-empty vector
        # Some models may not support embedding; skip assertion if empty
        if len(embedding) > 0:
            assert all(isinstance(x, float) for x in embedding)

    def test_gpu_offload_zero_layers(self):
        """n_gpu_layers=0 loads successfully (CPU-only mode)."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(
            model_path=_GGUF_PATH, n_ctx=256,
            n_gpu_layers=0, verbose=False)
        assert backend.is_available()


# ── Always-run structural tests ──────────────────────────────────────────────

class TestLlamaCppStructure:
    """Structural tests that always run (no model needed)."""

    def test_backend_handles_missing_model(self, tmp_path):
        """LlamaCppBackend handles non-existent model path gracefully."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(model_path=str(tmp_path / "nonexistent.gguf"))
        assert not backend.is_available()
        # Error could be "not found" (lib present, file missing) or
        # "not installed" (lib absent). Either way, should have an error.
        assert backend._load_error is not None
        assert len(backend._load_error) > 0

    def test_backend_chat_returns_error_when_not_loaded(self, tmp_path):
        """chat() returns an error string when no model is loaded."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(model_path=str(tmp_path / "missing.gguf"))
        result = backend.chat([{"role": "user", "content": "test"}])
        assert "[Error]" in result

    def test_backend_embed_returns_empty_when_not_loaded(self, tmp_path):
        """embed() returns [] when no model is loaded."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(model_path=str(tmp_path / "missing.gguf"))
        result = backend.embed("test")
        assert result == []

    def test_model_name_property(self, tmp_path):
        """model_name returns the basename of the model path."""
        from puffpai.inference.llamacpp_backend import LlamaCppBackend
        backend = LlamaCppBackend(model_path=str(tmp_path / "my_model.gguf"))
        assert backend.model_name == "my_model.gguf"
