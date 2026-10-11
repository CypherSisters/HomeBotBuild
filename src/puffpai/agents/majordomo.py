"""Majordomo — WF-5 proactive & scheduled work ("agents while you sleep").

The Majordomo owns background work: the daily briefing, reminders, calendar sync,
automation-flow ownership, and — the pillar-4 vision made concrete — *spawning
scoped task-workers with strict budgets*. It is the only holder of the automation API
key and calendar credentials (``AgentScope.MAJORDOMO``).

Two things make the background layer safe:

* **Budgets.** :meth:`spawn_task_worker` mints a fresh :class:`Envelope` with a
  caller-supplied :class:`Budget` (default: 1 action, no egress). The worker
  cannot outrun that budget — the bus aborts it if it tries.
* **A persistent queue.** Tasks live in a JSONL file under the writable state
  directory (:func:`puffpai.config.resolve_state_dir`) so a restart does not lose
  scheduled work.

Running a workflow or worker that reaches the automation engine is ``state_changing`` and passes
Sentinel review like any other plan.

**Scheduled tasks.** The queue is a small task *store*, one JSONL line per
task, keyed by ``id``, with a ``next_due`` timestamp, consumed by
:mod:`puffpai.agents.dispatcher`, so what the ``enqueue``/``schedule`` intents
append is actually read back. Records without a ``next_due`` (an older,
write-only shape) still parse; they are never dispatched.

Writes go through :meth:`_rewrite_queue`, which is temp-file-then-``os.replace``
under the module lock: a crash leaves either the old file or the new one, never
a half-written queue.

**The trust boundary is :meth:`MajordomoAgent.create_task`, not the HTTP route.**
Two doors mint tasks — the authenticated ``POST /agents/tasks`` and the
agent-facing ``enqueue``/``schedule`` intent — and both land there, so the
budget ceiling and the target/egress policy are enforced once, where a record is
actually written. Each record carries an ``origin`` marker (``owner`` vs
``agent``) that the agent-facing door hard-codes; a message cannot promote
itself. See the ceiling constants below.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

from puffpai.logging_config import get_logger
from puffpai.security.secrets_provider import AgentScope, SecretsProvider
# Safe at module scope: statedir is a stdlib-only leaf that defers its
# puffpai.config import (and with it the secrets provider) to call time, so
# importing it here adds nothing to this module's import graph. _STATE_DIR_ENV
# is the env-var NAME only, and tests/test_statedir.py pins it to
# config.STATE_DIR_ENV, so reading it here cannot drift from canon.
from puffpai.statedir import _STATE_DIR_ENV, resolve_runtime_dir

from .base import BaseAgent
from .envelope import Budget, Envelope

logger = get_logger(__name__)

#: Repo root — four directory levels up from
#: ``src/puffpai/agents/majordomo.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: Canonical default: the repo-root ``data`` directory, shared with
#: agent_events, security/consent_gate, security/consent_requests and
#: zone3/adguard_controller, rather than the package-relative
#: ``src/puffpai/data``, for two reasons: convergence (runtime state scattered
#: across several directories is how an operator relocates or backs up one and
#: silently gets half), and writability — the appliance unit's
#: ``ReadWritePaths`` covers ``/opt/puffpai/data`` but not
#: ``/opt/puffpai/src/puffpai/data``, so a queue defaulting there could not
#: persist scheduled work at all. This is still only the *fallback*:
#: ``<repo>/data`` sits inside the checkout, where a redeploy is free to
#: overwrite it, so a real deployment MUST set ``paths.state_dir`` (absolute)
#: or ``$PUFFPAI_DATA_DIR``. See :func:`puffpai.config.resolve_state_dir`.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

#: The legacy default, ``src/puffpai/data``. Nothing is written here any
#: more; a queue or briefing an earlier release left behind is adopted IN PLACE
#: by :func:`_resolve_state_file` — never copied, never moved — so an upgraded
#: dev box does not silently start with an empty task queue.
_LEGACY_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)

#: Absolute path to the task queue, resolved on first use and cached here.
#: ``None`` means "not resolved yet"; tests set it directly to override.
QUEUE_PATH: str | None = None
#: Absolute path to the stored daily briefing, resolved the same way.
BRIEFING_PATH: str | None = None

#: Ceiling on a scheduled task's serialized payload (matches the route's check).
MAX_PAYLOAD_BYTES = 8192
#: Ceiling on the briefing text kept on disk / returned to the dashboard.
MAX_BRIEFING_CHARS = 4000

#: Budget a scheduled task gets when it does not name its own.
DEFAULT_TASK_BUDGET = {"max_actions": 4, "max_seconds": 60.0, "allow_egress": False}

# ── task ceilings: ONE definition, enforced where tasks are created ──
#
# Enforcing these bounds only in ``POST /agents/tasks``' input validation
# would be launderable: the ``enqueue``/``schedule`` intent re-enters
# :meth:`MajordomoAgent.create_task` with a caller-supplied budget, target and
# ``state_changing`` flag, and a budget that is merely type-coerced would let
# an agent-facing message mint a task record carrying
# ``max_actions: 999999, allow_egress: true`` aimed at any agent, which the
# dispatcher would then honour. The limits therefore belong *here*, at the point a
# task record is written, and the route's own checks are now a friendlier
# restatement (they 400 instead of silently clamping) rather than the only copy.
#
# ``api_server._TASK_MAX_ACTIONS`` / ``_TASK_MAX_SECONDS`` / ``_TASK_INTENT_MAX``
# must stay equal to these; see the module tests, which assert it.
MIN_TASK_ACTIONS = 1
MAX_TASK_ACTIONS = 20
MIN_TASK_SECONDS = 1.0
MAX_TASK_SECONDS = 600.0
MAX_TASK_INTENT_CHARS = 64
MAX_TASK_TARGET_CHARS = 64
MAX_TASK_REQUESTER_CHARS = 64

#: Trust marker stored on every task record. ``owner`` is the authenticated
#: HTTP door (``POST /agents/tasks``); ``agent`` is anything minted from an
#: agent-facing intent. Anything unrecognised is treated as ``agent``.
TASK_ORIGIN_OWNER = "owner"
TASK_ORIGIN_AGENT = "agent"

#: The only targets an ``agent``-origin task may address. Deliberately excludes
#: every actuator — steward (house + media), caretaker (services), sentinel
#: (network policy). An agent may schedule its own follow-up work; putting a
#: lock, a siren or a firewall rule on a timer is the owner's decision, made
#: through the authenticated route.
AGENT_ORIGIN_TARGETS = frozenset({"majordomo", "concierge"})

#: How stale a one-shot ``run_at`` may be and still be accepted. A task
#: whose moment passed longer ago than this fires on the very next tick, which
#: is a surprise actuation rather than a schedule; it is refused at creation.
#: The window exists only to tolerate clock skew between caller and appliance.
#: Accepted-but-past tasks keep the documented rule: fire once, then ``done``.
MAX_RUN_AT_BACKDATE_SECONDS = 300

_QUEUE_LOCK = threading.RLock()

_DAILY_AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def utcnow_dt() -> datetime:
    """Aware UTC 'now'. Indirected so tests can pin the clock."""
    return datetime.now(timezone.utc)


# ── schedule parsing (pure, no I/O) ─────────────────────────────────

def parse_run_at(value) -> datetime:
    """Parse a one-shot ISO-8601 timestamp into an aware UTC datetime.

    A naive timestamp is read as *local* wall-clock (what a human types), then
    converted to UTC — the queue always stores UTC so a timezone change cannot
    silently move every scheduled task.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("run_at must be a non-empty ISO-8601 timestamp")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Never echo the whole input back to an API caller.
        raise ValueError("run_at must be an ISO-8601 timestamp") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret as local, with the offset then in effect
    return dt.astimezone(timezone.utc)


