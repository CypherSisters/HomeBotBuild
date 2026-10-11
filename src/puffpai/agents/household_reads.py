"""Answer household questions from the REAL stores, or decline.

Why this module exists
----------------------
The Validate pass caught one failure shape over and over: a household question
("did I take my medication today", "do we owe any money on bills", "any events
coming up", "what's the status of the house") reached a layer that had no
reader for it, and something downstream — the Concierge's LLM intent
refinement, or RAG — produced a confident answer about the wrong subject
entirely. Medication was answered with the Jellyfin/Calibre URLs; bills with a
list of Home Assistant entity names; the calendar with all 111 devices; the
house status with invented thermostat and leak-sensor readings.

Every one of those questions has a real answer sitting in ``app_data`` or in
Home Assistant. This module reads THOSE, and returns a plain "nothing recorded
yet" when the store is empty. It never guesses and it never calls a model.

Discipline, inherited from ``_CAPREAD_RULES`` in ``api_server``
--------------------------------------------------------------
* One narrow, anchored trigger per reader, plus an exclude that vetoes the
  write/action readings of the same words. First match wins; the table is
  ordered narrow-before-broad.
* A reader that cannot reach its store returns an honest sentence, never
  ``None`` — falling through is what handed these turns to the classifier that
  produced the misroute in the first place.
* Zero-argument READS only. Nothing here writes. The one write this work needed
  (setting an alarm) lives in its own fastpath in ``api_server``, where the
  write-intent guard already lives.

Injection
---------
Every reader takes its store as an optional argument so the tests can hand it a
fixture. The lazy defaults are resolved inside the call, never at import: this
module is imported from ``api_server`` at request time and must not drag the
config/secrets graph into the import cycle (see :mod:`puffpai.statedir`).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from puffpai import calendar_dates as _caldates

logger = logging.getLogger(__name__)

__all__ = ["answer", "household_members", "house_status", "lights_on",
           "room_temperature",
           "vault_status", "medication_status", "bills_status",
           "calendar_upcoming", "meal_today"]


def _re(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


# ── store accessors (lazy, injectable) ───────────────────────────────────────

def _app_data(app=None):
    if app is not None:
        return app
    # Prefer the process-wide instance: its per-app locks are what serialize a
    # read against a concurrent write. A fresh AppData() would carry its own
    # locks and serialize against nothing.
    import sys
    api = sys.modules.get("puffpai.api_server")
    if api is not None and hasattr(api, "_get_app_data"):
        shared = api._get_app_data()
        if shared is not None:
            return shared
    from puffpai.app_data import AppData
    return AppData()


def _ha_bridge(bridge=None):
    if bridge is not None:
        return bridge
    import sys
    api = sys.modules.get("puffpai.api_server")
    if api is not None and hasattr(api, "_get_assistant_tools"):
        return api._get_assistant_tools()._ha_bridge
    from puffpai.ha_bridge import HomeAssistantBridge
    return HomeAssistantBridge()


def _vault(vault=None):
    if vault is not None:
        return vault
    import sys
    api = sys.modules.get("puffpai.api_server")
    if api is not None and hasattr(api, "_get_vault"):
        v = api._get_vault()
        if v is not None:
            return v
    from puffpai.security.vault import Vault
    return Vault()


# ── house status ─────────────────────────────────────────────────────────────

#: Binary-sensor device classes worth reporting unprompted in a house summary.
#: Deliberately short: a "how's the house" answer that lists 111 entities is
#: not an answer.
_LEAK_CLASSES = frozenset({"moisture", "gas", "smoke", "co", "problem"})
_OPENING_CLASSES = frozenset({"door", "garage_door", "window", "opening"})

_UNKNOWN_STATES = frozenset({"unknown", "unavailable", "none", ""})


def house_status(bridge=None) -> str:
    """A real read of the house: lights, thermostats, and safety sensors.

    Every number here comes from ``get_all_states()``. When Home Assistant is
    unreachable this says so — it does not describe a house it cannot see.
    """
    try:
        states = _ha_bridge(bridge).get_all_states()
    except Exception as exc:
        logger.debug("house status: HA unreachable: %s", exc)
        return ("I can't reach Home Assistant right now, so I can't tell you "
                "the state of the house. Nothing below is a guess — I simply "
                "have no live reading.")
    if not states:
        return ("Home Assistant answered but reported no entities, so I have "
                "nothing to read the house from right now.")

    lights_on = lights_total = 0
    thermostats = []
    tripped = []
    open_now = []
    leak_sensors = opening_sensors = 0

    for raw in states:
        entity_id = str(raw.get("entity_id") or "")
        domain = entity_id.split(".", 1)[0]
        state = str(raw.get("state") or "").lower()
        attrs = raw.get("attributes") or {}
        name = attrs.get("friendly_name") or entity_id

        if domain == "light":
            lights_total += 1
            if state == "on":
                lights_on += 1
        elif domain == "climate":
            if state in _UNKNOWN_STATES:
                continue
            target = attrs.get("temperature")
            current = attrs.get("current_temperature")
            bits = [f"{name}: {state}"]
            if target is not None:
                bits.append(f"set to {target}°")
            if current is not None:
                bits.append(f"reading {current}°")
            thermostats.append(", ".join(bits))
        elif domain == "binary_sensor":
            dev_class = str(attrs.get("device_class") or "").lower()
            if dev_class in _LEAK_CLASSES:
                leak_sensors += 1
                if state == "on":
                    tripped.append(name)
            elif dev_class in _OPENING_CLASSES:
                opening_sensors += 1
                if state == "on":
                    open_now.append(name)

    parts = []
    if lights_total:
        parts.append(f"{lights_on} of {lights_total} lights are on")
    else:
        parts.append("no light entities are reporting")

    if thermostats:
        parts.append("thermostats — " + "; ".join(thermostats))
    else:
        parts.append("no thermostat is reporting")

    if leak_sensors:
        if tripped:
            safety = "ALERT — tripped right now: " + ", ".join(tripped)
        else:
            safety = "no leak, smoke or gas sensor has tripped"
        parts.append(f"{safety} ({leak_sensors} monitored)")
    else:
        parts.append("no leak, smoke or gas sensors are set up")

    if opening_sensors:
        if open_now:
            parts.append(f"open right now: {', '.join(open_now)}")
        else:
            parts.append(f"all {opening_sensors} doors and windows are closed")

    return "House status — " + "; ".join(parts) + "."


def lights_on(bridge=None) -> str:
    """Name the lights that are on right now, from live Home Assistant state.

    "which lights are on" reached no reader at all and fell through to the
    legacy agent, whose model answered 3/3 that it has "no way to read the
    current on/off status of your lights" while ``/ha/entities`` was returning
    210 live states. ``house_status`` already counts lights, but a question
    that asks WHICH ones wants their names, and a count is not responsive to
    it. Same store and the same honesty rules as ``house_status``: an
    unreachable bridge says so, and "none are on" is a real answer.
    """
    try:
        states = _ha_bridge(bridge).get_all_states()
    except Exception as exc:
        logger.debug("lights on: HA unreachable: %s", exc)
        return ("I can't reach Home Assistant right now, so I can't tell you "
                "which lights are on. That's a missing reading, not an empty "
                "house.")
    if not states:
        return ("Home Assistant answered but reported no entities, so I have "
                "no lights to read right now.")

    on_names = []
    total = 0
    unavailable = 0
    for raw in states:
        entity_id = str(raw.get("entity_id") or "")
        if entity_id.split(".", 1)[0] != "light":
            continue
        total += 1
        state = str(raw.get("state") or "").lower()
        attrs = raw.get("attributes") or {}
        if state in _UNKNOWN_STATES:
            unavailable += 1
            continue
        if state == "on":
            on_names.append(str(attrs.get("friendly_name") or entity_id))

    if not total:
        return ("No light entities are set up in Home Assistant yet, so "
                "there's nothing for me to read.")

    tail = ""
    if unavailable:
        tail = (f" ({unavailable} of {total} "
                f"{'is' if unavailable == 1 else 'are'} not reporting)")
    if not on_names:
        return f"No lights are on — all {total} are off{tail}."

    on_names.sort(key=str.lower)
    verb = "is" if len(on_names) == 1 else "are"
    return (f"{len(on_names)} of {total} lights {verb} on: "
            f"{', '.join(on_names)}{tail}.")


# ── room temperature ─────────────────────────────────────────────────────────
#
# "check the living room temperature, then ..." needs a reader of its own.
# ``_try_weather_answer`` deliberately declines a question that names a room
# (its ``_INDOOR_RE`` veto in api_server), ``house_status`` answers with a
# whole-house summary rather than the room asked about, and without this the
# clause would fall to RAG and be answered with a generic punt. This is the
# indoor half ``_INDOOR_RE`` assumes exists.

#: The rooms the indoor veto in ``api_server._INDOOR_RE`` already recognises,
#: plus the ones a household names out loud. Kept in one place so the trigger
#: and the entity matcher cannot drift apart.
_ROOM_NAMES = (
    "living room", "family room", "dining room", "bedroom", "bed room",
    "master bedroom", "kids room", "kid's room", "nursery", "kitchen",
    "bathroom", "garage", "hallway", "basement", "attic", "office",
    "den", "playroom", "laundry room", "sunroom", "porch",
)
_ROOM_ALT = "|".join(name.replace(" ", r"\s?") for name in _ROOM_NAMES)
_ROOM_NAMED_RE = _re(r"\b(" + _ROOM_ALT + r")\b")
_TEMP_WORD = r"(?:temperature|temp)\b|\bhow\s+(?:hot|cold|warm|chilly)\b"
#: An entity whose reading is the OUTDOORS. A room question must never be
#: answered from the weather station sensor - that is the exact confusion
#: ``_try_weather_answer`` was written to end, in the other direction.
_OUTDOOR_ENTITY_RE = _re(r"openweather|outdoor|outside|exterior|weather|"
                         r"forecast|yard|garden|roof|driveway")


def _room_named(text: str) -> "str | None":
    """The room this utterance names, normalised, or None for "in here"."""
    match_ = _ROOM_NAMED_RE.search(text or "")
    if not match_:
        return None
    return " ".join(match_.group(1).lower().split())


def _temperature_readings(states):
    """``(friendly_name, entity_id, value, unit)`` for every indoor reading."""
    out = []
    for raw in states or []:
        entity_id = str(raw.get("entity_id") or "")
        domain = entity_id.split(".", 1)[0]
        attrs = raw.get("attributes") or {}
        name = str(attrs.get("friendly_name") or entity_id)
        unit = str(attrs.get("unit_of_measurement") or "")
        value = None
        if domain == "sensor":
            device_class = str(attrs.get("device_class") or "").lower()
            if device_class != "temperature" and "temperature" not in entity_id:
                continue
            state = str(raw.get("state") or "").strip()
            if state.lower() in _UNKNOWN_STATES:
                continue
            try:
                float(state)
            except ValueError:
                continue
            value = state
        elif domain == "climate":
            current = attrs.get("current_temperature")
            if current is None:
                continue
            value = str(current)
            unit = unit or "°"
        else:
            continue
        out.append((name, entity_id, value, unit or "°"))
    return out


def _one_per_room(readings):
    """One reading for one room.

    Without it, "what's the temperature in the living room" is answered "The
    living room is — Living Room 75 degrees; Living Room Current Temperature
    75.2 degrees Fahrenheit.": the thermostat and its own companion sensor,
    the same fact twice, read out as a list. The THERMOSTAT's
    ``current_temperature`` is the room's temperature; the sensors that agree
    with it add nothing to a spoken answer.
    """
    climate = [r for r in readings if str(r[1]).startswith("climate.")]
    if climate:
        return climate[:1]
    seen = set()
    unique = []
    for reading in readings:
        try:
            key = round(float(reading[2]))
        except (TypeError, ValueError):
            key = str(reading[2])
        if key in seen:
            continue
        seen.add(key)
        unique.append(reading)
    return unique


def room_temperature(bridge=None, room=None) -> str:
    """Read the temperature of the room the question names, from live HA state.

    Honest by construction: an unreachable bridge says so, and a room with no
    temperature sensor is told it has no sensor rather than given a number from
    somewhere else in the house.
    """
    try:
        states = _ha_bridge(bridge).get_all_states()
    except Exception as exc:
        logger.debug("room temperature: HA unreachable: %s", exc)
        return ("I can't reach Home Assistant right now, so I can't read the "
                "temperature indoors. That's a missing reading, not a missing "
                "sensor.")
    if not states:
        return ("Home Assistant answered but reported no entities, so I have "
                "no temperature sensor to read right now.")

    readings = [r for r in _temperature_readings(states)
                if not _OUTDOOR_ENTITY_RE.search(r[0] + " " + r[1])]
    if not readings:
        return ("No indoor temperature sensor is reporting to Home Assistant, "
                "so there's nothing for me to read.")

    if room:
        squashed = room.replace(" ", "")
        scoped = [r for r in readings
                  if squashed in (r[0] + r[1]).lower().replace(" ", "")
                  .replace("_", "")]
        if not scoped:
            return (f"I don't have a temperature sensor reporting for the "
                    f"{room}, so I can't tell you what it reads. That's a "
                    f"missing sensor, not a missing reading.")
        readings = _one_per_room(scoped)
        lead = f"The {room} is"
    else:
        lead = "Indoors it reads"

    if len(readings) == 1:
        name, _eid, value, unit = readings[0]
        return f"{lead} {value}{unit} ({name})."
    bits = [f"{name} {value}{unit}" for name, _eid, value, unit in readings[:6]]
    tail = "" if len(readings) <= 6 else f" (+{len(readings) - 6} more)"
    return f"{lead} — " + "; ".join(bits) + f"{tail}."


# ── vault lock state ─────────────────────────────────────────────────────────

#: Bookkeeping files that are not user items. Mirrors the filter
#: ``assistant_tools._vault_list_stems`` applies, so the two counts agree.
_VAULT_HIDDEN_FILES = frozenset({"auto_detected_pii.json"})


def _vault_entry_count(vault) -> int:
    directory = getattr(vault, "_vault_dir", None)
    if directory is None:
        return 0
    try:
        if not directory.exists():
            return 0
        return sum(
            1 for f in directory.iterdir()
            if f.is_file() and not f.name.startswith(".")
            and not f.name.startswith("doc_")
            and f.name not in _VAULT_HIDDEN_FILES
        )
    except OSError:
        return 0


def vault_status(vault=None) -> str:
    """Locked or unlocked, plus how many items are protected — never WHAT.

    ``vault_list`` was the declared answer to "is the vault locked", and it
    cannot answer that question by construction: it lists labels. Worse, the
    labels ARE the sensitive part ("your router password, your bank PIN"), so
    the non-responsive answer also volunteered the inventory. This reader gives
    the lock state and a bare count and names nothing.
    """
    try:
        v = _vault(vault)
    except Exception as exc:
        logger.debug("vault status unavailable: %s", exc)
        return ("I can't reach your vault right now, so I can't tell you "
                "whether it's locked.")
    if v is None:
        return ("I can't reach your vault right now, so I can't tell you "
                "whether it's locked.")
    if not getattr(v, "is_initialized", False):
        return ("Your vault isn't set up yet — there's nothing to lock. "
                "You can create it with a PIN from the vault page on your "
                "dashboard.")
    unlocked = bool(getattr(v, "has_live_session", False))
    n = _vault_entry_count(v)
    items = (f"{n} protected item{'s' if n != 1 else ''}"
             if n else "no items stored yet")
    if unlocked:
        return (f"Your vault is UNLOCKED right now and holds {items}. "
                "It re-locks on its own when the session expires.")
    return (f"Your vault is LOCKED and holds {items}. "
            "I need your PIN to open anything in it.")


# ── medications ──────────────────────────────────────────────────────────────

def medication_status(app=None) -> str:
    """What the medication tracker actually holds. Empty is a real answer."""
    try:
        raw = _app_data(app).cal_list_meds()
    except Exception as exc:
        logger.debug("medication read failed: %s", exc)
        return ("I couldn't open your medication records just now, so I can't "
                "say whether anything was taken today.")
    if not raw or "No medications tracked" in raw:
        return ("Nothing recorded yet — there are no medications in your "
                "tracker, so I have no record of a dose today either way. "
                "Please don't treat that as confirmation that you did or "
                "didn't take one. Add a medication and I'll track it.")
    return ("Here's what your medication tracker holds. I can only report what "
            "has been logged:\n" + raw)


# ── shopping list ────────────────────────────────────────────────────────────

#: "how many items are on the list" asks for a NUMBER.
_COUNT_ONLY_RE = _re(r"\bhow\s+(?:many|much)\b|\bnumber\s+of\b|\bcount\b"
                     r"|\bhow\s+long\s+is\b")


def household_members(roster=None) -> str:
    """Who lives here — read from the ONE roster.

    Before this, "who is in my family" had no reader at all: it reached the
    model, which answered from the Calendar's four hardcoded placeholders
    (Dad, Mom, Alex, Emma) — four people who do not exist, named to a
    household of real ones.
    """
    try:
        from puffpai import household
        people = roster if roster is not None else household.members()
    except Exception as exc:
        logger.debug("household roster unavailable: %s", exc)
        return ("I can't reach the household roster right now, so I won't "
                "guess who's in it.")
    labelled = [f"{m.get('display_name')} ({m.get('role')})"
                for m in people if m.get('display_name')]
    if not labelled:
        return ("Nobody is set up in the household yet. Tell me to add "
                "someone and I'll create them.")
    if len(labelled) == 1:
        return f"Just you so far: {labelled[0]}."
    return ("There are %d people in your household: %s."
            % (len(labelled), ", ".join(labelled)))


def shopping_status(app=None, count_only=False) -> str:
    """HOW MANY items the shopping list holds — counted from the store.

    A count is the one thing a model will always produce and never know: asked
    "…then tell me how many items are on the list", a model can answer "There
    are five items listed." over a list of 31. The count is a
    read of FreshTable's ``shoppingList``, so it is either right or it is an
    honest miss.
    """
    try:
        items = _app_data(app).fresh_shopping_items() or []
    except Exception as exc:
        logger.debug("shopping read failed: %s", exc)
        return ("I couldn't open your shopping list just now, so I can't tell "
                "you how many items are on it.")
    count = len(items)
    if not count:
        return ("Your shopping list is empty — I read it just now and there "
                "are no items on it.")
    names = [str(i.get("name") or "").strip() for i in items]
    names = [n for n in names if n]
    unit = "item" if count == 1 else "items"
    if count_only:
        # "how many items are on the list" asks for a number, not the number
        # AND every name read out loud.
        return (f"There {'is' if count == 1 else 'are'} {count} {unit} on "
                f"your shopping list.")
    listed = ("" if not names
              else " (" + ", ".join(names[:12])
                   + (", …" if len(names) > 12 else "") + ")")
    return f"There {'is' if count == 1 else 'are'} {count} {unit} on your shopping list{listed}."


# ── bills ────────────────────────────────────────────────────────────────────

def bills_status(app=None) -> str:
    try:
        data = _app_data(app)
        pending = data.cal_list_bills("pending")
    except Exception as exc:
        logger.debug("bills read failed: %s", exc)
        return ("I couldn't open your bill records just now, so I can't tell "
                "you what's outstanding.")
    if not pending or "No pending bills" in pending:
        # Says what was searched. "Nothing recorded yet" alone reads as a shrug
        # and is exactly the shape a model rewrites into "I can't pull up your
        # bills", the miss has to name the store it just read.
        return ("No unpaid bills recorded — I read your bill records and "
                "nothing is pending, so as far as those records go nothing "
                "is owed. Add a bill and I'll track the amount and due date.")
    return pending


# ── calendar ─────────────────────────────────────────────────────────────────

def calendar_upcoming(app=None, days: int = 7, scope=None) -> str:
    """Upcoming events AND pending tasks/reminders, from the family calendar.

    ``scope`` is a window from :func:`puffpai.calendar_dates.day_scope` —
    "tomorrow", "this weekend", "Saturday". Without it every calendar
    question gets the same seven-day answer, so "what's on the calendar
    tomorrow" is byte-identical to "anything coming up". A scoped read
    answers about that window only and NAMES it, so an empty day says
    "Nothing on Saturday" rather than a bare "no events".

    A scoped read lists events only: every task ``cal_add_task`` stores also
    writes a mirror event on its due date, so the day's reminders are
    already in the window and listing the whole pending backlog underneath a
    one-day question would be the unscoped answer again in miniature.
    """
    try:
        data = _app_data(app)
        if scope:
            try:
                events = data.cal_list_events(
                    scope.get("days", 1), start=scope["start"],
                    end=scope["end"], label=scope.get("label"))
            except TypeError:
                # An older/stubbed store that only understands days-ahead.
                events = data.cal_list_events(scope.get("days", 1))
            tasks = ""
        else:
            events = data.cal_list_events(days)
            tasks = data.cal_list_tasks("pending")
    except Exception as exc:
        logger.debug("calendar read failed: %s", exc)
        return ("I couldn't open your calendar just now, so I can't tell you "
                "what's coming up.")
    has_events = bool(events) and not events.startswith(("No events", "Nothing"))
    has_tasks = bool(tasks) and not tasks.startswith("No pending")
    if not has_events and not has_tasks:
        if scope:
            label = scope.get("label") or "then"
            where = f"on {label}" if label[:1].isupper() else label
            return (f"Nothing {where} — your family calendar has no events "
                    "or reminders in that window.")
        return (f"Nothing recorded yet — no events in the next {days} "
                "days and no pending reminders on the family calendar.")
    parts = []
    if has_events:
        parts.append(events)
    if has_tasks:
        parts.append(tasks)
    return "\n\n".join(parts)


# ── tonight's meal ───────────────────────────────────────────────────────────

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def meal_today(app=None, now: datetime | None = None, meal: str = "dinner") -> str:
    """Tonight's dinner as the meal plan actually records it.

    Left to a model, "what's for dinner tonight" can be answered "pork ribs"
    with side-dish suggestions while the real plan has a different dish
    entirely. Nothing here is generated; the dish string is whatever was saved.
    """
    stamp = now or datetime.now()
    today = _WEEKDAYS[stamp.weekday()]
    try:
        plan = _app_data(app).fresh_get_meal_plan()
    except Exception as exc:
        logger.debug("meal plan read failed: %s", exc)
        return ("I couldn't open the meal plan just now, so I can't tell you "
                "what's planned.")
    entries = [e for e in (plan or [])
               if str(e.get("day", "")).lower() == today]
    wanted = [e for e in entries if str(e.get("meal", "")).lower() == meal]
    if wanted:
        entry = wanted[0]
        dish = str(entry.get("dish") or "").strip()
        if dish:
            note = str(entry.get("notes") or "").strip()
            line = f"Tonight ({today.capitalize()}) {meal} is {dish}."
            return f"{line} {note}" if note else line
    if entries:
        listed = "; ".join(
            f"{str(e.get('meal', '')).lower()}: {e.get('dish')}"
            for e in entries if e.get("dish"))
        if listed:
            return (f"There's no {meal} planned for {today.capitalize()}, but "
                    f"the plan does have — {listed}.")
    return (f"Nothing recorded yet — the meal plan has no {meal} for "
            f"{today.capitalize()}. Ask me to plan the week's meals and I'll "
            "fill it in.")


# ── the trigger table ────────────────────────────────────────────────────────
#
# Narrow before broad, first match wins. Each exclude vetoes the ACTION reading
# of the same words, so "add a bill" and "plan our meals" keep their own paths.

# ── bills, word-order-free ───────────────────────────────────────────────────
# Built from two halves so the trigger cannot depend on which one was said
# first. Every common phrasing, "are there any unpaid bills",
# "unpaid bills", "which bills are overdue", "what bills haven't we paid",
# "did we pay the electric bill" — is NOUN + STATE in one order or the other.
_BILLS_NOUN_SRC = r"\bbills?\b"
_BILLS_STATE_SRC = (
    r"\b(?:unpaid|outstanding|overdue|owed|owing|pending|due|past[-\s]due|"
    r"not\s+paid|still\s+open|left\s+to\s+pay)\b"
    r"|\b(?:haven'?t|hasn'?t|have\s+not|has\s+not|didn'?t|did\s+not)"
    r"(?:\s+(?:we|i|you|they))?\s+(?:paid|pay)\b"
    r"|\b(?:did|do|does|have|has)\s+(?:we|i|you)\s+(?:already\s+)?"
    r"(?:pay|paid)\b"
)
_BILLS_ORDER_FREE_SRC = (
    r"(?:" + _BILLS_NOUN_SRC + r")[^.?!]{0,30}(?:" + _BILLS_STATE_SRC + r")"
    r"|(?:" + _BILLS_STATE_SRC + r")[^.?!]{0,30}(?:" + _BILLS_NOUN_SRC + r")"
)
#: The action reading, and every OTHER thing called a bill. "bill of rights",
#: "the bill in congress", "Bill's birthday" and "the electric bill of a heat
#: pump" all have to stay out; the first three never reach the pattern above,
#: and this is the belt to that pair of braces. Deliberately NOT a blanket
#: \bpay\b|\bpaid\b — "did we pay the electric bill" is a question about the
#: record, and vetoing it would reopen the word-order hole in another form.
_BILLS_EXCLUDE_SRC = (
    r"\badd\b|\bcreate\b|\brecord\s+a\b|\bdelete\b|\bremove\b|\bmark\b"
    r"|\bset\s+(?:up\s+)?(?:a|an|the|my|our)\b"
    r"|\A\s*(?:please\s+|can\s+you\s+|could\s+you\s+|go\s+ahead\s+and\s+)?pay\b"
    r"|\blet'?s\s+pay\b|\b(?:i|we)\s+just\s+paid\b"
    r"|\bbill\s+of\b|\bbill\s+gates\b|\bbill['’]s\b"
    r"|\bbillboard|\bbilliard"
    r"|\b(?:congress|senate|legislat\w*|lawmakers?|amendment|statute|"
    r"parliament|bipartisan|filibuster|floor\s+vote|committee)\b"
)
_BILLS_EXCLUDE_RE = _re(_BILLS_EXCLUDE_SRC)

#: HOUSEHOLD-RECORD framing: the utterance is about THIS household's ledger
#: rather than about bills as a subject. Required by the registry fallback
#: below. Possessive/deixis on the noun, first person near the noun,
#: a ledger state word, or one of the "read me the ledger" idioms.
_BILLS_RECORD_SRC = (
    r"\b(?:our|my|these|those|the)\s+(?:\w+\s+){0,2}bills?\b"
    r"|\bbills?\b[^.?!]{0,24}\b(?:we|us|our|my|i)\b"
    r"|\b(?:we|us|our|my|i)\b[^.?!]{0,24}\bbills?\b"
    r"|\bow(?:e|es|ed|ing)\b"
    r"|\b(?:unpaid|outstanding|overdue|pending|past[-\s]due)\b"
    r"|\bbills?\b[^.?!]{0,24}\bdue\b|\bdue\b[^.?!]{0,24}\bbills?\b"
    r"|\bcoming\s+due\b"
    r"|\btotals?\b"
    r"|\brun\s+down\b|\brundown\b"
    r"|\bsituation\s+with\b"
)
_BILLS_RECORD_RE = _re(_BILLS_RECORD_SRC)

#: KNOWLEDGE / ADVICE / CIVICS framing, vetoed. "How does a bill become law",
#: "what is a spending bill", "how do i lower my electric bill" and "what is
#: the average water bill" are general questions that belong to RAG; the
#: household's real amounts must never be read out in answer to one.
#:
#: Applied ONLY in the fallback, never to the rules table above, so a genuine
#: record question that also carries one of these words ("how much are our
#: utility bills due this month") still matches the order-free rule and is
#: answered from the ledger. The veto's blast radius is exactly the phrasings
#: that already missed every hand-written rule.
_BILLS_KNOWLEDGE_SRC = (
    r"\bhow\s+(?:do|does|did|can|would|could)\b[^.?!]{0,48}\bworks?\b"
    r"|\bhow\s+(?:do|does|did)\s+(?:a|an|the)\b"
    r"|\bwhat\s+(?:is|are)\s+(?:a|an)\b|\bwhat'?s\s+(?:a|an)\b"
    r"|\bbecome\s+law\b|\bveto\w*\b|\bvot(?:e|es|ed|ing)\b"
    r"|\bpass(?:es|ed|ing)?\b|\bspending\b|\binfrastructure\b"
    r"|\butility\b|\baverage\b|\btypical\w*\b|\bnational\b"
    r"|\blower\b|\breduce\w*\b|\bcut\s+down\b|\bsave\s+on\b"
    r"|\btips?\b|\badvice\b|\bexplain\w*\b|\bdefine\b|\bdefinition\b"
    r"|\bpresident\b|\bsenators?\b|\brepresentatives?\b"
    r"|\bin\s+general\b|\bmean\s+by\b"
    r"|\bshould\b|\bought\b|\btaxes?\b|\btax\b|\bdeduct\w*\b"
    r"|\bwhat\s+should\s+we\s+do\s+about\b|\btoo\s+much\b"
    r"|\bhow\s+(?:do|does|can|could|should)\b"
    r"|\butility\s+bills?\s+work\b"
)
_BILLS_KNOWLEDGE_RE = _re(_BILLS_KNOWLEDGE_SRC)

#: The exception to the knowledge veto: explicit ledger/record framing with
#: an amount-or-due word. "How much are our utility bills due this month"
#: carries "utility" (knowledge) AND "due" (record) — the record framing
#: wins, so the ledger still answers rather than RAG.
_BILLS_AMOUNT_SRC = (
    r"\b(?:due|owe|owed|unpaid|overdue|paid|balance|total)\w*\b")
_BILLS_AMOUNT_RE = _re(_BILLS_AMOUNT_SRC)


def _knowledge_vetoed(text: str) -> bool:
    """True when the knowledge/advice/civics veto applies to ``text``.

    The veto is unconditional EXCEPT when the utterance also carries an
    explicit amount/due word — that combination is record framing, not a
    general-knowledge question, so it must still reach the ledger.
    """
    if not _BILLS_KNOWLEDGE_RE.search(text):
        return False
    return not _BILLS_AMOUNT_RE.search(text)


class _BillsKnowledgeExclude:
    """Wraps an exclude regex with the conditional knowledge veto so it can
    sit in the ``_RULES`` table's exclude slot alongside a plain regex."""

    def __init__(self, base_re):
        self._base_re = base_re

    def search(self, text):
        if self._base_re is not None and self._base_re.search(text):
            return True
        return _knowledge_vetoed(text) or None

