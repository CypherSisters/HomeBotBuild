"""The voice unit's own journal must not write down PINs or vault secrets.

Earlier behaviour: ``puffpai-voice`` is a separate
process with its own durable log (user journald + the rotating
``/data/puffpai/state/logs/voice.log``) and it logged, in plaintext:

  * ``ADDRESSED: %r`` -- the FULL Whisper transcript of every confirmed wake,
    before the brain was even called, so a spoken PIN landed on disk;
  * ``COMMAND: %r`` and the bounded ``heard=`` excerpts, same;
  * ``PUFF: %s`` -- the FULL assistant reply of every turn, so the decrypted
    secret of a vault-unlock turn landed on disk;
  * the leak-guard warning (``reply[:100]``) and the TTS-empty diagnostic
    (``text[:60]``), which see the same reply;
  * ``ANNOUNCE (%s): %s`` -- any queued sentence, codes included.

The two sides are fixed differently, because they are not the same risk:

  * UTTERANCES stay readable. Those lines are how a missed wake or a failed
    stop is diagnosed, and "unlock the front door" carries no secret. They are
    withheld only when the text is PIN-SHAPED, or while a vault challenge is
    ARMED and the next thing said may BE the PIN (``vault_challenge_pending``
    on the ``/api/chat`` response).
  * REPLIES follow the backend's ``private`` flag -- the only thing that knows
    a vault tool ran -- and are filtered on their own content besides.

No real household content is used. Nothing here is hardware-proven: there is
no mic, no Whisper and no box in this test run.
"""

import importlib
import json
import logging

import pytest

from puffpai.voice import log_privacy as lp

# ── the sentences under test ────────────────────────────────────────────────
#: A PIN dictated in words, with the retrieval ask in the same breath.
PIN_SPOKEN = "my pin is four eight two one read my wifi password"
#: The miss that vocabulary tiers never caught: digits, no vault word.
PIN_DIGITS = "get wifi 4821"
#: Vault vocabulary with no PIN in it. READABLE: the reply is what is secret.
VOCAB_ONLY = "what's my wifi password"
ORDINARY = "turn on the porch light"
ORDINARY_REPLY = "Okay, porch light is on"
SECRET = "hunter2-sekret"
SECRET_REPLY = "Your wifi password is: " + SECRET
#: What the backend says when it arms a PIN challenge (api_server:4918 /
#: :9043, dashboard_display:577 -- all three carry "your PIN").
CHALLENGE_REPLY = "To access it, please reply with your PIN."
#: Words that must never appear in a log record for a PIN-shaped turn.
PIN_LEAK_WORDS = ("4821", "four eight two one", SECRET)


# ── driving the service without audio hardware ──────────────────────────────