def parse_daily_at(value) -> tuple[int, int]:
    """Parse a recurring ``"HH:MM"`` local-time form into ``(hour, minute)``."""
    match = _DAILY_AT_RE.match(str(value).strip())
    if not match:
        raise ValueError("daily_at must be local 24-hour 'HH:MM'")
    return int(match.group(1)), int(match.group(2))


def next_daily_due(hour: int, minute: int, after: datetime | None = None) -> datetime:
    """Next local ``hour:minute`` strictly after *after*, returned in UTC.

    The candidate is built as a *naive* local datetime and converted with
    :meth:`datetime.astimezone`, so the UTC offset in effect on that date is
    used — a daily_at task keeps its wall-clock hour across a DST change
    instead of drifting by an hour.
    """
    after_local = (after or utcnow_dt()).astimezone()
    candidate = after_local.replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
    if candidate <= after_local.replace(tzinfo=None):
        candidate += timedelta(days=1)
    return candidate.astimezone().astimezone(timezone.utc)


def compute_next_due(*, run_at=None, daily_at=None,
                     after: datetime | None = None) -> str:
    """Return the ISO-UTC ``next_due`` for a schedule spec. Exactly one form."""
    if bool(run_at) == bool(daily_at):
        raise ValueError("exactly one of run_at or daily_at is required")
    if run_at:
        return parse_run_at(run_at).isoformat()
    hour, minute = parse_daily_at(daily_at)
    return next_daily_due(hour, minute, after=after).isoformat()


