"""Tests for app tool input parsing and natural date/time resolution.

Validates parse_natural_date(), parse_natural_time(), parse_duration(),
and parse_flexible_input() helpers that make the LLM-to-tool interface
resilient to varied input formats from small language models like
dolphin-llama3:8b.
"""

import json
import sys
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai.app_input_parser import parse_natural_date as _parse_natural_date
from puffpai.app_input_parser import parse_natural_time as _parse_natural_time
from puffpai.app_input_parser import parse_duration as _parse_duration
from puffpai.app_input_parser import parse_flexible_input as _parse_flexible_input


# ── _parse_natural_date tests ────────────────────────────────────────────


class TestParseNaturalDate:
    """Convert natural language dates to ISO format."""

    def test_today(self):
        result = _parse_natural_date("today")
        assert result == datetime.now().strftime('%Y-%m-%d')

    def test_tomorrow(self):
        expected = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        assert _parse_natural_date("tomorrow") == expected

    def test_yesterday(self):
        expected = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        assert _parse_natural_date("yesterday") == expected

    def test_iso_passthrough(self):
        assert _parse_natural_date("2026-03-06") == "2026-03-06"

    def test_iso_with_time_passthrough(self):
        assert _parse_natural_date("2026-03-06T15:00") == "2026-03-06T15:00"

    def test_in_n_days(self):
        expected = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
        assert _parse_natural_date("in 3 days") == expected

    def test_next_week(self):
        expected = (datetime.now() + timedelta(weeks=1)).strftime('%Y-%m-%d')
        assert _parse_natural_date("next week") == expected

    def test_day_of_week(self):
        """Named day gives future date (within 7 days)."""
        result = _parse_natural_date("monday")
        parsed = datetime.strptime(result, '%Y-%m-%d')
        assert parsed.weekday() == 0  # Monday
        assert parsed.date() > datetime.now().date()

    def test_next_day_of_week(self):
        """'next friday' gives future date."""
        result = _parse_natural_date("next friday")
        parsed = datetime.strptime(result, '%Y-%m-%d')
        assert parsed.weekday() == 4  # Friday
        assert parsed.date() > datetime.now().date()

    def test_month_day(self):
        """'march 6' gives ISO date."""
        result = _parse_natural_date("march 6")
        assert '-03-06' in result

    def test_month_day_ordinal(self):
        """'december 25th' gives ISO date."""
        result = _parse_natural_date("december 25th")
        assert '-12-25' in result

    def test_month_day_with_year(self):
        result = _parse_natural_date("march 6, 2026")
        assert result == "2026-03-06"

    def test_us_date_format(self):
        """US date format 3/6/2026."""
        result = _parse_natural_date("3/6/2026")
        assert result == "2026-03-06"

    def test_us_date_no_year(self):
        """US date format 3/6 assumes current year."""
        result = _parse_natural_date("3/6")
        now = datetime.now()
        assert f"-03-06" in result

    def test_unknown_passthrough(self):
        """Unknown text returns as-is for downstream handling."""
        assert _parse_natural_date("someday") == "someday"

    def test_strips_quotes(self):
        expected = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        assert _parse_natural_date("'tomorrow'") == expected
        assert _parse_natural_date('"tomorrow"') == expected


# ── _parse_flexible_input tests ──────────────────────────────────────────


