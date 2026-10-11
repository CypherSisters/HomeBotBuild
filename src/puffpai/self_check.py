"""Appliance self-check — orchestration Phase 5b (config/state repair).

Detect → report → repair *only what is provably safe*. Everything else is a
surfaced finding, never an action.

Why this module lives at package top level (not under ``agents/``): it spans
config, security, knowledge and the Home Assistant bridge, and it must be
callable from a route, a scheduled job, a CLI, or a test with no bus, no
engine and no agent envelope. It is a plain function library, like
``offline_guard`` and ``device_sentinel`` beside it.

Contract
--------
``run_self_check()`` is pure and side-effect-free — it reads the filesystem,
``os.environ`` and (optionally) makes read-only HTTP GETs. The ONLY writes it
can ever perform are the narrowly-scoped directory repairs, and those require
``repair=True``. A plain check can never change anything.

What it looks for — every one of these actually bit this build:

1. ``state_path:*``  A directory the service must WRITE to resolving into the
   read-only code tree (``ProtectSystem=strict`` + ``ReadWritePaths``). Silent:
   the modules degrade to a warning and drop the write. Cost hours.
2. ``ha_credential``  A stale/rejected Home Assistant token (401). Report only —
   this module never rotates, fabricates or reads a credential.
3. ``vault_uninitialized``  An empty-but-writable vault dir is NORMAL and needs
   a human to set the PIN; an *unwritable* vault dir is a bug. Distinguished.
4. ``ha_entities_unavailable``  Devices that vanished from the network. This
   house lost 128 entities at once when its ISP changed and the hubs were not
   reconnected — that is expected, so this check reads as informational.
5. ``fts_shards``  A knowledge index shard left ``building`` after a crash —
   told apart from one that is building *right now* by recency of progress,
   because a healthy multi-hour build carries the identical flag. Resumable,
   but the rebuild is long-running, so it is a *suggestion* only.

Nothing here restarts a service, writes to the house, or touches credentials.
"""

from __future__ import annotations

import glob
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Status values a check may carry, worst last. Used to fold the summary.
_SEVERITY_ORDER = ("ok", "warn", "fail")

#: A repair may create at most this many nested directory levels. A missing
#: ``/data/puffpai/state`` under an existing ``/data/puffpai`` is one level;
#: needing more than two means the configured root itself is absent, which is a
#: deployment question for a human, not something to invent a tree for.
MAX_REPAIR_DEPTH = 2


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── primitive probes ────────────────────────────────────────────────

def _is_writable(dir_path: str) -> bool:
    """True when *dir_path* is an existing directory this process can write to.

    ``os.access(W_OK)`` is used deliberately: on Linux ``access(2)`` reports
    ``EROFS`` for a read-only mount, which is exactly the ``ProtectSystem=strict``
    case this module exists to catch, and it makes NO filesystem change (a
    touch-and-delete probe would violate the "``repair=False`` changes nothing"
    contract). On Windows ``os.access`` is advisory only; the appliance is Linux
    and the tests substitute this helper.
    """
    try:
        return os.path.isdir(dir_path) and os.access(dir_path, os.W_OK | os.X_OK)
    except OSError:  # pragma: no cover - defensive
        return False


def _nearest_existing_ancestor(path: str) -> tuple[str, int]:
    """Return (deepest existing ancestor of *path*, levels missing below it)."""
    current = os.path.normpath(path)
    missing = 0
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:          # walked off the top of the filesystem
            return current, missing
        current = parent
        missing += 1
    return current, missing


