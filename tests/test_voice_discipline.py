"""The voice stops when told to, and stays quiet otherwise.

Three behaviours are pinned here:

* **A wake is confirmed, not guessed.** The stage-2 confirm must not be biased
  toward the wake name, or Whisper writes the name onto room noise
  (``heard="Puff"``, ``"Puff Puff Pie"``) at ``conf <= -0.79``, where the
  decoder itself is guessing. A two-pass confirm with a confidence floor
  rejects those.
* **Room talk is not a follow-up.** Phrases such as "So, Casey can't come.",
  "Yep." and "Stop." heard in the hands-free window must not be acted on,
  or every reply re-opens the window and room talk chains turn after turn.
* **Barge-in works while the voice is speaking**, so "stop" cuts a reply
  mid-sentence instead of waiting for it to finish.

No mic, no model, no PulseAudio: fake stream, fake transcriber, fake player.
"""

import numpy as np
import pytest

from puffpai.voice import service


# ── doubles ────────────────────────────────────────────────────────────────
def block(level: float) -> np.ndarray:
    return np.full(service.BLOCK, int(level), dtype=np.int16)


class ScriptedSTT:
    """Returns ``(text, conf)`` pairs in order, exposing conf like Whisper."""

    supports_bias = True

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []               # the bias each call was given ("" = none)
        self.last_avg_logprob = None

    def transcribe(self, pcm, bias=""):
        self.calls.append(bias)
        if not self.turns:
            self.last_avg_logprob = None
            return ""
        text, conf = self.turns.pop(0)
        self.last_avg_logprob = conf
        return text


class FakePlayer:
    """A paplay handle: finishes after ``blocks`` polls unless killed first."""

    def __init__(self, blocks=8):
        self.left = blocks
        self.returncode = None
        self.terminated = False

    def poll(self):
        if self.returncode is None and self.left <= 0:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


class PlaybackStream:
    """Mic blocks handed out while the player runs; each read advances it."""

    def __init__(self, player, levels, tail=0):
        self.player = player
        self.levels = list(levels)
        self.tail = tail
        self.reads = 0

    def read(self, n):
        self.reads += 1
        self.player.left -= 1
        level = self.levels.pop(0) if self.levels else self.tail
        return block(level), False


class FakeWake:
    def __init__(self, scores=()):
        self.scores = list(scores)
        self.resets = 0

    def push(self, samples):
        return self.scores.pop(0) if self.scores else 0.0

    def buffer_bytes(self):
        return b"\x01\x02"

    def reset(self):
        self.resets += 1


class FakeTTS:
    sample_rate = 22050

    def speak(self, text):
        return b"\x00\x00" * 100


@pytest.fixture(autouse=True)
def _shut_the_followup_window():
    """The hands-free window is MODULE state (it spans wakes by construction).

    Left open by one test it leaks into the next — and into other test files,
    which is exactly how a "room conversation never reaches a capture" test
    started seeing a capture. Every test starts and ends with it shut.
    """
    service.close_followup_window()
    service.reset_followup_chain()
    yield
    service.close_followup_window()
    service.reset_followup_chain()


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    # Every state file this module writes (voice_state.json, voice_control.json)
    # lands in tmp_path — a test must never create /data/puffpai/state.
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "play_ready", lambda: None)
    monkeypatch.setattr(service, "play_sleep", lambda: None)
    monkeypatch.setattr(service, "flush_stream", lambda s: None)
    monkeypatch.setattr(service, "normalize_utterance", lambda pcm, **kw: pcm)
    monkeypatch.setattr(service, "capture_fixed", lambda stream, secs: b"\x01\x02")
    monkeypatch.setattr(service, "_tts_wav", lambda tts, text: "/tmp/fake.wav")
    monkeypatch.setattr(service, "_unlink", lambda path: None)


# ═══ 1. the name must be REALLY heard ══════════════════════════════════════
#: Ten confirmed wakes, with what the BIASED pass
#: wrote and how sure it was. Every one of them was the room, not a person
#: addressing it. Pass 1 (unbiased) is what the mic honestly produced.
REPLAY_HALLUCINATIONS = [
    ("", "Puff", -0.79),
    ("yeah", "Puff Puff Pie", -0.85),
    ("so", "Hi Puff Pie", -0.91),
    ("hmm", "Puff", -1.02),
    ("uh", "Puff Pie", -0.80),
    ("okay", "Puff Puff Pie", -1.20),
    ("right", "Hi Puff Pie", -0.95),
    ("", "Hello my name is Puff Pie and I am here to submit to you ple", -0.83),
    ("I didn't write", "I didn't write so when you get Puff", -0.88),
    ("well", "Puff", -0.79),
]

#: …and two genuine wakes, at the confidence a person standing in front of
#: the box actually produces.
REPLAY_REAL = [
    ("Hey Puff Pie", -0.44),
    ("A puff pie some music", -0.50),
]