@pytest.fixture
def svc(monkeypatch, tmp_path):
    """``puffpai.voice.service`` with config read from a controlled env.

    The module reads env into module-level constants at import time (see
    tests/test_voice_transcript_retention.py), so each variation needs a reload.
    ``PUFF_WAKE_CONFIRM=0`` is what makes ``handle_wake`` drivable with no
    wake model: the stage-2 confirm is skipped and the address decision is
    taken from the transcript the fake STT returns.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFF_WAKE_CONFIRM", "0")
    monkeypatch.setenv("PUFF_FOLLOWUP", "0")
    monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
    import puffpai.voice.service as service
    service = importlib.reload(service)
    yield service
    for key in ("PUFF_WAKE_CONFIRM", "PUFF_FOLLOWUP", "PUFF_LOG_TRANSCRIPTS"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(service)


class _Resp:
    """Stands in for the requests.Response from /api/chat."""

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _STT:
    """Stands in for WhisperSTT: returns a fixed transcript, exposes a score."""

    last_avg_logprob = -0.31

    def __init__(self, text=""):
        self._text = text

    def transcribe(self, pcm, bias=None):
        return self._text


class _Duck:
    def on_wake(self):
        pass

    def on_addressed(self):
        pass

    def on_unaddressed(self, *a, **k):
        pass

    def restore(self):
        pass

    def playback_active(self):
        return False


def _brain(svc, monkeypatch, reply, private=None, pending=False,
           challenge=None):
    """Point ``ask_brain`` at a canned /api/chat body. Returns the body."""
    body = {"model": "puffpai", "message": {"role": "assistant",
                                            "content": reply}, "done": True}
    if private is not None:
        body["private"] = private
    if challenge is not None:
        body["vault_challenge_pending"] = challenge
    if pending:
        body["pending_clarifier"] = True
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(body))
    return body


def _quiet_audio(svc, monkeypatch):
    monkeypatch.setattr(svc, "play_ready", lambda: None)
    monkeypatch.setattr(svc, "play_sleep", lambda: None)
    monkeypatch.setattr(svc, "say_reply", lambda *a, **k: "")


def _turn(svc, monkeypatch, said, reply, private=None, challenge=None):
    """One full exchange through the REAL converse() + ask_brain()."""
    _brain(svc, monkeypatch, reply, private=private, challenge=challenge)
    _quiet_audio(svc, monkeypatch)
    history = []
    svc.converse(None, None, None, history, initial_command=said,
                 wake=None, confirmed=True, address="exact")
    return history


def _wake(svc, monkeypatch, heard):
    """One wake trigger through the REAL handle_wake() up to converse()."""
    seen = {}

    class _Wake:
        def buffer_bytes(self):
            return b"\x00\x00"

        def reset(self):
            pass

    def _converse(stream, stt, tts, history, initial_command="", **kwargs):
        seen["command"] = initial_command
        seen["address"] = kwargs.get("address")

    monkeypatch.setattr(svc, "_DUCK", _Duck())
    monkeypatch.setattr(svc, "converse", _converse)
    monkeypatch.setattr(svc, "flush_stream", lambda *a, **k: None)
    monkeypatch.setattr(svc, "normalize_utterance", lambda pcm, *a, **k: pcm)
    monkeypatch.setattr(svc, "record_capture",
                        lambda *a, **k: svc.Capture(b"\x00\x00"))
    svc.handle_wake(None, _STT(heard), None, _Wake(), [])
    return seen


def _arm(svc, when=None):
    """Put the service in the state a just-asked PIN challenge leaves it in."""
    import time as _t

    svc.VAULT_CHALLENGE_PENDING = True
    svc.VAULT_CHALLENGE_SINCE = _t.monotonic() if when is None else when


def _text(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


def _assert_no_leak(caplog, *words):
    blob = _text(caplog).lower()
    for word in (words or PIN_LEAK_WORDS):
        assert word.lower() not in blob, f"leaked {word!r} into the journal"


# ── (a) utterance sites: PIN-shaped is withheld ─────────────────────────────

@pytest.mark.parametrize("said", [PIN_SPOKEN, PIN_DIGITS])
def test_addressed_line_withholds_a_pin_shaped_utterance(svc, monkeypatch,
                                                         caplog, said):
    caplog.set_level(logging.DEBUG, logger="puffpai.voice")
    seen = _wake(svc, monkeypatch, "hey puff pie " + said)

    assert seen, "handle_wake did not reach converse"
    assert "ADDRESSED:" in _text(caplog), "the event itself must still be logged"
    _assert_no_leak(caplog)


def test_command_line_withholds_a_pin_shaped_utterance(svc, monkeypatch,
                                                       caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _turn(svc, monkeypatch, PIN_SPOKEN, "Unlocked.", private=True)

    assert "COMMAND:" in _text(caplog)
    _assert_no_leak(caplog)


@pytest.mark.parametrize("said", [PIN_SPOKEN, PIN_DIGITS, "4 8 2 1"])
def test_heard_excerpt_withholds_a_pin(svc, said):
    """``_heard`` is the one helper behind every ``heard=`` field."""
    rendered = svc._heard(said)
    for word in PIN_LEAK_WORDS + ("4 8 2 1",):
        assert word.lower() not in rendered.lower()
    assert rendered.startswith('"') and rendered.endswith('"')


def test_unaddressed_optin_dump_still_withholds_a_pin(monkeypatch, tmp_path,
                                                      caplog):
    """The dev opt-in writes verbatim room speech; a PIN is still a PIN."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFF_LOG_TRANSCRIPTS", "1")
    import puffpai.voice.service as service
    service = importlib.reload(service)
    try:
        caplog.set_level(logging.DEBUG, logger="puffpai.voice")
        service._log_unaddressed(_STT(), PIN_DIGITS)
        _assert_no_leak(caplog)
        assert "wake=fired" in _text(caplog)
    finally:
        monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
        importlib.reload(service)


