"""Owner-controlled writes to the egress door registry.

``egress_doors`` is the read side: it says which doors exist and whether each
is open, and it refuses calls. This module is the only place that *sets* a
door's flag, so the write path has one implementation, one audit line and one
set of refusals rather than one per caller.

Why it is a separate module
---------------------------
``egress_doors`` is the enforcement point. Nothing that decides whether a
socket may open should also be able to rewrite the file it reads that decision
out of; keeping the writer next door means the enforcement module stays a pure
function of (env, config, registry) and can be reasoned about — and tested —
without a write path in it.

What it refuses, and why those are refusals rather than no-ops
--------------------------------------------------------------
A toggle that appears to work and changes nothing is worse than a toggle that
says no. Three cases produce a refusal instead of a silent write:

* **Unenforced doors** (``enforcement="none"``; no built-in door today). The
  registry lists them for visibility and does not gate them; writing the flag
  would put a switch in the owner's hand that stops nothing. Refused with the
  reason.
* **Derived doors** (``connectivity_probe``). Its state is computed from the
  doors that are open right now, so the value written and the value read back
  would disagree. Refused.
* **Externally-enforced doors whose call site ignores this flag.**
  ``enforcement="external"`` means the socket is opened somewhere this registry
  cannot wrap, which covers two different situations, and only one of them is
  toggleable here: a door whose call site nonetheless ASKS this registry is a
  real control, and one that does not would move nothing but the status
  display. Every external door is in the first group: ``web_search``,
  ``web_fetch``, ``page_reader`` (asked by the dashboard server's reader
  route), ``tv_free_channels``, ``model_pull`` and
  ``embedding_model_download``. See :data:`EXTERNAL_DOORS_HONOURING_FLAG`,
  whose every member is a claim about one specific line of code that a test
  greps for. No door is in the second group today; the refusal stays because
  the next externally enforced door added will start there.
* **An environment override in force.** ``egress_allowed()`` gives
  ``PUFFPAI_EGRESS_*`` precedence over every file, so with one exported the
  write is real and completely inert. Refused, naming the variable, rather than
  leaving an owner to conclude the switch is broken.

Where the flag is written
-------------------------
Into the **owner overlay**, ``egress_doors.overlay_path()`` —
``$PUFFPAI_DATA_DIR/state/egress_doors.json``. Not into the appliance config,
which on a hardened box lives outside the unit's ``ReadWritePaths`` and cannot
be written at all.

The design deliberately does NOT add ``/opt/puffpai/config`` to
``ReadWritePaths``. That would let the API server rewrite every appliance
setting, so an RCE in it would inherit that reach — a large posture change
bought to flip a boolean. The overlay is one small file containing only door
booleans, inside the data directory the service already writes for its ledger,
so the new write permission is scoped to precisely the thing being changed.

Precedence is ``env > overlay > config > code default`` (see
:func:`egress_doors._resolve_flag`), and ``/system/status`` publishes a
``source`` per door saying which tier decided it. The config file remains a
write fallback only for a dev checkout where no data dir is writable.

Everything here is least-privilege by default: an unparseable config file or
overlay is never overwritten, an unknown door is never created, and the
returned status row is re-read from the registry rather than assumed from what
was written.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from puffpai.logging_config import get_logger
from puffpai.security import egress_doors, egress_ledger, egress_policy

logger = get_logger(__name__)

#: Mode for a config file this module has to create from nothing. It never
#: should have to — the appliance ships one — but if it does, the restrictive
#: choice is owner-only. An EXISTING file keeps its own mode (see
#: :func:`_write_config`); this constant is only the floor for a fresh one.
NEW_CONFIG_MODE = 0o600

#: Who the ledger records as the actor for an API-initiated toggle. Not a user
#: identity: the API key is a single shared appliance credential, so claiming
#: to know which person turned the switch would be a fabrication.
ACTOR = "owner-api"

#: Doors with ``enforcement="external"`` whose own call site nonetheless reads
#: this registry's flag, so writing it really does change what the appliance
#: does. Every member is a claim about one specific line of code and
#: ``tests/test_egress_api.py`` greps for that line, so the day a
#: refactor drops the check the set stops being true out loud instead of
#: quietly leaving an owner with a dead switch.
#:
#: ``web_fetch`` is in it because ``agents/toolsmith.py`` calls
#: ``require_open("web_fetch")`` before its own SSRF gates. Its per-request
#: ``Budget.allow_egress`` is still required; the door is a second, standing
#: consent rather than a status-only display.
#:
#: ``model_pull`` and ``embedding_model_download`` are in it because their
#: call sites (``brain_health.pull_model`` and
#: ``embeddings.chroma_default_may_run``) ask the registry, so this route
#: writes their switch and the Settings card shows both as enforced.
#:
#: Defined in :mod:`egress_doors` and re-exported here so ``door_status()`` can
#: publish ``writable`` without importing the writer, and the two can never
#: drift apart.
EXTERNAL_DOORS_HONOURING_FLAG = egress_doors._FLAG_HONOURED_EXTERNAL


class ToggleRefused(Exception):
    """A door toggle was refused. ``status`` is the HTTP code to answer with.

    ``reason`` is written to be shown to a person verbatim — the owner's next
    question after "it didn't work" is "why", and a generic 409 does not
    answer it.
    """

    def __init__(self, reason: str, status: int = 409, door_id: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.door_id = door_id


def config_path() -> str | None:
    """The config file the door flags live in, or None if it cannot be found.

    Delegates to the registry so the writer and the reader can never disagree
    about which file is authoritative — including under a test that repoints
    it.
    """
    return egress_doors._config_path()


def overlay_path() -> str:
    """The owner overlay this module writes. Delegates to the registry, so the
    writer and the reader can never disagree about which file is authoritative."""
    return egress_doors.overlay_path()


def toggleable(door: "egress_doors.Door") -> tuple[bool, str]:
    """May this door's flag be set from the API? ``(ok, reason_if_not)``."""
    if door.derived:
        return False, (
            f"The {door.id} door has no switch of its own: its state is "
            f"derived from the doors that are open right now. Open or close "
            f"those instead."
        )
    if door.enforcement == "none":
        return False, (
            f"The {door.id} door is listed for visibility but this registry "
            f"does not enforce it, so setting the switch would not change "
            f"what leaves the house. It is not a control yet."
        )
    if (door.enforcement == "external"
            and door.id not in EXTERNAL_DOORS_HONOURING_FLAG):
        return False, (
            f"The {door.id} door is stopped by its own gate, which does not "
            f"read this switch — setting it would change the status display "
            f"and nothing else. Change the control that actually applies."
        )
    if egress_doors._is_inert(door):
        # A door that names no host refuses every URL open or shut, because
        # ``_check_url`` asks ``host in door.hosts``. Every one of the five
        # shipped apps declares ``hosts: []`` — "this app reaches nowhere" —
        # and offering the owner a switch for it would be a control that
        # cannot change what leaves the house. Same rule as ``door_status()``
        # ``writable``; the two are computed from one predicate so they can
        # never disagree.
        app = egress_doors.app_of(door)
        return False, (
            f"The {door.id} door names no host"
            + (f": the '{app}' app declares that it reaches nowhere. " if app
               else ". ")
            + "There is nothing to open — it refuses every address whether "
              "the switch is on or off."
        )
    return True, ""


