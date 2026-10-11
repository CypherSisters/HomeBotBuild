"""The one place the Home Assistant token comes from.

With independent readers (``config.py`` loading
``config/home_assistant_config.json`` straight into ``cfg.ha.token``,
``ha_bridge.HABridge.__init__`` using that value, and a separate vault path in
``security/ha_vault.py``) the vault migration could never be finished: storing
the token in the vault and scrubbing the plaintext file would take Home
Assistant down, because nothing would read the vault copy.

Every reader goes through :func:`ha_token` (or :func:`resolve_ha_token`
when it wants to know *where* the token came from). ``tests/`` enforces that
with a grep gate over the tree.

Resolution order
----------------
1. ``vault-service`` -- the boot-secret tier: the token, AES-256-GCM encrypted
   under a Service Data Key that is wrapped under a machine-bound secret only
   root can install. No human, no PIN. **This is what makes a 03:00 reboot
   work.** Skipped in strict mode.
2. ``vault-session`` -- the token decrypted with a live PIN (or recovery)
   session. This is the only path in strict mode.
3. ``env-or-keyring`` -- ``HA_TOKEN`` through ``security/secrets_provider.py``
   (OS keyring, then ``/etc/puffpai/api.env``). Pre-existing operator path,
   preserved so a migration does not break an install that uses it. Skipped in
   strict mode: it is a plaintext secret at rest, which is exactly what strict
   mode refuses.
4. ``legacy-plaintext`` -- ``config/home_assistant_config.json``, and ONLY
   while ``PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK`` is on (it is on by
   default until the owner has run the migration). Emits the existing loud
   one-time warning. Skipped in strict mode.

Never, on any path, is the ``MIGRATED_TO_VAULT`` marker or the setup
placeholder returned as if it were a credential -- see
``ha_vault._usable_token``. A marker that reads as a bearer token produces a
401 nobody can trace.

Availability vs confidentiality
-------------------------------
This is a family appliance. The vault fails closed, and unlock/recovery must
never lock the user out. Both have to hold, so the default is
the service tier (confidential at rest against a stolen disk or a leaked
backup, available without a human) and the owner can opt into strict mode
(``ha_token_strict_mode``), which trades the 03:00 reboot for PIN-only
confidentiality. Nothing here logs, returns, or reports the token value.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from puffpai.security import ha_vault

logger = logging.getLogger(__name__)

#: Vault record holding the token. Must be in ``vault.SERVICE_SECRET_FILES``.
VAULT_FILENAME = "ha_token.json"

SOURCE_VAULT_SERVICE = "vault-service"
SOURCE_VAULT_SESSION = "vault-session"
SOURCE_ENV = "env-or-keyring"
SOURCE_PLAINTEXT = "legacy-plaintext"

#: Owner switch. Env wins over the ``agents`` config section, same convention
#: as every other appliance toggle (``agents/preferences.flag``).
STRICT_ENV = "PUFFPAI_HA_TOKEN_STRICT_MODE"
STRICT_CONFIG_KEY = "ha_token_strict_mode"

#: A successful resolution is cached so scrypt does not run on every HABridge
#: construction. FAILURES ARE NEVER CACHED -- that is what lets a strict-mode
#: appliance start serving Home Assistant the moment the owner unlocks the
#: vault, with no restart.
_CACHE_TTL_SECONDS = 300.0
_cache_lock = threading.Lock()
_cached: tuple | None = None  # (Resolution, monotonic_ts)


@dataclass(frozen=True)
class Resolution:
    """Where the token came from. ``token`` is the only field that is a secret;
    everything else is safe to log, report, or return over HTTP."""
    token: str | None
    source: str | None
    detail: str

    @property
    def locked(self) -> bool:
        """True when there is no token AND the reason is a locked vault rather
        than 'no token has ever been configured'."""
        return self.token is None and self.source is None


def strict_mode() -> bool:
    """True when the owner has chosen PIN-only Home Assistant credentials.

    Fail-safe toward availability on a config error: a broken preferences file
    means normal mode, not a house with no lights. The confidentiality default
    (service tier) is still encryption at rest -- it is not plaintext.
    """
    try:
        from puffpai.agents.preferences import flag
        return flag(STRICT_ENV, STRICT_CONFIG_KEY, default=False)
    except Exception as exc:
        logger.debug("strict-mode flag unavailable: %s", exc)
        return False


def invalidate_cache() -> None:
    """Drop the cached resolution. Called after a migration, a strict-mode
    change, or any 401 from Home Assistant."""
    global _cached
    with _cache_lock:
        _cached = None


def _from_vault_service(vault) -> str | None:
    """The unattended boot read. Machine key only -- never a PIN."""
    reader = getattr(vault, "read_service_secret_unattended", None)
    if not callable(reader):
        return None
    data = reader(VAULT_FILENAME)
    if not isinstance(data, dict):
        return None
    return ha_vault._usable_token(data.get("token"))


def _from_vault_session(vault, session_id: str | None) -> str | None:
    """The PIN-session read: an explicit session, else any live one."""
    reader = getattr(vault, "read_service_secret", None)
    if not callable(reader):
        return None
    sid = session_id
    if sid is None:
        finder = getattr(vault, "_first_live_session_id", None)
        sid = finder() if callable(finder) else None
    if sid is None:
        return None
    try:
        data = reader(VAULT_FILENAME, sid)
    except Exception as exc:
        # Locked, expired, no SDK enrolled, or an undecryptable record. Never
        # interpolate the exception text: it can carry a path or a payload.
        logger.debug("vault-session HA token read failed: %s",
                     type(exc).__name__)
        data = None
    token = ha_vault._usable_token(
        data.get("token") if isinstance(data, dict) else None)
    if token:
        return token

    # Backward compatibility: a box where somebody ran the OLD store_ha_token()
    # has ha_token.json encrypted under the DEK, not the SDK, so the read above
    # cannot open it. Read it the old way so such a box is not stranded; the
    # migration script rewrites it into the service tier (which is what makes
    # the unattended boot read work).
    legacy = getattr(vault, "read_secure_session", None)
    if not callable(legacy):
        return None
    try:
        data = legacy(VAULT_FILENAME, sid)
    except Exception as exc:
        logger.debug("legacy DEK-encrypted HA token read failed: %s",
                     type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    return ha_vault._usable_token(data.get("token"))


def _from_env_or_keyring() -> str | None:
    try:
        from puffpai.security.secrets_provider import get_secret
        return ha_vault._usable_token(get_secret("HA_TOKEN"))
    except Exception as exc:
        logger.debug("HA_TOKEN secrets-provider lookup failed: %s",
                     type(exc).__name__)
        return None


def _resolve_uncached(session_id: str | None) -> Resolution:
    strict = strict_mode()

    vault = None
    try:
        vault = ha_vault._get_vault()
    except Exception as exc:
        logger.debug("vault unavailable for HA token: %s", type(exc).__name__)

    if vault is not None:
        if not strict:
            token = _from_vault_service(vault)
            if token:
                return Resolution(token, SOURCE_VAULT_SERVICE,
                                  "boot-secret tier (machine-bound key)")
        token = _from_vault_session(vault, session_id)
        if token:
            return Resolution(token, SOURCE_VAULT_SESSION,
                              "unlocked vault session")

    if strict:
        return Resolution(
            None, None,
            "vault locked: strict mode is on, so the Home Assistant token is "
            "released only after the owner unlocks the vault with their PIN")

    token = _from_env_or_keyring()
    if token:
        return Resolution(token, SOURCE_ENV,
                          "HA_TOKEN from the OS keyring or the environment")

    # session_id=None on purpose: the vault paths are already exhausted above,
    # so this call is exercising ONLY the gated plaintext fallback (and its
    # existing one-time warning).
    token = ha_vault.get_ha_token(None)
    if token:
        return Resolution(token, SOURCE_PLAINTEXT,
                          "legacy plaintext config (migration not finished)")

    if not ha_vault._plaintext_ha_fallback_allowed():
        return Resolution(
            None, None,
            "vault locked: the plaintext fallback is disabled "
            "(" + ha_vault._HA_FALLBACK_ENV + "=0) and the vault holds no "
            "readable copy of the Home Assistant token")
    return Resolution(None, None,
                      "no Home Assistant token is configured on this appliance")


def resolve_ha_token(session_id: str | None = None, *,
                     refresh: bool = False) -> Resolution:
    """Resolve the HA token and say where it came from. Never raises.

    Pass *session_id* to prefer a specific unlocked vault session (the
    migration script does). *refresh* bypasses the success cache.
    """
    global _cached
    # Only the anonymous lookup is cached. A caller that names a session is
    # asking a different question ("what does THIS session see?") and must not
    # be answered from, or poison, the shared entry.
    cacheable = session_id is None
    if cacheable and not refresh:
        with _cache_lock:
            hit = _cached
        if hit is not None and (time.monotonic() - hit[1]) < _CACHE_TTL_SECONDS:
            return hit[0]

    result = _resolve_uncached(session_id)
    if cacheable:
        with _cache_lock:
            # Do NOT cache a miss: the owner unlocking the vault must take
            # effect on the very next call, not up to five minutes later.
            _cached = (result, time.monotonic()) if result.token else None
    return result


def ha_token(session_id: str | None = None, *,
             refresh: bool = False) -> str | None:
    """**The** Home Assistant token accessor. ``None`` when unavailable."""
    return resolve_ha_token(session_id, refresh=refresh).token


def ha_config_path(config_dir: str = "") -> str:
    """Absolute path of the plaintext Home Assistant config file.

    The setup wizard needs the path (to report where it wrote, and to read back
    ``url``/``enabled`` for a prefill) without naming the file. Handing it out
    from here keeps the filename in the two modules the grep gate allows, and
    keeps the wizard's idea of where the file lives identical to the token
    source's — they used to be two independent answers, and on a scratch
    install with ``$PUFFPAI_CONFIG_DIR`` set they were different files.
    """
    return str(ha_vault._config_file(config_dir))


def write_ha_config(url: str, token: str, *, enabled: bool = True,
                    config_dir: str = "") -> str:
    """**The** way to write the Home Assistant credential to disk.

    The setup wizard's one write goes through here rather than opening the file
    itself, so every path that touches the plaintext token -- read or write --
    is inside the two modules ``tests/test_improve_hb_vlt_05_boot.py`` allows.
    Delegates the bytes to :func:`ha_vault.write_plaintext_ha_config` (atomic,
    mode 600), then drops the resolution cache, because a token that has just
    changed must not keep being answered from a five-minute-old success.

    Returns the path written. Never logs, echoes or returns the token.

    This writes the token IN THE CLEAR. That is the state of this build (see
    :func:`ha_vault.ha_token_migration_status`): the vault migration has not
    been run on the appliance, and a wizard that wrote to a vault nothing reads
    at boot would take the house's lights down. When the migration lands, the
    replacement for this function is
    :func:`ha_vault.store_ha_token_service_tier` plus a scrub -- the call sites
    already go through one function, which is the point of it.
    """
    path = ha_vault.write_plaintext_ha_config(url, token, enabled, config_dir)
    invalidate_cache()
    return path


def ha_token_status() -> dict:
    """Content-free description of the HA credential, for ``/system/status``.

    Every value is a boolean, an enum-ish string, or a sentence. The token
    itself is never included, and neither is its length -- a length is a
    fingerprint of the credential type.
    """
    result = resolve_ha_token()
    strict = strict_mode()
    tier: dict = {}
    try:
        vault = ha_vault._get_vault()
        getter = getattr(vault, "service_tier_status", None)
        if callable(getter):
            tier = getter()
    except Exception as exc:
        logger.debug("service tier status unavailable: %s", type(exc).__name__)

    if result.token:
        status = "ready"
    elif strict:
        status = "vault_locked"
    else:
        status = "unavailable"

    return {
        "status": status,
        "token_source": result.source,
        "detail": result.detail,
        "strict_mode": strict,
        "service_wrap_present": bool(tier.get("service_wrap_present")),
        "machine_key_present": bool(tier.get("machine_key_present")),
        "machine_key_source": tier.get("machine_key_source"),
        "machine_key_problem": tier.get("machine_key_problem"),
        "plaintext_fallback_allowed":
            ha_vault._plaintext_ha_fallback_allowed(),
        "plaintext_token_on_disk":
            ha_vault.ha_token_migration_status()["plaintext_token_present"],
    }


# ══════════════════════════════════════════════════════════════════════
# The ELEVATED credential
# ══════════════════════════════════════════════════════════════════════
#
# Everything above resolves the EVERYDAY Home Assistant token: the credential
# of a deliberately NON-ADMIN Home Assistant user. That
# user cannot create scripts or automations and cannot call ``zha.permit``,
# which is correct -- the credential the appliance holds all day, and that a
# compromised model can reach through a tool call, should not be able to
# rewrite Home Assistant's configuration.
#
# But a few capabilities genuinely need one admin write: creating the two tiny
# scoped scripts that let the restricted user open a Zigbee pairing window is
# the first. Adding devices must not mean a manual Home Assistant chore for
# every new device, which rules out "tell the human to do it by hand"; the
# hardening rules out "make the everyday user an admin".
#
# So there is a SECOND record, ``ha_admin_token.json``, holding a long-lived
# token minted from an ADMIN Home Assistant account, and it is deliberately
# NOT a service-tier boot secret:
#
#   * It is written with ``write_secure_session`` and read with
#     ``read_secure_session`` -- AES-256-GCM under the PIN-derived DEK. There
#     is no machine-key wrap, so no unattended read exists, by construction.
#   * The name is NOT in ``vault.SERVICE_SECRET_FILES``, so
#     ``read_service_secret_unattended()`` refuses it before it touches a key
#     (``_service_secret_path`` raises "not a service-tier vault record").
#     :func:`_refuse_unattended_admin_read` re-asserts that at call time, so a
#     future edit to the allowlist trips an exception instead of quietly
#     creating a boot-readable admin credential.
#   * Nothing caches it. The everyday token is cached for five minutes because
#     it is read on every HA call; the admin token is read at most once per
#     owner-approved change and must die with that call.
#
# Availability is NOT a consideration here, which is what makes the tier choice
# easy: no house function depends on this credential. A locked vault means "the
# owner has to unlock before I can finish that one change", never "the lights
# are dead" -- so the availability argument that puts the everyday token in
# the service tier does not apply, and the stricter tier wins.
#
# Nothing below logs, returns, or reports the token value.

#: Vault record holding the elevated (admin) token. Must NOT be in
#: ``vault.SERVICE_SECRET_FILES`` -- see the block comment above.
ADMIN_VAULT_FILENAME = "ha_admin_token.json"

#: Bounds on what may be stored as an admin token. A long-lived HA token is a
#: JWT: a few hundred characters of base64url and two dots, no whitespace.
#: Shape checks at a trust boundary (the owner pastes this over HTTP), not a
#: claim that a well-shaped string is a valid credential.
_ADMIN_TOKEN_MIN_LEN = 32
_ADMIN_TOKEN_MAX_LEN = 4096


def _refuse_unattended_admin_read(vault=None) -> None:
    """Fail loudly if the admin record ever became unattended-readable.

    Cheap, and it turns an allowlist edit three releases from now from a silent
    privilege escalation into an exception with this function's name on it.
    """
    from puffpai.security import vault as _vault_mod
    if ADMIN_VAULT_FILENAME in getattr(_vault_mod, "SERVICE_SECRET_FILES", ()):
        raise RuntimeError(
            "ha_admin_token is in SERVICE_SECRET_FILES: the elevated Home "
            "Assistant credential must never be readable without the owner's "
            "PIN")


def _usable_admin_token(value) -> str | None:
    """The stored value if it is shaped like a credential, else ``None``."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not (_ADMIN_TOKEN_MIN_LEN <= len(token) <= _ADMIN_TOKEN_MAX_LEN):
        return None
    if any(ch.isspace() for ch in token):
        return None
    # Reuse the everyday guard so a migration marker or the setup placeholder
    # can never be presented as a bearer token.
    return ha_vault._usable_token(token)


