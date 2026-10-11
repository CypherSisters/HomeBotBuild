"""The voice recorder: stage-2 wake confirmation, barge-in, caps, VAD floor.

In a quiet room with no music, the raw-audio wake model can fire on ordinary
conversation, each
time recording a full 12 s before ``parse_address`` throws the transcript away,
and while one of those runs, the owner's real "hey puff pie, stop" is not heard.

``service`` is hardware-bound, so everything here runs against a fake stream of
synthetic blocks and a fake transcriber. No mic, no model, no PulseAudio.
"""

import numpy as np
import pytest

from puffpai.voice import service


BLOCK_SECONDS = service.BLOCK / service.SAMPLE_RATE   # 0.125 s


def block(level: float) -> np.ndarray:
    """One capture block whose RMS is exactly ``level``."""
    return np.full(service.BLOCK, int(level), dtype=np.int16)


class FakeStream:
    """Queued blocks by RMS level; repeats ``tail`` once the queue runs out."""

    def __init__(self, levels, tail=0.0):
        self.levels = list(levels)
        self.tail = tail
        self.reads = 0

    def read(self, n):
        self.reads += 1
        level = self.levels.pop(0) if self.levels else self.tail
        return block(level), False


class FakeSTT:
    """Returns queued transcripts; records how many times it was asked."""

    def __init__(self, *texts, conf=-0.25):
        self.texts = list(texts)
        self.calls = []
        self.last_avg_logprob = conf

    def transcribe(self, pcm):
        self.calls.append(len(pcm))
        return self.texts.pop(0) if self.texts else ""


class FakeWake:
    """Wake model that fires on demand after ``fire_after`` pushes."""

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


@pytest.fixture(autouse=True)
def _shut_the_followup_window():
    """The hands-free window is MODULE state, so it survives
    between tests and between FILES. A window another test left open turns a
    rejected wake into an accepted follow-up here."""
    service.close_followup_window()
    service.reset_followup_chain()
    yield
    service.close_followup_window()
    service.reset_followup_chain()


@pytest.fixture
def quiet(monkeypatch):
    """Silence the audible side effects and the duck policy."""
    chimes = []
    monkeypatch.setattr(service, "play_ready", lambda: chimes.append("ready"))
    monkeypatch.setattr(service, "play_sleep", lambda: chimes.append("sleep"))
    monkeypatch.setattr(service, "flush_stream", lambda s: None)
    monkeypatch.setattr(service, "normalize_utterance", lambda pcm, **kw: pcm)
    duck = StubDuck()
    monkeypatch.setattr(service, "_DUCK", duck)
    return chimes, duck


# ── 1. stage-2 wake confirmation ───────────────────────────────────────────
@pytest.mark.parametrize("heard", [
    "Hey puff pie, what time is it?",
    "Hey puffpai, stop the music",
    "hey puff pi what's playing",
    "Hey puff eye, turn off the lamp",
    "Hey puff hi, set a timer",
    "Hey, pie, what time is it?",          # Whisper dropped the "puff"
    "hey puppy, play the radio",
])
def test_confirm_accepts_every_way_whisper_writes_her_name(heard):
    assert service.wake_word_heard(heard) is True


@pytest.mark.parametrize("heard", [
    "no I told him Tuesday, not Thursday",
    "I've been waiting for a girl like you",
    "can you pass me the remote",
    "yeah we should leave around six if the traffic is okay",
    "",
    "   ",
])
def test_confirm_rejects_ordinary_room_conversation(heard):
    assert service.wake_word_heard(heard) is False


def test_confirm_transcribes_only_the_trigger_window_and_keeps_the_audio(quiet):
    """The confirm pass is cheap (a ~2.2 s window) and costs no command audio."""
    stream = FakeStream([], tail=800)
    stt = FakeSTT("Hey puff pie, what time is it")
    pre = b"\x00\x00" * service.SAMPLE_RATE          # 1 s trigger buffer

    ok, extra, text, ms, bridge, _pass, _why, _conf = service.confirm_wake(
        stream, stt, pre)

    assert ok is True
    assert len(extra) // 2 >= int(service.WAKE_CONFIRM_WINDOW * service.SAMPLE_RATE)
    # One transcription, of 1 s + the confirm window and nothing more.
    window_s = len(pre + extra) / 2 / service.SAMPLE_RATE
    assert stt.calls == [len(pre + extra)]
    assert window_s == pytest.approx(1.0 + service.WAKE_CONFIRM_WINDOW, abs=0.15)
    assert ms >= 0.0
    assert isinstance(bridge, bytes)


