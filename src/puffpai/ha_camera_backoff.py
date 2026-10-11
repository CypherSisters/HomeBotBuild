"""Per-camera failure backoff for the ``/ha/camera/<entity_id>`` proxy.

WHY. The dashboard polls every camera tile every 30 s. A camera that has
been unavailable for weeks makes every poll a doomed round trip to Home
Assistant that comes back 502: about 20 non-2xx per 10-minute window, 2,880
pointless upstream requests a day, and a journal in which a real fault would
be invisible.

The rule this module implements: a camera that fails is not retried
immediately. The first failure is reported at ERROR (once — the tile is
"unavailable" and someone should know); every subsequent failure in the same
streak doubles the wait, from 30 s up to a 10-minute ceiling, and is logged at
DEBUG. While a camera is in backoff the proxy answers from here WITHOUT
touching Home Assistant, with the same status code and body shape it always
returned plus a ``Retry-After`` and an explicit ``unavailable`` marker, so the
dashboard can stop asking rather than being told 502 twenty times an hour.

One success clears the streak completely: a camera that comes back is polled
at full rate again on the very next request.

State is per process and in memory. A restart re-probes every camera once,
which is the correct behaviour — the box may have been restarted precisely
because the camera was fixed.
"""

from __future__ import annotations

import threading
import time

#: Wait after the Nth consecutive failure, in seconds. Index 0 is the wait
#: after the FIRST failure. Doubling from 30 s, capped at 600 s (10 min) —
#: the ceiling the brief asks for, and roughly the interval at which a human
#: would notice a camera had come back.
BACKOFF_SCHEDULE = (30, 60, 120, 240, 480, 600)

#: The ceiling, named so callers and tests do not index the tuple.
MAX_BACKOFF = BACKOFF_SCHEDULE[-1]

_lock = threading.Lock()
#: entity_id -> {"streak": int, "until": monotonic deadline}
_state: "dict[str, dict]" = {}


def _now() -> float:
    return time.monotonic()


def backoff_for(streak: int) -> int:
    """Seconds to wait after *streak* consecutive failures (streak >= 1)."""
    if streak < 1:
        return 0
    index = min(streak, len(BACKOFF_SCHEDULE)) - 1
    return BACKOFF_SCHEDULE[index]


def retry_after(entity_id: str) -> int:
    """Seconds left in *entity_id*'s backoff window, or 0 if it may be tried.

    0 means "go ask Home Assistant". Anything positive means the last attempt
    failed recently enough that another one is only going to fail again.
    """
    with _lock:
        entry = _state.get(entity_id)
        if not entry:
            return 0
        remaining = entry["until"] - _now()
        if remaining <= 0:
            return 0
        return max(1, int(remaining + 0.999))


def record_failure(entity_id: str) -> "tuple[int, int, bool]":
    """Record one failed fetch.

    Returns ``(streak, retry_after_seconds, first_of_streak)``.
    ``first_of_streak`` is True exactly once per outage and is what the caller
    keys its single ERROR line off — every later failure is DEBUG.
    """
    with _lock:
        entry = _state.get(entity_id)
        streak = (entry["streak"] if entry else 0) + 1
        wait = backoff_for(streak)
        _state[entity_id] = {"streak": streak, "until": _now() + wait}
        return streak, wait, streak == 1


def record_success(entity_id: str) -> None:
    """Forget *entity_id*'s failure streak — it answered."""
    with _lock:
        _state.pop(entity_id, None)


def streak(entity_id: str) -> int:
    """Consecutive failures recorded for *entity_id* (0 when healthy)."""
    with _lock:
        entry = _state.get(entity_id)
        return int(entry["streak"]) if entry else 0


def reset(entity_id: "str | None" = None) -> None:
    """Clear one camera's state, or all of it. For tests and ops."""
    with _lock:
        if entity_id is None:
            _state.clear()
        else:
            _state.pop(entity_id, None)