@pytest.mark.parametrize("pass1,pass2,conf2", REPLAY_HALLUCINATIONS)
def test_the_mornings_hallucinated_wakes_are_all_rejected(pass1, pass2, conf2):
    ok, _text, which, reason, _conf = service.confirm_decision(
        pass1, -0.9, pass2, conf2)
    assert ok is False
    assert which == 2
    assert reason in ("low-confidence", "hotword-run-on")


@pytest.mark.parametrize("heard,conf", REPLAY_REAL)
def test_a_real_wake_still_confirms_on_the_unbiased_pass(heard, conf):
    ok, text, which, reason, _conf = service.confirm_decision(heard, conf)
    assert (ok, text, which, reason) == (True, heard, 1, "wakeword")


def test_a_hotwords_match_is_accepted_when_it_is_short_and_confident():
    ok, text, which, reason, conf = service.confirm_decision(
        "hey what's that", -0.55, "hey puff pie", -0.40)
    assert (ok, text, which, reason, conf) == (
        True, "hey puff pie", 2, "wakeword", -0.40)


def test_even_an_unbiased_name_is_refused_below_the_hard_floor():
    ok, _text, which, reason, _conf = service.confirm_decision(
        "hey puff pie", -1.30)
    assert (ok, which, reason) == (False, 1, "low-confidence")


def test_a_long_hotwords_assisted_match_is_the_bias_running_on():
    ok, _text, _which, reason, _conf = service.confirm_decision(
        "and then", -0.5, "hello my name is puff pie and I am here", -0.20)
    assert (ok, reason) == (False, "hotword-run-on")


@pytest.mark.parametrize("pass1", ["", "   "])
def test_silence_never_earns_a_second_biased_look(pass1):
    """Biasing an empty pass is precisely how "Puff" gets written onto a fan."""
    assert service.hotwords_pass_allowed(pass1) is False


def test_a_sentence_from_the_room_never_earns_a_second_look():
    assert service.hotwords_pass_allowed(
        "no I told him Tuesday not Thursday") is False


def test_a_short_nameless_pass_is_exactly_what_pass_two_is_for():
    assert service.hotwords_pass_allowed("hey what's that") is True


def test_a_name_already_heard_needs_no_second_look():
    assert service.hotwords_pass_allowed("hey puff pie") is False


def test_the_confirm_and_reject_lines_say_which_pass_decided(quiet, caplog,
                                                             monkeypatch):
    caplog.set_level("INFO")
    # The two-pass path is OFF by default; this test is
    # about what the lines SAY when somebody turns the bias back on.
    monkeypatch.setattr(service, "WAKE_CONFIRM_HOTWORDS", "Hey Puff Pie")
    monkeypatch.setattr(service, "record_capture",
                        lambda *a, **k: service.Capture(pcm=b""))
    monkeypatch.setattr(service, "converse", lambda *a, **k: None)

    stream = PlaybackStream(FakePlayer(blocks=10**6), [])
    service.handle_wake(stream, ScriptedSTT(("hmm", -0.9), ("Puff", -1.02)),
                        FakeTTS(), FakeWake(), [])
    service.handle_wake(stream, ScriptedSTT(("hey puff pie", -0.44),
                                            ("hey puff pie, what time is it", -0.4)),
                        FakeTTS(), FakeWake(), [])

    rejected = [m for m in caplog.messages if m.startswith("wake=rejected")][0]
    confirmed = [m for m in caplog.messages if m.startswith("wake=confirmed")][0]
    assert "pass=2" in rejected and "reason=low-confidence" in rejected
    assert "conf=-1.02" in rejected
    assert "pass=1" in confirmed and "conf=-0.44" in confirmed


# ═══ 2. a follow-up window only when she ASKED ═════════════════════════════
@pytest.fixture
def conversation(monkeypatch, quiet):
    """converse() with the mic, the brain and the speaker stubbed out."""
    monkeypatch.setattr(service, "FOLLOWUP", True)
    monkeypatch.setattr(service, "MAX_FOLLOWUPS", 3)
    monkeypatch.setattr(service, "FOLLOWUP_MODE", "question")
    monkeypatch.setattr(service, "LAST_REPLY_PENDING", False)
    monkeypatch.setattr(service, "extend_if_fragment", lambda s, stt, c: c)
    monkeypatch.setattr(service, "say_reply",
                        lambda tts, text, *a, **k: state["interrupt"].pop(0)
                        if state["interrupt"] else "")

    state = {"asked": [], "replies": [], "captures": 0, "interrupt": []}

    def _brain(history, text):
        state["asked"].append(text)
        return state["replies"].pop(0) if state["replies"] else "Done."

    def _capture(*a, **kw):
        state["captures"] += 1
        return service.Capture(b"\x01\x02")

    monkeypatch.setattr(service, "ask_brain", _brain)
    monkeypatch.setattr(service, "record_capture", _capture)
    return state


def test_a_statement_closes_the_turn_and_the_room_gets_no_window(conversation):
    conversation["replies"] = ["It is ten past three."]
    stt = ScriptedSTT(("So, Casey can't come.", -0.6))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="what time is it")

    assert conversation["asked"] == ["what time is it"]
    assert conversation["captures"] == 0, "no window may open after an answer"