class TestParseFlexibleInput:
    """Parse various LLM output formats into structured fields."""

    FIELDS = ['title', 'due', 'cat', 'assigned_to', 'prio']

    def test_pipe_delimited(self):
        result = _parse_flexible_input(
            "Pickup Sudafed|2026-03-06|other|dad|medium", self.FIELDS)
        assert result['title'] == "Pickup Sudafed"
        assert result['due'] == "2026-03-06"
        assert result['cat'] == "other"
        assert result['assigned_to'] == "dad"
        assert result['prio'] == "medium"

    def test_pipe_delimited_minimal(self):
        result = _parse_flexible_input(
            "Pickup Sudafed|tomorrow", self.FIELDS)
        assert result['title'] == "Pickup Sudafed"
        assert result['due'] == "tomorrow"
        assert result['cat'] is None
        assert result['assigned_to'] is None

    def test_json_format(self):
        inp = json.dumps({
            "title": "Buy groceries",
            "due": "2026-03-06",
            "cat": "family"
        })
        result = _parse_flexible_input(inp, self.FIELDS)
        assert result['title'] == "Buy groceries"
        assert result['due'] == "2026-03-06"
        assert result['cat'] == "family"

    def test_key_value_format(self):
        result = _parse_flexible_input(
            'title="Buy milk", due="tomorrow"', self.FIELDS)
        assert result['title'] == "Buy milk"
        assert result['due'] == "tomorrow"

    def test_single_value_goes_to_first_field(self):
        result = _parse_flexible_input("Just a task name", self.FIELDS)
        assert result['title'] == "Just a task name"
        assert result['due'] is None

    def test_parameter_name_stripped(self):
        """LLM writes task_info='Pickup|tomorrow' — strip the param name."""
        result = _parse_flexible_input(
            "task_info='Pickup Sudafed|tomorrow'", self.FIELDS)
        assert result['title'] == "Pickup Sudafed"
        assert result['due'] == "tomorrow"

    def test_quoted_values_stripped(self):
        result = _parse_flexible_input(
            "'Clean garage|next saturday|family|dad|low'", self.FIELDS)
        assert result['title'] == "Clean garage"
        assert result['due'] == "next saturday"

    def test_comma_separated(self):
        result = _parse_flexible_input(
            "Buy milk, tomorrow, family, mom, medium", self.FIELDS)
        assert result['title'] == "Buy milk"
        assert result['due'] == "tomorrow"
        assert result['cat'] == "family"

    def test_event_info_stripped(self):
        result = _parse_flexible_input(
            "event_info='Team Meeting|2026-03-06T15:00|work'",
            ['title', 'start', 'cat', 'assigned_to', 'prio'])
        assert result['title'] == "Team Meeting"
        assert result['start'] == "2026-03-06T15:00"


# ── End-to-end tool smoke tests (mocked AppData) ────────────────────────


class TestAppToolIntegration:
    """Verify that the tool wrappers accept various input formats."""

    def test_add_task_pipe_format(self):
        """Pipe-delimited input is parsed correctly."""
        fields = ['title', 'due', 'cat', 'assigned_to', 'prio']
        parsed = _parse_flexible_input(
            "Pickup Sudafed|tomorrow|other|dad|medium", fields)
        assert parsed['title'] == "Pickup Sudafed"
        due = _parse_natural_date(parsed['due'])
        expected = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        assert due == expected

    def test_add_task_natural_date(self):
        """Natural date resolution works end-to-end."""
        fields = ['title', 'due', 'cat', 'assigned_to', 'prio']
        parsed = _parse_flexible_input(
            "Doctor appointment|next monday|medical|mom|high", fields)
        due = _parse_natural_date(parsed['due'])
        dt = datetime.strptime(due, '%Y-%m-%d')
        assert dt.weekday() == 0  # Monday

    def test_add_event_with_default_time(self):
        """Event date without time gets default 09:00."""
        import re
        start = _parse_natural_date("tomorrow")
        if re.match(r'^\d{4}-\d{2}-\d{2}$', start):
            start += 'T09:00'
        assert 'T09:00' in start


# ── parse_natural_time tests ─────────────────────────────────────────────


