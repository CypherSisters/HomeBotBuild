"""Natural-language RECURRING calendar requests, parsed deterministically.

Why this module exists
----------------------
Recurring requests arrive in conversational shapes the generic routes
mishandle. Three turns of one example conversation:

1. "Every Monday, Tuesday, Thursday, and Friday morning at 6.15, I have to
   take the kids to soccer practice. Can you add that?" describes the thing
   first and asks second, which no other fastpath recognises, so it would
   fall to RAG and be refused.
2. "Yes, set up a reminder every weekday morning except for Wednesdays at 615
   take the kids to soccer practice." would reach ``_try_add_task``, which
   stores the WHOLE SENTENCE as a task title, due tomorrow at 09:00, with
   ``rec: null``: no 06:15, no recurrence, and a task rather than a repeating
   calendar item.
3. "Add it to the calendar view." must not create more events with other
   recurrences, and its reply must describe what THIS turn did rather than
   repeat turn 2's confirmation.

Everything here is deterministic: regexes over the words people actually
say, no model. It is registered as a ``_try_*`` fastpath ahead of
``_try_add_task`` (``api_server._try_calendar_recurring``) — the way this
codebase fixes a misroute — and ``_detect_route`` is not touched.

Three rules hold the behaviour together:

* **One item.** A day set plus a time plus an activity is ONE calendar event
  with a recurrence, never a task per day and never a sentence as a title.
* **Truthful confirmation.** The reply reads back the day set, the time and
  the next occurrence, and it says what THIS turn did — added, updated, or
  nothing because it was already there.
* **Idempotent.** Before writing, the store is asked whether an equivalent
  item exists (``AppData.cal_find_event``). A follow-up that names the same
  thing updates or declines; it never adds a second copy.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta

from puffpai import calendar_dates as _caldates

__all__ = [
    "parse_recurring_request",
    "handle",
    "handle_delete",
    "describe",
    "remember",
    "last_item",
    "forget",
    "forget_pending_delete",
    "note_turn",
]

# ── vocabulary ──────────────────────────────────────────────────────────────

_DAY_TOKENS = {
    "monday": 0, "mondays": 0, "mon": 0, "mons": 0,
    "tuesday": 1, "tuesdays": 1, "tue": 1, "tues": 1, "tuesdays": 1,
    "wednesday": 2, "wednesdays": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thursdays": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fridays": 4, "fri": 4, "fris": 4,
    "saturday": 5, "saturdays": 5, "sat": 5, "sats": 5,
    "sunday": 6, "sundays": 6, "sun": 6, "suns": 6,
}

_WEEKDAY_SET = frozenset({0, 1, 2, 3, 4})
_WEEKEND_SET = frozenset({5, 6})
_EVERY_DAY_SET = frozenset(range(7))

#: Phrases that name a GROUP of days rather than one day.
_GROUP_TOKENS = {
    "weekday": _WEEKDAY_SET, "weekdays": _WEEKDAY_SET,
    "week day": _WEEKDAY_SET, "week days": _WEEKDAY_SET,
    "school day": _WEEKDAY_SET, "school days": _WEEKDAY_SET,
    "schoolday": _WEEKDAY_SET, "schooldays": _WEEKDAY_SET,
    "school morning": _WEEKDAY_SET, "school mornings": _WEEKDAY_SET,
    "weekend": _WEEKEND_SET, "weekends": _WEEKEND_SET,
    "day": _EVERY_DAY_SET, "days": _EVERY_DAY_SET,
    "morning": _EVERY_DAY_SET, "mornings": _EVERY_DAY_SET,
    "afternoon": _EVERY_DAY_SET, "afternoons": _EVERY_DAY_SET,
    "evening": _EVERY_DAY_SET, "evenings": _EVERY_DAY_SET,
    "night": _EVERY_DAY_SET, "nights": _EVERY_DAY_SET,
}

#: Plural day words are recurring on their own ("Tuesdays and Thursdays at
#: 6"). A SINGULAR day is not — "on Monday at 3" is one appointment, and
#: stealing it here would turn every single-day errand into a weekly one.
#: "days" and the bare dayparts are NOT recurring on their own: "add a
#: reminder in 3 days at 5 to call mom" says "days" and means one day.
_BARE_PLURAL_VETO = ("days", "mornings", "afternoons", "evenings", "nights")
_PLURAL_TOKENS = {name: days for name, days in _GROUP_TOKENS.items()
                  if name.endswith("s") and name not in _BARE_PLURAL_VETO}
_PLURAL_TOKENS.update({name: {index} for name, index in _DAY_TOKENS.items()
                       if name.endswith("s") and name not in ("thurs", "tues",
                                                              "weds")})


def _alts(names) -> str:
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


_ITEM = r"(?:" + _alts(list(_GROUP_TOKENS) + list(_DAY_TOKENS)) + r")"
_PLURAL_ITEM = r"(?:" + _alts(_PLURAL_TOKENS) + r")"
#: ", " / " and " / ", and " / " & " / " / " — how a spoken day list is joined.
_SEP = r"(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s*[&/+]\s*)"


def _list_pattern(item: str) -> str:
    return r"(?:" + item + r")(?:" + _SEP + r"(?:" + item + r"))*"


_EXCEPT = (r"(?P<exc>\s*,?\s*(?:except|but\s+not|other\s+than|apart\s+from|"
           r"excluding)\s+(?:for\s+)?(?P<exbody>" + _list_pattern(_ITEM)
           + r"))?")

#: The recurrence phrase: "every <day list>" / "each <day list>", or a bare
#: plural day list. An optional daypart ("morning") and an optional
#: "except ..." tail belong to the SAME span so the title never keeps them.
_RECUR_RE = re.compile(
    r"(?i)(?:\b(?:every|each)\s+(?P<body>" + _list_pattern(_ITEM) + r")"
    r"|\b(?:on\s+)?(?P<pbody>" + _list_pattern(_PLURAL_ITEM) + r"))"
    r"(?P<part>\s+(?:in\s+the\s+)?(?:morning|mornings|afternoons?|"
    r"evenings?|nights?))?"
    + _EXCEPT)

_ITEM_RE = re.compile(r"(?i)\b" + _ITEM + r"\b")

#: "at 6:15", "at 6.15", "at 615", "at 6", "6:15 am". A bare 3-4 digit form is
#: what the transcriber writes for a spoken "six fifteen".
_TIME_RE = re.compile(
    r"(?i)\b(?:at|@|around|by)\s+"
    r"(?P<t1>\d{1,2}\s*[:.]\s*\d{2}|\d{3,4}|\d{1,2})"
    r"\s*(?P<ap1>a\.?m\.?|p\.?m\.?|o'?clock)?"
    r"|\b(?P<t2>\d{1,2}\s*[:.]\s*\d{2}|\d{1,2})\s*(?P<ap2>a\.?m\.?|p\.?m\.?)\b")

_MORNING_RE = re.compile(r"(?i)\b(?:morning|mornings|a\.?m\.?|breakfast|"
                         r"before\s+school|sunrise)\b")
_EVENING_RE = re.compile(r"(?i)\b(?:afternoon|afternoons|evening|evenings|"
                         r"night|nights|tonight|p\.?m\.?|after\s+school|"
                         r"dinner)\b")

#: An explicit request to put something on the calendar. Description alone is
#: never enough — the household talks about its week without meaning "write
#: this down", and a fastpath that wrote on every descriptive sentence would
#: be worse than the bug it fixes.
_ADD_INTENT_RE = re.compile(
    r"(?i)\b(?:add|adding|put|schedule|create|book|set\s+up|stick|"
    r"remind\s+me|reminder|reminders|calendar|calender)\b")

#: Reads, questions and the other apps' words. Any of these vetoes the write.
_VETO_RE = re.compile(
    r"(?i)\b(?:what(?:'s| is| are)?|when(?:'s| is)?|where|who|why|how\s+many|"
    r"show\s+me|list|tell\s+me|do\s+i\s+have|is\s+there|are\s+there|"
    r"delete|remove|cancel|clear|move|reschedule|"
    r"alarm|alarms|timer|timers|"
    r"shopping|grocer(?:y|ies)|pantry|"
    r"pill|pills|medication|medications|meds|medicine|dose|doses|insulin|"
    r"prescription)\b")

#: "Add it to the calendar", "put that on the calendar too" — a follow-up that
#: names NOTHING new. It refers to the item the previous turn created.
_FOLLOWUP_ADD_RE = re.compile(
    r"(?i)^\s*(?:yes|yeah|yep|ok|okay|sure|and|also|please)?[,\s]*"
    r"(?:can|could|would|will)?\s*(?:you)?\s*(?:please\s+)?"
    r"(?:add|put|stick|schedule|save)\s+(?:that|it|this|those|them)\b"
    r"(?:\s+(?:one|thing|event))?"
    r"(?:\s+(?:to|on|in|into))?\s*(?:my|the)?\s*"
    r"(?:calendar|calender|schedule)?(?:\s+(?:view|app|page))?\s*"
    r"(?:too|also|as\s+well|please)?\s*[.!?]*\s*$")

#: Words that POINT AT the thing the previous turn created. Without one of
#: these the utterance names a NEW item, however similar it sounds:
#: "add a reminder every Saturday at 9 am to take the kids to soccer
#: practice" would overwrite an existing Mon, Tue, Thu, Fri 06:15 row on a
#: title match alone, and an activity-less "add a reminder every Tuesday at 6" would
#: repurpose it. Both are silent data loss.
_REFERENTIAL_RE = re.compile(
    r"(?i)(?:\b(?:that|it|this|those|them)\b"
    r"|\bthe\s+one\s+(?:i|you|we)\s+just\b"
    r"|\bthe\s+one\s+(?:i|you|we)\s+(?:added|created|made|set\s+up)\b"
    r"|\b(?:that|the)\s+(?:existing|same)\s+one\b)")

#: "make that weekly", "change it to every day" — a recurrence change on the
#: item just created, with no new title and no new time.
_CHANGE_REC_RE = re.compile(
    r"(?i)^\s*(?:and\s+|also\s+|please\s+)?"
    r"(?:make|change|set|switch)\s+(?:that|it|this)\s+(?:to\s+)?"
    r"(?:be\s+|repeat\s+|recur\s+)?"
    r"(?P<rec>daily|every\s+day|weekly|every\s+week|biweekly|"
    r"every\s+other\s+week|monthly|every\s+month|yearly|every\s+year|"
    r"weekdays|every\s+weekday)\b\s*[.!?]*\s*$")

_CHANGE_REC_VALUES = {
    "daily": "daily", "every day": "daily",
    "weekly": "weekly", "every week": "weekly",
    "biweekly": "biweekly", "every other week": "biweekly",
    "monthly": "monthly", "every month": "monthly",
    "yearly": "yearly", "every year": "yearly",
    "weekdays": "weekdays", "every weekday": "weekdays",
}

# ── title cleanup ───────────────────────────────────────────────────────────

_TAIL_RES = (
    re.compile(r"(?i)[.,;]?\s*(?:and\s+)?(?:can|could|would|will)\s+you\s+"
               r"(?:please\s+)?(?:add|put|schedule|set|save)\s+"
               r"(?:that|it|this|them)?\s*(?:to|on|in|into)?\s*"
               r"(?:my|the)?\s*(?:calendar|calender|schedule|list)?"
               r"(?:\s+(?:view|app|page))?\s*[.?!]*\s*$"),
    re.compile(r"(?i)[.,;]?\s*(?:please\s+)?(?:add|put|schedule|save)\s+"
               r"(?:that|it|this|them)\s*(?:to|on|in|into)?\s*"
               r"(?:my|the)?\s*(?:calendar|calender|schedule)?"
               r"(?:\s+(?:view|app|page))?\s*(?:too|also|as\s+well)?"
               r"\s*[.?!]*\s*$"),
    re.compile(r"(?i)[.,;]?\s*(?:to|on|in)\s+(?:my|the)\s+"
               r"(?:calendar|calender|schedule|to-?do\s+list|list)"
               r"(?:\s+(?:instead|too|as\s+well|please))?"
               r"\s*[.?!]*\s*$"),
    re.compile(r"(?i)[.,;]?\s*(?:please|thanks|thank\s+you|ok|okay|instead|"
               r"too|as\s+well)\s*[.?!]*\s*$"),
)

_LEAD_RES = (
    re.compile(r"(?i)^\s*(?:yes|yeah|yep|no|ok|okay|sure|please|and|also|so|"
               r"hey|um|uh|well)\b[,\s]*"),
    re.compile(r"(?i)^\s*(?:can|could|would|will)\s+you\s+(?:please\s+)?"),
    re.compile(r"(?i)^\s*(?:i(?:'d| would)\s+like\s+(?:you\s+)?to|"
               r"i\s+need\s+you\s+to|i\s+want\s+you\s+to)\s+"),
    # Verb + the noun it is adding — the noun is REQUIRED here, so "put the
    # trash out" keeps its verb and only "set up a reminder ..." loses one.
    re.compile(r"(?i)^\s*(?:please\s+)?(?:add|create|make|set(?:\s+up)?|"
               r"schedule|put|stick|save)\s+(?:a|an|the|another)?\s*"
               r"(?:new\s+|recurring\s+|repeating\s+|weekly\s+|daily\s+|"
               r"calendar\s+)*"
               r"(?:reminders?|events?|appointments?|"
               r"tasks?|to-?dos?|entry)\b\s*"
               r"(?:to|for|that|about|:|-)?\s*"),
    # The two unambiguously calendar verbs may lead on their own.
    re.compile(r"(?i)^\s*(?:please\s+)?(?:add|schedule)\s+(?:a|an|the)?\s*"),
    re.compile(r"(?i)^\s*remind\s+me\s*(?:to|about|that)?\s*"),
    re.compile(r"(?i)^\s*(?:i|we)\s+(?:have\s+to|need\s+to|gotta|"
               r"have\s+got\s+to|must|always|usually)\s+"),
    re.compile(r"(?i)^\s*(?:that\s+|so\s+)?(?:i|we)\s+(?:have|need)\s+to\s+"),
    # The daypart the TIME left behind. "at 6 in the evening to take the bins
    # out" removes only "at 6", so the remainder still opens with "in the
    # evening" and the row would be titled "In the evening to take the bins
    # out". The daypart already did its one job:
    # _parse_time reads it to settle AM/PM — so it is noise by the time the
    # title is cut. A BARE "morning"/"evening" is only stripped when a
    # connector follows it, so "Evening yoga" keeps its name.
    re.compile(r"(?i)^\s*(?:(?:in\s+the|this|early|late)\s+"
               r"(?:morning|afternoon|evening|night)"
               r"|tonight|at\s+night"
               r"|(?:morning|afternoon|evening|night)"
               r"(?=[,\s]+(?:to|that|for)\b))"
               r"\b[,\s]*(?:to\s+|that\s+|for\s+)?"),
    # "after school at 4 to pick the kids up": same leftover, and the connector
    # is REQUIRED here so "after school pickup" stays an activity.
    re.compile(r"(?i)^\s*(?:after|before)\s+"
               r"(?:school|work|dinner|lunch|breakfast|practice)"
               r"\b[,\s]*(?:to\s+|that\s+)"),
    re.compile(r"(?i)^\s*(?:to|that|for|about)\s+(?=\w)"),
)

_EDGE = " \t,.;:-—–"

#: Longer than this and it is prose, not a command.
_MAX_INPUT = 400
#: A title is the ACTIVITY, never the sentence.
_MAX_TITLE_WORDS = 14
_MAX_TITLE_CHARS = 90


#: Stripped only when the sentence ALSO said where it goes ("put piano
#: lessons on the calendar"). Without that destination "put the trash out"
#: keeps its verb, which is the whole activity.
_DEST_LEAD_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:put|stick|add|schedule|save|make)\s+"
    r"(?:a|an|the)?\s*(?=\w)")


def _clean_title(text: str) -> "str | None":
    """The activity, with the request wrapper stripped off, or None."""
    title = (text or "").strip(_EDGE)
    named_destination = False
    for _ in range(6):
        before = title
        for index, rx in enumerate(_TAIL_RES):
            stripped = rx.sub("", title).strip(_EDGE)
            if stripped != title and index == 2:
                named_destination = True
            title = stripped
        for rx in _LEAD_RES:
            title = rx.sub("", title).strip(_EDGE)
        title = re.sub(r"\s{2,}", " ", title).strip(_EDGE)
        if title == before:
            break
    if named_destination:
        title = _DEST_LEAD_RE.sub("", title).strip(_EDGE)
    title = re.sub(r"\s{2,}", " ", title).strip(_EDGE)
    if len(title) < 2 or not re.search(r"[A-Za-z]", title):
        return None
    if len(title) > _MAX_TITLE_CHARS or len(title.split()) > _MAX_TITLE_WORDS:
        return None
    # A leftover that is only a filler word is not an activity.
    if re.fullmatch(r"(?i)(?:it|that|this|them|one|thing|event|reminder)",
                    title):
        return None
    # Neither is one that STARTS with a referent: "put that on the calendar
    # every Tuesday and Thursday at 6:15 instead" leaves "that instead", which
    # would otherwise become a row titled "That instead".
    if re.match(r"(?i)^(?:it|that|this|them|those)\b", title):
        return None
    return title[0].upper() + title[1:]


# ── parsing ─────────────────────────────────────────────────────────────────

class RecurringRequest:
    """A parsed "every <days> at <time>, <activity>" request."""

    __slots__ = ("title", "days", "hour", "minute", "recurrence", "start",
                 "ambiguous")

    def __init__(self, title, days, hour, minute, recurrence, start,
                 ambiguous=False):
        self.title = title
        self.days = days
        self.hour = hour
        self.minute = minute
        self.recurrence = recurrence
        self.start = start
        #: The speaker named an hour that could be AM or PM and said nothing
        #: that settles it. Nothing is written until they answer.
        self.ambiguous = ambiguous

    def __repr__(self):  # pragma: no cover - debugging aid
        return ("RecurringRequest(title=%r, rec=%r, start=%r)"
                % (self.title, self.recurrence, self.start))


def _days_from(body: str) -> set:
    """Every day named in a matched list body."""
    days = set()
    for m in _ITEM_RE.finditer(body or ""):
        token = m.group(0).lower()
        if token in _GROUP_TOKENS:
            days |= set(_GROUP_TOKENS[token])
        elif token in _DAY_TOKENS:
            days.add(_DAY_TOKENS[token])
    return days


def _parse_time(text: str):
    """``((hour, minute), (span_start, span_end), ambiguous)``, or None.

    *ambiguous* is True when the speaker named an hour that could be either
    half of the day and said nothing that settles it — "every Tuesday at 6".
    The alarm route already refuses to guess there
    (``api_server._try_alarm_clarify``); writing 6:00 AM silently here would
    be the same fabrication with a calendar row behind it.
    """
    for m in _TIME_RE.finditer(text or ""):
        raw = (m.group("t1") or m.group("t2") or "").strip()
        ampm = (m.group("ap1") or m.group("ap2") or "").lower()
        raw = re.sub(r"\s+", "", raw)
        if re.match(r"^\d{1,2}[:.]\d{2}$", raw):
            hour, minute = (int(part) for part in re.split(r"[:.]", raw))
        elif re.match(r"^\d{3,4}$", raw):
            hour, minute = int(raw[:-2]), int(raw[-2:])
        else:
            hour, minute = int(raw), 0
        if hour > 23 or minute > 59:
            continue
        ambiguous = False
        if ampm.startswith("a"):
            hour = 0 if hour == 12 else hour
        elif ampm.startswith("p"):
            hour = hour if hour == 12 else hour + 12
        elif hour < 12:
            # No meridiem said out loud: let the sentence decide, and leave a
            # 24-hour reading ("at 17:30") exactly as spoken.
            evening = _EVENING_RE.search(text)
            morning = _MORNING_RE.search(text)
            if evening and not morning:
                hour = hour + 12
            elif not morning and 1 <= hour <= 11:
                # Nothing in the sentence says which half of the day. 0 and 12
                # name one instant each; 1-11 do not.
                ambiguous = True
        if hour > 23:
            continue
        return (hour, minute), (m.start(), m.end()), ambiguous
    return None


def _next_start(days, hour: int, minute: int, now: datetime) -> datetime:
    """First occurrence at or after *now* on one of *days*."""
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute)
        if candidate > now:
            return candidate
    # Every day in the set is behind us today only when the set is empty.
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_recurring_request(text, now=None) -> "RecurringRequest | None":
    """Parse a spoken recurring-reminder request, or return None.

    Requires ALL of: an explicit add intent, a day set, and a time. Anything
    fuzzier is declined so it falls through to the paths that already own it
    (``_try_add_task`` for a one-off reminder, the planner for the rest).
    """
    raw = (text or "").strip()
    if not raw or len(raw) > _MAX_INPUT:
        return None
    if not _ADD_INTENT_RE.search(raw) or _VETO_RE.search(raw):
        return None
    recur = _RECUR_RE.search(raw)
    if not recur:
        return None
    days = _days_from(recur.group("body") or recur.group("pbody") or "")
    if not days:
        return None
    if recur.group("exbody"):
        days -= _days_from(recur.group("exbody"))
    if not days:
        return None
    parsed_time = _parse_time(raw)
    if not parsed_time:
        return None
    (hour, minute), time_span, ambiguous = parsed_time
    spans = sorted([recur.span(), time_span], reverse=True)
    remainder = raw
    for begin, finish in spans:
        remainder = remainder[:begin] + " " + remainder[finish:]
    title = _clean_title(remainder)
    now = now or datetime.now()
    return RecurringRequest(
        title=title,
        days=days,
        hour=hour,
        minute=minute,
        recurrence=_caldates.recurrence_for_days(days),
        start=_next_start(days, hour, minute, now),
        ambiguous=ambiguous,
    )


# ── speaking about it ───────────────────────────────────────────────────────

_REC_PHRASES = {
    "": "once",
    "daily": "every day",
    "weekdays": "every weekday",
    "biweekly": "every other week",
    "monthly": "every month",
    "yearly": "every year",
}


def _day_list_phrase(days) -> str:
    names = [_caldates.WEEKDAY_NAMES[i].capitalize() for i in sorted(days)]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _clock_phrase(when: datetime) -> str:
    return when.strftime("%I:%M %p").lstrip("0")


def _date_phrase(when: datetime) -> str:
    return when.strftime("%A, %B %d").replace(" 0", " ")


def recurrence_phrase(recurrence, start: datetime) -> str:
    """"every Monday, Tuesday, Thursday and Friday" — day set, spoken."""
    rec = (recurrence or "").strip().lower()
    custom = _caldates.custom_days(rec)
    if custom:
        return "every " + _day_list_phrase(custom)
    if rec == "weekly":
        return "every " + _caldates.WEEKDAY_NAMES[start.weekday()].capitalize()
    return _REC_PHRASES.get(rec, rec or "once")


def describe(title: str, start: datetime, recurrence) -> str:
    """"Take the kids to soccer practice, every Monday ... at 6:15 AM"."""
    rec = recurrence_phrase(recurrence, start)
    if rec == "once":
        return "%s, %s at %s" % (title, _date_phrase(start),
                                 _clock_phrase(start))
    return "%s, %s at %s" % (title, rec, _clock_phrase(start))


# ── what the last turn created (conversation-scoped) ────────────────────────
#
# Voice repeats itself, so "add it to the calendar" has to resolve to
# something. One box, one household, one conversation at a time: a module
# level record is the whole mechanism, and it expires so a follow-up cannot
# attach itself to yesterday's event.

_LAST_ITEM = {}
_LAST_ITEM_TTL_SECONDS = 3600


def remember(title, start, recurrence, event_id=None) -> None:
    _LAST_ITEM.clear()
    _LAST_ITEM.update({
        "title": title,
        "start": start,
        "rec": recurrence or "",
        "id": event_id,
        "at": time.time(),
    })


def last_item(max_age_seconds: float = _LAST_ITEM_TTL_SECONDS):
    """The item this conversation just created, or None once it is stale."""
    if not _LAST_ITEM:
        return None
    if time.time() - float(_LAST_ITEM.get("at") or 0) > max_age_seconds:
        return None
    return dict(_LAST_ITEM)


def forget() -> None:
    _LAST_ITEM.clear()


# ── writing it down ─────────────────────────────────────────────────────────

def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M")


def _start_of(event) -> "datetime | None":
    try:
        return datetime.fromisoformat(str(event.get("start"))[:19])
    except (TypeError, ValueError):
        return None


def _schedule_of(event) -> "tuple[tuple[int, int], set] | None":
    """``((hour, minute), day_set)`` for a stored row, or None.

    None means the row's schedule cannot be compared as a weekday set — a
    one-off, a ``biweekly``/``monthly``/``yearly`` cadence, an unreadable
    start. A caller must then treat it as a DIFFERENT item rather than
    guessing, because guessing would overwrite the Mon, Tue, Thu, Fri 06:15
    row.
    """
    start = _start_of(event)
    if start is None:
        return None
    days = _caldates.days_of_recurrence(event.get("rec"), start)
    if days is None:
        return None
    return (start.hour, start.minute), days


def _is_same_item(event, req: RecurringRequest) -> bool:
    """Same activity, same time of day, same days — then it is the same item.

    A matching title is NOT enough. "every Saturday at 9 am to take the kids
    to soccer practice" is a SECOND practice, not an edit of the Mon, Tue,
    Thu, Fri 06:15 one, and treating it as an edit would delete a real
    commitment from the household's week.
    """
    schedule = _schedule_of(event)
    if schedule is None:
        return False
    (hour, minute), days = schedule
    return (hour, minute) == (req.hour, req.minute) and days == set(req.days)


def _existing_phrase(event) -> str:
    """"the every Monday ... at 6:15 AM one" — how an untouched row is named."""
    start = _start_of(event)
    if start is None:
        return "the one already there"
    rec = recurrence_phrase(event.get("rec") or "", start)
    return "the %s at %s one" % (rec, _clock_phrase(start))


def apply_request(req: RecurringRequest, app, title=None,
                  referential=False) -> str:
    """Create, update or decline — and say which one actually happened.

    *referential* means the utterance pointed at the item the previous turn
    created ("make that Tuesdays and Thursdays instead"). Only then may a row
    whose schedule DIFFERS be rewritten; otherwise a differing schedule is a
    second item and gets its own row, said out loud.
    """
    subject = title or req.title
    start_iso = _iso(req.start)
    candidates = app.cal_find_events(subject)
    same = next((e for e in candidates if _is_same_item(e, req)), None)
    if same is not None:
        stored = _start_of(same) or req.start
        remember(subject, stored, same.get("rec") or req.recurrence,
                 same.get("id"))
        return ("That's already on the calendar: %s. Next one is %s. "
                "I didn't add it again."
                % (describe(subject, req.start, req.recurrence),
                   _date_phrase(req.start)))
    if referential:
        # THE REMEMBERED ROW, BY ID — never "the first one with this title".
        # candidates is oldest-first, so targeting candidates[0] would rewrite
        # the Mon, Tue, Thu, Fri 06:15 row when the turn before had created a
        # SECOND Saturday one and "that" meant the Saturday (silent data loss). If
        # the remembered row is gone, ask.
        remembered = (last_item() or {}).get("id")
        target = next((e for e in candidates
                       if remembered and e.get("id") == remembered), None)
        if target is None:
            return ("I'm not sure which one you meant — the one I just put "
                    "on your calendar isn't there any more. Say the whole "
                    "thing again (what it is, which days, what time) and "
                    "I'll set it up.")
        app.cal_edit_event(
            target.get("id"),
            start=start_iso,
            end=_iso(req.start + timedelta(hours=1)),
            rec=req.recurrence or None,
        )
        remember(subject, req.start, req.recurrence, target.get("id"))
        return ("Updated the one already on your calendar: %s. Next one is %s."
                % (describe(subject, req.start, req.recurrence),
                   _date_phrase(req.start)))
    app.cal_add_event(subject, start_iso, recurrence=req.recurrence or None)
    created = app.cal_find_event(subject, start_iso) or {}
    remember(subject, req.start, req.recurrence, created.get("id"))
    if candidates:
        # Something by the same name is already there on a different schedule.
        # Say that it survived, in the same breath — a household that hears
        # only "added" has no way to know whether the other one is still real.
        return ("Added a second one: %s. First one is %s. I left %s alone."
                % (describe(subject, req.start, req.recurrence),
                   _date_phrase(req.start), _existing_phrase(candidates[0])))
    return ("Added to the calendar: %s. First one is %s."
            % (describe(subject, req.start, req.recurrence),
               _date_phrase(req.start)))


def _bare_clock(req: RecurringRequest) -> str:
    """"6" / "6:30" — the hour as spoken, with no half of the day claimed."""
    if req.minute:
        return "%d:%02d" % (req.hour, req.minute)
    return str(req.hour)


def _ask_half_of_day(req: RecurringRequest) -> str:
    """The alarm route's rule, on the calendar route: ask, and write nothing."""
    clock = _bare_clock(req)
    return ("Did you mean %s AM or %s PM? Say the one you want and I'll put "
            "it on the calendar %s — I'd rather ask than pick the wrong half "
            "of the day."
            % (clock, clock, recurrence_phrase(req.recurrence, req.start)))