def test_room_conversation_never_reaches_a_capture(quiet, monkeypatch):
    """Ordinary room conversation must not trigger a 12 s recording."""
    chimes, duck = quiet
    captures = []
    monkeypatch.setattr(service, "WAKE_CONFIRM", True)
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: captures.append(kw) or service.Capture(b""))
    stt = FakeSTT("no I told him Tuesday, not Thursday")

    addressed = service.handle_wake(FakeStream([], tail=300), stt, object(),
                                    FakeWake(), [])

    assert addressed is False
    assert captures == [], "a rejected wake must not record the room"
    assert chimes == [], "a rejected wake must be silent — no chime"
    assert stt.calls == [len(stt.calls) and stt.calls[0]]  # exactly one pass


def test_a_confirmed_wake_proceeds_exactly_as_before(quiet, monkeypatch):
    chimes, duck = quiet
    seen = {}
    monkeypatch.setattr(service, "WAKE_CONFIRM", True)
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: service.Capture(b"\x01\x02"))
    monkeypatch.setattr(service, "converse",
                        lambda *a, **kw: seen.update(kw))
    stt = FakeSTT("Hey puff pie, what time is it",
                  "Hey puff pie, what time is it")

    addressed = service.handle_wake(FakeStream([], tail=800), stt, object(),
                                    FakeWake(), [])

    assert addressed is True
    assert seen["initial_command"] == "what time is it"
    assert seen["confirmed"] is True
    assert "addressed" in duck.events


def test_confirm_off_falls_back_to_the_old_path(quiet, monkeypatch):
    """PUFF_WAKE_CONFIRM=0 keeps today's behaviour — one transcription, no gate."""
    monkeypatch.setattr(service, "WAKE_CONFIRM", False)
    calls = []
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: calls.append(kw) or service.Capture(b"\x01"))
    stt = FakeSTT("no I told him Tuesday, not Thursday")

    assert service.handle_wake(FakeStream([], tail=300), stt, object(),
                               FakeWake(), []) is False
    assert len(stt.calls) == 1
    assert calls[0]["confirmed"] is False, "unconfirmed captures get the short cap"


# ── 2. barge-in ────────────────────────────────────────────────────────────
def test_a_confirmed_wake_aborts_a_running_capture(monkeypatch):
    monkeypatch.setattr(service, "BARGE_IN_AFTER", 0.0)
    monkeypatch.setattr(service, "BARGE_IN_COOLDOWN", 0.0)
    wake = FakeWake(fire_after=3)
    stream = FakeStream([], tail=800)          # someone talking, forever

    with pytest.raises(service.BargeIn) as caught:
        service.record_utterance(stream, first=True, max_utterance=20.0,
                                 wake=wake, confirm=lambda pcm: True)

    assert caught.value.pre == wake.buffer_bytes()


def test_an_unconfirmed_wake_mid_capture_does_not_abort(monkeypatch):
    """The wake model fires on the speaker's OWN voice — only words may abort."""
    monkeypatch.setattr(service, "BARGE_IN_AFTER", 0.0)
    monkeypatch.setattr(service, "BARGE_IN_COOLDOWN", 0.0)
    stream = FakeStream([800] * 40, tail=0)    # speech, then silence
    pcm = service.record_utterance(stream, first=True, max_utterance=20.0,
                                   wake=FakeWake(fire_after=1),
                                   confirm=lambda pcm: False)
    assert pcm is not None and len(pcm) > 0


