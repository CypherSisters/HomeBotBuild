"""Stage-2 wake confirm: what it heard, biased toward her name, and retries.

With the radio playing, stage-2 rejects can vastly outnumber confirmed
wakes, and if the reject line carries no transcript,
nobody can tell whether Whisper heard "puppy", "pop pie", "buffet" or
nothing at all while the owner stood there saying "hey puff pie". The fix
makes the reject say what it heard (bounded), biases the CONFIRM
pass toward the wake name, tolerates the clipped forms Whisper writes over
music, and treats a second reject in a few seconds as evidence the owner is
trying.

No mic, no model, no PulseAudio: fake stream, fake transcriber, fake clock.
"""

import numpy as np
import pytest

from puffpai.voice import service


# ── doubles ────────────────────────────────────────────────────────────────
class FakeStream:
    def read(self, n):
        return np.zeros(service.BLOCK, dtype=np.int16), False


class FakeSTT:
    """Records every transcription and the bias it was (or was not) given."""

    supports_bias = True

    def __init__(self, *texts, conf=-0.25):
        self.texts = list(texts)
        self.calls = []          # list of bias strings, "" when unbiased
        self.last_avg_logprob = conf

    def transcribe(self, pcm, bias=""):
        self.calls.append(bias)
        return self.texts.pop(0) if self.texts else ""


class PlainSTT(FakeSTT):
    """An engine with no bias support (Vosk, the stub) — must never get a kwarg."""

    supports_bias = False

    def transcribe(self, pcm):
        self.calls.append("")
        return self.texts.pop(0) if self.texts else ""


class FakeWake:
    def __init__(self):
        self.resets = 0

    def push(self, samples):
        return 0.0

    def buffer_bytes(self):
        return b"\x01\x02"

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

    @property
    def armed(self):
        return [e for e in self.events if e == ("unaddressed", True)]


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(service, "play_ready", lambda: None)
    monkeypatch.setattr(service, "play_sleep", lambda: None)
    monkeypatch.setattr(service, "flush_stream", lambda s: None)
    monkeypatch.setattr(service, "normalize_utterance", lambda pcm, **kw: pcm)
    monkeypatch.setattr(service, "capture_fixed", lambda stream, secs: b"\x00\x00" * 8)
    monkeypatch.setattr(service, "WAKE_CONFIRM", True)
    duck = StubDuck()
    monkeypatch.setattr(service, "_DUCK", duck)
    service.clear_wake_retry()
    yield duck
    service.clear_wake_retry()


# ── 1. tolerant key match ──────────────────────────────────────────────────
@pytest.mark.parametrize("heard", [
    "hey puffpie what time",                 # glued, no space
    "Hey, puff pi, stop",
    "a puff pie.",
    "hey puf pie, play the radio",           # one f
    "Hey pof pi what's the weather",
    "puffy pie, turn the lamp off",          # trailing letters on the head
    "okay hey puff, pie, set a timer",       # tail one token late
])
def test_clipped_forms_of_her_name_still_wake(heard):
    assert service.wake_word_heard(heard) is True


@pytest.mark.parametrize("heard", [
    "apple pie and ice cream",               # tail, no head
    "",
    "   ",
    "I made a pie for the party",
    "no I told him Tuesday, not Thursday",
    "can you pass me the remote",
])
def test_room_conversation_is_still_rejected(heard):
    assert service.wake_word_heard(heard) is False


@pytest.mark.parametrize("heard", [
    "apple pie and ice cream",               # tail, no head
    "puffy jacket",                          # head, no tail
    "the puppet show",
    "the puffin flew away and then landed",  # tail far too late, if at all
    "I made a pie for the party",
    "",
])
def test_the_new_fuzzy_rule_adds_no_new_false_positives(heard):
    """Asserted against the fuzzy rule itself: the legacy substring keys
    ("puf", "pup") already accept "puffy"/"puppet" and are deliberately left
    alone here, the appliance's problem is missing a real wake, not hearing
    the room."""
    assert service._fuzzy_name_heard(heard) is False


# ── 2. what it heard, bounded ──────────────────────────────────────────────
def test_heard_is_quoted_collapsed_and_bounded():
    assert service._heard("hey puff pie") == '"hey puff pie"'
    assert service._heard("hey\nputt  pie\r\n") == '"hey putt pie"'
    assert service._heard(None) == '""'
    long = "word " * 40
    rendered = service._heard(long)
    assert len(rendered) == service.HEARD_MAX + 2          # + the two quotes
    assert service.HEARD_MAX == 60


def test_heard_escapes_quotes_so_one_event_stays_parseable():
    assert service._heard('say "hi"') == '"say \\"hi\\""'
    assert service._heard('back\\slash') == '"back\\\\slash"'


def test_a_rejected_wake_logs_the_transcript_and_the_level(quiet, caplog):
    caplog.set_level("INFO")
    stt = FakeSTT("what a nice day")

    assert service.handle_wake(FakeStream(), stt, object(), FakeWake(), []) is False

    line = [r.getMessage() for r in caplog.records if "wake=rejected" in r.getMessage()]
    assert len(line) == 1
    assert 'heard="what a nice day"' in line[0]
    assert "reason=no-wakeword" in line[0]
    assert "rms=" in line[0]


