"""Home Assistant token vault wrapper.

Thin convenience layer for storing and retrieving the HA long-lived
access token via the Zone 1 Vault. The token is stored **AES-256-GCM
encrypted** under the session key (``write_secure_session`` /
``read_secure_session``); there is no plaintext write path. When the vault
is locked or the token cannot be decrypted, retrieval falls back to reading
the legacy plaintext ``home_assistant_config.json`` (read-only migration
window) — it never writes the token back in the clear.

Usage:
    from puffpai.security.ha_vault import get_ha_token, store_ha_token

    # Store (requires an unlocked vault session):
    store_ha_token("eyJ0...", session_id)

    # Retrieve (returns plaintext-config fallback when vault unavailable):
    token = get_ha_token()
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_HA_CONFIG_FILE = _REPO_ROOT / "config" / "home_assistant_config.json"
#: Frozen copy of the shipped default, so :func:`_config_file` can tell "nobody
#: touched it" from "a caller redirected it". Never reassign this one.
_DEFAULT_HA_CONFIG_FILE = _HA_CONFIG_FILE
_VAULT_FILENAME = "ha_token.json"


def _config_dir_override() -> str:
    """``$PUFFPAI_CONFIG_DIR``, or '' when it is unset.

    Imported lazily and by NAME from :mod:`puffpai.config` so the env var is
    spelled in exactly one place, with a literal fallback for the case where
    importing the config module here would recurse.
    """
    import os as _os
    try:
        from puffpai.config import CONFIG_DIR_ENV
    except Exception:            # pragma: no cover - import guard only
        CONFIG_DIR_ENV = "PUFFPAI_CONFIG_DIR"
    return _os.environ.get(CONFIG_DIR_ENV, "").strip()


def _config_file(config_dir: str = "") -> Path:
    """The plaintext HA config this process reads and writes.

    Three inputs, most specific first:

    1. an explicit *config_dir* from the caller (the setup wizard passes the
       one it is building into);
    2. a redirected module global — tests monkeypatch ``_HA_CONFIG_FILE`` and
       that must keep winning over any environment;
    3. the config directory ``config.py`` resolves
       (:func:`puffpai.config.resolve_config_dir`): ``$PUFFPAI_CONFIG_DIR``,
       else ``<data dir>/config`` on an install, else ``<repo>/config``.

    Step 3 is the same answer ``config.py`` gives, which is where the setup
    wizard writes. Without it a scratch install (or a test run) would read
    ``url``/``enabled`` out of its own config dir while the token fallback read
    the developer's real file next to the checkout, and an installed build
    (config under the data dir) never found the token setup saved. Reader and
    writer name the same file.
    """
    if config_dir:
        return Path(config_dir) / _DEFAULT_HA_CONFIG_FILE.name
    if _HA_CONFIG_FILE != _DEFAULT_HA_CONFIG_FILE:
        return _HA_CONFIG_FILE
    try:
        from puffpai.config import resolve_config_dir
        return Path(resolve_config_dir()) / _DEFAULT_HA_CONFIG_FILE.name
    except Exception:            # pragma: no cover - import guard only
        pass
    override = _config_dir_override()
    if override:
        return Path(override) / _DEFAULT_HA_CONFIG_FILE.name
    return _HA_CONFIG_FILE


def write_plaintext_ha_config(url: str, token: str, enabled: bool = True,
                              config_dir: str = "") -> str:
    """Write the plaintext HA config atomically, mode 600. Returns the path.

    **This module owns this file**, which is why the setup wizard's write lands
    here rather than in ``setup_ops``: the grep gate in
    ``tests/test_vault_migration_boot_path.py`` says nothing outside this module
    and the token source may name it.

    Stricter than the reads it pairs with in two ways, deliberately:

    * **atomic** — written to a sibling temp file and ``os.replace``d, so a
      crash or a full disk cannot leave a half-written config (or, worse, a
      truncated one with a readable token fragment) where the appliance will
      read it next boot;
    * **mode 600 before any content is in it** — the temp file is created with
      ``0o600`` rather than chmod'ed afterwards, so the token is never on disk
      under the process umask, not even for the width of one syscall.

    The token is never logged, echoed, or returned.
    """
    import tempfile

    path = _config_file(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"url": url, "token": token, "enabled": bool(enabled)}, indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".ha-",
                                    suffix=".json")
    try:
        os.chmod(tmp_name, 0o600)
    except OSError:
        pass                      # Windows: chmod only toggles the read-only bit
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)

# ── Zone-1 hard-gate control ───────────────────────────────────────────────
# The HA long-lived token is a Zone-1 secret. When the vault is locked (or the
# token was never migrated into it), get_ha_token() can fall back to reading the
# plaintext home_assistant_config.json so Home Assistant keeps working at a
# headless boot before anyone has unlocked the vault. That fallback exposes a
# Zone-1 secret WITHOUT a PIN session.
#
# This env flag gates that fallback. DEFAULT = enabled, deliberately: until the
# vault migration has been run the token lives ONLY in plaintext, so disabling
# the fallback by default would silently break HA. Set
# PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK=0 to fail closed once the token has
# been migrated into the vault AND a boot-time unlock path exists.
_HA_FALLBACK_ENV = "PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK"
_FALLBACK_WARNED = False

#: Values that occupy the ``token`` field but are NOT tokens. Both must be
#: rejected everywhere a token is read.
#:
#: ``MIGRATED_TO_VAULT`` is the one that bites: :func:`store_ha_token` writes it
#: over the plaintext token on success, and before this constant existed
#: :func:`get_ha_token` happily returned that literal string as the token on the
#: plaintext-fallback path — so a migrated appliance with a locked vault would
#: have sent ``Authorization: Bearer MIGRATED_TO_VAULT`` to Home Assistant and
#: failed with a 401 that named nothing. A migration marker must read as "no
#: token here", never as a credential.
_NOT_A_TOKEN = frozenset({
    "YOUR_LONG_LIVED_ACCESS_TOKEN_HERE",
    "MIGRATED_TO_VAULT",
})

#: What :func:`store_ha_token` leaves behind in the plaintext config.
MIGRATED_MARKER = "MIGRATED_TO_VAULT"


def _usable_token(value) -> str | None:
    """The value if it is a real token, else None. Never logs the value."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or token in _NOT_A_TOKEN:
        return None
    return token