def is_valid_task_id(value) -> bool:
    """True for the 12-hex-char ids :meth:`create_task` mints.

    The route checks this before touching the queue: a task id arrives as a URL
    path segment, and only the exact minted shape gets past here.
    """
    return isinstance(value, str) and bool(_TASK_ID_RE.match(value))


def is_due(record: dict, now: datetime | None = None) -> bool:
    """True when *record* is a live scheduled task whose due time has arrived."""
    if (record or {}).get("status") != "scheduled":
        return False
    next_due = record.get("next_due")
    if not next_due:
        return False
    try:
        due = parse_run_at(next_due)
    except (TypeError, ValueError):
        return False
    return due <= (now or utcnow_dt())


# ── state-file locations ────────────────────────────────────────────

def _resolve_state_file(filename: str) -> str:
    """Resolve *filename* inside the writable state directory, adopting legacy.

    Resolved lazily rather than at import: ``puffpai.config`` pulls in the
    secrets provider, so importing it at module scope would risk a cycle.
    (:mod:`puffpai.statedir` is imported at module scope precisely because it is
    stdlib-only and defers that import too — but it must still be *called* from
    in here, not at import.)

    The CONFIGURED tier stays a direct :func:`~puffpai.config.resolve_state_dir`
    call, because this module honours ``paths.state_dir`` and
    :func:`~puffpai.statedir.resolve_runtime_dir` always passes
    ``configured=None``. Everything below it collapses into that one leaf call,
    which IS both of the old degraded tiers: where config imported but
    ``get_config()`` raised, the canon's ``configured=None`` path is what the
    fallback should always have been; where config was unimportable, the leaf's
    own replica is differential-tested against the canon in
    ``tests/test_statedir.py`` rather than trusted per module.

    A config failure must never change *where* ``$PUFFPAI_DATA_DIR`` points.
    The old code read the env var raw on that tier — no strip, no ``~``
    expansion, no repo-root join for a relative value — so the documented
    relative value ``"data"`` put the queue next to whatever directory systemd
    happened to start the service in, and ``~/puff_state`` made a literal ``~``
    directory. That branch fires exactly when config is degraded, which is when
    a queue of scheduled background work can least afford to land somewhere
    nothing reads back: the tasks look accepted and vanish on restart.

    Legacy adoption is per FILE, not the directory-level ``legacy=`` of the
    canon: ``<repo>/data`` exists in every checkout, so a directory probe would
    never fall through to the legacy ``src/puffpai/data``. It fires only
    when nothing else decided the location — no configured path, no
    ``$PUFFPAI_DATA_DIR``, and no file of that name at the new path — and it
    points reads AND writes at the old file in place; nothing is copied or
    moved, so migrating stays the operator's decision (the warning is the cue).

    Both suppressors test for an EXPLICIT CHOICE, not for a moved directory. An
    operator who points ``$PUFFPAI_DATA_DIR`` at the default data dir (absolute
    ``<repo>/data``, or the relative value ``"data"``, which joins to the repo
    root) resolves to the shipped directory: comparing paths would read that as
    "unset", adopt, and then rewrite the queue inside the read-only code tree
    while an env var explicitly said otherwise. A non-empty ``paths.state_dir``
    is the same kind of decision and suppresses adoption the same way. The canon
    has this shape too — ``resolve_state_dir`` returns on an absolute
    *configured* or a non-empty ``env_root`` before it ever probes ``legacy``.
    """
    configured = ""
    try:
        from puffpai.config import get_config, resolve_state_dir
        cfg = get_config()
        configured = (cfg.paths.state_dir or "").strip()
        data_dir = resolve_state_dir(
            configured or None,
            repo_root=cfg.REPO_ROOT,
            default=_DEFAULT_DATA_DIR,
        )
    except Exception as exc:  # config must never break scheduled work
        logger.warning("config unavailable for majordomo state path: %s", exc)
        configured = ""
        data_dir = resolve_runtime_dir(_DEFAULT_DATA_DIR)

    new = os.path.join(data_dir, filename)
    legacy = os.path.join(_LEGACY_DATA_DIR, filename)
    env_set = bool(os.environ.get(_STATE_DIR_ENV, "").strip())
    if (not configured and not env_set
            and os.path.normpath(legacy) != os.path.normpath(new)
            and not os.path.exists(new)
            and os.path.isfile(legacy)):
        logger.warning(
            "using legacy majordomo state file %s; move it to %s to silence this",
            legacy, new,
        )
        return legacy
    return new