def _repair_missing_dir(dir_path: str) -> tuple[bool, str]:
    """Create *dir_path* when — and only when — that is provably safe.

    WHY THIS REPAIR IS SAFE:
      * It runs only when the directory does NOT exist, so it can never
        overwrite, truncate or relocate existing state.
      * It creates only *inside an ancestor that already exists and is already
        writable by this process*. It therefore grants no access the process did
        not already have, and cannot escape the configured ReadWritePaths root:
        if the root is read-only, the ancestor is not writable and we refuse.
      * It never changes permissions or ownership — widening a mode is how a
        "repair" becomes a vulnerability. A directory that exists but is not
        writable is reported, never touched.
      * At most :data:`MAX_REPAIR_DEPTH` levels are created, so a misconfigured
        absolute path cannot cause a deep tree to be conjured somewhere unrelated.
      * ``exist_ok=True`` plus the "only when missing" precondition makes it
        idempotent: a second run is a no-op that reports ``repaired=False``.
    """
    if os.path.isdir(dir_path):
        return False, "already present"
    if os.path.exists(dir_path):
        return False, "path exists but is not a directory; a human must look"

    ancestor, missing = _nearest_existing_ancestor(dir_path)
    if missing == 0 or missing > MAX_REPAIR_DEPTH:
        return False, (f"{missing} directory level(s) missing below {ancestor!r}; "
                       f"refusing to create more than {MAX_REPAIR_DEPTH}")
    if os.path.dirname(ancestor) == ancestor:
        return False, "nearest existing ancestor is a filesystem root; refusing"
    if not _is_writable(ancestor):
        return False, (f"nearest existing ancestor {ancestor!r} is not writable "
                       "by this process; this is a deployment fault, not "
                       "something to fix by widening permissions")

    try:
        os.makedirs(dir_path, exist_ok=True)
    except OSError as exc:
        return False, f"mkdir failed: {exc}"
    logger.warning("self_check repaired missing state directory %s", dir_path)
    return True, f"created {dir_path} under writable {ancestor}"


# ── the state-path registry ─────────────────────────────────────────
# Each entry resolves a directory the running service MUST be able to write to,
# using the SAME resolver the consuming module uses, so this check cannot drift
# away from reality. Resolvers are lazy: importing them at module scope would
# pull in config (and the secrets provider) on import of this module.

def _dirname_of(fn: Callable[[], str]) -> str:
    return os.path.dirname(os.path.abspath(fn()))


def _resolve_agent_events() -> str:
    from puffpai import agent_events
    return _dirname_of(agent_events.get_event_log_path)


def _resolve_majordomo_queue() -> str:
    from puffpai.agents import majordomo
    return _dirname_of(majordomo.get_queue_path)


def _resolve_caretaker_ledger() -> str:
    from puffpai.agents import caretaker
    return _dirname_of(caretaker.get_restart_ledger_path)


def _resolve_audit_log() -> str:
    from puffpai.security.audit_log import default_log_dir
    return os.path.abspath(str(default_log_dir()))


def _resolve_vault() -> str:
    from puffpai.security.vault import default_vault_dir
    return os.path.abspath(str(default_vault_dir()))


def _resolve_consent_gate() -> str:
    from puffpai.security import consent_gate
    return os.path.dirname(os.path.abspath(consent_gate.CONSENT_PATH))


def _resolve_consent_requests() -> str:
    from puffpai.security import consent_requests
    return os.path.dirname(os.path.abspath(consent_requests.REQUESTS_PATH))


#: ``(check id suffix, human module label, what it holds, resolver)``.
STATE_PATHS: tuple[tuple[str, str, str, Callable[[], str]], ...] = (
    ("agent_events", "puffpai.agent_events", "the agent event log",
     _resolve_agent_events),
    ("majordomo", "puffpai.agents.majordomo", "the scheduled-task queue",
     _resolve_majordomo_queue),
    ("caretaker", "puffpai.agents.caretaker", "the restart rate-limit ledger",
     _resolve_caretaker_ledger),
    ("audit_log", "puffpai.security.audit_log", "the security audit trail",
     _resolve_audit_log),
    ("vault", "puffpai.security.vault", "the encrypted secret vault",
     _resolve_vault),
    ("consent_gate", "puffpai.security.consent_gate", "device consent decisions",
     _resolve_consent_gate),
    ("consent_requests", "puffpai.security.consent_requests",
     "pending consent requests", _resolve_consent_requests),
)


