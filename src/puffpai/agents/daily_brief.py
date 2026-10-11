"""The daily briefing, composed from data this appliance already holds. No model.

Through the model, "give me my daily briefing" can time out after 45 seconds
and take **53 seconds** on the retry. The briefing route
(``assistant_tools.daily_briefing``) makes seven tool calls: a
forecast, the schedule, a web search, an
events feed, an expiry check, a Zone-3 probe and a fitness read — and then
hands the whole pile to the LLM to write up. A voice turn cannot wait for
that, and the answer it eventually produces can open with "Good morning" at
00:50 because the model writes the greeting from a template.

So the SPOKEN briefing is composed here instead, from four things the appliance can
read in milliseconds: the weather the forecast fastpath already fetches, the
air-quality number the dashboard card reads, today's calendar occurrences, and
the count of open tasks. Four sentences, every one of them a fact read from a
store — nothing is summarised, so nothing can be invented. The long, written
briefing stays exactly where it is for the dashboard and the morning digest.

Pure: :func:`compose` takes values and returns a string. Everything that does
I/O lives in ``api_server._try_daily_briefing``, which is where the stores are.
"""

from __future__ import annotations

from datetime import datetime

from puffpai.voice_text import daypart_greeting

#: How many of today's occurrences are named out loud. A spoken list longer
#: than this is unusable, and the rest are on the screen.
MAX_NAMED = 3


def _clock(value) -> str:
    """"9 AM" / "7:30 PM" from a datetime or an ISO string. "" when unknown."""
    when = value
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when[:19])
        except ValueError:
            return ""
    if not isinstance(when, datetime):
        return ""
    hour = when.hour % 12 or 12
    suffix = "AM" if when.hour < 12 else "PM"
    if when.minute:
        return f"{hour}:{when.minute:02d} {suffix}"
    return f"{hour} {suffix}"


def _join(names) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _number(value):
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return None


def weather_sentence(temp=None, high=None, aqi=None, aqi_label="") -> str:
    """"It's 82 out, with a high of 100 today, and the air quality is 30 —
    good." Every clause is dropped rather than guessed when it is missing."""
    temp_s, high_s = _number(temp), _number(high)
    bits = []
    if temp_s is not None:
        bits.append(f"It's {temp_s} out")
        if high_s is not None:
            bits.append(f"with a high of {high_s} today")
    elif high_s is not None:
        bits.append(f"Today's high is {high_s}")
    air = _number(aqi)
    if air is not None:
        label = str(aqi_label or "").strip().lower()
        clause = f"the air quality is {air}"
        if label:
            clause += f" — {label}"
        if bits:
            bits.append("and " + clause)
        else:
            bits.append(clause[0].upper() + clause[1:])
    if not bits:
        return ""
    return ", ".join(bits).replace(", and ", ", and ") + "."


def calendar_sentence(occurrences=()) -> str:
    """"You have 2 things on the calendar today: Dentist at 9 AM and …"."""
    rows = [r for r in (occurrences or []) if isinstance(r, dict)]
    if not rows:
        return "Nothing is on the calendar today."
    named = []
    for row in rows[:MAX_NAMED]:
        title = str(row.get("title") or "").strip() or "an untitled entry"
        if row.get("all_day"):
            named.append(title)
            continue
        at = _clock(row.get("start"))
        named.append(f"{title} at {at}" if at else title)
    thing = "thing" if len(rows) == 1 else "things"
    lead = f"You have {len(rows)} {thing} on the calendar today"
    more = len(rows) - len(named)
    tail = _join(named)
    if more > 0:
        tail += f", plus {more} more"
    return f"{lead}: {tail}."


def tasks_sentence(open_tasks=0) -> str:
    count = int(open_tasks or 0)
    if count <= 0:
        return "No tasks are open."
    return f"{count} task{'s' if count != 1 else ''} still open."


def compose(now=None, city="", temp=None, high=None, aqi=None,
            aqi_label="", occurrences=(), open_tasks=0) -> str:
    """The whole spoken briefing: four sentences, no model, no invention."""
    when = now or datetime.now()
    greeting = daypart_greeting(when.hour)
    city = str(city or "").strip()
    sentences = [f"{greeting}, {city}." if city else f"{greeting}."]
    weather = weather_sentence(temp, high, aqi, aqi_label)
    if weather:
        sentences.append(weather)
    sentences.append(calendar_sentence(occurrences))
    sentences.append(tasks_sentence(open_tasks))
    return " ".join(sentences)
