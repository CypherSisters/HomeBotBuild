"""The wake-confirm BRIDGE: audio recorded while stage 2 transcribes.

With the radio playing, the owner says
"hey puff pie, play some music". A log like this can result:

    wake=confirmed conf=-0.44 ms=1081
    vad=calibrated floor=161 threshold=241
    ADDRESSED: 'A puff pie some music'  (command='some music')

The verb is gone, so the brain answers "Which song?". The ~1.1 s the stage-2
transcription costs is not lost in the driver, ``CaptureStream`` queues 30 s
and shows no overflow, it is DISCARDED by the capture that
follows: ``record_utterance`` spends the first 0.3 s of it calibrating the VAD
(over the owner's own voice: floor=161 with the radio on) and then drops every
block below the resulting threshold while waiting for a speech onset that has
already happened. "play" is thrown away as ambient.

The fix records that window explicitly (``bridge``), prepends it to the command
audio, calibrates from audio that predates the trigger, and resumes the capture
already-speaking instead of waiting for an onset.

No mic, no model, no PulseAudio: fake stream, fake transcriber with an
injectable sleep.
"""

import logging
import time

import numpy as np
import pytest

from puffpai.voice import service


BLOCK_SECONDS = service.BLOCK / service.SAMPLE_RATE   # 0.125 s


def block(level: float) -> np.ndarray:
    """One capture block whose RMS is exactly ``level``."""
    return np.full(service.BLOCK, int(level), dtype=np.int16)


class FakeStream:
    """Blocks by RMS level, each stamped with the time it was handed over."""

    def __init__(self, levels, tail=0.0, realtime=False):
        self.levels = list(levels)
        self.tail = tail
        self.realtime = realtime  # pace reads at one block per 0.125 s, as a mic does
        self.t0 = time.monotonic()
        self.stamps = []          # (monotonic, level) per read

    def read(self, n):
        if self.realtime:
            due = self.t0 + len(self.stamps) * BLOCK_SECONDS
            time.sleep(max(0.0, due - time.monotonic()))
        level = self.levels.pop(0) if self.levels else self.tail
        self.stamps.append((time.monotonic(), level))
        return block(level), False


class SleepySTT:
    """Transcriber whose calls take ``delay`` seconds (the confirm is ~1.1 s)."""

    supports_bias = True

    def __init__(self, *texts, delay=0.0, conf=-0.44):
        self.texts = list(texts)
        self.delay = delay
        self.calls = []           # bytes-length of every transcription
        self.last_avg_logprob = conf

    def transcribe(self, pcm, bias=""):
        self.calls.append(len(pcm))
        if self.delay:
            time.sleep(self.delay)
        return self.texts.pop(0) if self.texts else ""


class FakeWake:
    def __init__(self, fire_after=None, name=b"\x01\x02"):
        self.fire_after = fire_after
        self.pushes = 0
        self.resets = 0
        self._name = name

    def push(self, samples):
        self.pushes += 1
        if self.fire_after is not None and self.pushes >= self.fire_after:
            return 1.0
        return 0.0

    def buffer_bytes(self):
        return self._name

    def reset(self):
        self.resets += 1


class StubDuck:
    def __init__(self):
        self.events = []

    def on_wake(self):
        self.events.append("wake")

    def on_addressed(self):
        self.events.append("addressed")

    def on_unaddressed(self, missed, reason="missed"):
        self.events.append(("unaddressed", missed))

    def restore(self):
        self.events.append("restore")


@pytest.fixture
def quiet(monkeypatch):
    """Silence the audible side effects, the duck policy and the gain stage."""
    chimes = []
    monkeypatch.setattr(service, "play_ready", lambda: chimes.append("ready"))
    monkeypatch.setattr(service, "play_sleep", lambda: chimes.append("sleep"))
    monkeypatch.setattr(service, "flush_stream", lambda s: None)
    monkeypatch.setattr(service, "normalize_utterance", lambda pcm, **kw: pcm)
    duck = StubDuck()
    monkeypatch.setattr(service, "_DUCK", duck)
    return chimes, duck


