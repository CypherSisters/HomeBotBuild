"""Task dispatcher, the consumer of the Majordomo queue.

``enqueue``/``schedule`` *record* work; this module runs it. It is that
consumer: a daemon thread that wakes on a fixed tick, selects the
tasks whose due time has arrived, mints a budgeted
:class:`~puffpai.agents.envelope.Envelope` for each, and sends it **through the
shared bus** — so Sentinel's veto, the per-envelope budget and the audit ledger
apply to scheduled work exactly as they do to a typed message (no side
doors).

It lives next to :mod:`puffpai.agents.majordomo` because it is that agent's
consumer: it reuses the Majordomo's queue file, record shape and atomic-rewrite
helpers rather than owning a second store.

**At-most-once, deliberately.** A task's ``next_due`` is advanced and persisted
*before* the envelope is sent. A crash between those two points therefore skips
that one occurrence instead of replaying it on restart. Scheduled work here ends
in house actuators and automation flows; a briefing that is missed once is a
nuisance, an action replayed on restart is a safety problem — and the budget /
veto model assumes one execution per envelope. A recurring task self-heals the
next day; a one-shot that is lost this way is reported by its ``status``.

Failure containment mirrors the rest of the background layer: the thread never
raises, per-task failures are recorded on the task (``last_error`` +
``failure_count``), and a task that fails ``max_consecutive_failures`` times in a
row is disabled so a broken schedule cannot spin forever. Idle ticks log nothing.

Every failure mode has to *land on the record*, or the disable counter is
decorative. Three are easy to miss: a ``bus.send`` that raises (caught by
the tick's outer guard, so the task would stay ``scheduled`` with
``failure_count`` 0, healthy-looking and undisableable), a failed one-shot
(left ``done``, since the advance is written before the send), and a task
``_advance`` has just judged unschedulable (disabled, then dispatched anyway).
All three are handled in
:meth:`TaskDispatcher._dispatch`; a failed one-shot ends :data:`STATUS_FAILED`.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import threading

from puffpai.logging_config import get_logger

from .envelope import Budget, Envelope
from .majordomo import (
    MajordomoAgent,
    clamp_task_budget,
    compute_next_due,
    is_due,
    utcnow_dt,
)

logger = get_logger(__name__)

DEFAULT_TICK_SECONDS = 30
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

#: Ceiling on the reply summary stored back on the task record.
_MAX_ERROR_CHARS = 300

#: Status for a one-shot whose single run failed. Distinct from ``done`` (it did
#: not succeed) and from ``disabled`` (nothing turned it off) — and, like both,
#: not ``scheduled``, so :func:`is_due` will not pick it up again.
STATUS_FAILED = "failed"


def _budget_from(record: dict) -> Budget:
    """Build the envelope budget from the task record.

    The stored spec is re-clamped through
    :func:`puffpai.agents.majordomo.clamp_task_budget` rather than trusted. The
    queue file is a trust boundary in its own right: it holds records written
    before the ceiling existed, records an operator edited by hand, and — until
    this fix — records minted through the ``enqueue`` laundering path with
    budgets like ``max_actions: 999999``. Clamping on read means such a record
    cannot be cashed in even though it is already on disk.

    Egress is granted only when the record explicitly asked for it; everything
    else inherits the fail-closed default.
    """
    spec = clamp_task_budget(record.get("budget"))
    return Budget(
        max_actions=spec["max_actions"],
        max_seconds=spec["max_seconds"],
        allow_egress=spec["allow_egress"],
    )


def envelope_for(record: dict) -> Envelope:
    """Mint the budgeted envelope a due task is dispatched as."""
    return Envelope(
        intent=str(record.get("intent", "task")),
        payload=dict(record.get("payload") or {}),
        requester=str(record.get("requester", "system")),
        origin_agent="dispatcher",
        budget=_budget_from(record),
        state_changing=bool(record.get("state_changing", True)),
    )


def _reply_failed(reply) -> str:
    """Return an error string when *reply* is a failure, else ``""``."""
    if not isinstance(reply, dict):
        return ""
    if reply.get("denied"):
        return f"denied by {reply.get('by', 'reviewer')}: {reply.get('reason', '')}"
    if reply.get("aborted"):
        return f"aborted ({reply['aborted']}): {reply.get('reason', '')}"
    if reply.get("ok") is False:
        return str(reply.get("error", "task reported failure"))
    return ""


class TaskDispatcher:
    """Daemon thread that runs due Majordomo tasks through the bus."""

    def __init__(self, *, majordomo: MajordomoAgent | None = None, bus=None,
                 tick_seconds: int = DEFAULT_TICK_SECONDS,
                 max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
                 now=None) -> None:
        self._majordomo = majordomo
        self._bus = bus
        self._tick = float(tick_seconds)
        self._max_failures = int(max_consecutive_failures)
        self._now = now or utcnow_dt
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dispatched = 0

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self) -> None:
        """Start the daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="majordomo-dispatcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to exit and wait for it briefly."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "tick_seconds": self._tick,
            "max_consecutive_failures": self._max_failures,
            "dispatched": self._dispatched,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # the tick loop must outlive any one failure
                logger.warning("dispatcher tick failed: %s", exc)
            if self._stop.wait(self._tick):
                break

    # ── wiring (lazy, guarded) ──────────────────────────────────────
    def _get_bus(self):
        """The SHARED agent bus — one Sentinel, one audit ledger."""
        if self._bus is not None:
            return self._bus
        from puffpai.agent_events import get_bus
        return get_bus()

    def _get_majordomo(self, bus):
        if self._majordomo is not None:
            return self._majordomo
        agent = bus.get("majordomo") if bus is not None else None
        return agent if isinstance(agent, MajordomoAgent) else None

    # ── one tick ────────────────────────────────────────────────────
    def run_once(self) -> int:
        """Dispatch every task that is due now. Returns how many ran.

        Never raises: this is what the thread calls, and it is also the seam the
        tests drive directly instead of waiting on a tick.
        """
        bus = self._get_bus()
        majordomo = self._get_majordomo(bus)
        if bus is None or majordomo is None:
            return 0

        now = self._now()
        due = [r for r in majordomo.list_tasks() if is_due(r, now)]
        if not due:
            return 0  # idle ticks stay silent

        ran = 0
        for record in due:
            try:
                self._dispatch(bus, majordomo, record, now)
                ran += 1
            except Exception as exc:  # one bad task must not stall the rest
                logger.warning("dispatch of task %s failed: %s",
                               record.get("id"), exc)
        self._dispatched += ran
        return ran

    def _dispatch(self, bus, majordomo: MajordomoAgent, record: dict,
                  now) -> None:
        task_id = str(record.get("id"))
        advance = self._advance(record, now)

        # ── a task judged unschedulable is never sent ──
        # ``_advance`` disables a task whose schedule it cannot compute. Sending
        # it anyway fired one occurrence of a task already ruled unrunnable, so
        # the verdict is applied first and the tick ends here.
        if advance.get("status") == "disabled":
            majordomo.update_task(task_id, **advance)
            logger.warning("task %s disabled without dispatch: %s",
                           task_id, advance.get("last_error", "unschedulable"))
            return

        # ── commit the advance BEFORE running (at-most-once, see module doc) ──
        majordomo.update_task(task_id, last_run_at=now.isoformat(),
                              run_count=int(record.get("run_count", 0)) + 1,
                              **advance)

        envelope = envelope_for(record)
        logger.info("dispatching task %s: %s -> %s (envelope %s)",
                    task_id, envelope.intent, record.get("target"), envelope.id)

        # ``bus.send`` is contained here, not by ``run_once``'s per-task guard.
        # Letting it raise past this point left ``failure_count`` at 0 and the
        # status at ``scheduled``, so a permanently broken schedule reported
        # itself healthy forever and could never reach max_consecutive_failures.
        try:
            reply = bus.send(envelope, to=str(record.get("target") or "majordomo"))
        except Exception as exc:
            error = f"send raised {exc.__class__.__name__}: {exc}"
            logger.warning("task %s: bus send raised: %s", task_id, exc)
        else:
            error = _reply_failed(reply)

        if not error:
            majordomo.update_task(task_id, failure_count=0, last_error=None)
            return

        failures = int(record.get("failure_count", 0)) + 1
        fields = {"failure_count": failures, "last_error": error[:_MAX_ERROR_CHARS]}
        if failures >= self._max_failures:
            fields["status"] = "disabled"
            logger.warning("task %s disabled after %d consecutive failures: %s",
                           task_id, failures, error[:_MAX_ERROR_CHARS])
        else:
            if advance.get("status") == "done":
                # A one-shot's only run failed. ``_advance`` already wrote
                # ``done`` (it runs before the send, by design); leaving it
                # there would report a task that never succeeded as completed.
                fields["status"] = STATUS_FAILED
            logger.warning("task %s failed (%d/%d): %s",
                           task_id, failures, self._max_failures,
                           error[:_MAX_ERROR_CHARS])
        majordomo.update_task(task_id, **fields)

    @staticmethod
    def _advance(record: dict, now) -> dict:
        """Fields that move a task past the occurrence about to run.

        A recurring task gets its next local slot; a one-shot is finished.
        """
        schedule = record.get("schedule") or {}
        daily_at = schedule.get("daily_at")
        if daily_at:
            try:
                return {"next_due": compute_next_due(daily_at=daily_at, after=now)}
            except ValueError as exc:
                logger.warning("task %s has an unusable daily_at (%s); disabling",
                               record.get("id"), exc)
                return {"status": "disabled", "last_error": str(exc)}
        return {"status": "done", "next_due": None}


