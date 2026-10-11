"""At-rest handling for the HTML apps' JSON state (``app_data/*.json``).

Two things live here and nothing else:

* :func:`write_json_secure` — the ONE way an app document reaches disk. It
  writes to a sibling temp file and ``os.replace``s it into place, so a crash
  or a concurrent reader can never see a half-written document, and it lands
  the file at ``0600`` in a ``0700`` directory.
* :func:`sweep_app_data_modes` — a one-time boot sweep that tightens whatever
  earlier code (or an earlier umask) left behind.

WHY THIS MODULE EXISTS
----------------------
A plain ``open(path, 'w')`` in ``AppData._save`` has two consequences:

1. **Mode drift.** A file's mode is set at CREATION from the process umask, and
   the backend can be started by several different launchers over the
   appliance's life. The two MOST sensitive documents can end up with the
   LOOSEST modes, ``calendar.json`` (medications, bills) at ``0644`` and
   ``fitfam.json`` (a family member's health history) at ``0664``, while
   another document is ``0600``. ``open(path,'w')`` truncates in place and
   therefore PRESERVES whatever mode the file already had, so the drift is
   permanent: nothing in the write path would ever correct it.
2. **Non-atomic replacement.** A truncate-then-write is a window in which the
   document is empty or short. That window is also what tempts a ``.bak-``
   copy as a safety net, and such a copy leaves a world-readable stale copy of
   the family calendar on disk. An atomic write removes the motive for the
   backup, so this module addresses BOTH problems, not just the mode one.

WHAT THIS IS NOT
----------------
This is permissions and durability, not encryption. A ``0600`` file is protected
from other UNIX accounts on the appliance and from a static file server; it is NOT
protected from the appliance's own account, from physical media access, or from
a backup that copies it elsewhere. At-rest encryption for ``calendar.json`` and
``fitfam.json`` is a separate, scoped piece of work (a per-field projection /
decryption pattern) and is deliberately not attempted here.

PLATFORM
--------
``os.chmod`` on Windows only toggles the read-only bit, so every mode operation
is best-effort and never fatal — the dev checkout must keep working. The
security property is asserted on POSIX, which is what the appliance is.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Owner-only. Every app document, without exception — the tier of the document
#: does not change the floor, because the floor is what stops the NEXT app from
#: being the one that leaks.
APP_DATA_FILE_MODE = 0o600

#: Owner-only directory. Removes the "list the household's apps" read that a
#: 0755 directory grants to every account on the appliance.
APP_DATA_DIR_MODE = 0o700

#: Bits that must NOT be set on an app-data file. Group and other, all classes.
_LOOSE_BITS = (stat.S_IRWXG | stat.S_IRWXO)

#: Only these are swept/tightened. A sweep that chmods everything it finds is a
#: sweep that will one day chmod something that needed to be readable.
_SWEEP_SUFFIXES = (".json",)

#: POSIX permission bits are a POSIX mechanism. On Windows ``os.stat`` reports a
#: synthetic 0666/0777 and ``os.chmod`` only toggles the read-only flag, so a
#: mode check there would report every file as world-readable and every chmod as
#: having failed. Reporting "unsupported platform" is the honest answer;
#: reporting "0600, secured" on a system where that is not what happened would
#: be a status line that lies, which is worse than no status line at all. The
#: appliance is Linux, which is where the guarantee has to hold.
MODES_ENFORCEABLE = (os.name == "posix")


def _chmod_quiet(path: str, mode: int) -> bool:
    """Best-effort chmod. Returns True if the call went through.

    Never raises: a permissions tweak must not be the thing that stops the
    appliance from writing the household's calendar.
    """
    try:
        os.chmod(path, mode)
        return True
    except OSError as exc:
        logger.debug("app-data chmod failed for %s: %s",
                     os.path.basename(path), exc)
        return False


def _is_loose(path: str) -> bool:
    """True if the file grants ANY group/other access.

    Always False where modes are not enforceable (see
    :data:`MODES_ENFORCEABLE`), so the sweep degrades to a no-op on a dev
    checkout instead of logging a permissions error on every start.
    """
    if not MODES_ENFORCEABLE:
        return False
    try:
        return bool(stat.S_IMODE(os.stat(path).st_mode) & _LOOSE_BITS)
    except OSError:
        return False


def ensure_secure_dir(directory: str) -> None:
    """Create ``directory`` if needed and hold it at :data:`APP_DATA_DIR_MODE`.

    Idempotent and cheap enough to call on every write: ``os.makedirs`` with
    ``exist_ok`` is a single stat in the common case, and the chmod only runs
    when the mode is actually loose.
    """
    try:
        os.makedirs(directory, mode=APP_DATA_DIR_MODE, exist_ok=True)
    except OSError as exc:
        logger.debug("app-data dir create failed: %s", exc)
        return
    if not MODES_ENFORCEABLE:
        return
    try:
        if stat.S_IMODE(os.stat(directory).st_mode) & _LOOSE_BITS:
            _chmod_quiet(directory, APP_DATA_DIR_MODE)
    except OSError:
        pass


def write_json_secure(path: str, payload, *, indent: int = 2,
                      default=str, ensure_ascii: bool = False) -> None:
    """Serialise ``payload`` to ``path`` atomically, at :data:`APP_DATA_FILE_MODE`.

    Order is load-bearing:

    1. ``mkstemp`` in the SAME directory (a cross-device ``os.replace`` would
       not be atomic) — and ``mkstemp`` already creates at ``0600``, so the
       plaintext never exists at a looser mode even for an instant.
    2. Serialise, ``flush``, ``fsync`` — the bytes are on the platter before
       anything points at them.
    3. ``os.replace`` — atomic on POSIX and on Windows. A reader either sees the
       whole old document or the whole new one, never a truncated file.
    4. ``fsync`` the directory on POSIX so the rename itself survives power loss.

    On any failure the temp file is removed and the ORIGINAL document is left
    exactly as it was. That is the property that makes ad-hoc ``.bak`` copies
    unnecessary — and ad-hoc ``.bak`` copies are how household data ends up
    world-readable and outside every retention rule.

    Raises whatever the serialisation or the filesystem raises: a caller that
    cannot persist must find out, not silently continue with a stale document.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_secure_dir(directory)

    base = os.path.basename(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + base + ".",
                               suffix=".tmp")
    try:
        # mkstemp is already 0600; assert it explicitly so the guarantee is
        # visible here rather than inherited from a stdlib detail.
        _chmod_quiet(tmp, APP_DATA_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # fdopen owns it now; the finally must not double-close
            json.dump(payload, handle, indent=indent, default=default,
                      ensure_ascii=ensure_ascii)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # Defensive: the replaced-in file carries the temp file's mode, but a future
    # platform quirk here would be silent, so re-assert rather than assume.
    if _is_loose(path):
        _chmod_quiet(path, APP_DATA_FILE_MODE)

    if hasattr(os, "O_DIRECTORY"):  # POSIX only
        try:
            dir_fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def sweep_app_data_modes(directory: str) -> dict:
    """Tighten every ``*.json`` in ``directory`` to :data:`APP_DATA_FILE_MODE`.

    Run once at service start. It exists because :func:`write_json_secure` only
    fixes a file the appliance happens to rewrite — a document nobody has
    touched since the umask changed would keep its loose mode indefinitely, and
    the least-written documents are not the least sensitive ones.

    LOGS NAMES ONLY. Never opens a file, never reads a byte, never logs a path
    outside the app-data directory. Returns a summary of counts and basenames
    for :func:`/system/status`; the caller must not add contents to it.

    Never raises — a failed sweep degrades to "the modes were not tightened",
    which is exactly the state we were already in, and must not stop boot.
    """
    summary = {
        "directory_secured": False,
        "tightened": [],
        "already_secure": [],
        "failed": [],
    }
    if not MODES_ENFORCEABLE:
        summary["skipped"] = "modes not enforceable on this platform"
        return summary
    try:
        ensure_secure_dir(directory)
        try:
            summary["directory_secured"] = not bool(
                stat.S_IMODE(os.stat(directory).st_mode) & _LOOSE_BITS)
        except OSError:
            pass

        for name in sorted(os.listdir(directory)):
            if not name.endswith(_SWEEP_SUFFIXES):
                continue
            full = os.path.join(directory, name)
            if not os.path.isfile(full):
                continue
            if not _is_loose(full):
                summary["already_secure"].append(name)
                continue
            if _chmod_quiet(full, APP_DATA_FILE_MODE) and not _is_loose(full):
                summary["tightened"].append(name)
            else:
                summary["failed"].append(name)
    except OSError as exc:
        logger.warning("app-data mode sweep could not run: %s", exc)
        return summary

    if summary["tightened"]:
        logger.warning(
            "app-data mode sweep tightened %d file(s) to 0600: %s",
            len(summary["tightened"]), ", ".join(summary["tightened"]))
    if summary["failed"]:
        logger.error(
            "app-data mode sweep COULD NOT tighten %d file(s); they remain "
            "group/world-readable: %s",
            len(summary["failed"]), ", ".join(summary["failed"]))
    if not summary["tightened"] and not summary["failed"]:
        logger.info("app-data mode sweep: %d file(s) already owner-only",
                    len(summary["already_secure"]))
    return summary


def app_data_mode_status(directory: str) -> dict:
    """Counts-only view of the at-rest modes, for ``/system/status``.

    Basenames are deliberately NOT included: the status endpoint is reachable by
    every keyed caller on the LAN, and "which apps this household uses" is
    itself household-internal. Counts answer the only question the owner is
    actually asking — "is anything still loose?".
    """
    if not MODES_ENFORCEABLE:
        return {"status": "unsupported_platform", "files_owner_only": 0,
                "files_loose": 0, "target_mode": "0600"}
    secure = loose = 0
    try:
        for name in os.listdir(directory):
            if not name.endswith(_SWEEP_SUFFIXES):
                continue
            full = os.path.join(directory, name)
            if not os.path.isfile(full):
                continue
            if _is_loose(full):
                loose += 1
            else:
                secure += 1
    except OSError:
        return {"status": "unavailable", "files_owner_only": 0,
                "files_loose": 0}
    return {
        "status": "ok" if loose == 0 else "loose_modes",
        "files_owner_only": secure,
        "files_loose": loose,
        "target_mode": "0600",
    }


__all__ = [
    "APP_DATA_FILE_MODE",
    "APP_DATA_DIR_MODE",
    "MODES_ENFORCEABLE",
    "ensure_secure_dir",
    "write_json_secure",
    "sweep_app_data_modes",
    "app_data_mode_status",
]