def get_queue_path() -> str:
    """Return the absolute task-queue path, resolving (and caching) on first use."""
    global QUEUE_PATH
    if QUEUE_PATH is None:
        QUEUE_PATH = _resolve_state_file("majordomo_tasks.jsonl")
    return QUEUE_PATH


def get_briefing_path() -> str:
    """Return the absolute path of the stored daily briefing."""
    global BRIEFING_PATH
    if BRIEFING_PATH is None:
        BRIEFING_PATH = _resolve_state_file("daily_briefing.json")
    return BRIEFING_PATH


def read_briefing() -> dict:
    """Latest stored briefing (``{}`` when none). Used by the dashboard/chat."""
    path = get_briefing_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class MajordomoAgent(BaseAgent):
    """Background/scheduled work owner + scoped task-worker spawner."""

    name = "majordomo"
    scope = AgentScope.MAJORDOMO

    def __init__(self, *, secrets: SecretsProvider | None = None,
                 automation_client=None, queue_path: str | None = None) -> None:
        super().__init__(secrets=secrets)
        self._automation = automation_client
        self._queue_path_override = queue_path

    @property
    def _queue_path(self) -> str:
        return self._queue_path_override or get_queue_path()

    # ── engine accessor (lazy, guarded) ─────────────────────────────
    def _automation_client(self):
        if self._automation is None:
            from puffpai.automation_client import AutomationClient
            self._automation = AutomationClient()
        return self._automation

    # ── persistent queue ────────────────────────────────────────────
    def _append_queue(self, record: dict) -> None:
        with _QUEUE_LOCK:
            os.makedirs(os.path.dirname(self._queue_path), exist_ok=True)
            with open(self._queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")

    def _rewrite_queue(self, records: list[dict]) -> None:
        """Replace the queue file atomically (temp file → ``os.replace``).

        Crash-safety: readers see either the complete old file or the complete
        new one. A crash *between* the write and the replace leaves a stale
        ``.tmp`` beside the queue, which the next rewrite overwrites — it is
        never read.
        """
        path = self._queue_path
        tmp = path + ".tmp"
        with _QUEUE_LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)

    def list_queue(self, limit: int = 100) -> list[dict]:
        """Read recent queued records (newest last)."""
        return self._read_queue()[-limit:]

    def _read_queue(self) -> list[dict]:
        """Every parseable record in the queue file, oldest first."""
        path = self._queue_path
        if not os.path.isfile(path):
            return []
        out: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        out.append(record)
        except OSError:
            return []
        return out

    # ── scheduled tasks (the dispatcher's input) ────────────────────
    def list_tasks(self) -> list[dict]:
        """Every record that is a *scheduled task* (has an id and a schedule)."""
        return [r for r in self._read_queue()
                if r.get("id") and r.get("schedule")]

    def get_task(self, task_id: str) -> dict | None:
        for record in self.list_tasks():
            if record.get("id") == task_id:
                return record
        return None

    def create_task(self, *, intent: str, payload: dict | None = None,
                    target: str = "majordomo", requester: str = "owner",
                    run_at=None, daily_at=None, budget: dict | None = None,
                    state_changing: bool = True,
                    origin: str = TASK_ORIGIN_OWNER) -> dict:
        """Append a scheduled task and return its record.

        **This is the trust boundary for scheduled work**, not the HTTP route.
        Two doors reach it: the authenticated ``POST /agents/tasks`` (origin
        ``owner``) and the agent-facing ``enqueue``/``schedule`` intent (origin
        ``agent``, hard-coded by :meth:`handle` — nothing in a payload can
        promote itself). Every limit the route applies is applied here too, so a
        task minted from an agent message can never exceed what the owner's own
        route would have allowed:

        * the budget is clamped, for both origins, by :func:`clamp_task_budget`;
        * an ``agent``-origin task may only address :data:`AGENT_ORIGIN_TARGETS`
          — no actuator — and may set neither ``state_changing`` nor egress;
        * ``intent``/``target``/``requester`` are length- and charset-bounded;
        * a stale one-shot ``run_at`` is refused (:func:`reject_stale_run_at`).

        An unrecognised *origin* is treated as ``agent``: the restrictive side.

        Raises ``ValueError`` on an unusable schedule, an oversized payload, or a
        parameter this origin may not set. The route surfaces that as a 400 and
        :meth:`handle` as ``{"ok": False, "error": ...}``, rather than storing a
        task that should never have existed.
        """
        origin = (TASK_ORIGIN_OWNER if origin == TASK_ORIGIN_OWNER
                  else TASK_ORIGIN_AGENT)
        intent = _task_field(intent, name="intent", max_len=MAX_TASK_INTENT_CHARS)
        target = _task_field(target or "majordomo", name="target",
                             max_len=MAX_TASK_TARGET_CHARS)
        requester = _task_field(requester or "system", name="requester",
                                max_len=MAX_TASK_REQUESTER_CHARS)
        state_changing = bool(state_changing)
        budget = clamp_task_budget(budget)

        if origin != TASK_ORIGIN_OWNER:
            if target not in AGENT_ORIGIN_TARGETS:
                raise ValueError(
                    f"an agent-created task may only target "
                    f"{sorted(AGENT_ORIGIN_TARGETS)}, not {target!r}; scheduling "
                    "an actuator is for the owner (POST /agents/tasks)")
            if state_changing:
                raise ValueError(
                    "an agent-created task may not declare itself "
                    "state_changing; the owner schedules state-changing work")
            if budget["allow_egress"]:
                raise ValueError(
                    "an agent-created task may not be granted egress")

        payload = dict(payload or {})
        blob = json.dumps(payload, default=str)
        if len(blob.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        next_due = compute_next_due(run_at=run_at, daily_at=daily_at)
        if run_at:
            reject_stale_run_at(next_due)
        schedule = {"run_at": str(run_at)} if run_at else {"daily_at": str(daily_at)}

        record = {
            "id": uuid.uuid4().hex[:12],
            "queued_at": _utcnow(),
            "origin": origin,
            "intent": intent,
            "target": target,
            "requester": requester,
            "payload": payload,
            "state_changing": state_changing,
            "budget": budget,
            "schedule": schedule,
            "next_due": next_due,
            "status": "scheduled",
            "failure_count": 0,
            "run_count": 0,
            "last_error": None,
            "last_run_at": None,
        }
        self._append_queue(record)
        logger.info("majordomo scheduled task %s (%s -> %s, origin %s) next due %s",
                    record["id"], record["intent"], record["target"], origin,
                    next_due)
        return record

    def update_task(self, task_id: str, **fields) -> dict | None:
        """Merge *fields* into one task and rewrite the queue atomically."""
        with _QUEUE_LOCK:
            records = self._read_queue()
            updated = None
            for record in records:
                if record.get("id") == task_id:
                    record.update(fields)
                    updated = record
            if updated is None:
                return None
            self._rewrite_queue(records)
            return updated

    def delete_task(self, task_id: str) -> bool:
        """Remove one task. Returns False when the id is unknown."""
        with _QUEUE_LOCK:
            records = self._read_queue()
            kept = [r for r in records if r.get("id") != task_id]
            if len(kept) == len(records):
                return False
            self._rewrite_queue(kept)
            logger.info("majordomo deleted task %s", task_id)
            return True

    # ── scoped task-worker spawning ─────────────────────────────────
    def spawn_task_worker(self, spec: dict, budget: Budget | None = None) -> Envelope:
        """Mint a scoped :class:`Envelope` for a background task-worker.

        The worker envelope carries a *strict* budget (default: a single action,
        no egress) so a runaway task is contained by the bus. The task is recorded
        in the persistent queue before the envelope is returned; the caller
        dispatches it via ``bus.send(envelope, to=spec["target"])``. For work that
        should fire on a schedule, use :meth:`create_task` instead — those records
        are what :mod:`puffpai.agents.dispatcher` consumes.

        Args:
            spec: ``{"intent": str, "target": str, "payload": dict,
                     "requester": str, "state_changing": bool}``.
            budget: budget for the worker; defaults to the strict single-action
                no-egress budget.
        """
        budget = budget or Budget(max_actions=1, max_seconds=30.0, allow_egress=False)
        envelope = Envelope(
            intent=str(spec.get("intent", "task")),
            payload=dict(spec.get("payload", {})),
            requester=str(spec.get("requester", "majordomo")),
            origin_agent=self.name,
            budget=budget,
            state_changing=bool(spec.get("state_changing", False)),
        )
        self._append_queue({
            "queued_at": _utcnow(),
            "envelope_id": envelope.id,
            "intent": envelope.intent,
            "target": spec.get("target"),
            "requester": envelope.requester,
            "state_changing": envelope.state_changing,
            "budget": budget.snapshot(),
            "status": "queued",
        })
        logger.info("majordomo spawned task-worker %s -> %s (%s)",
                    envelope.id, spec.get("target"), envelope.intent)
        return envelope

    # ── daily briefing ──────────────────────────────────────────────
    def daily_briefing(self, envelope: Envelope) -> dict:
        """Pull the Node-RED ``daily_briefing`` flow and summarize it.

        The flow call is egress-class work charged against the envelope budget;
        the summary is asked of the Concierge *through the bus* (intent ``ask``,
        the intent it already accepts) so the LLM hop is audited like any other.
        The result is stored for the dashboard/chat — it is deliberately **not**
        spoken: the Steward has no ``assist_satellite.announce`` tool.
        """
        p = envelope.payload or {}
        date = str(p.get("date") or datetime.now().strftime("%Y-%m-%d"))
        envelope.budget.consume(1, egress=True)
        ok, data = self._automation_client().call("daily_briefing", {"date": date})
        if not ok:
            logger.warning("daily briefing flow failed: %s", data)
            return {"ok": False, "error": str(data), "date": date}

        text = self._summarize_briefing(envelope, data)
        stored = self.store_briefing(date, text, data)
        logger.info("daily briefing ready for %s (%d chars)", date, len(text))
        return {"ok": True, "date": date, "text": text, "stored_at": stored}

    def _summarize_briefing(self, envelope: Envelope, data) -> str:
        """Ask the Concierge for prose; fall back to a deterministic rendering.

        Engine-less boxes (and a cold model) must still produce a briefing, so a
        missing answer degrades to the flattened flow output rather than an error.
        """
        flat = _flatten_briefing(data)
        try:
            envelope.budget.consume(1)
        except Exception as exc:
            logger.debug("briefing summary skipped: %s", exc)
            return flat
        if self.bus is None or self.bus.get("concierge") is None:
            return flat
        # ``summarize``, not ``ask``: the text below is a report to write prose
        # ABOUT, and it quotes live device readings ("Hallway Thermostat reads
        # 68."). Sent as ``ask`` it went through the Concierge's intent
        # resolution, which read that sentence as a ``set_climate`` to 68F and
        # dispatched it — the briefing moved the setpoint nobody had asked it to
        # touch. The Concierge refuses to resolve this envelope on two counts
        # now (agent origin, and this intent), but the intent is what says so at
        # the call site. ``mode`` stays "chat" as documentation; the router only
        # knows auto/home/general, so it was never what kept this safe.
        sub = Envelope(
            intent="summarize",
            payload={
                "text": "Summarize this household morning report in a few warm, "
                        "plain sentences:\n" + flat,
                "mode": "chat",
            },
            requester=envelope.requester,
            origin_agent=self.name,
            budget=Budget(max_actions=2, max_seconds=45.0, allow_egress=False),
            state_changing=False,
        )
        try:
            reply = self.ask("concierge", sub) or {}
        except Exception as exc:
            logger.warning("briefing summary failed: %s", exc)
            return flat
        answer = reply.get("answer")
        return str(answer)[:MAX_BRIEFING_CHARS] if answer else flat

    def store_briefing(self, date: str, text: str, data=None) -> str:
        """Persist the briefing where the dashboard/chat can read it back."""
        path = get_briefing_path()
        entry = {
            "date": date,
            "text": str(text)[:MAX_BRIEFING_CHARS],
            "generated_at": _utcnow(),
            "source": "nodered:daily_briefing",
        }
        if isinstance(data, dict):
            entry["data"] = data
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entry, f, default=str)
            os.replace(tmp, path)
        except OSError as exc:
            from puffpai.logging_config import log_unwritable_once
            log_unwritable_once(logger, path, "daily briefing", exc)
        return entry["generated_at"]

    # ── request handling ────────────────────────────────────────────
    def handle(self, envelope: Envelope) -> dict:
        intent = envelope.intent
        p = envelope.payload or {}
        try:
            if intent in ("run_workflow", "automation"):
                # Reaching the automation engine is egress-class work; charge it
                # and require the budget to permit egress (Sentinel also
                # veto-checks this).
                envelope.budget.consume(1, egress=True)
                ok, data = self._automation_client().call(p["webhook_name"], p.get("data", {}))
                return {"ok": ok, "data": data}
            if intent in ("daily_briefing", "briefing"):
                return self.daily_briefing(envelope)
            if intent in ("enqueue", "schedule"):
                if p.get("run_at") or p.get("daily_at"):
                    # ``origin`` is HARD-CODED, never read from the payload.
                    # This is the agent-facing door; the owner-facing one is
                    # POST /agents/tasks. A message arriving here cannot mint a
                    # task with parameters the owner's route could not have set,
                    # whatever it claims about itself — see create_task.
                    record = self.create_task(
                        intent=str(p.get("task_intent", "task")),
                        payload=p.get("task_payload") or {},
                        target=str(p.get("target") or "majordomo"),
                        requester=str(envelope.requester),
                        run_at=p.get("run_at"),
                        daily_at=p.get("daily_at"),
                        budget=p.get("budget"),
                        # Default False, not True: an agent-origin task may not
                        # be state-changing at all, so defaulting to True would
                        # make every enqueue a refusal.
                        state_changing=bool(p.get("state_changing", False)),
                        origin=TASK_ORIGIN_AGENT,
                    )
                    return {"ok": True, "queued": True, "task": record}
                # No schedule -> a note in the queue, not something that fires.
                self._append_queue({
                    "queued_at": _utcnow(),
                    "intent": str(p.get("task_intent", "task")),
                    "target": p.get("target"),
                    "requester": str(envelope.requester),
                    "payload_keys": sorted((p.get("task_payload") or {}).keys()),
                    "status": "queued",
                })
                logger.info("majordomo recorded an unscheduled task note "
                            "(no run_at/daily_at, so nothing will dispatch it)")
                return {"ok": True, "queued": True}
            if intent in ("list_queue", "queue"):
                return {"ok": True, "queue": self.list_queue(int(p.get("limit", 100)))}
            if intent in ("list_tasks", "tasks"):
                return {"ok": True, "tasks": self.list_tasks()}
            if intent == "delete_task":
                return {"ok": self.delete_task(str(p["task_id"]))}
            return {"ok": False, "error": f"majordomo: unknown intent {intent!r}"}
        except KeyError as exc:
            return {"ok": False, "error": f"majordomo: missing payload key {exc}"}
        except ValueError as exc:
            return {"ok": False, "error": f"majordomo: {exc}"}

    def health(self) -> dict:
        base = super().health()
        base["queue_path"] = self._queue_path
        base["queued_tasks"] = len(self.list_queue(limit=10_000))
        base["scheduled_tasks"] = len(self.list_tasks())
        return base