class TestParseNaturalTime:
    """Convert natural language times to (hour, minute) tuples."""

    def test_noon(self):
        assert _parse_natural_time("noon") == (12, 0)

    def test_midnight(self):
        assert _parse_natural_time("midnight") == (0, 0)

    def test_12_noon(self):
        assert _parse_natural_time("12 noon") == (12, 0)

    def test_12_midnight(self):
        assert _parse_natural_time("12 midnight") == (0, 0)

    def test_am_time_with_minutes(self):
        """'7:30 AM' → (7, 30)."""
        assert _parse_natural_time("7:30 AM") == (7, 30)

    def test_pm_time_with_minutes(self):
        """'3:45 PM' → (15, 45)."""
        assert _parse_natural_time("3:45 PM") == (15, 45)

    def test_12pm_is_noon(self):
        """'12:00 PM' → (12, 0), not (24, 0)."""
        assert _parse_natural_time("12:00 pm") == (12, 0)

    def test_12am_is_midnight(self):
        """'12:00 AM' → (0, 0)."""
        assert _parse_natural_time("12:00 am") == (0, 0)

    def test_pm_no_minutes(self):
        """'3pm' → (15, 0)."""
        assert _parse_natural_time("3pm") == (15, 0)

    def test_am_no_minutes(self):
        """'7am' → (7, 0)."""
        assert _parse_natural_time("7am") == (7, 0)

    def test_am_with_space(self):
        """'7 am' → (7, 0)."""
        assert _parse_natural_time("7 am") == (7, 0)

    def test_24_hour_format(self):
        """'15:00' → (15, 0)."""
        assert _parse_natural_time("15:00") == (15, 0)

    def test_24_hour_with_minutes(self):
        """'14:30' → (14, 30)."""
        assert _parse_natural_time("14:30") == (14, 30)

    def test_military_time(self):
        """'0800' → (8, 0)."""
        assert _parse_natural_time("0800") == (8, 0)

    def test_military_time_afternoon(self):
        """'1430' → (14, 30)."""
        assert _parse_natural_time("1430") == (14, 30)

    def test_plain_hour(self):
        """'7' → (7, 0) — assume AM hour."""
        assert _parse_natural_time("7") == (7, 0)

    def test_plain_hour_afternoon(self):
        """'15' → (15, 0) — 24h format."""
        assert _parse_natural_time("15") == (15, 0)

    def test_strips_quotes(self):
        """Quoted inputs are handled."""
        assert _parse_natural_time("'3pm'") == (15, 0)
        assert _parse_natural_time('"7:30 AM"') == (7, 30)

    def test_unparseable_returns_none(self):
        """Garbage input returns None."""
        assert _parse_natural_time("sometime") is None
        assert _parse_natural_time("around three") is None

    def test_edge_case_0_am(self):
        """'0' is technically hour 0 = midnight."""
        assert _parse_natural_time("0") == (0, 0)


# ── parse_duration tests ─────────────────────────────────────────────────


class TestParseDuration:
    """Parse duration strings into hours/minutes/seconds dicts."""

    def test_minutes_word(self):
        """'5 minutes' → {h:0, m:5, s:0}."""
        result = _parse_duration("5 minutes")
        assert result == {'hours': 0, 'minutes': 5, 'seconds': 0}

    def test_hour_word(self):
        """'1 hour' → {h:1, m:0, s:0}."""
        result = _parse_duration("1 hour")
        assert result == {'hours': 1, 'minutes': 0, 'seconds': 0}

    def test_compound_duration(self):
        """'1 hour 30 minutes' → {h:1, m:30, s:0}."""
        result = _parse_duration("1 hour 30 minutes")
        assert result == {'hours': 1, 'minutes': 30, 'seconds': 0}

    def test_compound_with_and(self):
        """'2 hours and 15 minutes' → {h:2, m:15, s:0}."""
        result = _parse_duration("2 hours and 15 minutes")
        assert result == {'hours': 2, 'minutes': 15, 'seconds': 0}

    def test_seconds_word(self):
        """'90 seconds' → {h:0, m:0, s:90}."""
        result = _parse_duration("90 seconds")
        assert result == {'hours': 0, 'minutes': 0, 'seconds': 90}

    def test_compact_hours_minutes(self):
        """'1h30m' → {h:1, m:30, s:0}."""
        result = _parse_duration("1h30m")
        assert result == {'hours': 1, 'minutes': 30, 'seconds': 0}

    def test_compact_minutes_only(self):
        """'5m' → {h:0, m:5, s:0}."""
        result = _parse_duration("5m")
        assert result == {'hours': 0, 'minutes': 5, 'seconds': 0}

    def test_compact_minutes_seconds(self):
        """'5m30s' → {h:0, m:5, s:30}."""
        result = _parse_duration("5m30s")
        assert result == {'hours': 0, 'minutes': 5, 'seconds': 30}

    def test_plain_number_assumes_minutes(self):
        """'10' → {h:0, m:10, s:0} — assume minutes."""
        result = _parse_duration("10")
        assert result == {'hours': 0, 'minutes': 10, 'seconds': 0}

    def test_abbreviated_words(self):
        """'5 min' and '2 hr' → correct parsing."""
        assert _parse_duration("5 min") == {'hours': 0, 'minutes': 5, 'seconds': 0}
        assert _parse_duration("2 hr") == {'hours': 2, 'minutes': 0, 'seconds': 0}

    def test_strips_quotes(self):
        result = _parse_duration("'10 minutes'")
        assert result == {'hours': 0, 'minutes': 10, 'seconds': 0}

    def test_unparseable_returns_none(self):
        """Non-duration text returns None."""
        assert _parse_duration("forever") is None
        assert _parse_duration("a while") is None


