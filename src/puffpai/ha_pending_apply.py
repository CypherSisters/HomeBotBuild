"""Approved Home Assistant changes waiting for the owner's vault PIN.

An approval given with the dashboard Approve button while the vault is
locked cannot run at once: the elevated credential is not available. Without
this module nothing would be created and nobody would be told. It keeps such
an approval alive, tied to that exact consent record, for
:data:`WAIT_SECONDS`:

* :func:`add_waiting` records ONLY a human-approved ``ha_provision`` record
  (resolved by a human surface). Nothing else can enter.
* :func:`apply_waiting` finishes a waiting record in exactly two cases
  a FULL vault unlock fired by the same paired device or
  session that tapped Approve (its requester id is recorded, hashed, when the
  record enters the waiting state), or the PIN given for THAT ask (in its
  thread or on its card; the caller names the records with ``only``). An
  unlock by any other device applies nothing; the thread is told once. Each
  eligible waiting record is taken out under a lock and handed to
  :func:`puffpai.ha_provisioning.apply_approved_change`, which re-reads the
  durable record, re-validates the allowlist, demands HUMAN_CONFIRMED and
  spends the approval, so a record is applied at most once.
* after :data:`WAIT_SECONDS` a record expires, its approval is voided, and a
  plain note is posted to the owner's chat thread.

Nothing here accepts a PIN or a token. Never raises into a caller.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WAIT_SECONDS = 30 * 60
FILENAME = "ha_pending_pin.json"
HUMAN_RESOLVERS = frozenset({"user_via_chat", "user_via_ui", "user_via_voice"})

#: Approvals already spent (applied or voided), persisted so a restart cannot
#: forget them: without this, a waiting entry planted (or left) for an
#: approval used before the restart could re-apply. Stamps only (the
#: record's ``resolved_at``), never a PIN or a token. Capped and aged out:
#: an approval older than the longest apply window (:data:`WAIT_SECONDS`)
#: is refused by the freshness check anyway, so the TTL has wide headroom.
USED_FILENAME = "ha_used_approvals.json"
USED_TTL_SECONDS = 24 * 3600
USED_MAX = 512

_LOCK = threading.RLock()
_used_path_override = None
_poster = None          # poster(text, conversation_id) -> bool, set by api_server
_bridge_getter = None   # () -> everyday HABridge, set by api_server


def set_poster(fn) -> None:
    global _poster
    _poster = fn


def set_bridge_getter(fn) -> None:
    global _bridge_getter
    _bridge_getter = fn


def _path() -> str:
    try:
        from puffpai.statedir import resolve_runtime_dir
        base = resolve_runtime_dir(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "data"))
    except Exception:                               # pragma: no cover
        base = os.environ.get("PUFFPAI_DATA_DIR") or "."
    return os.path.join(base, FILENAME)


def set_used_path_override(path) -> None:
    """Tests: point the used-approval ledger at a per-test file."""
    global _used_path_override
    _used_path_override = str(path) if path else None


def _used_path() -> str:
    if _used_path_override:
        return _used_path_override
    return os.path.join(os.path.dirname(_path()), USED_FILENAME)


#: Reserved key: when a corrupt ledger was quarantined (epoch). Every
#: approval resolved at or before it may be in the lost set, so it counts as
#: used: the set never shrinks, it is replaced by a superset. Ages out with
#: the TTL, by which time every such approval is far past any apply window.
QUARANTINE_KEY = "__quarantined_at__"


class _LedgerCorrupt(ValueError):
    pass


def _load_used() -> dict:
    """stamp -> when it was spent (epoch), plus QUARANTINE_KEY if set.
    Expired entries dropped. A file that is present but unreadable raises
    :class:`_LedgerCorrupt` (or OSError), so the caller can fail closed."""
    try:
        with open(_used_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise _LedgerCorrupt(str(exc)) from None
    if not isinstance(data, dict):
        raise _LedgerCorrupt("used-approval ledger is not an object")
    now = _now()
    out = {}
    for stamp, at in data.items():
        try:
            at = float(at)
        except (TypeError, ValueError):
            if stamp == QUARANTINE_KEY:
                raise _LedgerCorrupt("bad quarantine marker") from None
            continue
        if isinstance(stamp, str) and now - at <= USED_TTL_SECONDS:
            out[stamp] = at
    return out


def _save_used(used: dict) -> None:
    path = _used_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    marker = used.get(QUARANTINE_KEY)
    stamps = {k: v for k, v in used.items() if k != QUARANTINE_KEY}
    keep = dict(sorted(stamps.items(), key=lambda kv: kv[1])[-USED_MAX:])
    if marker is not None:
        keep[QUARANTINE_KEY] = marker
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keep, fh)
    os.replace(tmp, path)


def _quarantine_used() -> dict:
    """Move a corrupt ledger aside (``.corrupt-<stamp>``, kept for the owner)
    and write a fresh one whose marker refuses every approval that may have
    been in it. Raises if either step fails, so nothing overwrites the
    corrupt file and the caller keeps refusing."""
    path = _used_path()
    now = _now()
    aside = f"{path}.corrupt-{int(now)}"
    os.replace(path, aside)
    used = {QUARANTINE_KEY: now}
    _save_used(used)
    logger.warning("ha pending: used-approval ledger was unreadable; kept it "
                   "as %s and refusing every approval made before now",
                   os.path.basename(aside))
    return used


def _load_used_or_quarantine() -> dict:
    """The ledger, quarantining a corrupt one first. Raises only if the
    corrupt file could not be moved aside (callers then refuse)."""
    try:
        return _load_used()
    except _LedgerCorrupt:
        return _quarantine_used()


def _stamp_epoch(stamp: str):
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _covered(used: dict, stamp: str) -> bool:
    if stamp in used:
        return True
    marker = used.get(QUARANTINE_KEY)
    if marker is None:
        return False
    at = _stamp_epoch(stamp)
    return at is None or at <= float(marker)


def note_used(stamp) -> None:
    """Persist that the approval stamped *stamp* has been spent. Never
    raises (the in-memory spend in ha_provisioning already happened) and
    never shrinks the ledger: a corrupt one is quarantined, not overwritten."""
    if not isinstance(stamp, str) or not stamp:
        return
    with _LOCK:
        try:
            used = _load_used_or_quarantine()
            used[stamp] = _now()
            _save_used(used)
        except Exception as exc:
            logger.warning("ha pending: could not persist a used approval: %s",
                           type(exc).__name__)


def was_used(stamp) -> bool:
    """Has this approval been spent, by this process or before a restart?
    Fails CLOSED: an approval that may have been in a corrupt ledger, or any
    approval while the ledger cannot be read or quarantined, counts as used."""
    if not isinstance(stamp, str) or not stamp:
        return True
    with _LOCK:
        try:
            return _covered(_load_used_or_quarantine(), stamp)
        except Exception as exc:
            logger.warning("ha pending: used-approval ledger unreadable; "
                           "refusing: %s", type(exc).__name__)
            return True


def _load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(entries: list[dict]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh)
    os.replace(tmp, path)


def _now() -> float:
    return time.time()


def _post(text: str, conversation_id) -> None:
    if _poster is None:
        return
    try:
        _poster(text, conversation_id)
    except Exception as exc:
        logger.debug("ha pending: post failed: %s", type(exc).__name__)


def _name(record: dict) -> str:
    ctx = record.get("context") or {}
    payload = ctx.get("payload") or {}
    return str(payload.get("alias") or ctx.get("name") or record.get("label")
               or record.get("entity_id"))[:80]


def _approver_key(requester) -> str | None:
    """A stable, non-reversible tag for a requester id (paired device or
    session key), so the waiting file never holds the raw device id."""
    if not isinstance(requester, str) or not requester:
        return None
    import hashlib
    return hashlib.sha256(("ha-approver:" + requester).encode(
        "utf-8", "replace")).hexdigest()


def add_waiting(record: dict, *, mentioned: bool = False,
                approver: str | None = None) -> dict | None:
    """Keep an APPROVED ha_provision record waiting for the vault PIN.

    *approver* is the paired-device or session id that tapped Approve. With
    none (an anonymous or unidentifiable surface), no vault unlock can finish
    the record; only its PIN can."""
    if not isinstance(record, dict):
        return None
    if record.get("action") != "ha_provision" or record.get("status") != "approved" \
            or record.get("resolved_by") not in HUMAN_RESOLVERS \
            or not isinstance(record.get("resolved_at"), str):
        return None
    entry = {"entity_id": record.get("entity_id"),
             "resolved_at": record["resolved_at"],
             "name": _name(record),
             "conversation_id": (record.get("context") or {}).get("conversation_id"),
             "waiting_since": _now(), "mentioned": bool(mentioned),
             "approver": _approver_key(approver),
             "other_unlock_noted": False}
    with _LOCK:
        entries = [e for e in _load() if e.get("entity_id") != entry["entity_id"]]
        entries.append(entry)
        _save(entries)
    logger.info("ha pending: %s approved, waiting for the vault PIN",
                entry["entity_id"])
    return entry


def expire() -> list[dict]:
    """Drop entries older than WAIT_SECONDS, void them, post a plain note."""
    now = _now()
    with _LOCK:
        entries = _load()
        keep = [e for e in entries if now - float(e.get("waiting_since") or 0) <= WAIT_SECONDS]
        gone = [e for e in entries if e not in keep]
        if gone:
            _save(keep)
    for e in gone:
        try:
            from puffpai import ha_provisioning as _prov
            _prov.void_approval({"resolved_at": e.get("resolved_at")})
        except Exception:                           # pragma: no cover
            pass
        _post(f"Your approval for '{e.get('name')}' expired before the vault was "
              f"unlocked, so I didn't make it. Ask me again if you still want it.",
              e.get("conversation_id"))
    return gone


def list_waiting() -> list[dict]:
    expire()
    with _LOCK:
        return [dict(e) for e in _load()]


def discard(entity_ids) -> None:
    ids = set(entity_ids or [])
    with _LOCK:
        entries = _load()
        keep = [e for e in entries if e.get("entity_id") not in ids]
        if len(keep) != len(entries):
            _save(keep)


def take_unmentioned_notice() -> str | None:
    """The one-time chat/voice mention: "Your approval for 'X' is waiting for
    your vault PIN." Each waiting entry is mentioned once."""
    expire()
    with _LOCK:
        entries = _load()
        fresh = [e for e in entries if not e.get("mentioned")]
        if not fresh:
            return None
        for e in fresh:
            e["mentioned"] = True
        _save(entries)
    names = " and ".join(f"'{e.get('name')}'" for e in fresh)
    plural = len(fresh) > 1
    return (f"Your approval{'s' if plural else ''} for {names} "
            f"{'are' if plural else 'is'} waiting for your vault PIN. Unlock "
            f"the vault and I'll finish.")