_RULES = (
    # WHO lives here, from the roster. Ahead of every other rule: it is the
    # narrowest question in the table, and the only one whose store is
    # profiles.json rather than app_data. The exclude hands "add Riley to my
    # family" to the household WRITE fastpath, which owns it.
    ("household_members",
     _re(r"\bwho(?:\s+all)?(?:'s|\s+is|\s+are)\b[^.?!]{0,24}"
         r"\b(?:in|part\s+of|on)\b[^.?!]{0,16}"
         r"\b(?:my|our|the|this)\s+(?:household|family)\b"
         r"|\b(?:list|show|name|tell\s+me)\b[^.?!]{0,16}"
         r"\b(?:my|our|the)\s+(?:household|family)"
         r"(?:\s+(?:members|list|roster))?\b"
         r"|\b(?:my|our|the)\s+(?:household|family)\s+(?:members|roster)\b"
         r"|\bwho\s+lives\s+(?:here|in\s+(?:this|the)\s+(?:house|home))\b"),
     _re(r"\b(?:add|create|remove|delete|invite|register|set\s+up)\b")),
    # WHICH lights, ahead of the house SUMMARY. Both read the same store, but a
    # question naming lights wants their names and house_status answers with a
    # count — narrow before broad, so this sits first.
    ("lights_on",
     _re(r"\b(?:which|what|how\s+many)\s+lights?\b[^.?!]{0,24}"
         r"\b(?:are|is)\b[^.?!]{0,12}\bon\b"
         r"|\b(?:which|what)\s+lights?\s+(?:are\s+)?(?:still\s+)?on\b"
         r"|\b(?:are|is)\s+(?:there\s+)?any\s+lights?\s+(?:left\s+)?on\b"
         r"|\bany\s+lights?\s+(?:still\s+)?on\b"
         # "tell me about the Great Wall of China, then check if any lights
         # are on" must answer its second clause with the real HA read, not a
         # generic punt. A compound clause goes through this same table; the
         # TRIGGER is what matters: "check if any lights are on" puts the verb
         # between the noun and "on", which none of the rules above allow. It
         # is how a person actually asks, so it is a rule.
         r"|\b(?:any|the|our|my|which|what)\s+lights?\s+(?:that\s+)?"
         r"(?:are|is)\s+(?:still\s+|left\s+)?on\b"
         # Anchored at the end so "check the lights on the porch" - a question
         # about a fixture, not about which are lit - is left alone.
         r"|\b(?:check|see|find\s+out|tell\s+me|look)\b[^.?!]{0,16}"
         r"\blights?\b[^.?!]{0,16}\bon\b\s*[?.!]*$"
         r"|\b(?:are|is)\s+(?:the\s+|our\s+|my\s+)?lights?"
         r"\s+(?:still\s+)?on\b"
         # "is anything on" bare, someone sweeping the house before bed.
         # A trailing noun phrase makes it a different question entirely
         # ("is anything on the calendar", "is anything on TV"), so the
         # lookahead keeps this rule from claiming — and, because a matched
         # rule's exclude vetoes the whole table, from KILLING — those.
         r"|\bis\s+anything\s+(?:still\s+|left\s+)*on\b"
         r"(?!\s+(?:the|my|our|your|a|an|his|her|their|tv|television))"),
     # The ACTION reading of the same words ("turn on the kitchen lights",
     # "which lights should I switch on"), and the other "on" questions —
     # "is anything on the calendar / on TV / on the list" is not a lights read.
     _re(r"\bturn\b|\bswitch\b|\bdim\b|\bbrighten\b|\bset\b|\btoggle\b"
         r"|\bflip\b|\bpower\b|\bshut\b|\bkill\b"
         r"|\bon\s+(?:the|my|our|your)\s+"
         r"(?:calendar|schedule|agenda|menu|list|tv|telly|radio|news|"
         r"docket|plate|plan|program|programme)\b"
         r"|\bon\s+(?:tv|television|the\s+telly|tap|deck|sale|order|purpose)\b"
         r"|\bon\s+(?:netflix|jellyfin|spotify|youtube)\b")),

    # The INDOOR half of the weather fastpath's own veto. A question
    # that names a room and a temperature word is a sensor read, and it sits
    # ahead of house_status for the same narrow-before-broad reason lights_on
    # does — "check the living room temperature" wants that room's number, not
    # a whole-house summary. Every write reading of the same words ("set the
    # living room to 72") is vetoed by the exclude, and so is every outdoor
    # phrasing, which belongs to _try_weather_answer and reaches it first.
    ("room_temperature",
     _re(r"(?:" + _TEMP_WORD + r")[^.?!]{0,40}\b(?:" + _ROOM_ALT +
         r"|in\s+here|inside|indoors?|in\s+the\s+house)\b"
         r"|\b(?:" + _ROOM_ALT + r"|in\s+here|inside|indoors?|"
         r"in\s+the\s+house)\b[^.?!]{0,40}(?:" + _TEMP_WORD + r")"),
     _re(r"\bset\b|\bturn\b|\bchange\b|\braise\b|\blower\b|\badjust\b"
         r"|\bmake\s+it\b|\bbump\b|\bcrank\b|\bdrop\s+it\b"
         r"|\bweather\b|\bforecast\b|\boutside\b|\boutdoors?\b"
         r"|\bhigh\s+today\b|\btomorrow\b")),

    ("house_status",
     _re(r"\b(?:status|state)\s+of\s+(?:the\s+|our\s+|my\s+)?(?:house|home)\b"
         r"|\b(?:house|home)\s+status\b"
         r"|\bhow(?:'?s| is|\s+are)\s+(?:the\s+|our\s+|my\s+)?"
         r"(?:house|home)\s*(?:doing|looking)?\s*[?.!]*$"
         r"|\bhow(?:'?s| is)\s+everything\s+(?:in|at)\s+"
         r"(?:the\s+|our\s+|my\s+)?(?:house|home)\b"
         r"|\bis\s+everything\s+(?:ok|okay|alright|all\s+right|fine|good)\b"
         r"[^.?!]{0,24}\b(?:house|home)\b"),
     _re(r"\bwhite\s+house\b|\bhouse\s+of\b|\bfull\s+house\b"
         r"|\bhouse\s+(?:plant|music|party|cat|wine|band)\b"
         r"|\bhouse\s+of\s+representatives\b|\bopera\s+house\b")),

    # NOTE: vault lock state is NOT triggered from this table. Its trigger is
    # the ``vault_status`` entry in ``api_server._CAPREAD_RULES``, which runs
    # first and reaches the same reader through the ``vault_status`` tool. One
    # trigger table per capability; this module owns the READER only.
    ("medication_status",
     _re(r"\b(?:did|have)\s+(?:i|we)\s+(?:already\s+)?"
         r"(?:take|taken|had)\b[^.?!]{0,24}"
         r"\b(?:medication|medications|meds|medicine|pills?|dose)\b"
         r"|\bam\s+i\s+due\s+for\b[^.?!]{0,24}"
         r"\b(?:medication|meds|medicine|pills?|dose)\b"
         r"|\b(?:what|which|list|show)\b[^.?!]{0,24}"
         r"\b(?:medications|meds|medicine)\b[^.?!]{0,24}"
         r"\b(?:am\s+i|do\s+i|are\s+we|do\s+we|tracking|tracked|logged)\b"
         r"|\bmedication\s+(?:status|log|history|record|reminders?)\b"
         r"|\bmy\s+med(?:ication)?s?\s+(?:today|for\s+today)\b"),
     _re(r"\badd\b|\blog\b|\bremind\s+me\s+to\s+take\b|\brefill\b"
         r"|\bwhat\s+is\s+a\b|\bside\s+effects?\b|\binteract")),

    # The SAME question in the other word order. The rule below requires
    # "bills ... unpaid"; the household says "any unpaid bills" at least as
    # often, and without this that phrasing falls through to the model, which
    # can answer "I don't have a way to pull up a current list of your unpaid
    # bills", a
    # false incapacity claim about a capability that answers correctly one
    # synonym away. This rule is word-order-free: the bills noun and the
    # state word in either order, within one clause. It sits FIRST so its
    # narrower exclude (which must NOT veto "did we pay the electric bill")
    # wins over the blanket \bpay\b|\bpaid\b exclude on the rule below.
    ("bills_status", _re(_BILLS_ORDER_FREE_SRC),
     _BillsKnowledgeExclude(_BILLS_EXCLUDE_RE)),

    ("bills_status",
     _re(r"\b(?:do|does)\s+(?:we|i)\s+ow\w*\b"
         r"|\bhow\s+much\s+do\s+(?:we|i)\s+owe\b"
         r"|\b(?:what|which|any|how\s+many)\b[^.?!]{0,30}\bbills?\b"
         r"[^.?!]{0,30}\b(?:due|owed|owing|outstanding|unpaid|pending|"
         r"this\s+month|coming\s+up|left)\b"
         r"|\bbills?\b[^.?!]{0,20}\b(?:due|outstanding|unpaid|past\s+due)\b"
         r"|\b(?:list|show)\s+(?:me\s+)?(?:my\s+|our\s+|the\s+)?bills?\b"),
     _re(r"\badd\b|\bpay\b|\bpaid\b|\bmark\b|\bdelete\b|\bremove\b"
         r"|\bbill\s+of\s+rights\b|\bbill\s+gates\b")),

    ("calendar_upcoming",
     _re(r"\b(?:any|what|which|anything)\b[^.?!]{0,30}"
         r"\b(?:events?|reminders?|appointments?|meetings?)\b"
         r"[^.?!]{0,30}\b(?:coming\s+up|upcoming|today|tomorrow|this\s+week|"
         r"scheduled|planned|on\s+the\s+calendar|do\s+we\s+have|do\s+i\s+have|"
         r"are\s+there)\b"
         r"|\b(?:what'?s|what\s+is|whats)\s+(?:on\s+)?"
         r"(?:the\s+|our\s+|my\s+)?(?:calendar|schedule|agenda)\b"
         r"|\b(?:what'?s|whats|anything)\s+coming\s+up\b"
         r"|\b(?:events?|reminders?)\s+coming\s+up\b"),
     _re(r"\badd\b|\bcreate\b|\bschedule\s+(?:a|an|the)\b|\bdelete\b"
         r"|\bremove\b|\bcancel\b|\bmove\b|\breschedul")),

    # COUNTING the shopping list. ``app_list_shopping`` in api_server's capread
    # table owns the CONTENTS phrasings and runs first; it hands the counting
    # phrasings down here (its exclude names "how many"/"how much") because a
    # count is what a model fabricates most readily and this reader counts the
    # rows. "the list" with no other noun is the shopping list here — it is the
    # list this appliance keeps, and the exclude below returns every other
    # list's words (tasks, todo, chores, calendar, playlist, guests) to their
    # own readers.
    ("shopping_status",
     _re(r"\bhow\s+(?:many|much)\b[^.?!]{0,40}"
         r"\b(?:shopping|grocery|groceries)\b"
         r"|\bhow\s+(?:many|much)\b[^.?!]{0,32}\bon\s+(?:the|my|our)\s+list\b"
         r"|\bhow\s+many\s+(?:items?|things?|entries)\b[^.?!]{0,24}\blist\b"
         r"|\b(?:count|number\s+of)\b[^.?!]{0,24}"
         r"\b(?:shopping|grocery|groceries)\b[^.?!]{0,16}\blist\b"),
     _re(r"\bto-?do\b|\btasks?\b|\bchores?\b|\breminders?\b|\bcalendar\b"
         r"|\bplaylist\b|\bguests?\b|\bpeople\b|\bpantry\b|\bbills?\b"
         r"|\bmedications?\b|\bmeds\b|\bevents?\b|\brecipes?\b"
         r"|\badd\b|\bremove\b|\bcheck\s+off\b|\bcross\s+off\b"
         r"|\bcost\b|\bprice\b|\bspend\b|\btotal\b|\border\b|\bheb\b")),

    # The bare word is the commonest way it gets asked, and on the agent route
    # the tool call can come back with the FIRST entry in the plan (Monday)
    # labelled "tonight", a confident wrong day on a Saturday. Anchored to
    # the whole utterance so "dinner" inside a sentence keeps its other
    # meanings.
    ("meal_today",
     _re(r"\bwhat(?:'?s| is|\s+are\s+we\s+having)\b[^.?!]{0,20}"
         r"\bfor\s+(?:dinner|supper)\b"
         r"|\bwhat(?:'?s| is)\s+for\s+(?:dinner|supper)\b"
         r"|\bwhat\s+meal\s+is\s+plann?ed\b[^.?!]{0,20}"
         r"\b(?:today|tonight|for\s+today|for\s+tonight)\b"
         r"|\bwhat(?:'?s| is)\s+(?:on\s+)?the\s+menu\s+"
         r"(?:tonight|today|for\s+tonight|for\s+today)\b"
         r"|\bdinner\s+plan(?:ned)?\s+for\s+(?:tonight|today)\b"
         r"|\A\s*(?:so\s+|ok(?:ay)?\s+|hey\s+)?"
         r"(?:what(?:'?s| is)\s+)?(?:the\s+)?(?:dinner|supper)"
         r"(?:\s+(?:tonight|today))?\s*[?.!]*\s*\Z"),
     _re(r"\bplan\s+(?:my|our|the|this|next)\b|\bmake\s+me\b|\brecipe\b"
         r"|\bgrocery\b|\bgroceries\b|\bshopping\b|\border\b"
         r"|\bset\b|\bchange\b|\badd\b")),

    # A bare day name ("Saturday") must not reach knowledge/RAG, where it
    # comes back with trivia plus the household's own uploaded-photo filenames.
    # A day name, alone or as "what's on Saturday", is a calendar question;
    # the exclude keeps titles that merely CONTAIN a day name ("Saturday
    # Night Live", "Sunday Bloody Sunday", "Ruby Tuesday") out, along with
    # every watch/play/song reading of the same word.
    ("calendar_upcoming",
     _re(r"^\s*(?:on\s+|this\s+|next\s+|coming\s+)?"
         r"(?:mon|tues?|wed(?:nes)?|thur?s?|fri|satur|sun)day\s*[?.!]?\s*$"
         r"|\b(?:what'?s|what\s+is|whats|anything|what\s+do\s+(?:we|i)\s+have|"
         r"what(?:'s|\s+is)\s+happening|any\s+plans?)\b[^.?!]{0,24}"
         r"\b(?:on\s+|this\s+|next\s+|for\s+)?"
         r"(?:mon|tues?|wed(?:nes)?|thur?s?|fri|satur|sun)day\b"),
     _re(r"\bnight\s+live\b|\bsnl\b|\bbloody\b|\bruby\s+tuesday\b"
         r"|\b(?:watch|watching|play|playing|stream|streaming|listen|song|"
         r"album|band|movie|film|show|episode|series|channel|netflix|"
         r"jellyfin|youtube)\b"
         r"|\badd\b|\bcreate\b|\bdelete\b|\bremove\b|\bcancel\b|\bmove\b"
         r"|\bschedule\s+(?:a|an|the)\b|\bremind\s+me\b"
         r"|\bwhat\s+(?:day|date)\b|\bhow\s+do\s+you\s+spell\b")),

    # The calendar manifest's `list_day` utterances. "anything today" /
    # "anything tomorrow" name a day and nothing else — no "event", no
    # "calendar" — so neither calendar rule above could claim them and they
    # fell through to the model, which answered from nothing. Last in the
    # list on purpose: every earlier reader (meals, medications, bills) still
    # wins its own phrasings, and the exclude keeps the TV / weather / news
    # readings of the same words out.
    ("calendar_upcoming",
     _re(r"\b(?:anything|any\s+plans?|what\s+do\s+(?:we|i)\s+have)\b"
         r"[^.?!]{0,24}"
         r"\b(?:today|tonight|tomorrow|this\s+weekend|this\s+week|"
         r"next\s+week)\b"
         r"|\b(?:what'?s|what\s+is|whats)\s+"
         r"(?:going\s+on|happening|planned|scheduled|booked)\b"
         r"[^.?!]{0,24}\b(?:today|tonight|tomorrow|this\s+weekend|"
         r"this\s+week|next\s+week)\b"),
     _re(r"\badd\b|\bcreate\b|\bdelete\b|\bremove\b|\bcancel\b|\bmove\b"
         r"|\bremind\s+me\b|\bschedule\s+(?:a|an|the)\b|\breschedul"
         r"|\bweather\b|\bforecast\b|\bnews\b|\brain\b|\bsnow\b"
         r"|\b(?:watch|watching|play|playing|stream|streaming|listen|song|"
         r"album|band|movie|film|show|episode|series|channel|netflix|"
         r"jellyfin|youtube|tv|game)\b"
         r"|\bdinner\b|\bsupper\b|\blunch\b|\bbreakfast\b|\bmeal\b"
         r"|\bmedications?\b|\bmeds\b|\bbills?\b|\bgroceries\b")),
)

