"""
PuffPAI Vault -- Zone 1 Encrypted Storage

Provides session-based authentication and encrypted file I/O for
personally identifiable information (PII).

Development mode:  AES-256-GCM via the `cryptography` library (BSD license)
Production target: LUKS + TPM2.0 full-disk encryption

All vault access is logged through AuditLog.

The build engine reaches the vault through a few more doors, each built on
the same PIN, DEK and session model: named secrets and the key broker's
reserved records (:meth:`Vault.store`, :meth:`Vault._reserved_get`) in one
encrypted record, a lock event for Toolsmith's key lease
(:meth:`Vault.add_lock_listener`), and the remembered vault place
(:func:`remember_place`, :meth:`Vault.missing_reason`), so a vault on a drive
that is not plugged in is reported as missing and a new, empty one is never
made in its place.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def _doc_now_iso() -> str:
    """UTC ISO-8601 timestamp (seconds) for document-index 'added' fields."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.hazmat.primitives import hashes
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

try:
    import pyotp
    _HAS_TOTP = True
except ImportError:
    _HAS_TOTP = False

from ..logging_config import get_logger, log_unwritable_once
from .audit_log import AuditLog

logger = get_logger(__name__)

# NOTE: this file is src/puffpai/security/vault.py, so three parents up is
# <repo>/src, NOT the repo root. <repo>/src/vault_data is inside the
# read-only code tree on the appliance (`Vault unavailable: [Errno 30]
# Read-only file system: '/opt/puffpai/src/vault_data'`, every 60 s). The
# location comes from paths.vault_data_dir via resolve_state_dir();
# <repo>/src/vault_data is kept as the legacy fallback so a dev checkout with
# real vault files there does not silently start from an empty vault.
_SRC_DIR = Path(__file__).resolve().parent.parent.parent      # <repo>/src
_REPO_ROOT = _SRC_DIR.parent                                  # <repo>
_DEFAULT_VAULT_DIR = _REPO_ROOT / "vault_data"
_LEGACY_VAULT_DIR = _SRC_DIR / "vault_data"


def default_vault_dir() -> Path:
    """Resolve the vault directory from config, env, then the repo default.

    Resolved on each Vault construction (not at import) so config can pull in
    the secrets provider without an import cycle, and so tests can rebuild the
    config singleton. Any failure degrades to the repo-relative default.
    """
    try:
        from puffpai.config import get_config, resolve_state_dir
        cfg = get_config()
        return Path(resolve_state_dir(
            cfg.paths.vault_data_dir,
            repo_root=cfg.REPO_ROOT,
            default=str(_DEFAULT_VAULT_DIR),
            env_subdir="vault_data",
            legacy=str(_LEGACY_VAULT_DIR),
        ))
    except Exception as exc:  # config must never make the vault unbuildable
        logger.warning("config unavailable for vault path: %s", exc)
        return _DEFAULT_VAULT_DIR

# Files that migrate_plaintext_files() must NOT re-encrypt in place, because
# they are not encrypted under the PIN-derived DEK at all.
#: Vault records that hold BOOT SECRETS: encrypted under the Service Data Key
#: (SDK), not under the PIN-derived DEK. ``migrate_plaintext_files()`` must not
#: touch them (it would fail to decrypt them under the DEK and report them as
#: unclassifiable), and only these names may be read on the unattended path.
#: An allowlist, not a convention -- a caller cannot name its own file into the
#: service tier.
SERVICE_SECRET_FILES = frozenset({"ha_token.json"})

_MIGRATION_SKIP = SERVICE_SECRET_FILES

# ── Machine-bound service key ──────────────────────────────────────────────
#
# The problem it solves: the vault DEK is wrapped ONLY under the owner's PIN and
# their recovery code, so nothing on the appliance can decrypt anything without
# a human. That is right for the family's documents and PII. It is wrong for the
# Home Assistant token, which the appliance needs at 03:00 after a power cut
# with nobody awake (the rule: unlock/recovery must never lock the user out --
# an appliance whose lights stay dead until someone types a PIN has locked
# the household out of its own house).
#
# The answer is a THIRD wrap, but deliberately NOT a third wrap of the DEK. A DEK
# wrap under a machine key would mean that anyone who can read that machine key
# can decrypt EVERY Zone-1 record -- the documents, pii_refs.json, the lot. So
# instead there is a separate random Service Data Key (SDK) that encrypts only
# the SERVICE_SECRET_FILES allowlist, and the SDK is wrapped twice:
#
#   keyring["service"]        SDK wrapped under the machine-bound secret
#                             -> the unattended boot path
#   keyring["service_by_dek"] SDK wrapped under the DEK itself
#                             -> any PIN or recovery session can still read and
#                                rewrite the boot secrets (recovery)
#
# The DEK is NEVER wrapped under the machine secret. Someone who reads the
# machine secret gets the HA token and nothing else; the Zone-1 PIN gate is
# exactly as strong as it would be without this tier.
#
# Where the machine secret lives, in resolution order:
#   1. $CREDENTIALS_DIRECTORY/puffpai-vault-service-key -- systemd LoadCredential=,
#      a 0400 file in a per-service tmpfs. THIS is the intended production path:
#      the source file under /etc is root-only and the service never opens it.
#   2. $PUFFPAI_VAULT_SERVICE_KEY_FILE -- explicit override (tests, dev boxes).
#   3. /etc/puffpai/vault_service.key -- readable only when running as root,
#      i.e. by scripts/install_vault_service_key.sh itself.
_SERVICE_KEY_CRED_NAME = "puffpai-vault-service-key"
_SERVICE_KEY_ENV = "PUFFPAI_VAULT_SERVICE_KEY_FILE"
_SERVICE_KEY_DEFAULT_PATH = "/etc/puffpai/vault_service.key"
_SERVICE_WRAP_KEY = "service"
_SERVICE_DEK_WRAP_KEY = "service_by_dek"
_SDK_LENGTH = 32
_SDK_WRAP_AAD = b"puffpai-vault-sdk-v2"          # machine-secret wrap of the SDK
_SDK_DEK_WRAP_AAD = b"puffpai-vault-sdk-by-dek-v2"  # DEK wrap of the SDK
_service_key_warned: set = set()


def service_key_candidates() -> list:
    """The machine-secret file paths to try, in order. Never reads them."""
    out = []
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if cred_dir:
        out.append(("systemd-credential",
                    Path(cred_dir) / _SERVICE_KEY_CRED_NAME))
    override = os.environ.get(_SERVICE_KEY_ENV, "").strip()
    if override:
        out.append((_SERVICE_KEY_ENV, Path(override)))
    out.append(("etc", Path(_SERVICE_KEY_DEFAULT_PATH)))
    return out


def read_service_key(vault_dir=None):
    """Return ``(secret, source_label)`` for the machine-bound service key.

    *vault_dir* is accepted for callers that name a vault folder; the machine
    key belongs to the appliance, not to a folder, so it is not used.

    ``(None, reason)`` when no usable key is present. Never raises, never logs
    or returns the key material itself.

    A world-readable key file is REFUSED, not used: the whole claim of this tier
    is that the secret is unreadable by anything but root and the service, and
    honouring an 0644 key would make that claim a lie while looking like it
    worked. Refusing here does not take Home Assistant down -- resolution simply
    falls through to the next source (see security/ha_token_source.py).
    """
    reason = "no service key file found"
    for label, path in service_key_candidates():
        try:
            st = os.stat(path)
        except OSError:
            continue
        mode = stat.S_IMODE(st.st_mode)
        # POSIX only. On Windows os.stat() reports 0666/0444 from the
        # read-only attribute alone -- the bits carry no ACL information, so
        # enforcing them there would refuse every key while protecting nothing.
        # The appliance is Linux; this check is meaningful exactly where it
        # runs.
        if os.name != "nt" and mode & 0o077:  # ANY group or other access
            # Not just world-readable. A key at 0640 root:puffpai would let a
            # compromised service user read the raw /etc file directly, which
            # is precisely the property the systemd-credential path exists to
            # deny. The installer writes 0600 and systemd hands
            # the service a 0400 copy, so nothing legitimate trips this.
            reason = (f"service key at {label} is accessible beyond its owner "
                      f"(mode {mode:04o}); refused")
            if reason not in _service_key_warned:
                _service_key_warned.add(reason)
                logger.error(
                    "vault service key REFUSED: %s is accessible beyond its "
                    "owner (mode %04o). It must be 0600 root:root; systemd "
                    "hands the service a 0400 copy. Fix with: chmod 600 "
                    "<file>. Falling through to the next HA-token source.",
                    label, mode)
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                secret = f.read().strip()
        except OSError:
            reason = f"service key at {label} is unreadable"
            continue
        if len(secret) < 32:
            reason = f"service key at {label} is too short to be a machine key"
            continue
        return secret, label
    return None, reason


def _keyring_user(vault_dir: str) -> str:
    """The name a per-folder machine key would carry in an OS password store."""
    digest = hashlib.sha256(os.path.abspath(vault_dir).encode("utf-8"))
    return "vault-service-key:" + digest.hexdigest()[:12]


def _usable_keyring():
    """Always None: the machine key is never kept in an OS password store.

    It lives in a root-only file the service is handed by systemd (see
    :func:`service_key_candidates`), so a desktop keyring cannot widen what
    the unattended tier can open.
    """
    return None


def create_machine_key(vault_dir: str):
    """Refused: the machine key is installed by root, not made by the service.

    ``scripts/install_vault_service_key.sh`` writes it where only root can read
    it and systemd hands the service a copy; letting the running service mint
    one would let it widen its own unattended access.
    """
    raise VaultError("the machine key is installed by the system installer, "
                     "not made by the running service")


def forget_machine_key(vault_dir: str) -> None:
    """Nothing to forget: no machine key is bound to a vault folder."""
    return None

_SESSION_TIMEOUT_SECONDS = 900  # 15 minutes

# ── PIN floor (creation and change only) ────────────────────────────────────
#
# This used to be four characters, which is a 10,000-space for the digits-only
# PIN nearly everyone picks — and the lockout below throttles ONLINE guessing
# only. The vault files are at rest on a household machine, so an attacker who
# copies them off it attacks the KDF offline at whatever rate their hardware
# allows, where 480,000 PBKDF2 iterations buys far less than the four-digit
# space costs.
#
# The wizard (`puffpai.zone_vault.validate_pin`) already refused anything under
# eight characters — ten for digits-only. A vault whose own floor sat at four
# meant the policy held only as long as every caller went through the wizard;
# /vault/unlock's `{"initialize": true}` path did not. These are the same
# numbers, enforced where the PIN actually becomes a key.
#
# DELIBERATELY ONE-DIRECTIONAL: this is checked when a PIN is SET
# (`initialize`, `change_pin`, `recover_with_code`) and never when one is
# VERIFIED. An existing vault created under the old floor still unlocks with
# its old PIN — locking a household out of its own data to enforce a policy
# change would be the worse failure. `tests/test_wizard_homebot.py` pins the
# wizard's floor at or above this one so the two cannot cross.
MIN_PIN_LENGTH = 8
MIN_NUMERIC_PIN_LENGTH = 10

_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5-minute lockout
_KDF_ITERATIONS = 480_000  # OWASP recommendation
_SALT_LENGTH = 32
_KEY_LENGTH = 32  # AES-256
_NONCE_LENGTH = 12  # GCM standard


def _check_new_pin(pin) -> None:
    """Raise ``ValueError`` unless *pin* may be SET on a vault.

    The message never quotes the PIN back. Only the length floors live here:
    dictionary and pattern rules are the wizard's (``zone_vault.validate_pin``),
    which is stricter still, and duplicating them would put owner-facing copy in
    the crypto module.
    """
    if not isinstance(pin, str):
        raise ValueError(f"PIN must be a string of at least {MIN_PIN_LENGTH} characters")
    if pin.isdigit() and len(pin) < MIN_NUMERIC_PIN_LENGTH:
        raise ValueError(
            f"A digits-only PIN must be at least {MIN_NUMERIC_PIN_LENGTH} "
            f"digits (there are only ten digits to choose from). Adding a "
            f"letter lets you use {MIN_PIN_LENGTH}."
        )
    if len(pin) < MIN_PIN_LENGTH:
        raise ValueError(f"PIN must be at least {MIN_PIN_LENGTH} characters")


# ── idle lock, in plain words ──────────────────────────────────────────────
#
# A session that sits unused for _SESSION_TIMEOUT_SECONDS expires, wherever it
# was opened; these say so in the owner's words.
IDLE_LOCK_SECONDS = _SESSION_TIMEOUT_SECONDS


def idle_lock_minutes(seconds: float = IDLE_LOCK_SECONDS) -> int:
    """The idle time in whole minutes (at least 1), for plain words."""
    return max(1, int(round(float(seconds) / 60.0)))


def idle_lock_line(seconds: float = IDLE_LOCK_SECONDS) -> str:
    """The sentence a page shows about the vault locking itself."""
    minutes = idle_lock_minutes(seconds)
    unit = "minute" if minutes == 1 else "minutes"
    return (f"Your vault locks itself after {minutes} {unit} without use, "
            f"wherever it was opened: this page, a chat or voice. It locks "
            f"even when this page is closed.")


# ── where the vault should be ──────────────────────────────────────────────
_PLACE_FILENAME = "vault_place.json"
_PLACE_SCHEMA = "puffpai_vault_place/v1"
VAULT_DRIVE_MISSING = "Your vault drive is not plugged in."
#: What the place record's fingerprint is made from (see
#: :meth:`Vault.place_fingerprint`).
_PLACE_FP_CONTEXT = b"puffpai vault place fingerprint v1"

#: The encrypted record holding named secrets and the vault's own reserved
#: items (:meth:`Vault.store`, :meth:`Vault._reserved_get`).
_NAMED_SECRETS_FILE = "named_secrets.json"

# ── a new secret's floor, as a check others can call ───────────────────────
#
# The vault's own floor when a PIN is SET stays :func:`_check_new_pin` (length
# only); the patterns below are the wizard's (``zone_vault``) and
# :func:`validate_vault_secret`'s.
_MIN_SECRET_LENGTH = 8
_COMMON_SECRETS = frozenset({
    "password", "passw0rd", "password1", "12345678", "123456789", "1234567890",
    "87654321", "qwertyui", "iloveyou", "trustno1", "letmein1", "welcome1",
    "abcd1234", "11111111", "00000000", "aaaaaaaa", "changeme", "adminadmin",
    "1qaz2wsx", "1q2w3e4r", "1q2w3e4r5t", "zaq12wsx", "qazwsxedc",
    "qwertyuiop", "asdfghjkl", "zxcvbnm1",
})

# -- patterns a guessing program tries first --------------------------------
#
# Runs are read on rings (digits and letters wrap around: 7890123, xyzab) and
# along the keyboard's rows. "1234567890" passed the old check, because 9 -> 0
# is not a step of one; on the digit ring it is.
_RINGS = ("0123456789", "abcdefghijklmnopqrstuvwxyz")
_ROWS = ("1234567890-=", "qwertyuiop[]\\", "asdfghjkl;'", "zxcvbnm,./",
         "qwertyuiopasdfghjklzxcvbnm")


def _steps(a: str, b: str) -> set:
    """``(sequence, step)`` pairs under which *b* follows *a* by one."""
    found = set()
    for ring in _RINGS:
        i, j = ring.find(a), ring.find(b)
        if i >= 0 and j >= 0:
            if (i + 1) % len(ring) == j:
                found.add((ring, 1))
            if (i - 1) % len(ring) == j:
                found.add((ring, -1))
    for row in _ROWS:
        i, j = row.find(a), row.find(b)
        if i >= 0 and j >= 0 and abs(i - j) == 1:
            found.add((row, j - i))
    return found


def _run_segments(text: str) -> list[int]:
    """Lengths of the runs *text* splits into, greedily from the left."""
    out: list[int] = []
    i = 0
    while i < len(text):
        best = 1
        if i + 1 < len(text):
            for kind in _steps(text[i], text[i + 1]):
                j = i + 1
                while j + 1 < len(text) and kind in _steps(text[j], text[j + 1]):
                    j += 1
                best = max(best, j - i + 1)
        out.append(best)
        i += best
    return out


