"""First-run setup code — the thing that stops a LAN device finishing setup.

THE HOLE THIS CLOSES
--------------------
Before completion the wizard is, by design, permissive: ``_setup_gate`` in
``api_server`` refuses state-changing ``/setup/*`` calls only AFTER
``setup_ops.is_complete()``. That was sound while the only reachable caller was
on this host. It stopped being sound because:

  * ``dashboard`` served with ``next start -p 3003`` bound EVERY interface, so
    any device on the network could load it;
  * pairing enforcement defaults to ``"knowledge"``
    (``dashboard/app/_lib/server/pairing.ts``), which gates the knowledge
    surface and NOT ``/api/pai/setup/*``;
  * the dashboard's proxy attaches the backend API key server-side, so an
    unpaired LAN device inherits the appliance's own credential for those calls.

Net effect on a fresh install: the first device to reach ``http://<box>:3003``
— not necessarily the owner — could name the bot, write the Home Assistant
token, choose the household, and call ``POST /setup/vault``, whose response
carries the **vault recovery code**. Whoever answered first owned the vault.

THE CONTROL
-----------
A one-time code, minted by the backend on first start while setup is
incomplete, shown ONLY on the console of the machine running it (and reprinted
by ``puffpai setup-code``, which is also local). Every state-changing
``/setup/*`` request must carry it in ``X-Setup-Code`` — UNLESS the request's
effective client is loopback, which is what makes ``puffpai setup`` (no HTTP at
all) and a local ``curl`` unchanged.

It is a **proof of physical/console access**, not a password: possession of the
console is the thing being checked. Accordingly:

  * ``hmac.compare_digest`` for the comparison — no early-exit timing signal;
  * 5 wrong answers and the code is LOCKED OUT and **rotated**, so a guessing
    attacker is not merely slowed but invalidates the value it was guessing at;
    the owner reads the new one off the console (or ``puffpai setup-code``);
  * exponential backoff across rotations, so repeated campaigns get worse;
  * the code is DESTROYED at ``/setup/complete``. There is no window after
    setup in which it still means anything.

WHAT IS DELIBERATELY NOT DONE
-----------------------------
* **Never logged.** No ``logger`` call in this module receives the code, and
  the ``/setup/*`` responses never echo it. ``announce()`` writes it with a
  caller-supplied print function (the console), which is the only channel.
* **Not in the build manifest.** It is not configuration; nothing writes it
  into ``build_manifest.json``.
* **No "remind me" endpoint.** Reprinting it requires a local shell. An HTTP
  route that returned the code would recreate the hole.
* **Stored in cleartext, at 0600, under the state dir** — on purpose. A hash
  could not be reprinted for the owner, and the threat this defends against is
  a NETWORK peer, not someone who already reads the state directory (that
  person can read the API key in ``homebot.env`` next to it).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import time

logger = logging.getLogger(__name__)

#: Header the dashboard and any other off-box caller must send.
HEADER = "X-Setup-Code"

#: Alphabet without the characters people mis-read off a console: no 0/O,
#: 1/I/L, 5/S, 8/B. 32 symbols, two groups of four = 40 bits of entropy, which
#: is far beyond what 5 attempts and a rotation can be worn down by.
_ALPHABET = "234679ACDEFGHJKMNPQRTUVWXYZ"
_GROUP = 4
_GROUPS = 2

#: Wrong answers tolerated before lockout + rotation.
MAX_ATTEMPTS = 5

#: Lockout seconds: 60s after the first rotation, doubling, capped at an hour.
_BASE_LOCKOUT = 60
_MAX_LOCKOUT = 3600


def _state_path() -> str:
    """Where the code lives: ``<data dir>/data/setup_code.json``.

    Same directory as ``setup_state.json`` (``setup_ops._setup_flag_path``), so
    a scratch install with its own ``$PUFFPAI_DATA_DIR`` has its own code and
    cannot be driven with the box's.
    """
    from puffpai.config import get_config
    return os.path.join(get_config().DATA_DIR, "data", "setup_code.json")


def _read() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Create restricted, THEN write: a chmod after the write leaves a window in
    # which the file is world-readable.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    handle = os.open(path, flags, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    finally:
        try:
            os.chmod(path, 0o600)   # no-op on Windows; belt for a pre-existing file
        except OSError:
            pass


def _generate() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(_GROUP))
              for _ in range(_GROUPS)]
    return "-".join(groups)


def normalize(text) -> str:
    """Canonical form for comparison: upper-case, no spaces or dashes."""
    return "".join(ch for ch in str(text or "").upper()
                   if ch.isalnum())


def is_required() -> bool:
    """True when a code is needed at all: setup is not finished yet.

    Fail-closed: if the completion flag cannot be read, ``setup_ops.is_complete``
    already assumes finished, and a finished install needs no code (the
    post-completion gate takes over). The dangerous direction is claiming "no
    code needed" while setup is open, and that cannot happen here.
    """
    try:
        from puffpai import setup_ops
        return not setup_ops.is_complete()
    except Exception:
        return False


def current(*, mint: bool = True) -> str | None:
    """The active code, minting one if setup is open and none exists.

    Returns None when setup is already complete (nothing to gate) or when
    ``mint`` is False and no code has been minted.
    """
    if not is_required():
        return None
    data = _read()
    code = str(data.get("code") or "")
    if code:
        return code
    if not mint:
        return None
    code = _generate()
    _write({"code": code, "created": time.time(), "attempts": 0,
            "locked_until": 0, "rotations": 0})
    return code


def destroy() -> None:
    """Remove the code. Called when setup completes; idempotent."""
    try:
        os.remove(_state_path())
    except (FileNotFoundError, OSError):
        pass


def locked_for() -> int:
    """Seconds remaining on a lockout, 0 when not locked."""
    remaining = float(_read().get("locked_until") or 0) - time.time()
    return int(remaining) + 1 if remaining > 0 else 0


def verify(supplied) -> tuple[bool, str, int]:
    """Check *supplied* against the active code.

    Returns ``(ok, reason, retry_after)``. *reason* is a short machine code for
    the caller to turn into a message; it never contains the code, and neither
    does anything this function logs.
    """
    if not is_required():
        return True, "not_required", 0

    remaining = locked_for()
    if remaining:
        return False, "locked_out", remaining

    expected = current()
    if not expected:                       # unreadable/unwritable state dir
        return False, "unavailable", 0

    given = normalize(supplied)
    if not given:
        return False, "missing", 0

    if hmac.compare_digest(given, normalize(expected)):
        data = _read()
        if data.get("attempts"):
            data["attempts"] = 0
            _write(data)
        return True, "ok", 0

    data = _read()
    attempts = int(data.get("attempts") or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        rotations = int(data.get("rotations") or 0) + 1
        wait = min(_BASE_LOCKOUT * (2 ** (rotations - 1)), _MAX_LOCKOUT)
        _write({"code": _generate(), "created": time.time(), "attempts": 0,
                "locked_until": time.time() + wait, "rotations": rotations})
        logger.warning(
            "first-run setup code: %d wrong answers — code ROTATED and locked "
            "for %ds. Read the new one from the console of this machine or "
            "`puffpai setup-code`.", attempts, wait)
        return False, "locked_out", wait

    data["attempts"] = attempts
    _write(data)
    logger.warning("first-run setup code: wrong answer (%d of %d before "
                   "lockout)", attempts, MAX_ATTEMPTS)
    return False, "bad_code", 0


def announce(*, print_line=print) -> str | None:
    """Print the code on the console if setup is still open. Returns it.

    Called from the API entry points. When setup is already complete this
    destroys any stale file and prints nothing — the code must not outlive the
    wizard.
    """
    if not is_required():
        destroy()
        return None
    code = current()
    if not code:
        return None
    # ASCII ONLY. The first version of this banner used box-drawing characters
    # and died with UnicodeEncodeError on a stock Windows console (cp1252) —
    # which meant the ONE channel the code is published on printed nothing and
    # the owner could not run setup from another device at all. This block is
    # the whole publication mechanism; it must not depend on the code page.
    print_line("")
    print_line("  +-- FIRST-RUN SETUP CODE " + "-" * 28)
    print_line(f"  |   {code}")
    print_line("  |")
    print_line("  |   Setup is not finished. The browser wizard asks")
    print_line(f"  |   for this code. Header: {HEADER}.")
    print_line("  |   `puffpai setup` on this machine needs no code.")
    print_line("  |   Reprint it with: puffpai setup-code")
    print_line("  +" + "-" * 52)
    print_line("")
    return code


__all__ = ["HEADER", "MAX_ATTEMPTS", "announce", "current", "destroy",
           "is_required", "locked_for", "normalize", "verify"]