def store_ha_admin_token(token: str, session_id: str) -> bool:
    """Store the elevated HA token under the owner's live PIN session.

    Returns ``True`` on success; raises ``ValueError`` when *token* is not
    shaped like a credential. Never logs, echoes, or returns the value.
    """
    candidate = _usable_admin_token(token)
    if candidate is None:
        raise ValueError("that does not look like a Home Assistant long-lived "
                         "access token")
    vault = ha_vault._get_vault()
    if vault is None:
        raise RuntimeError("vault unavailable")
    _refuse_unattended_admin_read(vault)
    writer = getattr(vault, "write_secure_session", None)
    if not callable(writer):
        raise RuntimeError("this vault cannot hold session-tier secrets")
    writer(ADMIN_VAULT_FILENAME,
           {"token": candidate,
            "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
           session_id)
    logger.info("ha admin credential stored in the vault (session tier)")
    return True


#: The sentence a person can act on, reused by both locked branches so the two
#: cannot drift apart.
ADMIN_LOCKED_DETAIL = ("the vault is locked: unlock it with your PIN and I'll "
                       "finish the Home Assistant change")


def resolve_ha_admin_token(session_id: str | None = None) -> Resolution:
    """The ELEVATED Home Assistant token, and only while the vault is open.

    Never cached; never falls back to a file, an env var, or the boot-secret
    tier. A locked vault yields a ``locked`` resolution whose ``detail`` is a
    sentence a person can act on. Never raises except when the allowlist
    invariant above has been violated.
    """
    vault = None
    try:
        vault = ha_vault._get_vault()
    except Exception as exc:
        logger.debug("vault unavailable for HA admin token: %s",
                     type(exc).__name__)
    if vault is None:
        return Resolution(
            None, None,
            "the vault is not available, so I can't reach the Home Assistant "
            "admin credential")

    _refuse_unattended_admin_read(vault)

    reader = getattr(vault, "read_secure_session", None)
    if not callable(reader):
        return Resolution(None, None,
                          "this vault cannot hold session-tier secrets")

    sid = session_id
    if sid is None:
        finder = getattr(vault, "_first_live_session_id", None)
        sid = finder() if callable(finder) else None
    if sid is None:
        return Resolution(None, None, ADMIN_LOCKED_DETAIL)

    try:
        data = reader(ADMIN_VAULT_FILENAME, sid)
    except Exception as exc:
        # Locked, expired, or an undecryptable record. Never interpolate the
        # exception text: it can carry a path or a payload.
        logger.debug("vault-session HA admin token read failed: %s",
                     type(exc).__name__)
        return Resolution(None, None, ADMIN_LOCKED_DETAIL)

    token = _usable_admin_token(
        data.get("token") if isinstance(data, dict) else None)
    if token:
        return Resolution(token, SOURCE_VAULT_SESSION,
                          "elevated Home Assistant credential from an unlocked "
                          "vault session")
    return Resolution(
        None, None,
        "no Home Assistant admin credential is stored in the vault yet -- "
        "create a long-lived token from an administrator account in Home "
        "Assistant and save it in the vault once")


def ha_admin_token_status() -> dict:
    """Content-free description of the elevated credential, for status pages."""
    result = resolve_ha_admin_token()
    stored = False
    try:
        vault = ha_vault._get_vault()
        vault_dir = getattr(vault, "_vault_dir", None)
        if vault_dir is not None:
            stored = (vault_dir / ADMIN_VAULT_FILENAME).exists()
    except Exception as exc:
        logger.debug("admin credential presence check failed: %s",
                     type(exc).__name__)
    return {
        "record_present": bool(stored),
        "readable_now": result.token is not None,
        "tier": SOURCE_VAULT_SESSION,
        "detail": result.detail,
    }
