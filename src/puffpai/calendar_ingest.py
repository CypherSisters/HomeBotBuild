"""
Calendar file ingest — turn a user-loaded file into calendar event candidates.

Three extraction tiers, picked by file type:
  .ics  -> deterministic iCalendar parse (school calendars, Google exports)
  .csv  -> deterministic column mapping (header sniffing)
  other -> LLM extraction from the file's text (schedules pasted in PDFs,
           word docs, emails...), emitting strict JSON

Nothing here WRITES to the calendar. Every function returns candidate
event dicts shaped for AppData.cal_add_event:
    {title, start_iso, end_iso, cat, location, recurrence}
The dashboard shows candidates to the user, who confirms which to add —
LLM guesses especially must never land on the family calendar unreviewed.
"""

import csv
import io
import json
import re
from datetime import datetime, timedelta

# Hard cap per file: a runaway RRULE or a 5-year export shouldn't flood the UI.
MAX_EVENTS = 200

VALID_CATS = ('medical', 'sports', 'school', 'work', 'family',
              'bills', 'appointment', 'other')
VALID_RECURRENCE = ('daily', 'weekdays', 'weekly', 'biweekly',
                    'monthly', 'yearly')


def _clean_event(ev):
    """Validate/coerce one candidate; None if unusable."""
    title = str(ev.get('title') or '').strip()
    start = str(ev.get('start_iso') or '').strip()
    if not title or not start:
        return None
    try:
        start_dt = datetime.fromisoformat(start)
    except ValueError:
        return None
    end = str(ev.get('end_iso') or '').strip()
    try:
        end_dt = datetime.fromisoformat(end) if end else None
    except ValueError:
        end_dt = None
    if end_dt is None or end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)

    cat = str(ev.get('cat') or ev.get('category') or 'other').lower().strip()
    if cat not in VALID_CATS:
        cat = 'other'
    rec = ev.get('recurrence')
    rec = str(rec).lower().strip() if rec else None
    if rec not in VALID_RECURRENCE:
        rec = None
    loc = str(ev.get('location') or '').strip()[:200] or None

    return {
        'title': title[:200],
        'start_iso': start_dt.isoformat(),
        'end_iso': end_dt.isoformat(),
        'cat': cat,
        'location': loc,
        'recurrence': rec,
    }


# ── iCalendar (.ics) ─────────────────────────────────────────────────────────

def _unfold_ics(text):
    """RFC 5545 line unfolding: a line starting with space/tab continues
    the previous line."""
    out = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if raw[:1] in (' ', '\t') and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out

def _ics_unescape(value):
    return (value.replace('\\n', ' ').replace('\\,', ',')
                 .replace('\\;', ';').replace('\\\\', '\\')).strip()

def _parse_ics_dt(value):
    """DTSTART value -> naive local datetime. UTC ('Z') converts to local;
    TZID values are taken at face value (good enough for one-household use)."""
    value = value.strip()
    is_utc = value.endswith('Z')
    if is_utc:
        value = value[:-1]
    try:
        if 'T' in value:
            dt = datetime.strptime(value, '%Y%m%dT%H%M%S')
        else:
            dt = datetime.strptime(value, '%Y%m%d').replace(hour=9)
    except ValueError:
        return None
    if is_utc:
        try:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        except Exception:
            pass
    return dt

_WEEKDAY_SET = {'MO', 'TU', 'WE', 'TH', 'FR'}

def _rrule_to_recurrence(rrule):
    """Collapse an RRULE to the calendar's simple vocabulary; None if it
    doesn't fit (the concrete DTSTART instance still imports)."""
    parts = dict(
        p.split('=', 1) for p in rrule.split(';') if '=' in p)
    freq = parts.get('FREQ', '').upper()
    interval = int(parts.get('INTERVAL', '1') or 1)
    byday = {d.strip().upper() for d in parts.get('BYDAY', '').split(',') if d.strip()}
    if freq == 'DAILY' and interval == 1:
        return 'daily'
    if freq == 'WEEKLY':
        if byday and byday == _WEEKDAY_SET:
            return 'weekdays'
        if interval == 2:
            return 'biweekly'
        if interval == 1:
            return 'weekly'
    if freq == 'MONTHLY' and interval == 1:
        return 'monthly'
    if freq == 'YEARLY' and interval == 1:
        return 'yearly'
    return None