# ── check builders ──────────────────────────────────────────────────

def _check(check_id: str, title: str, status: str, detail: str, *,
           repairable: bool = False, repaired: bool = False,
           suggestion: str = "", data: dict | None = None) -> dict:
    return {
        "id": check_id,
        "title": title,
        "status": status if status in _SEVERITY_ORDER else "warn",
        "detail": detail,
        "repairable": bool(repairable),
        "repaired": bool(repaired),
        "suggestion": suggestion,
        "data": data or {},
    }


def check_state_paths(*, repair: bool = False) -> list[dict]:
    """One check per writable-state directory. The Phase 5b headline fault.

    A directory that resolves into the read-only code tree fails SILENTLY at
    runtime: every consumer logs once and drops the write. Here it is named,
    with the module that owns it and the path it actually resolved to.
    """
    checks: list[dict] = []
    for suffix, module, holds, resolver in STATE_PATHS:
        check_id = f"state_path:{suffix}"
        title = f"writable state for {module}"
        try:
            path = resolver()
        except Exception as exc:              # a broken resolver is itself news
            checks.append(_check(
                check_id, title, "warn",
                f"could not resolve the directory holding {holds}: {exc}",
                suggestion="check config.paths and $PUFFPAI_DATA_DIR",
                data={"module": module}))
            continue

        common = {"module": module, "path": path, "holds": holds}
        if _is_writable(path):
            checks.append(_check(check_id, title, "ok",
                                 f"{path} is writable", data=common))
            continue

        if os.path.isdir(path):
            # Exists but unwritable — the ProtectSystem=strict case. NOT
            # repairable: the only "fix" would be widening permissions or
            # writing outside the configured root, both of which are refused.
            checks.append(_check(
                check_id, title, "fail",
                f"{path} exists but is NOT writable by this process, so {holds} "
                f"is being silently dropped ({module})",
                suggestion=(f"add {path} to the unit's ReadWritePaths, or point "
                            f"config.paths / $PUFFPAI_DATA_DIR at a writable "
                            f"root. Do not chmod the code tree."),
                data=common))
            continue

        # Missing. Repairable only if it sits under an already-writable root.
        can_repair, why = _repair_dry_run(path)
        if repair and can_repair:
            repaired, why = _repair_missing_dir(path)
            if repaired:
                checks.append(_check(
                    check_id, title, "ok",
                    f"{path} was missing and has been created ({why})",
                    repairable=True, repaired=True, data=common))
                continue
            can_repair = False

        checks.append(_check(
            check_id, title, "fail",
            f"{path} does not exist, so {holds} is being silently dropped "
            f"({module}); {why}",
            repairable=can_repair,
            suggestion=("re-run the self-check with repair enabled"
                        if can_repair else
                        "create the writable root named in the unit file"),
            data=common))
    return checks


def _repair_dry_run(dir_path: str) -> tuple[bool, str]:
    """Would :func:`_repair_missing_dir` act? Answers without acting."""
    if os.path.exists(dir_path):
        return False, "path already exists"
    ancestor, missing = _nearest_existing_ancestor(dir_path)
    if missing == 0 or missing > MAX_REPAIR_DEPTH:
        return False, f"{missing} level(s) missing below {ancestor}; too deep to create"
    if os.path.dirname(ancestor) == ancestor:
        return False, "nearest existing ancestor is a filesystem root"
    if not _is_writable(ancestor):
        return False, f"{ancestor} is not writable, so this cannot be repaired here"
    return True, f"it can be created safely under the writable {ancestor}"


