"""
Ollama LLM Backend for PuffPAI (Development)

Wraps Ollama's HTTP API for local LLM inference.
Used during development on Windows/Linux with Ollama installed.
"""

from __future__ import annotations

import json
import requests

from ..config import DEFAULT_OLLAMA_URL
from .base import LLMBackend


class OllamaBackend(LLMBackend):
    """Ollama HTTP backend for development use.

    Requires a running Ollama server (default: puffpai.config.DEFAULT_OLLAMA_URL,
    i.e. the stock Ollama port 11434). Callers normally pass cfg.ollama.url.
    """

    def __init__(self, url: str = DEFAULT_OLLAMA_URL,
                 model: str = "qwen2.5:7b-instruct",
                 embed_model: str | None = None,
                 num_ctx: int = 2048,
                 num_predict: int = 256,
                 timeout: int = 30,
                 keep_alive: int | str | None = None):
        self._url = url.rstrip('/')
        self._model = model
        self._embed_model = embed_model or model
        self._num_ctx = num_ctx
        self._num_predict = num_predict
        self._timeout = timeout
        #: Passed on every request; a per-request keep_alive overrides the
        #: server's OLLAMA_KEEP_ALIVE for that model. -1 (JSON number, not the
        #: string "-1" — Go's duration parser rejects it) pins the model in
        #: memory; the appliance config uses that so chat never pays a reload.
        #: ``None`` (the default) resolves to that configured pin rather than
        #: to a local "30m" literal, so a bare construction in a helper script
        #: cannot quietly un-pin a model the appliance wants resident.
        from puffpai.inference import model_options as _model_options
        self._keep_alive = (_model_options.keep_alive()
                            if keep_alive is None else keep_alive)
        #: Whether this model accepts ``think`` (see _build_payload). Flipped
        #: off for good the first time a model rejects the field, so a
        #: non-thinking model costs one failed round-trip per process, not one
        #: per request.
        self._think_ok = True

    def _build_payload(self, messages: list, temperature: float,
                       max_tokens: int, stream: bool) -> dict:
        """Build a native /api/chat payload.

        Uses the native endpoint, not /v1/chat/completions: the
        OpenAI-compat endpoint silently ignores the "options" field, so
        num_ctx/num_predict would never be applied.
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": self._num_ctx,
                "num_predict": min(max_tokens, self._num_predict),
            },
        }
        # Suppress the reasoning block. Measured on the appliance:
        # gpt-oss:20b spent 1380 characters and HALF its wall clock thinking
        # before a two-sentence answer — 26.7s → 13.5s with this set, same
        # answer quality. On a hybrid model it matters even more than latency:
        # qwen3:30b-a3b with thinking on spent the whole num_predict budget
        # reasoning and returned an EMPTY reply, which is a silent failure the
        # chat path has no way to distinguish from a bad answer.
        # Models without thinking support reject the field; _post retries once
        # without it and remembers (see _think_ok).
        if self._think_ok:
            payload["think"] = False
        return payload

    def _post_chat(self, payload: dict):
        """POST /api/chat, retrying once without ``think`` if unsupported."""
        response = requests.post(f"{self._url}/api/chat", json=payload,
                                 timeout=self._timeout)
        if response.status_code >= 400 and "think" in payload:
            try:
                detail = json.dumps(response.json())
            except Exception:
                detail = response.text or ""
            if "think" in detail.lower():
                self._think_ok = False
                payload.pop("think")
                response = requests.post(f"{self._url}/api/chat", json=payload,
                                         timeout=self._timeout)
        return response

    def chat(self, messages: list, temperature: float = 0.7,
             max_tokens: int = 2048) -> str:
        """Send a chat request to Ollama's native /api/chat endpoint."""
        try:
            response = self._post_chat(
                self._build_payload(messages, temperature, max_tokens,
                                    stream=False))
            response.raise_for_status()
            data = response.json()
            return data["message"]["content"]
        except requests.exceptions.ConnectionError:
            return "[Error] Cannot connect to Ollama. Is it running?"
        except requests.exceptions.Timeout:
            return "[Error] Ollama request timed out."
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            return f"[Error] Unexpected Ollama response: {e}"
        except Exception as e:
            return f"[Error] Ollama error: {e}"

    def chat_stream(self, messages: list, temperature: float = 0.7,
                    max_tokens: int = 2048):
        """Stream tokens from Ollama as they arrive."""
        try:
            payload = self._build_payload(messages, temperature, max_tokens,
                                          stream=True)
            response = requests.post(f"{self._url}/api/chat", json=payload,
                                     timeout=self._timeout, stream=True)
            # Same one-shot ``think`` probe as chat(): a model that rejects the
            # field must not take the streaming path down with it.
            if response.status_code >= 400 and "think" in payload:
                self._think_ok = False
                payload.pop("think")
                response = requests.post(f"{self._url}/api/chat", json=payload,
                                         timeout=self._timeout, stream=True)
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8", errors="replace"))
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        yield token
                    if chunk.get("done"):
                        break
                except (ValueError, KeyError, IndexError):
                    continue
        except requests.exceptions.ConnectionError:
            yield "[Error] Cannot connect to Ollama. Is it running?"
        except requests.exceptions.Timeout:
            yield "[Error] Ollama request timed out."
        except Exception as e:
            yield f"[Error] Ollama error: {e}"

    def embed(self, text: str) -> list:
        """Generate embedding via Ollama's embedding endpoint."""
        try:
            # Per-model-role options (model_options): the embedder's
            # own trained window, no num_predict, plus the resident pin. The
            # chat num_ctx (8192) is larger than nomic-embed-text's 2048 and
            # would make ollama WARN and re-plan the runner.
            from puffpai.inference import model_options as _mo
            payload = {"model": self._embed_model, "prompt": text}
            payload.update(_mo.embedding_payload(self._embed_model))
            response = requests.post(
                f"{self._url}/api/embeddings",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            return response.json().get("embedding", [])
        except Exception:
            return []

    def is_available(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            response = requests.get(f"{self._url}/api/tags", timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    @property
    def model_name(self) -> str:
        return self._model