#: Budget the seeded morning briefing runs with: one egress action for the
#: Node-RED flow, one for the Concierge summary, headroom for neither to be
#: starved — and long enough that a cold local model can answer.
BRIEFING_BUDGET = {"max_actions": 4, "max_seconds": 180.0, "allow_egress": True}


def seed_briefing_task(daily_at: str = "07:00", *,
                       majordomo: "MajordomoAgent | None" = None) -> dict:
    """Register the recurring morning briefing. Idempotent.

    Returns the existing record when one is already scheduled, so re-running the
    seeder on a box (or on every deploy) cannot stack duplicate briefings.
    """
    agent = majordomo or MajordomoAgent()
    for record in agent.list_tasks():
        if (record.get("intent") == "daily_briefing"
                and record.get("status") != "disabled"):
            return record
    return agent.create_task(
        intent="daily_briefing",
        target="majordomo",
        payload={},
        requester="owner",
        daily_at=daily_at,
        budget=BRIEFING_BUDGET,
        state_changing=True,
    )


def _clamped(value, *, lo, hi, default, cast):
    """Cast *value* and pin it into ``[lo, hi]``; unusable input → *default*.

    Booleans are refused explicitly: ``int(True)`` is 1, so ``max_actions: true``
    would otherwise be read as a valid budget instead of as the bad input it is.
    """
    if isinstance(value, bool) or isinstance(value, (str, bytes)):
        return default
    try:
        number = cast(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN compares false against every bound
        return default
    return max(lo, min(hi, number))


def clamp_task_budget(budget: dict | None) -> dict:
    """Coerce **and clamp** a budget onto the stored three-field shape.

    Clamping rather than merely coercing is the point. Every path that writes a
    task record goes through here, so a stored budget can never name more than
    :data:`MAX_TASK_ACTIONS` actions or :data:`MAX_TASK_SECONDS` seconds no
    matter which route or intent created it. The dispatcher re-clamps on read
    (see :func:`puffpai.agents.dispatcher._budget_from`) so records written
    before this ceiling existed, or by anything that bypassed ``create_task``,
    are bounded too.

    Egress is fail-closed: only a literal ``True`` grants it, so a truthy
    ``"false"`` string cannot open the appliance up.
    """
    merged = dict(DEFAULT_TASK_BUDGET)
    if isinstance(budget, dict):
        if "max_actions" in budget:
            merged["max_actions"] = _clamped(
                budget["max_actions"], lo=MIN_TASK_ACTIONS, hi=MAX_TASK_ACTIONS,
                default=merged["max_actions"], cast=int)
        if "max_seconds" in budget:
            merged["max_seconds"] = _clamped(
                budget["max_seconds"], lo=MIN_TASK_SECONDS, hi=MAX_TASK_SECONDS,
                default=merged["max_seconds"], cast=float)
        if "allow_egress" in budget:
            merged["allow_egress"] = budget["allow_egress"] is True
    return merged


#: Retained name for the pre-ceiling helper; it is the same function now.
_normalize_budget = clamp_task_budget


def _task_field(value, *, name: str, max_len: int) -> str:
    """A stored identifier field: printable, whitespace-collapsed, capped.

    ``intent``/``target``/``requester`` all end up in log lines, audit records
    and routing decisions, so control characters and unbounded length are
    stripped here rather than at each consumer.
    """
    text = " ".join(
        "".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    if not text:
        raise ValueError(f"{name} must be a non-empty string")
    if len(text) > max_len:
        raise ValueError(f"{name} exceeds {max_len} characters")
    return text


def reject_stale_run_at(next_due_iso: str, *, now: datetime | None = None) -> None:
    """Refuse a one-shot whose moment is further past than the skew window.

    A ``run_at`` in the past is otherwise due the instant it is stored and fires
    on the next tick — indistinguishable, from the household's point of view,
    from an action nobody scheduled. Recurring ``daily_at`` tasks cannot hit
    this: :func:`next_daily_due` always lands in the future.
    """
    behind = ((now or utcnow_dt()) - parse_run_at(next_due_iso)).total_seconds()
    if behind > MAX_RUN_AT_BACKDATE_SECONDS:
        raise ValueError(
            f"run_at is {int(behind)}s in the past; anything more than "
            f"{MAX_RUN_AT_BACKDATE_SECONDS}s stale would fire immediately on the "
            "next tick rather than at a scheduled time")


def _flatten_briefing(data) -> str:
    """Render the flow's output as short ``key: value`` lines, length-capped."""
    if isinstance(data, str):
        return data[:MAX_BRIEFING_CHARS]
    if not isinstance(data, dict):
        return str(data)[:MAX_BRIEFING_CHARS]
    lines = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str)
        lines.append(f"{key}: {str(value)[:400]}")
    return "\n".join(lines)[:MAX_BRIEFING_CHARS]


__all__ = [
    "AGENT_ORIGIN_TARGETS",
    "BRIEFING_BUDGET",
    "BRIEFING_PATH",
    "DEFAULT_TASK_BUDGET",
    "MAX_BRIEFING_CHARS",
    "MAX_PAYLOAD_BYTES",
    "MAX_RUN_AT_BACKDATE_SECONDS",
    "MAX_TASK_ACTIONS",
    "MAX_TASK_INTENT_CHARS",
    "MAX_TASK_REQUESTER_CHARS",
    "MAX_TASK_SECONDS",
    "MAX_TASK_TARGET_CHARS",
    "MIN_TASK_ACTIONS",
    "MIN_TASK_SECONDS",
    "TASK_ORIGIN_AGENT",
    "TASK_ORIGIN_OWNER",
    "MajordomoAgent",
    "QUEUE_PATH",
    "clamp_task_budget",
    "compute_next_due",
    "reject_stale_run_at",
    "get_briefing_path",
    "get_queue_path",
    "is_due",
    "is_valid_task_id",
    "next_daily_due",
    "parse_daily_at",
    "parse_run_at",
    "read_briefing",
    "seed_briefing_task",
    "utcnow_dt",
]