def env_override(door: "egress_doors.Door") -> str | None:
    """The env var forcing this door's state, or None if the config decides.

    Mirrors the precedence in :func:`egress_doors.egress_allowed`: a set,
    non-empty ``PUFFPAI_EGRESS_*`` wins over the config file — including when
    its value is junk, which fails the door shut.
    """
    raw = os.environ.get(door.env_var)
    if raw is not None and raw.strip() != "":
        return door.env_var
    return None


def status_row(door_id: str) -> dict | None:
    """The published status for one door, re-read from the registry."""
    for row in egress_doors.door_status():
        if row.get("id") == door_id:
            return row
    return None


def _write_config(path: str, flag: str, value: bool) -> None:
    """Set ``egress_doors.<flag>`` in the config file, atomically.

    Read-modify-write, so every other setting in the file survives. Three
    properties matter and each is deliberate:

    * **Never clobber what we could not parse.** If the file exists and is not
      a JSON object, this raises rather than replacing it with a fresh dict
      holding one key. Losing an appliance's whole config to a stray byte is a
      worse outcome than a failed toggle.
    * **Atomic.** Temp file in the same directory, flushed and fsynced, then
      ``os.replace`` — atomic on POSIX and Windows alike. A crash mid-write
      leaves the old config intact, never a truncated one that would fail
      shut *and* lose every other setting.
    * **Mode preserved.** The temp file is chmod'ed to the target's existing
      mode before the rename, so a config the installer locked down does not
      silently come back world-readable.
    """
    data: dict = {}
    mode = NEW_CONFIG_MODE
    if os.path.exists(path):
        try:
            st = os.stat(path)
            mode = st.st_mode & 0o777
        except OSError:
            mode = NEW_CONFIG_MODE
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError(
                f"{path} is not a JSON object; refusing to overwrite it")
        data = loaded

    section = data.get(egress_doors.CONFIG_SECTION)
    if section is None:
        section = {}
    elif not isinstance(section, dict):
        raise ValueError(
            f"{egress_doors.CONFIG_SECTION} in {path} is not an object; "
            f"refusing to overwrite it")
    else:
        section = dict(section)
    section[flag] = bool(value)
    data[egress_doors.CONFIG_SECTION] = section

    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            # Windows implements a subset of chmod; the replace below still
            # inherits the destination's ACL, so this is not fatal there.
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _read_overlay_for_merge(path: str) -> dict:
    """The overlay's current ``doors`` map, for a read-modify-write.

    Raises ``ValueError`` when the file exists and will not parse. Rebuilding
    it from scratch would silently discard every OTHER door the owner had set
    — and those settings are the record of what this household consented to,
    so losing them quietly is worse than refusing the toggle.
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a JSON object")
    doors = loaded.get("doors", {})
    if not isinstance(doors, dict):
        raise ValueError(f"{path} has a non-object 'doors'")
    return dict(doors)


def _atomic_json_write(path: str, payload: dict, mode: int,
                       dir_mode: int | None = None) -> None:
    """Write *payload* to *path* atomically at *mode*.

    Temp file in the same directory, flushed and fsynced, chmod'ed BEFORE the
    rename so the file is never briefly readable at the ambient umask, then
    ``os.replace`` — atomic on POSIX and Windows alike. A crash mid-write
    leaves the previous file intact rather than a truncated one.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    if dir_mode is not None:
        try:
            os.chmod(directory, dir_mode)
        except OSError:
            pass  # Windows implements a subset of chmod
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _write_overlay(path: str, flag: str, value: bool) -> None:
    """Set one flag in the owner overlay, preserving every other door."""
    doors = _read_overlay_for_merge(path)
    doors[flag] = bool(value)
    _atomic_json_write(path, {
        "version": 1,
        "kind": egress_doors.OVERLAY_KIND,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "doors": doors,
    }, egress_doors.OVERLAY_FILE_MODE, egress_doors.OVERLAY_DIR_MODE)