def test_a_confirmed_wake_also_says_what_it_heard(quiet, caplog, monkeypatch):
    caplog.set_level("INFO")
    monkeypatch.setattr(service, "record_capture", lambda *a, **k: service.Capture(pcm=b""))
    monkeypatch.setattr(service, "converse", lambda *a, **k: None)
    stt = FakeSTT("hey puff pie", "hey puff pie, what time is it")

    service.handle_wake(FakeStream(), stt, object(), FakeWake(), [])

    line = [r.getMessage() for r in caplog.records if "wake=confirmed" in r.getMessage()]
    assert len(line) == 1
    assert 'heard="hey puff pie"' in line[0]


# ── 3. name bias, confirm pass only ────────────────────────────────────────
def test_only_the_second_look_is_biased_and_the_command_pass_never_is(
        quiet, monkeypatch):
    """Stage 2 leads with an UNBIASED pass. The hotwords are a
    tie-breaker on a short, name-less transcript — never the first word on it."""
    monkeypatch.setattr(service, "WAKE_CONFIRM_HOTWORDS", "Hey Puff Pie")
    monkeypatch.setattr(service, "record_capture", lambda *a, **k: service.Capture(pcm=b""))
    monkeypatch.setattr(service, "converse", lambda *a, **k: None)
    stt = FakeSTT("hey what's that", "hey puff pie",
                  "hey puff pie, what time is it")

    service.handle_wake(FakeStream(), stt, object(), FakeWake(), [])

    assert stt.calls == ["", "Hey Puff Pie", ""]


def test_a_name_heard_unbiased_needs_no_second_pass(quiet, monkeypatch):
    monkeypatch.setattr(service, "record_capture", lambda *a, **k: service.Capture(pcm=b""))
    monkeypatch.setattr(service, "converse", lambda *a, **k: None)
    stt = FakeSTT("hey puff pie", "hey puff pie, what time is it")

    service.handle_wake(FakeStream(), stt, object(), FakeWake(), [])

    assert stt.calls == ["", ""], "an honest pass 1 ends stage 2"


def test_the_bias_is_disabled_by_an_empty_env_override(quiet, monkeypatch):
    monkeypatch.setattr(service, "WAKE_CONFIRM_HOTWORDS", "")
    stt = FakeSTT("nothing here")

    service.handle_wake(FakeStream(), stt, object(), FakeWake(), [])

    assert stt.calls == [""]


def test_an_engine_without_bias_support_is_never_handed_the_kwarg(quiet):
    stt = PlainSTT("nothing here")

    service.handle_wake(FakeStream(), stt, object(), FakeWake(), [])

    assert stt.calls == [""]


# ── 4. a retry is evidence she is trying — if it LOOKS like trying ─────────
def test_two_rejects_inside_the_window_count_as_a_retry():
    service.clear_wake_retry()
    assert service.wake_retry_pressure("pie", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure("hey", now=104.0, window=12.0) is True


def test_a_reject_long_after_the_last_one_is_not_a_retry():
    service.clear_wake_retry()
    assert service.wake_retry_pressure("pie", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure("pie", now=130.0, window=12.0) is False


def test_a_confirmed_wake_ends_the_streak():
    service.clear_wake_retry()
    service.wake_retry_pressure("pie", now=100.0, window=12.0)
    service.clear_wake_retry()
    assert service.wake_retry_pressure("pie", now=101.0, window=12.0) is False


def test_singing_two_lines_in_a_row_is_not_a_retry():
    """A chorus can fire the wake model every few seconds, so a bare
    streak rule alone would stay permanently true and dip the music all song."""
    service.clear_wake_retry()
    assert service.wake_retry_pressure(
        "in your head they are fighting", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure(
        "with their tanks and their bombs", now=103.0, window=12.0) is False


def test_two_empty_rejects_are_not_a_retry():
    """Sung audio comes back as heard="" — short, but no name in it."""
    service.clear_wake_retry()
    assert service.wake_retry_pressure("", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure("", now=102.0, window=12.0) is False


def test_her_second_try_arms_the_peek_even_on_a_confident_transcript(quiet,
                                                                    monkeypatch):
    """A crisp but wrong transcript ("pop pie") looks strong, so the weak-text
    rule never armed a peek — and she kept talking into unducked music."""
    duck = quiet
    monkeypatch.setattr(service, "_looks_like_missed_wake", lambda stt, text: False)

    service.handle_wake(FakeStream(), FakeSTT("pie"), object(), FakeWake(), [])
    assert duck.armed == [], "one reject alone is still not evidence"

    service.handle_wake(FakeStream(), FakeSTT("pie"), object(), FakeWake(), [])
    assert len(duck.armed) == 1, "the second try in the window arms the peek"


def test_the_streak_does_not_depend_on_how_the_transcript_scored(quiet,
                                                                monkeypatch):
    """The first reject must be RECORDED even when the weak rule already fired,
    or a weak-then-crisp pair would never look like a retry."""
    duck = quiet
    weak = iter([True, False])
    monkeypatch.setattr(service, "_looks_like_missed_wake",
                        lambda stt, text: next(weak))

    service.handle_wake(FakeStream(), FakeSTT("pie"), object(), FakeWake(), [])
    service.handle_wake(FakeStream(), FakeSTT("hey"), object(), FakeWake(), [])

    assert len(duck.armed) == 2


def test_ducking_is_still_left_to_the_policy(quiet):
    """We only ARM: on_unaddressed itself refuses unless something is playing,
    which is what keeps room talk from dipping the music."""
    duck = quiet
    service.handle_wake(FakeStream(), FakeSTT("what a nice day"), object(),
                        FakeWake(), [])
    assert "addressed" not in duck.events
    assert [e for e in duck.events if e == "wake"] == ["wake"]