def _plaintext_ha_fallback_allowed() -> bool:
    """Whether the locked-vault plaintext-token fallback is permitted.

    Fail-closed only when the owner explicitly opts out; otherwise preserves
    HA availability. Recognized 'off' values: 0/false/no/off (case-insensitive).
    """
    val = os.environ.get(_HA_FALLBACK_ENV)
    if val is None:
        return True  # preserve availability by default (see note above)
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def _load_plaintext_config() -> dict:
    """Load the legacy plaintext HA config file."""
    if _config_file().exists():
        try:
            with open(_config_file(), 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "url": "http://homeassistant.local:8123",
        "token": "YOUR_LONG_LIVED_ACCESS_TOKEN_HERE",
        "enabled": False,
    }


def _get_vault():
    """Return the process-wide Vault singleton, else a fresh Vault, else None.

    Sessions (and their in-memory AES keys) are INSTANCE-scoped, so the
    encrypted read/write must run against the SAME Vault instance the API
    server unlocked — a fresh ``Vault()`` sees no live session and its
    session-key methods would always fail (fail-closed / locked). Prefer the
    shared singleton; only fall back to a fresh instance if the app server
    isn't importable (that instance has no sessions, so encrypted ops fail
    closed rather than writing plaintext). Imports are lazy to preserve the
    no-circular-dependency property.
    """
    try:
        from puffpai.api_server import _get_vault as _get_shared_vault
        v = _get_shared_vault()
        if v is not None:
            return v
    except Exception:
        pass
    try:
        from puffpai.security.vault import Vault
        return Vault()
    except Exception:
        return None