def test_barge_in_discards_the_runaway_and_starts_a_fresh_capture(quiet, monkeypatch):
    """'hey puff pie, stop' wins over a recording already in flight."""
    chimes, _ = quiet
    monkeypatch.setattr(service, "BARGE_IN", True)
    attempts = []

    def fake_record(stream, first, wait_override=0.0, **kw):
        attempts.append(kw["max_utterance"])
        if len(attempts) == 1:
            raise service.BargeIn(b"NAME")
        return b"STOP"

    monkeypatch.setattr(service, "record_utterance", fake_record)
    wake = FakeWake()

    cap = service.record_capture(FakeStream([]), FakeSTT(), wake,
                                 first=True, confirmed=False)

    assert cap.pcm == b"STOP"
    assert cap.barged is True
    assert cap.pre == b"NAME", "the restarted capture carries the new wake audio"
    assert chimes == ["ready"], "the fresh capture is announced"
    assert wake.resets == 1
    assert attempts == [service.MAX_UTTERANCE_UNCONFIRMED,
                        service.MAX_UTTERANCE_CONFIRMED]


def test_barge_in_restarts_are_bounded(quiet, monkeypatch):
    monkeypatch.setattr(service, "BARGE_IN", True)
    monkeypatch.setattr(service, "BARGE_IN_MAX_RESTARTS", 2)

    def always_barge(stream, first, wait_override=0.0, **kw):
        raise service.BargeIn(b"NAME")

    monkeypatch.setattr(service, "record_utterance", always_barge)
    cap = service.record_capture(FakeStream([]), FakeSTT(), FakeWake(),
                                 first=True, confirmed=True)
    assert cap.pcm is None and cap.barged is True


def test_barge_in_off_keeps_the_wake_model_out_of_the_capture(monkeypatch):
    monkeypatch.setattr(service, "BARGE_IN", False)
    seen = {}

    def fake_record(stream, first, wait_override=0.0, **kw):
        seen.update(kw)
        return b""

    monkeypatch.setattr(service, "record_utterance", fake_record)
    service.record_capture(FakeStream([]), FakeSTT(), FakeWake(),
                           first=True, confirmed=True)
    assert seen["wake"] is None and seen["confirm"] is None


# ── 3. the cap ─────────────────────────────────────────────────────────────
def test_cap_is_twenty_seconds_confirmed_and_six_unconfirmed():
    assert service.max_utterance_for(True) == 20.0
    assert service.max_utterance_for(False) == 6.0


@pytest.mark.parametrize("confirmed,expected", [(True, 20.0), (False, 6.0)])
def test_a_nonstop_talker_is_cut_at_its_own_cap(confirmed, expected):
    stream = FakeStream([], tail=900)          # never stops talking
    pcm = service.record_utterance(
        stream, first=True, max_utterance=service.max_utterance_for(confirmed))
    seconds = len(pcm) / 2 / service.SAMPLE_RATE
    assert expected <= seconds < expected + BLOCK_SECONDS * 2


def test_the_eight_second_hero_sentence_survives():
    """8 s of speech, then a pause — whole, not truncated at 12 s."""
    speech = [900] * int(8 / BLOCK_SECONDS)
    stream = FakeStream(speech + [20] * 20, tail=20)
    pcm = service.record_utterance(stream, first=True, max_utterance=20.0)
    seconds = len(pcm) / 2 / service.SAMPLE_RATE
    assert 8.0 <= seconds <= 9.5, "the sentence plus the silence hang, unclipped"


# ── 4. VAD floor calibration ───────────────────────────────────────────────
def test_threshold_is_the_floor_plus_margin_but_never_below_the_minimum():
    assert service.calibrated_threshold(200, 100, 80) == 280
    assert service.calibrated_threshold(10, 100, 80) == 100
    assert service.calibrated_threshold(0, 100, 80) == 100


def test_ducked_music_ends_the_utterance_within_the_silence_hang(monkeypatch):
    """The runaway's root cause: ambient ABOVE a fixed threshold is never silence."""
    monkeypatch.setattr(service, "VAD_CALIBRATE", True)
    music = 200                       # ducked radio, above PUFF_VAD_THRESHOLD=100
    speech = [1200] * 16              # 2 s of command over the music
    hang_blocks = int(service.SILENCE_HANG / BLOCK_SECONDS)
    stream = FakeStream([music] * 3 + speech, tail=music)

    pcm = service.record_utterance(stream, first=True, max_utterance=20.0)

    seconds = len(pcm) / 2 / service.SAMPLE_RATE
    assert seconds == pytest.approx(2.0 + service.SILENCE_HANG,
                                    abs=BLOCK_SECONDS * 2), (
        "the capture must end a hang after the words, not at the 20 s cap")
    assert seconds < 20.0
    assert len(pcm) / 2 / service.SAMPLE_RATE <= (
        len(speech) + hang_blocks + 2) * BLOCK_SECONDS