# Phone keypad and number-pad columns, top to bottom and bottom to top
# (1470258369 and 2580147369 are three column walks).
_PADS = ("147", "2580", "369", "741", "8520", "963")

# Keypad shapes as a class: the two diagonals (the same digits on
# both pads) and two loops, the diamond through the middle of each side (2, 6,
# 8, 4) and the four corners (1, 3, 9, 7), either way round from any key.
# With paths of neighbouring keys (:data:`_KING_RUN`) they count only when the
# WHOLE secret is cut into such pieces (:func:`_keypad_shape`): the X and the
# diamond drawn in another order (9517538426) are a shape; a random PIN with
# one of them inside it is not.
_KEYPAD_LINES = ("159", "357")
_LOOPS = ("2684", "1397")

# Shortest run that counts, by kind. A step of one (on a ring, along a row or
# down a keypad column) counts from three characters. Any other fixed step
# (24680, 97531, acegi, and 7777 as a step of nothing) counts from four: three
# characters counting by some step turn up by chance in one random place in
# ten, which would refuse honest random PINs (about 5% of random
# ten-digit PINs, mostly as "a pattern with a character or two added").
_UNIT_RUN = 3
_STEPPED_RUN = 4


def _cover_steps(a: str, b: str) -> set:
    """Every way *b* can follow *a* inside a run.

    :func:`_steps` (one up or down a ring, one along a row) and one along a
    keypad column, plus ``("step", ring, k)`` for any other fixed step on a
    ring, step 0 included. Only the ``"step"`` kinds need :data:`_STEPPED_RUN`
    characters.
    """
    found = set(_steps(a, b))
    for r, ring in enumerate(_RINGS):
        i, j = ring.find(a), ring.find(b)
        if i >= 0 and j >= 0:
            k = (j - i) % len(ring)
            if k not in (1, len(ring) - 1):
                found.add(("step", r, k))
    for pad in _PADS:
        i, j = pad.find(a), pad.find(b)
        if i >= 0 and j >= 0 and abs(i - j) == 1:
            found.add((pad, j - i))
    return found


def _keypad_steps(a: str, b: str) -> set:
    """The keypad kinds of :func:`_keypad_shape`: a diagonal, a loop (four
    long or more) or a path of neighbouring keys (:data:`_KING_RUN` long)."""
    found = set()
    for line in _KEYPAD_LINES:
        i, j = line.find(a), line.find(b)
        if i >= 0 and j >= 0 and abs(i - j) == 1:
            found.add((line, j - i))
    for loop in _LOOPS:
        i, j = loop.find(a), loop.find(b)
        if i >= 0 and j >= 0 and (j - i) % 4 in (1, 3):
            found.add(("loop", loop, (j - i) % 4))
    return found | _king_steps(a, b)


#: A path on a keypad: each move to a key right next to the last one, across,
#: up, down or corner to corner, turning any way. Seven keys or
#: more count as a run when the whole secret is cut into runs with nothing
#: left over; a path of the whole secret is :func:`_keypad_path`. Seven, not
#: six: the same shapes are caught and fewer random PINs are (measured
#: on 20,000 random PINs: ten digits 1.31% refused in all, twelve digits
#: 0.18%).
_KING_RUN = 7


def _king_steps(a: str, b: str) -> set:
    """``("king", pad)`` for every keypad on which *b* is right next to *a*."""
    found = set()
    for n, pad in enumerate(_KEYPADS):
        if a in pad and b in pad:
            (r1, c1), (r2, c2) = pad[a], pad[b]
            if (r1, c1) != (r2, c2) and max(abs(r1 - r2), abs(c1 - c2)) == 1:
                found.add(("king", n))
    return found


def _cover_and_king(a: str, b: str, king: bool) -> set:
    """:func:`_cover_steps`, plus the keypad kinds (:func:`_keypad_steps`)
    when *king*: only ever for a whole secret, never a piece of one."""
    found = _cover_steps(a, b)
    return found | _keypad_steps(a, b) if king else found


def _keypad_shape(text: str) -> bool:
    """Eight characters or more drawing keypad shapes from end to end: a
    path of neighbouring keys (:func:`_keypad_path`), or cut with nothing
    left over into runs, diagonals, loops and paths (9517538426: the X and
    the diamond, in another order)."""
    if len(text) < 8:
        return False
    return _keypad_path(text) or _run_cover(text, king=True)[0] == 0


def _keypad_path(text: str) -> bool:
    """Eight digits or more, every move to a key right next to the last one
    on one keypad (any turns): a path through neighbouring keys. About 0.2%
    of random ten-digit PINs are one (measured)."""
    if len(text) < 8 or not text.isdigit():
        return False
    kinds = None
    for a, b in zip(text, text[1:]):
        here = {k for k in _king_steps(a, b)}
        kinds = here if kinds is None else kinds & here
        if not kinds:
            return False
    return True


def _run_cover(text: str, king: bool = False) -> tuple[int, int]:
    """How few characters are left over when *text* is cut into runs.

    A run is characters each following the last by the same kind of step
    (:func:`_cover_steps`), at least :data:`_UNIT_RUN` long for a step of one
    and :data:`_STEPPED_RUN` long for any other step. Returns
    ``(left, left_with_long)``: the fewest characters not in any run, and the
    fewest when at least one run is one character longer than its shortest (a
    large number when there is no such cut). Exact, not greedy, so a run
    cannot hide by lending its first character to the run before it.
    """
    n = len(text)
    never = n + 1000
    left = [never] * (n + 1)
    left_long = [never] * (n + 1)
    left[0] = 0
    for i in range(n):
        if left[i] >= never:
            continue
        left[i + 1] = min(left[i + 1], left[i] + 1)
        left_long[i + 1] = min(left_long[i + 1], left_long[i] + 1)
        if i + 1 >= n:
            continue
        for kind in _cover_and_king(text[i], text[i + 1], king):
            shortest = (_KING_RUN if kind[0] == "king" else _STEPPED_RUN
                        if kind[0] in ("step", "loop") else _UNIT_RUN)
            j = i + 1
            while j + 1 < n and kind in _cover_and_king(text[j], text[j + 1], king):
                j += 1
                end = j + 1  # text[i:end] is a run of length end - i
                if end - i < shortest:
                    continue
                left[end] = min(left[end], left[i])
                left_long[end] = min(left_long[end], left_long[i])
                # A path of neighbouring keys is not "a run longer than its
                # shortest" (too many random PINs hold one with two over).
                if end - i > shortest and kind[0] != "king":
                    left_long[end] = min(left_long[end], left[i])
    return left[n], left_long[n]


# Where each digit sits on a phone keypad (123 on top, 0 under 8) and on a
# computer's number pad (789 on top, the wide 0 under 2 and under 1).
_KEYPADS = (
    {"1": (0, 0), "2": (0, 1), "3": (0, 2), "4": (1, 0), "5": (1, 1),
     "6": (1, 2), "7": (2, 0), "8": (2, 1), "9": (2, 2), "0": (3, 1)},
    {"7": (0, 0), "8": (0, 1), "9": (0, 2), "4": (1, 0), "5": (1, 1),
     "6": (1, 2), "1": (2, 0), "2": (2, 1), "3": (2, 2), "0": (3, 1)},
    {"7": (0, 0), "8": (0, 1), "9": (0, 2), "4": (1, 0), "5": (1, 1),
     "6": (1, 2), "1": (2, 0), "2": (2, 1), "3": (2, 2), "0": (3, 0)},
)


def _keypad_strokes(text: str, pad: dict) -> int:
    """How many straight strokes draw *text* on *pad*, or 0 when none do.

    Each move goes to a key in the same row, column or diagonal, at most two
    keys away (5 to 0 on a phone passes over 8). A stroke is moves in one
    direction; the corner key ends one stroke and starts the next.
    """
    directions = []
    for a, b in zip(text, text[1:]):
        (r1, c1), (r2, c2) = pad[a], pad[b]
        dr, dc = r2 - r1, c2 - c1
        if (dr, dc) == (0, 0) or max(abs(dr), abs(dc)) > 2:
            return 0
        if dr and dc and abs(dr) != abs(dc):
            return 0
        directions.append(((dr > 0) - (dr < 0), (dc > 0) - (dc < 0)))
    return 1 + sum(1 for x, y in zip(directions, directions[1:]) if x != y)


def _keypad_walk(text: str, part: bool = False) -> bool:
    """True when the digits draw a shape on a phone keypad or number pad.

    Snakes, spirals, the square around the edge, an X (1478963250,
    1236987410, 1593574862): every move to a key in line with the last, in
    at most ``len - 3`` strokes (so at least two moves carry straight on)
    and never more than seven. Six digits or more. About 0.2% of random
    ten-digit PINs draw such a shape and almost no longer ones (tested),
    against about 4% of ten-digit PINs if any number of turns were allowed.

    *part* (a PIN's middle, once a character or two is taken off): eight
    digits or more and one stroke fewer, so a shape with a character added
    (1478963250x) is still seen while random PINs almost never are (about
    0.03% more of them).
    """
    shortest, slack, most = (8, 4, 6) if part else (6, 3, 7)
    if len(text) < shortest or not text.isdigit():
        return False
    limit = min(len(text) - slack, most)
    for pad in _KEYPADS:
        strokes = _keypad_strokes(text, pad)
        if strokes and strokes <= limit:
            return True
    return False


#: The patterns the vault's own floor refuses (every path to a new secret
#: comes through it): one run, runs back to back, digits counting by a fixed
#: step, or a shape drawn on the keypad.
FLOOR_PATTERNS = frozenset({"run", "runs", "digit_step", "keypad"})