_HANDLERS = {
    "household_members": household_members,
    "lights_on": lights_on,
    "room_temperature": room_temperature,
    "house_status": house_status,
    "vault_status": vault_status,
    "medication_status": medication_status,
    "bills_status": bills_status,
    "shopping_status": shopping_status,
    "calendar_upcoming": calendar_upcoming,
    "meal_today": meal_today,
}

#: Narrows the calendar read to a single day when the question says so.
_TODAY_ONLY_RE = _re(r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening))\b")

#: Longest utterance this layer will claim. A paragraph carrying one of these
#: nouns is a conversation, not a lookup.
MAX_LEN = 160


#: The domain, and the shape of a question about it. Both are required by the
#: registry fallback below; neither is sufficient on its own.
_BILLS_DOMAIN_RE = _re(
    r"\bbills?\b|\bow(?:e|es|ed|ing)\b|\bcoming\s+due\b|\bpast[-\s]due\b")
#: Question OR request shape: the ledger is asked for in the imperative ("run
#: down the bills for me", "give me the bill totals") and as a bare possessive
#: noun phrase ("our bills") at least as often as it is asked as a question,
#: and all three must reach the reader rather than the model.
_BILLS_QUESTION_RE = _re(
    r"\A\s*(?:so\s+|ok(?:ay)?\s+|hey\s+|puff[,\s]+|and\s+)*"
    r"(?:do|does|did|is|are|am|was|were|have|has|had|can|could|will|would|"
    r"should|what|whats|what'?s|which|when|who|how|any|anything|"
    r"list|show|tell|give|run|read|pull|check|go|walk|catch|remind|"
    r"our|my|the|these|those)\b"
    r"|\?\s*\Z")