def test_without_calibration_the_same_room_runs_to_the_cap(monkeypatch):
    """Proof the fix is what ends it: same audio, calibration off."""
    monkeypatch.setattr(service, "VAD_CALIBRATE", False)
    stream = FakeStream([200] * 3 + [1200] * 16, tail=200)
    pcm = service.record_utterance(stream, first=True, max_utterance=6.0)
    assert len(pcm) / 2 / service.SAMPLE_RATE >= 6.0


def test_a_one_breath_command_is_never_calibrated_away(monkeypatch):
    """Speech in the calibration window stops calibration, it does not set the floor."""
    monkeypatch.setattr(service, "VAD_CALIBRATE", True)
    stream = FakeStream([2000] * 8, tail=20)      # talking from the first block
    pcm = service.record_utterance(stream, first=True, max_utterance=20.0)
    seconds = len(pcm) / 2 / service.SAMPLE_RATE
    assert seconds >= 1.0, "every word of a one-breath command is captured"


def test_calibration_does_not_eat_the_wait_for_speech_window(monkeypatch):
    """A silent room still times out and returns None."""
    monkeypatch.setattr(service, "VAD_CALIBRATE", True)
    monkeypatch.setattr(service, "START_TIMEOUT", 0.01)
    assert service.record_utterance(FakeStream([], tail=20), first=True) is None


# ── 5. the fragment guard is untouched ─────────────────────────────────────
def test_fragment_extension_still_opens_one_bounded_window(monkeypatch):
    """extend_if_fragment's contract with record_utterance must not drift."""
    calls = []

    def strict_record(stream, first, wait_override=0.0):
        # No new keywords: the fragment path is deliberately unchanged.
        calls.append((first, wait_override))
        return b"\x00\x01"

    monkeypatch.setattr(service, "record_utterance", strict_record)
    monkeypatch.setattr(service, "normalize_utterance", lambda pcm, **kw: pcm)
    monkeypatch.setattr(service, "FRAGMENT_EXTEND", 1.5)

    out = service.extend_if_fragment(object(), FakeSTT("and Wednesday too"),
                                     "every Tuesday.")

    assert calls == [(False, 1.5)]
    assert out == "every Tuesday and Wednesday too"


def test_an_override_window_skips_calibration(monkeypatch):
    """The 1.5 s extension is too short to spend 0.3 s measuring the room."""
    monkeypatch.setattr(service, "VAD_CALIBRATE", True)
    logged = []
    monkeypatch.setattr(service.logger, "info",
                        lambda fmt, *a: logged.append(fmt % a if a else fmt))
    stream = FakeStream([900] * 8, tail=20)
    service.record_utterance(stream, first=False, wait_override=1.5)
    assert not any("calibrated" in line for line in logged)


# ── C1: a capture that hit the cap says so to the brain ──────────────────

def test_a_capture_that_hits_the_cap_sets_the_flag(monkeypatch):
    monkeypatch.setattr(service, "VAD_CALIBRATE", False)
    monkeypatch.setattr(service, "LAST_CAPTURE_TRUNCATED", False)
    stream = FakeStream([200] * 3 + [1200] * 16, tail=1200)
    service.record_utterance(stream, first=True, max_utterance=1.0)
    assert service.LAST_CAPTURE_TRUNCATED is True
    assert service.brain_payload([{"role": "user", "content": "hi"}]) == {
        "messages": [{"role": "user", "content": "hi"}], "truncated": True}