def check_vault_initialized() -> list[dict]:
    """Distinguish "vault unwritable" (a bug) from "not yet initialized" (normal).

    Deliberately filesystem-only: constructing a :class:`~puffpai.security.vault.Vault`
    would mkdir and build an AuditLog, i.e. write. Presence of the key-salt file
    is exactly what ``Vault.is_initialized`` reads back, and nothing here opens
    or decrypts it.
    """
    try:
        from puffpai.security.vault import default_vault_dir
        vault_dir = os.path.abspath(str(default_vault_dir()))
    except Exception as exc:
        return [_check("vault_uninitialized", "vault status", "warn",
                       f"could not resolve the vault directory: {exc}")]

    salt_file = os.path.join(vault_dir, ".vault_key_salt")
    data = {"vault_dir": vault_dir}
    if not os.path.isdir(vault_dir):
        return [_check(
            "vault_uninitialized", "vault status", "warn",
            f"the vault directory {vault_dir} does not exist yet; "
            "see the writable-state check for whether that is repairable",
            suggestion="run the state-path repair, then set the vault PIN",
            data=data)]
    if not _is_writable(vault_dir):
        return [_check(
            "vault_uninitialized", "vault status", "fail",
            f"the vault directory {vault_dir} is NOT writable — this is a "
            "deployment bug, not a missing PIN; the vault has never been able "
            "to store anything",
            suggestion=f"grant the unit write access to {vault_dir}",
            data=data)]
    if not os.path.exists(salt_file):
        return [_check(
            "vault_uninitialized", "vault status", "warn",
            f"the vault at {vault_dir} is writable but has no PIN set yet. "
            "This is normal for a fresh appliance — it is not a fault and "
            "nothing here can fix it: a human must choose the PIN.",
            suggestion="set the vault PIN from the dashboard (human step)",
            data=data)]
    return [_check("vault_uninitialized", "vault status", "ok",
                   f"the vault at {vault_dir} is writable and initialized",
                   data=data)]


def check_ha_credential(bridge: Any) -> list[dict]:
    """Read-only probe of the Home Assistant credential.

    REPORT ONLY, always. This never rotates, fabricates, reads or logs a token,
    and makes no mutating call — ``get_status()`` is a GET of ``/api/config``.
    """
    if bridge is None:
        return [_check("ha_credential", "Home Assistant credential", "ok",
                       "Home Assistant probe skipped",
                       data={"probed": False})]
    try:
        status = bridge.get_status() or {}
    except Exception as exc:
        return [_check("ha_credential", "Home Assistant credential", "warn",
                       f"the Home Assistant probe itself failed: {exc}",
                       suggestion="check the HA bridge configuration",
                       data={"probed": True})]
    if not isinstance(status, dict):
        return [_check("ha_credential", "Home Assistant credential", "warn",
                       "the Home Assistant probe returned an unusable reply",
                       data={"probed": True})]

    if status.get("connected"):
        return [_check("ha_credential", "Home Assistant credential", "ok",
                       f"Home Assistant answered as "
                       f"{status.get('location_name') or 'the house'} "
                       f"(version {status.get('ha_version', 'unknown')})",
                       data={"probed": True, "connected": True})]

    error = str(status.get("error") or "")[:200]
    data = {"probed": True, "connected": False, "error": error}
    if "401" in error or "403" in error:
        return [_check(
            "ha_credential", "Home Assistant credential", "fail",
            f"Home Assistant rejected our token ({error}). The credential is "
            "stale or was revoked.",
            suggestion=("a human must issue a new long-lived access token in "
                        "Home Assistant and store it in the vault; PuffPAI will "
                        "never mint or rotate a credential for itself"),
            data=data)]
    if "disabled" in error.lower():
        return [_check("ha_credential", "Home Assistant credential", "ok",
                       "the Home Assistant integration is switched off",
                       data=data)]
    return [_check(
        "ha_credential", "Home Assistant credential", "warn",
        f"Home Assistant is not answering ({error}). This is a reachability "
        "problem, not necessarily a credential one.",
        suggestion="check that Home Assistant is up and reachable",
        data=data)]


#: Below this share of entities being unavailable, nothing is reported.
UNAVAILABLE_REPORT_FLOOR = 1