def _bills_registry_fallback(text: str) -> "str | None":
    """Send a bills-DOMAIN question to the bills reader, never to the model.

    A trigger hole has two halves. The first is closed by the word-order-free
    rule above. The second is the failure MODE behind it: when a hole is hit
    the turn reaches the LLM, which can claim "I don't have a way to pull up
    a current list of your unpaid bills", false, about a capability
    the registry itself declares (``bills.manage``) and the reader answers.
    A hand-written phrasing list will always have another hole; this closes
    the mode, so the worst case for an unanticipated bills phrasing is the
    real bill records rather than an invented incapacity.

    The gate is NOT the capability registry. Requiring
    ``capability_registry.find(text).id == "bills.manage"`` is wrong in both
    directions, because :func:`~puffpai.capability_registry.find` is a
    SUBSTRING RANKER, not a classifier: ``bills.manage`` declares the keyword
    ``"bill"``, matched with ``in``, so *any* string containing those four
    letters scores 4 and wins. "How does a bill become law", "can the president
    veto a bill", "what is a spending bill", "how do i lower my electric bill"
    and "what is the average water bill" would all be classified
    ``bills.manage`` and answered with the household's real ledger, a privacy
    leak of real amounts into general-knowledge questions. Meanwhile such a
    gate REFUSES real record questions: "what's the situation with bills"
    ranks as ``introspection.capabilities`` and "is there anything owed"
    scores nothing at all.

    So the four ANDs are deterministic and local. The utterance must

    1. name the domain (``_BILLS_DOMAIN_RE``),
    2. be shaped like a question or a request (``_BILLS_QUESTION_RE``),
    3. carry HOUSEHOLD-RECORD framing (``_BILLS_RECORD_RE``) — possessive or
       deixis on the noun, first person near it, a ledger state word, or a
       "read me the ledger" idiom, and
    4. survive both the action/homonym exclude (``_BILLS_EXCLUDE_RE``, which
       keeps out "bill of rights", "the bill in congress", "Bill's birthday")
       and the knowledge/advice/civics veto (``_BILLS_KNOWLEDGE_RE``).

    A knowledge question about bills reaches RAG. Only the household's own
    ledger framing reaches the household's own ledger.
    """
    if not _BILLS_DOMAIN_RE.search(text):
        return None
    if not _BILLS_QUESTION_RE.search(text):
        return None
    if not _BILLS_RECORD_RE.search(text):
        return None
    if _BILLS_EXCLUDE_RE.search(text):
        return None
    if _knowledge_vetoed(text):
        return None
    return "bills_status"


