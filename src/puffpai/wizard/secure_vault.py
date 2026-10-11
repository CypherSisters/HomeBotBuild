"""The vault ceremony, without a print statement in it.

The Secure skill and the card contract, together, set a problem this module is
the answer to: the vault's recovery code is generated while the skill applies,
it has to reach the owner exactly once, and it may not appear on the card, in
the answers, in the run log, in the API response or in an exception. So it goes
through the **face carried on the run's context** (``RunContext.show_once``,
set by :meth:`puffpai.wizard.runner.Runner.show_once` for the run that is
drawing the wizard) -- and this module holds the *policy* (how many times a
code is shown, how many wrong attempts, what happens when the owner cannot
write it down right now) while the face holds the I/O.

The face is an argument, never a module global. A global would mean a process
that once drew a card on a terminal answers "yes, somebody can see this" to
every later request, including one that arrived over HTTP from a browser.

The policy is the one ``cli._recovery_ceremony`` already ran, constant for
constant, because a converted skill may change its wording and may not change
what it does.

Two properties worth stating plainly:

* **Nothing here returns, stores or logs the code.** It is a local, it is
  handed to the face, and the function returns a boolean.
* **No face, no vault.** :func:`can_show` is False when nobody has registered,
  and the caller must not create a vault it cannot show the way back into.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from typing import Any

#: What the owner is told when a run seals a vault with nobody reading the
#: screen. Today's wizard prints this above the code on a ``--yes`` run.
UNCONFIRMED_LINES = (
    "  NO ONE CONFIRMED THIS RECOVERY CODE.",
    "  This is a non-interactive run, so nobody read the code back.",
    "  It is printed once, here, and it is now in whatever log this",
    "  output went to. Copy it somewhere safe and treat that log as",
    "  sensitive.",
)

#: And when the ceremony ran and nobody typed the code back.
NOT_CONFIRMED_LINES = (
    "  NO RECOVERY CODE WAS CONFIRMED.",
    "  Your vault is sealed and works, and your PIN opens it. Nobody",
    "  wrote down a way back in, so if you forget the PIN the contents",
    "  are gone. That is recorded in your build manifest as unconfirmed.",
)

SKIP_LINES = (
    "  If you cannot write it down right now, type 'skip' and we will",
    "  throw this one away and make you a new one.",
)


def face_of(ctx: Any) -> Any:
    """The show-once face this run carries, or ``None``. Fail closed.

    :meth:`puffpai.wizard.runner.RunContext.face` is the real check; this
    wrapper exists so a caller holding something context-shaped (a test double)
    gets the same fail-closed answer rather than an attribute error.
    """
    face = getattr(ctx, "face", None)
    if callable(face):
        return face()
    sink = getattr(ctx, "show_once", None)
    if sink is None:
        return None
    if not all(callable(getattr(sink, name, None)) for name in ("show", "ask")):
        return None
    return sink


def can_show(ctx: Any) -> bool:
    """True when this run has a face that can put a value in front of somebody."""
    return face_of(ctx) is not None


def _code_lines(code: str) -> list[str]:
    from puffpai import setup_ops
    return [*(f"  {line}" for line in setup_ops.vault_recovery_notice()),
            "", f"      {code}", ""]


def show_and_confirm(code: str, pin: str, vault: Any, *, sink: Any,
                     non_interactive: bool) -> bool:
    """Show the code once, ask for it back, and say whether it came back.

    The ``code`` parameter is the only place this value exists in this process
    outside the vault itself. It is never returned and never recorded; the
    caller gets a boolean and writes *that* into the manifest, which is exactly
    what ``vault_manifest_block`` has always carried.
    """
    from puffpai import cli, setup_ops, zone_vault
    from puffpai.security.vault import VaultError

    if sink is None:
        # Unreachable through the callers in this build -- they check first --
        # and kept because "show a code with nowhere to show it" must fail
        # closed wherever it is called from.
        return False

    if non_interactive:
        sink.show([*UNCONFIRMED_LINES, *_code_lines(code)])
        return False

    shows = 0
    mismatches = 0
    while shows < cli._RECOVERY_MAX_SHOWS:
        shows += 1
        sink.show([*_code_lines(code), *SKIP_LINES])

        while mismatches < cli._RECOVERY_MAX_MISMATCHES:
            typed = (sink.ask(f"  {zone_vault.RECOVERY_CONFIRM_PROMPT}: ")
                     or "").strip()
            if typed.lower() in cli._RECOVERY_ESCAPES or not typed:
                break
            if setup_ops.confirm_vault_recovery(code, typed):
                sink.show(["  That matches. Your vault is sealed and you have "
                           "a way back in."])
                return True
            mismatches += 1
            sink.show([f"  {zone_vault.RECOVERY_CONFIRM_FAILED}"])

        if mismatches >= cli._RECOVERY_MAX_MISMATCHES:
            break
        if shows >= cli._RECOVERY_MAX_SHOWS:
            break
        try:
            code = setup_ops.rotate_vault_recovery(code, pin, vault=vault)
        except VaultError as exc:
            sink.show([f"  Could not issue a new code: {exc}"])
            break
        sink.show(["  That code is now dead. Here is a fresh one."])

    sink.show(list(NOT_CONFIRMED_LINES))
    return False


__all__ = ["NOT_CONFIRMED_LINES", "SKIP_LINES", "UNCONFIRMED_LINES",
           "can_show", "face_of", "show_and_confirm"]
