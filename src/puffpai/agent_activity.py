"""The Agent Team activity feed — one file, one resolver, one owner.

``agent-activity.jsonl`` is the feed behind ``GET /agents/activity`` and behind
the per-agent rows of ``GET /agents/status`` (which keys on ``entry["agent"]``
and treats a ``queued``/``working``/``running`` line younger than 24h as live
work). It is the household-visible record of what the agents did — including
the two lines that matter most on a security appliance: *Sentinel asked the
user to approve device X*, and *the user granted or denied it*.

Why the feed needs an owner module of its own
---------------------------------------------
It has writers on both sides of an import wall. :mod:`puffpai.api_server`
writes a dozen kinds of entry and is the only READER;
:mod:`puffpai.security.consent_requests` writes the consent pair and sits far
below the server in the import graph, so it cannot ask the server where the
feed lives. Left to answer that question for themselves, they disagree:

* the server resolves through :func:`puffpai.config.resolve_state_dir`,
  honouring ``paths.agent_logs_dir`` / ``paths.state_dir``;
* a path computed relative to
  ``src/puffpai/security/consent_requests.py`` lands outside the state
  directory, on the appliance in a directory that does not exist and cannot
  be created under ``ProtectSystem=strict``. The append fails, and every
  consent request and every consent verdict goes missing from the feed.

A writer and a reader that resolve separately will drift on some configuration
even when both are "fixed": point ``paths.state_dir`` at ``/data/puffpai``
without setting ``$PUFFPAI_DATA_DIR`` (the appliance's own shape) and a consent
loop that only consulted the env var would write inside the read-only code tree
while the server read somewhere else entirely. So resolution lives here, once,
and both sides import it.

Where it lands
--------------
``<repo>/data`` by default: the directory ``consent_gate``,
``ha_device_watcher``, ``zone3/policy_engine`` and ``agent_events`` all use.
The activity feed is runtime state like the rest of them, and it sits beside
``agent_events.jsonl``. The default is only a fallback: ``<repo>/data``
is read-only on a hardened unit too, which is why a deployment sets
``paths.agent_logs_dir`` / ``paths.state_dir`` (absolute) or
``$PUFFPAI_DATA_DIR``.

It is deliberately NOT merged into ``agent_events.jsonl``. That log is the bus
intake trail (``{ts, source, event_type, data, decision}``, capped at 2000
lines); this one is the agent-status feed (``{agent, event, task, status,
timestamp, result}``) that ``/agents/status`` reads by agent name. They answer
different questions for different consumers, and folding one writer of fourteen
into the other log would have removed consent from the feed rather than
restored it.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from puffpai.logging_config import get_logger, log_unwritable_once
# Both safe at module scope: logging_config and statedir are stdlib-only leaves
# that defer their puffpai.config import (and with it the secrets provider) to
# call time, which is what lets a module as low as consent_requests import this
# one.
from puffpai.statedir import resolve_runtime_dir

logger = get_logger(__name__)

#: Repo root — three levels up from ``src/puffpai/agent_activity.py``.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Canonical default: the repo-root ``data`` directory, shared with
#: ``agent_events``, ``consent_gate`` and the rest of the state writers.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

#: The feed's filename. ``task-queue.jsonl`` is the other agent log and is
#: resolved the same way.
ACTIVITY_LOG_NAME = "agent-activity.jsonl"

#: Rotation policy for this feed. ``chat_activity`` writes a
#: ``turn_handled`` line per (turn, agent) with no bound, so this feed needs
#: the same size cap its sibling security logs already have
#: (``puffpai.security.egress_ledger``). The threshold here is smaller than
#: that ledger's ``MAX_BYTES`` (a busy chat/voice session turns over far
#: faster than door decisions do), so the constants are mirrored rather than
#: imported — the rotation *shape* (roll, cap generations, atomic rename) is
#: the same, the *numbers* are this feed's own.
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

_path_cache: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── location ────────────────────────────────────────────────────────

def _resolve_dir() -> str:
    """Return the directory the agent logs live in.

    Resolved lazily — never at import of this module's callers — because
    :mod:`puffpai.config` pulls in the secrets provider, and dragging that into
    the import graph of a security leaf is the cycle risk
    :mod:`puffpai.statedir` exists to avoid.

    The CONFIGURED tier stays a direct :func:`~puffpai.config.resolve_state_dir`
    call because this feed honours ``paths.agent_logs_dir`` /
    ``paths.state_dir``, and :func:`~puffpai.statedir.resolve_runtime_dir`
    always passes ``configured=None``. When the config is unimportable or
    broken, the leaf gives the same answer for every tier below that one — it
    IS the canon's ``configured=None`` path, differential-tested against it in
    ``tests/test_statedir.py``.
    """
    try:
        from puffpai.config import get_config, resolve_state_dir
        cfg = get_config()
        configured = (cfg.paths.agent_logs_dir or cfg.paths.state_dir or "").strip()
        return resolve_state_dir(
            configured or None,
            repo_root=cfg.REPO_ROOT,
            default=_DEFAULT_DATA_DIR,
        )
    except Exception as exc:  # a broken config must never break the feed
        logger.warning("config unavailable for agent-log path: %s", exc)
        return resolve_runtime_dir(_DEFAULT_DATA_DIR)


def resolve_agent_log(name: str) -> str:
    """Absolute path of one agent log file.

    The result is cached per name. Tests that move the environment call
    :func:`reset_cache`.
    """
    hit = _path_cache.get(name)
    if hit is not None:
        return hit

    resolved = os.path.join(_resolve_dir(), name)

    with _CACHE_LOCK:
        # Racing resolvers compute the same answer; first one in wins so every
        # caller in this process sees one path.
        return _path_cache.setdefault(name, resolved)


def get_activity_log_path() -> str:
    """Absolute path of ``agent-activity.jsonl``."""
    return resolve_agent_log(ACTIVITY_LOG_NAME)


def agent_logs_dir() -> str:
    """Directory the agent logs resolve into."""
    return _resolve_dir()


def reset_cache() -> None:
    """Drop the resolved-path cache so the next call re-reads the environment."""
    with _CACHE_LOCK:
        _path_cache.clear()


# ── writing ─────────────────────────────────────────────────────────

def _rotate_if_needed(path: str) -> "int | None":
    """Roll *path* over once it reaches :data:`MAX_BYTES`.

    ``agent-activity.jsonl`` -> ``agent-activity.1.jsonl`` -> ... ->
    ``agent-activity.3.jsonl``, oldest generation dropped, mirroring
    :func:`puffpai.security.egress_ledger._rotate_if_needed` (same shape,
    this feed's own threshold — see :data:`MAX_BYTES`). Called with
    :data:`_WRITE_LOCK` held.

    This feed sets no fixed file mode of its own (unlike the egress ledger's
    0640), so rather than invent one, whatever mode the live file already
    has is captured before it is rolled away and returned so the caller can
    re-apply it to the new file ``open("a", ...)`` creates — the rotated
    generation keeps the mode it already had, only the fresh file needs it
    restored.
    """
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return None
    except OSError:
        return None  # not there yet, or unreadable — nothing to roll

    try:
        preserved_mode = os.stat(path).st_mode & 0o777
    except OSError:
        preserved_mode = None

    root, ext = os.path.splitext(path)
    for index in range(BACKUP_COUNT, 0, -1):
        src = f"{root}.{index}{ext}"
        if not os.path.exists(src):
            continue
        if index == BACKUP_COUNT:
            try:
                os.remove(src)
            except OSError:
                pass
        else:
            try:
                os.replace(src, f"{root}.{index + 1}{ext}")
            except OSError:
                pass
    try:
        os.replace(path, f"{root}.1{ext}")
    except OSError:
        return None
    return preserved_mode


def append(entry: dict, *, path: str | None = None) -> bool:
    """Append one entry to the feed. Never raises; returns whether it landed.

    The feed RECORDS what the agents did, it does not participate in doing it,
    so a failed append must not turn a consent prompt or a dashboard action
    into an error. What it must not do is stay silent: an unwritable
    path here is a deployment fault that will not heal, and this feed is the
    only place a household sees that Sentinel asked for consent at all. Hence
    :func:`~puffpai.logging_config.log_unwritable_once`, which names the path
    and the remedy once per process rather than once per entry on a box that is
    already in trouble.
    """
    target = path or get_activity_log_path()
    try:
        with _WRITE_LOCK:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            preserved_mode = _rotate_if_needed(target)
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            if preserved_mode is not None:
                try:
                    os.chmod(target, preserved_mode)
                except OSError:
                    pass
    except OSError as exc:
        log_unwritable_once(logger, target, "agent activity feed", exc)
        return False
    except Exception as exc:  # serialization or anything else — still not fatal
        logger.warning("agent activity append failed: %s", exc)
        return False
    return True


def log_activity(agent: str, event: str, task: str, status: str,
                 result: dict | None = None, *, path: str | None = None) -> bool:
    """Record one activity entry in the feed's schema. Never raises.

    Schema is fixed by its readers: ``/agents/status`` groups by ``agent`` and
    ages out non-terminal ``status`` values, ``/agents/activity`` renders the
    rest verbatim.
    """
    entry = {"agent": agent, "event": event, "task": task, "status": status,
             "timestamp": _utcnow()}
    if result is not None:
        entry["result"] = result
    return append(entry, path=path)


__all__ = [
    "ACTIVITY_LOG_NAME",
    "BACKUP_COUNT",
    "MAX_BYTES",
    "agent_logs_dir",
    "append",
    "get_activity_log_path",
    "log_activity",
    "reset_cache",
    "resolve_agent_log",
]