def match(user_message: str) -> "str | None":
    """The reader this utterance names, or None. Pure — no I/O, for tests."""
    text = (user_message or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    for name, match_re, exclude_re in _RULES:
        if not match_re.search(text):
            continue
        if exclude_re is not None and exclude_re.search(text):
            return None
        return name
    return _bills_registry_fallback(text)


def answer(user_message: str, **stores) -> "str | None":
    """Read the household store this utterance names, or return None.

    ``stores`` is forwarded to the reader, so a test can inject ``app=``,
    ``bridge=`` or ``vault=`` fixtures. Never raises.
    """
    name = match(user_message)
    if name is None:
        return None
    handler = _HANDLERS[name]
    try:
        import inspect
        accepted = set(inspect.signature(handler).parameters)
        kwargs = {k: v for k, v in stores.items() if k in accepted}
        # "tomorrow", "on Saturday", "this weekend" scope
        # the read to exactly that window; the reader names the window back.
        if name == "calendar_upcoming":
            scope = None
            try:
                scope = _caldates.day_scope(user_message or "")
            except Exception as exc:      # a bad clock must not lose the read
                logger.debug("calendar scope failed: %s", exc)
            if scope and "scope" in accepted:
                kwargs["scope"] = scope
            elif _TODAY_ONLY_RE.search(user_message or ""):
                kwargs["days"] = 1
        # The room the question names scopes the sensor read, so
        # "the living room temperature" is answered from the living room's own
        # sensor and never from another room's.
        if name == "room_temperature" and "room" in accepted:
            kwargs.setdefault("room", _room_named(user_message))
        # A counting phrasing gets the count, not the inventory.
        if name == "shopping_status" and "count_only" in accepted:
            kwargs.setdefault("count_only",
                              bool(_COUNT_ONLY_RE.search(user_message or "")))
        result = handler(**kwargs)
    except Exception as exc:
        logger.debug("household read %s failed: %s", name, exc)
        return None
    if not result:
        return None
    # The room this read just answered about is what the NEXT turn means
    # by "set the thermostat to 74". Recorded here, where the room was
    # resolved, so the write fastpath can borrow it (see climate_writes).
    if name == "room_temperature" and kwargs.get("room"):
        try:
            from puffpai import turn_context
            turn_context.note_room(kwargs["room"])
        except Exception as exc:          # a memory miss must not lose a read
            logger.debug("room memory failed: %s", exc)
    logger.info("household read: %s", name)
    return str(result)