def pre_roll(room_level=20, name_level=600):
    """A 1 s trigger window: quiet room first, then the name."""
    blocks = int(1.0 / BLOCK_SECONDS)
    head = [block(room_level)] * (blocks // 2)
    tail = [block(name_level)] * (blocks - blocks // 2)
    return np.concatenate(head + tail).tobytes()


# ── (a) the audio recorded during the confirm reaches the command ──────────
def test_the_confirm_window_keeps_recording_while_it_transcribes(quiet, monkeypatch):
    """The blocks that arrive during the transcription come back in ``bridge``."""
    # After the confirm window the stream delivers the command at 900.
    monkeypatch.setattr(service, "WAKE_CONFIRM_WINDOW", 0.25)
    confirm_blocks = int(service.WAKE_CONFIRM_WINDOW / BLOCK_SECONDS) + 1
    stream = FakeStream([500] * confirm_blocks, tail=900, realtime=True)
    stt = SleepySTT("Hey puff pie, play some music", delay=0.5)

    ok, extra, text, ms, bridge, _pass, _why, _conf = service.confirm_wake(
        stream, stt, pre_roll())

    assert ok is True
    assert bridge, "nothing was recorded while the confirm transcription ran"
    heard = np.frombuffer(bridge, dtype=np.int16)
    assert int(heard.max()) == 900, "the bridge holds the command, not silence"
    # It covers the transcription: ~0.5 s of audio for a 0.5 s transcription.
    assert 0.3 <= len(bridge) / 2 / service.SAMPLE_RATE <= 0.9
    assert ms >= 400.0


def test_the_bridge_is_prepended_to_the_command_transcription(quiet, monkeypatch):
    """The 00:39 failure: the verb must be inside the audio Whisper sees."""
    seen = {}

    def fake_capture(stream, stt, wake, **kw):
        seen.update(kw)
        return service.Capture(block(900).tobytes())

    monkeypatch.setattr(service, "record_capture", fake_capture)
    monkeypatch.setattr(service, "converse", lambda *a, **kw: None)
    monkeypatch.setattr(service, "WAKE_CONFIRM_WINDOW", 0.25)
    confirm_blocks = int(service.WAKE_CONFIRM_WINDOW / BLOCK_SECONDS) + 1
    stream = FakeStream([500] * confirm_blocks, tail=900, realtime=True)
    stt = SleepySTT("Hey puff pie", "hey puff pie, play some music", delay=0.5)
    wake = FakeWake(name=pre_roll())

    assert service.handle_wake(stream, stt, None, wake, []) is True

    confirm_len, command_len = stt.calls
    # Everything the confirm saw, PLUS the bridge, PLUS the new capture.
    assert command_len > confirm_len + len(block(900).tobytes())
    bridged = command_len - confirm_len - len(block(900).tobytes())
    assert bridged / 2 / service.SAMPLE_RATE >= 0.3, "the bridge was dropped"
    # A command already in flight is resumed, never waited for or calibrated over.
    assert seen["resume"] is True
    assert seen["threshold_override"] is not None


def test_a_resumed_capture_neither_waits_nor_calibrates():
    """``resume`` starts in the speaking state: no onset wait, no calibration."""
    # Silence first: a non-resumed capture would sit in the wait and return None.
    stream = FakeStream([20] * 4 + [900] * 8 + [20] * 12, tail=20)
    pcm = service.record_utterance(stream, first=True, resume=True,
                                   threshold_override=100.0, max_utterance=20.0)
    assert pcm is not None
    assert len(pcm) / 2 / service.SAMPLE_RATE >= 1.0
    assert int(np.frombuffer(pcm, dtype=np.int16).max()) == 900


def test_the_floor_comes_from_before_the_trigger_not_from_her_voice():
    """The floor is the room before the name (20), not the speech after it."""
    assert service.ambient_floor(pre_roll(room_level=20, name_level=600)) == 20
    # All speech, no honest sample: None, and the caller falls back.
    loud = np.concatenate([block(900)] * 8).tobytes()
    assert service.ambient_floor(loud) is None
    assert service.has_speech(loud, 241.0) is True
    assert service.has_speech(np.concatenate([block(20)] * 8).tobytes(), 241.0) is False


# ── (b) a rejected confirm keeps nothing ───────────────────────────────────
def test_a_rejected_confirm_discards_the_bridge(quiet, monkeypatch):
    captures = []
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: captures.append(kw))
    confirm_blocks = int(service.WAKE_CONFIRM_WINDOW / BLOCK_SECONDS) + 1
    stream = FakeStream([500] * confirm_blocks, tail=900)
    stt = SleepySTT("no I told him Tuesday", delay=0.3)

    assert service.handle_wake(stream, stt, None, FakeWake(name=pre_roll()), []) is False

    assert captures == [], "room conversation must never reach a capture"
    assert stt.calls == [len(stt.calls) and stt.calls[0]]   # exactly one pass


# ── (c) barge-in still wins, and starts clean ──────────────────────────────
def test_barge_in_restarts_without_the_bridge_state(quiet, monkeypatch):
    """A barge-in is a NEW utterance: no resume, no inherited threshold."""
    monkeypatch.setattr(service, "BARGE_IN", True)
    attempts = []

    def fake_record(stream, first, wait_override=0.0, **kw):
        attempts.append(dict(first=first, wait_override=wait_override, **kw))
        if len(attempts) == 1:
            raise service.BargeIn(b"\x09\x09")
        return b"\x01\x02"

    monkeypatch.setattr(service, "record_utterance", fake_record)
    wake = FakeWake()
    cap = service.record_capture(FakeStream([], tail=900), SleepySTT(), wake,
                                 first=True, confirmed=True, resume=True,
                                 threshold_override=241.0)

    assert cap.barged is True and cap.pcm == b"\x01\x02"
    assert cap.pre == b"\x09\x09"
    assert attempts[0]["resume"] is True and attempts[0]["threshold_override"] == 241.0
    assert attempts[1]["resume"] is False and attempts[1]["threshold_override"] is None


def test_barge_in_during_a_resumed_capture_still_fires(monkeypatch):
    """The wake model keeps running even when the capture started already-speaking."""
    monkeypatch.setattr(service, "BARGE_IN_AFTER", 0.0)
    monkeypatch.setattr(service, "BARGE_IN_COOLDOWN", 0.0)
    wake = FakeWake(fire_after=2)
    with pytest.raises(service.BargeIn):
        service.record_utterance(FakeStream([], tail=900), first=True, resume=True,
                                 threshold_override=100.0, max_utterance=20.0,
                                 wake=wake, confirm=lambda pcm: True)


# ── (d) the log says how much was bridged, and what a follow-up heard ──────
def test_the_confirmed_line_reports_the_bridge_length(quiet, monkeypatch, caplog):
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: service.Capture(None))
    monkeypatch.setattr(service, "WAKE_CONFIRM_WINDOW", 0.25)
    confirm_blocks = int(service.WAKE_CONFIRM_WINDOW / BLOCK_SECONDS) + 1
    stream = FakeStream([500] * confirm_blocks, tail=900, realtime=True)
    stt = SleepySTT("Hey puff pie", "", delay=0.5)

    with caplog.at_level(logging.INFO, logger="puffpai.voice"):
        service.handle_wake(stream, stt, None, FakeWake(name=pre_roll()), [])

    line = [m for m in caplog.messages if m.startswith("wake=confirmed")][0]
    field = [f for f in line.split() if f.startswith("bridge_ms=")][0]
    assert 300 <= int(field.split("=")[1]) <= 900
    assert "resume=true" in line


def test_a_follow_up_says_what_it_heard_bounded(quiet, monkeypatch, caplog):
    monkeypatch.setattr(service, "FOLLOWUP", True)
    monkeypatch.setattr(service, "MAX_FOLLOWUPS", 1)
    # A window only opens after a QUESTION now.
    monkeypatch.setattr(service, "ask_brain", lambda h, t: "Which station?")
    monkeypatch.setattr(service, "speak", lambda tts, text: None)
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: service.Capture(b"\x01\x02"))
    monkeypatch.setattr(service, "extend_if_fragment", lambda s, stt, c: c)
    long_tail = "stop the music " * 10
    stt = SleepySTT(long_tail)

    with caplog.at_level(logging.INFO, logger="puffpai.voice"):
        service.converse(FakeStream([], tail=20), stt, None, [],
                         initial_command="play some jazz radio")

    line = [m for m in caplog.messages if m.startswith("wake=followup")][0]
    assert 'heard="stop the music' in line
    quoted = line.split('heard="', 1)[1].rstrip('"')
    assert len(quoted) <= service.HEARD_MAX
    assert f"len={len(long_tail.strip())}" in line