#: States Home Assistant uses for "this entity is not answering".
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})


def check_ha_entities(bridge: Any) -> list[dict]:
    """Which devices vanished, grouped by domain so a hub outage reads as one.

    INFORMATIONAL BY DESIGN. This house lost 128 entities at once when the ISP
    changed and the hubs were not reconnected; that is a true, expected fact
    about the house, not an alarm about the appliance. The check therefore never
    returns ``fail`` — the worst it says is "here is what is offline".
    """
    if bridge is None:
        return [_check("ha_entities_unavailable", "device availability", "ok",
                       "device availability check skipped",
                       data={"probed": False})]
    try:
        states = bridge.get_all_states()
    except Exception as exc:
        return [_check("ha_entities_unavailable", "device availability", "warn",
                       f"could not read entity states: {exc}",
                       data={"probed": True})]
    if not isinstance(states, (list, tuple)):
        return [_check("ha_entities_unavailable", "device availability", "warn",
                       "entity states came back in an unusable shape",
                       data={"probed": True})]

    total = 0
    by_domain: dict[str, int] = {}
    for raw in states:
        if not isinstance(raw, dict):
            continue
        entity_id = str(raw.get("entity_id") or "")
        if not entity_id:
            continue
        total += 1
        if str(raw.get("state") or "").strip().lower() in UNAVAILABLE_STATES:
            domain = entity_id.split(".", 1)[0] or "unknown"
            by_domain[domain] = by_domain.get(domain, 0) + 1

    offline = sum(by_domain.values())
    groups = sorted(by_domain.items(), key=lambda kv: (-kv[1], kv[0]))
    data = {"probed": True, "total_entities": total,
            "unavailable": offline,
            "by_domain": dict(groups)}

    if offline < UNAVAILABLE_REPORT_FLOOR:
        return [_check("ha_entities_unavailable", "device availability", "ok",
                       f"all {total} Home Assistant entities are answering",
                       data=data)]

    top = ", ".join(f"{d} x{n}" for d, n in groups[:5])
    return [_check(
        "ha_entities_unavailable", "device availability", "warn",
        f"{offline} of {total} Home Assistant entities are unavailable, "
        f"grouped as {top}. Whole groups going quiet together usually means a "
        "hub or bridge lost its network, not that the devices failed.",
        suggestion=("informational — reconnect the affected hub(s) if this is "
                    "unexpected; no PuffPAI action is warranted"),
        data=data)]


#: How long a ``building`` shard may go without touching its files before it is
#: called STALE rather than ACTIVE.
#:
#: Derived from the builder, not guessed. ``FTSBuilder.build`` commits every
#: ``batch_size`` entries (default 500) and each commit writes both the FTS rows
#: and the ``last_entry_index`` checkpoint, so a live build touches its files
#: every 500 scanned entries — seconds apart on this appliance, and well under a
#: minute even for a slow shard of large articles on cold storage. The window
#: that actually needs covering is the quiet one *before* the first batch: the
#: builder opens a multi-hundred-GB ZIM and seeks to the resume checkpoint,
#: which on a cold appliance is minutes. 15 minutes is roughly 1800 commit
#: intervals of headroom over the steady state and comfortable cover for
#: startup, while still catching a crash within one health-check cycle.
#:
#: The bias is deliberate: a false "stale" on a healthy multi-hour build trains
#: people to ignore this check, which costs more than noticing a crashed build
#: 15 minutes late.
SHARD_PROGRESS_STALE_SECONDS = 15 * 60