def test_a_question_opens_exactly_one_window(conversation):
    """"set an alarm for seven" → "AM or PM?" → "PM" needs no wake word."""
    conversation["replies"] = ["AM or PM?", "Alarm set for seven PM."]
    stt = ScriptedSTT(("PM", -0.35), ("and then she said no", -0.4))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="set an alarm for seven")

    assert conversation["asked"] == ["set an alarm for seven", "PM"]
    assert conversation["captures"] == 1, "the window is single use"


def test_a_second_question_may_re_open_it(conversation):
    conversation["replies"] = ["AM or PM?", "Which alarm — seven or eight?",
                               "Done."]
    stt = ScriptedSTT(("PM", -0.3), ("seven", -0.3))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="set an alarm")

    assert conversation["asked"] == ["set an alarm", "PM", "seven"]


@pytest.mark.parametrize("closing", [
    "stop", "Stop.", "never mind", "nevermind", "cancel", "that's all",
    "thanks", "Thank you.", "no thanks",
])
def test_closing_words_end_the_window_without_a_word(conversation, closing):
    conversation["replies"] = ["Anything else?"]
    stt = ScriptedSTT((closing, -0.3))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="what's the weather")

    assert conversation["asked"] == ["what's the weather"], "no brain call"


def test_an_uncertain_capture_in_the_window_is_the_room_talking(conversation):
    """Acted-on captures can score between -0.7 and -1.2."""
    conversation["replies"] = ["AM or PM?"]
    stt = ScriptedSTT(("I'm ready. I'm ready.", -0.95))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="set an alarm")

    assert conversation["asked"] == ["set an alarm"]


def test_an_empty_capture_in_the_window_is_ignored(conversation):
    conversation["replies"] = ["AM or PM?"]
    stt = ScriptedSTT(("", None))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="set an alarm")

    assert conversation["asked"] == ["set an alarm"]


def test_the_old_always_on_behaviour_is_still_reachable(conversation,
                                                        monkeypatch):
    monkeypatch.setattr(service, "FOLLOWUP_MODE", "always")
    conversation["replies"] = ["It is ten past three.", "Sunny."]
    stt = ScriptedSTT(("and the weather", -0.3), ("room talk", -0.9))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="what time is it")

    assert conversation["asked"] == ["what time is it", "and the weather"]


def test_a_pending_clarifier_from_the_brain_also_opens_a_window():
    assert service.followup_invited("Seven what?", pending=False) is True
    assert service.followup_invited("Alarm set.", pending=False) is False
    assert service.followup_invited("Alarm set.", pending=True) is True


# ═══ 3. "stop" while she is speaking ═══════════════════════════════════════
@pytest.fixture
def playback(monkeypatch, quiet):
    """speak_interruptible() over a fake player and a fake mic."""
    players = []

    def _start(path):
        player = FakePlayer(blocks=8)
        players.append(player)
        return player

    monkeypatch.setattr(service, "_paplay_async", _start)
    monkeypatch.setattr(service, "TTS_INTERRUPT_GRACE", 0.0)
    return players


def _interrupt(monkeypatch, playback, heard, *, levels=(800,), grace=0.0):
    monkeypatch.setattr(service, "TTS_INTERRUPT_GRACE", grace)
    # 40 blocks (5 s of speech): the interrupt CHECK
    # reads the mic itself, so a check costs blocks of playback here. A player
    # that runs out mid-check would be "finished", not "interrupted".
    player = FakePlayer(blocks=40)
    monkeypatch.setattr(service, "_paplay_async", lambda path: player)
    stream = PlaybackStream(player, list(levels), tail=levels[-1])
    wake = FakeWake([0.0, 0.9] + [0.0] * 20)
    outcome = service.speak_interruptible(
        FakeTTS(), "a long answer about the weather", stream,
        wake, ScriptedSTT((heard, -0.3)))
    return outcome, player


def test_stop_over_her_voice_kills_playback_and_says_nothing(
        monkeypatch, playback, caplog):
    caplog.set_level("INFO")
    outcome, player = _interrupt(monkeypatch, playback, "Stop.")

    assert outcome == "stop"
    assert player.terminated is True
    assert any("tts=interrupted reason=stop" in m for m in caplog.messages)


@pytest.mark.parametrize("heard", [
    "stop", "be quiet", "shut up", "that's enough", "okay okay", "cancel",
    "never mind", "enough already",
])
def test_the_stop_vocabulary(monkeypatch, playback, heard):
    outcome, player = _interrupt(monkeypatch, playback, heard)
    assert (outcome, player.terminated) == ("stop", True)


def test_her_name_mid_sentence_is_a_fresh_wake(monkeypatch, playback, caplog):
    caplog.set_level("INFO")
    outcome, player = _interrupt(monkeypatch, playback, "hey puff pie")

    assert outcome == "name"
    assert player.terminated is True
    assert any("tts=interrupted reason=name" in m for m in caplog.messages)