# ── AlarmFlow integration tests ──────────────────────────────────────────


class TestAlarmFlowParsing:
    """Test alarm time parsing as used by app_set_alarm."""

    def test_alarm_pipe_with_time_string(self):
        """'Wake up|7:30 AM' parses description and time."""
        fields = ['desc', 'time']
        parsed = _parse_flexible_input("Wake up|7:30 AM", fields)
        assert parsed['desc'] == "Wake up"
        time_result = _parse_natural_time(parsed['time'])
        assert time_result == (7, 30)

    def test_alarm_pipe_with_pm(self):
        """'Meeting reminder|3pm' parses correctly."""
        fields = ['desc', 'time']
        parsed = _parse_flexible_input("Meeting reminder|3pm", fields)
        assert parsed['desc'] == "Meeting reminder"
        time_result = _parse_natural_time(parsed['time'])
        assert time_result == (15, 0)

    def test_alarm_single_time_input(self):
        """Just '7:30 AM' goes to first field — parseable as time."""
        time_result = _parse_natural_time("7:30 AM")
        assert time_result == (7, 30)

    def test_alarm_json_format(self):
        """JSON alarm input works."""
        inp = json.dumps({"desc": "Wake up", "time": "6:00 AM"})
        fields = ['desc', 'time']
        parsed = _parse_flexible_input(inp, fields)
        assert parsed['desc'] == "Wake up"
        time_result = _parse_natural_time(parsed['time'])
        assert time_result == (6, 0)

    def test_alarm_info_stripped(self):
        """alarm_info='Wake up|7am' → stripped."""
        fields = ['desc', 'time']
        parsed = _parse_flexible_input("alarm_info='Wake up|7am'", fields)
        assert parsed['desc'] == "Wake up"
        time_result = _parse_natural_time(parsed['time'])
        assert time_result == (7, 0)


# ── Timer integration tests ─────────────────────────────────────────────


class TestTimerParsing:
    """Test timer duration parsing as used by app_set_timer."""

    def test_timer_natural_language(self):
        """'10 minute pizza timer' → 10 minutes via parse_duration."""
        # Extract the number from the description
        result = _parse_duration("10 minute")
        assert result == {'hours': 0, 'minutes': 10, 'seconds': 0}

    def test_timer_pipe_format(self):
        """'Pizza timer|10|0' — pipe with minutes/seconds."""
        fields = ['desc', 'minutes', 'seconds']
        parsed = _parse_flexible_input("Pizza timer|10|0", fields)
        assert parsed['desc'] == "Pizza timer"
        assert parsed['minutes'] == "10"

    def test_timer_compact_duration(self):
        """'5m30s' → 5 min 30 sec."""
        result = _parse_duration("5m30s")
        assert result['minutes'] == 5
        assert result['seconds'] == 30

    def test_timer_just_number(self):
        """'10' alone → assume 10 minutes."""
        result = _parse_duration("10")
        assert result['minutes'] == 10


# ── FreshTable integration tests ─────────────────────────────────────────


