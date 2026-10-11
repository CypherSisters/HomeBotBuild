"""The voice repeat-breaker must not fire when the SAME ask
legitimately earns the same deterministic reply three times (consent prompts,
status read-backs). It still fires when DIFFERENT asks get one parroted line."""
import requests

from puffpai.voice import service


class _Resp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": self._c}}


def _brain(monkeypatch, content):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(content))


PROMPT = ("I can open the Zigbee pairing window for 120 seconds, but it needs "
          "your approval first.")


def test_same_ask_same_reply_three_times_is_not_a_loop(monkeypatch):
    _brain(monkeypatch, PROMPT)
    history = []
    replies = [service.ask_brain(history, "pair my bulbs") for _ in range(3)]
    assert replies == [PROMPT] * 3
    assert history[-1] == {"role": "assistant", "content": PROMPT}


def test_different_asks_same_reply_three_times_still_breaks(monkeypatch):
    _brain(monkeypatch, "Plan: call tool then answer.")
    history = []
    service.ask_brain(history, "what time is it")
    service.ask_brain(history, "play jazz")
    third = service.ask_brain(history, "whats the weather")
    assert third.startswith("I caught myself repeating")
    # the unanswered third user turn was dropped, not stored: two full
    # exchanges remain and the breaker line itself never enters history
    assert len(history) == 4
    assert all(m["content"] != third for m in history)
