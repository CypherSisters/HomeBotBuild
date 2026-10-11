"""The box's clock: one server-side scheduler that fires what apps schedule.

Without a server-side scheduler, the same hole shows from two directions.
AlarmFlow firing lives only in the browser and runs only while that particular
screen is mounted, so an alarm due while the kiosk shows FreshTable rings
nowhere and stays ``status: "running"`` forever. Calendar accepts "remind me
to take the bins out on Monday", writes the row, and then nothing on the box
looks at it again. An appliance that takes a time from you and does not keep
it is not an appliance.

So the clock is not AlarmFlow's, and not Calendar's: it is the box's. One
daemon thread — the Device Sentinel / Majordomo-dispatcher pattern — walks a
list of **sources**, and every app that has due things declares one. Three
ship today, and they are separate on purpose: their rows live in different
arrays, carry different fields, and — for medications — obey a different rule
about what may be said out loud.

1. :class:`puffpai.alarm_scheduler.AlarmFlowSource` — alarms and timers.
2. :class:`CalendarSource` — pending tasks ("remind me to X on Monday") and
   events that explicitly opt into a reminder.
3. :class:`MedicationSource` — ``meds[]``, whose rows are ``kind:
   "medication"`` and may be ``vaulted``. A vaulted row is announced and
   logged by its ``label`` and NEVER by the drug name.

A source is any object with these five methods (duck-typed, no ABC needed):

``name``
    Short identifier, used in logs and in the occurrence token.
``refresh()``
    Read the app's document once per tick, into the source.
``occurrences()``
    Everything currently armed, as dicts:
    ``{"id", "due_ms", "sentence", "kind", "item"}``. ``id`` is stable per
    OCCURRENCE-bearing row; ``due_ms`` is the wall-clock instant it is due;
    ``sentence`` is what gets said; ``kind`` is a log label ("alarm", "timer",
    "reminder"). Return future occurrences too — the engine ignores them.
``mark(occ, status, iso, ms)``
    Record ``fired``/``missed`` on the underlying row, in memory.
``sweep(now_ms, ring_timeout_ms)``
    Resolve rows that have been ringing unattended for too long (an alarm rolls
    to its next occurrence, a one-shot stops). May be a no-op.
``flush()``
    Persist, once, if anything changed.

The engine owns the parts that must NOT be re-decided per app:

* **fired vs missed.** Late by less than :data:`GRACE_SECONDS`, or late while
  we were demonstrably up, means *ring now*. Late by more than the grace while
  ``last_checked`` shows we were absent means *missed* — recorded with a
  timestamp and announced once ("You missed an alarm at 7:00 AM"), because a
  silent alarm that leaves no trace is worse than a silent alarm.
* **At-most-once.** Occurrence tokens (``source:id@due_ms``) are persisted, and
  the app-data write happens BEFORE the announcement — the dispatcher's
  doctrine: losing one announcement to a crash beats replaying a 3 a.m. alarm
  on restart. The token carries the due time, so a snoozed alarm, tomorrow's
  daily and next week's recurring event are all *new* occurrences and ring
  normally, while a restart re-reading the same occurrence stays quiet.
* **The voice.** One announce path — :mod:`puffpai.announcer` — which the voice
  service drains under the duck policy (an announcement is an *addressed*
  event: duck, speak, restore). The API process cannot reach PulseAudio, so it
  never tries; see :func:`puffpai.voice.service.drain_announcements`.

Clock, announce sink and sources are all injected, so the entire policy is
testable without a thread, a speaker or a wall clock.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

from puffpai.logging_config import get_logger
from puffpai.statedir import resolve_runtime_dir

logger = get_logger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

STATE_FILENAME = "scheduler.json"

#: How often the thread wakes. Minute-granularity alarms need seconds-level
#: resolution to sound on time; a tick is one JSON read per source.
DEFAULT_TICK_SECONDS = 5

#: How late an occurrence may be and still count as "ringing now" rather than
#: "missed while we were off". Covers a slow tick, an NTP step, and a restart
#: that lands inside the deploy window.
GRACE_SECONDS = 120

#: How long a fired/missed row keeps its ring state before the source resolves
#: it (repeating alarm → next occurrence, one-shot → stopped).
RING_TIMEOUT_SECONDS = 300

#: Occurrence tokens kept across ticks, so an app-data write that failed cannot
#: re-announce the same due time on the next pass.
_RECENT_TOKENS_MAX = 120

#: How far back of the LAST TICK an occurrence may lie and still be allowed to
#: ring, or to be announced as missed. Older than this is history, not news.
MISSED_WINDOW_SECONDS = 24 * 3600

#: First-seen watermarks kept in the state file, for rows that carry no
#: ``created_at`` of their own. Pruned to the currently-armed items when it
#: grows past this.
_FIRST_SEEN_MAX = 400

#: Fields a row may carry its creation time in, in order of preference.
_CREATED_FIELDS = ("created_ms", "created_ts", "created_at", "createdAt",
                   "created")

STATUS_FIRED = "fired"
STATUS_MISSED = "missed"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat()


def created_ms(row) -> "int | None":
    """Epoch-ms at which *row* was created, or None if it does not say.

    A row that knows when it was written is the only honest answer to "was
    this occurrence ever ours to ring?". Accepts an ISO stamp (what the
    calendar and AlarmFlow write) or a bare number in seconds or milliseconds.
    """
    if not isinstance(row, dict):
        return None
    for key in _CREATED_FIELDS:
        value = row.get(key)
        if value is None or value == "" or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            number = float(value)
            if number <= 0:
                continue
            return int(number if number > 1e11 else number * 1000)
        when = parse_when(value, date_only_hour=0)
        if when is not None:
            return int(when.timestamp() * 1000)
    return None


def occurrence_item_key(source_name, occ) -> str:
    """Stable key for the ITEM behind an occurrence.

    Deliberately not the occurrence id: ``exec-calendar``'s feed makes that
    ``"<row id>@<ISO when>"``, unique per occurrence, so keying a first-seen
    watermark on it would re-stamp the same medication every single day and
    the watermark would never mean anything.
    """
    row = occ.get("item")
    ident = str(row.get("id") or "") if isinstance(row, dict) else ""
    if not ident:
        ident = str(occ.get("id") or "").split("@", 1)[0]
    return f"{source_name}:{ident}"


def missed_sentence(occurrences: list) -> str:
    """One sentence for everything that came due while the service was off."""
    # De-duplicated, order kept: a daily medication that was missed three times
    # over a long outage is "you missed your morning pills", not the same three
    # words read out three times.
    labels = []
    for occ in occurrences:
        label = str(occ.get("label") or occ.get("sentence") or "")
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return ""
    if len(labels) == 1:
        kind = occurrences[0].get("kind") or "alarm"
        article = "an" if kind[:1].lower() in "aeiou" else "a"
        return f"You missed {article} {kind} at {labels[0]}."
    joined = ", ".join(labels[:-1]) + f" and {labels[-1]}"
    return f"You missed {len(labels)} things while I was off: {joined}."


class Scheduler:
    """Daemon thread that fires every source's due occurrences."""

    def __init__(self, sources=None, *, announce=None,
                 tick_seconds: int = DEFAULT_TICK_SECONDS,
                 grace_seconds: int = GRACE_SECONDS,
                 ring_timeout_seconds: int = RING_TIMEOUT_SECONDS,
                 missed_window_seconds: int = MISSED_WINDOW_SECONDS,
                 clock=None, state_path: str | None = None):
        self.sources = list(sources) if sources is not None else None
        self._announce = announce
        self.tick_seconds = max(1, int(tick_seconds))
        self.grace_seconds = max(0, int(grace_seconds))
        self.ring_timeout_seconds = max(1, int(ring_timeout_seconds))
        self.missed_window_seconds = max(0, int(missed_window_seconds))
        self._now = clock or time.time
        self._state_path = state_path
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state: dict | None = None

    # ── collaborators ──────────────────────────────────────────────────────
    def _get_sources(self) -> list:
        if self.sources is None:
            self.sources = build_default_sources()
        return self.sources

    def _say(self, text: str) -> None:
        if not text:
            return
        if self._announce is not None:
            try:
                self._announce(text)
            except Exception as exc:
                logger.warning("scheduler: announce sink failed: %s", exc)
            return
        try:
            from puffpai import announcer
            announcer.announce(text, source="scheduler")
        except Exception as exc:
            logger.warning("scheduler: announce failed: %s", exc)

    # ── persisted cursor ───────────────────────────────────────────────────
    def state_path(self) -> str:
        if self._state_path is None:
            self._state_path = os.path.join(
                resolve_runtime_dir(_DEFAULT_DATA_DIR), STATE_FILENAME)
        return self._state_path

    def _load_state(self) -> dict:
        if self._state is not None:
            return self._state
        state = {"last_checked": None, "recent_tokens": [], "first_seen": {}}
        try:
            path = self.state_path()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    stored = json.load(fh)
                if isinstance(stored, dict):
                    state.update(stored)
        except Exception as exc:
            logger.debug("scheduler: state unreadable, starting fresh: %s", exc)
        if not isinstance(state.get("recent_tokens"), list):
            state["recent_tokens"] = []
        if not isinstance(state.get("first_seen"), dict):
            state["first_seen"] = {}
        self._state = state
        return state

    def _save_state(self) -> None:
        state = self._load_state()
        state["recent_tokens"] = list(state["recent_tokens"])[-_RECENT_TOKENS_MAX:]
        try:
            path = self.state_path()
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("scheduler: could not persist state: %s", exc)

    # ── the policy ─────────────────────────────────────────────────────────
    def tick(self) -> dict:
        """One pass over every source. Returns what it did."""
        now = self._now()
        now_ms = int(now * 1000)
        stamp = iso(now)
        state = self._load_state()
        last_checked = state.get("last_checked")
        recent = list(state.get("recent_tokens") or [])
        # "Missed" is only meaningful if we were actually absent: a merely slow
        # tick, or a clock nudge, must not invent one.
        was_absent = (last_checked is None
                      or (now - float(last_checked)) > self.grace_seconds)
        # An occurrence older than this was not ours even if nothing ever fired
        # it: the box was demonstrably already past it at its last tick.
        window_floor_ms = (
            int(float(last_checked) * 1000) - self.missed_window_seconds * 1000
            if last_checked is not None else None)
        first_seen = dict(state.get("first_seen") or {})
        seen_keys = set()

        fired: list[dict] = []
        missed: list[dict] = []
        silent: list[dict] = []
        resolved: list[dict] = []

        for source in self._get_sources():
            try:
                source.refresh()
            except Exception as exc:
                logger.warning("scheduler: source %s could not be read: %s",
                               getattr(source, "name", source), exc)
                continue
            dirty = False
            try:
                resolved_here = source.sweep(now_ms, self.ring_timeout_seconds * 1000)
            except Exception as exc:
                logger.warning("scheduler: sweep failed for %s: %s",
                               getattr(source, "name", "?"), exc)
                resolved_here = []
            if resolved_here:
                resolved.extend(resolved_here)
                dirty = True

            try:
                occurrences = source.occurrences()
            except Exception as exc:
                logger.warning("scheduler: source %s could not be listed: %s",
                               getattr(source, "name", "?"), exc)
                occurrences = []

            source_name = getattr(source, "name", "?")
            for occ in occurrences:
                due = occ.get("due_ms")
                if not due:
                    continue
                # The watermark is taken for EVERY armed occurrence, future
                # ones included: knowing when we first saw an item is what
                # still lets a genuine outage be reported as missed later.
                key = occurrence_item_key(source_name, occ)
                seen_keys.add(key)
                born = created_ms(occ.get("item"))
                if born is None:
                    if key in first_seen:
                        try:
                            born = int(first_seen[key])
                        except (TypeError, ValueError):
                            born = now_ms
                    else:
                        # Never seen before. If we were up, the item is
                        # genuinely new and whatever it resolves to in the past
                        # is not ours to announce; if we were down, the outage
                        # gets the benefit of the doubt.
                        born = (int(float(last_checked) * 1000)
                                if (was_absent and last_checked is not None)
                                else now_ms)
                    first_seen[key] = born
                if due > now_ms:
                    continue
                token = f"{source_name}:{occ.get('id')}@{int(due)}"
                if token in recent:
                    continue
                late = (now_ms - int(due)) / 1000.0
                quiet = False
                if late <= self.grace_seconds:
                    # The ordinary case: a tick lands a few seconds after the
                    # due time. Nothing below may make this one quiet.
                    status = STATUS_FIRED
                else:
                    floor = born
                    if window_floor_ms is not None:
                        floor = max(floor, window_floor_ms)
                    if int(due) < floor:
                        # A medication added at 01:12 with an 08:00 daily
                        # schedule resolves to YESTERDAY 08:00, and must not
                        # make the box say "time for your morning pills" in
                        # the dark. An occurrence that came
                        # due before the item was known is recorded and
                        # logged, and never spoken.
                        status, quiet = STATUS_MISSED, True
                    else:
                        status = (STATUS_MISSED if was_absent
                                  else STATUS_FIRED)
                try:
                    source.mark(occ, status, stamp, now_ms)
                except Exception as exc:
                    logger.warning("scheduler: could not mark %s: %s", token, exc)
                    continue
                occ["status"] = status
                if quiet:
                    occ["announced"] = False
                    silent.append(occ)
                    logger.info(
                        "scheduler: %s (%s) came due before the item was "
                        "known; recorded as missed, not announced",
                        occ.get("id"), occ.get("kind"))
                else:
                    (missed if status == STATUS_MISSED else fired).append(occ)
                recent.append(token)
                dirty = True

            if dirty:
                # The write lands BEFORE any announcement — at-most-once beats
                # a duplicate 3 a.m. alarm on restart.
                try:
                    source.flush()
                except Exception as exc:
                    logger.error("scheduler: could not persist %s: %s",
                                 getattr(source, "name", "?"), exc)

        state["last_checked"] = now
        state["recent_tokens"] = recent[-_RECENT_TOKENS_MAX:]
        if len(first_seen) > _FIRST_SEEN_MAX:
            first_seen = {k: v for k, v in first_seen.items() if k in seen_keys}
        state["first_seen"] = first_seen
        self._save_state()

        for occ in fired:
            logger.info("scheduler: firing %s (%s)", occ.get("id"), occ.get("kind"))
            self._say(occ.get("sentence") or "")
        if missed:
            logger.info("scheduler: %d occurrence(s) missed while offline", len(missed))
            self._say(missed_sentence(missed))

        return {"fired": fired, "missed": missed, "resolved": resolved,
                "silent": silent}

    # ── thread plumbing ────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:      # a broken tick must not end the thread
                logger.warning("scheduler: tick failed: %s", exc)
            self._stop.wait(self.tick_seconds)

    def start(self) -> "Scheduler":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="puffpai-scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        state = self._load_state()
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "tick_seconds": self.tick_seconds,
            "grace_seconds": self.grace_seconds,
            "ring_timeout_seconds": self.ring_timeout_seconds,
            "last_checked": state.get("last_checked"),
            "sources": [getattr(s, "name", "?") for s in (self.sources or [])],
        }