# ── (a2) ordinary speech, vault words included, stays READABLE ──────────────
#: This is a REQUIREMENT, not an oversight: one grep of ``heard=`` is how a
#: wake or a stop failure is diagnosed, and none of these carries a secret.
READABLE_UTTERANCES = (
    ORDINARY,
    "unlock the front door",
    VOCAB_ONLY,
    "log in to netflix",
    "pass me the pin cushion",
    "set the thermostat to 68",
    "it's 7:30",
    "what time is it, it's 10:45 already",
    "set an alarm for 6:15",
    "wake me at 23:59",
    "I want to go for a walk",
    "who won the game",
    "add milk to the shopping list",
    "spin class at 6",
    "we won too",
    "too much for me to eat",
    "set a timer for 10 minutes",
)


@pytest.mark.parametrize("said", READABLE_UTTERANCES)
def test_ordinary_speech_is_still_readable_in_the_journal(svc, said):
    assert lp.utterance_withholds(said) is False, said
    assert svc._heard(said) == '"' + said + '"'


def test_addressed_and_command_keep_a_vault_word_utterance(svc, monkeypatch,
                                                           caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    # converse() FIRST: _wake() stubs it out for the rest of the test.
    _turn(svc, monkeypatch, "unlock the front door", "Which door?",
          private=False, challenge=False)
    _wake(svc, monkeypatch, "hey puff pie unlock the front door")

    blob = _text(caplog)
    assert "unlock the front door" in blob          # ADDRESSED
    assert "COMMAND: 'unlock the front door'" in blob


def test_heard_excerpt_keeps_ordinary_speech_and_its_cap(svc):
    assert svc._heard(ORDINARY) == '"turn on the porch light"'
    long_room_talk = "so then he said the thing about the game " * 4
    assert len(svc._heard(long_room_talk)) <= svc.HEARD_MAX + 2


# ── (b) an ARMED vault challenge silences every utterance site ──────────────

def test_an_armed_challenge_withholds_every_utterance_site(svc, monkeypatch,
                                                           caplog):
    caplog.set_level(logging.DEBUG, logger="puffpai.voice")
    _arm(svc)

    # the heard= helper behind wake=rejected / dropped-junk / followup lines
    assert svc._heard("the game is on") == '"' + lp.WITHHELD_SECRET + '"'
    # ADDRESSED + COMMAND
    # converse() FIRST (_wake stubs it out), and the answer keeps the
    # challenge armed, so the ADDRESSED line below is still under the guard.
    _turn(svc, monkeypatch, "the game is on", "Okay.", private=False,
          challenge=True)
    _wake(svc, monkeypatch, "hey puff pie the game is on")

    blob = _text(caplog)
    assert "the game is on" not in blob
    assert "ADDRESSED:" in blob and "COMMAND:" in blob


def test_an_armed_challenge_withholds_a_rejected_wake_excerpt(svc, caplog):
    """A PIN said without the wake word landing is still a PIN."""
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _arm(svc)
    svc.logger.info("%s", svc._utterance_event(
        wake="rejected", reason="no-wakeword", heard=svc._heard("four eight")))
    assert "four eight" not in _text(caplog)
    assert "wake=rejected" in _text(caplog)


def test_the_armed_flag_is_set_by_the_backend_field(svc, monkeypatch):
    _turn(svc, monkeypatch, VOCAB_ONLY, CHALLENGE_REPLY, private=True,
          challenge=True)
    assert svc.vault_challenge_armed() is True


def test_the_armed_flag_clears_on_the_next_answer(svc, monkeypatch):
    _turn(svc, monkeypatch, VOCAB_ONLY, CHALLENGE_REPLY, private=True,
          challenge=True)
    assert svc.vault_challenge_armed() is True

    _turn(svc, monkeypatch, ORDINARY, ORDINARY_REPLY, private=False,
          challenge=False)
    assert svc.vault_challenge_armed() is False
    assert svc._heard(ORDINARY) == '"' + ORDINARY + '"'


def test_the_armed_flag_expires_on_its_own(svc):
    """A challenge that lapses with no further brain call must not silence
    the journal for the rest of the day."""
    _arm(svc, when=0.0)
    assert svc.vault_challenge_armed(now=svc.VAULT_CHALLENGE_TTL - 1) is True
    assert svc.vault_challenge_armed(now=svc.VAULT_CHALLENGE_TTL + 1) is False
    assert svc.VAULT_CHALLENGE_TTL >= 300, "must outlive the backend's window"


def test_a_brain_error_keeps_the_armed_flag(svc, monkeypatch):
    import requests

    _arm(svc)
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(
        requests.RequestException("connection refused")))
    svc.ask_brain([], "four eight")
    assert svc.vault_challenge_armed() is True, "an unanswered turn disarms nothing"