# ── module singleton + startup wiring ───────────────────────────────

_dispatcher: TaskDispatcher | None = None


def start_dispatcher() -> TaskDispatcher | None:
    """Start the shared dispatcher when ``dispatcher.enabled`` is set.

    Returns the dispatcher, or None when disabled/unconfigured. Never raises.
    """
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher
    try:
        from puffpai.config import get_config
        cfg = get_config().dispatcher
        if not cfg.enabled:
            return None
        dispatcher = TaskDispatcher(
            tick_seconds=cfg.tick_seconds,
            max_consecutive_failures=cfg.max_consecutive_failures,
        )
        dispatcher.start()
        _dispatcher = dispatcher
        return _dispatcher
    except Exception as exc:
        logger.warning("task dispatcher failed to start: %s", exc)
        return None


def get_dispatcher() -> TaskDispatcher | None:
    return _dispatcher


def stop_dispatcher() -> None:
    global _dispatcher
    if _dispatcher is not None:
        _dispatcher.stop()
        _dispatcher = None


__all__ = [
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_TICK_SECONDS",
    "STATUS_FAILED",
    "TaskDispatcher",
    "envelope_for",
    "get_dispatcher",
    "start_dispatcher",
    "stop_dispatcher",
]


# ── seeding CLI ─────────────────────────────────────────────────────
#
#   python -m puffpai.agents.dispatcher --seed-briefing --at 07:00
#   python -m puffpai.agents.dispatcher --list
#
# The equivalent over HTTP (API-key gated) is:
#   POST /agents/tasks
#   {"intent": "daily_briefing", "daily_at": "07:00",
#    "budget": {"max_actions": 4, "max_seconds": 180, "allow_egress": true}}

def _main(argv=None) -> int:
    import argparse
    import json as _json

    from .majordomo import (MajordomoAgent, get_queue_path,
                            seed_briefing_task)

    parser = argparse.ArgumentParser(
        prog="puffpai.agents.dispatcher",
        description="Inspect and seed the Majordomo scheduled-task queue.")
    parser.add_argument("--seed-briefing", action="store_true",
                        help="register the recurring morning briefing (idempotent)")
    parser.add_argument("--at", default="07:00", metavar="HH:MM",
                        help="local time for --seed-briefing (default 07:00)")
    parser.add_argument("--list", action="store_true",
                        help="print the scheduled tasks as JSON")
    args = parser.parse_args(argv)

    seeded_any = args.seed_briefing
    print(f"queue: {get_queue_path()}")
    if args.seed_briefing:
        record = seed_briefing_task(args.at)
        print(f"briefing task {record['id']} next due {record['next_due']}")
    if args.list or not seeded_any:
        print(_json.dumps(MajordomoAgent().list_tasks(), indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
