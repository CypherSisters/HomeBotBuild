"""At the surface: the reminder fastpath must send the day it heard.

``_try_add_task`` used to invoke ``app_add_task`` with the title only, so
"remind me to call the dentist Monday" stored the whole phrase as the title
and let the tool fall back to its tomorrow default — which is how a Monday
reminder was validated as landing on Sunday.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture()
def api():
    import puffpai.api_server as api_server
    return api_server


class _Tool:
    def __init__(self, sink):
        self.sink = sink

    def invoke(self, payload):
        self.sink.append(payload)
        return "Task added."


@pytest.fixture()
def calls(api, monkeypatch):
    sink = []

    class _Tools:
        tool_dict = {"app_add_task": _Tool(sink)}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    return sink


def test_the_day_in_the_sentence_becomes_the_due_date(api, calls):
    """The exact phrasing that stored Sunday for a Monday reminder."""
    assert api._try_add_task("remind me to call the dentist Monday")
    assert len(calls) == 1
    payload = calls[0]
    assert payload["title"] == "call the dentist"
    due = datetime.fromisoformat(payload["due_date"])
    assert due.weekday() == 0, "'Monday' did not resolve to a Monday"
    assert due.date() > datetime.now().date()


def test_a_time_rides_along(api, calls):
    assert api._try_add_task("remind me to call the dentist Monday at 3pm")
    due = datetime.fromisoformat(calls[0]["due_date"])
    assert (due.hour, due.minute) == (15, 0)
    assert due.weekday() == 0


def test_a_reminder_with_no_day_is_unchanged(api, calls):
    assert api._try_add_task("remind me to do the dishes")
    assert calls[0] == {"title": "do the dishes"}


def test_a_bare_time_still_gets_a_due_stamp(api, calls):
    """'remind me to take my medication at 8' — the D15 phrasing."""
    assert api._try_add_task("remind me to take my medication at 8")
    due = datetime.fromisoformat(calls[0]["due_date"])
    assert (due.hour, due.minute) == (8, 0)
    assert due >= datetime.now() - timedelta(minutes=1)
    assert calls[0]["title"] == "take my medication"


def test_the_fastpath_still_declines_what_it_declined_before(api, calls):
    for text in ("what's on my to-do list", "add milk to the shopping list",
                 "set a timer for 5 minutes"):
        assert api._try_add_task(text) is None
    assert calls == []
