"""`alarm_list` knows the scheduler's two ring states.

If `alarm_list` counted only `running | paused | ringing`, an alarm going
off RIGHT NOW (`status: "fired"`) or one that came due while the service was
off (`status: "missed"`) would be absent from "what alarms do I have", and the
reply would be "No active timers or alarms" while it is audibly ringing.

Real `AppData` on a temp data dir. Every row is invented.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


def _put(app, items):
    data = app.get_data('alarmflow') or {}
    data['items'] = items
    app.set_data('alarmflow', data)


def _alarm(desc, status, hour=7, minute=0):
    target = time.localtime()
    stamp = time.mktime((target.tm_year, target.tm_mon, target.tm_mday,
                         hour, minute, 0, 0, 0, -1))
    return {'id': f'test-fake-{status}', 'desc': desc, 'mode': 'alarm',
            'status': status, 'ts': int(stamp * 1000), 'tot': 0, 'rem': 0,
            'targetMs': int(stamp * 1000), 'repeat': 'once'}


def _timer(desc, status, remaining=90):
    return {'id': f'test-fake-timer-{status}', 'desc': desc, 'mode': 'timer',
            'status': status, 'ts': int(time.time() * 1000),
            'tot': remaining, 'rem': remaining}


# ── the ring states are listed ──────────────────────────────────────────────

def test_an_alarm_ringing_right_now_is_listed(app):
    _put(app, [_alarm('test-fake wake up', 'fired', hour=7)])
    reply = app.alarm_list()
    assert "No active timers or alarms" not in reply
    assert "test-fake wake up" in reply
    assert "ringing now" in reply


def test_a_missed_alarm_is_listed_with_its_time(app):
    _put(app, [_alarm('test-fake pills', 'missed', hour=7, minute=0)])
    reply = app.alarm_list()
    assert "test-fake pills" in reply
    assert "missed at 7:00 AM" in reply


def test_a_fired_alarm_names_the_time_it_was_set_for(app):
    _put(app, [_alarm('test-fake wake up', 'fired', hour=6, minute=45)])
    reply = app.alarm_list()
    assert "alarm for 6:45 AM" in reply


def test_a_fired_timer_says_ringing_now(app):
    _put(app, [_timer('test-fake pasta', 'fired')])
    reply = app.alarm_list()
    assert "test-fake pasta" in reply
    assert "ringing now" in reply


def test_a_missed_timer_says_missed(app):
    _put(app, [_timer('test-fake pasta', 'missed')])
    assert "(missed)" in app.alarm_list()


# ── ordering and counting ───────────────────────────────────────────────────

def test_what_is_ringing_is_said_first(app):
    _put(app, [_alarm('test-fake later', 'running', hour=9),
               _alarm('test-fake now', 'fired', hour=7)])
    reply = app.alarm_list()
    assert reply.index("test-fake now") < reply.index("test-fake later")


def test_every_reported_state_is_counted(app):
    _put(app, [_alarm('test-fake a', 'fired', hour=7),
               _alarm('test-fake b', 'missed', hour=6),
               _alarm('test-fake c', 'running', hour=9),
               _timer('test-fake d', 'paused')])
    reply = app.alarm_list()
    assert "You have 4 active items" in reply


def test_stopped_items_are_still_left_out(app):
    _put(app, [_alarm('test-fake done', 'stopped', hour=7)])
    assert app.alarm_list() == "No active timers or alarms."


# ── the other states read as before ─────────────────────────────────────────

def test_a_plain_running_alarm_reads_exactly_as_before(app):
    _put(app, [_alarm('test-fake wake up', 'running', hour=6, minute=45)])
    reply = app.alarm_list()
    assert reply == ("You have 1 active item:\n"
                     "test-fake wake up: alarm at 6:45 AM")


def test_a_running_timer_reads_exactly_as_before(app):
    _put(app, [_timer('test-fake pasta', 'running', remaining=90)])
    reply = app.alarm_list()
    assert reply == ("You have 1 active item:\n"
                     "test-fake pasta: 1m 30s remaining")


def test_a_paused_timer_still_says_paused(app):
    _put(app, [_timer('test-fake pasta', 'paused', remaining=90)])
    assert "(paused)" in app.alarm_list()


def test_a_row_with_no_status_cannot_break_the_read(app):
    _put(app, [{'id': 'test-fake-broken', 'desc': 'x', 'mode': 'alarm'},
               _alarm('test-fake ok', 'running', hour=8)])
    assert "test-fake ok" in app.alarm_list()