def _persist(d: "egress_doors.Door", want_open: bool) -> str:
    """Write the flag. Returns ``"overlay"`` or ``"config"``; raises ToggleRefused.

    **The overlay is tried first and is the normal answer.** The appliance
    config lives in ``/opt/puffpai/config``, which a hardened unit
    (``ProtectSystem=strict``) deliberately keeps out of ``ReadWritePaths``,
    so a write there answers 500. Widening the unit is NOT the answer: giving
    the API server write access to the whole appliance configuration so that a
    boolean can be flipped hands an RCE in that server the ability to rewrite
    every setting on the appliance. The overlay is a single file of booleans inside
    the data dir the service already writes, so the write permission this needs
    is scoped to exactly the thing being changed.

    The config file stays as a fallback for dev boxes where no data dir is
    writable but the checkout is. It is only reached when the overlay write
    actually failed — never preferred, because a config write would be
    SHADOWED by any overlay opinion on the same flag (see the precedence chain
    in :func:`egress_doors._resolve_flag`).
    """
    overlay = egress_doors.overlay_path()
    try:
        _write_overlay(overlay, d.config_flag, want_open)
        return "overlay"
    except ValueError as exc:
        # The overlay exists and will not parse. Rebuilding it would discard
        # the owner's other doors, so this is a refusal, not a fallback.
        logger.warning("egress door overlay unusable for %s: %s", d.id, exc)
        raise ToggleRefused(
            f"The owner overlay at {overlay} will not parse, so no door can "
            f"be changed without discarding the other doors it holds. Fix or "
            f"remove that file.", status=409, door_id=d.id) from exc
    except OSError as overlay_exc:
        logger.warning("egress door overlay unwritable (%s): %s — trying the "
                       "config file", overlay, overlay_exc)

    # Fallback: only for a box where the data dir is not writable.
    shadow = egress_doors._raw_overlay()
    if isinstance(shadow, dict) and d.config_flag in shadow:
        raise ToggleRefused(
            f"The owner overlay at {overlay} already sets this door and could "
            f"not be updated, and it takes precedence over the config file — "
            f"so writing the config would change nothing.",
            status=503, door_id=d.id)

    path = config_path()
    if path:
        try:
            _write_config(path, d.config_flag, want_open)
            return "config"
        except (OSError, ValueError) as exc:
            logger.warning("egress door toggle could write neither the "
                           "overlay nor the config for %s: %s", d.id, exc)

    raise ToggleRefused(
        "This appliance has nowhere writable to record the change: neither "
        f"{overlay} nor the appliance config file could be written. The door "
        "state is unchanged. An operator needs to make the data directory "
        "writable for the service.", status=503, door_id=d.id)