def test_ordinary_room_talk_does_not_interrupt_her(monkeypatch, playback):
    outcome, player = _interrupt(monkeypatch, playback,
                                 "and then we went to the store")
    assert outcome == ""
    assert player.terminated is False
    assert player.poll() == 0, "she finished the sentence"


def test_her_own_onset_in_the_first_moments_is_ignored(monkeypatch, playback):
    """The guard against self-trigger: nothing counts inside the grace."""
    outcome, player = _interrupt(monkeypatch, playback, "Stop.", grace=60.0)
    assert (outcome, player.terminated) == ("", False)


def test_a_quiet_block_never_counts_as_someone_talking_over_her(monkeypatch,
                                                                playback):
    """Mic RMS during playback sits near 25 (the XVF3800 cancels her voice);
    an interrupt has to be a person in the room, well above the floor."""
    outcome, player = _interrupt(monkeypatch, playback, "Stop.", levels=(40,))
    assert (outcome, player.terminated) == ("", False)


def test_a_stop_interrupt_ends_the_exchange_with_no_reply(conversation):
    conversation["replies"] = ["Here is your daily briefing. It is..."]
    conversation["interrupt"] = ["stop"]
    stt = ScriptedSTT(("anything at all", -0.3))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="give me my daily briefing")

    assert conversation["asked"] == ["give me my daily briefing"]
    assert conversation["captures"] == 0


def test_her_name_mid_reply_starts_a_new_turn(conversation):
    conversation["replies"] = ["Here is your daily briefing. It is...",
                               "Sunny and sixty."]
    conversation["interrupt"] = ["name", ""]
    stt = ScriptedSTT(("what's the weather", -0.3))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="give me my daily briefing")

    assert conversation["asked"] == ["give me my daily briefing",
                                     "what's the weather"]
    assert conversation["captures"] == 1


# ═══ 4. "stop" for the music still reaches the brain ═══════════════════════
def test_stop_after_her_name_is_forwarded_to_the_media_fastpath(conversation):
    """``is_stop`` used to swallow it here, so "hey puff pie, stop" ended the
    exchange and left the radio playing."""
    conversation["replies"] = ["Okay - stopping any music or radio."]
    stt = ScriptedSTT(("", None))

    service.converse(PlaybackStream(FakePlayer(10**6), []), stt, FakeTTS(), [],
                     initial_command="stop")

    assert conversation["asked"] == ["stop"]
    assert conversation["captures"] == 0, "a stop answer opens no window"


@pytest.mark.parametrize("phrase", ["stop", "Stop.", "pause", "just stop"])
def test_the_media_stop_phrases(phrase):
    assert service.is_media_stop(phrase) is True


@pytest.mark.parametrize("phrase", ["never mind", "go away", "that's all",
                                    "stop listening", "shut up"])
def test_other_stop_phrases_still_just_end_the_turn(phrase):
    assert service.is_media_stop(phrase) is False
    assert service.is_stop(phrase) is True


# ═══ 5. a stop that actually stops ════════════════════════
# A false wake (conf=-0.60, heard="Here in Puff") can produce a command like
# "here is this" and a long spoken answer that a single "stop" has to be able
# to end. Gating the interrupt on the WAKE MODEL firing during playback is not
# enough: one syllable does not trip a model trained on "hey puffpai". Four
# separate answers below.

class LevelStream:
    """A mic whose block levels come from a script, for the energy gate."""

    def __init__(self, player, levels, tail=0):
        self.player = player
        self.levels = list(levels)
        self.tail = tail
        self.reads = 0

    def read(self, n):
        self.reads += 1
        self.player.left -= 1
        level = self.levels.pop(0) if self.levels else self.tail
        return block(level), False


def _speak_over(monkeypatch, levels, heard, *, blocks=40, wake_scores=(),
                tail=None, floor=None):
    """Run speak_interruptible over a scripted mic. Wake model stays SILENT."""
    player = FakePlayer(blocks=blocks)
    monkeypatch.setattr(service, "_paplay_async", lambda path: player)
    monkeypatch.setattr(service, "TTS_INTERRUPT_GRACE", 0.0)
    stream = LevelStream(player, levels,
                         tail=levels[-1] if tail is None else tail)
    outcome = service.speak_interruptible(
        FakeTTS(), "a long answer nobody asked for", stream,
        FakeWake(wake_scores), ScriptedSTT((heard, -0.3)), floor=floor)
    return outcome, player, stream


def test_energy_alone_opens_the_check_the_wake_model_never_fires(
        monkeypatch, quiet, caplog):
    """The earlier failure, inverted: FakeWake scores 0.0 throughout."""
    caplog.set_level("INFO")
    outcome, player, _ = _speak_over(monkeypatch, [900, 900], "Stop!")

    assert (outcome, player.terminated) == ("stop", True)
    assert any("tts=interrupted reason=stop" in m for m in caplog.messages)


