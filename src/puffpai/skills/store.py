"""Where installed skills live, and how the rest of the appliance learns of them.

One directory per skill under ``<state dir>/skills/``, sibling of the Toolsmith
workspace -- never under ``/opt/puffpai`` or ``src/``. A skill is DATA. The
appliance's code is not editable by this loop, by anything in it, at any phase.

``rescan()`` is the whole install/uninstall mechanism, in the app-registry's
idiom: it re-reads the directory, rebuilds the capability rows, and re-registers
the skill doors wholesale. A skill whose folder is gone loses its row and its
door in the same breath -- a merge would leave a removed skill's door in the
table with the owner's old ``true`` still resolving against it.

Two things are deliberately NOT here:

* **Recovery.** ``remove()`` moves the directory to ``_removed/<id>-<ts>/`` so a
  mistake is not instantly destructive, but nothing reads that directory back.
  Re-installing is proposing again and approving again, because an un-delete
  path is a way to restore code without a consent card.
* **Trust by history.** A skill's installed sha256s are re-checked on every
  rescan and before every run. There is no "it was fine last time".

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

DRAFTS_DIRNAME = "_drafts"
REMOVED_DIRNAME = "_removed"

_lock = threading.RLock()
_root_override: str | None = None


def set_root(path: str | None) -> None:
    """Point the store at *path* (tests, and an operator moving the tree)."""
    global _root_override
    with _lock:
        _root_override = path


def skills_root() -> str:
    """Absolute path of the skills tree, created on use."""
    with _lock:
        override = _root_override
    if override:
        root = override
    else:
        from puffpai.statedir import resolve_runtime_dir
        root = os.path.join(resolve_runtime_dir(_DEFAULT_DATA_DIR), "skills")
    os.makedirs(root, exist_ok=True)
    return os.path.realpath(root)


def drafts_root() -> str:
    path = os.path.join(skills_root(), DRAFTS_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def removed_root() -> str:
    path = os.path.join(skills_root(), REMOVED_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def runs_root() -> str:
    """Per-run scratch roots. Under the skills tree, not the shared workspace:
    a skill run must not be able to read or clobber what the Toolsmith's
    ``workspace_exec`` left lying about."""
    path = os.path.join(skills_root(), "_runs")
    os.makedirs(path, exist_ok=True)
    return path


def skill_dir(skill_id: str) -> str:
    return os.path.join(skills_root(), _safe_id(skill_id))


def draft_dir(skill_id: str) -> str:
    return os.path.join(drafts_root(), _safe_id(skill_id))


def _safe_id(skill_id) -> str:
    from puffpai.skills.manifest import ID_RE, SkillManifestError
    value = str(skill_id or "").strip().lower()
    if not ID_RE.match(value):
        raise SkillManifestError(f"{skill_id!r} is not a usable skill id.")
    return value


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return default
    return loaded


def _write_json(path: str, payload) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


DEFAULT_STATE = {"enabled": True, "runs": 0, "last_error": "",
                 "last_refusal": "", "last_test": {"ok": False, "at": "", "log_tail": ""}}


def read_state(skill_id: str) -> dict:
    state = dict(DEFAULT_STATE)
    state["last_test"] = dict(DEFAULT_STATE["last_test"])
    loaded = _read_json(os.path.join(skill_dir(skill_id), "state.json"), {})
    if isinstance(loaded, dict):
        for key in state:
            if key in loaded:
                state[key] = loaded[key]
    return state


def write_state(skill_id: str, state: dict) -> None:
    _write_json(os.path.join(skill_dir(skill_id), "state.json"), state)


def read_installed(skill_id: str) -> dict:
    record = _read_json(os.path.join(skill_dir(skill_id), "installed.json"), {})
    return record if isinstance(record, dict) else {}


def installed_ids() -> list:
    """Ids with a directory on disk, in stable order. Never raises."""
    try:
        entries = sorted(os.listdir(skills_root()))
    except OSError:
        return []
    out = []
    for name in entries:
        if name.startswith("_"):
            continue
        if os.path.isdir(os.path.join(skills_root(), name)):
            out.append(name)
    return out


def integrity(skill_id: str) -> tuple[bool, str]:
    """``(ok, reason)`` -- do the files still hash to what was approved?

    Fails closed on a missing ``installed.json``: a skill directory nobody has
    a record of approving is not an approved skill.
    """
    from puffpai.skills import manifest as _manifest

    record = read_installed(skill_id)
    expected = record.get("sha256")
    if not isinstance(expected, dict) or not expected:
        return False, "there is no record of this skill being approved"
    folder = skill_dir(skill_id)
    for name, digest in expected.items():
        path = os.path.join(folder, name)
        if os.path.islink(path) or not os.path.isfile(path):
            return False, f"{name} is missing from the skill"
        try:
            if _manifest.sha256_file(path) != digest:
                return False, f"{name} is not the file you approved"
        except OSError:
            return False, f"{name} could not be read"
    return True, ""


def load(skill_id: str) -> dict | None:
    """Everything known about one installed skill, or None. Never raises."""
    from puffpai.skills import manifest as _manifest
    try:
        sid = _safe_id(skill_id)
    except Exception:
        return None
    folder = skill_dir(sid)
    raw = _read_json(os.path.join(folder, "manifest.json"), None)
    if raw is None:
        return None
    try:
        manifest = _manifest.validate(raw, skip_origin=f"skill:{sid}")
    except Exception as exc:
        logger.warning("skills: %s has an unusable manifest: %s", sid, exc)
        return None
    ok, reason = integrity(sid)
    return {"id": sid, "dir": folder, "manifest": manifest,
            "state": read_state(sid), "installed": read_installed(sid),
            "intact": ok, "integrity_reason": reason}


def all_loaded() -> list:
    out = []
    for sid in installed_ids():
        record = load(sid)
        if record is not None:
            out.append(record)
    return out


# ── the registry overlay ────────────────────────────────────────────

def capabilities() -> tuple:
    """One ``Capability`` row per installed skill. Never raises.

    ``authority_tool`` is ``"(none)"`` on purpose: Phase 1 ships no ``run_skill``
    LLM tool, and a row that named a tool the live tool set does not have would
    make ``capability_registry.validate()`` report the registry as dishonest --
    correctly. The row is reachable from the deterministic fastpath and from the
    dashboard; the model is not handed a lever to pull.

    ``keywords`` stays EMPTY. Keywords are a binding signal that widens which
    tools get offered for a query; a skill has no tools to offer, and a skill
    that widened the household's tool binding would be reaching past its own
    trigger.
    """
    from puffpai.capability_registry import LIVE, OFFLINE, READ, Capability

    rows = []
    for record in all_loaded():
        sid = record["id"]
        manifest = record["manifest"]
        hosts = manifest["permissions"]["network_hosts"]
        enabled = bool(record["state"].get("enabled"))
        tested = bool((record["state"].get("last_test") or {}).get("ok"))
        rows.append(Capability(
            id=f"skill.{sid}",
            domain="skills",
            label=manifest["name"],
            authority_tool="(none)",
            data_source=(f"skill:{sid} (sandboxed; "
                         f"hosts: {', '.join(hosts) if hosts else 'none'})"),
            access=READ,
            keywords=(),
            agent="toolsmith",
            surfaces=("voice", "chat", "dashboard"),
            phrases=tuple(manifest["triggers"]),
            dispatch_phrases=tuple(manifest["triggers"]),
            status=(LIVE if (enabled and tested and record["intact"]) else OFFLINE),
            note=manifest["description"],
            origin=f"skill:{sid}",
        ))
    return tuple(rows)


def door_entries() -> list:
    """``[(skill_id, [door block])]`` for :func:`egress_doors.register_skill_doors`.

    A skill that declares no host still gets no door rather than an empty one:
    an inert door is a switch in front of the owner that moves nothing.
    """
    entries = []
    for record in all_loaded():
        hosts = record["manifest"]["permissions"]["network_hosts"]
        if not hosts:
            continue
        entries.append((record["id"], [{
            "hosts": list(hosts),
            "purpose": record["manifest"]["description"],
            "data_sent_plain": ("The web address this skill asks for. Nothing "
                                "from your house goes with it."),
        }]))
    return entries


def entitled_door_flags() -> set:
    """The exact door flags installed, ENABLED skills are entitled to right now.

    Anything else in the ``skill_`` flag namespace is stale: a removed skill's
    leftover ``true``, a disabled skill's, or an older host digest whose card
    nobody approved. Computed from the same records the doors are registered
    from, so the two can never disagree about which flag belongs to whom.
    """
    from puffpai.security import egress_doors as _doors

    flags = set()
    for record in all_loaded():
        if not record["state"].get("enabled"):
            continue
        hosts = record["manifest"]["permissions"]["network_hosts"]
        if not hosts:
            continue
        flags.add(_doors.skill_door_flag(record["id"], 0, hosts))
    return flags


def ensure_door_registered(skill_id: str) -> bool:
    """Make sure this skill's door exists in THIS process. Never raises.

    Skill doors live only in ``egress_doors``' memory, and :func:`rescan` runs
    on install/enable/disable/remove, never at process start. Without this,
    after every API restart an installed, enabled skill with the owner's
    ``true`` on disk would have NO door, and its first run would be refused as
    if the owner had shut it. Registering here, before any run, closes that.

    Registration only: no flag is written or pruned, so this cannot open a
    door the owner did not open. It re-registers the whole skill-door set from
    the store, exactly as :func:`rescan` does. Returns True when the door is
    registered afterwards (or the skill declares no host and needs none).
    """
    try:
        from puffpai.security import egress_doors as _doors
        door_id = _doors.skill_door_id(_safe_id(skill_id), 0)
        if _doors.door(door_id) is not None:
            return True
        record = load(skill_id)
        if record is None or not record["manifest"]["permissions"]["network_hosts"]:
            return True
        logger.warning("skills: door %s was not registered in this process; "
                       "registering skill doors now", door_id)
        _doors.register_skill_doors(door_entries())
        return _doors.door(door_id) is not None
    except Exception as exc:
        logger.warning("skills: could not register the door for %s: %s",
                       skill_id, type(exc).__name__)
        return False


def rescan() -> tuple:
    """Re-read the tree: rebuild the registry overlay and the skill doors.

    Never raises. A rescan that cannot complete leaves the appliance with FEWER
    skills, never with stale ones: both writes below are wholesale replacements.
    """
    rows: tuple = ()
    try:
        rows = capabilities()
    except Exception as exc:
        logger.warning("skills: capability rescan failed: %s", exc)
        rows = ()
    try:
        from puffpai import capability_registry as _registry
        _registry.set_skill_overlay(rows)
    except Exception as exc:  # pragma: no cover - ships together
        logger.warning("skills: registry overlay not updated: %s", exc)
    try:
        from puffpai.security import egress_doors as _doors
        _doors.register_skill_doors(door_entries())
    except Exception as exc:  # pragma: no cover
        logger.warning("skills: skill doors not registered: %s", exc)
    try:
        # Unregistering a door is not the same as forgetting the owner's
        # ``true`` for it: the flag is the only on-disk record of a yes, and a
        # stale one would be waiting the next time a door of that name is
        # registered. Deletes only; the write that OPENS a door lives in
        # egress_admin.set_door_open and is reached from install/enable alone.
        # Skipped entirely if the tree could not be read (an unreadable store
        # must not be mistaken for "no skills are entitled to anything").
        from puffpai.security import egress_admin as _admin
        _admin.prune_skill_flags(entitled_door_flags())
    except Exception as exc:  # pragma: no cover - never fails a rescan
        logger.warning("skills: stale skill door flags not cleared: %s", exc)
    try:
        # A skill row's status IS its probe (capability_probes.SKILL_PREFIX),
        # and the probe is cached. Without this, switching a skill off would
        # keep reporting it live until the TTL expired -- a status page that
        # lags the switch is a status page that lies.
        from puffpai import capability_probes as _probes
        _probes.reset_cache()
    except Exception as exc:  # pragma: no cover
        logger.debug("skills: probe cache not reset: %s", exc)
    return rows


# ── mutation ────────────────────────────────────────────────────────

def set_enabled(skill_id: str, enabled: bool, *, reason: str = "") -> bool:
    """Flip one skill on or off. Disabled means offline, not removed."""
    sid = _safe_id(skill_id)
    if not os.path.isdir(skill_dir(sid)):
        return False
    with _lock:
        state = read_state(sid)
        state["enabled"] = bool(enabled)
        if reason:
            state["last_error"] = str(reason)[:300]
        write_state(sid, state)
    if not enabled:
        # Off means the door is shut on disk too, not just unreachable. Turning
        # it back on goes through ``skills.install.enable_installed``, which
        # re-verifies the bytes and the host digest before re-asserting it.
        _clear_door_flags(sid)
    rescan()
    logger.info("skills: %s %s%s", sid, "enabled" if enabled else "disabled",
                f" ({reason})" if reason else "")
    return True


def note_run(skill_id: str, *, ok: bool, error: str = "",
             refusal: str = "") -> None:
    """Record the outcome of one run. Content-free: reasons, never answers."""
    try:
        sid = _safe_id(skill_id)
        with _lock:
            state = read_state(sid)
            state["runs"] = int(state.get("runs") or 0) + 1
            state["last_error"] = "" if ok else str(error)[:300]
            if refusal:
                state["last_refusal"] = str(refusal)[:300]
            write_state(sid, state)
    except Exception as exc:  # pragma: no cover - bookkeeping never fails a run
        logger.debug("skills: could not record a run for %s: %s", skill_id, exc)


def note_test(skill_id: str, *, ok: bool, log_tail: str = "") -> None:
    sid = _safe_id(skill_id)
    with _lock:
        state = read_state(sid)
        state["last_test"] = {"ok": bool(ok), "log_tail": str(log_tail)[:500],
                              "at": datetime.now(timezone.utc).isoformat()}
        write_state(sid, state)


def _clear_door_flags(skill_id: str) -> int:
    """Forget the owner's ``true`` for every door this skill ever declared.

    Never raises: losing a skill's flag must not fail a removal, and failing to
    lose it is logged rather than silent. ``rescan()`` prunes the same keys, so
    this is belt and braces at exactly the two moments a skill stops being
    entitled to reach anything.
    """
    try:
        from puffpai.security import egress_admin as _admin
        return _admin.clear_skill_flags(skill_id)
    except Exception as exc:  # pragma: no cover - ships together
        logger.warning("skills: %s door flag(s) not cleared: %s", skill_id, exc)
        return 0


def remove(skill_id: str) -> bool:
    """Move a skill out of service. Returns False when there was nothing there.

    The directory goes to ``_removed/<id>-<ts>/`` -- kept so a mistake is
    survivable by hand, read by nothing. Re-installing means proposing and
    approving again.
    """
    sid = _safe_id(skill_id)
    folder = skill_dir(sid)
    if not os.path.isdir(folder):
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = os.path.join(removed_root(), f"{sid}-{stamp}")
    with _lock:
        try:
            os.replace(folder, target)
        except OSError:
            shutil.move(folder, target)
    _clear_door_flags(sid)
    rescan()
    logger.info("skills: %s removed (kept at %s)", sid, target)
    return True


__all__ = [
    "DEFAULT_STATE", "DRAFTS_DIRNAME", "REMOVED_DIRNAME", "all_loaded",
    "capabilities", "door_entries", "draft_dir", "drafts_root",
    "ensure_door_registered", "entitled_door_flags", "installed_ids",
    "integrity", "load", "note_run", "note_test", "read_installed",
    "read_state", "remove", "removed_root", "rescan", "runs_root", "set_enabled",
    "set_root", "skill_dir", "skills_root", "write_state",
]
