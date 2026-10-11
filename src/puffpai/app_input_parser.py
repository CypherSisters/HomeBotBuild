"""
PuffPAI App Input Parser

Smart input parsing and natural date resolution for LLM-to-tool
communication. Makes app tools resilient to varied input formats
from small language models like dolphin-llama3:8b.

The LLM might output tool input in many formats:
  - Pipe-delimited: 'title|date|category'
  - JSON: '{"title":"x","due":"y"}'
  - Key=value: 'title=x, due=y'
  - Natural language: 'pickup sudafed tomorrow'

These parsers normalize all formats into structured dicts.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta


def parse_natural_date(text: str) -> str:
    """Convert natural language dates to ISO format.

    Handles: 'today', 'tomorrow', 'next monday', 'march 6',
    'march 6th', '3/6/2026', or pass-through ISO dates.

    Returns ISO date string like '2026-03-06'.
    """
    original = text.strip().strip("'\"")
    now = datetime.now()

    # Already ISO format — preserve original case (T in datetime)
    if re.match(r'^\d{4}-\d{2}-\d{2}', original):
        return original

    text = original.lower()

    # One clock, one set of rules: puffpai.calendar_dates owns what "Monday",
    # "tomorrow", "this weekend" and "next Friday" mean relative to the box's
    # LOCAL date. The month/day and slash formats below stay here.
    try:
        from puffpai.calendar_dates import resolve_date as _resolve_date
        resolved = _resolve_date(original, now=now)
    except Exception:
        resolved = None
    if resolved is not None:
        return resolved.strftime('%Y-%m-%d')

    # Relative words
    if text == 'today':
        return now.strftime('%Y-%m-%d')
    if text == 'tomorrow':
        return (now + timedelta(days=1)).strftime('%Y-%m-%d')
    if text == 'yesterday':
        return (now - timedelta(days=1)).strftime('%Y-%m-%d')

    # "next week" / "in X days"
    m = re.match(r'in (\d+) days?', text)
    if m:
        return (now + timedelta(days=int(m.group(1)))).strftime('%Y-%m-%d')
    if text == 'next week':
        return (now + timedelta(weeks=1)).strftime('%Y-%m-%d')

    # Day of week: "monday", "next tuesday", "this friday"
    day_names = ['monday', 'tuesday', 'wednesday', 'thursday',
                 'friday', 'saturday', 'sunday']
    cleaned = text.replace('next ', '').replace('this ', '')
    if cleaned in day_names:
        target_day = day_names.index(cleaned)
        current_day = now.weekday()
        diff = (target_day - current_day) % 7
        if diff == 0:
            diff = 7  # next occurrence
        return (now + timedelta(days=diff)).strftime('%Y-%m-%d')

    # Month + day: "march 6", "march 6th", "mar 6", "december 25th"
    month_map = {
        'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
        'mar': 3, 'march': 3, 'apr': 4, 'april': 4,
        'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
        'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
        'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
        'dec': 12, 'december': 12,
    }
    m = re.match(r'(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?$', text)
    if m and m.group(1) in month_map:
        month = month_map[m.group(1)]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            dt = datetime(year, month, day)
            if dt.date() < now.date():
                dt = datetime(year + 1, month, day)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass

    # US date format: 3/6/2026 or 3/6
    m = re.match(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime('%Y-%m-%d')
        except ValueError:
            pass

    # Fallback: return as-is (AppData will handle or error)
    return text


def parse_natural_time(text: str) -> tuple[int, int] | None:
    """Convert natural language time to (hour_24, minute) tuple.

    Handles: '7:30 AM', '3pm', '15:00', '7 30', 'noon', 'midnight'.

    Returns (hour, minute) in 24-hour format, or None if unparseable.
    """
    text = text.strip().lower().strip("'\"")

    # Named times
    if text in ('noon', '12 noon', '12:00 noon'):
        return (12, 0)
    if text in ('midnight', '12 midnight', '12:00 midnight'):
        return (0, 0)

    # "HH:MM AM/PM" or "H:MM AM/PM"
    m = re.match(r'(\d{1,2}):(\d{2})\s*(am|pm)?$', text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        ampm = m.group(3)
        if ampm == 'pm' and hour != 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
        return (hour % 24, minute)

    # "Hpm" or "H am" (no minutes)
    m = re.match(r'(\d{1,2})\s*(am|pm)$', text)
    if m:
        hour = int(m.group(1))
        ampm = m.group(2)
        if ampm == 'pm' and hour != 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0
        return (hour % 24, 0)

    # 24-hour format "15:00" or "0800"
    m = re.match(r'(\d{1,2}):(\d{2})$', text)
    if m:
        return (int(m.group(1)) % 24, int(m.group(2)))

    m = re.match(r'(\d{2})(\d{2})$', text)
    if m and int(m.group(1)) < 24:
        return (int(m.group(1)), int(m.group(2)))

    # Just a number (hour only): "7" = 7:00 AM, "15" = 3:00 PM
    m = re.match(r'^(\d{1,2})$', text)
    if m:
        hour = int(m.group(1))
        if hour < 24:
            return (hour, 0)

    return None


def parse_duration(text: str) -> dict | None:
    """Parse a duration string into hours, minutes, seconds.

    Handles: '5 minutes', '1 hour 30 minutes', '90 seconds',
    '1h30m', '5m', '10', etc.

    Returns dict with 'hours', 'minutes', 'seconds' keys, or None.
    """
    text = text.strip().lower().strip("'\"")

    # Compact format first: "1h30m", "5m30s", "1h30m15s"
    # (must be checked before word-based to avoid partial matches)
    compact = re.match(r'(\d+)h(?:(\d+)m)?(?:(\d+)s)?$', text)
    if compact:
        return {
            'hours': int(compact.group(1)),
            'minutes': int(compact.group(2) or 0),
            'seconds': int(compact.group(3) or 0),
        }

    compact = re.match(r'(\d+)m(?:(\d+)s)?$', text)
    if compact:
        return {
            'hours': 0,
            'minutes': int(compact.group(1)),
            'seconds': int(compact.group(2) or 0),
        }

    # Word-based: "1 hour 30 minutes", "2 hours and 15 minutes",
    # "5 minutes", "90 seconds", "2 hr 10 min"
    # Use full word patterns (not single letters) to avoid false matches
    hours = 0
    minutes = 0
    seconds = 0

    h = re.search(r'(\d+)\s*(?:hours?|hrs?)', text)
    m = re.search(r'(\d+)\s*(?:minutes?|mins?)', text)
    s = re.search(r'(\d+)\s*(?:seconds?|secs?)', text)

    if h:
        hours = int(h.group(1))
    if m:
        minutes = int(m.group(1))
    if s:
        seconds = int(s.group(1))

    if hours or minutes or seconds:
        return {'hours': hours, 'minutes': minutes, 'seconds': seconds}

    # Plain number — assume minutes
    plain = re.match(r'^(\d+)$', text)
    if plain:
        return {'hours': 0, 'minutes': int(plain.group(1)), 'seconds': 0}

    return None


def parse_flexible_input(raw: str, field_names: list[str]) -> dict:
    """Parse tool input that may be pipe-delimited, JSON, or key=value.

    Handles all these formats from the LLM:
      - 'title|date|category'           (pipe-delimited)
      - '{"title":"x","due":"y"}'       (JSON)
      - 'title=x, due=y'               (key=value)
      - 'title=x due=y'                (key=value no comma)
      - just 'some title text'          (single value → first field)

    Returns dict mapping field names to values (or None if missing).
    """
    raw = raw.strip()
    # Only strip surrounding quotes if the ENTIRE string is wrapped
    if (len(raw) >= 2 and
            ((raw[0] == '"' and raw[-1] == '"') or
             (raw[0] == "'" and raw[-1] == "'"))):
        raw = raw[1:-1]

    # Handle key=value extraction from LLM outputs like:
    # task_info='...' or event_info='...'
    if "=" in raw and raw.split("=")[0].strip().replace("_", "").isalpha():
        first_key = raw.split("=")[0].strip()
        # Check if this is a parameter name assignment (e.g. task_info='...')
        if first_key in ('task_info', 'event_info', 'timer_info',
                         'alarm_info', 'bill_info', 'item_info',
                         'workout_info'):
            raw = raw.split("=", 1)[1].strip().strip("'\"")

    # Try JSON
    if raw.startswith('{'):
        try:
            parsed = json.loads(raw)
            result = {}
            for fn in field_names:
                result[fn] = parsed.get(fn, None)
            return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Try pipe-delimited (most common expected format)
    if '|' in raw:
        parts = [p.strip() for p in raw.split('|')]
        result = {}
        for i, fn in enumerate(field_names):
            result[fn] = parts[i] if i < len(parts) else None
        return result

    # Try key=value (e.g., title="foo", due="tomorrow" or title=foo, due=tomorrow)
    kv_pattern = re.findall(
        r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^,\s"\']+))',
        raw)
    if kv_pattern:
        result = {fn: None for fn in field_names}
        for key, v1, v2, v3 in kv_pattern:
            val = v1 or v2 or v3
            val = val.strip().strip("'\"") if val else val
            if key in result:
                result[key] = val
            else:
                # Try fuzzy match (e.g., 'due_date' → 'due')
                for fn in field_names:
                    if fn in key or key in fn:
                        result[fn] = val
                        break
        if any(v is not None for v in result.values()):
            return result

    # Try comma-separated (title, date, category)
    if ',' in raw:
        parts = [p.strip() for p in raw.split(',')]
        if len(parts) >= 2:
            result = {}
            for i, fn in enumerate(field_names):
                result[fn] = parts[i] if i < len(parts) else None
            return result

    # Single value → assign to first field
    result = {fn: None for fn in field_names}
    result[field_names[0]] = raw
    return result