def _shard_last_progress(db_path: str) -> float | None:
    """Newest mtime across a shard and its SQLite sidecars; None if unknowable.

    The sidecars matter, and this is why mtime of the ``.db`` alone is not the
    right signal: ``_connect`` sets ``journal_mode=WAL``, so every batch commit
    lands in ``<shard>.db-wal`` and the main ``.db`` is only touched when SQLite
    checkpoints. A heavy build checkpoints often enough that the ``.db`` looks
    fresh, but a slow-progress shard could sit with a stale ``.db`` while the
    ``-wal`` is being written every few seconds — exactly the false "stale" this
    is meant to avoid. Taking the newest of the three is still one cheap stat
    per file and needs no state file and no sampler.

    Never raises: a shard deleted between the directory listing and this stat
    simply has no readable mtime.
    """
    newest: float | None = None
    for candidate in (db_path, db_path + "-wal", db_path + "-shm"):
        try:
            mtime = os.path.getmtime(candidate)
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return newest


def _minutes(seconds: float | None) -> str:
    return "unknown" if seconds is None else f"{int(seconds // 60)}m"


def check_index_shards() -> list[dict]:
    """Knowledge shards mid-build: distinguish ACTIVE from STALE.

    ``status == 'building'`` means "a build was started and has not finished".
    It does NOT mean "crashed" — a live multi-hour index build carries exactly
    the same flag, and reporting that as a fault every cycle is how a
    self-healing system teaches people to ignore it. Recency of progress is what
    separates the two, via :func:`_shard_last_progress`.

    No better signal is available cheaply: ``shard_status`` exposes
    ``last_entry_index`` and ``article_count``, which are counters with no
    timestamp, so telling movement from stillness would need either two samples
    separated in wall-clock time or a persisted previous reading. Both are
    excluded — one stat is the right cost for a health check.

    REPORT ONLY, unchanged. Even a genuinely stale shard is not resumed here:
    the rebuild is long-running and belongs to a human or the dispatcher, with
    its own budget and cancellation.
    """
    try:
        from puffpai.knowledge.fts_index import resolve_fts_dir, shard_status
        fts_dir = resolve_fts_dir()
    except Exception as exc:
        return [_check("fts_shards", "knowledge index shards", "warn",
                       f"could not resolve the index directory: {exc}")]

    if not os.path.isdir(fts_dir):
        return [_check("fts_shards", "knowledge index shards", "ok",
                       f"no index directory at {fts_dir}; nothing to check",
                       data={"fts_dir": fts_dir, "shards": 0})]

    now = time.time()
    building: list[dict] = []
    active: list[dict] = []
    stale: list[dict] = []
    errored: list[dict] = []
    seen = 0
    for db_path in sorted(glob.glob(os.path.join(fts_dir, "*.db"))):
        seen += 1
        try:
            info = shard_status(db_path) or {}
        except Exception as exc:                    # documented never-raises, still
            errored.append({"path": db_path, "error": str(exc)[:200]})
            continue
        status = str(info.get("status") or "").strip().lower()
        entry = {"path": db_path,
                 "library_id": os.path.splitext(os.path.basename(db_path))[0],
                 "last_entry_index": info.get("last_entry_index"),
                 "article_count": info.get("article_count")}
        if status == "building":
            last = _shard_last_progress(db_path)
            age = None if last is None else max(0.0, now - last)
            entry["idle_seconds"] = age
            building.append(entry)
            # Unknown age (the file vanished under us) counts as active: without
            # evidence of stillness this check does not cry wolf.
            (stale if age is not None and age >= SHARD_PROGRESS_STALE_SECONDS
             else active).append(entry)
        elif status == "error":
            entry["error"] = str(info.get("error") or "")[:200]
            errored.append(entry)

    data = {"fts_dir": fts_dir, "shards": seen, "building": building,
            "active": active, "stale": stale, "errored": errored,
            "stale_after_seconds": SHARD_PROGRESS_STALE_SECONDS}

    def _named(rows):
        return ", ".join(r["library_id"] for r in rows)

    # ACTIVE builds are informational: say what is running, stay `ok`.
    if not stale and not errored:
        if active:
            freshest = min((r["idle_seconds"] for r in active
                            if r["idle_seconds"] is not None), default=None)
            return [_check(
                "fts_shards", "knowledge index shards", "ok",
                f"{len(active)} shard(s) building (active, last progress "
                f"{_minutes(freshest)} ago): {_named(active)}. A build in "
                "progress is not a fault.",
                data=data)]
        return [_check("fts_shards", "knowledge index shards", "ok",
                       f"all {seen} index shard(s) are complete", data=data)]

    parts = []
    if stale:
        oldest = max((r["idle_seconds"] for r in stale
                      if r["idle_seconds"] is not None), default=None)
        parts.append(f"{len(stale)} shard(s) stale mid-build (no progress for "
                     f"{_minutes(oldest)}): {_named(stale)}")
    if errored:
        parts.append(f"{len(errored)} shard(s) in an error state")
    detail = "; ".join(parts) + "."
    if active:
        detail += (f" Separately, {len(active)} shard(s) are building normally "
                   f"({_named(active)}) and need nothing.")
    if stale:
        detail += (" A stale shard is resumable by re-running the builder — it "
                   "restarts from its saved checkpoint, it does not start over.")
    return [_check(
        "fts_shards", "knowledge index shards", "warn", detail,
        suggestion=("re-run the FTS build for the affected libraries "
                    "(puffpai.knowledge.fts_build_cli). Not done here: the "
                    "rebuild is long-running and belongs to a human or the "
                    "dispatcher, not to a health check."),
        data=data)]


