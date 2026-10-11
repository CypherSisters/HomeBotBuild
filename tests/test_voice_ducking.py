"""Tests for the voice ducking policy.

The requirement: singing along or talking near the speaker must not mute or
lower the music. Ducking is only for when the assistant is speaking or a
conversation with it is under way.

Ducking to 45% the instant the wake model fires happens before transcription,
so it fires on ANY speech in the room:

    ducked 1 playback stream(s) to 45% while listening
    wake=fired addressed=false

The two invariants under test:

1. Listening to / transcribing an un-addressed utterance changes NOTHING
   audible.
2. An addressed turn, its spoken reply, and the hands-free follow-up window
   duck, and restore when the exchange ends.

Plus the case that makes a pure "never duck before the address check" policy
fail: saying the wake phrase over loud music, where the un-ducked continuation
degrades the joint Whisper decode and drops the wake name ("What time is
it?").
"""

import pytest

from puffpai.voice import ducking
from puffpai.voice import service


# ── fakes ──────────────────────────────────────────────────────────────────
class FakePactl:
    """Records every pactl invocation the policy makes."""

    def __init__(self, sink_inputs=("7",)):
        self.sink_inputs = list(sink_inputs)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["pactl", "list", "short"]:
            out = "".join(f"{i}\t2\t47\tprotocol-native.c\ts16le\n"
                          for i in self.sink_inputs)
            return type("R", (), {"stdout": out, "returncode": 0})()
        return type("R", (), {"stdout": "", "returncode": 0})()

    @property
    def volume_calls(self):
        return [c for c in self.calls if c[1] == "set-sink-input-volume"]

    @property
    def levels(self):
        """Every level the playback streams were set to, in order."""
        return [c[3].rstrip("%") for c in self.volume_calls]


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeSTT:
    """Yields a queued transcript per call, with a confidence."""

    def __init__(self, *transcripts):
        self.queue = list(transcripts)
        self.last_avg_logprob = None

    def transcribe(self, pcm):
        text, conf = self.queue.pop(0)
        self.last_avg_logprob = conf
        return text


class FakeWake:
    def buffer_bytes(self):
        return b"\x00\x00" * 16000

    def reset(self):
        pass


def _policy(pactl, clock=None, **kw):
    return ducking.DuckPolicy(addressed_level="45", peek_level="70",
                              duck_on_listen=False, peek_arm=20,
                              runner=pactl, clock=clock or FakeClock(), **kw)


@pytest.fixture
def wired(monkeypatch):
    """Patch the service's module-level policy and its I/O side effects."""
    pactl = FakePactl()
    policy = _policy(pactl)
    monkeypatch.setattr(service, "_DUCK", policy)
    # These tests are about the DUCK policy, so the stage-2 wake confirmation
    # (which costs its own transcription) is switched off here; it has its own
    # coverage in tests/test_voice_recorder.py.
    monkeypatch.setattr(service, "WAKE_CONFIRM", False)
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: service.Capture(b""))
    monkeypatch.setattr(service, "normalize_utterance", lambda pcm, **kw: pcm)
    monkeypatch.setattr(service, "flush_stream", lambda s: None)
    monkeypatch.setattr(service, "_log_unaddressed", lambda stt, text: None)
    return pactl, policy


# ── 1. un-addressed speech is silent ───────────────────────────────────────
@pytest.mark.parametrize("heard", [
    "I've been waiting for a girl like you",   # singing along
    "no I told him Tuesday, not Thursday",     # room conversation
    "can you pass me the remote",              # spoken near the appliance
])
def test_unaddressed_utterance_makes_zero_duck_calls(wired, heard):
    pactl, policy = wired
    stt = FakeSTT((heard, -0.25))

    addressed = service.handle_wake(object(), stt, object(), FakeWake(), [])

    assert addressed is False
    assert pactl.volume_calls == [], (
        "un-addressed speech must not touch playback volume at all")
    assert policy.active is False


def test_singing_along_does_not_arm_a_peek(wired):
    """A confident, name-free transcript is not a missed wake."""
    pactl, policy = wired
    stt = FakeSTT(("I've been waiting for a girl like you", -0.25))
    service.handle_wake(object(), stt, object(), FakeWake(), [])
    assert policy.peek_armed() is False


# ── 2. an addressed turn ducks, then restores ──────────────────────────────
def test_addressed_turn_ducks_then_restores(wired, monkeypatch):
    pactl, policy = wired
    spoken = []
    monkeypatch.setattr(service, "converse",
                        lambda *a, **kw: spoken.append(policy.level))
    stt = FakeSTT(("Hey puff pie, what time is it?", -0.3))

    addressed = service.handle_wake(object(), stt, object(), FakeWake(), [])

    assert addressed is True
    assert pactl.levels == ["45", "100"]
    # The reply is spoken while the music is still down.
    assert spoken == ["45"]
    assert policy.active is False


def test_tts_reply_plays_over_a_ducked_stream_and_restores(wired, monkeypatch):
    """TTS starts AFTER the snapshot, so it is never itself ducked."""
    pactl, policy = wired
    seen = []

    def fake_converse(stream, stt, tts, history, initial_command="", **kw):
        # Puff's own reply appears as a new sink-input mid-exchange.
        pactl.sink_inputs.append("9")
        service.speak(tts, "It is ten past four.")
        seen.append((policy.level, list(policy._ducked)))

    monkeypatch.setattr(service, "converse", fake_converse)
    monkeypatch.setattr(service, "speak", lambda tts, text: None)
    stt = FakeSTT(("Hey puffpai, what time is it?", -0.3))

    service.handle_wake(object(), stt, object(), FakeWake(), [])

    assert seen == [("45", ["7"])], "the reply stream must not be ducked"
    assert pactl.levels == ["45", "100"]