def test_an_old_backend_arms_from_the_challenge_sentence(svc, monkeypatch):
    """No field at all: the backend's own wording is the only signal."""
    assert lp.challenge_armed({}, CHALLENGE_REPLY) is True
    assert lp.challenge_armed({}, ORDINARY_REPLY) is False

    _turn(svc, monkeypatch, VOCAB_ONLY, CHALLENGE_REPLY)   # no field
    assert svc.vault_challenge_armed() is True


@pytest.mark.parametrize("value", [0, 1, [], "", "true", None, {}])
def test_a_non_boolean_challenge_field_fails_closed(value):
    assert lp.challenge_armed({"vault_challenge_pending": value},
                              ORDINARY_REPLY) is True


# ── (c) reply sites on a turn the backend called private ────────────────────

def test_private_turn_logs_no_slice_of_the_reply(svc, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="puffpai.voice")
    _turn(svc, monkeypatch, VOCAB_ONLY, SECRET_REPLY, private=True,
          challenge=False)

    blob = _text(caplog)
    assert "PUFF: [private turn]" in blob
    assert SECRET not in blob
    assert svc.LAST_REPLY_PRIVATE is True


def test_private_turn_leak_guard_warning_carries_no_reply(svc, caplog):
    caplog.set_level(logging.WARNING, logger="puffpai.voice")
    svc.LAST_REPLY_PRIVATE = True
    leaky = "the user wants me to say " + SECRET_REPLY
    out = svc.guard_reply(leaky)

    assert out != leaky, "the leak guard must still fire"
    assert "suppressed reasoning/tool-trace leak" in _text(caplog)
    assert SECRET not in _text(caplog)


def test_private_turn_tts_empty_diagnostic_carries_no_reply(svc, caplog):
    caplog.set_level(logging.WARNING, logger="puffpai.voice")
    svc.LAST_REPLY_PRIVATE = True

    class _MuteTTS:
        sample_rate = 22050

        def speak(self, text):
            return b""

    assert svc._tts_wav(_MuteTTS(), SECRET_REPLY) is None
    assert "TTS produced no audio" in _text(caplog)
    assert SECRET not in _text(caplog)


def test_the_tts_stub_never_logs_the_sentence(caplog):
    """H1: the stub is a debug fallback reached from the API and the pipeline
    too. It has no use for the sentence, so it does not get it."""
    from puffpai.voice.tts import StubTTS

    caplog.set_level(logging.DEBUG)
    assert StubTTS().speak("the wifi is CorrectHorseBatteryStaple") is None
    blob = _text(caplog)
    assert "CorrectHorseBatteryStaple" not in blob
    assert "[TTS stub]" in blob


def test_a_private_reply_is_withheld_even_by_content_alone(svc, monkeypatch,
                                                           caplog):
    """A backend that says ``private: false`` does not license a secret."""
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _turn(svc, monkeypatch, ORDINARY, SECRET_REPLY, private=False,
          challenge=False)
    assert SECRET not in _text(caplog)


@pytest.mark.parametrize("reply", [
    "the pass word is hunter",
    "your pass-code is open",
    "log in with this one",
    "the P.I.N. is set",
    "Pass Word is hunter",
])
def test_reply_vocabulary_survives_the_recognizers_spelling(reply):
    """H2: "pass word", "pass-code", "log in", "P.I.N." are what the
    recognizer actually writes."""
    assert lp.withholds(reply) is True
    assert lp.safe_reply(reply, limit=100) == lp.WITHHELD_SECRET


# ── (d) skew: an OLD backend that sends no ``private`` field ────────────────