def parse_ics(text):
    """Parse VEVENTs. Returns (events, notes)."""
    events, notes = [], []
    in_event, cur = False, {}
    for line in _unfold_ics(text):
        u = line.upper()
        if u.startswith('BEGIN:VEVENT'):
            in_event, cur = True, {}
            continue
        if u.startswith('END:VEVENT'):
            in_event = False
            ev = _clean_event(cur)
            if ev:
                events.append(ev)
            if len(events) >= MAX_EVENTS:
                notes.append(f'Stopped after {MAX_EVENTS} events.')
                break
            continue
        if not in_event or ':' not in line:
            continue
        prop, value = line.split(':', 1)
        name = prop.split(';', 1)[0].upper()
        if name == 'SUMMARY':
            cur['title'] = _ics_unescape(value)
        elif name == 'LOCATION':
            cur['location'] = _ics_unescape(value)
        elif name == 'DTSTART':
            dt = _parse_ics_dt(value)
            if dt:
                cur['start_iso'] = dt.isoformat()
        elif name == 'DTEND':
            dt = _parse_ics_dt(value)
            if dt:
                cur['end_iso'] = dt.isoformat()
        elif name == 'RRULE':
            rec = _rrule_to_recurrence(value)
            if rec:
                cur['recurrence'] = rec
            else:
                notes.append(
                    f"Repeat rule on '{cur.get('title', '?')}' was too complex — imported as a single event.")
    return events, notes


# ── CSV ──────────────────────────────────────────────────────────────────────

_COL_ALIASES = {
    'title': ('title', 'event', 'subject', 'name', 'description', 'what'),
    'date': ('date', 'start date', 'start', 'day', 'when'),
    'time': ('time', 'start time'),
    'end': ('end', 'end date', 'end time', 'until'),
    'location': ('location', 'where', 'place', 'room'),
    'cat': ('category', 'cat', 'type', 'calendar'),
}

def _match_col(header, want):
    hl = [h.lower().strip() for h in header]
    for alias in _COL_ALIASES[want]:
        if alias in hl:
            return hl.index(alias)
    return None

def _parse_loose_dt(date_s, time_s=''):
    date_s, time_s = (date_s or '').strip(), (time_s or '').strip()
    if not date_s:
        return None
    fmts = ['%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%B %d, %Y', '%b %d, %Y',
            '%d %B %Y', '%Y/%m/%d']
    base = None
    for f in fmts:
        try:
            base = datetime.strptime(date_s, f)
            break
        except ValueError:
            continue
    if base is None:
        try:
            base = datetime.fromisoformat(date_s)
        except ValueError:
            return None
    if time_s:
        for tf in ('%H:%M', '%I:%M %p', '%I:%M%p', '%I %p'):
            try:
                t = datetime.strptime(time_s.upper(), tf)
                return base.replace(hour=t.hour, minute=t.minute)
            except ValueError:
                continue
    if base.hour == 0 and base.minute == 0 and 'T' not in date_s:
        base = base.replace(hour=9)
    return base

