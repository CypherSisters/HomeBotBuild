"""Canonical state-directory resolution for import-light modules.

:func:`puffpai.config.resolve_state_dir` is the canon: it defines what
``$PUFFPAI_DATA_DIR`` means (strip, ``~`` expansion, a relative value joined to
the repo root — never the CWD of whoever started the process). Every module
that writes runtime state must land in the same directory, or an appliance's
ledgers and audit trails scatter, and under a hardened systemd unit
(``ProtectSystem=strict``) some of them land outside ``ReadWritePaths`` and the
write simply fails.

Reaching the canon is awkward, though: importing :mod:`puffpai.config` at
module scope pulls in the secrets provider, which reaches the vault — the
cycle risk documented in :mod:`puffpai.agent_events` (see
``_resolve_event_log_path``). The house rule is therefore "import config inside
the function, not at module scope", and the cost of following it is a
per-module replica of the env-handling, each free to drift from the canon and
from each other.

This module is the shared leaf that removes that cost. It imports **stdlib
only** at module scope, so anything in the tree can import it at module scope,
and it defers the :mod:`puffpai.config` import to call time. Two tiers, both
producing the canonical answer:

1. :mod:`puffpai.config` imports — delegate to the real
   :func:`~puffpai.config.resolve_state_dir` (a pure function of
   args/env/filesystem; it needs no config object).
2. :mod:`puffpai.config` cannot be imported at all — fall back to
   :func:`_fallback_runtime_dir`, a local replica of exactly the
   ``configured=None`` path of the canon. It is differential-tested against
   the canon in ``tests/test_statedir.py``.

Callers keep whatever caching and override hooks they already have; this module
deliberately holds no cache of its own, so a test that changes the environment
sees the change on the next call.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Repo root — three directory levels up from ``src/puffpai/statedir.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Mirrors :data:`puffpai.config.STATE_DIR_ENV`. Duplicated rather than
#: imported to keep this module free of any ``puffpai`` import at module scope;
#: ``tests/test_statedir.py`` pins the two together via differential tests.
_STATE_DIR_ENV = "PUFFPAI_DATA_DIR"

#: The "config is unimportable" warning is worth saying once per process, not
#: once per state-dir lookup on every write.
_warned = False


def _fallback_runtime_dir(
    default: str, *, env_subdir: str = "", legacy: str | None = None,
) -> str:
    """Resolve ``$PUFFPAI_DATA_DIR`` the way :mod:`puffpai.config` would.

    Reached only on the tier where :mod:`puffpai.config` cannot be imported at
    all, so the real resolver is out of reach. It replicates
    ``resolve_state_dir(None, repo_root=_REPO_ROOT, default=default,
    env_subdir=env_subdir, legacy=legacy)`` step for step:

    * strip the env value; empty or whitespace-only means "unset", which yields
      ``normpath(default)`` — and note *env_subdir* is **not** joined in that
      branch, matching the canon, where it applies only to the env value.
    * otherwise expanduser, join a relative value to :data:`_REPO_ROOT`, join
      *env_subdir* when given, normalize.
    * *legacy* applies **only** in the env-unset branch, and only when the
      resolved default does not exist yet while the legacy directory does —
      again the canon's rule: an explicit ``$PUFFPAI_DATA_DIR`` is a decision,
      and adoption must never second-guess it.
    """
    env_root = os.environ.get(_STATE_DIR_ENV, "").strip()
    if not env_root:
        resolved = os.path.normpath(default)
        if legacy:
            legacy = os.path.normpath(legacy)
            if (legacy != resolved and not os.path.exists(resolved)
                    and os.path.isdir(legacy)):
                logger.warning(
                    "using legacy state directory %s (expected %s). Move it "
                    "and set an absolute path in config to silence this.",
                    legacy, resolved,
                )
                return legacy
        return resolved
    env_root = os.path.expanduser(env_root)
    if not os.path.isabs(env_root):
        env_root = os.path.join(_REPO_ROOT, env_root)
    return os.path.normpath(
        os.path.join(env_root, env_subdir) if env_subdir else env_root
    )


def resolve_runtime_dir(
    default: str, *, env_subdir: str = "", legacy: str | None = None,
) -> str:
    """Return the directory holding a module's WRITABLE runtime state.

    *default* is the package/repo-relative fallback the calling module shipped
    with, used when ``$PUFFPAI_DATA_DIR`` is unset. *env_subdir*, when given,
    is appended to the env root only — a module that wants its own subtree of a
    relocated data dir (``$PUFFPAI_DATA_DIR/uploads``) while keeping its
    historical in-tree default gets it with one argument.

    *legacy*, when given, names a directory an earlier release wrote to. Its
    semantics are the canon's (see
    :func:`puffpai.config.resolve_state_dir`): it is adopted only when the
    resolved default does not exist yet and the legacy directory does, so
    upgrading a box that already holds state does not silently start empty. It
    never overrides ``$PUFFPAI_DATA_DIR`` — an env root that was set on purpose
    stays the answer whether or not anything lives there yet.

    **Call this lazily — on first use, not at import time.** The whole point of
    this leaf is that it defers the :mod:`puffpai.config` import to call time;
    calling it while your own module is still being imported drags config into
    the import graph anyway and re-creates the cycle risk this avoids. Resolve
    inside an accessor and cache the result there.

    A config that cannot be imported is never fatal: it warns once and falls
    back to :func:`_fallback_runtime_dir`, which yields the same answer (the
    ``configured=None`` path of the canon). Only the *import* is guarded — the
    canonical resolver itself is pure path arithmetic, and an exception out of
    it is a bug that should surface rather than be papered over with a second
    opinion about where state lives.
    """
    global _warned
    try:
        from puffpai.config import resolve_state_dir
    except Exception as exc:  # a broken config must not break state writes
        if not _warned:
            _warned = True
            logger.warning(
                "puffpai.config unavailable for state-dir resolution (%s); "
                "using the local $%s replica", exc, _STATE_DIR_ENV,
            )
        return _fallback_runtime_dir(
            default, env_subdir=env_subdir, legacy=legacy)

    return resolve_state_dir(
        None, repo_root=_REPO_ROOT, default=default, env_subdir=env_subdir,
        legacy=legacy,
    )
