""""set an alarm for seven" must be heard as a clock time.

If every alarm pattern wants digits, "set an alarm for seven" matches neither
the clarifier nor the setter, the turn falls to the agent graph, and 30
seconds later it has guessed 7 AM and written the alarm. Spoken clock words
are normalised to digits before the existing patterns run; an hour with no
half of the day still ASKS, exactly as "alarm 7" does.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402


class _Tool:
    def __init__(self, sink):
        self.sink = sink

    def invoke(self, kwargs):
        self.sink.append(kwargs)
        return "Alarm set!"


@pytest.fixture()
def alarm_calls(monkeypatch):
    calls = []

    class _Tools:
        tool_dict = {"app_set_alarm": _Tool(calls)}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    monkeypatch.setattr(api, "_app_installed", lambda name: True)
    return calls


# ── the normaliser itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("set an alarm for seven", "set an alarm for 7"),
    ("wake me up at six thirty", "wake me up at 6:30"),
    ("set an alarm for half past seven", "set an alarm for 7:30"),
    ("set an alarm for a quarter to eight", "set an alarm for 7:45"),
    ("set an alarm for quarter past six", "set an alarm for 6:15"),
    ("set an alarm for eight o'clock", "set an alarm for 8"),
    ("wake me at seven forty-five", "wake me at 7:45"),
    ("set an alarm for seven oh five", "set an alarm for 7:05"),
])
def test_spoken_clock_words_become_digits(text, expected):
    assert api._normalize_spoken_clock(text) == expected


def test_a_sentence_with_no_number_words_is_untouched():
    text = "set an alarm for 7:30 pm"
    assert api._normalize_spoken_clock(text) is text or \
        api._normalize_spoken_clock(text) == text


# ── the clarifier asks rather than guessing ─────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "set an alarm for seven",
    "alarm at seven",
    "set an alarm for six thirty",
    "set an alarm for half past seven",
    "set an alarm for a quarter to eight",
])
def test_a_spoken_hour_with_no_half_of_the_day_asks(phrase):
    reply = api._try_alarm_clarify(phrase)
    assert reply is not None, phrase
    assert "AM or" in reply and "PM" in reply


def test_a_bare_spoken_hour_writes_nothing_until_answered(alarm_calls):
    reply = api._try_alarm_set("set an alarm for seven")
    assert reply is not None and "AM or" in reply
    assert alarm_calls == []


# ── with a half of the day named, it just sets it ───────────────────────────

@pytest.mark.parametrize("phrase,expected", [
    ("set an alarm for seven pm", (19, 0)),
    ("set an alarm for seven a.m.", (7, 0)),
    ("set an alarm for half past seven pm", (19, 30)),
    ("wake me up at seven", (7, 0)),          # "wake me" names the morning
])
def test_a_spoken_time_with_a_half_of_the_day_is_set(alarm_calls, phrase,
                                                     expected):
    assert api._try_alarm_clarify(phrase) is None, phrase
    reply = api._try_alarm_set(phrase)
    assert reply == "Alarm set!"
    assert (alarm_calls[-1]["hour"], alarm_calls[-1]["minute"]) == expected


@pytest.mark.parametrize("phrase,expected", [
    ("set an alarm for seven pm", (19, 0)),
    ("set an alarm for quarter to eight in the morning", (7, 45)),
    ("set an alarm for six thirty am", (6, 30)),
])
def test_time_parsing_of_spoken_phrases(phrase, expected):
    assert api._parse_alarm_time(phrase) == expected


# ── nothing about the digit phrasings changed ───────────────────────────────

@pytest.mark.parametrize("phrase", ["alarm 7", "set an alarm for 7",
                                    "set an alarm for 7:30", "set my alarm for 8"])
def test_digit_hours_still_ask(phrase):
    reply = api._try_alarm_clarify(phrase)
    assert reply is not None and "AM or" in reply


@pytest.mark.parametrize("phrase,expected", [
    ("set an alarm for 7am", (7, 0)),
    ("set an alarm for 7:30 pm", (19, 30)),
    ("set an alarm for noon", (12, 0)),
    ("set an alarm for midnight", (0, 0)),
    ("wake me up at 7 in the morning", (7, 0)),
])
def test_digit_times_parse_as_before(phrase, expected):
    assert api._parse_alarm_time(phrase) == expected


def test_a_timer_is_still_not_an_alarm():
    assert api._try_alarm_clarify("set a timer for seven minutes") is None
    assert api._try_alarm_set("set a timer for seven minutes") is None