def parse_csv(text):
    """Header-sniffing CSV parse. Returns (events, notes)."""
    events, notes = [], []
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        return [], [f'CSV could not be read: {e}']
    if not rows:
        return [], ['CSV file is empty.']
    header = rows[0]
    ti = _match_col(header, 'title')
    di = _match_col(header, 'date')
    if ti is None or di is None:
        return [], ["Couldn't find title/date columns. Expected headers "
                    "like 'Title' and 'Date' (aliases: event/subject, "
                    "start/when...)."]
    hi = _match_col(header, 'time')
    ei = _match_col(header, 'end')
    li = _match_col(header, 'location')
    ci = _match_col(header, 'cat')

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) else ''

    skipped = 0
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        start = _parse_loose_dt(cell(row, di), cell(row, hi))
        if not start or not cell(row, ti):
            skipped += 1
            continue
        end = _parse_loose_dt(cell(row, ei)) if ei is not None else None
        ev = _clean_event({
            'title': cell(row, ti),
            'start_iso': start.isoformat(),
            'end_iso': end.isoformat() if end else '',
            'location': cell(row, li),
            'cat': cell(row, ci),
        })
        if ev:
            events.append(ev)
        if len(events) >= MAX_EVENTS:
            notes.append(f'Stopped after {MAX_EVENTS} events.')
            break
    if skipped:
        notes.append(f'{skipped} row(s) skipped (missing title or unreadable date).')
    return events, notes


# ── LLM extraction (freeform text / PDFs / docs) ─────────────────────────────

_LLM_PROMPT = """Extract every calendar event from the document below.

Today's date is {today}. Resolve relative dates ("next Friday") against it;
if a date has no year, assume the next occurrence from today.

Reply with ONLY a JSON array, no prose. Each element:
  {{"title": str, "start_iso": "YYYY-MM-DDTHH:MM:SS", "end_iso": str|null,
    "location": str|null, "cat": one of {cats},
    "recurrence": null or one of {recs}}}
Use 09:00 for events with no stated time. If there are no events, reply [].

DOCUMENT:
{document}
"""

def llm_extract(text, llm_chat):
    """Ask the LLM for events. `llm_chat(messages) -> str`. Returns
    (events, notes)."""
    doc = text[:12000]
    prompt = _LLM_PROMPT.format(
        today=datetime.now().strftime('%A, %Y-%m-%d'),
        cats=list(VALID_CATS), recs=list(VALID_RECURRENCE), document=doc)
    try:
        reply = llm_chat([{"role": "user", "content": prompt}]) or ''
    except Exception as e:
        return [], [f'LLM extraction failed: {e}']
    m = re.search(r'\[.*\]', reply, re.DOTALL)
    if not m:
        return [], ['The AI found no events in this file.']
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], ["The AI's answer wasn't valid JSON — try a cleaner file."]
    if not isinstance(raw, list):
        return [], ['The AI found no events in this file.']
    events, dropped = [], 0
    for ev in raw[:MAX_EVENTS]:
        cleaned = _clean_event(ev) if isinstance(ev, dict) else None
        if cleaned:
            events.append(cleaned)
        else:
            dropped += 1
    notes = ['Events below were read by the AI — check them before adding.']
    if dropped:
        notes.append(f'{dropped} suggestion(s) dropped (missing title/date).')
    if len(text) > 12000:
        notes.append('Long document — only the first part was read.')
    return events, notes


# ── Dispatch ─────────────────────────────────────────────────────────────────

def ingest_text(filename, text, llm_chat=None):
    """Parse file content into candidates. Returns
    {method, events, notes}."""
    ext = (filename.rsplit('.', 1)[-1] if '.' in filename else '').lower()
    if ext == 'ics' or 'BEGIN:VCALENDAR' in text[:2000].upper():
        events, notes = parse_ics(text)
        return {'method': 'ics', 'events': events, 'notes': notes}
    if ext == 'csv':
        events, notes = parse_csv(text)
        return {'method': 'csv', 'events': events, 'notes': notes}
    if llm_chat is None:
        return {'method': 'none', 'events': [],
                'notes': ['This file type needs AI extraction, which is unavailable right now.']}
    events, notes = llm_extract(text, llm_chat)
    return {'method': 'llm', 'events': events, 'notes': notes}
