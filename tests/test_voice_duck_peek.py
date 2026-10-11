"""The music only ducks for a wake that LOOKS like a wake.

Singing along to the radio looks like this in the voice journal:

    wake=rejected conf=n/a  ms=140  rms=156 reason=no-wakeword heard=""
    weak transcript while playing — peek duck armed for 20s
    ducked 1 playback stream(s) to 70% (peek: retrying a possible missed wake)
    wake=rejected conf=-1.19 ms=16800 rms=633 reason=no-wakeword heard="Ahhhhhhhhhhh"
    wake=rejected conf=-1.26 ms=8887  rms=788 reason=no-wakeword heard="Ohhhh!"

Singing fires the wake model (it is a speech detector), stage 2 rejects, and
the sung audio comes back EMPTY or as a one-word interjection at poor
confidence. Without the rules below, both peek arms read that as "a wake we
missed" and the next note dips the music.

The rules under test here, both of which demand wake-SHAPED evidence:

* retry arm — two rejects in the window only count if BOTH are short and at
  least one carries a fragment of her name;
* missed arm — a short transcript with a name fragment, or a command-length
  (3–5 word) one at poor confidence. Never an empty pass, never an interjection,
  never a whole sung line.

Plus: a peek duck is a guess, so it always lets go — on the reject that
disproves it, and by ``PUFF_DUCK_PEEK_HOLD`` if anything else forgets. And
``PUFF_DUCK_PEEK=0`` turns the entire path off.
"""

import pytest

from puffpai.voice import ducking
from puffpai.voice import service


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakePactl:
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
    def levels(self):
        return [c[3].rstrip("%") for c in self.calls
                if c[1] == "set-sink-input-volume"]


class FakeSTT:
    def __init__(self, conf=None):
        self.last_avg_logprob = conf


def _policy(pactl, clock, **kw):
    return ducking.DuckPolicy(addressed_level="45", peek_level="70",
                              duck_on_listen=False, peek_arm=20, peek_hold=4,
                              runner=pactl, clock=clock, **kw)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(autouse=True)
def _clean_streak():
    service.clear_wake_retry()
    yield
    service.clear_wake_retry()


# ── 1. the retry arm needs retry-shaped evidence ───────────────────────────
def test_two_short_name_fragment_rejects_arm_the_peek():
    """"hey" … "pie" three seconds apart: she is trying and we keep missing."""
    assert service.wake_retry_pressure("hey", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure("pie", now=103.0, window=12.0) is True


def test_two_sung_lines_never_arm_the_peek():
    assert service.wake_retry_pressure(
        "in your head in your head they are fighting",
        now=100.0, window=12.0) is False
    assert service.wake_retry_pressure(
        "with their tanks and their bombs and their guns",
        now=104.0, window=12.0) is False


def test_one_short_name_fragment_plus_one_sung_line_does_not_arm():
    """The pair must BOTH be short; a chorus between two tries breaks the
    streak, which is the honest reading — she was not repeating herself."""
    assert service.wake_retry_pressure("hey", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure(
        "and the violence caused such silence", now=102.0,
        window=12.0) is False


def test_one_short_nameless_plus_one_short_fragment_arms():
    """Only ONE of the pair has to carry the fragment: the first attempt often
    comes back empty and the second as "pie"."""
    assert service.wake_retry_pressure("", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure("pie", now=102.0, window=12.0) is True


def test_two_short_rejects_with_no_name_in_either_do_not_arm():
    assert service.wake_retry_pressure("Ohhhh!", now=100.0, window=12.0) is False
    assert service.wake_retry_pressure("I do.", now=102.0, window=12.0) is False


# ── 2. the missed-wake arm needs a short, wake-shaped transcript ───────────
@pytest.mark.parametrize("text,conf,expected", [
    ("", None, False),                                   # sung note, heard nothing
    ("Ahhhhhhhhhhh", -1.19, False),                       # a held note
    ("Love you", -0.15, False),                           # sung along, confident
    ("in your head they are fighting and crying", -1.1, False),   # a whole line
    ("What time is it?", -0.9, True),                     # name eaten by the decode
    ("pie what time", -0.9, True),                        # half the name survived
    ("hey", -0.3, True),                                  # half the name, confident
    ("so then I said puff pie should hear this", -0.3, True),     # name, late
])
def test_looks_like_missed_wake(text, conf, expected):
    assert service._looks_like_missed_wake(FakeSTT(conf), text) is expected


def test_a_twelve_word_low_confidence_line_over_music_is_not_a_missed_wake():
    line = ("but you see it's not me it's not my family in your head " * 1).strip()
    assert len(line.split()) >= 12
    assert service._looks_like_missed_wake(FakeSTT(-1.3), line) is False


# ── 3. a peek duck always lets go ──────────────────────────────────────────
def test_peek_duck_is_released_by_the_reject_that_disproves_it(clock):
    pactl = FakePactl()
    policy = _policy(pactl, clock)

    policy.on_unaddressed(True, reason="missed")          # armed
    assert policy.peek_armed() is True
    policy.on_wake()                                      # next trigger peeks
    assert policy.level == "70" and policy.peeking is True

    clock.t += 1.2                                        # the stage-2 pass
    policy.on_unaddressed(False)                          # …and it rejected
    assert policy.active is False
    assert pactl.levels == ["70", "100"]


def test_a_forgotten_peek_duck_expires_after_the_hold(clock):
    pactl = FakePactl()
    policy = _policy(pactl, clock)
    policy.on_unaddressed(True, reason="retry")
    policy.on_wake()
    assert policy.level == "70"

    clock.t += 5                                          # nothing confirmed
    assert policy.expire_peek() is True
    assert policy.active is False
    assert pactl.levels == ["70", "100"]


def test_a_confirmed_wake_keeps_the_addressed_duck_through_the_hold(clock):
    pactl = FakePactl()
    policy = _policy(pactl, clock)
    policy.on_unaddressed(True, reason="missed")
    policy.on_wake()
    policy.on_addressed()                                 # she really said it
    assert policy.level == "45"

    clock.t += 30                                         # a long answer
    assert policy.expire_peek() is False
    assert policy.level == "45", "an addressed exchange is not a guess"
    policy.restore()
    assert pactl.levels == ["70", "45", "100"]


# ── 4. the kill switch ─────────────────────────────────────────────────────
def test_peek_kill_switch_disables_both_arms(clock):
    pactl = FakePactl()
    policy = _policy(pactl, clock, peek_enabled=False)

    assert policy.on_unaddressed(True, reason="missed") is False
    assert policy.peek_armed() is False
    policy.on_wake()
    assert pactl.levels == [], "PUFF_DUCK_PEEK=0: only an addressed turn ducks"
    policy.on_addressed()
    assert pactl.levels == ["45"]


# ── 5. what the arms log, so the next complaint is one grep ────────────────
def test_the_peek_path_logs_its_reason_and_level(clock, caplog):
    pactl = FakePactl()
    policy = _policy(pactl, clock)
    with caplog.at_level("INFO", logger="puffpai.voice"):
        policy.on_unaddressed(True, reason="retry")
        policy.on_wake()
        policy.restore()
    text = caplog.text
    assert "duck=armed reason=retry" in text
    assert "duck=peek reason=retry level=70" in text
    assert "duck=restored" in text