class TestFreshTableParsing:
    """Test pantry/shopping input parsing as used by FreshTable tools."""

    def test_pantry_pipe_format(self):
        """'Milk|dairy|2|gallons|2026-03-15' parses all fields."""
        fields = ['name', 'category', 'quantity', 'unit', 'expiry']
        parsed = _parse_flexible_input(
            "Milk|dairy|2|gallons|2026-03-15", fields)
        assert parsed['name'] == "Milk"
        assert parsed['category'] == "dairy"
        assert parsed['quantity'] == "2"
        assert parsed['unit'] == "gallons"
        assert parsed['expiry'] == "2026-03-15"

    def test_pantry_minimal(self):
        """'Milk' alone → just name, rest None."""
        fields = ['name', 'category', 'quantity', 'unit', 'expiry']
        parsed = _parse_flexible_input("Milk", fields)
        assert parsed['name'] == "Milk"
        assert parsed['category'] is None

    def test_pantry_json(self):
        """JSON format pantry input."""
        inp = json.dumps({"name": "Eggs", "category": "dairy", "quantity": "12"})
        fields = ['name', 'category', 'quantity', 'unit', 'expiry']
        parsed = _parse_flexible_input(inp, fields)
        assert parsed['name'] == "Eggs"
        assert parsed['category'] == "dairy"
        assert parsed['quantity'] == "12"

    def test_pantry_item_info_stripped(self):
        """item_info='Bread|bakery|1|loaf' strips param name."""
        fields = ['name', 'category', 'quantity', 'unit', 'expiry']
        parsed = _parse_flexible_input(
            "item_info='Bread|bakery|1|loaf'", fields)
        assert parsed['name'] == "Bread"
        assert parsed['category'] == "bakery"

    def test_shopping_pipe_format(self):
        """'Chicken breast|meat|2|lbs' parses correctly."""
        fields = ['name', 'category', 'quantity', 'unit']
        parsed = _parse_flexible_input(
            "Chicken breast|meat|2|lbs", fields)
        assert parsed['name'] == "Chicken breast"
        assert parsed['category'] == "meat"
        assert parsed['quantity'] == "2"

    def test_pantry_with_natural_date_expiry(self):
        """Expiry date 'next friday' gets resolved."""
        fields = ['name', 'category', 'quantity', 'unit', 'expiry']
        parsed = _parse_flexible_input(
            "Yogurt|dairy|1|cup|next friday", fields)
        expiry = _parse_natural_date(parsed['expiry'])
        dt = datetime.strptime(expiry, '%Y-%m-%d')
        assert dt.weekday() == 4  # Friday
        assert dt.date() > datetime.now().date()


# ── FitFam integration tests ────────────────────────────────────────────


class TestFitFamParsing:
    """Test workout logging input parsing."""

    def test_workout_pipe_format(self):
        """'Push-ups|calisthenics|3|10|bodyweight' parses fields."""
        fields = ['exercise', 'type', 'sets', 'reps', 'weight']
        parsed = _parse_flexible_input(
            "Push-ups|calisthenics|3|10|bodyweight", fields)
        assert parsed['exercise'] == "Push-ups"
        assert parsed['type'] == "calisthenics"
        assert parsed['sets'] == "3"
        assert parsed['reps'] == "10"

    def test_workout_minimal(self):
        """'Push-ups' alone → just exercise name."""
        fields = ['exercise', 'type', 'sets', 'reps', 'weight']
        parsed = _parse_flexible_input("Push-ups", fields)
        assert parsed['exercise'] == "Push-ups"
        assert parsed['type'] is None

    def test_workout_json(self):
        """JSON workout input."""
        inp = json.dumps({
            "exercise": "Squats",
            "type": "calisthenics",
            "sets": "4",
            "reps": "15"
        })
        fields = ['exercise', 'type', 'sets', 'reps', 'weight']
        parsed = _parse_flexible_input(inp, fields)
        assert parsed['exercise'] == "Squats"
        assert parsed['sets'] == "4"

    def test_workout_info_stripped(self):
        """workout_info='Deadlift|gym|3|8|135' strips param name."""
        fields = ['exercise', 'type', 'sets', 'reps', 'weight']
        parsed = _parse_flexible_input(
            "workout_info='Deadlift|gym|3|8|135'", fields)
        assert parsed['exercise'] == "Deadlift"
        assert parsed['type'] == "gym"
        assert parsed['weight'] == "135"


