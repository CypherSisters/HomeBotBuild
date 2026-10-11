"""
Sentinel consent requests — the "ask the user on Sentinel's behalf" loop.

When an agent action is blocked because a device has no per-device consent,
Sentinel doesn't just deny: it files a consent request here, the request is
logged to the Agent Team activity feed, and the LLM asks the user directly
in chat. A yes/no reply resolves the request via the assistant tools
``sentinel_approve_device`` / ``sentinel_deny_device`` — approval is recorded
in the ConsentStore as granted by the user (by="user_via_chat"), never by the
model on its own initiative. The tools can only act on entities that have an
OPEN request, so the LLM cannot self-approve arbitrary devices.

Persists at <repo>/data/consent_requests.json (see below). The
activity feed those entries go to is owned by :mod:`puffpai.agent_activity`,
which resolves it through the same state-dir canon.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

# statedir is a stdlib-only leaf: importing it at module scope pulls in nothing,
# because it defers the puffpai.config import (and the secrets provider behind
# it) to call time. _STATE_DIR_ENV is the env-var NAME only; tests/test_statedir
# .py pins it to config.STATE_DIR_ENV, so reading it here cannot drift from
# canon.
from puffpai.statedir import _STATE_DIR_ENV, resolve_runtime_dir
# Same deal: agent_activity imports only stdlib plus the two leaves above, so
# owning the activity-feed path there costs this module nothing at import. Its
# resolver is still called lazily, on first write.
from puffpai import agent_activity

logger = logging.getLogger(__name__)

# ── state location (lazy) ─────────────────────────────────────────────
#
# Same rule as consent_gate: resolving the request queue at import from a
# raw os.environ.get("PUFFPAI_DATA_DIR", ...) — no strip, no "~" expansion, a
# relative value taken against the process CWD, would let a hardened
# deployment park pending consent requests outside its writable root, where
# the user is never asked and the answer is never recorded. It resolves on
# first use through puffpai.statedir, the stdlib-only leaf that reaches the
# canon (config.resolve_state_dir) at call time.
#
# The env-unset DEFAULT is the <repo>/data that consent_gate,
# ha_device_watcher and zone3/policy_engine use. This queue and consent_gate's
# ledger are two halves of one decision record, resolve() closes the request
# here and then writes the verdict through consent_gate.get_store(), so
# splitting them across two directories would mean an operator who relocated
# or backed up one got half the record, and a restored appliance could show a
# consent request that was already answered. A queue in the legacy
# <repo>/src/puffpai/data directory is not orphaned: it is adopted
# individually in _requests_path() below.

#: Repo root — four directory levels up from
#: ``src/puffpai/security/consent_requests.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: Canonical default: the repo-root ``data`` directory, shared with
#: consent_gate and the rest of the state-writing modules. Read-only on a
#: hardened deployment, which is why such a deployment sets
#: ``$PUFFPAI_DATA_DIR`` (or an absolute ``paths.*`` entry).
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

#: The legacy default, ``src/puffpai/data``. Nothing is written here;
#: a queue an earlier release left behind is adopted by
#: :func:`_requests_path`, which only points reads and writes at it in place —
#: nothing is copied or moved, so migrating stays the operator's decision.
_LEGACY_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

#: Resolved data directory; ``None`` = not resolved yet. A reload re-runs this
#: assignment, which is how the tests re-resolve after setting the env var.
_data_dir_cache: str | None = None

# ``REQUESTS_PATH`` and ``ACTIVITY_LOG`` are served lazily by ``__getattr__``
# below; assigning either (as a test override would) shadows that, so drop any
# leftover on reload.
globals().pop("REQUESTS_PATH", None)
globals().pop("ACTIVITY_LOG", None)


def _data_dir() -> str:
    """Directory holding the request queue, resolved on first use and cached.

    This MUST stay lazy — never resolved at import. :mod:`puffpai.statedir` is
    a stdlib-only leaf, but the resolution it performs imports
    ``puffpai.config`` (which reaches the secrets provider), so running it while
    this module is still being imported would drag config into the import graph
    and re-create the cycle risk the leaf exists to avoid.

    The ``legacy=`` handed over here is the canon's DIRECTORY-level adoption,
    which fires only when ``<repo>/data`` does not exist at all. Every checkout
    ships one, so on a real box it effectively never fires; the per-file
    adoption in :func:`_requests_path` is what actually rescues the pending
    queue on an upgraded box.
    """
    global _data_dir_cache
    if _data_dir_cache is None:
        _data_dir_cache = resolve_runtime_dir(
            _DEFAULT_DATA_DIR, legacy=_LEGACY_DATA_DIR)
    return _data_dir_cache


def _requests_path() -> str:
    """Absolute path of the queue file, with file-level legacy adoption.

    An explicit ``REQUESTS_PATH`` assignment wins over everything, adoption
    included: it is the test/ops override, and it is checked first.

    Adoption is per FILE rather than the directory-level ``legacy=`` of the
    canon, because ``<repo>/data`` exists in every checkout and a directory
    probe would therefore never fall through to the legacy
    ``src/puffpai/data``. It fires only when nothing else decided the location —
    no override, no ``$PUFFPAI_DATA_DIR``, and no queue at the new path — and it
    only points reads and writes at the old file in place; nothing is copied,
    moved, or created there, so the warning is the operator's cue to migrate it.

    The env test is "is the var set", NOT "did the resolved dir move". Those
    differ exactly when an operator points $PUFFPAI_DATA_DIR at the default data
    dir (absolute <repo>/data, or the relative value "data", which joins to the
    repo root): a dir comparison reads that as "unset", adopts, and the queue is
    then read AND written inside the read-only code tree while an env var says
    otherwise. The canon has the same shape — resolve_state_dir returns on a
    non-empty env_root before it ever probes legacy.

    The normpath inequality is a separate guard: when the directory-level
    ``legacy=`` in :func:`_data_dir` already resolved to the legacy directory,
    the two are the same file and there is nothing to adopt.
    """
    override = globals().get("REQUESTS_PATH")
    if isinstance(override, str) and override:
        return override
    new = os.path.join(_data_dir(), "consent_requests.json")
    legacy = os.path.join(_LEGACY_DATA_DIR, "consent_requests.json")
    env_set = bool(os.environ.get(_STATE_DIR_ENV, "").strip())
    if (not env_set
            and os.path.normpath(legacy) != os.path.normpath(new)
            and not os.path.exists(new)
            and os.path.isfile(legacy)):
        logger.warning(
            "using legacy consent-request queue %s; move it to %s to silence "
            "this", legacy, new,
        )
        return legacy
    return new


def _activity_log_path() -> str:
    """Absolute path of the Agent Team activity feed, resolved on first use.

    An explicit ``ACTIVITY_LOG`` assignment wins, like ``REQUESTS_PATH`` above:
    it is the test/ops override, so it is checked first.

    Everything else is :mod:`puffpai.agent_activity`'s decision, not this
    module's. A path derived here by walking directory levels up from this
    file would land outside the checkout, in a directory that does not exist
    and cannot be created under ``ProtectSystem=strict``: every append would
    raise, an ``except OSError`` would swallow it, and the feed that is
    supposed to show "Sentinel asked to approve device X" and "the user
    answered" would be empty on the appliance, silently.

    Resolution is shared with ``api_server`` rather than re-derived here because
    that module is the feed's only READER: a writer and a reader resolving
    separately drift apart on any configuration one of them does not consult
    (``paths.state_dir`` set without ``$PUFFPAI_DATA_DIR`` is the appliance's
    own shape), and a feed written where nothing reads is the same outage in a
    different directory.
    """
    override = globals().get("ACTIVITY_LOG")
    if isinstance(override, str) and override:
        return override
    return agent_activity.get_activity_log_path()


def __getattr__(name: str) -> str:
    """Serve the former path constants (PEP 562) lazily.

    ``puffpai.self_check`` reads ``REQUESTS_PATH`` as an attribute at call time
    to report where pending consent requests live; ``ACTIVITY_LOG`` is served
    the same way so ops and tests can still read (and override) it.
    """
    if name == "REQUESTS_PATH":
        return _requests_path()
    if name == "ACTIVITY_LOG":
        return _activity_log_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_LOCK = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict]:
    try:
        with open(_requests_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    path = _requests_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def log_agent_activity(agent: str, event: str, task: str, status: str,
                       result: dict | None = None) -> None:
    """Append to the Agent Team activity feed (same schema as agent-router).

    Still never raises — the feed records the consent conversation, it is not
    part of holding it, so a dead feed must not stop the user being asked. It no
    longer fails *silently*, though: :func:`puffpai.agent_activity.append` names
    the unwritable path once per process instead of dropping the entry with a
    bare ``pass``.
    """
    agent_activity.log_activity(agent, event, task, status, result,
                                path=_activity_log_path())


def create_request(entity_id: str, label: str, action: str, reason: str,
                   requested_by: str = "sentinel", context: dict | None = None,
                   ttl_seconds: float | None = None) -> dict:
    """File (or refresh) an open consent request for entity+action.

    ``ttl_seconds`` opts the request into auto-expiry: an ``expires_at`` stamp is
    stored, and once it passes an unanswered request is flipped to
    ``status="expired"`` by :func:`_expire_stale` on the next read — never to a
    denial, so the ConsentStore is left untouched (a question that timed out is
    not an answer). Omit it and the request never expires, which is every
    caller's behaviour today except the Zigbee pairing assistant. A refresh (an
    idempotent hit on an existing open request) keeps the original
    ``expires_at``: the clock runs from when the question was first asked, so
    re-asking cannot extend a stale window.
    """
    with _LOCK:
        items = _load()
        for req in items:
            if (req.get("entity_id") == entity_id
                    and req.get("action") == action
                    and req.get("status") == "open"):
                return req  # already pending — idempotent
        now = datetime.now(timezone.utc)
        req = {
            "entity_id": entity_id,
            "label": label,
            "action": action,
            "reason": reason,
            "requested_by": requested_by,
            "context": context or {},
            "status": "open",
            "created_at": now.isoformat(),
        }
        if ttl_seconds is not None:
            req["expires_at"] = (now + timedelta(seconds=ttl_seconds)).isoformat()
        items.append(req)
        _save(items)
    log_agent_activity(
        "sentinel", "consent_requested",
        f"Consent request: {action} for {label} ({entity_id})",
        "waiting_user",
        {"entity_id": entity_id, "reason": reason})
    return req


def _expire_stale() -> int:
    """Flip OPEN requests whose ``expires_at`` has passed to ``status="expired"``.

    Generic and opt-in: it acts only on requests that carry an ``expires_at``
    (set at creation via ``create_request(..., ttl_seconds=...)``). A request
    without one never expires, so nothing that did not ask for a TTL is touched —
    today that means only the Zigbee pairing assistant's questions age out, and
    every camera/device Sentinel request is left exactly as it was.

    An expiry records NO verdict. Unlike :func:`resolve` it never reaches into
    the consent_gate store, because a question that timed out unanswered is not a
    denial and must not be written as one. The request keeps its history and
    simply stops being ``open``, so every reader that filters on
    ``status == "open"`` drops it with no further change.

    Acquires :data:`_LOCK`; never call it while already holding the lock. Returns
    the number of requests flipped.
    """
    now = datetime.now(timezone.utc)
    with _LOCK:
        items = _load()
        changed = 0
        for req in items:
            if not isinstance(req, dict) or req.get("status") != "open":
                continue
            stamp = req.get("expires_at")
            if not isinstance(stamp, str):
                continue
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if now >= when:
                req["status"] = "expired"
                req["expired_at"] = now.isoformat()
                changed += 1
        if changed:
            _save(items)
    return changed


def list_open() -> list[dict]:
    _expire_stale()
    return [r for r in _load() if r.get("status") == "open"]


def find_open(query: str = "") -> dict | None:
    """Match an open request by entity_id/label; empty query → sole open one."""
    open_reqs = list_open()
    if not open_reqs:
        return None
    q = (query or "").strip().lower().strip("'\"")
    if not q:
        return open_reqs[0] if len(open_reqs) == 1 else None
    exact = [r for r in open_reqs if r["entity_id"].lower() == q]
    if exact:
        return exact[0]
    tokens = [t for t in q.replace(".", " ").replace("_", " ").split()
              if t not in ("camera", "cam", "the", "my", "device", "it",
                           "yes", "please", "approve", "that")]
    if not tokens:
        return open_reqs[0] if len(open_reqs) == 1 else None

    def score(r: dict) -> int:
        hay = f"{r.get('label','')} {r['entity_id']}".lower().replace("_", " ")
        return sum(1 for t in tokens if t in hay)

    scored = sorted(open_reqs, key=score, reverse=True)
    return scored[0] if score(scored[0]) > 0 else None


def resolve(entity_id: str, *, approved: bool, by: str = "user_via_chat",
            note: str = "") -> dict | None:
    """Close the open request and record the verdict in the ConsentStore.

    A stale request is swept to ``expired`` first (outside the lock, which is not
    reentrant), so a verdict can only ever land on a request that is still
    genuinely open — an approval arriving long after a TTL-bound question lapsed
    finds nothing to resolve and returns ``None`` rather than acting on it.
    """
    _expire_stale()
    with _LOCK:
        items = _load()
        target = None
        for req in items:
            if req.get("entity_id") == entity_id and req.get("status") == "open":
                target = req
                break
        if target is None:
            return None
        target["status"] = "approved" if approved else "denied"
        target["resolved_by"] = by
        target["resolved_at"] = _utcnow()
        if note:
            target["note"] = note
        _save(items)

    from puffpai.security.consent_gate import get_store
    if approved:
        get_store().approve(entity_id, by=by)
    else:
        get_store().deny(entity_id, by=by, reason=note or "declined in chat")

    log_agent_activity(
        "sentinel", "consent_resolved",
        f"Consent {'granted' if approved else 'denied'}: "
        f"{target.get('label', entity_id)} ({entity_id})",
        "completed",
        {"entity_id": entity_id, "approved": approved, "by": by})
    return target