def test_followup_window_ducks_only_while_open(wired, monkeypatch):
    """PUFF_FOLLOWUP=1: the window is inside the exchange, so it stays ducked
    for its 8 s and is restored the moment it closes — with no extra churn."""
    pactl, policy = wired
    during = []

    def fake_converse(stream, stt, tts, history, initial_command="", **kw):
        during.append(policy.level)          # answering
        during.append(policy.level)          # follow-up window open
        during.append(policy.level)          # follow-up answered

    monkeypatch.setattr(service, "converse", fake_converse)
    monkeypatch.setattr(service, "FOLLOWUP", True)
    stt = FakeSTT(("Hey puffpai, what time is it?", -0.3))

    service.handle_wake(object(), stt, object(), FakeWake(), [])

    assert during == ["45", "45", "45"]
    assert pactl.levels == ["45", "100"], "one duck, one restore across the window"
    assert policy.active is False


# ── 3. the peek path: the wake phrase over loud music ──────────────────────
def test_low_confidence_over_music_arms_then_peeks_once_at_70(wired):
    """A weak transcript while something is playing arms a peek; the next wake
    ducks to 70% (not 45%) before recording, then restores."""
    pactl, policy = wired
    stt = FakeSTT(("mm what time", -0.95))

    service.handle_wake(object(), stt, object(), FakeWake(), [])
    assert pactl.volume_calls == [], "the weak pass itself stays silent"
    assert policy.peek_armed() is True

    # She repeats herself; this attempt is captured with a light peek duck.
    stt.queue.append(("What time is it?", -0.9))
    service.handle_wake(object(), stt, object(), FakeWake(), [])

    assert pactl.levels == ["70", "100"], "exactly one peek duck at 70%, then restore"
    assert policy.active is False


def test_wake_phrase_over_loud_music_is_addressed_via_the_peek(wired, monkeypatch):
    """The loud-music case, end to end.

    Loud music: the first "Hey PuffPAI, what time is it?" comes back as the
    logged 'What time is it?' — her name eaten by the degraded joint decode, so
    parse_address rejects it. That arms a peek; her repeat is captured with
    the music at 70% and this time the name survives.
    """
    pactl, policy = wired
    monkeypatch.setattr(service, "converse", lambda *a, **kw: None)
    stt = FakeSTT(("What time is it?", -0.9),
                  ("Hey puff pie, what time is it?", -0.3))

    assert service.handle_wake(object(), stt, object(), FakeWake(), []) is False
    assert policy.peek_armed() is True
    assert pactl.volume_calls == []

    assert service.handle_wake(object(), stt, object(), FakeWake(), []) is True
    # Peek while listening, deepened once addressed, restored at the end.
    assert pactl.levels == ["70", "45", "100"]
    assert policy.active is False


def test_weak_transcript_with_nothing_playing_arms_nothing(wired):
    pactl, policy = wired
    pactl.sink_inputs = []
    stt = FakeSTT(("mm what time", -0.95))
    service.handle_wake(object(), stt, object(), FakeWake(), [])
    assert policy.peek_armed() is False
    assert pactl.volume_calls == []


def test_peek_arm_expires(wired):
    pactl, policy = wired
    clock = policy._now
    stt = FakeSTT(("mm what time", -0.9))
    service.handle_wake(object(), stt, object(), FakeWake(), [])
    assert policy.peek_armed() is True
    clock.t += 21
    assert policy.peek_armed() is False


# ── policy unit tests ──────────────────────────────────────────────────────
def test_duck_on_listen_opt_in_restores_old_behaviour():
    pactl = FakePactl()
    policy = _policy(pactl)
    policy.duck_on_listen = True
    policy.on_wake()
    assert pactl.levels == ["70"]
    policy.restore()
    assert pactl.levels == ["70", "100"]


def test_deepen_does_not_resnapshot():
    pactl = FakePactl(sink_inputs=["7"])
    policy = _policy(pactl)
    policy.duck("70", "peek")
    pactl.sink_inputs.append("9")            # Puff's reply starts
    policy.deepen("45", "addressed")
    policy.restore()
    assert [c[2] for c in pactl.volume_calls] == ["7", "7", "7"]


def test_restore_with_nothing_ducked_is_a_noop():
    pactl = FakePactl()
    policy = _policy(pactl)
    policy.restore()
    assert pactl.calls == []


def test_duck_with_nothing_playing_records_nothing():
    pactl = FakePactl(sink_inputs=[])
    policy = _policy(pactl)
    assert policy.duck("45", "addressed") == 0
    assert policy.active is False


def test_pactl_failure_never_raises():
    def boom(argv, **kw):
        raise OSError("pactl not found")
    policy = ducking.DuckPolicy(runner=boom)
    assert policy.playback_active() is False
    assert policy.duck("45", "addressed") == 0
    policy.restore()


# ── the weak-transcript heuristic ──────────────────────────────────────────
@pytest.mark.parametrize("text,conf,expected", [
    ("", None, False),          # heard nothing: the signature of singing, not
                                # of a missed wake
    ("Ohhhh!", -1.26, False),   # a held note: weak, but an interjection
    ("hey", -1.1, True),        # half her name is evidence at any confidence
    ("What time is it?", -0.9, True),                    # low confidence
    ("so then I said puff pie should hear this", -0.3, True),   # late name
    ("I've been waiting for a girl like you", -0.25, False),    # singing
    ("no I told him Tuesday", -0.3, False),              # room talk
    ("no I told him Tuesday", None, False),              # Vosk: no confidence
])
def test_looks_like_missed_wake(text, conf, expected):
    stt = FakeSTT()
    stt.last_avg_logprob = conf
    assert service._looks_like_missed_wake(stt, text) is expected