def test_a_single_loud_block_is_not_yet_a_run(monkeypatch, quiet):
    """200 ms of loud audio buys the transcription; one 125 ms block does not."""
    outcome, player, _ = _speak_over(monkeypatch, [900, 20, 900, 20],
                                     "Stop!", blocks=4, tail=20)
    assert (outcome, player.terminated) == ("", False)


@pytest.mark.parametrize("heard", [
    "Stop!", "Stop.", "stop it", "stop stop", "okay stop", "Puff, stop",
    "puff pie stop", "quiet", "be quiet", "hush", "shush", "shut up",
    "enough", "that's enough", "cancel", "never mind", "nevermind", "pause",
    "Stopped", "Top", "Stahp",
])
def test_the_tolerant_stop_set(monkeypatch, quiet, heard):
    """Whisper mangles a shouted syllable; the set forgives the mangling."""
    outcome, player, _ = _speak_over(monkeypatch, [900, 900], heard)
    assert (outcome, player.terminated) == ("stop", True)


@pytest.mark.parametrize("heard", [
    "I stopped by the shop on the way home",     # "stopped" mid-sentence
    "the top shelf in the kitchen cupboard",     # "top" mid-sentence
    "and then we went to the store",
])
def test_room_talk_over_her_is_not_a_stop(monkeypatch, quiet, heard):
    outcome, player, _ = _speak_over(monkeypatch, [900, 900], heard)
    assert (outcome, player.terminated) == ("", False)


def test_her_name_over_the_top_is_still_a_fresh_turn(monkeypatch, quiet):
    outcome, player, _ = _speak_over(monkeypatch, [900, 900], "hey puff pie")
    assert (outcome, player.terminated) == ("name", True)


def test_every_check_is_logged_pass_or_fail(monkeypatch, quiet, caplog):
    """A failure nobody can see is a failure nobody can fix, so every
    interrupt check must be logged."""
    caplog.set_level("INFO")
    _speak_over(monkeypatch, [900, 900], "and then we went to the store")

    checks = [m for m in caplog.messages if m.startswith("tts=check")]
    assert len(checks) >= 1
    assert "match=none" in checks[0] and "rms=" in checks[0] and "ms=" in checks[0]


def test_a_matching_check_says_so(monkeypatch, quiet, caplog):
    caplog.set_level("INFO")
    _speak_over(monkeypatch, [900, 900], "Stop!")
    checks = [m for m in caplog.messages if m.startswith("tts=check")]
    assert "match=stop" in checks[0]


def test_the_cooldown_stops_a_check_per_block(monkeypatch, quiet, caplog):
    """A sentence spoken over her is loud for seconds; that must not cost a
    transcription every 250 ms."""
    caplog.set_level("INFO")
    monkeypatch.setattr(service, "TTS_INTERRUPT_COOLDOWN", 60.0)
    _speak_over(monkeypatch, [900] * 20, "and then we went to the store",
                blocks=20)
    assert len([m for m in caplog.messages if m.startswith("tts=check")]) == 1


