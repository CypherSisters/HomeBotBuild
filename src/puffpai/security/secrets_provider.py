"""
PuffPAI Secrets Provider — single source of truth for service credentials.

Service secrets (HA token, automation API token, and the like) read from
plaintext ``.env`` files via ``os.getenv`` sit on disk in the clear. The Vault
(``vault.py``) protects user PII; this module lets it be the source of service
secrets too, so "the vault is the only source" can be enforced.

This module is the migration path. It resolves a secret by name through an
ordered chain of providers and returns the first hit:

    1. PuffPAI Vault          — the appliance target (requires an unlocked
                                vault session; secrets live encrypted at rest)
    1b. The install's env file — a name this install's ``homebot.env`` actually
                                provided (see below); outranks the keychain
    2. OS keychain            — Windows Credential Manager / macOS Keychain /
                                Secret Service, via the optional ``keyring`` lib
                                (good for the dev laptop; no plaintext on disk)
    3. Environment / .env     — legacy fallback, emits a deprecation warning so
                                the remaining plaintext usages are visible

WHY THE ENV FILE OUTRANKS THE OS KEYCHAIN
-----------------------------------------
Plain ``vault -> keyring -> env`` would make a HomeBotBuild install unusable on
any machine that has ever run another PuffPAI build: the OS keychain entry
(service ``puffpai``, entry ``PUFFPAI_API_KEY``) would shadow the key the
installer just minted into ``<state>/homebot.env``, the one the install docs
tell the owner to copy into ``dashboard/.env.local`` and into every client.
The backend would authenticate against the keychain value, so every keyed
request would answer 401 and the documented key would look simply wrong.

The rule: **a name that THIS install's env file provided beats the
keychain.** The reasoning, in order of weight:

  * The installer minted it, in this install's own state directory, seconds ago.
  * The operator can SEE it (``grep PUFFPAI_API_KEY <state>/homebot.env``), so
    "the key I was given is the key in use" is checkable — which is the whole
    point of a shared secret the owner has to distribute.
  * The keychain entry is per-USER, not per-install: it may belong to a
    different checkout, a different appliance, or a build that was uninstalled.
    Nothing ties it to this state directory.

Scope is deliberately narrow. Only names recorded by
:func:`note_env_file_keys` — which only :mod:`puffpai.envfile` calls, only for
variables it actually set out of that one file, and only for names that were
NOT already in ``os.environ`` — get the promotion. An ordinary ``os.environ``
value (a stray ``export`` in a shell profile) does NOT: it stays at tier 3,
below the keychain. The Vault still outranks everything.

No secret value is ever logged. :func:`get_with_source` returns the tier name
so an entrypoint can print WHICH source won without printing what won.

It is intentionally NON-BREAKING. Adopt it incrementally by replacing
``os.getenv("HA_TOKEN")`` with ``get_secret("HA_TOKEN")``. Per-agent scoping
is supported via
the ``scope`` argument so Cortex can be denied device/cloud credentials.

No secret value is ever logged; only the resolving provider name is.
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# Optional OS keychain support — absence is fine, chain skips it.
try:
    import keyring  # type: ignore
    _HAS_KEYRING = True
except Exception:  # pragma: no cover - environment dependent
    _HAS_KEYRING = False

# The Vault import is deferred (see _vault_lookup) to avoid a hard dependency
# and any import cycle with security/__init__.

_KEYRING_SERVICE = "puffpai"

#: Names this install's ``homebot.env`` provided, recorded by
#: :func:`note_env_file_keys`. Process-local on purpose: provenance is a fact
#: about what THIS process loaded, and putting it in an environment variable
#: would make it forgeable by anything that can set one.
_ENV_FILE_KEYS: set[str] = set()


def note_env_file_keys(names) -> None:
    """Record that *names* came from this install's env file.

    Called by :func:`puffpai.envfile.load_env_file` with the variables it
    actually set. Additive; never logs a value.
    """
    for name in names or ():
        if name:
            _ENV_FILE_KEYS.add(str(name))


def env_file_keys() -> frozenset[str]:
    """The recorded env-file-provided names (for tests and diagnostics)."""
    return frozenset(_ENV_FILE_KEYS)


def reset_env_file_keys() -> None:
    """Forget the recorded provenance. Tests only."""
    _ENV_FILE_KEYS.clear()


class AgentScope(Enum):
    """Which agent is asking. Enforces least privilege: no agent holds a
    credential it does not need.

    The first four scopes are the original three-agent roster (plus SYSTEM) and
    are retained verbatim for backward compatibility. The rest are the product
    agent team; Concierge is deliberately credential-free, mirroring the
    retired CORTEX scope. TOOLSMITH (the generic tool tier) is credential-free
    for a sharper reason: it is
    the only agent that reads attacker-controlled text off the public web, so it
    is the one that must not be holding a house key when it is fooled.
    """

    CORTEX = "cortex"      # inference only — NO device or cloud credentials
    BRIDGE = "bridge"      # integration — HA + Node-RED + smart-home
    SENTINEL = "sentinel"  # security — audit/firewall, no outbound creds
    SYSTEM = "system"      # the app itself / setup — unrestricted
    # ── product agent team ──────────────────────────────────────────
    CONCIERGE = "concierge"    # conversation — holds NO external secret
    STEWARD = "steward"        # house ops — sole holder of HA + media creds
    ARCHIVIST = "archivist"    # memory — sole holder of vault/chromadb creds
    MAJORDOMO = "majordomo"    # proactive — sole holder of automation + calendar
    CARETAKER = "caretaker"    # self-maint — sole holder of service-control
    TOOLSMITH = "toolsmith"    # generic tools — credential-free BY DESIGN


# Per-agent allowlists. A secret not listed for a scope is refused even if it
# exists in a provider. SYSTEM is unrestricted. Extend as services are added.
#
# Some six-agent names below (e.g. VAULT_SERVICE_KEY, SERVICE_CONTROL_KEY) are
# forward-looking: today the vault is PIN-derived and service control is OS-level
# (systemd/Windows SCM), so those secrets are not yet populated in any provider.
# Listing them now fixes the *ownership boundary* — when the credential lands, it
# resolves for exactly one scope. Names that the codebase resolves today
# (HA_TOKEN, JELLYFIN_API_KEY, ADGUARD_PASSWORD, NODERED_API_TOKEN) match reality.
_SCOPE_ALLOW: dict[AgentScope, set[str]] = {
    AgentScope.CORTEX: set(),  # deliberately empty — inference needs no secrets
    AgentScope.BRIDGE: {
        # NODERED_API_TOKEN is only needed when Node-RED adminAuth is enabled.
        "HA_TOKEN", "HA_ACCESS_TOKEN", "NODERED_API_TOKEN",
        "OLLAMA_URL",
    },
    AgentScope.SENTINEL: {
        "PIHOLE_PASSWORD", "ADGUARD_PASSWORD", "PUFFPAI_AUDIT_KEY",
    },
    # ── product agent team ──────────────────────────────────────────
    AgentScope.CONCIERGE: set(),  # talks only to other agents — no secret
    AgentScope.STEWARD: {
        "HA_TOKEN", "HA_ACCESS_TOKEN", "JELLYFIN_API_KEY",
    },
    AgentScope.ARCHIVIST: {
        "VAULT_SERVICE_KEY", "CHROMADB_AUTH_TOKEN",
    },
    AgentScope.MAJORDOMO: {
        "NODERED_API_TOKEN", "CALENDAR_API_KEY",
    },
    AgentScope.CARETAKER: {
        "SERVICE_CONTROL_KEY", "UPDATE_VERIFICATION_KEY",
    },
    # Empty, and it must stay empty. The Toolsmith fetches web pages, which are
    # attacker-controlled text and the team's main prompt-injection surface. The
    # protection is not that it resists being fooled — it is that a fooled
    # Toolsmith holds nothing worth stealing: no HA token, no vault key, no media
    # credential. Adding a name here would put the injection surface and a house
    # credential in the same process and undo the reason this agent is separate
    # from the Steward at all.
    AgentScope.TOOLSMITH: set(),
    # SYSTEM handled specially (no filtering).
}


class SecretNotFound(KeyError):
    """Raised when a secret cannot be resolved by any provider."""


class SecretAccessDenied(PermissionError):
    """Raised when an agent scope is not permitted a given secret."""


@dataclass
class SecretsProvider:
    """Ordered secret resolver. Construct once and reuse.

    Args:
        vault: an authenticated PuffPAI Vault instance, or None. If provided
            and unlocked, it is tried first.
        allow_env_fallback: if False, refuses to read from environment/.env
            (use in the shipped appliance to enforce vault-only).
        allow_keyring: if False, the OS keychain tier is skipped entirely —
            ``keyring`` is never called at all, not merely ignored.
    """

    vault: object | None = None
    allow_env_fallback: bool = True
    # Tests and vault-only deployments set False so the OS keychain (which on a
    # dev machine holds real credentials) is never consulted.
    allow_keyring: bool = True
    _warned: set[str] = field(default_factory=set, repr=False)

    # ── public API ──────────────────────────────────────────────────
    def get(
        self,
        name: str,
        *,
        scope: AgentScope = AgentScope.SYSTEM,
        default: Optional[str] = None,
        required: bool = False,
    ) -> Optional[str]:
        """Resolve a secret by name through the provider chain.

        Raises SecretAccessDenied if ``scope`` is not allowed the secret,
        SecretNotFound if ``required`` and nothing resolves.
        """
        value, _source = self.get_with_source(
            name, scope=scope, default=default, required=required)
        return value

    def get_with_source(
        self,
        name: str,
        *,
        scope: AgentScope = AgentScope.SYSTEM,
        default: Optional[str] = None,
        required: bool = False,
    ) -> tuple[Optional[str], str]:
        """:meth:`get`, plus the NAME of the tier that answered.

        Source is one of ``"vault"``, ``"env file"``, ``"OS keyring"``,
        ``"environment"``, or ``"default"`` when nothing resolved. Never the
        value — this exists so a startup line can say where the credential came
        from without printing it.
        """
        self._check_scope(name, scope)

        for provider_name, lookup in (
            ("vault", self._vault_lookup),
            # The install's own env file outranks the keychain: see the module
            # docstring. Narrow by construction — _env_file_lookup answers only
            # for names puffpai.envfile recorded.
            ("env file", self._env_file_lookup),
            ("OS keyring", self._keyring_lookup),
            ("environment", self._env_lookup),
        ):
            value = lookup(name)
            if value is not None:
                logger.debug("secret %r resolved via %s", name, provider_name)
                return value, provider_name

        if required and default is None:
            raise SecretNotFound(
                f"secret {name!r} not found in vault, this install's env file, "
                f"the OS keyring, or the environment"
            )
        return default, "default"

    # ── scope enforcement ───────────────────────────────────────────
    @staticmethod
    def _check_scope(name: str, scope: AgentScope) -> None:
        if scope is AgentScope.SYSTEM:
            return
        allowed = _SCOPE_ALLOW.get(scope, set())
        if name not in allowed:
            raise SecretAccessDenied(
                f"agent scope {scope.value!r} is not permitted secret {name!r}"
            )

    # ── providers ───────────────────────────────────────────────────
    def _vault_lookup(self, name: str) -> Optional[str]:
        # The Vault service-secret tier reads an encrypted ``service_secrets``
        # record via Vault.get_service_secret (defined in security/vault.py).
        # It returns None when the vault is locked or the secret is absent, so
        # the chain then falls through to keyring/env — this is a *lookup*, and
        # falling through is the intended (fail-open-for-read) behavior, not a
        # security downgrade: writes are never routed here.
        if self.vault is None:
            return None
        getter = getattr(self.vault, "get_service_secret", None)
        if not callable(getter):
            # A vault object was supplied but does not implement the tier.
            # Surface this once (not a silent no-op) and skip the tier.
            if "__vault_tier_unavailable__" not in self._warned:
                self._warned.add("__vault_tier_unavailable__")
                logger.warning(
                    "secrets vault-tier disabled: supplied vault lacks "
                    "get_service_secret(); resolving via keyring/env only"
                )
            return None
        try:
            return getter(name)
        except Exception as exc:  # never let vault errors leak or crash callers
            logger.debug("vault lookup for %r failed: %s", name, exc)
            return None

    def _env_file_lookup(self, name: str) -> Optional[str]:
        """The value of *name* IF this install's env file provided it.

        Honours ``allow_env_fallback``: a vault-only deployment that refuses the
        plaintext tier refuses this one too — it is the same plaintext file,
        merely ranked higher when it is allowed at all.
        """
        if not self.allow_env_fallback:
            return None
        if name not in _ENV_FILE_KEYS:
            return None
        return os.getenv(name)

    def _keyring_lookup(self, name: str) -> Optional[str]:
        if not (_HAS_KEYRING and self.allow_keyring):
            return None
        try:
            return keyring.get_password(_KEYRING_SERVICE, name)
        except Exception as exc:  # pragma: no cover
            logger.debug("keyring lookup for %r failed: %s", name, exc)
            return None

    def _env_lookup(self, name: str) -> Optional[str]:
        if not self.allow_env_fallback:
            return None
        value = os.getenv(name)
        if value is not None and name not in self._warned:
            self._warned.add(name)
            warnings.warn(
                f"secret {name!r} was read from the environment/.env — this is "
                f"the legacy plaintext path. Migrate it "
                f"into the Vault or OS keychain.",
                DeprecationWarning,
                stacklevel=3,
            )
        return value


# ── module-level convenience singleton ──────────────────────────────
_default_provider: SecretsProvider | None = None


def get_default_provider() -> SecretsProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = SecretsProvider()
    return _default_provider


def set_default_provider(provider: SecretsProvider) -> None:
    """Install a provider (e.g. one holding an unlocked Vault) at startup."""
    global _default_provider
    _default_provider = provider


def get_secret(
    name: str,
    *,
    scope: AgentScope = AgentScope.SYSTEM,
    default: Optional[str] = None,
    required: bool = False,
) -> Optional[str]:
    """Shorthand for ``get_default_provider().get(...)``.

    Drop-in for ``os.getenv(name)`` — with vault/keychain precedence, per-agent
    scoping, and a deprecation warning whenever a value still comes from .env.
    """
    return get_default_provider().get(
        name, scope=scope, default=default, required=required
    )


def get_secret_with_source(
    name: str,
    *,
    scope: AgentScope = AgentScope.SYSTEM,
    default: Optional[str] = None,
    required: bool = False,
) -> tuple[Optional[str], str]:
    """Shorthand for ``get_default_provider().get_with_source(...)``."""
    return get_default_provider().get_with_source(
        name, scope=scope, default=default, required=required
    )


__all__ = [
    "AgentScope",
    "SecretsProvider",
    "SecretNotFound",
    "SecretAccessDenied",
    "get_secret",
    "get_secret_with_source",
    "get_default_provider",
    "set_default_provider",
    "note_env_file_keys",
    "env_file_keys",
    "reset_env_file_keys",
]