# ══════════════════════════════════════════════════════════════════════════
#  Calendar source
# ══════════════════════════════════════════════════════════════════════════

#: How far back an occurrence may lie and still be worth reporting as missed.
#: Beyond this it is history, not news.
MISSED_LOOKBACK_SECONDS = 24 * 3600

#: A date-only due ("remind me on Monday") means the morning, matching
#: ``cal_add_task``'s own 09:00 convention for the mirror event.
DATE_ONLY_HOUR = 9

_RECURRENCE_STEPS = {"daily": 1, "weekdays": 1, "weekly": 7, "biweekly": 14}


def parse_when(value, *, date_only_hour: int = DATE_ONLY_HOUR):
    """Datetime named by an ISO-ish string, or None. Never raises."""
    if not value:
        return None
    text = str(value).strip()
    # Python 3.10's fromisoformat (the box venv) rejects a trailing Z, which
    # an imported calendar can carry.
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        when = datetime.fromisoformat(iso_text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                when = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    if "T" not in text and " " not in text:
        when = when.replace(hour=date_only_hour, minute=0, second=0, microsecond=0)
    return when


def parse_time_of_day(value, now: datetime):
    """"8:00 AM" (a medication's ``next``) as today's instant, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            parsed = datetime.strptime(text.upper(), fmt)
        except ValueError:
            continue
        return now.replace(hour=parsed.hour, minute=parsed.minute,
                           second=0, microsecond=0)
    return parse_when(text)


def next_occurrence(base: datetime, recurrence, after: datetime):
    """First occurrence of *base* under *recurrence* that is > *after*.

    Deliberately small: the recurrence vocabulary is ``cal_add_event``'s own
    (daily / weekdays / weekly / biweekly / monthly / yearly) and nothing here
    invents a wider one. ``exec-calendar`` owns a richer expansion; when
    :meth:`AppData.cal_expand_occurrences` lands, :class:`CalendarSource` uses
    it and this becomes the fallback.
    """
    rec = (recurrence or "").lower()
    if not rec or base > after:
        return base if base > after else None
    # A weekday-set recurrence ("custom:tue,thu") steps a day at a time and
    # keeps only the days it names. Without this the legacy derivation — the
    # fallback for a box whose AppData predates cal_schedulable_reminders —
    # would silently fire nothing at all for such a row.
    try:
        from puffpai.calendar_dates import custom_days as _custom_days
        wanted = _custom_days(rec)
    except Exception:
        wanted = None
    step = 1 if wanted else _RECURRENCE_STEPS.get(rec)
    when = base
    for _ in range(800):
        if step:
            when = when + timedelta(days=step)
            if rec == "weekdays" and when.weekday() >= 5:
                continue
            if wanted and when.weekday() not in wanted:
                continue
        elif rec == "monthly":
            month = when.month + 1
            year = when.year + (month > 12)
            month = 1 if month > 12 else month
            day = min(when.day, [31, 29 if year % 4 == 0 and (year % 100 or not year % 400)
                                 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
            when = when.replace(year=year, month=month, day=day)
        elif rec == "yearly":
            try:
                when = when.replace(year=when.year + 1)
            except ValueError:              # Feb 29 on a non-leap year
                when = when.replace(year=when.year + 1, day=28)
        else:
            return None
        if when > after:
            return when
    return None


def _pick_occurrence(found, now: datetime, lookback_seconds: float):
    """From a list of expanded occurrences, the one to act on now.

    The most recent one at or before *now* that is still recent enough to
    matter, else the next future one — the same rule as
    :func:`latest_or_next`, applied to somebody else's expansion.
    """
    horizon = now - timedelta(seconds=lookback_seconds)
    stamps = []
    for entry in found:
        when = entry if isinstance(entry, datetime) else parse_when(entry)
        if when is not None:
            stamps.append(when)
    if not stamps:
        return None
    stamps.sort()
    past = [w for w in stamps if w <= now and w >= horizon]
    if past:
        return past[-1]
    future = [w for w in stamps if w > now]
    return future[0] if future else None


def latest_or_next(base: datetime, recurrence, now: datetime,
                   lookback_seconds: float = MISSED_LOOKBACK_SECONDS):
    """The occurrence the scheduler should be looking at right now.

    The most recent one at or before *now* if it is recent enough to still
    matter, otherwise the next future one. This is what lets a weekly reminder
    that came due while the box was off be reported as missed exactly once,
    while a weekly reminder created months ago does not shout about every
    occurrence it never saw.
    """
    horizon = now - timedelta(seconds=lookback_seconds)
    candidate = base
    if candidate > now:
        return candidate
    if not recurrence:
        return candidate if candidate >= horizon else None
    seen = candidate
    for _ in range(800):
        nxt = next_occurrence(seen, recurrence, seen)
        if nxt is None:
            break
        if nxt > now:
            return seen if seen >= horizon else nxt
        seen = nxt
    return seen if seen >= horizon else None


#: Rows flagged ``vaulted`` name something the room must not hear (calendar
#: medication reminders may be for vaulted medications). Such a row is announced -- and logged -- by its ``label``
#: only, never by its title. If it carries no label, this generic one is used,
#: which is still a useful reminder and still says nothing about what the
#: medication is.
VAULTED_FALLBACK_LABEL = "medication"


def safe_label(row: dict, default: str = "reminder") -> str:
    """What this row may be called out loud, and in the logs.

    An explicit ``label`` always wins. For a ``vaulted`` row it is the ONLY
    thing that can be used: the title is a drug name, the announcement is
    audible to whoever is in the room, and the activity feed is readable by
    every dashboard client -- so the title must not reach either.
    """
    label = str(row.get("label") or "").strip()
    if label:
        return label
    if row.get("vaulted"):
        return default if default != "reminder" else VAULTED_FALLBACK_LABEL
    for key in ("title", "name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return default


def spoken_clock(when: datetime) -> str:
    h12 = when.hour % 12 or 12
    half = "AM" if when.hour < 12 else "PM"
    if when.minute == 0:
        return f"{h12} {half}"
    return f"{h12}:{when.minute:02d} {half}"


class CalendarSource:
    """Calendar reminders: pending tasks, medications, and events that opt in.

    "remind me to X on Monday" writes a task row (and a mirror event) that
    nothing else fires. Tasks are therefore scheduled by their ``due``,
    and medications by their ``next`` time under their ``freq``.

    Events are NOT scheduled wholesale, on purpose. A family calendar full of
    school runs and work meetings would turn the box into a PA system, and
    nobody asked for that; an event fires only when it carries an explicit
    ``remind_at`` (ISO) or ``remind`` (lead minutes before ``start``). The
    task-mirror events ``cal_add_task`` writes (``src == "task"``) are skipped
    so a reminder is never announced twice.

    Marks go on ``reminder_status`` / ``reminded_at``, never on a task's
    ``status`` -- pending/done is the family's field, not ours -- and
    ``reminder_occurrence_ms`` records WHICH occurrence was handled, so a
    daily medication is armed again tomorrow.
    """

    name = "calendar"

    def __init__(self, app_data=None, *, clock=None,
                 lookback_seconds: float = MISSED_LOOKBACK_SECONDS):
        self._app_data = app_data
        self._now = clock or time.time
        self.lookback_seconds = lookback_seconds
        self._doc: dict | None = None
        self._dirty = False

    def _data(self):
        if self._app_data is None:
            self._app_data = _shared_app_data()
        return self._app_data

    def refresh(self) -> None:
        self._doc = self._data().get_data("calendar") or {}
        self._dirty = False

    #: (occurrence kind, document key) this source is responsible for.
    ROWS = (("task", "tasks"), ("event", "events"))

    def _rows(self):
        doc = self._doc or {}
        for kind, key in self.ROWS:
            for row in (doc.get(key) or []):
                if isinstance(row, dict):
                    yield kind, row

    def _expand(self, base, recurrence, now, row=None):
        """The occurrence to act on: exec-calendar's expansion if the box has it.

        ``exec-calendar`` owns the calendar's date parsing and recurrence and
        is exposing ``expanded_occurrences``; when it lands, it wins, and the
        local :func:`latest_or_next` becomes the fallback for a box (or a
        test) that does not have it yet. Either name is accepted so the two
        branches can land in either order.
        """
        for attr in ("cal_expand_occurrences",):
            helper = getattr(self._data(), attr, None)
            if not callable(helper):
                continue
            try:
                found = helper(base, recurrence, now)
            except Exception as exc:
                logger.debug("scheduler: %s unusable: %s", attr, exc)
                continue
            if isinstance(found, (list, tuple)):
                found = _pick_occurrence(found, now, self.lookback_seconds)
            if found:
                return found
        return latest_or_next(base, recurrence, now, self.lookback_seconds)

    def _base_and_recurrence(self, kind, row, now):
        """(first occurrence, recurrence) for a row, or (None, None)."""
        if kind == "task":
            if (row.get("status") or "pending") != "pending":
                return None, None
            return parse_when(row.get("remind_at") or row.get("due")), None
        if kind == "med":
            if (row.get("status") or "due") == "skipped":
                return None, None
            when = parse_when(row.get("remind_at"))
            if when is None:
                when = parse_time_of_day(row.get("next"), now)
            freq = str(row.get("freq") or "daily").lower()
            known = freq in _RECURRENCE_STEPS or freq in ("monthly", "yearly")
            return when, (freq if known else "daily")
        if row.get("src") == "task":
            return None, None            # the mirror of a task we already fire
        base = parse_when(row.get("remind_at"))
        if base is None:
            lead, start = row.get("remind"), parse_when(row.get("start"))
            if lead is None or start is None:
                return None, None
            try:
                base = start - timedelta(minutes=float(lead))
            except (TypeError, ValueError):
                return None, None
        return base, row.get("rec")

    #: Occurrence kinds this source owns in ``cal_schedulable_reminders``.
    KINDS = ("event", "task", "bill")

    def _reminder_rows(self):
        """The calendar's occurrence feed, or None if the box lacks it.

        ``AppData.cal_schedulable_reminders(from_dt, to_dt)`` returns one row per OCCURRENCE, already
        expanded for recurrence, in naive LOCAL wall-clock — which is what the
        calendar store holds, so nothing here converts. It
        is strictly better than deriving occurrences from raw rows, and it is
        the only place a medication's schedule now lives, so it wins whenever
        it exists; the local derivation below stays as the fallback for a box
        (or a test) that predates it.

        Its ``display`` is the ONLY field that may be spoken, shown or logged
        — for a vaulted medication the real name is not in the payload at all,
        and must not be fetched from anywhere else.
        """
        helper = getattr(self._data(), "cal_schedulable_reminders", None)
        if not callable(helper):
            return None
        now = datetime.fromtimestamp(self._now())
        try:
            rows = helper(now - timedelta(seconds=self.lookback_seconds),
                          now + timedelta(days=1))
        except Exception as exc:
            logger.warning("scheduler: cal_schedulable_reminders failed: %s", exc)
            return None
        if rows is None:
            return None
        out = []
        for row in rows:
            if not isinstance(row, dict) or row.get("kind") not in self.KINDS:
                continue
            when = row.get("when")
            if not isinstance(when, datetime):
                when = parse_when(when)
            display = str(row.get("display") or "").strip()
            if when is None or not display:
                continue
            medication = row.get("kind") == "medication"
            out.append({
                # occurrence_id is "<row id>@<ISO
                # when>" — already unique per occurrence, so the engine's token
                # is stable without rebuilding one. `id` is the STORED ROW id
                # and is what a mark resolves through, never the dedupe key.
                "id": str(row.get("occurrence_id") or row.get("id")
                          or row.get("source_id") or display),
                "due_ms": int(when.timestamp() * 1000),
                "kind": "medication" if medication else "reminder",
                "label": display,
                "sentence": (f"It's {spoken_clock(when)} -- time for your "
                             f"{display}." if medication
                             else f"Reminder: {display}."),
                "item": self._stored_row(row),
            })
        return out

    def _stored_row(self, row) -> dict:
        """The stored row an occurrence came from, so a mark has somewhere to go.

        Best-effort: when the id resolves to nothing (a derived occurrence with
        no row of its own), the engine's persisted occurrence token is still
        the at-most-once guarantee — the mark is a convenience for the UI, not
        the correctness mechanism.
        """
        wanted = {str(row.get("source_id") or ""), str(row.get("id") or "")}
        wanted.discard("")
        doc = self._doc or {}
        for key in ("tasks", "meds", "events", "bills"):
            for stored in (doc.get(key) or []):
                if isinstance(stored, dict) and str(stored.get("id")) in wanted:
                    return stored
        return {}

    def occurrences(self) -> list:
        from_feed = self._reminder_rows()
        if from_feed is not None:
            return [o for o in from_feed
                    if not self._already_handled(o["item"], o["due_ms"])]
        now = datetime.fromtimestamp(self._now())
        out = []
        for kind, row in self._rows():
            base, recurrence = self._base_and_recurrence(kind, row, now)
            if base is None:
                continue
            when = self._expand(base, recurrence, now, row)
            if when is None:
                continue
            due = int(when.timestamp() * 1000)
            if self._already_handled(row, due):
                continue
            default = VAULTED_FALLBACK_LABEL if kind == "med" else "reminder"
            label = safe_label(row, default)
            if kind == "med":
                sentence = f"It's {spoken_clock(when)} -- time for your {label}."
            else:
                sentence = f"Reminder: {label}."
            out.append({
                "id": str(row.get("id") or label),
                "due_ms": due,
                "kind": "medication" if kind == "med" else "reminder",
                "label": label,
                "sentence": sentence,
                "item": row,
            })
        return out

    @staticmethod
    def _already_handled(row, due_ms) -> bool:
        """Has this exact occurrence already fired?

        Survives token eviction, and a recurring row's NEXT occurrence carries
        a different stamp, so it stays armed.
        """
        try:
            return int((row or {}).get("reminder_occurrence_ms") or 0) == due_ms
        except (TypeError, ValueError):
            return False

    def mark(self, occ, status, when_iso, when_ms) -> None:
        row = occ.get("item")
        if not row:
            # A derived occurrence with no stored row: the persisted token is
            # the at-most-once guarantee, so there is nothing to write.
            return
        row["reminder_status"] = status
        row["reminded_at" if status == STATUS_FIRED else "missed_at"] = when_iso
        row["reminder_ts"] = when_ms
        row["reminder_occurrence_ms"] = occ.get("due_ms")
        self._dirty = True

    def sweep(self, now_ms, ring_timeout_ms) -> list:
        """Nothing to sweep: a reminder is spoken once and does not ring on."""
        return []

    def flush(self) -> None:
        if self._dirty and self._doc is not None:
            self._data().set_data("calendar", self._doc)
        self._dirty = False


class MedicationSource(CalendarSource):
    """Calendar medications — the same clock, a stricter tongue.

    Meds live in ``meds[]``, not ``tasks[]``, and are stored as bare
    wall-clock strings ("8:00 AM") with a ``freq``, so nothing that reads
    tasks could fire them. They get their own source because their shape is
    their own and because of the vault rule: a medication reminder may be
    for a VAULTED medication, and the room must hear the label ("time for your
    morning pills"), never the drug name. The name never reaches the
    announcement, and never reaches a log line either — the engine logs the
    row id and the kind, and this source's occurrences carry only the label.
    """

    name = "medications"
    ROWS = (("med", "meds"),)
    KINDS = ("medication",)


def _shared_app_data():
    """The SAME AppData the app REST routes use, when we are in-process.

    ``sys.modules`` lookup, never an import (announcer's pattern), so this
    module stays importable in tests and tools without dragging the server up.
    """
    import sys
    api = sys.modules.get("puffpai.api_server")
    getter = getattr(api, "_get_app_data", None) if api else None
    if callable(getter):
        try:
            shared = getter()
            if shared is not None:
                return shared
        except Exception as exc:
            logger.debug("scheduler: shared AppData unavailable: %s", exc)
    from puffpai.app_data import AppData
    return AppData()


def build_default_sources() -> list:
    """Every app that schedules things. New apps append one line here."""
    from puffpai.alarm_scheduler import AlarmFlowSource
    shared = _shared_app_data()
    return [AlarmFlowSource(shared), CalendarSource(shared),
            MedicationSource(shared)]


_scheduler: Scheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> Scheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = Scheduler()
        return _scheduler


def start_scheduler() -> Scheduler | None:
    """Start the singleton. Returns None when disabled by the environment."""
    if os.environ.get("PUFFPAI_SCHEDULER", "1") == "0":
        return None
    return get_scheduler().start()


def reset_for_tests() -> None:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.stop()
        _scheduler = None
