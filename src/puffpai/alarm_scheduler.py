"""AlarmFlow's schedule source — the thing that makes an alarm an alarm.

Without server-side firing, an alarm fires only in the browser
(``alarm-app.html``'s ``tick()``, the dashboard root's mount effect, and the
dedicated AlarmFlow page), and each of those only runs while *that* component
is the mounted screen. Walk the kiosk from the dashboard into FreshTable, let
a timer expire, and it rings nowhere (no chime, no speech, no notification),
and the item stays ``status: "running"`` forever, so nothing afterwards can
even tell you it was missed. An appliance whose alarm depends on which screen
is open is not an alarm clock.

This module is AlarmFlow's half of server-side firing: a source for
:class:`puffpai.scheduler.Scheduler`, which owns the thread, the fired/missed
policy and the announce path. What is AlarmFlow-specific lives here:

* **Due time.** An alarm carries a wall-clock ``targetMs``; a timer carries a
  start stamp plus a duration, and a paused timer carries no start stamp at all.
* **The ring contract.** ``status`` moves to ``fired`` (with ``fired_at`` /
  ``fired_ts``) or ``missed`` (with ``missed_at``). Because the write lands in
  ``alarmflow.json``, ``last_updated`` changes and every open client sees it
  through the ``/apps/alarmflow/updated`` poll it already runs. The three
  browser implementations therefore do not compute due times; they simply
  render ``status === "fired"``, display-only.
* **Rings, then gives up.** A ``fired``/``missed`` item nobody dismisses is
  resolved after the engine's ring timeout: a repeating alarm rolls to its next
  occurrence and goes back to ``running``, a one-shot ends ``stopped``. Without
  this a daily alarm would fire once and never again, since the user dismissing
  it is what rolls it forward everywhere else.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import time

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

STATUS_RUNNING = "running"
STATUS_FIRED = "fired"
STATUS_MISSED = "missed"
STATUS_STOPPED = "stopped"

#: Statuses this source may advance from. ``paused`` and ``stopped`` are the
#: user's business, not the scheduler's.
_FIREABLE = (STATUS_RUNNING, "ringing")


def _fmt_clock(hour: int, minute: int) -> str:
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def due_ms(item: dict) -> int | None:
    """Epoch-ms at which *item* comes due, or None if it never does."""
    if not isinstance(item, dict):
        return None
    if item.get("mode") == "alarm":
        try:
            target = int(item.get("targetMs") or 0)
        except (TypeError, ValueError):
            return None
        return target or None
    try:
        started = int(item.get("ts") or 0)
        total = int(item.get("tot") or 0)
    except (TypeError, ValueError):
        return None
    if not started or total <= 0:
        return None
    return started + total * 1000


def describe(item: dict) -> str:
    """How this item is named out loud."""
    desc = str(item.get("desc") or "").strip()
    if item.get("mode") == "alarm":
        try:
            hour, minute = int(item.get("hour", 0)), int(item.get("minute", 0))
        except (TypeError, ValueError):
            return desc or "your alarm"
        clock = _fmt_clock(hour, minute)
        if desc and desc.lower() != "alarm":
            return f"{desc}, {clock}"
        return clock
    return desc or "your timer"


def fired_sentence(item: dict) -> str:
    if item.get("mode") == "alarm":
        return f"Alarm: {describe(item)}."
    return f"Timer finished: {describe(item)}."


class AlarmFlowSource:
    """AlarmFlow alarms and timers, as scheduler occurrences."""

    name = "alarmflow"

    def __init__(self, app_data=None, *, clock=None):
        self._app_data = app_data
        self._now = clock or time.time
        self._doc: dict | None = None
        self._dirty = False

    def _data(self):
        if self._app_data is None:
            from puffpai.scheduler import _shared_app_data
            self._app_data = _shared_app_data()
        return self._app_data

    # ── source protocol ────────────────────────────────────────────────────
    def refresh(self) -> None:
        self._doc = self._data().get_data("alarmflow") or {}
        self._dirty = False

    def _items(self) -> list:
        items = (self._doc or {}).get("items") or []
        return [i for i in items if isinstance(i, dict)]

    def occurrences(self) -> list:
        out = []
        for item in self._items():
            if item.get("status") not in _FIREABLE:
                continue
            when = due_ms(item)
            if when is None:
                continue
            out.append({
                "id": str(item.get("id") or ""),
                "due_ms": when,
                "kind": "alarm" if item.get("mode") == "alarm" else "timer",
                "label": describe(item),
                "sentence": fired_sentence(item),
                "item": item,
            })
        return out

    def mark(self, occ, status, when_iso, when_ms) -> None:
        item = occ.get("item") or {}
        item["status"] = status
        if status == STATUS_FIRED:
            item["fired_at"] = when_iso
            item["fired_ts"] = when_ms
            item["rem"] = 0
        else:
            item["missed_at"] = when_iso
            item["missed_ts"] = when_ms
        self._dirty = True

    def sweep(self, now_ms, ring_timeout_ms) -> list:
        """Resolve anything that has been ringing unattended for too long."""
        resolved = []
        for item in self._items():
            if item.get("status") not in (STATUS_FIRED, STATUS_MISSED):
                continue
            try:
                stamp = int(item.get("fired_ts") or item.get("missed_ts") or 0)
            except (TypeError, ValueError):
                stamp = 0
            if not stamp or (now_ms - stamp) < ring_timeout_ms:
                continue
            if not self._reschedule(item, now_ms / 1000.0):
                item["status"] = STATUS_STOPPED
            resolved.append(item)
            self._dirty = True
        return resolved

    def flush(self) -> None:
        # The items were mutated in place inside ``self._doc``; the document is
        # written back whole, so rows this source never looked at survive
        # untouched.
        if self._dirty and self._doc is not None:
            self._data().set_data("alarmflow", self._doc)
        self._dirty = False

    # ── AlarmFlow specifics ────────────────────────────────────────────────
    def _reschedule(self, item: dict, now: float) -> bool:
        """Roll a repeating alarm to its next occurrence. True if it rolled.

        Reuses ``AppData._next_alarm_target`` rather than re-deriving the
        repeat rules, so a weekday alarm skips Saturday here for exactly the
        same reason it does when the user dismisses one.
        """
        if item.get("mode") != "alarm" or (item.get("repeat") or "once") == "once":
            return False
        try:
            from datetime import datetime
            from puffpai.app_data import AppData
            nxt = AppData._next_alarm_target(
                int(item.get("hour", 8)), int(item.get("minute", 0)),
                item.get("repeat", "daily"), datetime.fromtimestamp(now),
                anchor_weekday=item.get("weekday"))
        except Exception as exc:
            logger.warning("alarmflow source: could not reschedule %s: %s",
                           item.get("id"), exc)
            return False
        item["targetMs"] = int(nxt.timestamp() * 1000)
        item["tot"] = item["rem"] = max(0, int(nxt.timestamp() - now))
        item["ts"] = int(now * 1000)
        item["status"] = STATUS_RUNNING
        item["snoozed"] = 0
        return True