# ── entry point ─────────────────────────────────────────────────────

def _default_bridge():
    """The live HA bridge, or None. Never raises; never mutates the house."""
    try:
        from puffpai.ha_bridge import get_bridge
        return get_bridge()
    except Exception as exc:
        logger.debug("self_check could not build the HA bridge: %s", exc)
        return None


def run_self_check(*, repair: bool = False, probe_network: bool = True,
                   bridge: Any = None) -> dict:
    """Run every detector and return structured findings.

    Side-effect-free unless *repair* is True, and even then the only possible
    write is creating a missing state directory under an already-writable root
    (see :func:`_repair_missing_dir` for why that is provably safe). Nothing
    here restarts a service, writes to the house, or touches a credential.

    *bridge* is injectable so tests — and callers who already hold one — never
    reach the network. When it is None and *probe_network* is True the live
    bridge is built; when *probe_network* is False the HA checks are skipped.
    """
    if bridge is None and probe_network:
        bridge = _default_bridge()

    checks: list[dict] = []
    for producer in (
        lambda: check_state_paths(repair=repair),
        check_vault_initialized,
        lambda: check_ha_credential(bridge),
        lambda: check_ha_entities(bridge),
        check_index_shards,
    ):
        try:
            produced = producer()
        except Exception as exc:  # a detector must never break the self-check
            logger.warning("self-check detector failed: %s", exc)
            continue
        checks.extend(c for c in produced if isinstance(c, dict))

    counts = {level: 0 for level in _SEVERITY_ORDER}
    for c in checks:
        counts[c.get("status", "warn")] = counts.get(c.get("status", "warn"), 0) + 1

    overall = "ok"
    if counts.get("fail"):
        overall = "fail"
    elif counts.get("warn"):
        overall = "warn"

    return {
        "checks": checks,
        "summary": {
            "status": overall,
            "total": len(checks),
            "ok": counts.get("ok", 0),
            "warn": counts.get("warn", 0),
            "fail": counts.get("fail", 0),
            "repaired": [c["id"] for c in checks if c.get("repaired")],
            "repairable": [c["id"] for c in checks
                           if c.get("repairable") and not c.get("repaired")],
            "repair_enabled": bool(repair),
            "generated_at": _utcnow(),
        },
    }


__all__ = [
    "MAX_REPAIR_DEPTH",
    "SHARD_PROGRESS_STALE_SECONDS",
    "STATE_PATHS",
    "UNAVAILABLE_STATES",
    "check_ha_credential",
    "check_ha_entities",
    "check_index_shards",
    "check_state_paths",
    "check_vault_initialized",
    "run_self_check",
]
