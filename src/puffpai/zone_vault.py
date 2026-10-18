"""Zone 1, the Vault - the step where the owner sets their own lock.

Step 4 of the wizard. The vault is one of the three zones the pitch is built on,
and the pitch is only true if the owner sets it themselves. So this step is
explicit: they choose a PIN, we generate a recovery code, we show it once, and
they type it back before the wizard moves on. Typing it back is the whole point;
a checkbox does not make anybody write anything down.

What this module is careful about
---------------------------------
* **No second crypto path.** Sealing goes through
  :func:`puffpai.setup_ops.create_vault`, the single adapter over this build's
  :class:`puffpai.security.vault.Vault` (PBKDF2-derived KEK wrapping a random
  AES-256-GCM data key, with the recovery code wrapping the same data key a
  second time). Nothing here derives a key, wraps a key, hashes a PIN, or
  invents a format.
* **Secrets flow through, never into.** The PIN is handed to the vault and goes
  out of scope. The recovery code is returned to the caller for one screen. This
  module writes neither to disk, to a log, or to the manifest, and
  :func:`vault_manifest_block` does not even accept them as arguments - it
  structurally cannot record what it is never given.
* **Randomness comes from :mod:`secrets`.** Never :mod:`random`.

How the recovery code is actually redeemed
------------------------------------------
``RECOVERY_ESCROW_AVAILABLE`` is **True**: :mod:`puffpai.security.vault` stores
the vault's data key wrapped twice, once under the PIN and once under this code,
with independent salts. Either secret unwraps the same data key, and neither
wrapped copy reveals the other secret. Redeeming the code is a *recovery, not a
login* - :meth:`puffpai.security.vault.Vault.redeem_recovery_code` requires a new
PIN, re-keys the contents, and issues a fresh code, because a code that has been
read off paper should be assumed spent.

:data:`RECOVERY_UNWIRED_WARNING` is kept for the case where that mechanism is
ever absent again. :func:`recovery_notice_lines` adds it automatically whenever
the flag is False, which is why callers should use that function rather than
hand-rolling the screen.

All strings here are ASCII: they reach a Windows console.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# PIN policy
# ---------------------------------------------------------------------------
#
# The PIN is the only secret protecting the vault, and the vault files sit on a
# disk somebody could walk off with, so the realistic attack is offline guessing
# against PBKDF2. The iteration count buys time; the PIN has to buy the rest.
# Four digits does not, which is why the floor here is deliberately above the
# 4-16 character range api_server accepts when *recognising* a typed PIN. That
# range is a recogniser for an existing PIN; this is the policy for setting a
# new one, and setting policy is where the strictness belongs.

# The wizard's floor must never be LOOSER than the vault's own floor (this
# build's `Vault.initialize` refuses under four characters), or the prompt would
# accept a PIN the seal then rejects - the owner picks something, is told it is
# fine, and hits an error a screen later. The wizard may be stricter, never
# laxer; tests/test_wizard_homebot.py pins the direction.
_MIN_PIN_LENGTH = 8           # mixed characters; twice the vault's own floor
_MIN_NUMERIC_PIN_LENGTH = 10  # digits only: a smaller alphabet needs more length
_MAX_PIN_LENGTH = 128         # bound the input at the trust boundary

# Well-known choices. Most short ones are already excluded by the length floors;
# these catch the longer classics that would otherwise sail through.
_COMMON_PINS = frozenset({
    "000000", "00000000", "111111", "11111111", "123456", "1234567", "12345678",
    "123456789", "654321", "87654321", "112233", "121212", "123123", "123321",
    "696969", "abc123", "abcd1234", "password", "passw0rd", "letmein", "qwerty",
    "qwertyui", "iloveyou", "trustno1", "welcome", "monkey", "dragon", "sunshine",
    "puffpai", "buildbot", "homebot",
})


class VaultRecoveryUnavailable(RuntimeError):
    """The vault sealed but no recovery code came back.

    Raised rather than returning an empty string, so a caller can never run the
    "write this code down" ceremony over nothing. Only reachable if this build's
    vault loses its recovery-code primitive.
    """


def _is_all_same(text: str) -> bool:
    """True when every character is identical (0000, aaaaaa)."""
    return len(set(text)) == 1


def _is_sequential_run(text: str) -> bool:
    """True when the whole string steps by exactly +1 or -1 (1234, 4321, abcd)."""
    if len(text) < 3:
        return False
    step = ord(text[1]) - ord(text[0])
    if step not in (1, -1):
        return False
    return all(ord(text[i + 1]) - ord(text[i]) == step for i in range(len(text) - 1))


def _is_repeated_block(text: str) -> bool:
    """True when the string is a short block repeated (121212, 123123, abcabc)."""
    length = len(text)
    for block in range(1, 4):
        if block < length and length % block == 0 and text == text[:block] * (length // block):
            return True
    return False


def validate_pin(pin: str) -> tuple[bool, str]:
    """Check a proposed vault PIN. Returns ``(ok, reason)``.

    ``reason`` is empty when the PIN is acceptable, and otherwise a plain,
    non-judgemental sentence saying what to change and why it matters. Nothing
    about the PIN is logged, stored, or echoed back inside the reason.
    """
    candidate = pin if isinstance(pin, str) else ""

    if not candidate:
        return False, (
            "Your vault needs a PIN. This is the one thing that stands between "
            "your private data and anyone who picks up this machine."
        )
    if any(ch.isspace() for ch in candidate):
        return False, (
            "Leave spaces out of the PIN. Spaces are easy to lose track of when "
            "you type it back later, so we do not allow them."
        )
    if any(not (32 < ord(ch) < 127) for ch in candidate):
        return False, (
            "Use characters you can type on any keyboard: letters, digits, or "
            "punctuation. Anything fancier can be impossible to enter on a "
            "different device."
        )
    if len(candidate) > _MAX_PIN_LENGTH:
        return False, (
            f"That is longer than {_MAX_PIN_LENGTH} characters. Anything past "
            f"that adds no real protection and is easy to mistype."
        )
    if candidate.isdigit() and len(candidate) < _MIN_NUMERIC_PIN_LENGTH:
        return False, (
            f"A digits-only PIN needs at least {_MIN_NUMERIC_PIN_LENGTH} digits. "
            f"There are only ten digits to choose from, so a short one can be "
            f"worked through quickly by a machine. Adding a letter lets you use "
            f"{_MIN_PIN_LENGTH}."
        )
    if len(candidate) < _MIN_PIN_LENGTH:
        return False, (
            f"That is {len(candidate)} characters. Use at least "
            f"{_MIN_PIN_LENGTH} characters so it is not quick to guess."
        )
    if _is_all_same(candidate):
        return False, (
            "That is the same character repeated. It is one of the first things "
            "anyone would try, so pick something with some variety in it."
        )
    if _is_sequential_run(candidate):
        return False, (
            "That runs straight up or down the keyboard. Guessing programs start "
            "with runs like that, so it would not hold for long."
        )
    if _is_repeated_block(candidate):
        return False, (
            "That is a short pattern repeated. It is shorter than it looks to "
            "anything trying to guess it, so mix it up a bit."
        )
    if candidate.lower() in _COMMON_PINS:
        return False, (
            "That one shows up near the top of every published list of common "
            "choices, which is the first place a guessing program looks. Almost "
            "anything else is a better pick."
        )
    return True, ""


# ---------------------------------------------------------------------------
# recovery code
# ---------------------------------------------------------------------------
#
# THE VAULT OWNS THIS FORMAT. Everything below describes, validates and renders
# the code that ``security/vault.py`` actually mints and actually redeems; none
# of it invents one.
#
# A second format here would be a trap. ``Vault.generate_recovery_code``
# produces six groups of four base32 characters and ``recover_with_code`` only
# ever accepts those; a wizard that described or printed any other shape would
# never be caught by a test, because the wizard's ceremony (show it, take it
# back, compare the two strings) is self-consistent whatever the code says. The
# cost of that is the worst kind: an owner who wrote down the real code from the
# real vault and typed it into a prompt that describes a format it is not - or
# worse, a prompt that showed them a decorative string that opens nothing.
#
# So: one source of truth, and it is the half that holds the key.
#
# The vault's alphabet is RFC 4648 base32 (A-Z and 2-7). It is not a
# hand-transcription alphabet - 0/O and 1/I are absent because they are not
# base32 digits rather than because anyone chose that, and S/5 and B/8
# confusions are still available - but it is what the KDF input is normalised
# to (``vault._normalize_recovery_code``), so it is the only alphabet a
# redeemable code can be written in.
_VAULT_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _vault_format_shape() -> tuple[str, int, int, int]:
    """``(alphabet, groups, group_length, code_length)`` of the real format.

    Measured from the vault itself, by formatting a fixed block of the size it
    uses, so a change to ``_RECOVERY_CODE_BYTES`` or to the grouping moves the
    wizard's copy with it instead of leaving it stale. Falls back to the values
    this build ships with if the vault module cannot be imported: the constants
    are used in printed copy and a missing import must not take the wizard down.
    """
    try:
        from puffpai.security import vault as _vault
        sample = _vault._format_recovery_code(
            b"\x00" * _vault._RECOVERY_CODE_BYTES)
    except Exception:                     # pragma: no cover - import guard only
        sample = "-".join(["AAAA"] * 6)
    groups = sample.split("-")
    return (_VAULT_BASE32_ALPHABET, len(groups), len(groups[0]),
            len(sample.replace("-", "")))


(RECOVERY_ALPHABET, RECOVERY_GROUPS, RECOVERY_GROUP_LENGTH,
 RECOVERY_CODE_LENGTH) = _vault_format_shape()

# The honest entropy number is the size of the random block the vault draws,
# NOT the length of its rendering: base32 is an encoding, so 24 characters of it
# carry the 120 bits of ``_RECOVERY_CODE_BYTES`` and not the 120.4 bits that
# 32**24 would suggest. (The two happen to be nearly equal here because base32
# is 5 bits per character and 15 bytes divide exactly; that coincidence is
# exactly why it is worth computing from the bytes rather than the string.)
#
# 120 bits is the right order against what it protects: the vault wraps an
# AES-256 data key, so the cipher is not the weak link, and a handwritten code
# meaningfully shorter than this would be.
def _recovery_entropy_bits() -> int:
    try:
        from puffpai.security import vault as _vault
        return int(_vault._RECOVERY_CODE_BYTES) * 8
    except Exception:                     # pragma: no cover - import guard only
        return RECOVERY_CODE_LENGTH * 5


RECOVERY_CODE_ENTROPY_BITS = _recovery_entropy_bits()

# True once the reference vault carries a second, recovery-wrapped copy of the
# data key. The wizard reads this to decide whether it is allowed to describe the
# code as a way back in. It is the gate that stops the ceremony ever shipping
# without the mechanism behind it.
RECOVERY_ESCROW_AVAILABLE = True


def generate_recovery_code() -> str:
    """Mint a one-time vault recovery code, in the vault's own format.

    Rendered as :data:`RECOVERY_GROUPS` groups of
    :data:`RECOVERY_GROUP_LENGTH` uppercase base32 characters
    (``XXXX-XXXX-XXXX-XXXX-XXXX-XXXX``), carrying
    :data:`RECOVERY_CODE_ENTROPY_BITS` bits drawn from :mod:`secrets`.

    The bytes and the rendering both come from ``security/vault.py``, so a code
    this function returns is a code that vault will accept. Note that the code
    minted here is NOT wrapped around any data key: it is the format, not an
    enrolment. A code that has to *open* a vault comes from
    :func:`seal_vault_with_recovery`, which asks the vault to mint and enrol one
    in the same call.

    The returned value is a secret. It is shown once and never persisted by this
    module; a caller that writes it anywhere has broken the contract this step
    makes with the owner.
    """
    from puffpai.security import vault as _vault

    return _vault._format_recovery_code(
        secrets.token_bytes(_vault._RECOVERY_CODE_BYTES))


def _normalize_recovery_code(text: str) -> str:
    """Reduce a code to comparable form: uppercase base32 characters only.

    Hyphens, spaces, line breaks and stray punctuation are dropped, because a
    person copying two dozen characters off a card should not be failed for
    where they put the dashes or whether they used capitals.

    Delegates to the vault's own normaliser, which is the function whose output
    is fed to the KDF. Comparing here under a *different* rule than the one the
    redemption path uses is how a confirmation step comes to bless a code that
    the vault will later refuse.
    """
    if not isinstance(text, str):
        return ""
    try:
        from puffpai.security import vault as _vault
        return _vault._normalize_recovery_code(text)
    except Exception:                     # pragma: no cover - import guard only
        return "".join(ch for ch in text.upper() if ch.isalnum())


def is_recovery_code(text) -> bool:
    """True when *text* has the shape of a code this vault could redeem.

    Shape only: length and alphabet after normalisation. It says nothing about
    whether the code opens anything -- only :meth:`Vault.recover_with_code` can
    answer that, and it is rate-limited for the reason that makes it the only
    honest answer. This exists so a prompt can reject a typo, or a code from a
    different appliance, before spending an attempt against the real thing.
    """
    normalized = _normalize_recovery_code(text)
    return (len(normalized) == RECOVERY_CODE_LENGTH
            and set(normalized) <= set(RECOVERY_ALPHABET))


def verify_recovery_confirmation(shown: str, typed: str) -> bool:
    """True when ``typed`` is the code we ``shown``, allowing for handwriting.

    Case, spacing and hyphenation are normalized away first; the comparison
    itself uses :func:`secrets.compare_digest` so it does not leak how much of
    the code was correct through timing. Fails closed: an empty or unusable
    value on either side is never a match.
    """
    left = _normalize_recovery_code(shown)
    right = _normalize_recovery_code(typed)
    if not left or not right:
        return False
    return secrets.compare_digest(left, right)


# ---------------------------------------------------------------------------
# wizard copy (ASCII - this reaches a Windows console)
# ---------------------------------------------------------------------------

RECOVERY_NOTICE_TITLE = "Write this code down before you go any further."

RECOVERY_NOTICE_LINES: tuple[str, ...] = (
    "This code is the only way back into your vault if you forget your PIN.",
    "We cannot recover it for you. It is not sent anywhere and no copy of it is kept.",
    "There is no reset email, because there is no us. Nobody is holding a spare.",
    "Write it on paper and keep the paper where you would keep a spare house key.",
    "You will be asked to type it back before you go on. That is on purpose.",
)

RECOVERY_SHOWN_ONCE_WARNING = (
    "This code is shown once. Once you leave this screen it is gone from the "
    "screen and from memory, and we cannot show it to you again."
)

RECOVERY_CONFIRM_PROMPT = (
    "Type the recovery code back in. Capitals and dashes do not matter."
)

RECOVERY_CONFIRM_FAILED = (
    "That does not match the code on the screen. Check what you wrote down and "
    "try it again - this is exactly the mistake it is better to catch now."
)

# Shown while RECOVERY_ESCROW_AVAILABLE is False. Says the true thing rather
# than the reassuring thing.
RECOVERY_UNWIRED_WARNING = (
    "Heads up: on this build the recovery code cannot be redeemed yet, because "
    "the vault seals under your PIN alone. Keep the code, but treat your PIN as "
    "the only way in for now, and do not lose it."
)

PIN_INTRO_LINES: tuple[str, ...] = (
    "Your vault holds the things you would not hand to a stranger.",
    "You set the PIN. It is not sent anywhere, and it is never stored as text.",
    "Even your bot has to ask before it can read what is inside.",
)


def recovery_notice_lines() -> list[str]:
    """The full 'write this down' screen, in order, ready to print.

    Assembles the notice, the shown-once warning, and - while
    :data:`RECOVERY_ESCROW_AVAILABLE` is False - the warning that says the code
    cannot be redeemed on this build yet. Callers should use this rather than
    picking constants individually, so the honest caveat cannot be dropped by
    accident.
    """
    lines = [RECOVERY_NOTICE_TITLE, ""]
    lines.extend(RECOVERY_NOTICE_LINES)
    lines.append(RECOVERY_SHOWN_ONCE_WARNING)
    if not RECOVERY_ESCROW_AVAILABLE:
        lines.append(RECOVERY_UNWIRED_WARNING)
    return lines


# ---------------------------------------------------------------------------
# manifest block
# ---------------------------------------------------------------------------

_VAULT_METHODS = frozenset({"pin", "passphrase"})

# Descriptive, non-secret facts about how the reference vault protects things.
# Stating the primitives makes a build auditable; none of this is derived from
# the PIN or the code, and no salt, hash, or ciphertext appears here.
# ADAPTED FOR HOMEBOTBUILD: BuildBot's vault wrapped with PBKDF2 + Fernet. This
# build derives the key-encryption-key with scrypt (RFC 7914, memory-hard) and
# wraps the random data key with AES-256-GCM; see
# ``security/vault.py::_derive_kek`` and ``_wrap_dek``. A manifest that named
# the other primitives would be an audit trail describing a different build.
_KEY_DERIVATION = "scrypt"
_CIPHER = "aes-256-gcm"


def vault_manifest_block(
    *,
    configured: bool = True,
    sealed: bool = True,
    method: str = "pin",
    recovery_issued: bool = False,
    recovery_confirmed: bool = False,
    configured_at: Optional[float] = None,
) -> dict[str, Any]:
    """Record that a vault exists and is sealed. Status flags only.

    This function takes **no PIN and no recovery code**, by design. It cannot
    write the PIN, the code, a hash of either, a salt, or anything derived from
    them, because it is never handed them in the first place. That is a stronger
    guarantee than a promise not to, and it is what the test asserts.

    ``configured_at`` defaults to now when the vault is configured, and is None
    when it is not. Never raises.
    """
    chosen_method = str(method).strip().lower()
    if chosen_method not in _VAULT_METHODS:
        chosen_method = "unspecified"

    if configured:
        when: Optional[float] = configured_at if configured_at is not None else time.time()
    else:
        when = None

    return {
        "configured": bool(configured),
        "sealed": bool(sealed and configured),
        "method": chosen_method,
        "recovery_issued": bool(recovery_issued),
        "recovery_confirmed": bool(recovery_confirmed),
        "recovery_escrow": "wrapped-key" if RECOVERY_ESCROW_AVAILABLE else "none",
        "configured_at": when,
        "key_derivation": _KEY_DERIVATION,
        "cipher": _CIPHER,
    }


# ---------------------------------------------------------------------------
# sealing (hand the PIN over, keep nothing)
# ---------------------------------------------------------------------------

def seal_vault(pin: str, *, vault: Any = None) -> dict[str, Any]:
    """Create the vault under ``pin`` and return the manifest block.

    Validates the PIN first and refuses a weak one (fail closed), then hands the
    PIN straight to :func:`puffpai.setup_ops.create_vault`, which owns the only
    crypto path in the build. The PIN is not copied, cached, logged, or included
    in the return value; it goes out of scope when this function returns.

    Raises :class:`ValueError` if the PIN does not pass :func:`validate_pin`, and
    lets :class:`puffpai.security.vault.VaultError` propagate if a vault already
    exists - refusing to overwrite an existing vault is the vault's own rule and
    this step does not soften it.
    """
    ok, reason = validate_pin(pin)
    if not ok:
        raise ValueError(reason)

    from puffpai.setup_ops import create_vault

    create_vault(pin, vault=vault)
    return vault_manifest_block(configured=True, sealed=True, method="pin")


def seal_vault_with_recovery(pin: str, *, vault: Any = None) -> tuple[dict[str, Any], str]:
    """Create the vault under ``pin`` and issue its recovery code.

    This is the wizard's path: it is what makes the "write this down" screen mean
    something. Returns ``(manifest_block, recovery_code)`` - the code is a
    **separate return value on purpose**, never a field inside the block, so that
    a caller which persists the manifest cannot persist the code by accident.

    Show the code once, have the owner type it back
    (:func:`verify_recovery_confirmation`), then drop it. Neither the PIN nor the
    code is retained here, and the vault is left sealed.

    Same failure modes as :func:`seal_vault`: :class:`ValueError` for a PIN that
    does not pass :func:`validate_pin`, and
    :class:`puffpai.security.vault.VaultError` if a vault already exists.
    """
    ok, reason = validate_pin(pin)
    if not ok:
        raise ValueError(reason)

    from puffpai.setup_ops import create_vault

    # The vault handle is resolved inside create_vault (from this install's data
    # dir) rather than by letting Vault() pick its own default. Without that, a
    # wizard run pointed at a scratch PUFFPAI_DATA_DIR would seal the DEFAULT
    # install's vault instead - in the worst case, a real owner's vault during a
    # test run. create_vault is also the ONE place that knows how this build's
    # vault spells "create, then mint a recovery code"; see its docstring.
    code = create_vault(pin, vault=vault, with_recovery=True)
    if not code:
        raise VaultRecoveryUnavailable(
            "the vault created without issuing a recovery code")
    block = vault_manifest_block(
        configured=True, sealed=True, method="pin", recovery_issued=True,
    )
    return block, code


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def vault_summary_lines(block: dict[str, Any]) -> list[str]:
    """Plain-language lines describing a vault block, for the CLI/API."""
    lines: list[str] = []
    if not block.get("configured"):
        lines.append("Vault: not set up yet. Nothing is protected until you set a PIN.")
        return lines

    state = "sealed" if block.get("sealed") else "unlocked"
    lines.append(f"Vault: set up and {state}, opened by your {block.get('method', 'pin')}.")
    if block.get("recovery_confirmed"):
        lines.append("  Recovery code: generated, and you typed it back to confirm it.")
    elif block.get("recovery_issued"):
        lines.append("  Recovery code: generated, but not confirmed. Worth redoing.")
    else:
        lines.append("  Recovery code: none issued.")
    if block.get("recovery_escrow") == "none":
        lines.append("  Note: this build cannot redeem a recovery code yet. Your PIN is the way in.")
    return lines


__all__ = [
    "RECOVERY_ALPHABET",
    "RECOVERY_CODE_ENTROPY_BITS",
    "RECOVERY_CODE_LENGTH",
    "RECOVERY_CONFIRM_FAILED",
    "RECOVERY_CONFIRM_PROMPT",
    "RECOVERY_ESCROW_AVAILABLE",
    "RECOVERY_GROUPS",
    "RECOVERY_GROUP_LENGTH",
    "RECOVERY_NOTICE_LINES",
    "RECOVERY_NOTICE_TITLE",
    "RECOVERY_SHOWN_ONCE_WARNING",
    "RECOVERY_UNWIRED_WARNING",
    "PIN_INTRO_LINES",
    "generate_recovery_code",
    "is_recovery_code",
    "recovery_notice_lines",
    "seal_vault",
    "seal_vault_with_recovery",
    "validate_pin",
    "vault_manifest_block",
    "vault_summary_lines",
    "verify_recovery_confirmation",
]