def _has_owner_switch(d: "egress_doors.Door") -> bool:
    """True for a built-in service: its switch is ALSO kept in
    ``outside_switches.json``, which the engine reads (``is_off``) and the
    registry reads as the owner's switch. Apps' and skills' doors are not."""
    return egress_doors._BY_ID.get(d.id) is d  # noqa: SLF001 - same family


def _set_owner_switch(d: "egress_doors.Door", on: bool) -> None:
    """Write the same answer to ``outside_switches.json``. Raises
    :class:`ToggleRefused` (503) when that list cannot be read."""
    from puffpai.security import outside_switches
    try:
        outside_switches.set_switch(d.id, on, by="owner")
    except outside_switches.SwitchesUnreadable as exc:
        raise ToggleRefused(exc.message, status=503, door_id=d.id) from exc


def sync_overlay(door_id: str, on: bool) -> None:
    """After the owner's switch for *door_id* was set elsewhere (the list of
    outside services), make the overlay say the same thing when it already
    holds an answer for that door, so the two stores never disagree. Never
    raises; an overlay that cannot be written keeps its answer, and when the
    two disagree OFF wins (:func:`egress_doors._resolve_flag`)."""
    d = egress_doors._BY_ID.get(str(door_id or "").strip().lower())  # noqa: SLF001
    if d is None:
        return
    try:
        shadow = egress_doors._raw_overlay()  # noqa: SLF001 - same family
        if isinstance(shadow, dict) and d.config_flag in shadow:
            _write_overlay(egress_doors.overlay_path(), d.config_flag, bool(on))
    except Exception as exc:  # noqa: BLE001 - OFF still wins at read time
        logger.warning("egress door overlay not brought in line for %s: %s",
                       d.id, type(exc).__name__)
    egress_doors.reset_cache()


