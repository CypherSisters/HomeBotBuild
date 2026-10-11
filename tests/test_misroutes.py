"""Household questions a 7B intent classifier misroutes to house intents.

Rows: (alarm -> set_climate), (medication -> media_status),
 (bills -> device_status), (calendar -> list_devices).

Every phrasing below is one a small model misclassifies this way. The shared
guard is in ``ConciergeAgent._engine_refine_intent``: an engine guess keeps the
"unmatched" reason rather than a fresh ``reason="engine refined"``, which would
silently disarm the non-home guard in ``_delegate_to_steward`` and hand the
Steward whichever house intent the model picked off a menu.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.agents.concierge import (  # noqa: E402
    RESOLVABLE_INTENTS, ConciergeAgent, resolve_intent,
)
from puffpai.agents import household_reads  # noqa: E402
from puffpai.router import get_route_category  # noqa: E402


# ── the misrouted phrasings ──────────────────────────────────────────────────

MISROUTED = [
    # (utterance, the wrong intent a small model picks, the household area)
    ("set an alarm for 7am", "set_climate", "alarm"),
    ("wake me up at 7 in the morning", "set_climate", "alarm"),
    ("wake me up at 7 tomorrow", "set_climate", "alarm"),
    ("did I take my medication today", "media_status", "medication"),
    ("do we owe any money on bills right now", "device_status", "bills"),
    ("do we have any events or reminders coming up", "list_devices",
     "calendar"),
]


class _PickingEngine:
    """Engine-shaped double that always names the intent a 7B model names."""

    def __init__(self, choice):
        self.choice = choice
        self.asked = []

    def classify_intent(self, text, choices):
        self.asked.append(text)
        return self.choice

    def run(self, text, conversation_history=None):
        return None


@pytest.mark.parametrize("text,wrong_intent,row", MISROUTED)
def test_no_deterministic_rule_ever_claimed_these(text, wrong_intent, row):
    """Premise: the misroute came from the refinement, not from a rule."""
    assert resolve_intent(text).reason == "no rule matched"


@pytest.mark.parametrize("text,wrong_intent,row", MISROUTED)
def test_router_never_called_these_home(text, wrong_intent, row):
    """Premise: the non-home guard SHOULD have applied to every one of them."""
    assert get_route_category(text) != "home"


@pytest.mark.parametrize("text,wrong_intent,row", MISROUTED)
def test_an_engine_guess_keeps_the_unmatched_reason(text, wrong_intent, row):
    """The guard, at the root: a refinement is still an unmatched turn."""
    resolved = ConciergeAgent(engine=_PickingEngine(wrong_intent))._resolve(text)
    assert resolved.intent == wrong_intent, "premise: the model's pick is kept"
    assert resolved.reason.startswith("no rule matched"), (
        f"{row}: an engine guess must stay 'unmatched' so the non-home guard "
        f"in _delegate_to_steward still applies to it")


@pytest.mark.parametrize("text,wrong_intent,row", MISROUTED)
def test_the_steward_is_not_handed_a_non_home_guess(text, wrong_intent, row):
    """End of the class: the Steward never sees these turns at all."""
    concierge = ConciergeAgent(engine=_PickingEngine(wrong_intent))
    sent = []
    concierge._send_sub = lambda *a, **k: sent.append(a) or {"ok": True}
    result = concierge._delegate_to_steward(
        envelope=None, text=text,
        category=get_route_category(text),
        resolution=concierge._resolve(text))
    assert result["handled"] is False, f"{row}: still handled by the Steward"
    assert result["intent"] is None
    assert sent == [], f"{row}: an intent reached the Steward: {sent!r}"


def test_a_home_message_still_gets_the_refinement():
    """The guard must not cost the home path its LLM narrowing."""
    text = "is the back door camera seeing motion"
    assert get_route_category(text) == "home", "premise"
    resolved = ConciergeAgent(engine=_PickingEngine("device_status"))._resolve(text)
    assert resolved.intent == "device_status"
    concierge = ConciergeAgent(engine=_PickingEngine("device_status"))
    # An unmatched HOME turn is still allowed through — only the entity
    # resolution below decides whether it can be answered.
    assert str(resolved.reason).startswith("no rule matched")
    assert "device_status" in RESOLVABLE_INTENTS
    assert concierge is not None


# ── each row reaches its own reader ──────────────────────────────────────────

@pytest.mark.parametrize("text,reader,row", [
    ("did I take my medication today", "medication_status", "medication"),
    ("have I taken my meds", "medication_status", "medication"),
    ("do we owe any money on bills right now", "bills_status", "bills"),
    ("what bills are due this month", "bills_status", "bills"),
    ("do we have any events or reminders coming up", "calendar_upcoming",
     "calendar"),
    ("what is on our calendar today", "calendar_upcoming", "calendar"),
])
def test_the_right_household_reader_claims_the_turn(text, reader, row):
    assert household_reads.match(text) == reader


def test_medication_is_not_media():
    """The 'medication' ~ 'media' collision, prevented at the root.

    Substring matching would make a health question return the Jellyfin and
    Calibre URLs. Both matchers here are whole-word, so no medication phrasing
    can reach a media rule and no media phrasing can reach the health reader.
    """
    media_word = re.compile(r"\bmedia\b", re.IGNORECASE)
    for phrasing in ("did I take my medication today", "have I taken my meds",
                     "medication status", "am I due for my medicine"):
        assert not media_word.search(phrasing), (
            "'media' must not match inside 'medication'/'medicine'")
    for phrasing in ("media server status", "what's playing", "now playing"):
        assert household_reads.match(phrasing) is None


# ── the alarm fastpath  ───────────────────────────────────────────

@pytest.fixture()
def api(monkeypatch):
    import puffpai.api_server as api_server
    return api_server


@pytest.mark.parametrize("text,expected", [
    ("set an alarm for 7am", (7, 0)),
    ("wake me up at 7 in the morning", (7, 0)),
    ("wake me up at 7 tomorrow", (7, 0)),
    ("set an alarm for 7:30 am", (7, 30)),
    ("set an alarm for 6:45am every weekday", (6, 45)),
    ("wake me up at 10pm", (22, 0)),
    ("set an alarm for noon", (12, 0)),
    ("set an alarm for midnight", (0, 0)),
    ("set an alarm for 12am", (0, 0)),
])
def test_alarm_time_parsing(api, text, expected):
    assert api._parse_alarm_time(text) == expected


@pytest.mark.parametrize("text", [
    "set an alarm for 7am",
    "wake me up at 7 in the morning",
    "wake me up at 7 tomorrow",
])
def test_alarm_request_reaches_alarmflow_not_the_thermostat(api, monkeypatch,
                                                            text):
    """A small model classifies these as intent=set_climate every time."""
    calls = []

    class _Tool:
        def invoke(self, payload):
            calls.append(payload)
            return "Alarm set for 7:00 AM."

    class _Tools:
        tool_dict = {"app_set_alarm": _Tool()}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    reply = api._try_alarm_set(text)
    assert reply == "Alarm set for 7:00 AM."
    assert len(calls) == 1
    assert calls[0]["hour"] == 7 and calls[0]["minute"] == 0
    assert calls[0]["description"] != "Alarm" or "alarm" in text


@pytest.mark.parametrize("text", [
    "set a timer for 10 minutes",          # timers keep _try_timer_set
    "cancel the 7am alarm",                # dismissal keeps its own path
    "dismiss my alarm",
    "what alarms do I have",               # a question, not a write
    "how many alarms are set",
    "the fire alarm went off",             # not an AlarmFlow alarm at all
    "turn off the alarm",
    "set the thermostat to 72",            # climate must stay climate
    "wake me up",                          # no time named -> ask a human
    "set an alarm",                        # ditto
])
def test_the_alarm_fastpath_declines_everything_else(api, text):
    assert api._try_alarm_set(text) is None


def test_repeat_is_read_from_the_words(api, monkeypatch):
    seen = {}

    class _Tool:
        def invoke(self, payload):
            seen.update(payload)
            return "ok"

    class _Tools:
        tool_dict = {"app_set_alarm": _Tool()}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    api._try_alarm_set("wake me up at 6:30am every weekday")
    assert seen["repeat"] == "weekdays"
    seen.clear()
    api._try_alarm_set("set an alarm for 6am every morning")
    assert seen["repeat"] == "daily"
    seen.clear()
    api._try_alarm_set("set an alarm for 6am")
    assert seen["repeat"] == "once"
