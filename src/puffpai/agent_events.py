"""Event intake — the agent team's senses (orchestration Phase 1).

Without this module the agent team acts only when a human types or speaks.
This module pushes the world at it: the single entry point every event producer uses
— the HA WebSocket listener (:mod:`puffpai.ha_events`), the Device Sentinel, and
Node-RED via ``POST /agents/event``.

An event becomes an :class:`~puffpai.agents.envelope.Envelope` with
``intent="event"``, ``state_changing=False`` and a deliberately tight budget
(2 actions / 15 s / no egress), and is sent to the Concierge *through the bus*,
so Sentinel's review and the audit ledger stay in the path exactly as they are
for a chat message.

Two properties matter for callers:

* :func:`submit_agent_event` **never raises**. A sensor thread or the Device
  Sentinel must not die because the agent team is off, wedged, or misconfigured;
  failures come back as ``{"ok": False, "error": ...}`` and are logged.
* The event log is **bounded** (:data:`MAX_EVENT_LOG_ENTRIES`) and stores a
  truncated copy of the payload, so a chatty sensor cannot fill the disk.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from puffpai.logging_config import get_logger, log_unwritable_once
# Safe at module scope: statedir is a stdlib-only leaf that defers its
# puffpai.config import (and with it the secrets provider) to call time, so
# importing it here adds nothing to this module's import graph. _STATE_DIR_ENV
# is the env-var NAME only, and tests/test_statedir.py pins it to
# config.STATE_DIR_ENV, so reading it here cannot drift from canon.
from puffpai.statedir import _STATE_DIR_ENV, resolve_runtime_dir

logger = get_logger(__name__)

#: Repo root — three levels up from ``src/puffpai/agent_events.py``. A RELATIVE
#: ``$PUFFPAI_DATA_DIR`` is resolved against it, exactly as
#: :func:`puffpai.config.resolve_state_dir` does (and as
#: ``device_sentinel._REPO_ROOT`` does for the sentinel's baseline).
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Canonical default: the repo-root ``data`` directory, not the
#: package-relative ``src/puffpai/data``, for convergence:
#: ha_device_watcher, zone3/policy_engine and
#: security/consent_gate all land in ``<repo>/data``, and runtime state
#: scattered across several directories is exactly what the shared resolver
#: exists to prevent: it is how one module writes a trail another module never
#: finds. This is still only the *fallback*. ``<repo>/data`` is inside the
#: checkout and equally read-only under ``ProtectSystem=strict``, so a real
#: deployment MUST set ``paths.agent_events_dir`` / ``paths.state_dir``
#: (absolute) or ``$PUFFPAI_DATA_DIR``. See
#: :func:`puffpai.config.resolve_state_dir`.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

#: The legacy default, ``src/puffpai/data``. Nothing is written here;
#: an event log an earlier release left behind is adopted IN PLACE by
#: :func:`_resolve_event_log_path` — never copied, never moved — so an upgraded
#: box keeps its history and ``GET /agents/events`` does not come back empty.
_LEGACY_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

#: Absolute path to the event log, resolved on first use and cached here.
#: ``None`` means "not resolved yet"; set it to a path to override (tests do),
#: or back to ``None`` to force re-resolution.
EVENT_LOG_PATH: str | None = None

#: Hard cap on retained log lines; the file is trimmed to this on overflow.
MAX_EVENT_LOG_ENTRIES = 2000
#: Ceiling on the serialized ``data`` payload (matches the route's validation).
MAX_DATA_BYTES = 8192

_LOG_LOCK = threading.Lock()
_line_count: int | None = None  # lazily counted once, then tracked in-process

_bus = None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── event-log location ──────────────────────────────────────────────

def _resolve_event_log_path() -> str:
    """Compute the event-log path from config / env / default, adopting legacy.

    Resolved lazily rather than at import: this module is imported by
    ``api_server`` and by background threads, and ``puffpai.config`` pulls in
    the secrets provider (which reaches the vault), so importing it at module
    scope would risk a cycle. (:mod:`puffpai.statedir` is imported at module
    scope precisely because it is stdlib-only and defers that import too.)

    The CONFIGURED tier stays a direct :func:`~puffpai.config.resolve_state_dir`
    call: this module is unique in honouring ``paths.agent_events_dir`` /
    ``paths.state_dir``, and :func:`~puffpai.statedir.resolve_runtime_dir`
    always passes ``configured=None``. Everything below it collapses into that
    one leaf call. That collapse is safe because the leaf IS both of the old
    degraded tiers: where the config imported but ``get_config()`` raised, the
    old code fell back to ``resolve_state_dir(None, ...)`` and the leaf reaches
    exactly that canon; where the config was unimportable, the old code used a
    local replica and the leaf uses its own — differential-tested against the
    canon in ``tests/test_statedir.py`` rather than trusted per module.

    A config failure never changes *where* ``$PUFFPAI_DATA_DIR`` points. The
    original code read the env var raw on that tier (no strip, no ``~``
    expansion, no repo-root join for a relative value), which put the event log
    somewhere no other module looked — and, under ``ProtectSystem=strict``,
    potentially outside the writable root, where the append then failed.

    Legacy adoption is per FILE, not the directory-level ``legacy=`` of the
    canon: ``<repo>/data`` exists in every checkout, so a directory probe would
    never fall through to the legacy ``src/puffpai/data``. It fires
    only when nothing else decided the location — no configured path, no
    ``$PUFFPAI_DATA_DIR``, and no log at the new path — and it points reads AND
    appends at the old file in place; nothing is copied or moved, so migrating
    stays the operator's decision (the warning is the cue).

    Both suppressors test for an EXPLICIT CHOICE, not for a moved directory. An
    operator who points ``$PUFFPAI_DATA_DIR`` at the default data dir (absolute
    ``<repo>/data``, or the relative value ``"data"``, which joins to the repo
    root) resolves to the shipped directory: comparing paths would read that as
    "unset", adopt, and then append to the read-only code tree while an env var
    explicitly said otherwise. A non-empty ``paths.agent_events_dir`` /
    ``paths.state_dir`` is the same kind of decision and suppresses adoption
    the same way. The canon has this shape too — ``resolve_state_dir`` returns
    on an absolute *configured* or a non-empty ``env_root`` before it ever
    probes ``legacy``.
    """
    configured = ""
    try:
        from puffpai.config import get_config, resolve_state_dir
        cfg = get_config()
        configured = (cfg.paths.agent_events_dir or cfg.paths.state_dir or "").strip()
        data_dir = resolve_state_dir(
            configured or None,
            repo_root=cfg.REPO_ROOT,
            default=_DEFAULT_DATA_DIR,
        )
    except Exception as exc:  # a broken/unimportable config must never break intake
        logger.warning("config unavailable for event-log path: %s", exc)
        configured = ""
        data_dir = resolve_runtime_dir(_DEFAULT_DATA_DIR)

    new = os.path.join(data_dir, "agent_events.jsonl")
    legacy = os.path.join(_LEGACY_DATA_DIR, "agent_events.jsonl")
    env_set = bool(os.environ.get(_STATE_DIR_ENV, "").strip())
    if (not configured and not env_set
            and os.path.normpath(legacy) != os.path.normpath(new)
            and not os.path.exists(new)
            and os.path.isfile(legacy)):
        logger.warning(
            "using legacy agent event log %s; move it to %s to silence this",
            legacy, new,
        )
        return legacy
    return new


def get_event_log_path() -> str:
    """Return the absolute event-log path, resolving (and caching) on first use."""
    global EVENT_LOG_PATH
    if EVENT_LOG_PATH is None:
        EVENT_LOG_PATH = _resolve_event_log_path()
    return EVENT_LOG_PATH


# ── bus wiring ──────────────────────────────────────────────────────

def set_bus(bus) -> None:
    """Adopt an already-built :class:`~puffpai.agents.bus.AgentBus`.

    ``api_server`` calls this so events and chat share one bus (one audit
    ledger, one Sentinel) instead of the module quietly building a second team.
    """
    global _bus
    _bus = bus


def get_bus():
    """Return the shared agent bus, building one lazily if needed.

    Returns ``None`` when the team is disabled or fails to construct — the
    caller degrades to log-only rather than failing.
    """
    global _bus
    if _bus is not None:
        return _bus
    if os.environ.get("PUFFPAI_AGENT_TEAM_ENABLED", "1").lower() not in (
        "1", "true", "yes",
    ):
        return None
    try:
        from puffpai.agents import build_team
        _bus = build_team()
    except Exception as exc:
        logger.warning("agent bus unavailable for events: %s", exc)
        _bus = None
    return _bus


# ── event log (bounded JSONL) ───────────────────────────────────────

def _truncate_data(data: dict) -> dict:
    """Return *data* if it serializes small enough, else a keys-only stand-in."""
    try:
        blob = json.dumps(data, default=str)
    except Exception:
        return {"_unserializable": True}
    if len(blob.encode("utf-8")) <= MAX_DATA_BYTES:
        return data
    return {"_truncated": True, "keys": sorted(str(k) for k in data.keys())}


def _trim_locked() -> None:
    """Keep only the newest :data:`MAX_EVENT_LOG_ENTRIES` lines. Caller holds the lock."""
    global _line_count
    path = get_event_log_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    keep = lines[-MAX_EVENT_LOG_ENTRIES:]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except OSError:
        return
    _line_count = len(keep)


def _append_event(entry: dict) -> None:
    """Append one event record, trimming the log when it overflows the cap."""
    global _line_count
    path = get_event_log_path()
    with _LOG_LOCK:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if _line_count is None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _line_count = sum(1 for _ in f)
            except OSError:
                _line_count = 0
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        _line_count += 1
        if _line_count > MAX_EVENT_LOG_ENTRIES:
            _trim_locked()


def read_events(limit: int = 50) -> list[dict]:
    """Return the most recent event-log entries, newest first."""
    path = get_event_log_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


# ── human escalation channel ────────────────────────────────────────

#: HA ``notify.<service>`` used for escalations. The default is always present
#: and shows in the HA UI on every household screen; set
#: ``PUFFPAI_NOTIFY_SERVICE=mobile_app_<device>`` to reach a phone instead.
DEFAULT_NOTIFY_SERVICE = "persistent_notification"

#: At most one push per event_type in this window — a flapping sensor must not
#: turn the escalation channel into noise nobody reads.
NOTIFY_MIN_INTERVAL_SECONDS = 600

_last_notified: dict[str, float] = {}


def _should_notify(decision) -> bool:
    """A fresh fault a human must know about: needs_human, or attention-class.

    Duplicates never notify — the Concierge's dedup already folded them, and
    re-pushing an unchanged fault is how an escalation channel gets muted.
    """
    if not isinstance(decision, dict):
        return False
    if decision.get("duplicate"):
        return False
    return (decision.get("suggested_action") == "needs_human"
            or decision.get("severity") == "attention")


def _notify_human(event_type: str, decision: dict) -> dict:
    """Push the decision at a human via the Steward. Never raises.

    The notification travels the bus like any other action — Sentinel review
    and the audit ledger apply. This module holds no credential and no HA
    client; if the team is down, the log entry records the skip instead.
    (The per-type rate limit races benignly across threads: the worst case is
    one extra notification, never a lost one.)
    """
    import time as _time

    now = _time.monotonic()
    if len(_last_notified) > 256:
        # Keyed by caller-supplied event_type: bound it so an authenticated
        # spammer cannot grow this dict forever (drop the oldest half).
        for k in sorted(_last_notified, key=_last_notified.get)[:128]:
            del _last_notified[k]
    last = _last_notified.get(event_type)
    if last is not None and (now - last) < NOTIFY_MIN_INTERVAL_SECONDS:
        return {"skipped": "rate_limited"}

    bus = get_bus()
    if bus is None:
        return {"skipped": "bus unavailable"}

    summary = str(decision.get("summary") or event_type)[:400]
    suggested = str(decision.get("suggested_action") or "")
    message = f"{summary} — suggested: {suggested}" if suggested else summary
    try:
        from puffpai.agents import Budget, Envelope
        reply = bus.send(
            Envelope(
                intent="notify_user",
                payload={
                    "service": (os.environ.get("PUFFPAI_NOTIFY_SERVICE", "").strip()
                                or DEFAULT_NOTIFY_SERVICE),
                    "title": f"PuffPAI: {event_type.replace('_', ' ')}",
                    "message": message,
                },
                requester="system",
                origin_agent="concierge",
                budget=Budget(max_actions=2, max_seconds=10.0, allow_egress=False),
                state_changing=True,
            ),
            to="steward",
        )
    except Exception as exc:
        logger.warning("escalation notify failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    if isinstance(reply, dict) and reply.get("ok"):
        _last_notified[event_type] = now
    return reply if isinstance(reply, dict) else {"ok": False, "error": "no reply"}


# ── intake ──────────────────────────────────────────────────────────

def submit_agent_event(source: str, event_type: str, data: dict | None = None) -> dict:
    """Wrap an event in a budgeted envelope, route it to the Concierge, log it.

    Args:
        source: who observed it (``"ha"``, ``"device_sentinel"``, ``"nodered"``).
        event_type: ``camera_person`` | ``camera_motion`` | ``network_new_device``
            | ``weather_alert`` | anything else (classified as informational).
        data: free-form observation payload; truncated in the log.

    Returns the Concierge's reply dict, or ``{"ok": False, "error": ...}``.
    Never raises — event producers are background threads.
    """
    source = str(source)
    event_type = str(event_type)
    payload_data = dict(data or {})

    decision: dict
    try:
        bus = get_bus()
        if bus is None:
            decision = {"ok": False, "error": "agent team unavailable"}
        else:
            from puffpai.agents import Budget, Envelope
            envelope = Envelope(
                intent="event",
                payload={
                    "source": source,
                    "event_type": event_type,
                    "data": payload_data,
                },
                requester="system",
                origin_agent=source,
                budget=Budget(max_actions=2, max_seconds=15.0, allow_egress=False),
                state_changing=False,
            )
            decision = bus.send(envelope, to="concierge")
    except Exception as exc:  # a sensor thread must never die on our account
        logger.warning("event %s/%s failed to route: %s", source, event_type, exc)
        decision = {"ok": False, "error": str(exc)}

    # Escalation: a fresh needs_human / attention decision must reach a person,
    # not just this file, which nobody reads. The Concierge's event reply nests the verdict under "decision"
    # ({ok, source, event_type, decision:{severity, ...}}); unwrap before
    # judging, but accept a flat dict too. Outcome lands on the event entry.
    notified: dict | None = None
    verdict = decision
    if isinstance(decision, dict) and isinstance(decision.get("decision"), dict):
        verdict = decision["decision"]
    if _should_notify(verdict):
        notified = _notify_human(event_type, verdict)

    entry = {
        "ts": _utcnow(),
        "source": source,
        "event_type": event_type,
        "data": _truncate_data(payload_data),
        "decision": decision,
    }
    if notified is not None:
        entry["notified"] = notified
    entity_id = payload_data.get("entity_id")
    if entity_id:
        entry["entity_id"] = str(entity_id)
    try:
        _append_event(entry)
    except OSError as exc:
        # A read-only / missing state directory is a deployment fault: it will
        # not heal, and events arrive on a timer. Say so once, loudly, naming
        # the path — then keep returning decisions with the log degraded.
        log_unwritable_once(logger, get_event_log_path(), "agent event log", exc)
    except Exception as exc:
        logger.warning("event log append failed: %s", exc)

    return decision


__all__ = [
    "EVENT_LOG_PATH",
    "MAX_DATA_BYTES",
    "MAX_EVENT_LOG_ENTRIES",
    "get_bus",
    "get_event_log_path",
    "read_events",
    "set_bus",
    "submit_agent_event",
]
