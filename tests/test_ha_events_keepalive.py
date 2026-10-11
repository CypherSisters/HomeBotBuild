"""HA event listener keepalive and quiet re-subscribe.

Without a keepalive, websocket-client's own receive timeout fires in a quiet
gap and the journal logs "HA event listener disconnected: Connection timed
out" every few hours.
Fake sockets only; no HA, no network.
Run: pytest tests/test_ha_events_keepalive.py -q
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from puffpai import ha_events


class WebSocketTimeoutException(Exception):
    """Same NAME as websocket-client's, which is not a TimeoutError."""


TIMEOUT = object()


class FakeWS:
    """Scripted socket: frames are dicts, TIMEOUT raises a receive timeout,
    callables run (for stop()); drained means the link closed."""

    def __init__(self, frames, exc=WebSocketTimeoutException):
        self._frames = list(frames)
        self._exc = exc
        self.sent: list[dict] = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def recv(self):
        if not self._frames:
            raise ConnectionError("fake socket drained")
        item = self._frames.pop(0)
        if item is TIMEOUT:
            raise self._exc("Connection timed out")
        if callable(item):
            return item()
        return json.dumps(item)

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


def _event(entity="binary_sensor.yard_camera_person"):
    return {"id": 1, "type": "event", "event": {
        "event_type": "state_changed",
        "data": {"entity_id": entity, "old_state": {"state": "off"},
                 "new_state": {"state": "on", "attributes": {}}}}}


def _listener(**kw):
    calls = []
    lst = ha_events.HAEventListener(
        url="http://ha.test:8123", token="test-token",
        watch_entities=["binary_sensor.yard_camera_*"],
        debounce_seconds=0, submit=lambda *a: calls.append(a), **kw)
    return lst, calls


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_connect_sets_the_ping_interval_as_the_receive_timeout():
    ws = FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}])
    lst, _ = _listener(ws_factory=lambda url: ws)
    lst._connect()
    assert ws.timeout == ha_events.PING_INTERVAL_SECONDS
    assert ha_events.PING_INTERVAL_SECONDS < 30


@pytest.mark.parametrize("exc", [WebSocketTimeoutException, TimeoutError])
def test_a_quiet_gap_sends_a_ping_and_keeps_the_socket(exc):
    ws = FakeWS([TIMEOUT, {"id": 2, "type": "pong"}, _event()], exc=exc)
    lst, calls = _listener()
    lst._next_id = 1
    with pytest.raises(ConnectionError) as err:
        lst._pump(ws)
    assert "drained" in str(err.value)          # the script ended, not a drop
    assert ws.sent == [{"id": 2, "type": "ping"}]
    assert len(calls) == 1                       # the event still arrived


def test_ping_ids_keep_increasing_across_gaps():
    ws = FakeWS([TIMEOUT, {"id": 2, "type": "pong"},
                 TIMEOUT, {"id": 3, "type": "pong"}])
    lst, _ = _listener()
    lst._next_id = 1
    with pytest.raises(ConnectionError):
        lst._pump(ws)
    assert [m["id"] for m in ws.sent] == [2, 3]


def test_an_unanswered_ping_drops_the_dead_socket():
    clock = Clock()

    def later():
        clock.t += ha_events.PONG_TIMEOUT_SECONDS + 1
        raise WebSocketTimeoutException("Connection timed out")

    ws = FakeWS([TIMEOUT, later])
    lst, _ = _listener()
    with pytest.raises(ConnectionError) as err:
        lst._pump(ws, clock=clock)
    assert "no pong" in str(err.value)
    assert len(ws.sent) == 1


def test_a_short_wait_for_the_pong_does_not_drop():
    clock = Clock()

    def soon():
        clock.t += 1
        raise WebSocketTimeoutException("Connection timed out")

    ws = FakeWS([TIMEOUT, soon, {"type": "pong", "id": 2}])
    lst, _ = _listener()
    with pytest.raises(ConnectionError) as err:
        lst._pump(ws, clock=clock)
    assert "drained" in str(err.value) and len(ws.sent) == 1


def test_other_errors_still_raise_at_once():
    class Boom(Exception):
        pass

    class WS(FakeWS):
        def recv(self):
            raise Boom("reset by peer")

    lst, _ = _listener()
    with pytest.raises(Boom):
        lst._pump(WS([]))