def set_door_open(door_id: str, want_open: bool) -> dict:
    """Open or close one door and return its freshly-read status row.

    Raises :class:`ToggleRefused` — carrying the HTTP status the caller should
    answer with — for an unknown door (404), an unenforced or derived one
    (409), one an environment variable is currently forcing (409), an
    unparseable overlay (409), or nowhere writable to record the change (503).
    """
    d = egress_doors.door(door_id)
    if d is None:
        raise ToggleRefused(f"No egress door named {str(door_id)[:64]!r}.",
                            status=404, door_id=str(door_id)[:64])

    ok, why = toggleable(d)
    if not ok:
        raise ToggleRefused(why, status=409, door_id=d.id)

    forced = env_override(d)
    if forced:
        raise ToggleRefused(
            f"{forced} is set in this appliance's environment. It takes "
            f"precedence over both the owner overlay and the config file, so "
            f"this switch would have no effect. Change it in the service unit "
            f"instead.",
            status=409, door_id=d.id)

    want_open = bool(want_open)
    was_open, _reason = egress_doors.egress_allowed(d.id)
    wrote_to = _persist(d, want_open)
    # One answer in both stores: a built-in service's switch is also kept in
    # outside_switches.json (what the engine reads). Closing writes the overlay
    # first, so the door is shut even if the list cannot be written; opening
    # is undone in the overlay when the list cannot be written. Not after the
    # config-file fallback (a dev checkout with no writable data dir, where
    # that list lives too): the engine then keeps reading the service as off.
    if wrote_to == "overlay" and _has_owner_switch(d):
        try:
            _set_owner_switch(d, want_open)
        except ToggleRefused:
            if want_open:
                try:
                    _persist(d, False)
                finally:
                    egress_doors.reset_cache()
                raise
            logger.warning("egress door %s closed in the overlay; the list of "
                           "outside switches could not be written", d.id)

    # The registry caches both files; without this the write lands on disk and
    # /system/status keeps serving the old answer until a restart.
    egress_doors.reset_cache()

    row = status_row(d.id) or {}
    now_open = bool(row.get("open", want_open))

    # Audit before returning: an owner asking "when did this open" must get an
    # answer from the same trail that records the requests themselves.
    try:
        egress_ledger.append({
            "event": "door_toggled",
            "kind": "door_toggled",
            "door": d.id,
            "open": now_open,
            "by": ACTOR,
            "result": "opened" if now_open else "closed",
            "stored": wrote_to,
            "purpose": d.purpose[:160],
            "third_party": d.third_party[:160] if d.third_party else None,
        })
    except Exception as exc:  # an audit fault must not lose the write
        logger.debug("egress door toggle ledger append failed for %s: %s",
                     d.id, exc)

    logger.info("egress door %s set to %s by %s (was %s), stored in the %s",
                d.id, "open" if now_open else "shut", ACTOR,
                "open" if was_open else "shut", wrote_to)
    return row


# ── skill door flags ────────────────────────────────────────
#
# A skill's door flag is written HERE, by :func:`set_door_open`, when the owner
# approves the consent card, so it has to be un-written here too. Otherwise
# ``skills.store.remove()`` unregisters the door but leaves the owner's
# ``skill_<id>_<n>_<digest>: true`` sitting in the overlay. Inert while
# no door of that name is registered, and a trap the moment one is again,
# because that flag is the ONLY on-disk record of a yes.
#
# Both functions below only ever DELETE keys. Nothing here can write ``true``,
# so no reach is granted from this section; :func:`set_door_open` stays the one
# way a door opens, and it is reachable from the install/enable path only —
# never from ``skills.dispatch`` or ``skills.broker``, which ask
# ``egress_doors`` and are refused.

#: Namespace every skill door flag lives in. Mirrors
#: :func:`egress_doors.skill_door_flag`; no built-in or app flag starts with it.
SKILL_FLAG_PREFIX = "skill_"


def skill_flag_prefix(skill_id) -> str | None:
    """``skill_<id>_`` for a usable id, else ``None``.

    The id is validated against the same pattern the door builder uses, so a
    junk, empty or wildcard-ish id matches NOTHING rather than a whole
    namespace — the failure mode of a prefix delete is deleting too much.
    """
    sid = str(skill_id or "").strip().lower()
    if not egress_doors._APP_ID_RE.match(sid):
        return None
    return f"{SKILL_FLAG_PREFIX}{sid}_"