def get_ha_token(session_id: str | None = None) -> str | None:
    """Retrieve the HA token, preferring the encrypted vault over plaintext.

    1. If *session_id* is provided and the vault is initialized → decrypt from
       the vault via ``read_secure_session``. If the stored file is a legacy
       plaintext blob and the session is unlocked, read it with
       ``allow_plaintext=True`` and opportunistically re-encrypt it (best
       effort — a migration failure never produces a plaintext write and never
       breaks the read).
    2. On a locked/undecryptable vault, fall back to reading the legacy
       plaintext ``home_assistant_config.json`` with a single content-free
       warning.

    Returns ``None`` when no usable token exists.
    """
    # Try the encrypted vault first.
    if session_id:
        vault = _get_vault()
        if vault and vault.is_initialized:
            # Lazy import to preserve the no-circular-dependency property.
            from puffpai.security.vault import VaultError, AuthenticationError
            try:
                data = vault.read_secure_session(_VAULT_FILENAME, session_id)
                token = _usable_token(
                    data.get("token") if isinstance(data, dict) else None)
                if token:
                    return token
            except (VaultError, AuthenticationError):
                # We cannot tell "locked" from "undecryptable" by type alone.
                # Only retry with allow_plaintext when the session is unlocked
                # (i.e. the failure was decryption, not a locked/expired
                # session). Otherwise fall through to the plaintext config.
                if vault.is_unlocked(session_id):
                    try:
                        data = vault.read_secure_session(
                            _VAULT_FILENAME, session_id, allow_plaintext=True)
                        token = _usable_token(
                            data.get("token") if isinstance(data, dict) else None)
                        if token:
                            # Opportunistically re-encrypt the legacy plaintext
                            # blob. Best-effort: a failure here must not break
                            # the read, and write_secure_session never falls
                            # back to a plaintext write.
                            try:
                                vault.write_secure_session(
                                    _VAULT_FILENAME, data, session_id)
                            except (VaultError, AuthenticationError):
                                pass
                            return token
                    except (VaultError, AuthenticationError):
                        pass  # fall through to plaintext config
            except Exception:
                pass  # unexpected — fall through to plaintext config

    # Plaintext config fallback (read-only; never re-writes the token).
    # Gated: exposing the Zone-1 token without a PIN session is an owner
    # decision. Fail closed when the fallback is disabled.
    if not _plaintext_ha_fallback_allowed():
        global _FALLBACK_WARNED
        if not _FALLBACK_WARNED:
            _FALLBACK_WARNED = True
            # Content-free: no token material.
            warnings.warn(
                "Vault locked and plaintext HA-token fallback is disabled "
                f"({_HA_FALLBACK_ENV}); HA token withheld until the vault is "
                "unlocked.",
                stacklevel=2,
            )
        return None

    cfg = _load_plaintext_config()
    token = _usable_token(cfg.get("token"))
    if token:
        # Content-free: no token material, no interpolated exception text.
        warnings.warn(
            "Vault locked or HA token unreadable; "
            "falling back to plaintext config.",
            stacklevel=2,
        )
        return token

    return None


def get_ha_config(session_id: str | None = None) -> dict:
    """Return the full HA config dict with token resolved from vault.

    Always returns a dict with keys ``url``, ``token``, ``enabled``.
    The ``token`` value comes from the vault when possible.
    """
    cfg = _load_plaintext_config()
    # Resolve through the one token source (vault service tier ->
    # vault session -> keyring/env -> this file), not just this module's own
    # vault path. Lazy import: ha_token_source imports THIS module at module
    # scope, so the dependency has to run one way at import time.
    try:
        from puffpai.security.ha_token_source import ha_token as _ha_token
        vault_token = _ha_token(session_id)
    except Exception:
        vault_token = get_ha_token(session_id)
    if vault_token:
        cfg["token"] = vault_token
    elif _usable_token(cfg.get("token")) is None:
        # Never hand a caller the migration marker or the setup placeholder as
        # if it were a credential: an empty token fails loudly at the HA client,
        # a marker fails as a 401 nobody can trace.
        cfg["token"] = ""
    return cfg


