"""HomeBot voice capture stall detection, and wake-phrase remnants.

Two properties:

1. A capture stream can wedge, leaving the service inside an untimed
   PortAudio read until the ReSpeaker is physically replugged. Without stall
   detection, a reopen and finally an exit, the unit's ``Restart=on-failure``
   never fires.
2. The address parser must not leave the wake-phrase remnant in the command:
   ``'Hey puff pie, what time is it?'`` must not produce
   ``'pie, what time is it'``.
"""

import threading
import time

import pytest

from puffpai.voice.capture import CaptureStream, CaptureStalled
from puffpai.voice.service import parse_address


# ── fake PortAudio stream ──────────────────────────────────────────────────
class FakeStream:
    """A mock Pulse record stream whose frames can be stopped on command."""

    def __init__(self, block, live=True):
        self.block = block
        self.live = live
        self.aborted = False
        self.closed = False
        self._stop = threading.Event()

    def stall(self):
        """Stop delivering frames — the wedged-PCM condition."""
        self.live = False

    def read(self, frames):
        while True:
            if self._stop.is_set():
                raise RuntimeError("stream aborted")
            if self.live:
                self._stop.wait(0.005)      # ~ a capture block interval
                return [0] * self.block, False
            self._stop.wait(0.01)

    def abort(self):
        self.aborted = True
        self._stop.set()

    def close(self):
        self.closed = True
        self._stop.set()


def make_opener(streams, fail_after=None):
    """Return an opener that hands out ``streams`` in order.

    ``fail_after`` opens raise, to exercise the reopen-failure path.
    """
    opened = []

    def opener(device, samplerate, blocksize, channels, dtype):
        if fail_after is not None and len(opened) >= fail_after:
            raise OSError("no such capture device")
        stream = streams[len(opened)] if len(opened) < len(streams) \
            else FakeStream(blocksize, live=False)
        opened.append(stream)
        return stream

    opener.opened = opened
    return opener


def drain_one(cap):
    """Read one block with a hard test-side ceiling so a bug cannot hang CI."""
    box = {}

    def run():
        try:
            box["value"] = cap.read(cap._blocksize)
        except BaseException as exc:      # noqa: BLE001 - surfaced to the test
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "read() blocked — the stall watchdog did not fire"
    return box


# ── 1. frames stop → watchdog fires → reopen ───────────────────────────────
def test_stalled_capture_is_reopened(caplog):
    first, second = FakeStream(64), FakeStream(64)
    opener = make_opener([first, second])
    cap = CaptureStream(device="pulse", samplerate=16000, blocksize=64,
                        stall_timeout=0.4, max_reopen=2, opener=opener,
                        queue_seconds=0.5)
    caplog.set_level("INFO", logger="puffpai.voice")
    with cap:
        assert drain_one(cap).get("error") is None
        first.stall()
        time.sleep(0.6)
        cap.flush()
        box = drain_one(cap)

    assert box.get("error") is None, box
    assert len(opener.opened) == 2, "the stream was never reopened"
    assert first.aborted, "the wedged stream was not aborted"
    text = caplog.text
    assert "capture stalled (no frames" in text
    assert "reopened" in text


# ── 2. two failed reopens → CaptureStalled → non-zero exit ─────────────────
def test_two_failed_reopens_raise_capture_stalled():
    first = FakeStream(64)
    # Every reopen yields a stream that is already dead: audio never returns.
    opener = make_opener([first])
    cap = CaptureStream(device="pulse", samplerate=16000, blocksize=64,
                        stall_timeout=0.2, max_reopen=2, opener=opener,
                        queue_seconds=0.5)
    with cap:
        assert drain_one(cap).get("error") is None
        first.stall()
        time.sleep(0.3)
        cap.flush()
        box = drain_one(cap)

    assert isinstance(box.get("error"), CaptureStalled), box
    # max_reopen=2 means two reopens were attempted before giving up.
    assert len(opener.opened) == 3      # initial + 2 reopens


def test_reopen_that_cannot_open_still_exits():
    first = FakeStream(64)
    opener = make_opener([first], fail_after=1)   # every reopen raises
    cap = CaptureStream(device="pulse", samplerate=16000, blocksize=64,
                        stall_timeout=0.2, max_reopen=2, opener=opener,
                        queue_seconds=0.5)
    with cap:
        assert drain_one(cap).get("error") is None
        first.stall()
        time.sleep(0.3)
        cap.flush()
        box = drain_one(cap)
    assert isinstance(box.get("error"), CaptureStalled), box


def test_main_returns_nonzero_when_capture_dies(monkeypatch):
    """``main`` must return non-zero so ``Restart=on-failure`` recovers us."""
    from puffpai.voice import service

    monkeypatch.setattr(service, "make_cues", lambda: None)
    monkeypatch.setattr(service, "WakeWord", lambda *a, **k: object())
    monkeypatch.setattr(service, "_load_engines", lambda: (object(), object()))
    monkeypatch.setattr(service, "ANNOUNCE_ONLINE", False)

    class Dead:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise CaptureStalled("capture device 'pulse' delivered no audio")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("puffpai.voice.capture.CaptureStream", Dead)
    assert service.main() == 1


# ── 3. healthy stream keeps working; flush drains the backlog ──────────────
def test_healthy_stream_does_not_trip_the_watchdog():
    opener = make_opener([FakeStream(64)])
    cap = CaptureStream(device="pulse", samplerate=16000, blocksize=64,
                        stall_timeout=0.3, max_reopen=2, opener=opener,
                        queue_seconds=0.5)
    with cap:
        for _ in range(5):
            data, overflowed = cap.read(64)
            assert len(data) == 64
            assert overflowed is False
        time.sleep(0.05)
        cap.flush()
        assert cap.read_available == 0
    assert len(opener.opened) == 1, "a healthy stream must never be reopened"


# ── 4. wake-phrase remnant must be stripped from the command ───────────────
@pytest.mark.parametrize("spelling", [
    "puff pie", "puff pi", "puffpie", "puff pay", "puffpai",
])
def test_wake_phrase_remnant_is_stripped(spelling):
    text = "Hey %s, what time is it?" % spelling
    addressed, command = parse_address(text)
    assert addressed is True
    assert command == "what time is it", (spelling, command)


def test_misheard_wake_phrase_with_comma():
    """A typical voice.log line: the wake word misheard as "puff pie"."""
    addressed, command = parse_address("Hey puff pie, what time is it?")
    assert (addressed, command) == (True, "what time is it")


def test_address_without_a_name_tail_is_untouched():
    addressed, command = parse_address("Hey puff, what time is it?")
    assert (addressed, command) == (True, "what time is it")


def test_unaddressed_speech_is_still_rejected():
    assert parse_address("what time is it") == (False, "")
