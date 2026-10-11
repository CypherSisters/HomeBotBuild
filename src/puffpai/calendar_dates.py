"""Local-clock date resolution, day scoping, and recurrence expansion.

Why this module exists
----------------------
The natural date a person says has to be parsed, and parsed against the
box's local clock. Without that, the calendar goes wrong in four ways that
share one root:

* "remind me to call the dentist Monday" sends the whole phrase as the TITLE
  and no due date at all, so the task falls back to the "tomorrow" default.
  The date is in the sentence; something has to look for it.
* "what's on the calendar tomorrow" needs a one-day window, not the full
  seven-day one.
* ``recurrence`` is stored on the event and has to be expanded by the
  server, or the chat/voice surface loses a recurring event after its first
  occurrence while the static page (which expands client-side) keeps
  showing it.
* a bare "Saturday" has to reach the calendar, not RAG.

Everything here is pure: it takes a ``now`` and returns dates. The calendar
stores naive local wall-clock time in the box's own timezone, so "today",
"tomorrow" and "Monday" are resolved against the
box's LOCAL date and the datetimes returned are naive local ones — the same
convention the store already uses. No timezone conversion happens anywhere;
introducing one here would silently shift every existing stored event.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

__all__ = [
    "WEEKDAY_NAMES",
    "WEEKDAY_ABBR",
    "CUSTOM_PREFIX",
    "custom_days",
    "normalise_recurrence",
    "recurrence_for_days",
    "days_of_recurrence",
    "resolve_date",
    "extract_date_phrase",
    "day_scope",
    "expanded_occurrences",
    "parse_time_hint",
    "normalize_spoken_clock",
    "spoken_clock_number",
]

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday")

#: The short forms a ``custom:`` recurrence is written with.
WEEKDAY_ABBR = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: A recurrence that carries its own WEEKDAY SET, written as one string so
#: every existing reader of ``rec`` (the store's JSON row, the app PUT merge
#: map, cal_edit_event, the scheduler's fallback) keeps taking a plain string
#: and nothing needs a second field plumbed through it:
#:
#:     rec = "custom:mon,tue,thu,fri"
#:
#: "every Monday, Tuesday, Thursday and Friday at 6:15" is an ordinary thing
#: to say, and the fixed vocabulary (daily / weekdays /
#: weekly / biweekly / monthly / yearly) cannot express it: forcing it into
#: one gives "weekdays" or "weekly", both wrong. A reader that predates this
#: value sees an unknown recurrence and
#: degrades to the single base occurrence rather than to wrong days.
CUSTOM_PREFIX = "custom:"


def custom_days(recurrence) -> "set[int] | None":
    """The weekday numbers in a ``custom:`` recurrence, else None.

    None means "not a weekday-set recurrence" — every other value in the
    vocabulary, and anything unparseable.
    """
    text = str(recurrence or "").strip().lower()
    if not text.startswith(CUSTOM_PREFIX):
        return None
    days = set()
    for token in text[len(CUSTOM_PREFIX):].split(","):
        token = token.strip()
        if token in _WEEKDAY_INDEX:
            days.add(_WEEKDAY_INDEX[token])
    return days or None


def recurrence_for_days(days) -> str:
    """The narrowest recurrence value that expresses *days* (weekday ints).

    Prefers the vocabulary that already existed — Mon–Fri is "weekdays", all
    seven is "daily", one day is "weekly" — so a ``custom:`` value is only
    ever written when nothing simpler can say it.
    """
    wanted = {int(d) % 7 for d in days}
    if not wanted:
        return ""
    if wanted == {0, 1, 2, 3, 4}:
        return "weekdays"
    if wanted == set(range(7)):
        return "daily"
    if len(wanted) == 1:
        return "weekly"
    return CUSTOM_PREFIX + ",".join(
        WEEKDAY_ABBR[i] for i in sorted(wanted))


def normalise_recurrence(value) -> "str | None":
    """Canonical form of a ``custom:`` recurrence, or None if it is not one.

    Used by the store to accept a weekday-set recurrence without widening the
    fixed vocabulary to anything a caller feels like sending.

    The ``custom:`` prefix is KEPT, always. Delegating to
    :func:`recurrence_for_days` here turned ``"custom:tue"`` into ``"weekly"``
    — which reads back as "every <whatever weekday the start happens to fall
    on>", so a stored row whose start drifted (an edit, an import, a
    daylight-saving shift) would silently repeat on the wrong day. A caller
    that WANTS the narrowest legacy word asks :func:`recurrence_for_days` with
    a day set; a caller normalising a value that already carries its own days
    keeps them.
    """
    days = custom_days(value)
    if not days:
        return None
    return CUSTOM_PREFIX + ",".join(WEEKDAY_ABBR[i] for i in sorted(days))


def days_of_recurrence(recurrence, start=None) -> "set[int] | None":
    """The weekdays a stored ``rec`` value repeats on, or None if it has none.

    None means "this recurrence is not a weekday set" — ``biweekly``,
    ``monthly``, ``yearly`` and an unknown value all repeat on a cadence that
    a day set cannot describe, so a caller comparing two rows must not treat
    them as comparable. ``weekly`` needs *start* to know which day it means.
    """
    rec = str(recurrence or "").strip().lower()
    custom = custom_days(rec)
    if custom:
        return custom
    if rec == "weekdays":
        return {0, 1, 2, 3, 4}
    if rec == "daily":
        return set(range(7))
    if rec == "weekly":
        return {start.weekday()} if start is not None else None
    return None

#: Full names plus the abbreviations a person actually says or types.
_WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAY_NAMES)}
_WEEKDAY_INDEX.update({
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
})

_MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10,
    'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}

_WEEKDAY_ALTS = "|".join(sorted(_WEEKDAY_INDEX, key=len, reverse=True))

# ── spoken clock words ──────────────────────────────────────────────────────
#
# People say "today before nine" and "set an alarm for seven". A clock
# pattern that wants digits makes the number words a person actually says
# invisible: the task lands on the tomorrow default and the alarm falls to
# the agent, which guesses. This lives here
# because it is pure text-to-digits, it has no dependencies, and BOTH the
# calendar parsers and the alarm fastpaths in api_server need exactly one
# copy of it.

_CLOCK_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_CLOCK_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,
               "fifty": 50}
_HOUR_ALT = "|".join(sorted((w for w, n in _CLOCK_UNITS.items() if n <= 12),
                            key=len, reverse=True))
_UNIT_ALT = "|".join(sorted((w for w, n in _CLOCK_UNITS.items() if n <= 9),
                            key=len, reverse=True))
_TENS_ALT = "|".join(sorted(_CLOCK_TENS, key=len, reverse=True))
_ALL_UNIT_ALT = "|".join(sorted(_CLOCK_UNITS, key=len, reverse=True))
#: The minute half of "six thirty" / "seven oh five" / "eight forty-five".
_MIN_PHRASE = (rf"(?:(?:{_TENS_ALT})(?:[\s-](?:{_UNIT_ALT}))?"
               rf"|o[h']?\s+(?:{_UNIT_ALT})|{_ALL_UNIT_ALT})")
#: A whole spoken clock time, for embedding in a larger pattern.
SPOKEN_CLOCK_ALT = (rf"(?:(?:an?\s+)?half\s+past\s+|(?:an?\s+)?quarter\s+"
                    rf"(?:past|to|til|till)\s+)?(?:{_HOUR_ALT})"
                    rf"(?:[\s-](?:{_MIN_PHRASE}))?(?:\s+o'?\s?clock)?")
#: Cheap guard: a sentence with no spoken number at all is returned untouched,
#: so every existing digit phrasing keeps byte-identical behaviour.
_CLOCK_WORDS_PRESENT_RE = re.compile(
    rf"(?i)\b(?:{_ALL_UNIT_ALT}|{_TENS_ALT}|half\s+past|quarter)\b")


def spoken_clock_number(phrase: str) -> "int | None":
    """The number named by a one- or two-word spoken phrase, or None. Pure."""
    parts = [p for p in re.split(r"[\s-]+", (phrase or "").strip().lower()) if p]
    if len(parts) == 1:
        word = parts[0]
        if word.isdigit():
            return int(word)
        return _CLOCK_UNITS.get(word, _CLOCK_TENS.get(word))
    if len(parts) == 2:
        first, second = parts
        if first in ("oh", "o", "o'", "zero"):
            return _CLOCK_UNITS.get(second)
        if first in _CLOCK_TENS and _CLOCK_UNITS.get(second, 99) <= 9:
            return _CLOCK_TENS[first] + _CLOCK_UNITS[second]
    return None


def normalize_spoken_clock(text: str) -> str:
    """Rewrite spoken clock times in *text* as digits. Pure — no I/O.

    "seven" -> "7", "six thirty" -> "6:30", "half past seven" -> "7:30",
    "quarter to eight" -> "7:45". Nothing else in the sentence is touched, and
    a sentence with no number words at all is returned as-is.
    """
    low = text or ""
    if not _CLOCK_WORDS_PRESENT_RE.search(low):
        return low

    def _hour(word: str) -> int:
        word = word.strip().lower()
        return int(word) if word.isdigit() else _CLOCK_UNITS[word]

    hour_alt = rf"(?:{_HOUR_ALT}|\d{{1,2}})"
    low = re.sub(rf"(?i)\b(?:an?\s+)?half\s+past\s+({hour_alt})\b",
                 lambda m: f"{_hour(m.group(1))}:30", low)
    low = re.sub(rf"(?i)\b(?:an?\s+)?quarter\s+past\s+({hour_alt})\b",
                 lambda m: f"{_hour(m.group(1))}:15", low)

    def _quarter_to(m):
        hour = _hour(m.group(1)) - 1
        return f"{hour or 12}:45"

    low = re.sub(rf"(?i)\b(?:an?\s+)?quarter\s+(?:to|til|till|before)"
                 rf"\s+({hour_alt})\b", _quarter_to, low)

    def _compound(m):
        minute = spoken_clock_number(m.group(2))
        if minute is None or minute > 59:
            return m.group(0)
        return f"{_hour(m.group(1))}:{minute:02d}"

    low = re.sub(rf"(?i)\b({_HOUR_ALT})[\s-]+({_MIN_PHRASE})\b", _compound, low)
    low = re.sub(rf"(?i)\b({_HOUR_ALT})\s+o'?\s?clock\b",
                 lambda m: str(_hour(m.group(1))), low)
    return re.sub(rf"(?i)\b({_HOUR_ALT})\b",
                  lambda m: str(_hour(m.group(1))), low)


#: The prepositions a time hangs off in speech. Without "before" here,
#: "today BEFORE nine" is not recognised as naming a time at all, so the
#: whole phrase stays in the title and the task falls to the tomorrow default.
_TIME_PREP = r"(?:at|by|before|around|about)"

#: A date phrase sitting at the END of a spoken command ("...call the dentist
#: on Monday at 3pm"). Anchored to the tail so it never eats the task title.
_TRAILING_DATE_RE = re.compile(
    r"(?i)(?:\s+|^)(?:(?:on|by|for|this|next|come)\s+)?"
    r"(?P<phrase>"
    # "this morning"/"this evening" name a day AND a half of it; without them
    # here "do the dishes this morning by nine" reached the bare-time branch
    # and rolled to tomorrow.
    r"this\s+(?:morning|afternoon|evening)|"
    r"today|tonight|tomorrow|the\s+day\s+after\s+tomorrow|"
    r"(?:this\s+|next\s+)?weekend|"
    r"in\s+\d+\s+days?|next\s+week|"
    r"(?:this\s+|next\s+)?(?:" + _WEEKDAY_ALTS + r")|"
    r"(?:" + "|".join(_MONTHS) + r")\s+\d{1,2}(?:st|nd|rd|th)?|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r")"
    r"(?P<time>\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)"
    r"|\s+" + _TIME_PREP + r"\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    r"|\s+" + _TIME_PREP + r"\s+(?:noon|midnight)"
    r"|\s+" + _TIME_PREP + r"\s+(?:" + SPOKEN_CLOCK_ALT + r")"
    r"\s*(?:am|pm)?)?"
    r"\s*[.?!]?$")

#: A time with no day ("...at 8", "...at 8:30pm") at the end of a command.
_BARE_TIME_TAIL_RE = re.compile(
    r"(?i)\s+" + _TIME_PREP + r"\s+(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    r"|noon|midnight|(?:" + SPOKEN_CLOCK_ALT + r")\s*(?:am|pm)?)"
    r"\s*[.?!]?$")

_TIME_RE = re.compile(
    r"(?i)(?:at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"|(?:at\s+)?(?P<named>noon|midnight)")


def _parse_time_hint(text: str) -> "tuple[int, int] | None":
    """(hour, minute) from a trailing time fragment, or None."""
    if not text:
        return None
    m = _TIME_RE.search(normalize_spoken_clock(text.strip()))
    if not m:
        return None
    if m.group("named"):
        return (12, 0) if m.group("named").lower() == "noon" else (0, 0)
    if m.group("h") is None:
        return None
    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return (hour, minute)


#: Public alias: app_data parses a medication time ("8:00 AM") with this.
parse_time_hint = _parse_time_hint


def _weekday_date(target: int, now: datetime, time_hint=None,
                  explicit_next: bool = False) -> date:
    """The date a bare or "next" weekday name means, from ``now``.

    A weekday name always points FORWARD. Saying "Monday" on a Monday means
    later today only when a time is given that has not passed yet; with no
    time (a due date) or a time already gone, it means the Monday a week out
    — a family that says "Monday" at 4pm about a 9am thing means next week.
    "next Monday" always means the Monday of the following calendar week.
    """
    today = now.date()
    if explicit_next:
        next_monday = today + timedelta(days=7 - now.weekday())
        return next_monday + timedelta(days=target)
    diff = (target - now.weekday()) % 7
    if diff == 0:
        if time_hint is not None and time_hint > (now.hour, now.minute):
            return today
        return today + timedelta(days=7)
    return today + timedelta(days=diff)


def resolve_date(text: str, now: "datetime | None" = None,
                 time_hint=None) -> "date | None":
    """The local date a natural phrase names, or None if it names none.

    ``time_hint`` is an optional ``(hour, minute)`` already extracted from
    the same utterance; it only ever matters for "today"-vs-"a week out" on
    a weekday name.
    """
    if not text:
        return None
    now = now or datetime.now()
    raw = text.strip().strip("'\"").lower()
    raw = re.sub(r"^(?:on|by|for|come)\s+", "", raw).strip()
    raw = raw.rstrip(".?!,").strip()
    if not raw:
        return None

    if raw in ("today", "tonight", "this morning", "this afternoon",
               "this evening"):
        return now.date()
    if raw == "tomorrow":
        return now.date() + timedelta(days=1)
    if raw == "yesterday":
        return now.date() - timedelta(days=1)
    if raw in ("day after tomorrow", "the day after tomorrow"):
        return now.date() + timedelta(days=2)

    m = re.fullmatch(r"in (\d{1,3}) days?", raw)
    if m:
        return now.date() + timedelta(days=int(m.group(1)))
    if raw == "next week":
        return now.date() + timedelta(weeks=1)

    # "this weekend" / "the weekend" / "next weekend" → that Saturday.
    m = re.fullmatch(r"(?:(this|next|the)\s+)?weekend", raw)
    if m:
        if m.group(1) != "next" and now.weekday() >= 5:
            return now.date()   # said ON the weekend, it means this one
        return _weekday_date(5, now, explicit_next=(m.group(1) == "next"))

    m = re.fullmatch(r"(?:(this|next|coming)\s+)?(" + _WEEKDAY_ALTS + r")", raw)
    if m:
        return _weekday_date(_WEEKDAY_INDEX[m.group(2)], now,
                             time_hint=time_hint,
                             explicit_next=(m.group(1) == "next"))

    m = re.fullmatch(r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?",
                     raw)
    if m and m.group(1) in _MONTHS:
        month, day = _MONTHS[m.group(1)], int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            found = date(year, month, day)
        except ValueError:
            return None
        if m.group(3) is None and found < now.date():
            try:
                found = date(year + 1, month, day)
            except ValueError:
                return None
        return found

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


#: A half of the day the person actually said. Without one, an hour said
#: about TODAY is read as the nearer plausible one (see _evening_reading).
_EXPLICIT_HALF_RE = re.compile(
    r"(?i)(?<![a-z])(?:am|pm|a\.m|p\.m)\b|\b(?:noon|midnight)\b")
_TODAY_EVENING_PHRASES = ("tonight", "this evening", "this afternoon")
#: The half of the day the words themselves name. "this morning by nine" is
#: 9 AM at any hour of the clock — including hours 1-5, where the bare-hour
#: rule below would otherwise reach for the afternoon.
_TODAY_MORNING_PHRASES = ("this morning", "morning")

#: How much of the morning has to be LEFT before an unqualified hour is worth
#: asking about. Said at 00:55, "before nine" has two live readings eight
#: hours apart and guessing either one is a coin toss. Said at 08:10 it has
#: one reading anybody means: the one fifty minutes away.
BARE_HOUR_ASK_MARGIN = timedelta(hours=2)


def bare_hour_deadline(hint, time_text, day_phrase, now=None):
    """``((hour, minute), ask)`` for an unqualified hour said about TODAY.

    "add a reminder to do the dishes today before nine" can be read as
    **9 AM**, or, said at 2:51 PM, rolled to tomorrow. Both are the same
    defect (an hour with no half of the day named, resolved silently), and
    the honest answers differ by the clock:

    * the words name a half ("tonight", "this morning") → use it;
    * the morning slot is already behind us → the evening is the only reading
      of the day she actually said that is still ahead;
    * the morning slot is still ahead by more than
      :data:`BARE_HOUR_ASK_MARGIN` → both readings are live, so ASK (the
      caller registers the AM/PM clarifier) rather than pick;
    * the morning slot is minutes away → that is the one she means.

    Never rolls to tomorrow, never touches a time that carried its own am/pm,
    an hour of 12 or 0, or a day other than today. Returns the hint unchanged
    with ``ask`` False whenever none of the rules apply.
    """
    now = now or datetime.now()
    if hint is None or _EXPLICIT_HALF_RE.search(time_text or ""):
        return hint, False
    hour, minute = hint
    if not (1 <= hour <= 11):
        return hint, False
    phrase = re.sub(r"^(?:on|by|for|come)\s+", "",
                    (day_phrase or "").strip().lower()).strip()
    if phrase in _TODAY_MORNING_PHRASES:
        return hint, False
    if phrase in _TODAY_EVENING_PHRASES:
        return (hour + 12, minute), False
    if phrase != "today":
        return hint, False
    if (hour, minute) <= (now.hour, now.minute):
        return (hour + 12, minute), False
    morning = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if morning - now >= BARE_HOUR_ASK_MARGIN:
        return hint, True
    return hint, False


def _evening_reading(hint, time_text, day_phrase, now):
    """Back-compatible wrapper: the resolved hint only, never the question."""
    return bare_hour_deadline(hint, time_text, day_phrase, now)[0]


def extract_date_phrase(text: str, now: "datetime | None" = None) -> dict:
    """Split "call the dentist Monday at 3pm" into title + resolved date.

    Without this the reminder fastpath passes the whole sentence through as
    a title, so a date the person said out loud is never stored. Returns
    ``{"title": str, "date": "YYYY-MM-DD"|None, "time": (h, m)|None,
    "phrase": str|None}``. When no date phrase is found the title comes back
    unchanged and ``date`` is None, so the caller's old behaviour stands.
    """
    now = now or datetime.now()
    title = (text or "").strip()
    m = _TRAILING_DATE_RE.search(title)
    if not m:
        # No day named, but a time might be ("remind me to take my
        # medication at 8"). A time alone means today if it has not passed,
        # otherwise tomorrow — the same thing an alarm means by it.
        t = _BARE_TIME_TAIL_RE.search(title)
        if t:
            hint = _parse_time_hint(t.group("time"))
            if hint is not None:
                day = now.date()
                if hint <= (now.hour, now.minute):
                    day = day + timedelta(days=1)
                remainder = title[:t.start()].strip(" ,.-") or title
                return {"title": remainder,
                        "date": day.strftime("%Y-%m-%d"),
                        "time": hint, "phrase": t.group("time").strip(),
                        "ask_half": False,
                        "iso": "%s T%02d:%02d".replace(" ", "") % (
                            day.strftime("%Y-%m-%d"), hint[0], hint[1])}
        return {"title": title, "date": None, "time": None, "phrase": None,
                "ask_half": False, "iso": None}
    time_hint, ask_half = bare_hour_deadline(
        _parse_time_hint(m.group("time") or ""),
        m.group("time") or "", m.group("phrase"), now)
    resolved = resolve_date(m.group("phrase"), now=now, time_hint=time_hint)
    if resolved is None:
        return {"title": title, "date": None, "time": None, "phrase": None,
                "ask_half": False, "iso": None}
    remainder = title[:m.start()].strip(" ,.-")
    if len(remainder) < 2:
        # The whole utterance WAS the date ("Monday"). Keep the title.
        remainder = title
    iso = resolved.strftime("%Y-%m-%d")
    if time_hint is not None:
        iso = "%sT%02d:%02d" % (iso, time_hint[0], time_hint[1])
    return {
        "title": remainder,
        "date": resolved.strftime("%Y-%m-%d"),
        "time": time_hint,
        "phrase": m.group("phrase").strip(),
        # True when the hour named could be either half of the day and the
        # sentence settled neither: the caller ASKS (api_server._try_add_task
        # registers the AM/PM clarifier) instead of writing this reading.
        "ask_half": ask_half,
        # Date, or date+time when the person said one — a reminder due at
        # 3pm must not silently become the 09:00 default.
        "iso": iso,
    }


# ── scoping a READ to the day the question names ─────────────────────────────

_SCOPE_TODAY_RE = re.compile(
    r"(?i)\b(?:today|tonight|this\s+(?:morning|afternoon|evening))\b")
_SCOPE_TOMORROW_RE = re.compile(r"(?i)\btomorrow\b")
_SCOPE_WEEKEND_RE = re.compile(r"(?i)\b(?:this\s+|the\s+|over\s+the\s+)?weekend\b")
_SCOPE_WEEK_RE = re.compile(r"(?i)\bthis\s+week\b|\brest\s+of\s+the\s+week\b")
_SCOPE_NEXT_WEEK_RE = re.compile(r"(?i)\bnext\s+week\b")
_SCOPE_WEEKDAY_RE = re.compile(
    r"(?i)\b(?:on\s+|this\s+|next\s+|coming\s+)?(" + _WEEKDAY_ALTS + r")\b")


def day_scope(text: str, now: "datetime | None" = None) -> "dict | None":
    """The window a calendar question names, or None for "coming up".

    Returns ``{"start": datetime, "end": datetime, "label": str,
    "days": int}``. ``label`` is what the reply should call the window
    ("tomorrow", "Saturday") so an empty answer can say "nothing on
    Saturday" instead of a bare "no events".
    """
    if not text:
        return None
    now = now or datetime.now()
    today = now.date()

    def _window(first: date, last: date, label: str) -> dict:
        return {
            "start": datetime.combine(first, datetime.min.time()),
            "end": datetime.combine(last, datetime.max.time()).replace(
                microsecond=0),
            "label": label,
            "days": (last - first).days + 1,
        }

    if _SCOPE_NEXT_WEEK_RE.search(text):
        monday = today + timedelta(days=7 - now.weekday())
        return _window(monday, monday + timedelta(days=6), "next week")
    if _SCOPE_TOMORROW_RE.search(text):
        tomorrow = today + timedelta(days=1)
        return _window(tomorrow, tomorrow, "tomorrow")
    if _SCOPE_TODAY_RE.search(text):
        return _window(today, today, "today")
    if _SCOPE_WEEKEND_RE.search(text):
        saturday = today + timedelta(days=(5 - now.weekday()) % 7)
        return _window(saturday, saturday + timedelta(days=1), "this weekend")
    if _SCOPE_WEEK_RE.search(text):
        sunday = today + timedelta(days=6 - now.weekday())
        return _window(today, sunday, "this week")
    m = _SCOPE_WEEKDAY_RE.search(text)
    if m:
        target = _WEEKDAY_INDEX[m.group(1).lower()]
        explicit_next = bool(re.search(r"(?i)\bnext\s+$", text[:m.start(1)]))
        if target == now.weekday() and not explicit_next:
            # A bare "Saturday" said ON Saturday asks about today.
            when = today
        else:
            when = _weekday_date(target, now, explicit_next=explicit_next)
        return _window(when, when, WEEKDAY_NAMES[target].capitalize())
    return None


# ── recurrence expansion ─────────────────────────────────────────────────────

_MAX_OCCURRENCES = 400


def _event_start(event: dict) -> "datetime | None":
    raw = event.get("start")
    if not isinstance(raw, str) or not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # The store's convention is naive local wall-clock; an
        # offset-carrying row is normalised to that same wall clock.
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _add_months(stamp: datetime, months: int) -> "datetime | None":
    month = stamp.month - 1 + months
    year = stamp.year + month // 12
    month = month % 12 + 1
    try:
        return stamp.replace(year=year, month=month)
    except ValueError:
        return None  # e.g. the 31st in a 30-day month — skip that month


def expanded_occurrences(events, start: datetime, end: datetime) -> list:
    """Every occurrence falling in [start, end], recurrence expanded.

    Returns ``[(occurrence_datetime, event_dict), ...]`` sorted by time. The
    event dict is the STORED row, unmodified — callers read ``title``/``cat``
    off it and must not mutate it. Non-recurring events pass through, so this
    is a superset of a plain ``cal_list_events`` read.

    Supported ``rec`` values match what ``cal_add_event`` accepts and what
    the static page expands client-side: daily, weekdays (Mon-Fri), weekly,
    biweekly, monthly, yearly, plus ``custom:<days>`` (see CUSTOM_PREFIX),
    which steps a day at a time and keeps only the weekdays it names.
    """
    out = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        base = _event_start(event)
        if base is None:
            continue
        rec = (event.get("rec") or "").strip().lower()
        if not rec:
            if start <= base <= end:
                out.append((base, event))
            continue
        if base > end:
            continue
        wanted = custom_days(rec)
        cursor = base
        guard = 0
        while cursor <= end and guard < _MAX_OCCURRENCES:
            guard += 1
            if (cursor >= start
                    and (rec != "weekdays" or cursor.weekday() < 5)
                    and (wanted is None or cursor.weekday() in wanted)):
                out.append((cursor, event))
            if wanted is not None:
                cursor = cursor + timedelta(days=1)
            elif rec == "daily" or rec == "weekdays":
                cursor = cursor + timedelta(days=1)
            elif rec == "weekly":
                cursor = cursor + timedelta(weeks=1)
            elif rec == "biweekly":
                cursor = cursor + timedelta(weeks=2)
            elif rec == "monthly":
                nxt = _add_months(cursor, 1)
                if nxt is None:
                    nxt = _add_months(cursor.replace(day=1), 2)
                    if nxt is None:
                        break
                cursor = nxt
            elif rec == "yearly":
                try:
                    cursor = cursor.replace(year=cursor.year + 1)
                except ValueError:  # Feb 29
                    cursor = cursor.replace(year=cursor.year + 1, day=28)
            else:
                break
    out.sort(key=lambda pair: pair[0])
    return out