def has_unmentioned() -> bool:
    with _LOCK:
        return any(not e.get("mentioned") for e in _load())


def _approved_record(entry: dict) -> dict | None:
    from puffpai.security import consent_requests as _creq
    for req in _creq._load():
        if isinstance(req, dict) and req.get("action") == "ha_provision" \
                and req.get("entity_id") == entry.get("entity_id") \
                and req.get("resolved_at") == entry.get("resolved_at") \
                and req.get("status") == "approved" \
                and req.get("resolved_by") in HUMAN_RESOLVERS:
            return req
    return None


OTHER_DEVICE_NOTE = ("The vault was unlocked on a different device, so I "
                     "didn't finish '{name}'. Unlock it on the device you "
                     "approved it from, or give me your vault PIN for it "
                     "here.")


def _eligible(entry: dict, requester, only) -> bool:
    if only is not None and entry.get("entity_id") in only:
        return True
    key = _approver_key(requester)
    return bool(key and entry.get("approver") and entry.get("approver") == key)


def apply_waiting(session_id: str | None, *, requester: str | None = None,
                  only=None) -> list[str]:
    """Apply the waiting approvals this unlocked session may finish.

    Eligible: entries whose recorded approver is *requester* (the device or
    session that fired the unlock), or whose entity id is in *only* (the
    caller took the PIN for exactly those asks). Nothing else; with neither
    argument nothing is applied. Only runs when the session actually yields
    the admin credential. Entries are TAKEN before applying, so two unlocks
    can never apply one twice; an entry whose apply reports the vault locked
    again goes back on the list. An entry this unlock may not finish stays
    waiting, and its thread is told so once.
    """
    only = set(only) if only is not None else None
    expire()
    try:
        from puffpai.security.ha_token_source import resolve_ha_admin_token
        if not resolve_ha_admin_token(session_id).token:
            return []
    except Exception as exc:
        logger.debug("ha pending: credential check failed: %s", type(exc).__name__)
        return []
    notes = []
    with _LOCK:
        all_entries = _load()
        if not all_entries:
            return []
        entries = [e for e in all_entries if _eligible(e, requester, only)]
        rest = [e for e in all_entries if e not in entries]
        if only is None:
            for e in rest:
                if not e.get("other_unlock_noted"):
                    e["other_unlock_noted"] = True
                    notes.append(e)
        _save(rest)
    for e in notes:
        _post(OTHER_DEVICE_NOTE.format(name=e.get("name")),
              e.get("conversation_id"))
    if not entries:
        return []
    from puffpai import ha_provisioning as _prov
    from puffpai.ha_bridge import HUMAN_CONFIRMED
    bridge = None
    try:
        bridge = _bridge_getter() if _bridge_getter else None
    except Exception:
        bridge = None
    results, retry = [], []
    for entry in entries:
        record = _approved_record(entry)
        if record is None or _prov.approval_spent(entry.get("resolved_at")):
            continue                    # never approved, or already applied
        try:
            out = _prov.apply_approved_change(
                request=record, bridge=bridge, human_confirmed=HUMAN_CONFIRMED,
                vault_session_id=session_id, freshness_seconds=WAIT_SECONDS) or {}
        except Exception as exc:
            out = {"message": f"I couldn't make '{entry.get('name')}' "
                              f"({type(exc).__name__})."}
        if out.get("vault_locked"):
            retry.append(entry)
            continue
        message = out.get("message") or f"Done: '{entry.get('name')}'."
        results.append(message)
        _post(message, entry.get("conversation_id"))
    if retry:
        with _LOCK:
            _save(_load() + retry)
    return results


def on_unlock(session_id: str) -> None:
    """Vault unlock listener. Finishes only what the unlocking device
    approved. Never raises."""
    try:
        requester = None
        try:
            from puffpai.security import vault as _vault
            requester = _vault.unlock_requester(session_id)
        except Exception:
            requester = None
        apply_waiting(session_id, requester=requester)
    except Exception as exc:                        # pragma: no cover
        logger.warning("ha pending: apply after unlock failed: %s",
                       type(exc).__name__)


def reset() -> None:
    """Tests only."""
    with _LOCK:
        for path in (_path(), _used_path()):
            try:
                os.remove(path)
            except OSError:
                pass