def test_old_backend_pin_utterance_withholds_the_reply(svc, monkeypatch,
                                                       caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _turn(svc, monkeypatch, PIN_DIGITS, "It is " + SECRET)   # no field at all

    assert svc.LAST_REPLY_PRIVATE is True
    assert "PUFF: [private turn]" in _text(caplog)
    _assert_no_leak(caplog)


def test_old_backend_ordinary_turn_still_logs(svc, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _turn(svc, monkeypatch, ORDINARY, ORDINARY_REPLY)

    assert svc.LAST_REPLY_PRIVATE is False
    assert "PUFF: Okay, porch light is on" in _text(caplog)


def test_a_brain_error_falls_back_to_the_utterance_verdict(svc, monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(requests, "post", _boom)
    svc.ask_brain([], VOCAB_ONLY)
    assert svc.LAST_REPLY_PRIVATE is True
    svc.ask_brain([], ORDINARY)
    assert svc.LAST_REPLY_PRIVATE is False


def test_the_private_flag_does_not_stick_to_the_next_turn(svc, monkeypatch,
                                                          caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _turn(svc, monkeypatch, VOCAB_ONLY, SECRET_REPLY, private=True)
    caplog.clear()
    _turn(svc, monkeypatch, ORDINARY, ORDINARY_REPLY, private=False)

    assert svc.LAST_REPLY_PRIVATE is False
    assert "PUFF: Okay, porch light is on" in _text(caplog)


@pytest.mark.parametrize("value", [0, 1, [], "", "false", None, {}])
def test_a_non_boolean_private_field_fails_closed(value):
    """H4: ``bool()`` made ``0`` and ``[]`` authoritative "not private"."""
    assert lp.reply_is_private({"private": value}, ORDINARY,
                               ORDINARY_REPLY) is True


def test_a_real_boolean_private_field_is_trusted():
    assert lp.reply_is_private({"private": False}, ORDINARY,
                               ORDINARY_REPLY) is False
    assert lp.reply_is_private({"private": True}, ORDINARY,
                               ORDINARY_REPLY) is True
    # absent -> the content fallback, vocabulary included
    assert lp.reply_is_private({}, PIN_DIGITS, "ok") is True


# ── (e) the ordinary turn is unchanged ──────────────────────────────────────

def test_an_ordinary_turn_logs_both_sides_as_before(svc, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _turn(svc, monkeypatch, ORDINARY, ORDINARY_REPLY, private=False)

    blob = _text(caplog)
    assert "COMMAND: 'turn on the porch light'" in blob
    assert "PUFF: Okay, porch light is on" in blob


# ── (f) announcements, and the state file ───────────────────────────────────

class _Ann:
    """Stands in for puffpai.announcer."""

    def __init__(self, text):
        self._records = [{"ts": "2026-09-19T20:00:00", "source": "alarm",
                          "text": text}]
        self.cursor = ""

    def pending_speech(self):
        return self._records

    def recent(self, n):
        return self._records[-n:]

    def read_cursor(self):
        return self.cursor

    def write_cursor(self, ts):
        self.cursor = ts


def test_an_announcement_with_a_code_is_withheld(svc, caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    spoken = []
    svc.drain_announcements(None, ann=_Ann("reminder: the gate code is 4821"),
                            duck=_Duck(),
                            speaker=lambda tts, text: spoken.append(text))

    blob = _text(caplog)
    assert "ANNOUNCE (alarm)" in blob, "the source label must survive"
    assert "4821" not in blob
    assert spoken == ["reminder: the gate code is 4821"], \
        "it must still be SPOKEN in full -- only the log is redacted"


def test_an_ordinary_announcement_is_still_logged(svc, caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    svc.drain_announcements(None, ann=_Ann("time to leave for practice"),
                            duck=_Duck(), speaker=lambda tts, text: None)
    assert "time to leave for practice" in _text(caplog)


def test_an_announcement_naming_a_secret_is_withheld(svc, caplog):
    """H6: outbound speech gets the reply-side test, vocabulary included."""
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    spoken = []
    svc.drain_announcements(None, ann=_Ann("your new password is hunter2"),
                            duck=_Duck(),
                            speaker=lambda tts, text: spoken.append(text))

    blob = _text(caplog)
    assert "ANNOUNCE (alarm)" in blob
    assert "hunter2" not in blob and "password" not in blob
    assert spoken == ["your new password is hunter2"], "still spoken in full"


def test_an_announcement_is_withheld_while_a_challenge_is_armed(svc, caplog):
    """The room is being asked for a PIN; nothing gets written down."""
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    _arm(svc)
    svc.drain_announcements(None, ann=_Ann("time to leave for practice"),
                            duck=_Duck(), speaker=lambda tts, text: None)

    blob = _text(caplog)
    assert "ANNOUNCE (alarm)" in blob
    assert "practice" not in blob


def test_the_state_file_carries_no_text_at_all(svc, tmp_path):
    """voice_state.json is read by the dashboard; it must stay text-free."""
    svc.write_speaking_state(True, 1234.5)
    state = json.loads((tmp_path / "voice_state.json").read_text("utf-8"))
    assert set(state) == {"speaking", "since"}


# ── (g) the shape rule: recognizer artefacts, and the one relaxation ────────

WITHHELD_SHAPES = (
    "4821", "4 8 2 1", "4-8-2-1", "9 9 3 1", "48 21",
    "forty eight twenty one", "my pin is four eight two one",
    # H3(a): homophones
    "for ate to won", "my code is for ate to won", "won too tree",
    "ate won for", "tree free nein for",
    # H3(b): digits with letters shot through them
    "4a8b2c1", "the numbers are 4a8b2c1",
    # unchanged from the backend rule
    "wifi password please, 4 8 2 1", "router password | 4821",
    "play some music from 1984", "zip code 78701",
    "٤٨٢١",          # Arabic-Indic 4821
    "４８２１",          # fullwidth 4821
    # H5: the clock exemption is for a LONE, unqualified time only
    "pin 12:34", "the pin is 12:34 read my wifi password",
    "unlock my wifi password pin 12:34", "my code is 12:34 and 56:78",
    "9:99", "pin is 7:30", "code 10:45", "12:34 12:35",
    "4 8 2 1 at 7:30",
    # accepted false withholds -- named so a future reader knows they are known
    "count to ten for me", "my 401k statement", "set an alarm for 7 30",
)


@pytest.mark.parametrize("text", WITHHELD_SHAPES)
def test_these_shapes_are_withheld_from_the_journal(text):
    assert lp.has_pin_shape(text) is True, text
    assert lp.utterance_withholds(text) is True


#: The single place this side is LAXER than the backend, and why: a clock time
#: is not a PIN, and it is most of what a household says with digits in it.
RELAXED_TIMES = ("it's 7:30", "the alarm is at 10:45", "7:30",
                 "set an alarm for 6:15", "wake me at 23:59")


@pytest.mark.parametrize("text", RELAXED_TIMES)
def test_a_clock_time_stays_readable(text):
    assert lp.has_pin_shape(text) is False, text


# ── (h) the two PIN-shape implementations must not drift apart ──────────────

PARITY_CORPUS = (
    "", "   ", "hello", ORDINARY, ORDINARY_REPLY, VOCAB_ONLY,
    PIN_SPOKEN, PIN_DIGITS, SECRET_REPLY,
    "4821", "4 8 2 1", "4-8-2-1", "48 21", "12", "1 2",
    "forty eight twenty one", "double four nine one", "triple nine",
    "my pin is four eight two one", "oh four eight two",
    "wifi password please, 4 8 2 1", "router password | 4821",
    "the password policy for 2024", "remind me to change the password in 30 days",
    "set a timer for 10 minutes", "set the thermostat to 68",
    "play some music from 1984", "the area code for 512",
    "zip code 78701", "unlock the front door", "what's the vault",
    "٤٨٢١", "４８２１",
    "call me at five five five one two one two",
    "chapter 12 verse 3", "add 2 cups of flour",
) + WITHHELD_SHAPES


@pytest.mark.parametrize("text", PARITY_CORPUS)
def test_pin_shape_is_a_superset_of_the_backend_implementation(text):
    """The voice copy may be STRICTER than api_server's, never laxer.

    Equality is the wrong contract: this side reads recognizer output, which
    spells PINs in ways a typed surface never does ("for ate to won",
    "4a8b2c1"), so it withholds more. The invariant is containment --
    anything the backend withholds, this withholds too. The ONE named
    exception is a clock-time token (``RELAXED_TIMES``), which is tested
    separately; no string in this corpus relies on it.

    api_server is imported HERE and only here: the voice unit must never pull
    the Flask app into the mic loop, which is why the rule is duplicated at
    all.
    """
    from puffpai import api_server

    if api_server._log_text_has_pin_shape(text):
        assert lp.has_pin_shape(text) is True, f"voice is laxer on {text!r}"


def test_the_voice_rule_is_stricter_where_it_should_be():
    from puffpai import api_server

    for text in ("for ate to won", "4a8b2c1", "won too tree"):
        assert api_server._log_text_has_pin_shape(text) is False, text
        assert lp.has_pin_shape(text) is True, text


def test_vocabulary_never_withholds_an_utterance_only_a_reply():
    for text in ("unlock the front door", VOCAB_ONLY, "log in to netflix"):
        assert lp.has_secret_vocabulary(text) is True
        assert lp.utterance_withholds(text) is False    # the mic side
        assert lp.withholds(text) is True               # the reply side


def test_privacy_helpers_fail_closed_on_a_broken_input():
    class _Boom:
        def __str__(self):
            raise RuntimeError("no")

        def strip(self):
            raise RuntimeError("no")

    bad = _Boom()
    assert lp.utterance_withholds(bad) is True
    assert lp.withholds(bad) is True
    assert lp.safe_heard(bad) == lp.WITHHELD_SECRET
    assert lp.safe_reply(bad) == lp.WITHHELD_REPLY
    assert lp.reply_is_private(bad, bad, bad) is True
    assert lp.challenge_armed(bad, bad) is True


# ── (i) the backend contract: /api/chat carries both flags ──────────────────

DEVICE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SEALED_SAID = "what's my wifi password, the PIN is 4821"
ORDINARY_ASK = "what is the capital of France"


@pytest.fixture
def api():
    from puffpai import api_server
    return api_server


@pytest.fixture
def client(api, monkeypatch):
    api.app.config.update(TESTING=True)
    api._rate_limit_store.clear()
    api._reset_vault_turn_privacy()
    api._live_turn_clear()
    monkeypatch.setattr(api, "_record_voice_turn", lambda *a, **k: None)
    monkeypatch.setattr(api, "_apply_pii_honesty", lambda text, *a, **k: text)
    monkeypatch.setattr(api, "_pii_deterministic_reply", lambda *a, **k: None)
    with api.app.test_client() as c:
        yield c
    api._reset_vault_turn_privacy()
    api._live_turn_clear()


def _headers(api):
    return {"X-API-Key": api._API_KEY, "X-Puff-Device-Id": DEVICE}


def _say(api, client, text, **extra):
    body = {"messages": [{"role": "user", "content": text}]}
    body.update(extra)
    return client.post("/api/chat", headers=_headers(api), json=body)


def _voice_query(api, monkeypatch, reply="Paris.", mark_vault=False):
    def _run(message, history=None, **kwargs):
        if mark_vault:
            # The REAL boundary the vault tools announce at (assistant_tools
            # .note_vault_tool_calls -> _mark_vault_turn_private), which is
            # what tests/test_vault_tool_turn_private.py exercises end to end.
            from puffpai import assistant_tools as rt
            rt.note_vault_tool_calls(["vault_unlock_and_get"])
        return reply

    monkeypatch.setattr(api, "_process_voice_query", _run)


def _arm_backend_challenge(monkeypatch, armed=True):
    """Arm the Zone-2 display challenge the seal predicate already reads."""
    from puffpai import dashboard_display as dd
    monkeypatch.setattr(dd, "pending_vault_active", lambda: armed)


def test_api_chat_marks_a_sealed_utterance_private(api, client, monkeypatch):
    _voice_query(api, monkeypatch, reply="I opened it.")
    body = _say(api, client, SEALED_SAID).get_json()
    assert body["private"] is True
    assert body["done"] is True and body["message"]["role"] == "assistant"


def test_api_chat_marks_a_vault_tool_turn_private(api, client, monkeypatch):
    """No PIN in the words at all -- the TOOL is what makes it private."""
    _voice_query(api, monkeypatch, reply="Here it is.", mark_vault=True)
    body = _say(api, client, "read me the thing I saved").get_json()
    assert body["private"] is True


def test_api_chat_marks_a_pin_shaped_utterance_private(api, client,
                                                       monkeypatch):
    """No vault tool, no seal: "get wifi 4821" answered from the air."""
    _voice_query(api, monkeypatch, reply="Okay.")
    body = _say(api, client, PIN_DIGITS).get_json()
    assert body["private"] is True


def test_api_chat_leaves_an_ordinary_turn_public(api, client, monkeypatch):
    _voice_query(api, monkeypatch, reply="Paris.")
    body = _say(api, client, ORDINARY_ASK).get_json()
    assert body["private"] is False
    assert body["vault_challenge_pending"] is False
    assert body["message"]["content"] == "Paris."


def test_api_chat_reports_an_armed_vault_challenge(api, client, monkeypatch):
    """The voice unit cannot see this state and logs before it could ask."""
    _voice_query(api, monkeypatch, reply="What's your PIN?")
    _arm_backend_challenge(monkeypatch, True)
    body = _say(api, client, "pull up my passport").get_json()
    assert body["vault_challenge_pending"] is True
    assert body["private"] is True, "a pending challenge also seals the turn"


def test_api_chat_clears_the_flag_once_the_challenge_resolves(api, client,
                                                              monkeypatch):
    _voice_query(api, monkeypatch, reply="Paris.")
    _arm_backend_challenge(monkeypatch, False)
    body = _say(api, client, ORDINARY_ASK).get_json()
    assert body["vault_challenge_pending"] is False


def test_api_chat_challenge_flag_fails_closed(api, client, monkeypatch):
    _voice_query(api, monkeypatch, reply="Paris.")

    def _boom():
        raise RuntimeError("pending state unreadable")

    from puffpai import dashboard_display as dd
    monkeypatch.setattr(dd, "pending_vault_active", _boom)
    body = _say(api, client, ORDINARY_ASK).get_json()
    assert body["vault_challenge_pending"] is True


def test_api_chat_keeps_its_ollama_shape(api, client, monkeypatch):
    _voice_query(api, monkeypatch, reply="Paris.")
    body = _say(api, client, ORDINARY_ASK).get_json()
    assert set(body) == {"model", "created_at", "message", "done", "private",
                         "vault_challenge_pending", "total_duration",
                         "load_duration", "prompt_eval_count", "eval_count"}


def test_api_chat_streaming_chunk_carries_both_flags(api, client, monkeypatch):
    _voice_query(api, monkeypatch, reply="I opened it.")
    resp = _say(api, client, SEALED_SAID, stream=True)
    chunk = json.loads(resp.get_data(as_text=True).splitlines()[0])
    assert chunk["private"] is True
    assert chunk["vault_challenge_pending"] is False
    assert chunk["done"] is True


def test_api_chat_error_returns_fail_closed(api, client):
    no_messages = client.post("/api/chat", headers=_headers(api), json={})
    assert no_messages.status_code == 400
    assert no_messages.get_json()["private"] is True
    assert no_messages.get_json()["vault_challenge_pending"] is True

    no_user = client.post("/api/chat", headers=_headers(api),
                          json={"messages": [{"role": "assistant",
                                              "content": "hi"}]})
    assert no_user.status_code == 400
    assert no_user.get_json()["private"] is True
    assert no_user.get_json()["vault_challenge_pending"] is True


def test_every_return_path_of_the_handler_carries_both_fields(api):
    """Source-level enumeration: six returns, five of each field.

    The HA-PIN answer is one dict returned two ways (streamed or not), and
    that dict carries both fields once. A return added later without them
    would silently downgrade the voice journal to "log it", so the count is
    pinned here.
    """
    import inspect

    src = inspect.getsource(api.ollama_chat)
    returns = [line.strip() for line in src.splitlines()
               if line.strip().startswith("return ")]
    assert len(returns) == 6, returns
    assert src.count("'private'") + src.count('"private"') == 5
    assert (src.count("'vault_challenge_pending'")
            + src.count('"vault_challenge_pending"')) == 5


def test_the_private_flag_is_computed_fail_closed(api, client, monkeypatch):
    """If deciding raises, the turn is private."""
    _voice_query(api, monkeypatch, reply="Paris.")

    def _boom(*a, **k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(api, "_log_text_has_pin_shape", _boom)
    body = _say(api, client, ORDINARY_ASK).get_json()
    assert body["private"] is True
