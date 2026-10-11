"""Send to Puff: messages and voice memos into the ONE household thread.

The phone page posts typed text to /chat/send with ``surface: phone`` and a
voice memo to /chat/voice-memo. Both land in ``home-owner`` through the one
save point, under the chat rules (HA PIN hold, private turns). The memo is
transcribed on the box; here the transcriber is stubbed.

Pinned here:
* message route: household thread, ``phone`` tag, PIN hold, key required;
* memo route: key required, size and type limits, length cap, transcript
  stored as "Voice memo: <text>" and handled like typed text, a spoken PIN
  held and never echoed, the audio deleted after transcription.

Run: python -m pytest -q -p no:cacheprovider tests/test_send_to_puff_chat.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_ha_approval_scope import api, leaks  # noqa: E402,F401
from test_one_conversation import thread, brain, _home  # noqa: E402,F401

HOME = "home-owner"
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64


def _h(api):
    return {"X-API-Key": api._API_KEY}


@pytest.fixture
def stt(api, monkeypatch):
    """Stub the on-box transcriber; record the paths it was handed."""
    seen = {"paths": [], "text": "turn on the porch light", "seconds": 4.0}

    def fake(path):
        seen["paths"].append(path)
        assert os.path.exists(path)            # the upload reached the box
        return seen["text"], seen["seconds"]

    monkeypatch.setattr(api, "_transcribe_voice_memo", fake)
    return seen


def _memo(c, api, data=M4A, name="memo.m4a", ctype="audio/mp4", key=True):
    return c.post("/chat/voice-memo",
                  data={"file": (io.BytesIO(data), name, ctype)},
                  content_type="multipart/form-data",
                  headers=_h(api) if key else {})


# ── message route ───────────────────────────────────────────────────────────

def test_phone_message_lands_in_the_household_thread_tagged_phone(
        api, thread, brain):
    c = api.app.test_client()
    r = c.post("/chat/send", json={"message": "hello from my phone",
                                   "surface": "phone"}, headers=_h(api))
    assert r.status_code == 200
    body = r.get_json()
    assert body["conversation_id"] == HOME and body["private"] is False
    assert body["response"] == "typed: hello from my phone"
    msgs = _home(api)["messages"]
    assert [(m["role"], m["content"], m["surface"]) for m in msgs] == [
        ("user", "hello from my phone", "phone"),
        ("assistant", "typed: hello from my phone", "phone")]
    # The dashboard's thread poll sees it (what the Chat page and phone show).
    polled = c.get(f"/chat/history?c={HOME}", headers=_h(api)).get_json()
    assert [m["surface"] for m in polled["conversation"]["messages"]] == [
        "phone", "phone"]


def test_phone_message_pin_is_held_like_chat(api, leaks):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    c = api.app.test_client()
    r = c.post("/chat/send", json={"message": "4821", "surface": "phone"},
               headers=_h(api))
    assert r.status_code == 200 and r.get_json()["private"] is True
    assert leaks["applied"] == [(HOME, "4821")]
    assert leaks["saved"] == [] and leaks["llm"] == []


def test_phone_message_needs_the_key(api, leaks):
    c = api.app.test_client()
    r = c.post("/chat/send", json={"message": "hi", "surface": "phone"},
               environ_base={"REMOTE_ADDR": "192.168.1.77"})
    assert r.status_code == 401
    assert leaks["saved"] == [] and leaks["llm"] == []


def test_a_client_cannot_claim_a_server_surface(api):
    assert api._client_surface("phone") == "phone"
    assert api._client_surface("voice") == "chat"
    assert api._client_surface("ha") == "chat"


# ── memo route ──────────────────────────────────────────────────────────────

def test_memo_route_needs_the_key_and_is_not_public(api, stt):
    from puffpai.security import endpoint_gate
    assert "chat_voice_memo" not in api._PUBLIC_ENDPOINTS
    assert "chat_voice_memo" not in endpoint_gate.LOCAL_ONLY_ENDPOINTS
    c = api.app.test_client()
    r = c.post("/chat/voice-memo",
               data={"file": (io.BytesIO(M4A), "memo.m4a", "audio/mp4")},
               content_type="multipart/form-data",
               environ_base={"REMOTE_ADDR": "192.168.1.77"})
    assert r.status_code == 401
    assert stt["paths"] == []


def test_memo_transcript_goes_into_the_thread_as_a_phone_turn(
        api, thread, brain, stt):
    c = api.app.test_client()
    r = _memo(c, api)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["transcript"] == "turn on the porch light"
    assert body["response"] == "typed: turn on the porch light"
    assert body["conversation_id"] == HOME and body["private"] is False
    msgs = _home(api)["messages"]
    assert [(m["role"], m["content"], m["surface"]) for m in msgs] == [
        ("user", "Voice memo: turn on the porch light", "phone"),
        ("assistant", "typed: turn on the porch light", "phone")]
    # Puff acted on the words, not on the label.
    assert brain["chat"]  # the chat brain served it
    # The audio is gone once transcribed.
    assert stt["paths"] and not os.path.exists(stt["paths"][0])


def test_memo_accepts_common_phone_formats(api, thread, brain, stt):
    c = api.app.test_client()
    webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
    ogg = b"OggS" + b"\x00" * 64
    mp3 = b"ID3\x04" + b"\x00" * 64
    aac = b"\xff\xf1\x50\x80" + b"\x00" * 64
    for data, name, ctype in [
            (M4A, "a.m4a", "audio/x-m4a"), (M4A, "a.m4a", "audio/mp4"),
            (webm, "a.webm", "audio/webm;codecs=opus"), (ogg, "a.ogg", "audio/ogg"),
            (mp3, "a.mp3", "audio/mpeg"), (WAV, "a.wav", "audio/wav"),
            (aac, "a.aac", "audio/aac"),
            (M4A, "Recording.m4a", "application/octet-stream")]:
        r = _memo(c, api, data, name, ctype)
        assert r.status_code == 200, (name, ctype, r.get_json())


def test_memo_type_limits(api, thread, stt):
    c = api.app.test_client()
    # Declared type not audio.
    assert _memo(c, api, M4A, "x.m4a", "image/png").status_code == 415
    assert _memo(c, api, M4A, "x.sh", "text/x-shellscript").status_code == 415
    # Generic type with a non-audio extension.
    assert _memo(c, api, M4A, "x.exe",
                 "application/octet-stream").status_code == 415
    # Declared audio, but the bytes are not an audio container.
    r = _memo(c, api, b"#!/bin/sh\nrm -rf /\n" + b" " * 40, "x.m4a", "audio/mp4")
    assert r.status_code == 415
    r = _memo(c, api, b"MZ\x90\x00" + b"\x00" * 60, "x.wav", "audio/wav")
    assert r.status_code == 415
    # Undecodable audio is refused, not crashed on.
    def bad(path):
        raise ValueError("not decodable audio")
    api_trans = api._transcribe_voice_memo
    try:
        api._transcribe_voice_memo = bad
        assert _memo(c, api).status_code == 415
    finally:
        api._transcribe_voice_memo = api_trans
    # No file at all.
    r = c.post("/chat/voice-memo", data={}, content_type="multipart/form-data",
               headers=_h(api))
    assert r.status_code == 400
    assert _home(api) is None


def test_memo_size_and_length_limits(api, thread, stt, monkeypatch):
    c = api.app.test_client()
    big = M4A + b"\x00" * (api.VOICE_MEMO_MAX_BYTES + 128 * 1024)
    r = _memo(c, api, big)
    assert r.status_code == 413 and r.get_json()["max_mb"] == 10
    assert stt["paths"] == []
    # Just over the byte cap but under the header slack: the saved-file check.
    monkeypatch.setattr(api, "VOICE_MEMO_MAX_BYTES", 32)
    r = _memo(c, api, M4A)
    assert r.status_code == 413
    monkeypatch.setattr(api, "VOICE_MEMO_MAX_BYTES", 10 * 1024 * 1024)
    # Over 2 minutes.
    stt["text"], stt["seconds"] = None, 300.0
    r = _memo(c, api)
    assert r.status_code == 413 and r.get_json()["max_seconds"] == 120
    # Silence.
    stt["text"], stt["seconds"] = "", 3.0
    assert _memo(c, api).status_code == 422
    assert _home(api) is None
    # Every refused memo was deleted too.
    assert all(not os.path.exists(p) for p in stt["paths"])


def test_memo_stt_failure_is_honest(api, thread, monkeypatch):
    def boom(path):
        raise RuntimeError("model missing")
    monkeypatch.setattr(api, "_transcribe_voice_memo", boom)
    r = _memo(api.app.test_client(), api)
    assert r.status_code == 503
    assert _home(api) is None


def test_a_spoken_pin_in_a_memo_is_held_and_never_echoed(api, leaks, stt):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    stt["text"] = "4821"
    r = _memo(api.app.test_client(), api)
    assert r.status_code == 200
    body = r.get_json()
    assert body["private"] is True and body["transcript"] is None
    assert leaks["applied"] == [(HOME, "4821")]
    assert leaks["saved"] == [] and leaks["llm"] == []
    assert "4821" not in json.dumps(body)


def test_the_label_does_not_hide_a_pin_from_the_save_point(api, thread):
    api._ha_pin_slot_add("automation.puffpai_x", HOME,
                         {"entity_id": "automation.puffpai_x"})
    try:
        assert api._append_thread_turn(HOME, "owner", "phone", "4821", "ok",
                                       label=api.VOICE_MEMO_LABEL) is False
    finally:
        api._ha_pin_reset()
    assert api._append_thread_turn(HOME, "owner", "phone", "hello", "hi",
                                   label=api.VOICE_MEMO_LABEL) is True
    assert _home(api)["messages"][0]["content"] == "Voice memo: hello"


def test_memo_sniffer(api):
    sniff = api._voice_memo_sniff
    assert sniff(M4A) and sniff(WAV)
    assert sniff(b"OggS" + b"\x00" * 12) and sniff(b"\x1a\x45\xdf\xa3" + b"\x00" * 12)
    assert sniff(b"ID3\x03" + b"\x00" * 12) and sniff(b"\xff\xfb\x90" + b"\x00" * 13)
    assert not sniff(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert not sniff(b"%PDF-1.7" + b"\x00" * 8)
    assert not sniff(b"short")


# ── bounded decoding and a streaming upload cap ─────────────────────────────

class _Seg:
    text = " hello puff"


class _Model:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        return [_Seg()], None


@pytest.fixture
def decoder(api, monkeypatch, tmp_path):
    """Stub PyAV: a declared length and a stream of 1-second chunks."""
    import numpy as np
    st = {"declared": 3.0, "chunks": 3, "yielded": 0, "locked": [],
          "model": _Model()}

    def probe(path):
        st["locked"].append(api._voice_memo_lock.locked())
        if st["declared"] == "bad":
            raise RuntimeError("Invalid data found when processing input")
        return st["declared"]

    def frames(path):
        st["locked"].append(api._voice_memo_lock.locked())
        n = 0
        while st["chunks"] is None or n < st["chunks"]:
            n += 1
            st["yielded"] += 1
            yield np.zeros(api._VOICE_MEMO_RATE, dtype=np.float32)

    monkeypatch.setattr(api, "_voice_memo_probe", probe)
    monkeypatch.setattr(api, "_voice_memo_frames", frames)
    monkeypatch.setattr(api, "_voice_memo_stt", lambda: st["model"])
    f = tmp_path / "memo.m4a"
    f.write_bytes(M4A)
    st["path"] = str(f)
    return st


def test_a_normal_memo_decodes_and_transcribes_under_the_lock(api, decoder):
    text, seconds = api._transcribe_voice_memo(decoder["path"])
    assert text == "hello puff" and seconds == 3.0
    assert decoder["locked"] == [True, True]          # probe and decode
    assert decoder["model"].calls == 1


def test_a_long_declared_memo_is_refused_before_decoding(api, decoder):
    decoder["declared"] = 600.0
    text, seconds = api._transcribe_voice_memo(decoder["path"])
    assert text is None and seconds == 600.0
    assert decoder["yielded"] == 0 and decoder["model"].calls == 0
    # And through the route: 413, nothing in the thread.
    r = _memo(api.app.test_client(), api)
    assert r.status_code == 413 and r.get_json()["max_seconds"] == 120


def test_a_lying_header_cannot_decode_past_the_cap(api, decoder):
    decoder["declared"] = 10.0          # says ten seconds...
    decoder["chunks"] = None            # ...and never stops
    text, _ = api._transcribe_voice_memo(decoder["path"])
    assert text is None
    assert decoder["yielded"] == api.VOICE_MEMO_DECODE_CAP_SECONDS + 1
    assert decoder["model"].calls == 0
    # No declared length at all: the same cap holds.
    decoder["declared"], decoder["yielded"] = None, 0
    assert api._transcribe_voice_memo(decoder["path"])[0] is None
    assert decoder["yielded"] == api.VOICE_MEMO_DECODE_CAP_SECONDS + 1


def test_between_two_and_the_cap_is_still_refused(api, decoder):
    decoder["declared"], decoder["chunks"] = None, 123
    assert api._transcribe_voice_memo(decoder["path"])[0] is None
    assert decoder["model"].calls == 0


def test_an_unreadable_container_is_a_415(api, thread, decoder):
    decoder["declared"] = "bad"
    r = _memo(api.app.test_client(), api)
    assert r.status_code == 415
    assert _home(api) is None


def test_a_chunked_oversize_upload_stops_with_413(api, thread, stt,
                                                 monkeypatch):
    # Refused while the body streams in: the route never gets to write it.
    import tempfile

    def no_spool(*a, **k):
        raise AssertionError("the memo was written before the cap hit")
    monkeypatch.setattr(tempfile, "mkstemp", no_spool)
    boundary = "puffboundary"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"memo.m4a\"\r\nContent-Type: audio/mp4\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + M4A + b"\x00" * (api.VOICE_MEMO_MAX_BYTES + 256 * 1024) + tail
    c = api.app.test_client()
    r = c.post("/chat/voice-memo", input_stream=io.BytesIO(body),
               content_type=f"multipart/form-data; boundary={boundary}",
               headers=dict(_h(api), **{"Transfer-Encoding": "chunked"}),
               environ_overrides={"wsgi.input_terminated": True})
    assert r.status_code == 413
    assert stt["paths"] == []
    assert _home(api) is None


def test_memo_model_prefers_the_data_disk_copy(tmp_path, monkeypatch):
    """puffpai-api has ProtectHome=yes, so the memo model must load from a
    directory on the data disk when one is there, and fall back to the name."""
    from puffpai import api_server as api
    monkeypatch.setenv("PUFF_MEMO_WHISPER_DIR", str(tmp_path))
    monkeypatch.setenv("PUFF_WHISPER_MODEL", "base.en")
    assert api._voice_memo_model_source() == "base.en"
    (tmp_path / "model.bin").write_bytes(b"x")
    assert api._voice_memo_model_source() == str(tmp_path)