# ── Trivia integration tests ────────────────────────────────────────────


class TestTriviaParsing:
    """Test trivia input parsing for category selection."""

    def test_trivia_single_category(self):
        """'science' as single value → first field."""
        fields = ['category']
        parsed = _parse_flexible_input("science", fields)
        assert parsed['category'] == "science"

    def test_trivia_random(self):
        """'random' picks any category."""
        fields = ['category']
        parsed = _parse_flexible_input("random", fields)
        assert parsed['category'] == "random"

    def test_trivia_quoted_category(self):
        """\"'history'\" strips quotes."""
        fields = ['category']
        parsed = _parse_flexible_input("'history'", fields)
        assert parsed['category'] == "history"


# ── Cross-app flexible input edge cases ──────────────────────────────────


class TestFlexibleInputEdgeCases:
    """Edge cases for the flexible input parser across all apps."""

    def test_empty_pipe_fields(self):
        """'title||category' → empty string for middle field."""
        fields = ['title', 'due', 'cat']
        parsed = _parse_flexible_input("Buy milk||family", fields)
        assert parsed['title'] == "Buy milk"
        assert parsed['due'] == ""
        assert parsed['cat'] == "family"

    def test_extra_pipe_fields_ignored(self):
        """More pipe segments than fields → extras ignored."""
        fields = ['title', 'due']
        parsed = _parse_flexible_input("Buy|tomorrow|extra|more", fields)
        assert parsed['title'] == "Buy"
        assert parsed['due'] == "tomorrow"
        assert len(parsed) == 2

    def test_whitespace_around_pipes(self):
        """'title | date | cat' strips whitespace from values."""
        fields = ['title', 'due', 'cat']
        parsed = _parse_flexible_input("Buy milk | tomorrow | family", fields)
        assert parsed['title'] == "Buy milk"
        assert parsed['due'] == "tomorrow"
        assert parsed['cat'] == "family"

    def test_key_value_with_single_quotes(self):
        """title='Buy milk', due='tomorrow' works."""
        fields = ['title', 'due']
        parsed = _parse_flexible_input(
            "title='Buy milk', due='tomorrow'", fields)
        assert parsed['title'] == "Buy milk"
        assert parsed['due'] == "tomorrow"

    def test_nested_json_fields(self):
        """JSON with unrecognized fields → mapped to None."""
        inp = json.dumps({"title": "test", "unknown": "val"})
        fields = ['title', 'due']
        parsed = _parse_flexible_input(inp, fields)
        assert parsed['title'] == "test"
        assert parsed['due'] is None


# ── Event time extraction tests ─────────────────────────────────────────


import re


def _extract_event_time(start_str: str):
    """Simulate the event time extraction logic from app_add_event.

    Returns the resolved datetime string with time.
    """
    time_embedded = None
    at_match = re.search(
        r'\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)',
        start_str, re.IGNORECASE)
    if at_match:
        time_str = at_match.group(1)
        time_parsed = _parse_natural_time(time_str)
        if time_parsed:
            time_embedded = time_parsed
        start_str = start_str[:at_match.start()].strip()

    date_resolved = _parse_natural_date(start_str)
    # Add time if date is just YYYY-MM-DD
    if re.match(r'^\d{4}-\d{2}-\d{2}$', date_resolved):
        if time_embedded:
            date_resolved += f'T{time_embedded[0]:02d}:{time_embedded[1]:02d}'
        else:
            date_resolved += 'T09:00'

    return date_resolved