def test_the_self_rms_of_her_own_voice_is_measured_once_per_utterance(
        monkeypatch, quiet, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(service, "TTS_INTERRUPT_GRACE", 10.0)  # never exits
    player = FakePlayer(blocks=6)
    monkeypatch.setattr(service, "_paplay_async", lambda path: player)
    service.speak_interruptible(FakeTTS(), "hello", LevelStream(player, [25] * 6),
                                FakeWake(), ScriptedSTT())
    # Grace never ended, so no measurement line and no check — exactly the
    # guard against her interrupting herself.
    assert not [m for m in caplog.messages if m.startswith("tts=selfrms")]

    caplog.clear()
    _speak_over(monkeypatch, [20, 20, 900, 900], "Stop!", blocks=10, tail=900)
    selfrms = [m for m in caplog.messages if m.startswith("tts=selfrms")]
    assert len(selfrms) == 1, "one measurement per utterance, not per block"


def test_the_gate_is_derived_from_her_own_level_when_the_aec_is_not_working():
    """If her own voice measures above the absolute floor, the gate follows it
    — otherwise she interrupts herself on every sentence."""
    assert service.interrupt_gate(None, 0.0) == service.TTS_INTERRUPT_RMS
    assert service.interrupt_gate(None, 25.0) == service.TTS_INTERRUPT_RMS
    assert service.interrupt_gate(None, 400.0) == pytest.approx(640.0)
    # …and a noisy room still lifts it the old way.
    assert service.interrupt_gate(300.0, 0.0) == pytest.approx(600.0)


def test_a_gate_derived_from_a_leaky_aec_refuses_her_own_voice(monkeypatch,
                                                               quiet, caplog):
    """Grace measures 400 (a leaking canceller), so 400-level blocks after it
    are HER, not a person: gate = 1.6 x 400 = 640."""
    caplog.set_level("INFO")
    monkeypatch.setattr(service, "TTS_INTERRUPT_GRACE", 0.3)
    player = FakePlayer(blocks=10)
    monkeypatch.setattr(service, "_paplay_async", lambda path: player)
    outcome = service.speak_interruptible(
        FakeTTS(), "a long answer", LevelStream(player, [400] * 10),
        FakeWake(), ScriptedSTT(("Stop!", -0.3)))
    assert outcome == ""
    assert "p95=400 gate=640" in [m for m in caplog.messages
                                  if m.startswith("tts=selfrms")][0]


# ── the control channel: a stop with no microphone in it ──────────────────
def test_a_control_stop_kills_playback_within_a_poll(monkeypatch, quiet,
                                                     tmp_path, caplog):
    import json
    import time as _time
    caplog.set_level("INFO")
    monkeypatch.setattr(service, "TTS_INTERRUPT_GRACE", 0.0)
    player = FakePlayer(blocks=10 ** 6)          # she would talk forever
    monkeypatch.setattr(service, "_paplay_async", lambda path: player)
    (tmp_path / "voice_control.json").write_text(
        json.dumps({"cmd": "stop", "ts": _time.time() + 1}), encoding="utf-8")

    t0 = _time.monotonic()
    outcome = service.speak_interruptible(
        FakeTTS(), "a very long answer", LevelStream(player, [20] * 10, tail=20),
        FakeWake(), ScriptedSTT())
    elapsed = (_time.monotonic() - t0) * 1000.0

    assert (outcome, player.terminated) == ("stop", True)
    assert elapsed < 150, "a control stop must not wait for anything"
    assert any("tts=interrupted reason=control" in m for m in caplog.messages)
    assert not (tmp_path / "voice_control.json").exists(), "consumed"


def test_a_stale_control_file_does_not_stop_the_next_sentence(monkeypatch,
                                                              quiet, tmp_path):
    import json
    import time as _time
    (tmp_path / "voice_control.json").write_text(
        json.dumps({"cmd": "stop", "ts": _time.time() - 300}), encoding="utf-8")

    outcome, player, _ = _speak_over(monkeypatch, [20] * 6, "", blocks=6,
                                     tail=20)
    assert (outcome, player.terminated) == ("", False)
    assert not (tmp_path / "voice_control.json").exists(), "cleared anyway"


def test_the_speaking_state_file_is_written_at_start_and_end(monkeypatch,
                                                             quiet, tmp_path):
    import json
    seen = []
    real = service.write_speaking_state

    def _spy(speaking, since):
        real(speaking, since)
        seen.append((speaking,
                     json.loads((tmp_path / "voice_state.json").read_text())))

    monkeypatch.setattr(service, "write_speaking_state", _spy)
    _speak_over(monkeypatch, [20] * 6, "", blocks=6, tail=20)

    assert [s for s, _ in seen] == [True, False]
    assert seen[0][1]["speaking"] is True and seen[0][1]["since"] > 0
    assert seen[-1][1]["speaking"] is False


# ── announcements are speech too ──────────────────────────────────────────
def test_an_announcement_can_be_stopped_like_any_other_sentence(monkeypatch,
                                                                quiet):
    """An earlier fix left the announce path on the deaf speak()."""
    spoken = []
    monkeypatch.setattr(service, "say_reply",
                        lambda tts, text, *a, **k: spoken.append(text) or "")

    class _Ann:
        @staticmethod
        def pending_speech():
            return [{"text": "Your timer is done.", "ts": "t1"}]

        @staticmethod
        def write_cursor(ts):
            pass

        @staticmethod
        def recent(n):
            return []

    class _Duck:
        def on_addressed(self):
            pass

        def restore(self):
            pass

    said = service.drain_announcements(
        FakeTTS(), ann=_Ann, duck=_Duck(), stream=object(),
        wake=FakeWake(), stt=ScriptedSTT())
    assert (said, spoken) == (1, ["Your timer is done."])


# ═══ 6. the junk-wake guard ═══════════════════════════════════════════════
@pytest.mark.parametrize("command", [
    "here is this", "Here is this.", "so anyway", "oh", "the other one",
    "yeah okay then",
])
def test_room_noise_behind_a_confirmed_wake_never_reaches_the_brain(command):
    assert service.is_junk_command(command) is True


@pytest.mark.parametrize("command", [
    "stop", "play jazz", "what time is it", "is it raining", "turn off",
    "set a timer", "who is he", "what's the weather like outside today",
    "read my messages",
])
def test_a_real_short_command_still_goes_through(command):
    assert service.is_junk_command(command) is False


def test_the_junk_guard_drops_the_turn_and_logs_it(conversation, caplog):
    """"here is this" must cost nothing."""
    caplog.set_level("INFO")
    service.converse(PlaybackStream(FakePlayer(10 ** 6), []),
                     ScriptedSTT(), FakeTTS(), [],
                     initial_command="here is this")

    assert conversation["asked"] == [], "the brain was never called"
    dropped = [m for m in caplog.messages if m.startswith("wake=dropped")]
    assert dropped and "reason=junk" in dropped[0]


def test_an_unconfirmed_follow_up_is_not_junk_filtered(conversation):
    """The guard is for hallucinated WAKES; a follow-up answer ("yes please")
    is a different thing and keeps its own rules."""
    conversation["replies"] = ["Done."]
    service.converse(PlaybackStream(FakePlayer(10 ** 6), []),
                     ScriptedSTT(), FakeTTS(), [],
                     initial_command="here is this", confirmed=False)
    assert conversation["asked"] == ["here is this"]


# ═══ 7. no biased confirm by default ══════════════════════════════════════
def test_the_hotwords_bias_is_off_unless_somebody_asks_for_it(monkeypatch):
    """The drop-in must be empty by default, and the code default agrees."""
    import importlib
    monkeypatch.delenv("PUFF_WAKE_CONFIRM_HOTWORDS", raising=False)
    reloaded = importlib.reload(service)
    try:
        assert reloaded.WAKE_CONFIRM_HOTWORDS == ""
    finally:
        importlib.reload(service)


# ═══ 8. follow-ups that sound like a request ═════════════
# Natural follow-ups after a reply. A follow-up window that opened only after a
# reply ending in "?" would drop these as ``wake=rejected reason=no-wakeword``.

#: Follow-up requests. Every one of these MUST be heard with no wake word.
TONIGHT_FOLLOWUPS = [
    ("Can you find a different jazz radio?", -0.39),
    ("stop the music.", -0.65),
    ("Can you play a different", -0.30),
    ("Add a reminder to do the dish.", -0.60),
    ("Play K exp radio", -0.78),
]

#: Room talk the follow-up window must not act on. Each line carries the
#: confidence such talk produces; "Did the bus?" is replayed at a CONFIDENT
#: -0.40, so it must be refused on its WORDS, not rescued by the conf floor.
MORNING_ROOM_TALK = [
    ("So, Casey can't come.", -0.56),
    ("I'm ready. I'm ready.", -0.87),
    ("Yep.", -0.49),
    ("Alright.", -0.93),
    ("What?", -0.68),
    ("Did the bus?", -0.40),
    ("My pace is so much money. You can illustrate them. Yeah.", -0.60),
]


@pytest.mark.parametrize("text,conf", TONIGHT_FOLLOWUPS)
def test_follow_up_requests_are_heard_without_the_wake_word(text, conf):
    assert service.followup_is_request(text, conf) is True
    assert service.classify_followup(text, conf) == "act"


@pytest.mark.parametrize("text,conf", MORNING_ROOM_TALK)
def test_this_mornings_room_talk_is_still_ignored(text, conf):
    assert service.followup_is_request(text, conf) is False
    assert service.classify_followup(text, conf) == "ignore"


def test_a_confident_room_sentence_that_opens_with_a_filler_is_still_the_room():
    """"So", "okay" and "and" are stripped — what is UNDER them decides."""
    assert service.followup_is_request("Okay, Casey can't come.", -0.3) is False
    assert service.followup_is_request("Okay, play some jazz", -0.3) is True


def test_an_uncertain_request_is_the_room():
    assert service.followup_is_request("play some jazz", -0.95) is False


def test_a_close_word_closes_the_window_and_never_reaches_the_brain():
    assert service.classify_followup("thanks", -0.2) == "close"
    assert service.classify_followup("never mind", -0.2) == "close"
    # ...but "stop the music" is an instruction, not a goodbye.
    assert service.classify_followup("stop the music.", -0.65) == "act"


def test_a_question_she_asked_still_takes_any_confident_answer():
    """The question path is kept: after "AM or PM?" the answer needs no opener."""
    assert service.classify_followup("PM", -0.35, question=True) == "act"
    assert service.classify_followup("PM", -0.35, question=False) == "ignore"


def test_question_mode_still_refuses_a_request(monkeypatch):
    monkeypatch.setattr(service, "FOLLOWUP_MODE", "question")
    assert service.classify_followup("play some jazz", -0.3) == "ignore"
    monkeypatch.setattr(service, "FOLLOWUP_MODE", "always")
    assert service.classify_followup("So, Casey can't come.", -0.56) == "act"


def test_the_default_mode_is_request(monkeypatch):
    import importlib
    monkeypatch.delenv("PUFF_FOLLOWUP_MODE", raising=False)
    monkeypatch.delenv("PUFF_FOLLOWUP_WINDOW", raising=False)
    monkeypatch.delenv("PUFF_FOLLOWUP_MAX_CHAIN", raising=False)
    reloaded = importlib.reload(service)
    try:
        assert reloaded.FOLLOWUP_MODE == "request"
        assert reloaded.FOLLOWUP_WINDOW == 10.0
        assert reloaded.FOLLOWUP_MAX_CHAIN == 3
    finally:
        importlib.reload(service)


# ── the window: 10 s, measured from the END of TTS ─────────────────────────
def test_the_window_runs_from_the_end_of_tts():
    service.open_followup_window("Playing Classic Vinyl HD.", now=1000.0)
    assert service.followup_window_open(now=1000.0 + 9.9) is True
    assert service.followup_window_open(now=1000.0 + 10.1) is False


def test_a_statement_now_opens_a_window_too():
    assert service.open_followup_window("Playing Classic Vinyl HD.",
                                        now=0.0) is True
    assert service.followup_question_pending() is False


def test_a_question_marks_the_window_as_awaiting_an_answer():
    service.open_followup_window("AM or PM?", now=0.0)
    assert service.followup_question_pending() is True


def test_wake_less_turns_are_capped_then_the_name_is_required():
    service.open_followup_window("Playing.", now=0.0)          # wake turn
    assert service.open_followup_window("Done.", now=0.0, chained=True) is True
    assert service.open_followup_window("Done.", now=0.0, chained=True) is True
    assert service.open_followup_window("Done.", now=0.0, chained=True) is False
    assert service.followup_window_open(now=0.0) is False
    # her name starts the chain over
    assert service.open_followup_window("Playing.", now=0.0) is True


# ── "stop the music" with no wake word, only while she is playing ──────────
def test_a_bare_media_stop_is_addressed_by_the_music_itself():
    assert service.is_bare_media_stop("stop the music.", -0.65, 379,
                                      playing=True) is True
    assert service.is_bare_media_stop("stop", -0.5, 400, playing=True) is True
    assert service.is_bare_media_stop("stop it now", -0.5, 400,
                                      playing=True) is False   # not the shape
    assert service.is_bare_media_stop("pause the radio", -0.5, 400,
                                      playing=True) is True


def test_a_media_stop_does_not_exist_when_nothing_is_playing():
    assert service.is_bare_media_stop("stop the music.", -0.65, 379,
                                      playing=False) is False


def test_a_media_stop_needs_to_be_said_toward_the_speakers():
    assert service.is_bare_media_stop("stop the music.", -0.65, 120,
                                      playing=True) is False   # too quiet
    assert service.is_bare_media_stop("stop the music.", -0.95, 379,
                                      playing=True) is False   # a guess


def test_a_sentence_about_stopping_is_not_a_media_stop():
    for text in ("I told him to stop the music at eleven",
                 "stop the timer", "we should stop"):
        assert service.is_bare_media_stop(text, -0.3, 500, playing=True) is False


def test_playback_active_never_raises():
    class Boom:
        def playback_active(self):
            raise RuntimeError("no pactl here")
    assert service.playback_active(Boom()) is False


# ── the junk guard may not touch an EXACT address ──────────────────────────
def test_an_exactly_addressed_command_is_never_dropped_as_junk(conversation):
    """21:19:49: ``address=exact`` and the command was thrown away anyway."""
    conversation["replies"] = ["Playing KEXP."]
    service.converse(PlaybackStream(FakePlayer(10**6), []), ScriptedSTT(),
                     FakeTTS(), [], initial_command="playk exp radio",
                     confirmed=True, address="exact")
    assert conversation["asked"] == ["play kexp radio"]


def test_the_junk_guard_still_holds_for_a_trusted_address(conversation):
    conversation["replies"] = ["Done."]
    service.converse(PlaybackStream(FakePlayer(10**6), []), ScriptedSTT(),
                     FakeTTS(), [], initial_command="here is this",
                     confirmed=True, address="trusted")
    assert conversation["asked"] == [], "room noise must not reach the brain"


# ── STT spelling repair ────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,fixed", [
    ("playk exp radio", "play kexp radio"),
    ("play k exp radio", "play kexp radio"),
    ("play k e x p radio", "play kexp radio"),
    ("play k-e-x-p radio", "play kexp radio"),
    ("play kex p radio", "play kexp radio"),
    ("play k x p radio", "play kexp radio"),
    ("play w o s u", "play wosu"),
    ("plaik some jazz", "play some jazz"),
    ("plake the radio", "play the radio"),
    # untouched: ordinary speech, and the case of words it does not repair
    ("add a reminder to do the dishes", "add a reminder to do the dishes"),
    ("set an alarm for seven PM", "set an alarm for seven PM"),
    ("what time is it", "what time is it"),
])
def test_the_stt_repair_table(raw, fixed):
    assert service.normalize_command(raw) == fixed
    assert service.normalize_command(fixed) == fixed, "must be idempotent"


# ── a bare tail is the weakest evidence there is ───────────────────────────
@pytest.mark.parametrize("heard,confirms", [
    ("Very over to the eye.", False),      # 18:13:18 — a sentence from the room
    ("Hey, pie, what time is it", True),   # address-shaped: opens with "hey"
    ("Pie.", True),
    ("pi.", True),
    ("Okay, pie, play some jazz please", True),
    ("I would like a slice of that pie.", False),
])
def test_a_bare_tail_only_confirms_on_address_shaped_audio(heard, confirms):
    assert service.tail_confirms(heard.lower(),
                                 service.WAKE_CONFIRM_TAILS) is confirms
    assert service.wake_word_heard(heard) is confirms