def test_a_drop_is_re_subscribed_at_once_and_quietly(caplog):
    lst_box = {}

    def stop():
        lst_box["l"]._stop.set()
        raise ConnectionError("stopping")

    sockets = [
        # healthy (a real event arrived), then drops
        FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}, _event()]),
        FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}, stop]),
    ]
    lst, _ = _listener(ws_factory=lambda url: sockets.pop(0))
    lst_box["l"] = lst
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="puffpai.ha_events"):
        lst._run()
    assert time.monotonic() - started < ha_events.BACKOFF_START_SECONDS
    assert sockets == []
    assert lst.status()["reconnects"] == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "connected" in infos[0]


def test_a_failed_re_subscribe_warns_once_then_backs_off(monkeypatch, caplog):
    lst_box = {}
    attempts = {"n": 0}

    def factory(url):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeWS([{"type": "auth_required"}, {"type": "auth_ok"},
                           _event()])
        if attempts["n"] >= 3:
            lst_box["l"]._stop.set()
        raise OSError("HA is down")

    lst, _ = _listener(ws_factory=factory)
    lst_box["l"] = lst
    waits = []
    real_wait = lst._stop.wait
    monkeypatch.setattr(lst._stop, "wait",
                        lambda t=None: waits.append(t) or real_wait(0))
    with caplog.at_level(logging.DEBUG, logger="puffpai.ha_events"):
        lst._run()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "HA is down" in warnings[0].getMessage()
    assert waits[0] < 1 and waits[1] == ha_events.BACKOFF_START_SECONDS



# ── a socket that auths and then closes at once ─────────────────────

def _flapping_run(monkeypatch, attempts_before_stop, clock):
    lst_box, made = {}, {"n": 0}

    def factory(url):
        made["n"] += 1
        if made["n"] >= attempts_before_stop:
            lst_box["l"]._stop.set()
        # authenticates, then the socket closes immediately
        return FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}])

    lst, _ = _listener(ws_factory=factory)
    lst_box["l"] = lst
    lst._clock = clock
    waits = []

    def fake_wait(t=None):
        waits.append(t)
        clock.t += t or 0
        return lst._stop.is_set()

    monkeypatch.setattr(lst._stop, "wait", fake_wait)
    return lst, waits, made


def test_auth_then_close_backs_off_instead_of_looping(monkeypatch, caplog):
    clock = Clock()
    lst, waits, made = _flapping_run(monkeypatch, 9, clock)
    with caplog.at_level(logging.DEBUG, logger="puffpai.ha_events"):
        lst._run()
    assert made["n"] == 9
    assert waits[:8] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0]
    assert 0.5 not in waits                     # never the quick re-subscribe
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1                    # 315 s of failing: one warning


def test_a_long_flap_warns_once_per_ten_minutes(monkeypatch, caplog):
    clock = Clock()
    lst, waits, made = _flapping_run(monkeypatch, 30, clock)
    with caplog.at_level(logging.DEBUG, logger="puffpai.ha_events"):
        lst._run()
    elapsed = sum(waits)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert elapsed > 2 * ha_events.WARN_EVERY_SECONDS
    assert len(warnings) == int(elapsed // ha_events.WARN_EVERY_SECONDS) or         len(warnings) == int(elapsed // ha_events.WARN_EVERY_SECONDS) + 1
    assert 2 <= len(warnings) <= 4


def test_a_session_that_stayed_up_a_minute_is_healthy(monkeypatch):
    clock = Clock()
    lst_box, made = {}, {"n": 0}

    def quiet_minute():
        clock.t += ha_events.HEALTHY_SESSION_SECONDS + 1
        raise ConnectionError("dropped after a quiet minute")

    def factory(url):
        made["n"] += 1
        if made["n"] >= 2:
            lst_box["l"]._stop.set()
        return FakeWS([{"type": "auth_required"}, {"type": "auth_ok"},
                       quiet_minute])

    lst, _ = _listener(ws_factory=factory)
    lst_box["l"] = lst
    lst._clock = clock
    waits = []
    monkeypatch.setattr(lst._stop, "wait",
                        lambda t=None: waits.append(t) or lst._stop.is_set())
    lst._run()
    assert waits[0] == 0.5                       # quick, quiet re-subscribe