class TestEventTimeExtraction:
    """Verify that embedded time in event start strings is extracted correctly."""

    def test_next_monday_at_2pm(self):
        """'next monday at 2pm' → date with T14:00."""
        result = _extract_event_time("next monday at 2pm")
        assert 'T14:00' in result

    def test_next_friday_at_4pm(self):
        """'next friday at 4pm' → date with T16:00."""
        result = _extract_event_time("next friday at 4pm")
        assert 'T16:00' in result

    def test_tomorrow_at_9_30_am(self):
        """'tomorrow at 9:30 AM' → date with T09:30."""
        result = _extract_event_time("tomorrow at 9:30 AM")
        assert 'T09:30' in result

    def test_tomorrow_at_noon(self):
        """'tomorrow at 12pm' → date with T12:00."""
        result = _extract_event_time("tomorrow at 12pm")
        assert 'T12:00' in result

    def test_march_10_at_3pm(self):
        """'march 10 at 3pm' → 2026-03-10T15:00."""
        result = _extract_event_time("march 10 at 3pm")
        assert result == '2026-03-10T15:00'

    def test_no_time_defaults_to_9am(self):
        """'next monday' without time → T09:00."""
        result = _extract_event_time("next monday")
        assert 'T09:00' in result

    def test_tomorrow_no_time(self):
        """'tomorrow' without time → T09:00."""
        result = _extract_event_time("tomorrow")
        assert 'T09:00' in result

    def test_iso_date_only(self):
        """'2026-03-15' → '2026-03-15T09:00' (default time)."""
        result = _extract_event_time("2026-03-15")
        assert result == '2026-03-15T09:00'

    def test_iso_with_time_passthrough(self):
        """'2026-03-15T14:00' → unchanged (already has time)."""
        result = _extract_event_time("2026-03-15T14:00")
        assert result == '2026-03-15T14:00'

    def test_at_10am(self):
        """'next wednesday at 10am' → T10:00."""
        result = _extract_event_time("next wednesday at 10am")
        assert 'T10:00' in result

    def test_at_7_pm(self):
        """'tomorrow at 7 pm' → T19:00."""
        result = _extract_event_time("tomorrow at 7 pm")
        assert 'T19:00' in result

    def test_at_military_time(self):
        """'next tuesday at 15:30' → T15:30."""
        result = _extract_event_time("next tuesday at 15:30")
        assert 'T15:30' in result

    def test_date_part_preserved(self):
        """Date portion is correctly parsed after time extraction."""
        result = _extract_event_time("march 20 at 2pm")
        assert result.startswith('2026-03-20')
        assert result.endswith('T14:00')

    def test_next_day_at_8_30_am(self):
        """'next thursday at 8:30am' → T08:30."""
        result = _extract_event_time("next thursday at 8:30am")
        assert 'T08:30' in result


class TestEventTimeWithPipeParsing:
    """Test time extraction through the full pipe-parsing pipeline."""

    def test_pipe_with_time_in_start(self):
        """'title|next monday at 3pm|work|all|medium' → start has time."""
        fields = ['title', 'start', 'cat', 'assigned_to', 'prio']
        parsed = _parse_flexible_input(
            "Team Meeting|next monday at 3pm|work|all|medium", fields)
        assert parsed['title'] == "Team Meeting"
        assert parsed['start'] == "next monday at 3pm"

    def test_pipe_with_iso_time(self):
        """'title|2026-03-09T14:00|work|all|medium' → start has ISO time."""
        fields = ['title', 'start', 'cat', 'assigned_to', 'prio']
        parsed = _parse_flexible_input(
            "Team Meeting|2026-03-09T14:00|work|all|medium", fields)
        assert parsed['start'] == "2026-03-09T14:00"

    def test_pipe_date_only(self):
        """'title|next monday|work|all|medium' → start is just date."""
        fields = ['title', 'start', 'cat', 'assigned_to', 'prio']
        parsed = _parse_flexible_input(
            "Team Meeting|next monday|work|all|medium", fields)
        assert parsed['start'] == "next monday"

    def test_end_to_end_time_extraction_2pm(self):
        """Full pipeline: parse + time extraction → T14:00."""
        fields = ['title', 'start', 'cat', 'assigned_to', 'prio']
        parsed = _parse_flexible_input(
            "Dentist|next friday at 2pm|medical|dad|high", fields)
        result = _extract_event_time(parsed['start'])
        assert 'T14:00' in result

    def test_end_to_end_time_extraction_9_30am(self):
        """Full pipeline: parse + time extraction → T09:30."""
        fields = ['title', 'start', 'cat', 'assigned_to', 'prio']
        parsed = _parse_flexible_input(
            "Standup|tomorrow at 9:30am|work|all|medium", fields)
        result = _extract_event_time(parsed['start'])
        assert 'T09:30' in result