def _drop_overlay_flags(predicate) -> int:
    """Delete every overlay key *predicate* accepts. Never raises.

    Returns how many keys were removed. An overlay that will not parse is LEFT
    ALONE, exactly as :func:`_persist` leaves it alone: rebuilding it would
    discard every other door this household consented to. The consequence is
    narrow — a stale skill flag can outlive a removal on a box whose overlay is
    already broken, and a broken overlay already fails every door shut
    (:func:`egress_doors._raw_overlay` returns ``None``).
    """
    path = egress_doors.overlay_path()
    try:
        doors = _read_overlay_for_merge(path)
    except (OSError, ValueError) as exc:
        logger.warning("egress: skill flags left in place — overlay %s is "
                       "unusable: %s", path, exc)
        return 0
    doomed = [key for key in doors if isinstance(key, str) and predicate(key)]
    if not doomed:
        return 0
    for key in doomed:
        doors.pop(key, None)
    try:
        _atomic_json_write(path, {
            "version": 1,
            "kind": egress_doors.OVERLAY_KIND,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "doors": doors,
        }, egress_doors.OVERLAY_FILE_MODE, egress_doors.OVERLAY_DIR_MODE)
    except OSError as exc:
        logger.warning("egress: skill flags not cleared, overlay %s is not "
                       "writable: %s", path, exc)
        return 0
    egress_doors.reset_cache()
    logger.info("egress: cleared %d stale skill door flag(s): %s",
                len(doomed), ", ".join(sorted(doomed)))
    return len(doomed)


def clear_skill_flags(skill_id) -> int:
    """Forget every ``skill_<id>_*`` flag the owner ever set for this skill.

    Called when a skill is removed or switched off, so that "its door is gone"
    is true on disk and not merely true in the registry's memory. Idempotent,
    and a no-op for an id this registry would refuse to build a door for.
    """
    prefix = skill_flag_prefix(skill_id)
    if prefix is None:
        logger.warning("egress: refusing to clear skill flags for unusable "
                       "id %r", str(skill_id)[:64])
        return 0
    return _drop_overlay_flags(lambda key: key.startswith(prefix))


def prune_skill_flags(keep) -> int:
    """Drop every flag in the skill namespace that is not in *keep*.

    *keep* is the exact set of flags that installed, ENABLED skills are
    entitled to right now (``skills.store.entitled_door_flags()``). Everything
    else under ``skill_`` is stale: a removed skill's leftover ``true``, a
    disabled skill's, or an older host digest whose card nobody approved.

    Two narrowings, both deliberate: only the ``skill_`` namespace is
    considered, and a flag belonging to a door registered right now that is NOT
    a skill door is never dropped (belt and braces — no built-in or app flag
    uses this prefix today, and this keeps it true if one ever tries to).
    """
    keep = {k for k in (keep or ()) if isinstance(k, str) and k}
    protected = {d.config_flag for d in egress_doors.all_doors()
                 if not str(d.config_flag).startswith(SKILL_FLAG_PREFIX)}

    def _stale(key: str) -> bool:
        return (key.startswith(SKILL_FLAG_PREFIX) and key not in keep
                and key not in protected)

    return _drop_overlay_flags(_stale)


# ── egress policies ─────────────────────────────────────────────────────────
#
# A door says WHERE the appliance may go; a policy says WHO on the appliance may walk
# through an open one. Same write posture, deliberately: its own overlay file
# in the same data dir, atomic write, mode preserved, one ledger line, and a
# refusal rather than an inert write when the environment is forcing the value.
#
# Separate file, not a key in the door overlay: a policy write must never be
# able to touch a door flag, and a corrupt door overlay must never silently
# change a policy. The blast radius of each write stays what it says it is.


def policy_path() -> str:
    """The policy overlay this module writes. Delegates to the reader."""
    return egress_policy.policy_path()


def policy_env_override(p: "egress_policy.Policy") -> str | None:
    """The env var forcing this policy, or None if a file decides.

    Mirrors the precedence in :func:`egress_policy.resolve`: a set, non-empty
    variable wins over both the overlay and the config — including when its
    value is junk, which fails the policy off.
    """
    raw = os.environ.get(p.env_var)
    if raw is not None and raw.strip() != "":
        return p.env_var
    return None


def policy_row(policy_id: str) -> dict | None:
    """The published status for one policy, re-read from the registry."""
    for row in egress_policy.policy_status():
        if row.get("id") == policy_id:
            return row
    return None