def _ask_what_it_is_for(req: RecurringRequest) -> str:
    """A time and a day set with no activity. Nothing is written."""
    clock = _bare_clock(req) if req.ambiguous else _clock_phrase(req.start)
    return ("What is that for? Tell me what happens %s at %s and I'll add it."
            % (recurrence_phrase(req.recurrence, req.start), clock))


def _handle_followup(text: str, app) -> "str | None":
    """"Add it to the calendar" / "make that weekly" — the SAME item."""
    item = last_item()
    if not item:
        return None
    title = item.get("title")
    start = item.get("start")
    if not title or not isinstance(start, datetime):
        return None

    change = _CHANGE_REC_RE.match(text or "")
    if change:
        rec = _CHANGE_REC_VALUES.get(
            re.sub(r"\s+", " ", change.group("rec").strip().lower()))
        if not rec:
            return None
        event = app.cal_find_event(title, _iso(start))
        if not event:
            return None
        if (event.get("rec") or "") == rec:
            return ("That's already how it repeats: %s."
                    % describe(title, start, rec))
        app.cal_edit_event(event.get("id"), rec=rec)
        remember(title, start, rec, event.get("id"))
        return ("Changed how that repeats: %s. Next one is %s."
                % (describe(title, start, rec), _date_phrase(start)))

    if not _FOLLOWUP_ADD_RE.match(text or ""):
        return None
    event = app.cal_find_event(title, _iso(start))
    if event:
        stored = _start_of(event) or start
        remember(title, stored, event.get("rec") or "", event.get("id"))
        return ("It's already on the calendar: %s. Next one is %s. "
                "Nothing else to add."
                % (describe(title, stored, event.get("rec") or ""),
                   _date_phrase(stored)))
    # Remembered but not in the store (the write failed, or it was deleted):
    # writing it now is the honest thing, and it says so.
    app.cal_add_event(title, _iso(start), recurrence=item.get("rec") or None)
    created = app.cal_find_event(title, _iso(start)) or {}
    remember(title, start, item.get("rec") or "", created.get("id"))
    return ("Added to the calendar: %s. First one is %s."
            % (describe(title, start, item.get("rec") or ""),
               _date_phrase(start)))


