"""Tests for the proactive announcer (satellite-first, queue-always).

Pure in-process; no HA, no sockets, no LLM.
Run: pytest tests/test_announcer.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai import announcer, zigbee_pairing
from puffpai.agents.envelope import Envelope


class RecordingBus:
    def __init__(self, reply=None, raise_exc=None):
        self.sent: list[tuple[str, Envelope]] = []
        self.reply = {"ok": True} if reply is None else reply
        self.raise_exc = raise_exc

    def send(self, envelope, to):
        self.sent.append((to, envelope))
        if self.raise_exc:
            raise self.raise_exc
        return self.reply


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    announcer.reset_cache()
    yield tmp_path
    announcer.reset_cache()


def read_queue(tmp_path):
    path = tmp_path / announcer.QUEUE_FILENAME
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_announce_speaks_via_the_bus_and_queues(state_dir):
    bus = RecordingBus()
    out = announcer.announce("The shed door is open.", source="test", bus=bus)

    assert out["spoken"] is True and out["sink"] == "assist_satellite"
    assert out["queued"] is True
    to, env = bus.sent[0]
    assert to == "steward" and env.intent == "announce"
    assert env.payload == {"text": "The shed door is open."}
    # Honest provenance: a courtesy, never a person's command.
    assert env.origin_agent == "announcer" and env.requester == "system"
    assert env.state_changing is True
    rows = read_queue(state_dir)
    assert rows[-1]["text"] == "The shed door is open."
    assert rows[-1]["spoken"] is True


def test_offline_satellite_still_queues(state_dir):
    bus = RecordingBus(reply={"ok": False, "error": "entity unavailable"})
    out = announcer.announce("hello", bus=bus)
    assert out["spoken"] is False and out["sink"] is None
    assert out["queued"] is True
    assert read_queue(state_dir)[-1]["spoken"] is False


def test_a_raising_bus_never_raises_out(state_dir):
    bus = RecordingBus(raise_exc=RuntimeError("bus exploded"))
    out = announcer.announce("hello", bus=bus)
    assert out["ok"] is True and out["spoken"] is False
    assert read_queue(state_dir)


def test_no_bus_at_all_is_queue_only(state_dir):
    out = announcer.announce("hello")
    assert out["ok"] is True and out["spoken"] is False
    assert read_queue(state_dir)


def test_text_is_clamped_and_blank_is_refused(state_dir):
    out = announcer.announce("x" * 1000, bus=RecordingBus())
    assert len(out["text"]) == announcer.MAX_TEXT_CHARS
    blank = announcer.announce("   \n  ")
    assert blank["ok"] is False and blank["queued"] is False
    assert len(read_queue(state_dir)) == 1


def test_queue_is_a_rolling_window(state_dir, monkeypatch):
    monkeypatch.setattr(announcer, "MAX_QUEUE_ENTRIES", 5)
    for i in range(8):
        announcer.announce(f"msg {i}")
    rows = read_queue(state_dir)
    assert len(rows) == 5
    assert rows[0]["text"] == "msg 3" and rows[-1]["text"] == "msg 7"


def test_recent_is_newest_first(state_dir):
    for i in range(3):
        announcer.announce(f"msg {i}")
    out = announcer.recent(2)
    assert [r["text"] for r in out] == ["msg 2", "msg 1"]


# ── the pairing watcher raises its hand ─────────────────────────────

def test_pairing_result_announces_found_devices(state_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(announcer, "announce",
                        lambda text, **kw: calls.append((text, kw)) or {"ok": True})
    # The report is about DEVICES (grouped by
    # zigbee_pairing.group_devices), not entities.
    zigbee_pairing._announce_result(
        ["New Bulb", "New Plug"], {"light.new_bulb", "switch.new_plug"})
    text, kw = calls[0]
    assert "2 new devices joined" in text
    assert "New Bulb" in text and "New Plug" in text
    assert "asked on the dashboard" in text
    assert kw["source"] == "zigbee_pairing"


def test_pairing_result_announces_the_empty_window_too(state_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(announcer, "announce",
                        lambda text, **kw: calls.append(text) or {"ok": True})
    zigbee_pairing._announce_result([], set())
    assert "nothing new joined" in calls[0]
    assert "say yes" in calls[0]


# ── playback receipts from the room speaker ───────────────────────────────
class _Duck:
    def on_addressed(self):
        pass

    def restore(self):
        pass


def test_room_speaker_playback_marks_the_announcement_spoken(state_dir):
    from puffpai.voice import service
    announcer.announce("Dinner is ready.", source="test", bus=None)
    assert announcer.recent(1)[0]["spoken"] is False

    heard = []
    count = service.drain_announcements(
        tts=None, ann=announcer, duck=_Duck(),
        speaker=lambda _t, text: heard.append(text))

    assert count == 1 and heard == ["Dinner is ready."]
    rec = announcer.recent(1)[0]
    assert rec["spoken"] is True
    assert rec["sink"] == announcer.LOCAL_SPEAKER_SINK
    # The local speaker does not say it twice.
    assert announcer.pending_speech() == []


def test_failed_playback_leaves_the_announcement_unspoken(state_dir):
    from puffpai.voice import service
    announcer.announce("Dinner is ready.", source="test", bus=None)

    def _explode(_t, _text):
        raise RuntimeError("piper is down")

    service.drain_announcements(tts=None, ann=announcer, duck=_Duck(),
                                speaker=_explode)
    rec = announcer.recent(1)[0]
    assert rec["spoken"] is False and rec["sink"] is None


def test_skipped_stale_records_are_not_marked_spoken(state_dir):
    from puffpai.voice import service
    announcer.announce("For the feed only.", source="test", bus=None,
                       speak=False)
    service.drain_announcements(tts=None, ann=announcer, duck=_Duck(),
                                speaker=lambda *_: None)
    # The cursor parked past it, but nothing was said out loud.
    assert announcer.read_cursor()
    assert announcer.recent(1)[0]["spoken"] is False


def test_spoken_receipts_are_a_rolling_window(state_dir, monkeypatch):
    monkeypatch.setattr(announcer, "MAX_QUEUE_ENTRIES", 3)
    for i in range(5):
        announcer.mark_spoken(f"2026-10-10T00:00:0{i}+00:00")
    lines = (state_dir / announcer.SPOKEN_FILENAME).read_text(
        encoding="utf-8").split()
    assert lines == [f"2026-10-10T00:00:0{i}+00:00" for i in (2, 3, 4)]