def _read_policy_overlay_for_merge(path: str) -> dict:
    """The overlay's current ``policies`` map, for a read-modify-write.

    Raises ``ValueError`` when the file exists and will not parse. Rebuilding
    it would silently discard every OTHER policy the owner had set, and those
    settings are the record of what this household consented to.
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a JSON object")
    policies = loaded.get("policies", {})
    if not isinstance(policies, dict):
        raise ValueError(f"{path} has a non-object 'policies'")
    return dict(policies)


def _write_policy_overlay(path: str, policy_id: str, value: bool) -> None:
    """Set one policy in the owner overlay, preserving every other."""
    policies = _read_policy_overlay_for_merge(path)
    policies[policy_id] = bool(value)
    _atomic_json_write(path, {
        "version": 1,
        "kind": egress_policy.POLICY_KIND,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policies": policies,
    }, egress_policy.POLICY_FILE_MODE, egress_policy.POLICY_DIR_MODE)


def set_policy(policy_id: str, value: bool) -> dict:
    """Turn one egress policy on or off; return its freshly-read status row.

    Raises :class:`ToggleRefused` carrying the HTTP status to answer with:
    404 for an unknown policy, 409 for one an environment variable is forcing
    or an overlay that will not parse, 503 when there is nowhere writable.

    There is no config-file fallback, unlike the door writer. The door path
    keeps one because it predates the overlay and a dev checkout may have a
    writable config and no data dir; a policy has only ever had the overlay,
    and adding a second writable tier for it would be adding a way to write
    the appliance config that nothing needs.
    """
    p = egress_policy.policy(policy_id)
    if p is None:
        raise ToggleRefused(
            f"No egress policy named {str(policy_id)[:64]!r}.",
            status=404, door_id=str(policy_id)[:64])

    forced = policy_env_override(p)
    if forced:
        raise ToggleRefused(
            f"{forced} is set in this appliance's environment. It takes "
            f"precedence over both the owner overlay and the config file, so "
            f"this switch would have no effect. Change it in the service unit "
            f"instead.",
            status=409, door_id=p.id)

    value = bool(value)
    was_on = egress_policy.enabled(p.id)
    overlay = policy_path()
    try:
        _write_policy_overlay(overlay, p.id, value)
    except ValueError as exc:
        logger.warning("egress policy overlay unusable for %s: %s", p.id, exc)
        raise ToggleRefused(
            f"The policy overlay at {overlay} will not parse, so no setting "
            f"can be changed without discarding the others it holds. Fix or "
            f"remove that file.", status=409, door_id=p.id) from exc
    except OSError as exc:
        logger.warning("egress policy overlay unwritable (%s): %s",
                       overlay, exc)
        raise ToggleRefused(
            "This appliance has nowhere writable to record the change: "
            f"{overlay} could not be written. The setting is unchanged. An "
            "operator needs to make the data directory writable for the "
            "service.", status=503, door_id=p.id) from exc

    # The reader caches both the overlay and the config; without this the write
    # lands on disk and /system/status keeps serving the old answer.
    egress_policy.reset_cache()

    row = policy_row(p.id) or {}
    now_on = bool(row.get("enabled", value))

    try:
        egress_ledger.append({
            "event": "policy_toggled",
            "kind": "policy_toggled",
            "door": p.id,
            "open": now_on,
            "by": ACTOR,
            "result": "enabled" if now_on else "disabled",
            "stored": "overlay",
            "purpose": p.purpose[:160],
            "third_party": None,
        })
    except Exception as exc:  # an audit fault must not lose the write
        logger.debug("egress policy ledger append failed for %s: %s",
                     p.id, exc)

    logger.info("egress policy %s set to %s by %s (was %s)",
                p.id, "on" if now_on else "off", ACTOR,
                "on" if was_on else "off")
    return row


__all__ = [
    "ACTOR",
    "EXTERNAL_DOORS_HONOURING_FLAG",
    "NEW_CONFIG_MODE",
    "ToggleRefused",
    "config_path",
    "env_override",
    "overlay_path",
    "policy_env_override",
    "policy_path",
    "policy_row",
    "set_door_open",
    "set_policy",
    "status_row",
    "sync_overlay",
    "toggleable",
]