def handle(text, app, now=None) -> "str | None":
    """The whole fastpath: follow-up first, then a fresh recurring request.

    Returns the reply to speak, or None to let every existing path have the
    turn exactly as before.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > _MAX_INPUT:
        return None
    followup = _handle_followup(raw, app)
    if followup:
        return followup
    req = parse_recurring_request(raw, now=now)
    if req is None:
        return None
    subject = req.title
    referential = False
    if not subject:
        # A recurrence and a time but no activity ("add a reminder every
        # Tuesday at 6"). The remembered item is consulted ONLY when the
        # utterance actually points at it — without that gate this repurposed
        # an unrelated row that happened to be the last one created.
        #
        # An utterance that DOES name its own activity is never referential,
        # whatever pronouns it contains: turn 1 ends "Can you add that?" and
        # means the sentence in front of it, not a stored row.
        item = last_item() if _REFERENTIAL_RE.search(raw) else None
        if not (item and item.get("title")):
            return _ask_what_it_is_for(req)
        subject = item["title"]
        referential = True
    if req.ambiguous:
        return _ask_half_of_day(req)
    return apply_request(req, app, title=subject, referential=referential)


# ── deleting one of them ────────────────────────────────────────────────────
#
# "delete the soccer practice reminder" is vetoed by _VETO_RE above (rightly,
# it is not an ADD); falling through to the planner risks a "deleted" reply
# while the row is still there. Sent to the store directly it is no better: a
# task and an event of the same title make cal_delete_event answer "I found 2
# that match: 'X', 'X'. Which one?", two identical options, nothing removed.
#
# So deleting gets the same treatment adding got: deterministic resolution, a
# disambiguation that says what each candidate ACTUALLY IS, and a
# confirmation counted from the store AFTER the write.

_DELETE_RE = re.compile(
    r"(?i)^\s*(?:yes|yeah|yep|ok|okay|please|and|also|hey)?[,\s]*"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:delete|remove|cancel|clear|drop|get\s+rid\s+of|take\s+off)\s+"
    r"(?P<body>.+?)\s*[.!?]*\s*$")

#: The other apps' words, and the mass-delete phrasings. Any of these and the
#: turn is not ours: an alarm keeps AlarmFlow, a med keeps the vault route,
#: and "delete everything" is not a thing a fastpath may decide.
_DELETE_VETO_RE = re.compile(
    r"(?i)\b(?:alarm|alarms|timer|timers|"
    r"shopping|grocer(?:y|ies)|pantry|"
    r"pill|pills|medication|medications|meds|medicine|dose|doses|insulin|"
    r"prescription|"
    r"light|lights|lamp|music|song|songs|playlist|radio|"
    r"history|memor(?:y|ies)|conversation|chat|message|messages|"
    r"account|file|files|photo|photos|document|documents|"
    r"note|notes|recipe|recipes|meal|meals|"
    r"everything|every\s+event|all\s+(?:my|the)\s+(?:events|tasks|reminders)|"
    r"whole\s+calendar)\b")

_DELETE_TAIL_RES = (
    re.compile(r"(?i)[\s,]*(?:from|off|out\s+of)\s+(?:my|the)?\s*"
               r"(?:calendar|calender|schedule|to-?do\s+list|list|"
               r"reminders?|tasks?|events?)\s*$"),
    re.compile(r"(?i)\s+(?:reminders?|events?|tasks?|to-?dos?|appointments?|"
               r"entry|entries|item|items|thing|things|one)\s*$"),
    re.compile(r"(?i)[\s,]*(?:please|for\s+me|thanks|thank\s+you)\s*$"),
)

_DELETE_LEAD_RE = re.compile(
    r"(?i)^\s*(?:all\s+of\s+the|all\s+the|the|my|our|that|this|a|an)\s+")

#: What is left after the wrapper comes off must still NAME something. These
#: do not, so they are declined rather than resolved against every row.
_DELETE_EMPTY = frozenset({
    "", "calendar", "calender", "schedule", "event", "events", "task",
    "tasks", "reminder", "reminders", "it", "that", "this", "them", "one",
    "thing", "everything", "all",
})

#: "both", "all of them", "both of them" — the answer to a disambiguation.
_DELETE_BOTH_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:delete|remove|cancel)?\s*"
    r"(?:them\s+)?(?:both|all\s+of\s+(?:them|those)|all\s+of\s+it|all)"
    r"(?:\s+of\s+them)?\s*(?:please)?\s*[.!?]*\s*$")

#: "the task", "the event", "the recurring one" — the other useful answer.
_DELETE_KIND_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:delete|remove|cancel|just)?\s*"
    r"(?:the\s+)?(?P<kind>task|to-?do|reminder|event|recurring\s+one|"
    r"repeating\s+one)\s*(?:one)?\s*(?:please)?\s*[.!?]*\s*$")

#: An open "which one should I delete?" prompt, PER CONVERSATION. A single
#: module-global record with a long life, consulted at the top of every turn
#: before any veto, would let a bare "both" said minutes later, or in a
#: different conversation entirely, delete two rows nobody had named on that
#: turn. Three things hold it shut: the
#: conversation id it was asked in, a two-minute life, and ``note_turn``,
#: which drops it the moment ANY turn arrives that is not a bare answer.
_PENDING_DELETES: dict = {}
_PENDING_DELETE_TTL_SECONDS = 120


def _bare_delete_answer(text) -> bool:
    """"both" / "the task" — the only two shapes a prompt may ever claim."""
    raw = text or ""
    return bool(_DELETE_BOTH_RE.match(raw) or _DELETE_KIND_RE.match(raw))


def _pending_key(conversation_id) -> str:
    return str(conversation_id or "").strip()


def _remember_pending_delete(key, phrase, items) -> None:
    if not key:
        # No conversation to answer in (a surface that does not carry an id).
        # Ask anyway, but file it under a key nothing can ever look up: a
        # later bare "both" from anywhere must not resolve it.
        key = "anon:" + uuid.uuid4().hex
    _PENDING_DELETES[key] = {
        "phrase": phrase,
        "items": [{"kind": i["kind"], "id": i["row"].get("id"),
                   "title": i["row"].get("title")} for i in items],
        "at": time.time(),
    }


def _pending_delete(key):
    pending = _PENDING_DELETES.get(key) if key else None
    if not pending:
        return None
    if time.time() - float(pending.get("at") or 0) > \
            _PENDING_DELETE_TTL_SECONDS:
        _PENDING_DELETES.pop(key, None)
        return None
    return dict(pending)


def note_turn(text, conversation_id=None) -> None:
    """Any turn that is NOT a bare answer closes the open delete prompt.

    Called at the top of the fastpath layer as well as of :func:`handle_delete`,
    so a turn some other capability answers still counts as an intervening
    turn. Without an id to scope it to, every open prompt is dropped — the
    failure mode has to be "the household repeats itself", never "something
    was deleted that nobody named".
    """
    if _bare_delete_answer(text):
        return
    key = _pending_key(conversation_id)
    if key:
        _PENDING_DELETES.pop(key, None)
    else:
        _PENDING_DELETES.clear()


def forget_pending_delete() -> None:
    _PENDING_DELETES.clear()


def _short_date(when: datetime) -> str:
    return when.strftime("%B %d").replace(" 0", " ")


def _parse_stored(value) -> "datetime | None":
    try:
        return datetime.fromisoformat(str(value)[:19])
    except (TypeError, ValueError):
        return None


def _item_phrase(item) -> str:
    """"a task due September 10" / "a recurring event, every Tuesday ...".

    The KIND and the SCHEDULE, because that is the only thing that tells two
    same-titled rows apart. The prompt this replaces offered "'X', 'X'".
    """
    row = item["row"]
    if item["kind"] == "task":
        due = _parse_stored(row.get("due"))
        if due is None:
            return "a task with no due date"
        return "a task due %s" % _short_date(due)
    start = _parse_stored(row.get("start"))
    if start is None:
        return "an event with no date"
    rec = (row.get("rec") or "").strip()
    if rec:
        return ("a recurring event, %s at %s"
                % (recurrence_phrase(rec, start), _clock_phrase(start)))
    return "an event on %s at %s" % (_short_date(start), _clock_phrase(start))


def _clean_delete_body(body: str) -> "str | None":
    text = (body or "").strip(_EDGE)
    for _ in range(4):
        before = text
        for rx in _DELETE_TAIL_RES:
            text = rx.sub("", text).strip(_EDGE)
        text = _DELETE_LEAD_RE.sub("", text).strip(_EDGE)
        if text == before:
            break
    text = re.sub(r"\s{2,}", " ", text).strip(_EDGE)
    if text.lower() in _DELETE_EMPTY or len(text) < 3:
        return None
    return text


def _delete_item(item, app) -> None:
    row_id = item["row"].get("id")
    if not row_id:
        return
    if item["kind"] == "task":
        app.cal_delete_task(row_id)
    else:
        app.cal_delete_event(row_id)


def _confirm_delete(app, phrase, targets) -> str:
    """Delete, then read the store back and describe what is ACTUALLY gone.

    Never templated: a templated confirmation can say "deleted" about a row
    that is still there.
    """
    wanted = {t["row"].get("id") for t in targets} - {None}
    for target in targets:
        _delete_item(target, app)
    left = app.cal_find_items(phrase)
    surviving_ids = {i["row"].get("id") for i in left}
    gone = [t for t in targets if t["row"].get("id") not in surviving_ids]
    if not gone:
        return ("I couldn't remove %s — it's still on your calendar. Open the "
                "calendar app, or tell me its exact title."
                % (targets[0]["row"].get("title") or "that"))
    title = gone[0]["row"].get("title") or phrase
    what = " and ".join(_item_phrase(t) for t in gone)
    reply = "Deleted %s: %s." % (what, title)
    others = [i for i in left if i["row"].get("id") not in wanted]
    if others:
        reply += (" Still there under that name: %s."
                  % " and ".join(_item_phrase(i) for i in others))
    else:
        reply += " Nothing else by that name is left."
    return reply


def _answer_pending_delete(text: str, app, key: str) -> "str | None":
    """"both" / "the task" — the answer to a disambiguation we just asked."""
    if not _bare_delete_answer(text):
        return None
    pending = _pending_delete(key)
    if not pending:
        return None
    phrase = pending.get("phrase") or ""
    remembered = {(i["kind"], i["id"]) for i in pending.get("items") or []}
    live = [i for i in app.cal_find_items(phrase)
            if (i["kind"], i["row"].get("id")) in remembered]
    if len(live) < 2:
        return None

    if _DELETE_BOTH_RE.match(text or ""):
        _PENDING_DELETES.pop(key, None)
        return _confirm_delete(app, phrase, live)

    kind_match = _DELETE_KIND_RE.match(text or "")
    if not kind_match:
        return None
    said = re.sub(r"[\s-]+", "", kind_match.group("kind").strip().lower())
    if said in ("task", "todo", "reminder"):
        chosen = [i for i in live if i["kind"] == "task"]
    elif said == "event":
        chosen = [i for i in live if i["kind"] == "event"]
    else:       # "the recurring one" / "the repeating one"
        chosen = [i for i in live
                  if i["kind"] == "event" and (i["row"].get("rec") or "")]
    if len(chosen) != 1:
        return None
    _PENDING_DELETES.pop(key, None)
    return _confirm_delete(app, phrase, chosen)


def _register_clarifier(key, phrase, items, sentence, app) -> None:
    """Keep this question where the NEXT turn will look for it.

    Without this, an answer like "the every-weekday one" is routed as a
    brand-new query: it reaches the knowledge library and comes back with a
    dictionary definition of "every weekday" while both events stay on the
    calendar. The local record here only accepts "both" and "the task"/"the
    event"; every other way of naming one of two rows needs the clarifier.

    So the same candidates are ALSO registered with the shared clarifier store,
    which matches on the words of the option itself ("every weekday", "recurring",
    "Tuesday"), on an ordinal ("the first one") and on "both". The local record
    stays exactly as it is — it is what answers a bare "both" said to the
    ``handle_delete`` entry point — and the completion below pops it, so one
    answer can never delete twice.
    """
    try:
        from puffpai import pending_clarifier
    except Exception:      # pragma: no cover - import guard
        return
    options = []
    for item in items[:4]:
        row = item["row"]
        rec = (row.get("rec") or "").strip()
        options.append({
            "label": _item_phrase(item),
            "value": (item["kind"], row.get("id")),
            "tokens": [_item_phrase(item), item["kind"],
                       "todo" if item["kind"] == "task" else "",
                       "reminder" if item["kind"] == "task" else "",
                       "recurring repeating" if rec else "single one-off"],
        })
    if len(options) < 2:
        return

    def _complete(chosen, said, phrase=phrase, key=key, app=app):
        wanted = {c["value"] for c in (chosen or [])}
        if not wanted:
            return None
        live = [i for i in app.cal_find_items(phrase)
                if (i["kind"], i["row"].get("id")) in wanted]
        if not live:
            return None
        _PENDING_DELETES.pop(key, None)
        return _confirm_delete(app, phrase, live)

    pending_clarifier.register("calendar_delete", _complete, options,
                               conversation_id=key, prompt=sentence)


def handle_delete(text, app, conversation_id=None) -> "str | None":
    """Delete a calendar item by name, deterministically, or decline.

    Returns the reply to speak, or None to leave the turn exactly as it is
    today. Declines whenever the phrase names nothing that is actually in the
    calendar store, so an alarm, a med, a grocery and a question all keep the
    path they already have.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > _MAX_INPUT:
        return None
    key = _pending_key(conversation_id)
    # An intervening turn closes the prompt BEFORE anything else looks at it.
    note_turn(raw, conversation_id)
    answered = _answer_pending_delete(raw, app, key)
    if answered:
        return answered
    if _DELETE_VETO_RE.search(raw):
        return None
    match = _DELETE_RE.match(raw)
    if not match:
        return None
    phrase = _clean_delete_body(match.group("body"))
    if not phrase:
        return None
    items = app.cal_find_items(phrase)
    if not items:
        # Nothing by that name here. Decline rather than claim a miss: a day
        # phrase or a raw id is still the planner's to resolve.
        return None
    if len(items) == 1:
        _PENDING_DELETES.pop(key, None)
        return _confirm_delete(app, phrase, items)
    _remember_pending_delete(key, phrase, items)
    listed = " and ".join(_item_phrase(i) for i in items[:4])
    more = "" if len(items) <= 4 else " (and %d more)" % (len(items) - 4)
    sentence = ("There's %s%s under that name. Which one should I delete — or "
                "both?" % (listed, more))
    _register_clarifier(key, phrase, items, sentence, app)
    return sentence