def weak_pattern(secret: str, strict: bool = False) -> str:
    """The pattern that makes *secret* guessable, or '' when there is none.

    ``run``: one run up or down a ring or a keyboard row (1234567890,
    0987654321, 7890123456, xyzabcde, qwertyuiop). ``runs``: the whole
    secret cut into runs, three or more long counting by one on a ring,
    along a keyboard row or down a keypad column, or four or more long
    counting by any other fixed step (abcd1234, 1234554321, 123abc456,
    123456123456123456, qwertqwertqwert, 1234512345123, 2468013579,
    8642097531, 1470258369, 2580147369, 7418529630, acegi13579), or with at
    most two characters left over when one run is longer than its shortest
    (abcdefgh1, 6210401234). ``repeat``: a block of one to three characters
    said again and again, a part-block at the end included (abcabcabc,
    121212, abcabcab). ``digit_step``: digits counting by any fixed step,
    wrapping (1357913579, 9630741852). ``keypad``: digits drawing a shape on
    a phone keypad or number pad (:func:`_keypad_walk`). Case does not
    matter. A passphrase with spaces is never one of these.

    *strict* is for a part of a PIN (the middle once a character or two is
    taken off, or every other character): there the whole part must be the
    pattern, with no characters left over and a keypad shape held to fewer
    turns, because a few random characters around something that is only
    nearly a pattern make no pattern at all.

    The vault's own floor refuses :data:`FLOOR_PATTERNS`; the wizard's PIN
    rule refuses every kind.
    """
    text = str(secret or "").lower()
    if len(text) < 3:
        return ""
    length = len(text)
    for block in range(1, min(3, length // 2) + 1):
        if all(text[k] == text[k % block] for k in range(length)):
            return "repeat"
    if text.isdigit() and length >= 6:
        step = (int(text[1]) - int(text[0])) % 10
        if all((int(text[k + 1]) - int(text[k])) % 10 == step
               for k in range(length - 1)):
            return "digit_step"
    segments = _run_segments(text)
    if len(segments) == 1:
        return "run"
    left, left_long = _run_cover(text)
    if left == 0 or (not strict and left_long <= 2):
        return "runs"
    if _keypad_walk(text, part=strict):
        return "keypad"
    if not strict:
        if _keypad_shape(text) or _keypad_with_marks(text):
            return "keypad"
        block = _said_twice_block(text)
        if block:
            kind = weak_pattern(block)
            if kind in FLOOR_PATTERNS:
                return kind
    return ""


def _keypad_with_marks(text: str) -> bool:
    """A keypad shape with one or two other characters put in anywhere.

    Without this, 1478963k250, 14789k63250,
    123698k7410 and 15935k74862 make real vaults. The digits are read on
    their own when there are at least six of them and at most two other
    characters, by the same shape rule as a PIN of digits only (so a random
    PIN is caught about as rarely as a random PIN of digits).
    """
    digits = "".join(ch for ch in text if ch.isdigit())
    others = len(text) - len(digits)
    if not 0 < others <= 2 or len(digits) < 6:
        return False
    # Any keypad shape, drawn in any order (9517538426q)
    return _keypad_walk(digits) or _keypad_shape(digits)


def _said_twice_block(text: str) -> str:
    """The block of four or more that *text* says again and again, or ''."""
    n = len(text)
    for block in range(4, n // 2 + 1):
        if all(text[k] == text[k % block] for k in range(n)):
            return text[:block]
    return ""


#: Secret names with this prefix belong to the vault itself (the write-only
#: inbox key, see :mod:`puffpai.security.vault_inbox`). No public method reads,
#: writes, lists or deletes them, so no API route can hand one out.
_RESERVED_PREFIX = "__puffpai."


def _is_reserved(name: object) -> bool:
    return str(name).startswith(_RESERVED_PREFIX)


#: Sign-up key records (:mod:`puffpai.security.keys`).
#: Reserved like the inbox key, so every public method and every generic vault
#: route refuses or hides them; on top of that ONLY the key broker may read,
#: write or delete one (:func:`_require_key_broker`).
_KEY_PREFIX = _RESERVED_PREFIX + "key."
_KEY_BROKER_MODULE = "puffpai.security.keys"


def _is_key_name(name: object) -> bool:
    return str(name).startswith(_KEY_PREFIX)


def _require_key_broker(depth: int = 2) -> None:
    """Refuse a key-record call from anywhere but the key broker.

    A guard against accident and against a plugin or model-driven call site,
    not a sandbox: code already running in this process can patch anything.
    """
    caller = sys._getframe(depth).f_globals.get("__name__", "")
    if caller != _KEY_BROKER_MODULE:
        raise VaultError("sign-up key records belong to the key broker")



def validate_vault_secret(secret: str) -> tuple[bool, str]:
    """A floor for an unlocking secret. Returns ``(ok, reason)``.

    Weaker than :func:`puffpai.zone_vault.validate_pin`, the policy the wizard
    applies, and stricter than :func:`_check_new_pin`: it also refuses one
    character repeated, a common password and the patterns in
    :data:`FLOOR_PATTERNS`, while still allowing a spaced passphrase.
    """
    if not isinstance(secret, str) or not secret:
        return False, "a vault password is required"
    if len(secret) < _MIN_SECRET_LENGTH:
        return False, (
            f"a vault password must be at least {_MIN_SECRET_LENGTH} characters; "
            f"a short numeric PIN can be worked through offline"
        )
    if len(set(secret)) == 1:
        return False, "a vault password cannot be one character repeated"
    if secret.lower() in _COMMON_SECRETS:
        return False, "that password is on every published list of common passwords"
    if len(secret) >= 3:
        step = ord(secret[1]) - ord(secret[0])
        if step in (1, -1) and all(
            ord(secret[i + 1]) - ord(secret[i]) == step for i in range(len(secret) - 1)
        ):
            return False, "a vault password cannot be a straight run of characters"
    if weak_pattern(secret) in FLOOR_PATTERNS:
        return False, ("a vault password cannot count up or down, by ones or "
                       "by bigger steps, run along the keyboard or keypad, "
                       "even when it wraps around, or draw a shape on the "
                       "keypad")
    return True, ""

# ── Document-display allowlist + taxonomy (Zone-1 vault doc viewer) ──────────
# The ONLY MIME types the ephemeral vault viewer will store or serve. Validated
# at BOTH store and serve. image/svg+xml is deliberately EXCLUDED — SVG can carry
# active content (scripts) and must never be rendered from the vault.
VAULT_DOC_MIME_ALLOWLIST = frozenset({
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "text/plain",
})

# MIME → filename extension for the stored blob name (doc_<id>.<ext>).
_VAULT_DOC_MIME_EXT = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "text/plain": "txt",
}

# Sensitivity tiers for the canonical doc_type enum (privacy taxonomy). STRICT
# and HEALTH documents get the shorter 45s on-screen TTL; the rest get 90s.
# These are the canonical doc_type values stored in the encrypted index; the
# spoken-phrase → canonical mapping lives in dashboard_display (used by both the
# tagger and the matcher so they can never drift).
STRICT_DOC_TYPES = frozenset({
    "ssn", "passport", "drivers_license", "state_id", "bank_account",
    "other_vault_doc",
})
HEALTH_DOC_TYPES = frozenset({
    "medical_record", "prescription", "immunization_record", "insurance_card",
})
FINANCIAL_DOC_TYPES = frozenset({
    "tax_return", "bank_statement",
})

#: The encrypted, Zone-1-only document index (doc_id -> metadata). Never read by
#: Zone 2; only decryptable post-PIN with a live session key.
VAULT_DOC_INDEX = "vault_documents.json"
# ── Wrapped-key layer (recovery gate) ─────────────────────────────────────
# The vault historically derived its AES key DIRECTLY from the PIN, so a
# forgotten PIN = permanently unreadable data. That is unacceptable once
# Scrub-and-Vault makes the vault the only copy of scrubbed secrets. The
# recovery gate introduces a random Data Encryption Key (DEK) that actually
# encrypts vault contents; the DEK is then WRAPPED (encrypted) separately by a
# PIN-derived key-encryption-key (KEK) and by a recovery-code-derived KEK. Either
# credential can unwrap the DEK, so losing the PIN is recoverable with the code.
# The DEK itself is never written to disk in the clear, and neither the PIN nor
# the recovery code is ever stored (only their KDF salts + the ciphertext wraps).
_KEYRING_FILE = ".vault_keyring.json"
_KEYRING_VERSION = 2
_DEK_LENGTH = 32                        # AES-256 data key
_DEK_WRAP_AAD = b"puffpai-vault-dek-v2"  # AES-GCM additional-authenticated-data
# scrypt work factors (interactive-login class; RFC 7914 sizing). n must be a
# power of two. These bind each wrap; the params are also recorded in the wrap so
# a future change stays backward-readable.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_RECOVERY_CODE_BYTES = 15              # 120 bits of entropy -> 24 base32 chars


# ── unlock events ─────────────────────────────────────────────────
#
# Listeners run after a FULL unlock (every enrolled factor passed), each in its
# own daemon thread so an unlock never waits on them. They receive only the new
# session id; no PIN, no key. Used to finish HA changes the owner approved
# while the vault was locked (puffpai.ha_pending_apply).

_UNLOCK_LISTENERS: list = []
_UNLOCK_TL = threading.local()
#: Who unlocked: an opaque requester id (the API's paired-device or session
#: key) captured on the UNLOCKING thread, before listeners go to their own
#: threads, so a listener can tell which device the unlock came from. Never a
#: PIN or key. Bounded; None when the unlock had no identifiable requester.
_unlock_requester_provider = None
_UNLOCK_REQUESTERS: dict = {}
_UNLOCK_REQUESTERS_MAX = 64


def add_unlock_listener(fn) -> None:
    if fn not in _UNLOCK_LISTENERS:
        _UNLOCK_LISTENERS.append(fn)


def set_unlock_requester_provider(fn) -> None:
    global _unlock_requester_provider
    _unlock_requester_provider = fn


def unlock_requester(session_id):
    """The requester id recorded when *session_id* fired its unlock, or None."""
    return _UNLOCK_REQUESTERS.get(session_id)


def _fire_unlock(session_id: str) -> None:
    who = None
    if _unlock_requester_provider is not None:
        try:
            who = _unlock_requester_provider()
        except Exception:
            who = None
    _UNLOCK_REQUESTERS[session_id] = who if isinstance(who, str) and who else None
    while len(_UNLOCK_REQUESTERS) > _UNLOCK_REQUESTERS_MAX:
        _UNLOCK_REQUESTERS.pop(next(iter(_UNLOCK_REQUESTERS)), None)
    for fn in list(_UNLOCK_LISTENERS):
        def _run(f=fn):
            try:
                f(session_id)
            except Exception:
                pass
        threading.Thread(target=_run, name="vault-unlock-listener",
                         daemon=True).start()


class VaultError(Exception):
    """Raised when vault operations fail."""
    pass


class AuthenticationError(VaultError):
    """Raised when authentication fails."""
    pass


class VaultLocked(VaultError):
    """Raised when an operation needs an unlocked vault but none is open."""


class VaultThrottled(VaultError):
    """Raised when too many unlock attempts have failed too recently.

    Carries :attr:`retry_after`, the whole seconds remaining, so a caller can
    tell "your PIN is wrong" from "you are going too fast".
    """

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0, int(round(retry_after)))
        super().__init__(
            f"too many failed attempts; try again in {self.retry_after} seconds"
        )


class WeakSecretError(VaultError):
    """Raised when a proposed PIN is below the floor or a caller's policy."""


class VaultMissing(VaultError):
    """The vault should be in a folder (or on a drive) that is not there.

    ``str(exc)`` is written to be shown to a person as it is. Nothing is
    created while this stands: a second, empty vault is never made in the
    missing one's place.
    """


# -- Keyring read-modify-write lock -----------------------------------------
#
# Every keyring mutation is a read-modify-write: load the JSON, add or drop one
# wrap, save it. _atomic_write makes each SAVE atomic, but on its own nothing
# stops two writers from both loading the same version and the second one
# saving a copy that never had the first one's change in it. Last writer wins
# WHOLESALE.
#
# The PIN and recovery wraps are triggered by a person, one at a time, but the
# service tier adds four more writers (enroll_service_wrap,
# remove_service_wrap, _sdk_from_session(create=True), write_service_secret)
# that the owner exercises while puffpai-api is running. Racing
# enroll_service_wrap against generate_recovery_code without the lock loses an
# entry nearly every time, most often the RECOVERY wrap, with
# generate_recovery_code still returning the code to the owner as if it were
# enrolled. A silently-dropped recovery wrap is a lockout with a delay fuse on
# it.
#
# So: one lock file per vault directory, held across the whole load-modify-save
# span. flock on POSIX, msvcrt on Windows, plus a process-wide RLock so threads
# in one process serialize even where the OS lock is per-process. Reentrant --
# enroll_service_wrap calls _sdk_from_session, which takes it too.
#
# Deliberately NOT taken on read-only paths. authenticate() on a v2 vault does
# not write the keyring, so the boot path never touches this lock and a stuck
# lock file cannot keep the appliance from starting.
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

#: How long to wait for another writer before refusing. A write that cannot get
#: the lock FAILS -- it does not proceed unlocked. Dropping someone else's
#: recovery wrap is worse than an error the caller can retry.
_KEYRING_LOCK_TIMEOUT = 10.0
_KEYRING_LOCK_POLL = 0.05
_keyring_locks_guard = threading.Lock()
_keyring_locks: dict = {}
_keyring_lock_unavailable_warned = False


class _KeyringLock:
    """Reentrant, cross-process exclusion for one vault's keyring file."""

    def __init__(self, path):
        self._path = Path(path)
        self._rlock = threading.RLock()
        self._depth = 0
        self._fh = None

    def _acquire_os_lock(self) -> None:
        global _keyring_lock_unavailable_warned
        if _fcntl is None and _msvcrt is None:
            if not _keyring_lock_unavailable_warned:
                _keyring_lock_unavailable_warned = True
                logger.warning(
                    "no OS file-locking primitive available; keyring writes are "
                    "serialized within this process only")
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self._path, "a+b")
        except OSError as exc:
            raise VaultError(
                "cannot open the vault keyring lock file") from exc
        deadline = time.monotonic() + _KEYRING_LOCK_TIMEOUT
        while True:
            try:
                if _fcntl is not None:
                    _fcntl.flock(fh.fileno(),
                                 _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                else:
                    fh.seek(0)
                    _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
                self._fh = fh
                return
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise VaultError(
                        "another vault operation is holding the keyring lock; "
                        "retry in a moment")
                time.sleep(_KEYRING_LOCK_POLL)

    def _release_os_lock(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                fh.seek(0)
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass

    def __enter__(self):
        self._rlock.acquire()
        self._depth += 1
        if self._depth == 1:
            try:
                self._acquire_os_lock()
            except BaseException:
                self._depth -= 1
                self._rlock.release()
                raise
        return self

    def __exit__(self, *exc_info):
        self._depth -= 1
        if self._depth == 0:
            self._release_os_lock()
        self._rlock.release()
        return False


def _keyring_lock_for(keyring_file) -> _KeyringLock:
    """The shared lock for one vault directory. Keyed by path so two Vault
    instances over the same directory share it in-process too."""
    key = str(Path(keyring_file).resolve().parent)
    with _keyring_locks_guard:
        lock = _keyring_locks.get(key)
        if lock is None:
            lock = _KeyringLock(Path(keyring_file).with_name(
                _KEYRING_FILE + ".lock"))
            _keyring_locks[key] = lock
        return lock


def _format_recovery_code(raw: bytes) -> str:
    """Render recovery-code entropy as human-transcribable groups.

    base32 (Crockford-ish A-Z2-7, no 0/1/8/9 ambiguity from RFC4648) grouped in
    fours with dashes, e.g. ``ABCD-EFGH-IJKL-MNOP-QRST-UVWX``. Display-only; the
    KDF input is the normalized form (see :func:`_normalize_recovery_code`).
    """
    import base64
    s = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def _normalize_recovery_code(code: str) -> str:
    """Canonicalize a user-entered recovery code for KDF input.

    Uppercases and strips everything but [A-Z2-7] so dashes, spaces, and case
    typed by the user do not change the derived key. Applied identically at
    generation and verification so the same code always derives the same KEK.
    """
    if not isinstance(code, str):
        return ""
    return "".join(ch for ch in code.upper() if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


class VaultSession:
    """Tracks an authenticated vault session."""

    def __init__(self, session_id: str, created_at: float):
        self.session_id = session_id
        self.created_at = created_at
        self.last_access = created_at
        # In-memory only: the PBKDF2/PIN-derived AES-256 key for this session.
        # Set at authenticate() so post-unlock requests (which carry only a
        # session id, not the PIN) can encrypt/decrypt. Never written to disk;
        # dropped when the session is logged out or expires.
        self.key: bytes | None = None

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_access) > _SESSION_TIMEOUT_SECONDS

    def touch(self):
        self.last_access = time.time()


class _UnlockState:
    """``vault.is_unlocked``, read both ways.

    Called with a session id it answers for that session
    (``vault.is_unlocked(sid)``). Read as a truth value (``if
    vault.is_unlocked:``) it answers whether any live session holds a key, the
    same as :attr:`Vault.has_live_session`. A plain method here would read as
    always true, which would make a locked vault look open.
    """

    __slots__ = ("_vault",)

    def __init__(self, vault) -> None:
        self._vault = vault

    def __call__(self, session_id: str) -> bool:
        return self._vault._session_unlocked(session_id)

    def __bool__(self) -> bool:
        return bool(self._vault.has_live_session)

    def __repr__(self) -> str:
        return f"<vault unlocked={bool(self)}>"


class _UnlockStateDescriptor:
    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return _UnlockState(obj)


_named_locks_guard = threading.Lock()
_named_locks: dict = {}


def _named_lock_for(vault_dir) -> _KeyringLock:
    """One lock per vault folder for the named-secrets record's
    read-modify-write, shared in-process and across processes."""
    key = str(Path(vault_dir).resolve())
    with _named_locks_guard:
        lock = _named_locks.get(key)
        if lock is None:
            lock = _KeyringLock(Path(vault_dir) / ".named_secrets.lock")
            _named_locks[key] = lock
        return lock


class Vault:
    """Zone 1 encrypted storage with session-based authentication.

    Usage:
        vault = Vault()
        vault.initialize("my-secure-pin")

        session = vault.authenticate("my-secure-pin")
        data = vault.read("profiles.json", session.session_id)
        vault.write("profiles.json", data, session.session_id)
    """

    def __init__(self, audit_log=None, vault_dir=None):
        self._audit = audit_log or AuditLog()
        self._vault_dir = Path(vault_dir) if vault_dir else default_vault_dir()
        self._key_file = self._vault_dir / ".vault_key_salt"
        self._sessions = {}
        self._failed_attempts = 0
        self._lockout_until = 0.0
        # Recovery code is a credential in its own right, so it gets its OWN
        # rate-limit/lockout counters (separate from the PIN's) — brute-forcing
        # the code must not be masked by, or reset, the PIN attempt budget.
        self._recovery_failed_attempts = 0
        self._recovery_lockout_until = 0.0
        self._pin_hash = None
        self._salt = None
        # Pluggable second-factor verifiers, keyed by factor name. PIN is NOT
        # in here — it is the mandatory first factor verified by authenticate()
        # (rate-limited, fail-closed). Additional factors are OPTIONAL layers on
        # top of the PIN and are consulted by authenticate_mfa() only when
        # required_factors() lists them. Seeded with the existing TOTP factor;
        # a future factor (e.g. voice MFA) registers a verifier via
        # register_auth_factor("voice", fn) and is added to required_factors().
        self._factor_verifiers = {"totp": self.verify_totp}
        #: Called with a reason ("lock", "idle") when the last live session
        #: ends. A listener's error never stops a lock.
        self._lock_listeners: list = []
        #: Only the install's own vault (no folder given) is remembered and
        #: checked for a missing drive.
        self._tracked = vault_dir is None

        try:
            self._vault_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Fail closed: without a writable directory the vault cannot store
            # anything, so construction fails and callers degrade. Report the
            # cause once — this path is retried on a timer by the API server.
            log_unwritable_once(logger, str(self._vault_dir), "vault", exc)
            raise VaultError(
                f"vault directory {self._vault_dir} is not writable: {exc}"
            ) from exc
        if self._key_file.exists():
            self._load_key_material()

    def _load_key_material(self):
        try:
            with open(self._key_file, 'rb') as f:
                data = f.read()
            if len(data) >= _SALT_LENGTH + 32:
                self._salt = data[:_SALT_LENGTH]
                self._pin_hash = data[_SALT_LENGTH:_SALT_LENGTH + 32]
        except (IOError, OSError):
            pass

    def _save_key_material(self):
        if self._salt is None or self._pin_hash is None:
            raise VaultError("No key material to save")
        with open(self._key_file, 'wb') as f:
            f.write(self._salt + self._pin_hash)

    def _derive_key(self, pin: str, salt: bytes) -> bytes:
        """Derive an AES-256 key from a PIN using PBKDF2."""
        if not _HAS_CRYPTO:
            raise VaultError(
                "cryptography library required. Install: pip install cryptography"
            )
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_LENGTH,
            salt=salt,
            iterations=_KDF_ITERATIONS,
        )
        return kdf.derive(pin.encode('utf-8'))

    def _hash_pin(self, pin: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            'sha256', pin.encode('utf-8'), salt + b'auth_verify', _KDF_ITERATIONS
        )

    # ── Wrapped-key layer: keyring, KEK derivation, DEK wrap/unwrap ────────

    @property
    def _keyring_file(self) -> Path:
        return self._vault_dir / _KEYRING_FILE

    @property
    def _keyring_lock(self):
        """Hold this across any keyring LOAD-MODIFY-SAVE span.
        Read-only paths deliberately do not take it, so a stuck lock file can
        never keep the appliance from unlocking or booting."""
        return _keyring_lock_for(self._keyring_file)

    @property
    def has_keyring(self) -> bool:
        """True if the wrapped-key keyring exists (i.e. the vault is on the v2
        model). A False on an initialized vault means it is still a legacy
        PIN-derived vault that will migrate on next unlock."""
        return self._keyring_file.exists()

    def _load_keyring(self) -> dict | None:
        """Return the parsed keyring dict, or None if absent/unreadable.

        The keyring holds ONLY public KDF salts, nonces, and ciphertext wraps of
        the DEK — never the DEK, PIN, or recovery code. A parse failure returns
        None (treated as "no keyring") rather than raising, so callers fail
        closed toward re-derivation/migration instead of crashing.
        """
        try:
            with open(self._keyring_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _save_keyring(self, keyring: dict) -> None:
        """Atomically persist the keyring. Written as a dotfile so every vault
        directory listing (which skips '.'-prefixed control files) ignores it."""
        self._atomic_write(
            self._keyring_file,
            json.dumps(keyring, indent=2).encode('utf-8'),
        )

    def _derive_kek(self, secret: str, salt: bytes, wrap: dict | None = None) -> bytes:
        """Derive a key-encryption-key from a PIN or recovery code.

        Uses scrypt (memory-hard, RFC 7914) by default. When re-deriving to
        UNWRAP, the KDF name/params are read back from the stored ``wrap`` so a
        wrap written under different params (or a PBKDF2 fallback) still opens.
        Fail-closed: no crypto backend -> raise, never a weaker path.
        """
        if not _HAS_CRYPTO:
            raise VaultError(
                "cryptography library required. Install: pip install cryptography"
            )
        kdf_name = (wrap or {}).get("kdf", "scrypt")
        if kdf_name == "scrypt":
            n = (wrap or {}).get("n", _SCRYPT_N)
            r = (wrap or {}).get("r", _SCRYPT_R)
            p = (wrap or {}).get("p", _SCRYPT_P)
            kdf = Scrypt(salt=salt, length=_KEY_LENGTH, n=n, r=r, p=p)
            return kdf.derive(secret.encode('utf-8'))
        if kdf_name == "pbkdf2":
            iterations = (wrap or {}).get("iterations", _KDF_ITERATIONS)
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(), length=_KEY_LENGTH,
                salt=salt, iterations=iterations,
            )
            return kdf.derive(secret.encode('utf-8'))
        raise VaultError(f"unsupported keyring KDF: {kdf_name!r}")

    def _wrap_dek(self, dek: bytes, secret: str) -> dict:
        """Encrypt (wrap) the DEK under a KEK derived from ``secret``.

        Returns a JSON-serializable wrap holding only the KDF salt, GCM nonce,
        and ciphertext — enough to re-derive the KEK from the credential and
        decrypt, and nothing that reveals the DEK or the credential.
        """
        salt = secrets.token_bytes(_SALT_LENGTH)
        wrap = {"kdf": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
                "salt": salt.hex()}
        kek = self._derive_kek(secret, salt, wrap)
        nonce = secrets.token_bytes(_NONCE_LENGTH)
        ct = AESGCM(kek).encrypt(nonce, dek, _DEK_WRAP_AAD)
        wrap["nonce"] = nonce.hex()
        wrap["ct"] = ct.hex()
        return wrap

    def _unwrap_dek(self, wrap: dict, secret: str) -> bytes:
        """Recover the DEK from a wrap using ``secret``.

        Raises (cryptography ``InvalidTag`` under the hood) if the credential is
        wrong or the wrap is tampered — GCM authentication IS the credential
        check, so no separate verifier/hash of the PIN or recovery code is
        stored. Callers convert the raise into a fail-closed auth error.
        """
        if not isinstance(wrap, dict):
            raise VaultError("invalid keyring wrap")
        salt = bytes.fromhex(wrap["salt"])
        nonce = bytes.fromhex(wrap["nonce"])
        ct = bytes.fromhex(wrap["ct"])
        kek = self._derive_kek(secret, salt, wrap)
        return AESGCM(kek).decrypt(nonce, ct, _DEK_WRAP_AAD)

    def _dek_from_pin(self, pin: str) -> bytes:
        """Return the DEK for a PIN: unwrap from the keyring on the v2 model,
        or fall back to the legacy PIN-derived key when no keyring exists yet.
        Used by the PIN-bearing read_secure/write_secure helpers."""
        keyring = self._load_keyring()
        if keyring and isinstance(keyring.get("pin"), dict):
            return self._unwrap_dek(keyring["pin"], pin)
        return self._derive_key(pin, self._salt)

    def _resolve_dek_on_unlock(self, pin: str) -> bytes:
        """Resolve the session DEK after the PIN has been verified.

        v2 vault (keyring present): unwrap the DEK with the PIN. A keyring that
        exists but will not unwrap despite a matching PIN hash is corrupt/tampered
        -> fail closed (never silently re-key and orphan the data).

        Legacy vault (no keyring): the existing data is encrypted under the
        PIN-derived key, so that key BECOMES the DEK (zero re-encryption, zero
        data loss). We then persist a keyring wrapping this DEK under the PIN, so
        the vault is on the v2 model from now on — a transparent, lossless
        migration that writes no DEK/PIN plaintext.
        """
        keyring = self._load_keyring()
        if keyring and isinstance(keyring.get("pin"), dict):
            try:
                return self._unwrap_dek(keyring["pin"], pin)
            except Exception as exc:
                raise VaultError(
                    "vault keyring is unreadable; cannot unlock"
                ) from exc

        # Legacy migration: adopt the PIN-derived key as the DEK and wrap it.
        # Build ON TOP of whatever is already there rather than replacing the
        # file: a keyring that exists but has no "pin" wrap would otherwise
        # lose its service wraps here, orphaning the boot secrets silently.
        # Under the lock, and re-read inside it, so a concurrent writer's
        # entry cannot be clobbered by a version we loaded before they saved.
        dek = self._derive_key(pin, self._salt)
        try:
            with self._keyring_lock:
                new_keyring = dict(self._load_keyring() or keyring or {})
                new_keyring["version"] = _KEYRING_VERSION
                new_keyring.setdefault("created", time.time())
                new_keyring["migrated_from"] = "pin-derived-v1"
                new_keyring["pin"] = self._wrap_dek(dek, pin)
                self._save_keyring(new_keyring)
            self._audit.log(action="vault_keyring_migrate", user="system",
                            result="success",
                            details="legacy PIN-derived vault -> wrapped-key v2")
        except Exception:
            # Could not persist the keyring (e.g. transient FS error). Still
            # unlock with the derived DEK so there is no data loss / lockout;
            # migration retries on the next unlock. Never log the key.
            self._audit.log(action="vault_keyring_migrate", user="system",
                            result="deferred",
                            details="keyring save failed; will retry next unlock")
        return dek

    # -- Service-key layer: the unattended boot tier -----------------------
    #
    # See the module-level note above _SERVICE_KEY_CRED_NAME for the threat
    # model. In short: a SEPARATE random Service Data Key encrypts only the
    # SERVICE_SECRET_FILES allowlist. It is wrapped under the machine-bound
    # secret (unattended reads) and under the DEK (so the owner's PIN or
    # recovery code always reaches it). The DEK itself is never wrapped under
    # anything a machine can supply.

    def _wrap_key_under_secret(self, material: bytes, secret: str,
                               aad: bytes) -> dict:
        """scrypt-KEK wrap of raw key material under a text secret."""
        salt = secrets.token_bytes(_SALT_LENGTH)
        wrap = {"kdf": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
                "salt": salt.hex()}
        kek = self._derive_kek(secret, salt, wrap)
        nonce = secrets.token_bytes(_NONCE_LENGTH)
        wrap["nonce"] = nonce.hex()
        wrap["ct"] = AESGCM(kek).encrypt(nonce, material, aad).hex()
        return wrap

    def _unwrap_key_under_secret(self, wrap: dict, secret: str,
                                 aad: bytes) -> bytes:
        if not isinstance(wrap, dict):
            raise VaultError("invalid keyring wrap")
        kek = self._derive_kek(secret, bytes.fromhex(wrap["salt"]), wrap)
        return AESGCM(kek).decrypt(bytes.fromhex(wrap["nonce"]),
                                   bytes.fromhex(wrap["ct"]), aad)

    def _wrap_key_under_key(self, material: bytes, kek: bytes,
                            aad: bytes) -> dict:
        """AES-GCM wrap under an existing 32-byte key. No KDF: the DEK is
        already uniformly random, so stretching it would only burn CPU."""
        nonce = secrets.token_bytes(_NONCE_LENGTH)
        return {"kdf": "raw", "nonce": nonce.hex(),
                "ct": AESGCM(kek).encrypt(nonce, material, aad).hex()}

    def _unwrap_key_under_key(self, wrap: dict, kek: bytes,
                              aad: bytes) -> bytes:
        if not isinstance(wrap, dict):
            raise VaultError("invalid keyring wrap")
        return AESGCM(kek).decrypt(bytes.fromhex(wrap["nonce"]),
                                   bytes.fromhex(wrap["ct"]), aad)

    @property
    def has_service_wrap(self) -> bool:
        """True if the keyring carries a machine-bound wrap of the SDK, i.e.
        boot secrets can be read with no human present."""
        keyring = self._load_keyring()
        return bool(keyring
                    and isinstance(keyring.get(_SERVICE_WRAP_KEY), dict))

    def _sdk_from_session(self, session_id: str, *, create: bool) -> bytes:
        """Recover (or mint) the Service Data Key using an unlocked session.

        ``create=True`` mints and persists an SDK when none exists yet; the
        caller must be a PIN/recovery session. ``create=False`` raises when
        there is no SDK, so readers never invent a key and silently orphan the
        records written under the real one.
        """
        dek = self._session_key(session_id)  # raises if locked/expired
        # The whole load-modify-save span is under the lock. The read-only
        # answer costs nothing extra: the lock is released straight after.
        with self._keyring_lock:
            keyring = self._load_keyring()
            if not keyring or not isinstance(keyring.get("pin"), dict):
                # Fail closed rather than mint a keyring here. A pin-less
                # keyring written by this tier would be rebuilt on the next
                # unlock, which would drop the service wraps and orphan the
                # boot secrets without anything failing loudly.
                raise VaultError(
                    "vault keyring is not ready for the boot-secret tier; "
                    "unlock the vault once with the PIN first")
            wrap = keyring.get(_SERVICE_DEK_WRAP_KEY)
            if isinstance(wrap, dict):
                try:
                    return self._unwrap_key_under_key(wrap, dek,
                                                      _SDK_DEK_WRAP_AAD)
                except Exception as exc:
                    raise VaultError(
                        "service key wrap is unreadable; cannot open boot "
                        "secrets") from exc
            if not create:
                raise VaultError("no service key enrolled in this vault")
            sdk = secrets.token_bytes(_SDK_LENGTH)
            keyring[_SERVICE_DEK_WRAP_KEY] = self._wrap_key_under_key(
                sdk, dek, _SDK_DEK_WRAP_AAD)
            self._save_keyring(keyring)
        self._audit.log(action="vault_service_key_create", user="system",
                        result="success",
                        details="minted a service data key (DEK-wrapped)")
        return sdk

    def _sdk_from_machine_key(self) -> bytes:
        """Recover the SDK with the machine-bound secret. No human, no PIN."""
        keyring = self._load_keyring()
        wrap = (keyring or {}).get(_SERVICE_WRAP_KEY)
        if not isinstance(wrap, dict):
            raise VaultError("no service wrap enrolled")
        secret, source = read_service_key()
        if secret is None:
            raise VaultError("machine service key unavailable: " + str(source))
        try:
            return self._unwrap_key_under_secret(wrap, secret, _SDK_WRAP_AAD)
        except Exception as exc:
            raise VaultError(
                "service wrap did not open with this machine key"
            ) from exc

    def enroll_service_wrap(self, session_id: str) -> bool:
        """Add the machine-bound wrap of the SDK. Requires an unlocked session.

        Idempotent: re-running re-wraps the SAME SDK under the current machine
        key, which is also how a rotated machine key is adopted without
        re-writing any boot secret. Returns False (never raises) when the vault
        is locked or no machine key is installed.
        """
        # Machine key first: a failed enrolment must not leave a freshly
        # minted (and unused) service-key wrap behind in the keyring.
        secret, source = read_service_key()
        if secret is None:
            logger.warning("cannot enroll vault service wrap: %s", source)
            return False
        try:
            # Reentrant: _sdk_from_session takes the same lock, and holding it
            # across BOTH steps means a concurrent generate_recovery_code
            # cannot land between the mint and the wrap.
            with self._keyring_lock:
                sdk = self._sdk_from_session(session_id, create=True)
                keyring = self._load_keyring()
                if not keyring:
                    return False
                wrap = self._wrap_key_under_secret(sdk, secret, _SDK_WRAP_AAD)
                wrap["created"] = time.time()
                keyring[_SERVICE_WRAP_KEY] = wrap
                self._save_keyring(keyring)
        except (VaultError, AuthenticationError):
            return False
        except Exception:
            self._audit.log(action="vault_service_wrap_enroll", user="system",
                            result="error", details="keyring save failed")
            return False
        self._audit.log(action="vault_service_wrap_enroll", user="system",
                        result="success",
                        details="machine key source: " + str(source))
        return True

    def remove_service_wrap(self, session_id: str) -> bool:
        """Strict mode: drop the machine-bound wrap so boot secrets need a PIN.

        Requires an unlocked session -- removing it is an availability decision
        (Home Assistant will be down until someone unlocks after a reboot), so
        it is not something an unauthenticated caller gets to do. The DEK wrap
        of the SDK is KEPT, so the owner can still read and rewrite the boot
        secrets, and can re-enroll later.
        """
        try:
            self._session_key(session_id)
        except (VaultError, AuthenticationError):
            return False
        try:
            with self._keyring_lock:
                keyring = self._load_keyring()
                if not keyring or _SERVICE_WRAP_KEY not in keyring:
                    return True  # already absent -- idempotent
                keyring.pop(_SERVICE_WRAP_KEY, None)
                self._save_keyring(keyring)
        except Exception:
            return False
        self._audit.log(action="vault_service_wrap_remove", user="system",
                        result="success",
                        details="strict mode: boot secrets are PIN-only")
        return True

    def _service_secret_path(self, filename: str) -> Path:
        safe_name = self._safe_vault_name(filename)
        if safe_name not in SERVICE_SECRET_FILES:
            raise VaultError("not a service-tier vault record: " + safe_name)
        return self._vault_dir / safe_name

    def _decrypt_service_record(self, filepath, sdk: bytes):
        if not filepath.exists():
            return None
        with open(filepath, "rb") as f:
            raw = f.read()
        try:
            return json.loads(self.decrypt_data(raw, sdk).decode("utf-8"))
        except Exception as exc:
            raise VaultError(
                "cannot decrypt service record: " + filepath.name) from exc

    def write_service_secret(self, filename: str, data, session_id: str) -> None:
        """Encrypt a boot secret under the SDK. Requires an unlocked session."""
        filepath = self._service_secret_path(filename)
        sdk = self._sdk_from_session(session_id, create=True)
        plaintext = json.dumps(data, default=str,
                               ensure_ascii=False).encode("utf-8")
        encrypted = self.encrypt_data(plaintext, sdk)
        try:
            self._atomic_write(filepath, encrypted)
        except Exception:
            self._audit.log(action="vault_service_secret_write", user="system",
                            result="error",
                            details="write failed: " + filepath.name)
            raise VaultError(
                "Failed to write service vault record: " + filepath.name)
        self._audit.log(action="vault_service_secret_write", user="system",
                        result="success",
                        details="wrote " + filepath.name)

    def read_service_secret(self, filename: str, session_id: str):
        """Read a boot secret using an unlocked PIN/recovery session."""
        filepath = self._service_secret_path(filename)
        sdk = self._sdk_from_session(session_id, create=False)
        return self._decrypt_service_record(filepath, sdk)

    def read_service_secret_unattended(self, filename: str):
        """Read a boot secret with the machine key alone -- no PIN, no session.

        This is the 03:00-reboot path. Returns None (never raises, never logs
        the value) when there is no service wrap, no machine key, no record, or
        the record does not authenticate. Reaches ONLY the
        SERVICE_SECRET_FILES allowlist: it cannot open a Zone-1 document,
        pii_refs.json, or anything else, because the SDK is not the DEK.
        """
        try:
            filepath = self._service_secret_path(filename)
            if not filepath.exists():
                # Cheap exit BEFORE the KDF. Between the root step and the
                # owner step the wrap exists but the record does not, and
                # /system/status is polled -- a scrypt per poll on a 50 W-capped
                # box would be a self-inflicted wound.
                return None
            sdk = self._sdk_from_machine_key()
            return self._decrypt_service_record(filepath, sdk)
        except (VaultError, AuthenticationError) as exc:
            logger.debug("unattended service-secret read unavailable: %s",
                         type(exc).__name__)
            return None
        except Exception:
            return None

    def service_tier_status(self) -> dict:
        """Content-free description of the boot-secret tier, for status pages."""
        keyring = self._load_keyring() or {}
        secret, source = read_service_key()
        have_key = secret is not None
        return {
            "service_wrap_present": isinstance(
                keyring.get(_SERVICE_WRAP_KEY), dict),
            "service_key_wrapped_by_pin": isinstance(
                keyring.get(_SERVICE_DEK_WRAP_KEY), dict),
            "machine_key_present": have_key,
            "machine_key_source": source if have_key else None,
            "machine_key_problem": None if have_key else source,
        }

    @property
    def is_initialized(self) -> bool:
        return self._pin_hash is not None and self._salt is not None

    @property
    def has_live_session(self) -> bool:
        """True if the vault is initialized AND at least one live session holds
        a key — i.e. secure reads/writes can succeed right now. Read-only guard
        for callers that must fail closed BEFORE mutating anything (e.g. the PII
        rescan/remediation path) rather than discover the locked state mid-write.

        NOTE: named ``has_live_session`` (not ``is_unlocked``) on purpose. The
        class also defines ``is_unlocked(session_id)`` (a method) further down;
        a property named ``is_unlocked`` here would be shadowed by that method at
        class-body evaluation time, so ``vault.is_unlocked`` resolved to the bound
        method (always truthy) and the fail-closed guard silently read True even
        when the vault was locked. Keep these two names distinct.
        """
        return self.is_initialized and self._first_live_session_id() is not None

    def initialize(self, pin: str):
        """Set up the vault with an initial PIN/password."""
        if self.is_initialized:
            raise VaultError("Vault already initialized. Use change_pin() instead.")
        self._refuse_if_missing()
        _check_new_pin(pin)

        self._salt = secrets.token_bytes(_SALT_LENGTH)
        self._pin_hash = self._hash_pin(pin, self._salt)
        self._save_key_material()

        # v2 wrapped-key model: encrypt vault contents under a FRESH random DEK
        # (not derived from the PIN), then wrap that DEK under a PIN-derived KEK.
        # This makes the data key independent of the PIN's entropy and is the
        # foundation the recovery code plugs into (a second wrap of the same DEK).
        # Guarded on the crypto backend; without it the vault stays on the legacy
        # path and encrypted operations fail explicitly (never plaintext).
        if _HAS_CRYPTO:
            dek = secrets.token_bytes(_DEK_LENGTH)
            keyring = {
                "version": _KEYRING_VERSION,
                "created": time.time(),
                "pin": self._wrap_dek(dek, pin),
            }
            self._save_keyring(keyring)

        self._audit.log(action="vault_initialize", user="system",
                        result="success", details="Vault initialized")
        self._adopt_place()

    def authenticate(self, pin: str, *, notify: bool = True) -> VaultSession:
        """Authenticate and create a session."""
        if not self.is_initialized:
            raise VaultError("Vault not initialized. Call initialize() first.")

        if time.time() < self._lockout_until:
            remaining = int(self._lockout_until - time.time())
            self._audit.log(action="vault_auth", user="unknown",
                            result="lockout", details=f"Locked for {remaining}s")
            raise AuthenticationError(
                f"Too many failed attempts. Try again in {remaining} seconds."
            )

        candidate_hash = self._hash_pin(pin, self._salt)
        if not hmac.compare_digest(candidate_hash, self._pin_hash):
            self._failed_attempts += 1
            self._audit.log(action="vault_auth", user="unknown", result="failed",
                            details=f"Attempt {self._failed_attempts}/{_MAX_FAILED_ATTEMPTS}")
            if self._failed_attempts >= _MAX_FAILED_ATTEMPTS:
                self._lockout_until = time.time() + _LOCKOUT_SECONDS
                self._failed_attempts = 0
            raise AuthenticationError("Invalid PIN")

        self._failed_attempts = 0
        session_id = secrets.token_hex(32)
        session = VaultSession(session_id=session_id, created_at=time.time())
        # Cache the DEK (data encryption key) in memory for the session lifetime
        # so encrypted read/write can be served from a session id alone (the PIN
        # is not retained). On the v2 model the DEK is unwrapped from the keyring
        # with the PIN; a legacy vault is transparently migrated here. Skipped if
        # the crypto backend is unavailable — the encrypted paths then fail
        # explicitly rather than falling back to plaintext.
        if _HAS_CRYPTO:
            session.key = self._resolve_dek_on_unlock(pin)
        self._sessions[session_id] = session
        self._audit.log(action="vault_auth", user="authenticated",
                        result="success", details=f"Session: {session_id[:8]}...")
        # A PIN-only session is a FULL unlock only when no other factor is
        # enrolled; authenticate_mfa fires its own event after every factor.
        if not getattr(_UNLOCK_TL, "in_mfa", False):
            try:
                extra = [f for f in self.required_factors() if f != "pin"]
            except Exception:
                extra = ["unknown"]
            if not extra:
                self._after_open()
                if notify:
                    _fire_unlock(session_id)
        return session

    def _after_open(self) -> None:
        """Work that waits for the vault to open. Never raises.

        Runs after every full unlock and recovery: the records of sign-up keys
        removed while the vault was locked are cleared, and a Toolsmith run
        waiting for the vault goes on.
        """
        try:
            from puffpai.security import keys as _keys
            _keys.on_vault_opened(self)
        except Exception:  # pragma: no cover - never block opening the vault
            pass
        try:
            from puffpai.security import toolsmith_cloud as _tc
            _tc.on_vault_opened(self)
        except Exception:  # pragma: no cover - never block opening the vault
            pass

    def _validate_session(self, session_id: str) -> VaultSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise AuthenticationError("Invalid session")
        if session.is_expired:
            del self._sessions[session_id]
            self._after_session_end("idle")
            raise AuthenticationError("Session expired -- please re-authenticate")
        session.touch()
        return session

    def encrypt_data(self, plaintext: bytes, key: bytes) -> bytes:
        """Encrypt data using AES-256-GCM. Returns nonce + ciphertext + tag."""
        if not _HAS_CRYPTO:
            raise VaultError("cryptography library required")
        nonce = secrets.token_bytes(_NONCE_LENGTH)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ciphertext

    def decrypt_data(self, encrypted: bytes, key: bytes) -> bytes:
        """Decrypt AES-256-GCM data."""
        if not _HAS_CRYPTO:
            raise VaultError("cryptography library required")
        if len(encrypted) < _NONCE_LENGTH + 16:
            raise VaultError("Encrypted data is too short")
        nonce = encrypted[:_NONCE_LENGTH]
        ciphertext = encrypted[_NONCE_LENGTH:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None)

    def read(self, filename: str, session_id: str):
        """Read a vault file (JSON, unencrypted for dev mode)."""
        self._validate_session(session_id)
        safe_name = Path(filename).name
        if safe_name != filename or '..' in filename:
            raise VaultError(f"Invalid vault filename: {filename!r}")

        filepath = self._vault_dir / safe_name
        self._audit.log(action="vault_read", user=f"session:{session_id[:8]}",
                        result="attempt", details=f"Reading {safe_name}")

        if not filepath.exists():
            return {}

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._audit.log(action="vault_read", user=f"session:{session_id[:8]}",
                            result="encrypted", details=f"{safe_name} may be encrypted")
            raise VaultError("File may be encrypted. Use read_encrypted() with key.")

    def write(self, filename: str, data, session_id: str):
        """Write JSON data to the vault (unencrypted for dev mode)."""
        self._validate_session(session_id)
        safe_name = Path(filename).name
        if safe_name != filename or '..' in filename:
            raise VaultError(f"Invalid vault filename: {filename!r}")

        filepath = self._vault_dir / safe_name
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
            self._audit.log(action="vault_write", user=f"session:{session_id[:8]}",
                            result="success", details=f"Wrote {safe_name}")
        except Exception as e:
            self._audit.log(action="vault_write", user=f"session:{session_id[:8]}",
                            result="error", details=str(e))
            raise VaultError(f"Failed to write vault file: {e}")

    def write_encrypted(self, filename: str, data, session_id: str, key: bytes):
        """Write encrypted data to the vault."""
        self._validate_session(session_id)
        safe_name = Path(filename).name
        if safe_name != filename or '..' in filename:
            raise VaultError(f"Invalid vault filename: {filename!r}")

        filepath = self._vault_dir / safe_name
        plaintext = json.dumps(data, default=str, ensure_ascii=False).encode('utf-8')
        encrypted = self.encrypt_data(plaintext, key)

        try:
            with open(filepath, 'wb') as f:
                f.write(encrypted)
            self._audit.log(action="vault_write_encrypted",
                            user=f"session:{session_id[:8]}",
                            result="success",
                            details=f"Wrote encrypted {safe_name} ({len(encrypted)} bytes)")
        except Exception as e:
            self._audit.log(action="vault_write_encrypted",
                            user=f"session:{session_id[:8]}",
                            result="error", details=str(e))
            raise VaultError(f"Failed to write encrypted vault file: {e}")

    def logout(self, session_id: str):
        """End a vault session."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            self._audit.log(action="vault_logout",
                            user=f"session:{session_id[:8]}", result="success")
            self._after_session_end("lock")

    def cleanup_expired_sessions(self) -> int:
        """Remove all expired sessions."""
        expired = [sid for sid, s in self._sessions.items() if s.is_expired]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            self._after_session_end("idle")
        return len(expired)

    # ── Recovery code + PIN change (wrapped-key layer) ────────────────────
    #
    # The recovery code is the interlock that makes Scrub-and-Vault safe to
    # enable: once the vault is the only copy of scrubbed secrets, a forgotten
    # PIN would otherwise mean permanent, total loss. Generating a code adds a
    # SECOND wrap of the same DEK, so EITHER the PIN OR the code can unlock. The
    # code is shown to the user exactly once and never stored in the clear.
    #
    # Threat model, stated plainly: the recovery code protects against a
    # forgotten/rotated PIN. It does NOT protect against loss of BOTH the PIN and
    # the code — that is unrecoverable BY DESIGN (there is no third party, no
    # escrow, no backdoor; the DEK exists only wrapped under these two secrets).

    @property
    def has_recovery_code(self) -> bool:
        """True if a recovery-code wrap is currently enrolled."""
        keyring = self._load_keyring()
        return bool(keyring and isinstance(keyring.get("recovery"), dict))

    @property
    def recovery_code_created_at(self) -> float | None:
        """Unix time the current recovery wrap was minted, or None if none.

        Read-only metadata for a status surface. Returns ONLY the timestamp
        recorded on the wrap — never the code, the DEK, or any wrap bytes. A
        wrap without a recorded ``created`` (or no wrap at all) yields None.
        """
        keyring = self._load_keyring()
        if not keyring:
            return None
        wrap = keyring.get("recovery")
        if not isinstance(wrap, dict):
            return None
        created = wrap.get("created")
        return float(created) if isinstance(created, (int, float)) else None

    def generate_recovery_code(self, session_id: str) -> str:
        """Mint a one-time recovery code for an UNLOCKED vault and return it once.

        Requires a live unlocked session (the DEK must be in hand to wrap it).
        Wraps the current DEK under a fresh recovery-code-derived KEK and stores
        that wrap in the keyring — REPLACING any previous recovery wrap, so
        regenerating invalidates the old code. The plaintext code is returned to
        the caller for a single display and is NEVER persisted or logged.

        Fail-closed: raises VaultError if the vault is locked or the crypto
        backend / keyring is unavailable.
        """
        if not _HAS_CRYPTO:
            raise VaultError("cryptography library required")
        dek = self._session_key(session_id)  # raises if locked/expired
        code = _format_recovery_code(secrets.token_bytes(_RECOVERY_CODE_BYTES))
        wrap = self._wrap_dek(dek, _normalize_recovery_code(code))
        wrap["created"] = time.time()
        # Under the lock, re-read inside it. This hands the owner a code as if
        # it were enrolled, so a lost write here is a lockout with a delay
        # fuse on it (unlocked races lose this exact entry most often).
        with self._keyring_lock:
            keyring = self._load_keyring()
            if not keyring:
                # An initialized vault should always have a keyring here
                # (created at initialize() or migrated at unlock). If it is
                # missing, fail closed rather than fabricate one from an
                # unverified source.
                raise VaultError(
                    "vault keyring unavailable; cannot enroll recovery code")
            keyring["recovery"] = wrap
            self._save_keyring(keyring)
        # A freshly minted code resets the recovery lockout budget.
        self._recovery_failed_attempts = 0
        self._recovery_lockout_until = 0.0
        # Go-live interlock: has_recovery_code just went True. Invalidate the
        # scrub-and-vault activation cache so the feature flips LIVE (starts
        # vaulting) without a restart. Best-effort + lazy to avoid any import
        # cycle; the interlock also has a TTL backstop if this ever no-ops.
        self._invalidate_scrub_activation_cache()
        self._audit.log(action="vault_recovery_enroll",
                        user=f"session:{session_id[:8]}", result="success",
                        details="one-time recovery code minted (value not logged)")
        return code

    @staticmethod
    def _invalidate_scrub_activation_cache() -> None:
        """Best-effort hook: tell the scrub-and-vault interlock to re-read
        has_recovery_code on its next activation check (the recovery wrap just
        changed). Lazy import + swallow errors so vault I/O never depends on it.
        """
        try:
            from puffpai.security.scrub_and_vault import (
                invalidate_recovery_code_cache)
            invalidate_recovery_code_cache()
        except Exception:
            pass

    def recover_with_code(self, recovery_code: str, new_pin: str) -> VaultSession:
        """Recover a vault with the recovery code and set a NEW PIN in one step.

        Flow: verify the code by unwrapping the DEK from the recovery wrap ->
        set the new PIN (new salt + hash) -> re-wrap the DEK under the new
        PIN-KEK -> invalidate the used recovery code (single-use) -> return a
        live unlocked session.

        Rate-limited with its OWN lockout counters (separate from the PIN's).
        Fail-closed: an unconfigured recovery code, a wrong code, or a locked-out
        state all raise AuthenticationError; nothing is mutated on failure.
        """
        if not _HAS_CRYPTO:
            raise VaultError("cryptography library required")
        if not self.is_initialized:
            raise VaultError("Vault not initialized. Call initialize() first.")

        now = time.time()
        if now < self._recovery_lockout_until:
            remaining = int(self._recovery_lockout_until - now)
            self._audit.log(action="vault_recovery", user="unknown",
                            result="lockout",
                            details=f"Locked for {remaining}s")
            raise AuthenticationError(
                f"Too many failed recovery attempts. Try again in {remaining} seconds."
            )

        _check_new_pin(new_pin)

        keyring = self._load_keyring()
        if not keyring or not isinstance(keyring.get("recovery"), dict):
            # No enrolled recovery wrap -> nothing can unlock. Fail closed.
            raise AuthenticationError("No recovery code is configured for this vault")

        try:
            dek = self._unwrap_dek(keyring["recovery"],
                                   _normalize_recovery_code(recovery_code))
        except Exception:
            self._recovery_failed_attempts += 1
            self._audit.log(
                action="vault_recovery", user="unknown", result="failed",
                details=f"Attempt {self._recovery_failed_attempts}/{_MAX_FAILED_ATTEMPTS}")
            if self._recovery_failed_attempts >= _MAX_FAILED_ATTEMPTS:
                self._recovery_lockout_until = time.time() + _LOCKOUT_SECONDS
                self._recovery_failed_attempts = 0
            raise AuthenticationError("Invalid recovery code")

        # Verified. Reset the recovery budget, rotate the PIN, re-wrap the DEK.
        self._recovery_failed_attempts = 0
        new_salt = secrets.token_bytes(_SALT_LENGTH)
        self._salt = new_salt
        self._pin_hash = self._hash_pin(new_pin, new_salt)
        self._save_key_material()

        keyring["pin"] = self._wrap_dek(dek, new_pin)
        # Single-use: the code that was just spent no longer unlocks anything.
        keyring.pop("recovery", None)
        self._save_keyring(keyring)
        # Go-live interlock: has_recovery_code just went False (the wrap was
        # consumed). Invalidate the activation cache so scrub-and-vault stops
        # vaulting live data until a fresh code is minted — no restart needed.
        self._invalidate_scrub_activation_cache()

        # Also reset the PIN attempt budget — the credential just changed.
        self._failed_attempts = 0
        self._lockout_until = 0.0

        session_id = secrets.token_hex(32)
        session = VaultSession(session_id=session_id, created_at=time.time())
        session.key = dek
        self._sessions[session_id] = session
        self._audit.log(action="vault_recovery", user="authenticated",
                        result="success",
                        details=f"PIN reset via recovery; session {session_id[:8]}...")
        self._after_open()
        return session

    def change_pin(self, session_id: str, old_pin: str, new_pin: str) -> None:
        """Change the PIN on an UNLOCKED vault, re-wrapping the DEK.

        Verifies the old PIN (rate-limited via the same hash gate authenticate()
        uses), then re-wraps the in-session DEK under a new PIN-KEK and rotates
        the PIN salt/hash. The DEK — and therefore all encrypted data — is
        unchanged, so no file is re-encrypted. Any enrolled recovery code stays
        valid (it wraps the same DEK). Fail-closed on a wrong old PIN or a
        locked session.
        """
        if not self.is_initialized:
            raise VaultError("Vault not initialized. Call initialize() first.")
        _check_new_pin(new_pin)
        # The caller must already hold an unlocked session (DEK in memory).
        dek = self._session_key(session_id)
        if not hmac.compare_digest(self._hash_pin(old_pin, self._salt),
                                   self._pin_hash):
            self._audit.log(action="vault_change_pin",
                            user=f"session:{session_id[:8]}", result="failed",
                            details="old PIN mismatch")
            raise AuthenticationError("Invalid PIN")

        new_salt = secrets.token_bytes(_SALT_LENGTH)
        self._salt = new_salt
        self._pin_hash = self._hash_pin(new_pin, new_salt)
        self._save_key_material()

        with self._keyring_lock:
            keyring = self._load_keyring() or {"version": _KEYRING_VERSION,
                                               "created": time.time()}
            keyring["pin"] = self._wrap_dek(dek, new_pin)
            self._save_keyring(keyring)
        self._audit.log(action="vault_change_pin",
                        user=f"session:{session_id[:8]}", result="success",
                        details="PIN rotated; DEK re-wrapped")

    # ── TOTP MFA ──────────────────────────────────────────────────────────

    @property
    def totp_enabled(self) -> bool:
        """Check if TOTP MFA is set up."""
        return self._totp_secret_file.exists()

    @property
    def _totp_secret_file(self) -> Path:
        return self._vault_dir / ".vault_totp_secret"

    def setup_totp(self, session_id: str) -> dict:
        """Generate a TOTP secret and return provisioning info.

        Returns a dict with 'secret' and 'provisioning_uri' for QR code generation.
        Requires an active session (must be authenticated with PIN first).
        """
        if not _HAS_TOTP:
            raise VaultError("pyotp library required. Install: pip install pyotp")
        self._validate_session(session_id)

        secret = pyotp.random_base32()
        with open(self._totp_secret_file, 'w') as f:
            f.write(secret)

        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(name="PuffPAI Vault", issuer_name="PuffPAI")

        self._audit.log(action="totp_setup", user=f"session:{session_id[:8]}",
                        result="success", details="TOTP MFA configured")
        return {'secret': secret, 'provisioning_uri': uri}

    def verify_totp(self, code: str) -> bool:
        """Verify a TOTP code against the stored secret.

        Fail-closed: returns False when TOTP is not configured (no secret is
        enrolled, so no code can be valid) and when the code does not match.
        TOTP is an ADDITIONAL factor; this method is NEVER an access gate on
        its own. The PIN gate is authenticate()/authenticate_mfa()
        (hmac.compare_digest, rate-limited, fail-closed). A caller that wants
        "PIN + optional TOTP" must itself check ``totp_enabled`` and require a
        code only when it is set (see authenticate_mfa) — it must NOT rely on
        this method passing through when TOTP is unconfigured.
        """
        if not _HAS_TOTP:
            raise VaultError("pyotp library required")
        if not self.totp_enabled:
            # No secret enrolled => no valid TOTP code exists. Fail closed so
            # this can never act as a standalone bypass on a box where TOTP was
            # never set up (the shipped default).
            return False

        try:
            with open(self._totp_secret_file, 'r') as f:
                secret = f.read().strip()
            totp = pyotp.TOTP(secret)
            return totp.verify(code, valid_window=1)
        except Exception:
            return False

    def required_factors(self) -> list[str]:
        """Ordered authentication factors required to unlock, PIN first.

        This is the single extension point for adding a second factor without
        rewiring the unlock flow. Today it returns ``["pin"]`` plus ``"totp"``
        when TOTP is enrolled. To add e.g. voice MFA later: register a verifier
        with ``register_auth_factor("voice", fn)`` and append ``"voice"`` here
        (conditionally, like TOTP, or unconditionally to make it mandatory).

        Fail-closed contract: an OPTIONAL factor that is not enrolled is simply
        not listed here (it is not silently treated as satisfied); a factor that
        IS listed is enforced by authenticate_mfa() and cannot be skipped.
        """
        factors = ["pin"]
        if self.totp_enabled:
            factors.append("totp")
        return factors

    def register_auth_factor(self, name: str, verifier) -> None:
        """Register/replace the verifier for a named second factor.

        ``verifier`` is a callable ``(code: str) -> bool`` that MUST fail closed
        (return False, never raise-through, when the factor is unconfigured or
        the code is wrong). The PIN factor is handled by authenticate() and is
        not pluggable here. Registering a verifier does NOT by itself make the
        factor required — add it to required_factors() for that.
        """
        if name == "pin":
            raise ValueError(
                "PIN is verified by authenticate(); it is not a pluggable factor")
        if not callable(verifier):
            raise TypeError("verifier must be callable (code) -> bool")
        self._factor_verifiers[name] = verifier

    def authenticate_mfa(self, pin: str, totp_code: str = None, *,
                         factor_codes: dict | None = None,
                         notify: bool = True) -> VaultSession:
        """Authenticate with PIN + every additional required factor (MFA).

        PIN is always verified first (rate-limited, fail-closed). Then each
        factor named by required_factors() beyond "pin" must pass its registered
        verifier or the just-created session is revoked and the unlock fails.

        Codes are supplied via ``factor_codes={"totp": "123456", ...}``;
        ``totp_code`` remains accepted as a convenience alias for the "totp"
        factor so existing callers keep working. When no additional factor is
        required (the shipped default: TOTP not enrolled), this is PIN-only.
        """
        # Step 1: Verify PIN (this checks lockout, rate-limiting, etc.)
        _UNLOCK_TL.in_mfa = True
        try:
            session = self.authenticate(pin)
        finally:
            _UNLOCK_TL.in_mfa = False

        # Assemble the supplied codes, honoring the legacy positional/keyword
        # totp_code without letting it override an explicit factor_codes entry.
        codes = dict(factor_codes or {})
        if totp_code is not None:
            codes.setdefault("totp", totp_code)

        # Step 2: Enforce every ADDITIONAL required factor. Fail closed: a
        # required factor with no registered verifier, no supplied code, or a
        # failing check revokes the session and refuses the unlock.
        extra_factors = [f for f in self.required_factors() if f != "pin"]
        for factor in extra_factors:
            verifier = self._factor_verifiers.get(factor)
            code = codes.get(factor)
            if not code:
                self.logout(session.session_id)
                raise AuthenticationError(f"{factor} code required for MFA")
            if verifier is None or not verifier(code):
                self.logout(session.session_id)
                self._audit.log(action="mfa_verify", user="unknown",
                                result="failed",
                                details=f"factor={factor} invalid")
                raise AuthenticationError(f"Invalid {factor} code")

        if extra_factors:
            self._audit.log(action="mfa_verify",
                            user=f"session:{session.session_id[:8]}",
                            result="success",
                            details=f"MFA complete: {'+'.join(extra_factors)}")

        self._after_open()
        if notify:
            _fire_unlock(session.session_id)
        return session

    # ── Encrypted read/write (production default) ─────────────────────────

    def read_secure(self, filename: str, session_id: str, pin: str):
        """Read and decrypt a vault file using the session's PIN-derived key."""
        self._validate_session(session_id)
        safe_name = Path(filename).name
        if safe_name != filename or '..' in filename:
            raise VaultError(f"Invalid vault filename: {filename!r}")

        filepath = self._vault_dir / safe_name
        self._audit.log(action="vault_read_secure",
                        user=f"session:{session_id[:8]}",
                        result="attempt", details=f"Reading {safe_name}")

        if not filepath.exists():
            return {}

        key = self._dek_from_pin(pin)

        # Try encrypted first, fall back to plaintext JSON for migration
        try:
            with open(filepath, 'rb') as f:
                encrypted = f.read()
            decrypted = self.decrypt_data(encrypted, key)
            return json.loads(decrypted.decode('utf-8'))
        except (VaultError, Exception):
            # Try plaintext fallback (for pre-encryption vault files)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise VaultError(f"Cannot read vault file: {safe_name}")

    def write_secure(self, filename: str, data, session_id: str, pin: str):
        """Encrypt and write data to the vault using the session's PIN-derived key."""
        self._validate_session(session_id)
        safe_name = Path(filename).name
        if safe_name != filename or '..' in filename:
            raise VaultError(f"Invalid vault filename: {filename!r}")

        filepath = self._vault_dir / safe_name
        key = self._dek_from_pin(pin)
        plaintext = json.dumps(data, default=str, ensure_ascii=False).encode('utf-8')
        encrypted = self.encrypt_data(plaintext, key)

        try:
            with open(filepath, 'wb') as f:
                f.write(encrypted)
            self._audit.log(action="vault_write_secure",
                            user=f"session:{session_id[:8]}",
                            result="success",
                            details=f"Wrote encrypted {safe_name} ({len(encrypted)} bytes)")
        except Exception as e:
            self._audit.log(action="vault_write_secure",
                            user=f"session:{session_id[:8]}",
                            result="error", details=str(e))
            raise VaultError(f"Failed to write encrypted vault file: {e}")

    # ── Session-key encrypted I/O (used by request handlers) ──────────────
    #
    # write_secure/read_secure above re-derive the key from the PIN on every
    # call. Request handlers past the unlock step no longer hold the PIN — only
    # a session id — so these variants use the AES key cached on the session at
    # authenticate() time. If the session is locked/expired or has no cached key
    # they FAIL EXPLICITLY (VaultError); they never fall back to plaintext writes.

    def _safe_vault_name(self, filename: str) -> str:
        safe_name = Path(filename).name
        if safe_name != filename or '..' in filename:
            raise VaultError(f"Invalid vault filename: {filename!r}")
        return safe_name

    def _session_key(self, session_id: str) -> bytes:
        """Return the in-memory AES key for an unlocked session.

        Raises VaultError if the session is invalid/expired or has no cached
        key (i.e. the vault is effectively locked for encrypted operations).
        """
        session = self._validate_session(session_id)  # raises on invalid/expired
        key = getattr(session, 'key', None)
        if key is None:
            raise VaultError("Vault is locked: no session encryption key available")
        return key

    is_unlocked = _UnlockStateDescriptor()

    def _session_unlocked(self, session_id: str) -> bool:
        """True if the session is valid and carries a usable encryption key."""
        try:
            self._session_key(session_id)
            return True
        except (VaultError, AuthenticationError):
            return False

    def _first_live_session_id(self):
        """Return any non-expired session id that holds an encryption key.

        Single-PIN vault: every live session derives the same key, so any live
        session can encrypt/decrypt. Used by writers that run outside a
        PIN-bearing request context (e.g. auto-PII detection). Returns None if
        the vault is currently locked.
        """
        for sid, session in list(self._sessions.items()):
            if not session.is_expired and getattr(session, 'key', None) is not None:
                return sid
        return None

    def _is_decryptable(self, raw: bytes, key: bytes) -> bool:
        """True if `raw` authenticates+decrypts under `key` (i.e. already
        encrypted by this vault). GCM's auth tag makes false positives
        negligible, so this reliably distinguishes ciphertext from plaintext."""
        try:
            self.decrypt_data(raw, key)
            return True
        except Exception:
            return False

    def write_secure_session(self, filename: str, data, session_id: str):
        """Encrypt+write JSON `data` using the session's cached key."""
        key = self._session_key(session_id)
        safe_name = self._safe_vault_name(filename)
        filepath = self._vault_dir / safe_name
        plaintext = json.dumps(data, default=str, ensure_ascii=False).encode('utf-8')
        encrypted = self.encrypt_data(plaintext, key)
        try:
            self._atomic_write(filepath, encrypted)
            self._audit.log(action="vault_write_secure",
                            user=f"session:{session_id[:8]}",
                            result="success",
                            details=f"Wrote encrypted {safe_name} ({len(encrypted)} bytes)")
        except Exception:
            # Do not include the underlying error text — it can echo the path
            # or, on some platforms, buffered content.
            self._audit.log(action="vault_write_secure",
                            user=f"session:{session_id[:8]}",
                            result="error", details=f"write failed: {safe_name}")
            raise VaultError(f"Failed to write encrypted vault file: {safe_name}")

    def _atomic_write(self, filepath, data: bytes):
        """Write bytes durably and atomically: fsync a temp file, then
        os.replace() it over the target. os.replace is atomic on both Windows
        and POSIX (same directory), so a crash mid-write leaves either the old
        file intact or the fully-written new one — never a truncated blob that
        would fail to decrypt. The temp name is a dotfile so a leftover from a
        crash between write and replace is ignored by migration/listing/readers.
        """
        tmp = filepath.with_name(f".{filepath.name}.tmp-{secrets.token_hex(4)}")
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, filepath)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def _quarantine_corrupt(self, filename: str):
        """Move an undecryptable/corrupt vault file aside (atomic rename) so a
        fresh log can be started without silently discarding the old blob — it
        may still be recoverable (e.g. after a key/salt change). Returns the
        quarantine file name, or None if the file is absent / rename failed."""
        safe_name = self._safe_vault_name(filename)
        src = self._vault_dir / safe_name
        if not src.exists():
            return None
        n = 0
        while True:
            dest = self._vault_dir / f"{safe_name}.corrupt-{n}"
            if not dest.exists():
                break
            n += 1
        try:
            os.replace(src, dest)
            self._audit.log(action="vault_quarantine", user="system",
                            result="success",
                            details=f"{safe_name} -> {dest.name}")
            return dest.name
        except OSError:
            return None

    def read_secure_session(self, filename: str, session_id: str,
                            *, allow_plaintext: bool = False):
        """Decrypt a JSON vault file using the session's cached key.

        Returns {} if the file does not exist. If `allow_plaintext` is set and
        the file is not validly encrypted, falls back to reading it as plaintext
        JSON (bounded migration window only). Raises VaultError if the vault is
        locked or the file cannot be read.
        """
        key = self._session_key(session_id)
        safe_name = self._safe_vault_name(filename)
        filepath = self._vault_dir / safe_name
        if not filepath.exists():
            return {}
        with open(filepath, 'rb') as f:
            raw = f.read()
        try:
            return json.loads(self.decrypt_data(raw, key).decode('utf-8'))
        except Exception:
            if allow_plaintext:
                try:
                    return json.loads(raw.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise VaultError(f"Cannot read vault file: {safe_name}")
            raise VaultError(f"Cannot decrypt vault file: {safe_name}")

    def write_secure_blob(self, filename: str, data, session_id: str):
        """Encrypt+write a raw document blob (bytes or text) using the session
        key. Unlike write_secure_session this does not JSON-wrap the payload, so
        arbitrary document bytes round-trip exactly."""
        key = self._session_key(session_id)
        safe_name = self._safe_vault_name(filename)
        filepath = self._vault_dir / safe_name
        plaintext = data if isinstance(data, (bytes, bytearray)) else str(data).encode('utf-8')
        encrypted = self.encrypt_data(bytes(plaintext), key)
        try:
            self._atomic_write(filepath, encrypted)
            self._audit.log(action="vault_write_secure",
                            user=f"session:{session_id[:8]}",
                            result="success",
                            details=f"Wrote encrypted {safe_name} ({len(encrypted)} bytes)")
        except Exception:
            raise VaultError(f"Failed to write encrypted vault file: {safe_name}")

    def read_secure_blob(self, filename: str, session_id: str,
                         *, allow_plaintext: bool = False):
        """Decrypt a document blob and return its UTF-8 text (best-effort
        decode). Returns None if the file does not exist. `allow_plaintext`
        permits reading a not-yet-migrated plaintext file during the migration
        window. Raises VaultError if the vault is locked or unreadable."""
        key = self._session_key(session_id)
        safe_name = self._safe_vault_name(filename)
        filepath = self._vault_dir / safe_name
        if not filepath.exists():
            return None
        with open(filepath, 'rb') as f:
            raw = f.read()
        try:
            return self.decrypt_data(raw, key).decode('utf-8', errors='replace')
        except Exception:
            if allow_plaintext:
                return raw.decode('utf-8', errors='replace')
            raise VaultError(f"Cannot decrypt vault file: {safe_name}")

    def read_secure_blob_bytes(self, filename: str, session_id: str):
        """Decrypt a document blob and return its EXACT decrypted bytes.

        Unlike read_secure_blob (which best-effort UTF-8 *decodes* the plaintext
        and thus CORRUPTS binary documents — a PDF/JPEG comes back as U+FFFD
        garbage), this returns raw ``bytes`` with no ``.decode()`` and NO
        plaintext-text fallback. Use this for every binary vault document.

        Returns None if the file does not exist. Raises VaultError if the vault
        is locked (no session key) or the blob does not authenticate/decrypt
        under the session key — fail-closed: a blob that is not valid ciphertext
        is treated as unreadable, never served as raw bytes.
        """
        key = self._session_key(session_id)  # raises VaultError if locked
        safe_name = self._safe_vault_name(filename)
        filepath = self._vault_dir / safe_name
        if not filepath.exists():
            return None
        with open(filepath, 'rb') as f:
            raw = f.read()
        try:
            return self.decrypt_data(raw, key)
        except Exception:
            raise VaultError(f"Cannot decrypt vault file: {safe_name}")

    # ── Zone-1 document index (encrypted, session-keyed, Zone-1-only) ──────
    #
    # vault_documents.json maps doc_id -> {blob, label, owner, doc_type, mime,
    # added, size}. It is written via write_secure_session/read_secure_session
    # (AES-256-GCM under the live session key) so it is only readable post-PIN.
    # Zone 2 never touches it. Audit lines are content-free (doc_id only): never
    # the label, owner, doc_type value, mime, or bytes.

    def _read_doc_index(self, session_id: str) -> dict:
        """Decrypt the doc index. Returns {} when absent. Raises if locked."""
        data = self.read_secure_session(VAULT_DOC_INDEX, session_id)
        return data if isinstance(data, dict) else {}

    def store_document(self, owner: str, doc_type: str, label: str,
                       data: bytes, mime: str, session_id: str) -> str:
        """Encrypt+store a tagged document and index it. Returns the doc_id.

        Validates ``mime`` against VAULT_DOC_MIME_ALLOWLIST (raises VaultError
        on a disallowed type — fail-closed, no SVG/active content). The blob is
        written with write_secure_blob (exact byte round-trip) as
        ``doc_<doc_id>.<ext>``; the metadata is appended to the encrypted index.
        Requires a live session key (raises VaultError if locked).
        """
        if mime not in VAULT_DOC_MIME_ALLOWLIST:
            raise VaultError("Unsupported document type")
        if not isinstance(data, (bytes, bytearray)):
            raise VaultError("Document data must be bytes")
        # Fail-closed on a locked vault BEFORE minting an id / writing anything.
        self._session_key(session_id)

        doc_id = secrets.token_hex(16)
        ext = _VAULT_DOC_MIME_EXT[mime]
        blob = f"doc_{doc_id}.{ext}"
        payload = bytes(data)
        self.write_secure_blob(blob, payload, session_id)

        index = self._read_doc_index(session_id)
        index[doc_id] = {
            "doc_id": doc_id,
            "blob": blob,
            "label": label,
            "owner": owner,
            "doc_type": doc_type,
            "mime": mime,
            "added": _doc_now_iso(),
            "size": len(payload),
        }
        self.write_secure_session(VAULT_DOC_INDEX, index, session_id)
        self._audit.log(action="vault_doc_store",
                        user=f"session:{session_id[:8]}",
                        result="success", details=f"doc={doc_id[:8]}")
        return doc_id

    def find_documents(self, owner_token, doc_type: str,
                       session_id: str) -> list:
        """Return index metadata (NO bytes) for docs matching the tags.

        Exact match only — NEVER a substring cross between owners or types:
          * ``doc_type`` MUST match exactly (required).
          * if ``owner_token`` is given (non-empty), ``owner`` MUST match exactly;
            if it is None/empty, docs of the type from any owner are returned
            (the caller disambiguates by owner).
        Requires a live session key (raises VaultError if locked).
        """
        if not doc_type:
            return []
        index = self._read_doc_index(session_id)
        out = []
        for meta in index.values():
            if not isinstance(meta, dict):
                continue
            if meta.get("doc_type") != doc_type:
                continue
            if owner_token and meta.get("owner") != owner_token:
                continue
            # Metadata only — never the bytes.
            out.append({
                "doc_id": meta.get("doc_id"),
                "blob": meta.get("blob"),
                "label": meta.get("label"),
                "owner": meta.get("owner"),
                "doc_type": meta.get("doc_type"),
                "mime": meta.get("mime"),
                "added": meta.get("added"),
                "size": meta.get("size"),
            })
        return out

    def get_document_bytes(self, doc_id: str, session_id: str):
        """Return ``(bytes, mime, meta)`` for a doc_id, or None if not found.

        Decrypts the blob via read_secure_blob_bytes (raw bytes, no text
        decode). Fail-closed: returns None if the doc_id is unknown or its blob
        is missing; raises VaultError if the vault is locked or the blob cannot
        be decrypted.
        """
        if not doc_id or not isinstance(doc_id, str):
            return None
        index = self._read_doc_index(session_id)
        meta = index.get(doc_id)
        if not isinstance(meta, dict):
            return None
        blob = meta.get("blob")
        mime = meta.get("mime")
        if not blob or mime not in VAULT_DOC_MIME_ALLOWLIST:
            return None
        raw = self.read_secure_blob_bytes(blob, session_id)
        if raw is None:
            return None
        self._audit.log(action="vault_doc_read",
                        user=f"session:{session_id[:8]}",
                        result="success", details=f"doc={doc_id[:8]}")
        return raw, mime, meta

    def migrate_plaintext_files(self, session_id: str) -> dict:
        """Re-encrypt any plaintext files in vault_data using the session key.

        Idempotent: files that already authenticate under the session key are
        skipped. Runs only when the vault is unlocked (a cached key is required).
        Files are re-encrypted in place — the plaintext path is overwritten with
        ciphertext. This is best-effort erasure: on copy-on-write / flash media
        the original plaintext blocks may persist until the filesystem reclaims
        them; secure-erase is out of scope here.

        Files whose format cannot be classified safely (not valid ciphertext,
        not valid JSON, not a ``doc_*`` blob) are left untouched and reported
        under ``failed`` rather than risking corruption.

        Files listed in ``_MIGRATION_SKIP`` (the SERVICE_SECRET_FILES boot
        secrets) are deliberately NOT migrated: they are encrypted under the
        Service Data Key, not the DEK, so re-encrypting them here would orphan
        the unattended boot read.
        """
        key = self._session_key(session_id)
        migrated: list[str] = []
        skipped = 0
        failed: list[str] = []
        if not self._vault_dir.exists():
            return {'migrated': migrated, 'skipped': skipped, 'failed': failed}

        for fname in sorted(os.listdir(self._vault_dir)):
            if fname.startswith('.'):
                continue  # control files: .vault_key_salt, .vault_totp_secret
            if fname in _MIGRATION_SKIP:
                # Externally managed via plaintext read/write API — encrypting
                # here would break that reader. Left for its own migration.
                skipped += 1
                continue
            fpath = self._vault_dir / fname
            if not fpath.is_file():
                continue
            try:
                with open(fpath, 'rb') as f:
                    raw = f.read()
            except OSError:
                failed.append(fname)
                continue
            if not raw:
                continue
            if self._is_decryptable(raw, key):
                skipped += 1
                continue  # already encrypted under this key
            try:
                if fname.startswith('doc_'):
                    # Arbitrary document bytes — preserve exactly.
                    self.write_secure_blob(fname, raw, session_id)
                    migrated.append(fname)
                else:
                    try:
                        obj = json.loads(raw.decode('utf-8'))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        # Not classifiable — leave it alone rather than corrupt.
                        failed.append(fname)
                        continue
                    self.write_secure_session(fname, obj, session_id)
                    migrated.append(fname)
            except Exception:
                failed.append(fname)

        self._audit.log(action="vault_migrate",
                        user=f"session:{session_id[:8]}",
                        result="success",
                        details=(f"migrated={len(migrated)} "
                                 f"skipped={skipped} failed={len(failed)}"))
        return {'migrated': migrated, 'skipped': skipped, 'failed': failed}

    # ── Service-secret tier (consumed by security/secrets_provider.py) ────

    def read_json(self, record: str):
        """Decrypt and return a JSON vault record using any live session key.

        `record` may omit the ``.json`` suffix. Returns None when the vault is
        locked (no live session), the record is absent, or it cannot be decoded
        — callers treat None as "not resolved here" and fall through the chain.
        Never raises and never logs the value.
        """
        sid = self._first_live_session_id()
        if sid is None:
            return None
        fname = record if record.endswith('.json') else f"{record}.json"
        try:
            data = self.read_secure_session(fname, sid)
            return data if isinstance(data, dict) else None
        except (VaultError, AuthenticationError):
            return None

    def get_service_secret(self, name: str):
        """Return a single service secret from the encrypted ``service_secrets``
        vault record, or None if the vault is locked or the secret is absent.
        Fail-closed: never falls back to plaintext, never raises, never logs the
        value."""
        data = self.read_json("service_secrets")
        if not data:
            return None
        value = data.get(name)
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    # ── Writer-context helpers (no PIN-bearing request; use any live session) ─
    #
    # Some writers (chat auto-PII detection, document ingestion) run outside a
    # request that carries a session id. They persist under any currently-live
    # session's key (single-PIN vault ⇒ all live sessions share the key). Each
    # returns True if it persisted (vault unlocked) and False if the vault is
    # locked — in which case the CALLER must drop the data, never write
    # plaintext. Shared here so those call sites cannot drift apart.

    def append_auto_pii(self, record) -> bool:
        """Encrypt-append a PII-detection record to ``auto_detected_pii.json``.

        Best-effort secure append: returns True if persisted, False otherwise
        (vault locked, or a write failure). NEVER raises — callers gate on the
        boolean, so a raise here could let an exception escape into a caller's
        fall-through path. Never writes plaintext; never logs record contents.

        If an existing log file is present but neither decryptable under the
        current session key nor valid plaintext JSON (corrupt, crash-truncated,
        or written under a different key/salt), it is quarantined aside and a
        fresh encrypted log is started, so this detection is not lost and the
        old blob remains recoverable.
        """
        sid = self._first_live_session_id()
        if sid is None:
            return False
        try:
            try:
                existing = self.read_secure_session(
                    'auto_detected_pii.json', sid, allow_plaintext=True)
            except (VaultError, AuthenticationError):
                # Undecryptable/corrupt existing log — quarantine and start fresh.
                self._quarantine_corrupt('auto_detected_pii.json')
                existing = []
            if not isinstance(existing, list):
                existing = []
            existing.append(record)
            self.write_secure_session('auto_detected_pii.json', existing, sid)
            return True
        except Exception:
            # Never propagate: log a content-free audit line and report failure.
            self._audit.log(action="vault_auto_pii",
                            user=f"session:{sid[:8]}",
                            result="error", details="secure append failed")
            return False

    def store_document_secure(self, filename: str, data) -> bool:
        """Encrypt+write a document blob (bytes or text) under any live session.

        Returns True if persisted, False if the vault is locked. Never writes
        plaintext.
        """
        sid = self._first_live_session_id()
        if sid is None:
            return False
        self.write_secure_blob(filename, data, sid)
        return True

    # ── Scrub-and-Vault reference store (pii_refs.json) ───────────────────
    #
    # The reference-handle store maps an opaque, random ref-id to the ORIGINAL
    # sensitive value that was substituted out of an LLM-visible surface by a
    # ``{{vault:<ref-id>}}`` handle. It is written by the request-path choke
    # point (security/scrub_and_vault.py) only for values the Archivist quality
    # gate judged GOOD. Same fail-closed contract as append_auto_pii: True iff
    # persisted encrypted, False when locked or on any write failure; never
    # raises, never writes plaintext, never logs the value.

    def append_pii_ref(self, ref_id: str, record) -> bool:
        """Encrypt-store one scrub-and-vault reference into ``pii_refs.json``.

        Maps ``ref_id -> record`` (``{type, tier, value, created, ...}``) in an
        encrypted dict. Best-effort secure write: returns True if persisted,
        False when the vault is locked or the write fails. NEVER raises.

        If the existing store is present but neither decryptable under the
        current session key nor valid JSON (corrupt / written under a different
        key), it is quarantined aside and a fresh encrypted store is started, so
        this reference is not lost and the old blob stays recoverable — the same
        policy append_auto_pii uses.
        """
        if not ref_id or not isinstance(ref_id, str):
            return False
        sid = self._first_live_session_id()
        if sid is None:
            return False
        try:
            try:
                existing = self.read_secure_session('pii_refs.json', sid)
            except (VaultError, AuthenticationError):
                self._quarantine_corrupt('pii_refs.json')
                existing = {}
            if not isinstance(existing, dict):
                existing = {}
            existing[ref_id] = record
            self.write_secure_session('pii_refs.json', existing, sid)
            return True
        except Exception:
            self._audit.log(action="vault_pii_ref",
                            user=f"session:{sid[:8]}",
                            result="error", details="secure ref write failed")
            return False

    def get_pii_ref(self, ref_id: str, *, caller_user_id=None,
                    enforce_owner: bool = False):
        """Return the stored record for ``ref_id``, or None.

        Minimal read primitive for the rehydration resolver: resolving a
        ``{{vault:<ref-id>}}`` handle back to its plaintext value on an
        authorized surface. Fail-closed: returns None when the vault is locked or
        the ref is absent, never raises, never logs the value.

        Multi-user owner filter (Increment 1): pii_refs records carry the
        ``user_id`` of the principal who created them. When ``enforce_owner`` is
        set, a record whose recorded owner is NOT the ``caller_user_id`` is
        treated as absent (returns None) — the owner cannot resolve the other
        principal's refs
        and vice versa. ``enforce_owner`` DEFAULTS OFF so the single-user paths
        (and the flag-off world) behave exactly as before. Legacy refs written
        without a user_id are attributed to the household owner (see
        ``security.multiuser.record_owner``), so the owner keeps resolving them.
        """
        if not ref_id or not isinstance(ref_id, str):
            return None
        sid = self._first_live_session_id()
        if sid is None:
            return None
        try:
            data = self.read_secure_session('pii_refs.json', sid)
            if not isinstance(data, dict):
                return None
            rec = data.get(ref_id)
            if not isinstance(rec, dict):
                return None
            if enforce_owner:
                # Fail-closed: any error deciding ownership hides the record.
                try:
                    from puffpai.security.multiuser import can_access
                    if not can_access(rec.get("user_id"), caller_user_id):
                        return None
                except Exception:
                    return None
            return rec
        except (VaultError, AuthenticationError):
            return None

    # ── the build engine's doors ───────────────────────────────────────────

    @property
    def vault_dir(self) -> str:
        """The folder this vault lives in."""
        return str(self._vault_dir)

    def add_lock_listener(self, callback: Callable[[str], None]) -> None:
        """Call ``callback(reason)`` whenever the last live session ends
        (``"lock"`` or ``"idle"``). A listener's error never stops a lock."""
        if callable(callback) and callback not in self._lock_listeners:
            self._lock_listeners.append(callback)

    def _fire_lock(self, reason: str) -> None:
        for fn in list(self._lock_listeners):
            try:
                fn(reason)
            except Exception:
                pass

    def _after_session_end(self, reason: str) -> None:
        """Tell the lock listeners when no live session is left."""
        if self._first_live_session_id() is None:
            self._fire_lock(reason)

    def unlock(self, pin: str) -> VaultSession:
        """Open the vault with *pin* alone and return the session.

        For the build engine's own steps (the key broker's terminal commands,
        moving the vault). Same lockout as :meth:`authenticate`, reported as
        :class:`VaultThrottled`. Refused when another factor is enrolled: those
        paths go through :meth:`authenticate_mfa`. Unlock listeners are not
        told, because this is not the household opening its vault.
        """
        if not self.is_initialized:
            self._refuse_if_missing()
            raise VaultError("Vault not initialized. Call initialize() first.")
        if time.time() < self._lockout_until:
            raise VaultThrottled(self._lockout_until - time.time())
        try:
            extra = [f for f in self.required_factors() if f != "pin"]
        except Exception:
            extra = ["unknown"]
        if extra:
            raise AuthenticationError(
                "this vault also needs your second factor; open it from the "
                "Vault page")
        return self.authenticate(pin, notify=False)

    def lock(self) -> None:
        """End every live session now. The data key is dropped from memory."""
        if not self._sessions:
            return
        self._sessions.clear()
        self._audit.log(action="vault_lock", user="system", result="success",
                        details="every session ended")
        self._fire_lock("lock")

    def check_pin(self, pin: str) -> bool:
        """Is *pin* this vault's PIN? Never opens the vault.

        Counts against the same lockout as :meth:`authenticate` (checked
        before any key derivation), so it is never a faster way to guess.
        Raises :class:`VaultThrottled` in a lockout, :class:`VaultMissing` for
        a vault whose drive is not plugged in, and :class:`VaultError` when
        there is no vault.
        """
        if not self.is_initialized:
            self._refuse_if_missing()
            raise VaultError("Vault not initialized. Call initialize() first.")
        if time.time() < self._lockout_until:
            raise VaultThrottled(self._lockout_until - time.time())
        if not isinstance(pin, str) or not pin:
            return False
        candidate_hash = self._hash_pin(pin, self._salt)
        if not hmac.compare_digest(candidate_hash, self._pin_hash):
            self._failed_attempts += 1
            self._audit.log(action="vault_auth", user="unknown", result="failed",
                            details=(f"pin check, attempt {self._failed_attempts}"
                                     f"/{_MAX_FAILED_ATTEMPTS}"))
            if self._failed_attempts >= _MAX_FAILED_ATTEMPTS:
                self._lockout_until = time.time() + _LOCKOUT_SECONDS
                self._failed_attempts = 0
            return False
        self._failed_attempts = 0
        self._audit.log(action="vault_auth", user="authenticated",
                        result="success", details="pin check")
        return True

    def redeem_recovery_code(
        self, code: str, new_password: str, *,
        policy: Optional[Callable[[str], tuple[bool, str]]] = None,
    ) -> tuple[VaultSession, str]:
        """Recover with the recovery code, set a new PIN, and get a new code.

        :meth:`recover_with_code` (the used code stops working) followed by
        :meth:`generate_recovery_code`, so the caller has a fresh code to show
        once. *policy* is an optional stricter rule for the new PIN, checked
        first, along with :func:`_check_new_pin`, so a refused PIN costs no
        key derivation and changes nothing.
        """
        if policy is not None:
            ok, reason = policy(new_password)
            if not ok:
                raise WeakSecretError(reason or "that PIN is not allowed")
        try:
            _check_new_pin(new_password)
        except ValueError as exc:
            raise WeakSecretError(str(exc)) from None
        session = self.recover_with_code(code, new_password)
        new_code = self.generate_recovery_code(session.session_id)
        return session, new_code

    # -- where the vault should be ---------------------------------------

    def missing_reason(self) -> str:
        """Plain words when this vault's folder or drive is missing, else ''.

        Only for the install's own vault, and only once a vault was made (or
        opened) here before: see :func:`remember_place`. A fresh install has
        nothing remembered and nothing missing.
        """
        if not self._tracked:
            return ""
        if self.is_initialized:
            self._adopt_place()
            return ""
        recorded = read_place()
        if not recorded:
            return ""
        here = os.path.normcase(os.path.abspath(self._vault_dir))
        there = os.path.normcase(os.path.abspath(recorded))
        if here != there and _vault_files_present(recorded):
            return (f"Your vault is in {recorded}, but this computer is set to "
                    f"use {self._vault_dir}, where there is none. No second "
                    f"vault was made. Set it back to {recorded} to use your "
                    f"vault.")
        return (f"{VAULT_DRIVE_MISSING} Your vault was made in {recorded}, "
                f"and that folder is not there now. Plug in the drive that "
                f"holds it and try again. A new, empty vault is never made in "
                f"its place. If that drive is gone for good and you want to "
                f"start a new, empty vault, delete {_PLACE_FILENAME} from the "
                f"state folder first (puffpai where shows that folder).")

    def _refuse_if_missing(self) -> None:
        why = self.missing_reason()
        if why:
            self._audit.log(action="vault_missing", user="system",
                            result="refused", details="vault folder missing")
            raise VaultMissing(why)

    def _adopt_place(self) -> None:
        """Remember where the install's own vault is, once it exists."""
        if self._tracked and self.is_initialized:
            remember_place(str(self._vault_dir))

    def place_fingerprint(self) -> str:
        """Which vault this is, as only its PIN can show; ``""`` when locked.

        An HMAC of a fixed text under the open vault's data key: nobody
        without the key can make a vault that gives the same answer.
        """
        for session in list(self._sessions.values()):
            key = getattr(session, "key", None)
            if not session.is_expired and key:
                return hmac.new(bytes(key), _PLACE_FP_CONTEXT,
                                hashlib.sha256).hexdigest()
        return ""

    # -- named secrets and the vault's own reserved items ------------------
    #
    # One encrypted record (:data:`_NAMED_SECRETS_FILE`) under the data key,
    # read from disk on every call (a terminal and the server may both hold a
    # handle on the same folder) and rewritten under a per-folder lock.
    # Names starting with ``__puffpai.`` belong to the vault itself: no public
    # method reads, writes, lists or deletes them, and sign-up key records
    # (``__puffpai.key.*``) only the key broker may touch.

    def _open_key(self, *, touch: bool = True) -> bytes:
        for session in list(self._sessions.values()):
            key = getattr(session, "key", None)
            if not session.is_expired and key is not None:
                if touch:
                    session.touch()
                return key
        raise VaultLocked("the vault is locked")

    def _named_load(self, key: bytes) -> dict:
        path = self._vault_dir / _NAMED_SECRETS_FILE
        if not path.exists():
            return {}
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            data = json.loads(self.decrypt_data(raw, key).decode("utf-8"))
        except Exception:
            raise VaultError("Cannot decrypt vault file: " + _NAMED_SECRETS_FILE)
        return data if isinstance(data, dict) else {}

    def _named_save(self, key: bytes, data: dict) -> None:
        plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._atomic_write(self._vault_dir / _NAMED_SECRETS_FILE,
                           self.encrypt_data(plaintext, key))

    def store(self, name: str, value: str) -> None:
        """Store (or replace) a named secret string. Requires an open vault."""
        if _is_reserved(name):
            raise VaultError("that name is reserved for the vault itself")
        key = self._open_key()
        with _named_lock_for(self._vault_dir):
            data = self._named_load(key)
            data[str(name)] = str(value)
            self._named_save(key, data)
        self._audit.log(action="vault_store_secret", user="system",
                        result="success", details="named secret stored")

    def retrieve(self, name: str) -> Optional[str]:
        """Return a named secret, or ``None`` if it is not present."""
        key = self._open_key()
        if _is_reserved(name):
            return None
        value = self._named_load(key).get(str(name))
        return value if isinstance(value, str) else None

    def list_secrets(self) -> list:
        """The sorted names of stored secrets (names only, never values)."""
        key = self._open_key()
        return sorted(k for k in self._named_load(key) if not _is_reserved(k))

    def delete(self, name: str) -> bool:
        """Delete a named secret. Returns True if it existed."""
        if _is_reserved(name):
            return False
        key = self._open_key()
        with _named_lock_for(self._vault_dir):
            data = self._named_load(key)
            existed = data.pop(str(name), None) is not None
            if existed:
                self._named_save(key, data)
        if existed:
            self._audit.log(action="vault_delete_secret", user="system",
                            result="success", details="named secret deleted")
        return existed

    def _reserved_get(self, name: str) -> Optional[str]:
        """Read a reserved item. Internal: for :mod:`vault_inbox` and, for
        ``__puffpai.key.*`` names only, :mod:`puffpai.security.keys`."""
        # An automatic job reading a key is not the owner using the vault.
        key = self._open_key(touch=False)
        if not _is_reserved(name):
            raise VaultError("not a reserved name")
        if _is_key_name(name):
            _require_key_broker()
        value = self._named_load(key).get(str(name))
        return value if isinstance(value, str) else None

    def _reserved_set(self, name: str, value: str) -> None:
        """Write a reserved item. Internal: for :mod:`vault_inbox` and, for
        ``__puffpai.key.*`` names only, :mod:`puffpai.security.keys`."""
        key = self._open_key()
        if not _is_reserved(name):
            raise VaultError("not a reserved name")
        if _is_key_name(name):
            _require_key_broker()
        with _named_lock_for(self._vault_dir):
            data = self._named_load(key)
            data[str(name)] = str(value)
            self._named_save(key, data)
        self._audit.log(action="vault_store_secret", user="system",
                        result="success", details="reserved")

    def _reserved_delete(self, name: str) -> bool:
        """Delete a sign-up key record. Internal: for the key broker only."""
        key = self._open_key()
        if not _is_key_name(name):
            raise VaultError("only a sign-up key record can be deleted here")
        _require_key_broker()
        with _named_lock_for(self._vault_dir):
            data = self._named_load(key)
            existed = data.pop(str(name), None) is not None
            if existed:
                self._named_save(key, data)
        if existed:
            self._audit.log(action="vault_delete_secret", user="system",
                            result="success", details="reserved")
        return existed

    def _reserved_key_names(self) -> list:
        """Names (never values) of the sign-up key records. Broker only."""
        key = self._open_key(touch=False)
        _require_key_broker()
        return sorted(n for n in self._named_load(key) if _is_key_name(n))

    def delete_service_secret(self, filename: str) -> bool:
        """Remove one service-tier key record. Needs no key: removing is "off".

        Only ``key_*`` names, and only for the key broker. The service tier
        holds :data:`SERVICE_SECRET_FILES` alone, so on this build there is
        normally nothing of that name to remove.
        """
        safe = os.path.basename(str(filename or ""))
        if safe != filename or not safe.startswith("key_") or ".." in safe:
            raise VaultError("not a service-tier key record")
        _require_key_broker()
        path = self._vault_dir / safe
        existed = path.is_file()
        try:
            path.unlink()
        except OSError:
            pass
        if existed:
            self._audit.log(action="vault_service_secret_delete", user="system",
                            result="success", details=safe)
        return existed



# ---------------------------------------------------------------------------
# where the vault should be
# ---------------------------------------------------------------------------

def _write_file_atomic(path: str, data: bytes) -> None:
    """Write *data* to *path* through a temp file and ``os.replace``."""
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def place_record_path() -> Optional[str]:
    """``<state>/vault_place.json``, or None when there is no state folder."""
    try:
        from puffpai.config import get_state_dir
        return os.path.join(get_state_dir(), _PLACE_FILENAME)
    except Exception:
        return None


def read_place() -> Optional[str]:
    """The folder the install's vault was made (or last opened) in, or None.

    A record that cannot be read is read as "somewhere, unknown": the
    install's own folder is then treated as missing rather than as fresh, so
    a damaged record can never lead to a silent second vault: the configured
    folder is returned in that case.
    """
    path = place_record_path()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        folder = data.get("vault_dir") if isinstance(data, dict) else None
        if isinstance(folder, str) and folder.strip() and os.path.isabs(folder):
            return folder
    except (OSError, ValueError):
        pass
    return _default_vault_dir()


def remember_place(vault_dir: str, *, fingerprint: Optional[str] = None) -> bool:
    """Record where the install's vault is, and (when known) which vault it
    is (:meth:`Vault.place_fingerprint`). Never raises; True when written.

    A record for the same folder keeps its fingerprint when none is given. A
    record for another folder never inherits one: a fingerprint is only ever
    written by a caller that has the vault open.
    """
    path = place_record_path()
    if not path:
        return False
    folder = os.path.abspath(vault_dir)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if (isinstance(data, dict) and data.get("vault_dir") == folder
                    and (not fingerprint or data.get("fingerprint") == fingerprint)):
                return False
    except (OSError, ValueError):
        pass
    record: dict = {"schema": _PLACE_SCHEMA, "vault_dir": folder,
                    "at": time.time()}
    if fingerprint:
        record["fingerprint"] = str(fingerprint)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_file_atomic(path, json.dumps(record, indent=2).encode("utf-8"))
        return True
    except OSError:
        return False


def remembered_fingerprint() -> str:
    """The remembered vault's fingerprint, or ``""`` when none is known."""
    path = place_record_path()
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        fp = data.get("fingerprint") if isinstance(data, dict) else None
        return fp if isinstance(fp, str) else ""
    except (OSError, ValueError):
        return ""


def forget_place() -> bool:
    """Stop waiting for the remembered vault: delete the record (never the
    vault). Only for a choice made at this computer's command line. True
    when a record was removed."""
    path = place_record_path()
    if not path or not os.path.exists(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def missing_words(vault: Any) -> str:
    """The owner's words when *vault*'s folder or drive is missing, else ''.

    For the places that say "there is no vault yet": when the vault is on a
    drive that is not plugged in, they say that instead, and never offer to
    make a new one. Never raises.
    """
    try:
        why = getattr(vault, "missing_reason", None)
        return str(why() or "") if callable(why) else ""
    except Exception:
        return ""


def _vault_files_present(folder: str) -> bool:
    """True when *folder* already holds a vault (this build's files, or the
    files of the simpler vault format)."""
    names = (".vault_key_salt", _KEYRING_FILE, "vault.meta.json")
    if any(os.path.isfile(os.path.join(folder, n)) for n in names):
        return True
    return (os.path.isfile(os.path.join(folder, "vault.salt"))
            and os.path.isfile(os.path.join(folder, "vault.enc")))


def _default_vault_dir() -> str:
    """The configured vault folder, as text."""
    return str(default_vault_dir())
