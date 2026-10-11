"""
Tests for puffpai.vision — local Ollama vision-model image transcription.

The network is fully stubbed (monkeypatched requests.post); no real Ollama is
contacted. We assert the request payload shape and the never-raise contract.
"""

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

pytest.importorskip("PIL")

from puffpai import vision  # noqa: E402


def _make_image(tmp_path, size=(64, 32)):
    from PIL import Image
    p = tmp_path / "sample.png"
    Image.new("RGB", size, (200, 200, 200)).save(p)
    return str(p)


class _FakeResp:
    def __init__(self, status_code=200, payload=None, raise_exc=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._raise_exc = raise_exc

    def json(self):
        if self._raise_exc:
            raise self._raise_exc
        return self._payload


def test_success_returns_text_and_payload_shape(tmp_path, monkeypatch):
    img = _make_image(tmp_path)
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp(200, {"message": {"content": "  Milk\nEggs  "}})

    monkeypatch.setattr(vision.requests, "post", fake_post)

    text, meta = vision.extract_text_from_image(
        img, ollama_url="http://x:11434", model="qwen2.5vl:7b", timeout=42)

    # Return contract
    assert text == "Milk\nEggs"
    assert meta == {"extractor": "qwen_vl", "model": "qwen2.5vl:7b"}

    # Endpoint + timeout
    assert captured["url"] == "http://x:11434/api/chat"
    assert captured["timeout"] == 42

    # Payload shape
    body = captured["json"]
    assert body["model"] == "qwen2.5vl:7b"
    assert body["stream"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"]["temperature"] == 0.1
    msg = body["messages"][0]
    assert msg["role"] == "user"
    assert "transcribe" in msg["content"].lower()
    # A base64 image is present with NO data: URL prefix, and it decodes.
    assert len(msg["images"]) == 1
    b64 = msg["images"][0]
    assert not b64.startswith("data:")
    assert base64.b64decode(b64)  # valid base64, non-empty


def test_non_200_returns_empty_without_raising(tmp_path, monkeypatch):
    img = _make_image(tmp_path)
    monkeypatch.setattr(
        vision.requests, "post",
        lambda *a, **k: _FakeResp(500, {}))

    text, meta = vision.extract_text_from_image(
        img, ollama_url="http://x:11434", model="qwen2.5vl:7b")

    assert text == ""
    assert meta["extractor"] == "qwen_vl"
    assert "500" in meta["error"]


def test_timeout_returns_empty_without_raising(tmp_path, monkeypatch):
    img = _make_image(tmp_path)

    def boom(*a, **k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(vision.requests, "post", boom)

    text, meta = vision.extract_text_from_image(
        img, ollama_url="http://x:11434", model="qwen2.5vl:7b")

    assert text == ""
    assert meta["extractor"] == "qwen_vl"
    assert "timed out" in meta["error"]


def test_malformed_json_returns_empty_without_raising(tmp_path, monkeypatch):
    img = _make_image(tmp_path)
    monkeypatch.setattr(
        vision.requests, "post",
        lambda *a, **k: _FakeResp(200, raise_exc=ValueError("no json")))

    text, meta = vision.extract_text_from_image(
        img, ollama_url="http://x:11434", model="qwen2.5vl:7b")

    assert text == ""
    assert "error" in meta


def test_large_image_is_downscaled(tmp_path, monkeypatch):
    """Longest side is capped so the encoded upload stays bounded."""
    from PIL import Image
    big = tmp_path / "big.png"
    Image.new("RGB", (4000, 2000), (10, 20, 30)).save(big)

    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["json"] = json
        return _FakeResp(200, {"message": {"content": "ok"}})

    monkeypatch.setattr(vision.requests, "post", fake_post)
    vision.extract_text_from_image(
        str(big), ollama_url="http://x:11434", model="m")

    raw = base64.b64decode(captured["json"]["messages"][0]["images"][0])
    dec = Image.open(__import__("io").BytesIO(raw))
    assert max(dec.size) <= vision._MAX_SIDE