def test_a_capture_that_ended_on_silence_sends_no_flag(monkeypatch):
    monkeypatch.setattr(service, "VAD_CALIBRATE", False)
    monkeypatch.setattr(service, "LAST_CAPTURE_TRUNCATED", True)
    hang = int(service.SILENCE_HANG / BLOCK_SECONDS) + 2
    stream = FakeStream([1200] * 8 + [20] * hang, tail=20)
    service.record_utterance(stream, first=True, max_utterance=30.0)
    assert service.LAST_CAPTURE_TRUNCATED is False, (
        "each capture reports its OWN ending, not the one before it")
    assert service.brain_payload([]) == {"messages": []}


def test_the_payload_builder_can_be_told_directly():
    assert service.brain_payload([], truncated=True)["truncated"] is True
    assert "truncated" not in service.brain_payload([], truncated=False)


# ── the two ways in that need no wake word ────────────────
class PlayingDuck(StubDuck):
    """A duck policy that reports the radio is on."""

    def playback_active(self):
        return True


def _wire(monkeypatch, *, spoken):
    """Stub the capture + the exchange, and record what converse was handed."""
    monkeypatch.setattr(service, "WAKE_CONFIRM", True)
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **kw: service.Capture(b"\x01\x02"))
    monkeypatch.setattr(service, "converse",
                        lambda *a, **kw: spoken.append(
                            (kw.get("initial_command"), kw.get("address"),
                             kw.get("followup"))))


def test_a_request_inside_the_window_is_acted_on_with_no_wake_word(
        quiet, monkeypatch):
    """21:20:32 "Add a reminder to do the dish." — and the FULL sentence is
    what reaches the brain, captured with the wake path's own bridge/resume."""
    spoken = []
    _wire(monkeypatch, spoken=spoken)
    service.open_followup_window("Added a task.", now=service.time.monotonic())
    stt = FakeSTT("Add a reminder to do the dish.",
                  "Add a reminder to do the dishes tonight before 11.",
                  conf=-0.60)

    addressed = service.handle_wake(FakeStream([], tail=300), stt, object(),
                                    FakeWake(), [])

    assert addressed is True
    assert spoken == [("add a reminder to do the dishes tonight before 11",
                       "followup", True)]


def test_room_talk_inside_the_window_is_ignored_and_does_not_re_open_it(
        quiet, monkeypatch):
    spoken = []
    _wire(monkeypatch, spoken=spoken)
    now = service.time.monotonic()
    service.open_followup_window("It is ten past three.", now=now)
    stt = FakeSTT("So, Casey can't come.", conf=-0.56)

    addressed = service.handle_wake(FakeStream([], tail=300), stt, object(),
                                    FakeWake(), [])

    assert addressed is False
    assert spoken == []
    # the window keeps its ORIGINAL deadline — room talk does not prop it open
    assert service.followup_window_open(now=now + 9.0) is True
    assert service.followup_window_open(now=now + 11.0) is False


def test_a_closed_window_sends_a_request_straight_back_to_the_wake_word(
        quiet, monkeypatch):
    spoken = []
    _wire(monkeypatch, spoken=spoken)
    stt = FakeSTT("Can you find a different jazz radio?", conf=-0.39)

    addressed = service.handle_wake(FakeStream([], tail=300), stt, object(),
                                    FakeWake(), [])

    assert (addressed, spoken) == (False, [])


def test_stop_the_music_needs_no_window_and_no_name_while_she_plays(
        quiet, monkeypatch):
    """21:17:16: the radio was on and "stop the music." was thrown away."""
    spoken = []
    _wire(monkeypatch, spoken=spoken)
    monkeypatch.setattr(service, "_DUCK", PlayingDuck())
    stt = FakeSTT("stop the music.", conf=-0.65)

    addressed = service.handle_wake(FakeStream([], tail=400), stt, object(),
                                    FakeWake(), [])

    assert addressed is True
    assert spoken == [("stop the music.", "media-stop", None)]


def test_stop_the_music_with_nothing_playing_is_just_room_talk(
        quiet, monkeypatch):
    spoken = []
    _wire(monkeypatch, spoken=spoken)          # quiet's StubDuck: nothing plays
    stt = FakeSTT("stop the music.", conf=-0.65)

    addressed = service.handle_wake(FakeStream([], tail=400), stt, object(),
                                    FakeWake(), [])

    assert (addressed, spoken) == (False, [])