def store_ha_token(token: str, session_id: str) -> bool:
    """DEPRECATED. Use store_ha_token_service_tier() instead.

    Two reasons not to call this. It encrypts under the PIN-derived
    DEK, so the appliance cannot read the result after an unattended reboot --
    only a PIN session can. And it scrubs the plaintext config as a SIDE
    EFFECT of storing: storing is safe, scrubbing is not, and they must be two
    decisions. Kept so an appliance where somebody already ran it is not
    stranded (ha_token_source reads that DEK-encrypted record on the PIN path).

    Store the HA token AES-256-GCM encrypted in the vault.

    Requires an unlocked vault session. Writes via ``write_secure_session``,
    which encrypts under the session key and never falls back to a plaintext
    write. After success the plaintext ``token`` field is scrubbed from the
    config file (replaced with a placeholder).

    Returns True on success, False on failure.
    """
    vault = _get_vault()
    if vault is None or not vault.is_initialized:
        warnings.warn("Vault not initialized — cannot store HA token.", stacklevel=2)
        return False

    # Lazy import to preserve the no-circular-dependency property.
    from puffpai.security.vault import VaultError, AuthenticationError
    try:
        vault.write_secure_session(_VAULT_FILENAME, {"token": token}, session_id)
    except (VaultError, AuthenticationError):
        # Generic message only — never interpolate the exception text (could
        # carry payload) and never log the token.
        warnings.warn(
            "Failed to store HA token in vault (locked or unavailable).",
            stacklevel=2,
        )
        return False

    # Scrub plaintext token from the config file
    try:
        cfg = _load_plaintext_config()
        cfg["token"] = MIGRATED_MARKER
        _config_file().parent.mkdir(parents=True, exist_ok=True)
        with open(_config_file(), 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass  # non-fatal — vault already has the token

    return True


# ── the migration ──────────────────────────────────────────────────────────
#
# vault.migrate_plaintext_files() walks the vault directory only, so it cannot
# see config/home_assistant_config.json; no normal operation ever moves the
# token. Until the migration below is run, the appliance holds a plaintext
# long-lived token at mode 600 and logs "falling back to plaintext" warnings.
#
# What it needs, and why an operator cannot do it unattended: the write goes
# through vault.write_secure_session(), which encrypts under the session key
# from vault._session_key(session_id). That key is the Data Encryption Key,
# unwrapped from .vault_keyring.json by a KEK derived from either the owner's
# PIN or their recovery code. The DEK's keyring carries exactly two wraps --
# "pin" and "recovery". There is no service or unattended wrap of the DEK. So
# no material on the appliance can unwrap the DEK without a human typing a
# secret, and the migration needs the owner by construction, not by a missing
# feature.
#
# The scrub is a SEPARATE decision from the store, and it defaults off. Storing
# the token in the vault is additive and safe. Scrubbing the plaintext config
# is not: a Home Assistant read that does not go through the vault (see
# ha_token_source) fails once the token is replaced with the marker, and
# without a boot-time unlock path that takes Home Assistant down completely on
# the next config load. Hence
# scrub_plaintext=False by default, and a caller that asks for it gets told
# exactly what it costs.


def ha_token_migration_status() -> dict:
    """Where the HA token lives right now. Content-free — never the value.

    Safe to call from a status endpoint or a report: every field is a boolean,
    a length, or a filename.
    """
    cfg = _load_plaintext_config()
    raw = cfg.get("token")
    return {
        "vault_file": _VAULT_FILENAME,
        "plaintext_config": str(_config_file()),
        "plaintext_token_present": _usable_token(raw) is not None,
        "plaintext_token_len": len(raw) if isinstance(raw, str) else 0,
        "migrated_marker_present": raw == MIGRATED_MARKER,
        "plaintext_fallback_allowed": _plaintext_ha_fallback_allowed(),
        "fallback_env": _HA_FALLBACK_ENV,
    }


def store_ha_token_service_tier(token: str, session_id: str,
                                vault=None) -> bool:
    """Store the token in the vault's BOOT-SECRET tier. No scrub, no marker.

    *vault* MUST be passed by any caller that authenticated its own ``Vault``.
    Sessions and their in-memory keys are INSTANCE-scoped -- ``_get_vault()``
    returns a different object with an empty ``_sessions`` dict, so storing
    through it with a session id minted elsewhere fails with "vault is locked",
    blaming the PIN the owner typed correctly.

    Encrypted under the Service Data Key, which is wrapped both under the DEK
    (so the owner's PIN always reaches it) and — once the service wrap is
    enrolled — under a machine-bound secret, which is what lets the appliance
    read it after an unattended 03:00 reboot.

    Deliberately separate from :func:`store_ha_token`: that one scrubs the
    plaintext config as a side effect of storing. Storing and scrubbing are
    two decisions.

    Returns True on success. Never logs or returns the token.
    """
    if vault is None:
        vault = _get_vault()
    if vault is None or not getattr(vault, "is_initialized", False):
        warnings.warn("Vault not initialized — cannot store HA token.",
                      stacklevel=2)
        return False
    writer = getattr(vault, "write_service_secret", None)
    if not callable(writer):
        warnings.warn(
            "This vault has no boot-secret tier; upgrade security/vault.py.",
            stacklevel=2)
        return False
    from puffpai.security.vault import VaultError, AuthenticationError
    try:
        writer(_VAULT_FILENAME, {"token": token}, session_id)
    except (VaultError, AuthenticationError):
        # Generic only — the exception text can carry a path or a payload.
        warnings.warn(
            "Failed to store HA token in the vault (locked or unavailable).",
            stacklevel=2)
        return False
    return True


def scrub_plaintext_ha_token() -> bool:
    """Replace the plaintext token with the migration marker. Mode 600.

    Split out of :func:`store_ha_token` so a caller can verify a live Home
    Assistant read from the vaulted copy FIRST and only then remove the copy
    that is keeping the house working.
    """
    try:
        cfg = _load_plaintext_config()
        cfg["token"] = MIGRATED_MARKER
        _config_file().parent.mkdir(parents=True, exist_ok=True)
        with open(_config_file(), 'w') as f:
            json.dump(cfg, f, indent=2)
        try:
            os.chmod(_config_file(), 0o600)
        except OSError:
            pass
        return True
    except Exception:
        return False


def migrate_ha_token_to_vault(session_id: str,
                              scrub_plaintext: bool = False,
                              vault=None) -> dict:
    """Move the plaintext HA token into the vault's boot-secret tier.

    Pass *vault* when you authenticated your own ``Vault`` instance: sessions
    are instance-scoped, so *session_id* is meaningless to any other one.

    Requires an unlocked session (the owner's PIN). Idempotent and safe to
    re-run: an already-scrubbed config is reported as done rather than treated
    as an error, and re-running on an unscrubbed box simply rewrites the same
    record.

    *scrub_plaintext* still defaults to **False**. Storing is additive and
    safe; removing the plaintext copy is not, and the caller
    (``scripts/migrate_ha_token.py``) only asks for it after it has verified a
    live Home Assistant read using the vaulted token.

    Returns a content-free dict; never raises, never logs the token.
    """
    result = {
        "stored": False,
        "scrubbed": False,
        "already_stored": False,
        "reason": "",
    }

    cfg = _load_plaintext_config()
    raw = cfg.get("token")

    if raw == MIGRATED_MARKER:
        result["already_stored"] = True
        result["reason"] = ("the config already carries the migration marker; "
                            "nothing left in plaintext to move")
        return result

    token = _usable_token(raw)
    if token is None:
        result["reason"] = ("no usable token in the plaintext config — nothing "
                            "to migrate")
        return result

    if vault is None:
        vault = _get_vault()
    if vault is None or not vault.is_initialized:
        result["reason"] = "vault is not initialized"
        return result
    if not vault.is_unlocked(session_id):
        result["reason"] = ("vault is locked for this session; the owner's PIN "
                            "is required. (If you are calling this from a "
                            "script that authenticated its own Vault, pass "
                            "that instance as vault= -- sessions are "
                            "instance-scoped.)")
        return result

    if not store_ha_token_service_tier(token, session_id, vault=vault):
        result["reason"] = "vault write failed; plaintext config left untouched"
        return result
    result["stored"] = True

    if scrub_plaintext:
        result["scrubbed"] = scrub_plaintext_ha_token()
        result["reason"] = (
            "token stored in the vault and scrubbed from the plaintext config"
            if result["scrubbed"] else
            "token stored in the vault, but the plaintext config could not be "
            "rewritten -- the plaintext copy is still on disk")
        return result

    result["reason"] = (
        "token stored in the vault's boot-secret tier; the plaintext config "
        "was left in place. Verify a live Home Assistant read from the vaulted "
        "copy, then re-run with --scrub to remove the plaintext one and set "
        f"{_HA_FALLBACK_ENV}=0.")
    return result
