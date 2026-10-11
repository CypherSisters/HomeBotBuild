"""Caretaker — WF-6 self-maintenance (deterministic, LLM-independent).

The thing that restarts services cannot be one of the services it restarts, and
it must keep working when the LLM stack is down. So the Caretaker is **mostly
deterministic code**: reachability probes with short timeouts, each fully
guarded, so a health sweep always returns a report even if every dependency is
unreachable. Nothing here imports the inference stack.

Health targets: Ollama, ChromaDB (local persistent path), the api_server, Node-RED,
and Jellyfin. Each probe returns ``{"service", "reachable", "detail"}``.

:meth:`restart_service` is real but deliberately narrow.
It can restart **only** the units in :data:`RESTARTABLE_UNITS`, never itself, and
never faster than :data:`MIN_RESTART_INTERVAL_SECONDS`. Four layers stand between
a caller and a running ``systemctl``:

1. the intent is declared state-changing, so ``bus.send`` hands the envelope to
   Sentinel's veto before the Caretaker ever sees it;
2. a strict name pattern + an explicit allowlist, both checked *before* any
   subprocess is built (no shell, argv list, timeout, captured output);
3. a persisted rate-limit ledger — a watchdog that restarts a crashing service
   every 30 s is a denial of service, not a heal;
4. on the appliance, a root-owned wrapper (``scripts/puffpai-service-control.sh``)
   that re-checks the unit name where an unprivileged process cannot edit it.

``verify_update_pack`` remains a clearly-marked stub.

Credentials: service-control + update-pack verification keys
(``AgentScope.CARETAKER``) — forward-looking; not exercised here (elevation comes
from sudo policy on the appliance, not from a secret this process holds).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

from puffpai.logging_config import get_logger
from puffpai.security.secrets_provider import AgentScope, SecretsProvider
# Safe at module scope: statedir is a stdlib-only leaf that defers its
# puffpai.config import (and with it the secrets provider) to call time.
# _STATE_DIR_ENV is the env-var NAME only, and tests/test_statedir.py pins it to
# config.STATE_DIR_ENV, so reading it here cannot drift from canon.
from puffpai.statedir import _STATE_DIR_ENV, resolve_runtime_dir

from .base import BaseAgent
from .envelope import Envelope

logger = get_logger(__name__)

_PROBE_TIMEOUT = 2.0  # seconds — a health sweep must stay snappy


# ── intent classification ───────────────────────────────────────────
#: Mirrors ``steward.STATE_CHANGING_INTENTS``: the envelope minter looks this up
#: to set ``state_changing=True``, and :meth:`CaretakerAgent.handle` refuses an
#: intent that arrived without it — i.e. one that skipped Sentinel's veto in
#: ``bus.send``. Health probes stay read-only.
STATE_CHANGING_INTENTS = frozenset({"restart_service"})


# ── service control policy ──────────────────────────────────────────
#: Units the Caretaker may restart. Deliberately tiny.
#:
#: * ``puffpai-api`` — listed so a future split (Caretaker in its own unit, which
#:   is what the module docstring argues for) works by changing ``self_unit``
#:   alone. Today the Caretaker runs *inside* this unit, so :meth:`restart_service`
#:   refuses it as a self-restart; see :data:`SELF_RESTART_REFUSAL`.
#: * ``puffpai-dashboard`` — the Next.js UI. Stateless, no in-flight agent work,
#:   restarting it is the textbook heal for a wedged front end.
#:
#: NOT here, on purpose: ``ollama`` (bouncing the inference engine mid-answer is
#: a bigger blast radius than a hung UI), ``docker`` and ``nftables`` (a watchdog
#: must never be able to drop the firewall), and ``puffpai-nodered`` (its
#: ExecStart is ``docker compose up -d``; a recreated container can land on a
#: different bridge network than the firewall rules allow, so "restart" there is
#: a network-policy change wearing a heal's clothing).
RESTARTABLE_UNITS = frozenset({"puffpai-api", "puffpai-dashboard"})

#: Names a caller might plausibly try, with the reason they are refused. Purely
#: for a useful error message — the allowlist above is what actually decides.
DENIED_UNITS = {
    "ollama": "restarting the inference engine mid-answer is not a watchdog's call",
    "docker": "container runtime restart takes the whole automation layer down",
    "nftables": "the watchdog must never be able to drop the firewall",
    "puffpai-nodered": "compose recreate can move the container off the bridge "
                       "the firewall allows; treat as a deploy, not a heal",
}

#: Exact-match aliases so a caller can hand over the *health-check* service name
#: (``run_health_checks`` reports ``api_server``, not ``puffpai-api``) without
#: the validator having to tolerate underscores or fuzzy matching. A closed
#: table, applied only on an exact match, before validation.
UNIT_ALIASES = {
    "api": "puffpai-api",
    "api_server": "puffpai-api",
    "dashboard": "puffpai-dashboard",
}

#: Unit names must look exactly like this before anything else happens.
UNIT_NAME_PATTERN = re.compile(r"^puffpai-[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_UNIT_NAME_LEN = 64

#: The unit this process runs inside. Restarting it would kill the caller
#: mid-call; see :data:`SELF_RESTART_REFUSAL`.
DEFAULT_SELF_UNIT = "puffpai-api"

def _first_existing(candidates: tuple[str, ...], fallback: str) -> str:
    """First path that exists, else *fallback* (resolved via PATH at exec time)."""
    for path in candidates:
        try:
            if os.path.isfile(path):
                return path
        except OSError:
            continue
    return fallback


#: Absolute binaries, resolved once at import. Naming them absolutely means a
#: manipulated ``$PATH`` cannot substitute a different ``sudo``/``systemctl``;
#: the bare-name fallback exists only for hosts (and the dev box) where neither
#: standard location is present, and there the platform check refuses first.
SUDO_PATH = _first_existing(("/usr/bin/sudo", "/bin/sudo"), "sudo")
SYSTEMCTL_PATH = _first_existing(("/usr/bin/systemctl", "/bin/systemctl"), "systemctl")

#: Root-owned wrapper installed by ``scripts/puffpai-service-control.sh``. When
#: present it is preferred over calling ``systemctl`` directly, because it
#: re-checks the unit name somewhere the puffpai user cannot rewrite. Not an env
#: var on purpose: the path of a binary invoked under sudo is not something an
#: environment should get a vote on.
SERVICE_CONTROL_HELPER = "/opt/puffpai/bin/puffpai-service-control"

#: Rate limits, per unit unless noted. A crash-looping service must NOT become a
#: restart loop: the watchdog would turn a broken feature into a broken box.
MIN_RESTART_INTERVAL_SECONDS = 600.0    # 10 minutes between restarts of one unit
RESTART_WINDOW_SECONDS = 3600.0         # rolling window for the counts below
MAX_RESTARTS_PER_UNIT = 3               # per unit, per window
#: All units combined, per window. Deliberately lower than
#: ``MAX_RESTARTS_PER_UNIT × len(RESTARTABLE_UNITS)`` so it actually binds: past
#: this many restarts the appliance is unhealthy, not one unit, and the answer
#: is a human rather than more restarts.
MAX_RESTARTS_GLOBAL = 4
MAX_LEDGER_ENTRIES_PER_UNIT = 50        # the file can never grow without bound

#: Hard ceiling on how long a restart may block the caller.
RESTART_TIMEOUT_SECONDS = 30.0

#: Why the Caretaker will not restart the unit it lives in.
SELF_RESTART_REFUSAL = (
    "refusing to restart the unit this process runs inside: the restart kills "
    "the caller mid-call, so the outcome can never be observed, the rate-limit "
    "record can be lost mid-write, and the health check that triggered it runs "
    "again on the new process — that is a restart loop, not a heal. systemd's "
    "own Restart=on-failure already revives a *dead* api; a *live* api "
    "restarting itself is nearly always the wrong fix. Restart it by hand: "
    "sudo systemctl restart {unit}"
)

#: Repo root — four directory levels up from ``src/puffpai/agents/caretaker.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: Canonical default: the repo-root ``data`` directory, shared with
#: agent_events, consent_gate and zone3/policy_engine, rather than the
#: package-relative ``src/puffpai/data``, for convergence, runtime state
#: scattered across several directories is how one module writes a trail
#: another module never finds. This is still only the *fallback*: ``<repo>/data``
#: is inside the checkout and equally read-only under ``ProtectSystem=strict``,
#: so a deployment MUST set ``paths.state_dir`` (absolute) or
#: ``$PUFFPAI_DATA_DIR``. See :func:`puffpai.config.resolve_state_dir`.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

#: The legacy default, ``src/puffpai/data``. Nothing is written here any
#: more; a ledger an earlier release left behind is adopted IN PLACE by
#: :func:`_resolve_ledger_path`, never copied, never moved, so an upgraded appliance
#: keeps its restart history and the rate limiter does not start from zero.
_LEGACY_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

#: Filename of the restart ledger, under whichever directory wins.
_LEDGER_FILENAME = "caretaker_restarts.json"

#: Absolute path to the restart ledger, resolved on first use and cached here.
#: ``None`` means "not resolved yet"; tests set it directly.
RESTART_LEDGER_PATH: str | None = None

_LEDGER_LOCK = threading.Lock()


class ServiceControlError(RuntimeError):
    """A restart was refused before any subprocess was built."""


def _resolve_ledger_path() -> str:
    """Resolve the restart ledger inside the writable state directory.

    Resolved lazily rather than at import (``puffpai.config`` pulls in the
    secrets provider, so importing it at module scope risks a cycle), and a
    config failure is never fatal.

    The CONFIGURED tier stays a direct :func:`~puffpai.config.resolve_state_dir`
    call, because this module honours ``paths.state_dir`` and
    :func:`~puffpai.statedir.resolve_runtime_dir` always passes
    ``configured=None``. The degraded tier below it is the leaf. Reading
    ``$PUFFPAI_DATA_DIR`` RAW (no strip, no ``~`` expansion, no repo-root join
    for a relative value) would put the ledger for the documented relative
    value ``"data"`` under the CWD the watchdog happened to start in. That is
    a ledger nothing else reads: the restart rate limiter is only a limit if the
    next process finds the same file.

    Legacy adoption is per FILE, not the directory-level ``legacy=`` of the
    canon: ``<repo>/data`` exists in every checkout, so a directory probe would
    never fall through to the legacy ``src/puffpai/data``. It fires only
    when nothing else decided the location — no configured path, no
    ``$PUFFPAI_DATA_DIR``, and no ledger at the new path — and it points reads
    AND writes at the old file in place; nothing is copied or moved, so
    migrating stays the operator's decision.

    Both suppressors test for an EXPLICIT CHOICE, not for a moved directory. An
    operator who points ``$PUFFPAI_DATA_DIR`` at the default data dir (absolute
    ``<repo>/data``, or the relative value ``"data"``, which joins to the repo
    root) resolves to the shipped directory: comparing paths would read that as
    "unset" and adopt, writing the ledger into the read-only code tree while an
    env var explicitly said otherwise.
    """
    configured = ""
    try:
        from puffpai.config import get_config, resolve_state_dir
        cfg = get_config()
        configured = (cfg.paths.state_dir or "").strip()
        data_dir = resolve_state_dir(
            configured or None,
            repo_root=cfg.REPO_ROOT,
            default=_DEFAULT_DATA_DIR,
        )
    except Exception as exc:  # config must never break the watchdog
        logger.warning("config unavailable for caretaker ledger path: %s", exc)
        configured = ""
        data_dir = resolve_runtime_dir(_DEFAULT_DATA_DIR)

    new = os.path.join(data_dir, _LEDGER_FILENAME)
    legacy = os.path.join(_LEGACY_DATA_DIR, _LEDGER_FILENAME)
    env_set = bool(os.environ.get(_STATE_DIR_ENV, "").strip())
    if (not configured and not env_set
            and os.path.normpath(legacy) != os.path.normpath(new)
            and not os.path.exists(new)
            and os.path.isfile(legacy)):
        logger.warning(
            "using legacy caretaker restart ledger %s; move it to %s to "
            "silence this", legacy, new,
        )
        return legacy
    return new


def get_restart_ledger_path() -> str:
    """Return the absolute restart-ledger path, resolving on first use."""
    global RESTART_LEDGER_PATH
    if RESTART_LEDGER_PATH is None:
        RESTART_LEDGER_PATH = _resolve_ledger_path()
    return RESTART_LEDGER_PATH


def _tcp_reachable(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> bool:
    """Deterministic TCP connect probe. Never raises."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _host_port_from_url(url: str, default_port: int) -> tuple[str, int]:
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or default_port
        return host, port
    except Exception:
        return "127.0.0.1", default_port


class CaretakerAgent(BaseAgent):
    """Deterministic watchdog / health reporter with restart stubs."""

    name = "caretaker"
    scope = AgentScope.CARETAKER

    def __init__(self, *, secrets: SecretsProvider | None = None,
                 config=None, self_unit: str = DEFAULT_SELF_UNIT,
                 helper_path: str = SERVICE_CONTROL_HELPER) -> None:
        super().__init__(secrets=secrets)
        self._config = config
        #: The unit this process runs inside; never restarted (see
        #: :data:`SELF_RESTART_REFUSAL`). Split the Caretaker into its own unit
        #: one day and this is the single value that changes.
        self._self_unit = self_unit
        self._helper_path = helper_path

    def _cfg(self):
        if self._config is None:
            try:
                from puffpai.config import get_config
                self._config = get_config()
            except Exception as exc:
                logger.debug("caretaker config unavailable: %s", exc)
                self._config = None
        return self._config

    # ── health checks (deterministic, no LLM) ───────────────────────
    def run_health_checks(self) -> dict:
        """Probe every service and return a report. Always succeeds."""
        checks = [
            self._check_ollama(),
            self._check_chromadb(),
            self._check_api_server(),
            self._check_automation(),
            self._check_jellyfin(),
        ]
        healthy = sum(1 for c in checks if c["reachable"])
        return {
            "ok": True,
            "healthy": healthy,
            "total": len(checks),
            "checks": checks,
        }

    def _check_ollama(self) -> dict:
        cfg = self._cfg()
        url = None
        if cfg is not None:
            url = getattr(getattr(cfg, "ollama", None), "url", None)
        from puffpai.config import DEFAULT_OLLAMA_URL
        url = url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL
        host, port = _host_port_from_url(
            url, _host_port_from_url(DEFAULT_OLLAMA_URL, 11434)[1])
        return {"service": "ollama", "reachable": _tcp_reachable(host, port),
                "detail": f"{host}:{port}"}

    def _check_chromadb(self) -> dict:
        cfg = self._cfg()
        path = None
        if cfg is not None:
            paths = getattr(cfg, "paths", None)
            path = getattr(paths, "chromadb_path", None) or getattr(
                getattr(cfg, "chromadb", None), "path", None)
        reachable = bool(path) and os.path.isdir(path)
        return {"service": "chromadb", "reachable": reachable,
                "detail": "persistent path present" if reachable else "path missing"}

    def _check_api_server(self) -> dict:
        cfg = self._cfg()
        host, port = "127.0.0.1", 8000
        if cfg is not None:
            api = getattr(cfg, "api_server", None)
            host = getattr(api, "host", host) or host
            port = int(getattr(api, "port", port) or port)
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        return {"service": "api_server", "reachable": _tcp_reachable(host, port),
                "detail": f"{host}:{port}"}

    def _check_automation(self) -> dict:
        cfg = self._cfg()
        url = getattr(getattr(cfg, "nodered", None), "url", None) if cfg else None
        url = url or "http://127.0.0.1:1880"
        host, port = _host_port_from_url(url, 1880)
        return {"service": "nodered", "reachable": _tcp_reachable(host, port),
                "detail": f"{host}:{port}"}

    def _check_jellyfin(self) -> dict:
        cfg = self._cfg()
        url = None
        if cfg is not None:
            media = getattr(cfg, "media_server", None)
            url = getattr(media, "jellyfin_url", None) or getattr(media, "url", None)
        if not url:
            return {"service": "jellyfin", "reachable": False, "detail": "not configured"}
        host, port = _host_port_from_url(url, 8096)
        return {"service": "jellyfin", "reachable": _tcp_reachable(host, port),
                "detail": f"{host}:{port}"}

    # ── service control (real, narrow, rate-limited) ────────────────
    @staticmethod
    def normalize_unit(service) -> str:
        """Turn a caller's service name into a validated systemd unit name.

        Accepts ``"dashboard"`` or ``"puffpai-dashboard"``; the ``puffpai-``
        prefix is added when missing. Everything then has to satisfy
        :data:`UNIT_NAME_PATTERN`, which admits only lowercase letters, digits
        and single interior hyphens — so paths, ``.service`` suffixes, ``@``
        template instances, globs, whitespace, newlines and every shell
        metacharacter are refused here, before an argv list is even built.

        Raises:
            ServiceControlError: on anything that is not a plain unit name.
        """
        if not isinstance(service, str):
            raise ServiceControlError("service must be a string")
        unit = service.strip()
        if not unit:
            raise ServiceControlError("service is required")
        if len(unit) > MAX_UNIT_NAME_LEN:
            raise ServiceControlError("service name too long")
        unit = UNIT_ALIASES.get(unit, unit)
        if not unit.startswith("puffpai-"):
            unit = f"puffpai-{unit}"
        if not UNIT_NAME_PATTERN.match(unit):
            raise ServiceControlError(f"not a valid unit name: {service!r}")
        return unit

    def restart_service(self, service: str) -> dict:
        """Restart an allowlisted PuffPAI unit. Fail-closed at every step.

        Order matters, and every rejection happens *before* a subprocess exists:
        name validation → allowlist → self-restart refusal → rate limit →
        platform check → ``subprocess.run([...], shell=False, timeout=...)``.

        The rate-limit record is written *before* the restart runs, so an
        attempt that crashes, times out, or takes the caller down with it still
        counts against the budget. Fail-closed: if the ledger cannot be written,
        the restart does not happen, because an unenforceable rate limit is the
        same as none.

        Returns a dict; never raises for a policy refusal.
        """
        try:
            unit = self.normalize_unit(service)
        except ServiceControlError as exc:
            logger.warning("caretaker refused restart of %r: %s", service, exc)
            return {"ok": False, "refused": "invalid_unit", "service": service,
                    "error": str(exc)}

        if unit not in RESTARTABLE_UNITS:
            reason = DENIED_UNITS.get(unit) or DENIED_UNITS.get(
                unit[len("puffpai-"):] if unit.startswith("puffpai-") else unit)
            logger.warning("caretaker refused restart of non-allowlisted unit %s", unit)
            return {
                "ok": False, "refused": "not_allowlisted", "unit": unit,
                "allowlist": sorted(RESTARTABLE_UNITS),
                "error": f"{unit} is not restartable by the Caretaker"
                         + (f": {reason}" if reason else ""),
            }

        if unit == self._self_unit:
            logger.warning("caretaker refused self-restart of %s", unit)
            return {"ok": False, "refused": "self_restart", "unit": unit,
                    "error": SELF_RESTART_REFUSAL.format(unit=unit)}

        allowed, detail, history = self._rate_limit_check(unit)
        if not allowed:
            logger.warning("caretaker rate-limited restart of %s: %s", unit, detail)
            return {"ok": False, "refused": "rate_limited", "unit": unit,
                    "error": detail, "policy": self.restart_policy()}

        if not self._record_restart(unit, history):
            return {"ok": False, "refused": "ledger_unwritable", "unit": unit,
                    "error": "cannot persist the restart rate-limit ledger at "
                             f"{get_restart_ledger_path()}; refusing to restart "
                             "with an unenforceable rate limit"}

        argv = self._restart_argv(unit)
        if argv is None:
            return {"ok": False, "refused": "unsupported_platform",
                    "unit": unit, "platform": platform.system(),
                    "error": "service control is implemented for systemd/Linux "
                             "only; nothing was executed"}

        logger.warning("caretaker restarting %s via %s", unit, argv[0])
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, shell=False
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=RESTART_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "unit": unit, "argv": argv, "timeout": True,
                    "error": f"restart timed out after {RESTART_TIMEOUT_SECONDS}s"}
        except OSError as exc:
            return {"ok": False, "unit": unit, "argv": argv, "error": str(exc)}

        ok = proc.returncode == 0
        return {
            "ok": ok,
            "unit": unit,
            "argv": argv,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-2000:],
        }

    def _restart_argv(self, unit: str) -> list[str] | None:
        """Build the exact argv for *unit*, or None on an unsupported platform.

        Prefers the root-owned wrapper when it is installed; falls back to the
        sudo grant the appliance already has (``systemctl restart puffpai-*``
        NOPASSWD), so this needs no new sudoers entry to work today. ``sudo -n``
        never prompts: if the grant is absent the call fails loudly instead of
        hanging on a password read.
        """
        if platform.system() != "Linux":
            return None
        helper = self._helper_path
        if helper and os.path.isfile(helper) and os.access(helper, os.X_OK):
            return [SUDO_PATH, "-n", helper, "restart", unit]
        return [SUDO_PATH, "-n", SYSTEMCTL_PATH, "restart", unit]

    @staticmethod
    def restart_policy() -> dict:
        """The rate-limit policy, for error payloads and status reporting."""
        return {
            "min_interval_seconds": MIN_RESTART_INTERVAL_SECONDS,
            "window_seconds": RESTART_WINDOW_SECONDS,
            "max_per_unit_per_window": MAX_RESTARTS_PER_UNIT,
            "max_total_per_window": MAX_RESTARTS_GLOBAL,
        }

    # ── restart ledger (persisted, survives a process restart) ──────
    @staticmethod
    def _read_ledger() -> dict[str, list[float]]:
        """Load ``{unit: [epoch, ...]}``. A missing/corrupt file reads as empty.

        A corrupt ledger is logged and treated as no history rather than as a
        permanent refusal: refusing forever on a damaged file would be its own
        denial of service. The write in :meth:`_record_restart` still has to
        succeed, so the limit cannot be defeated by making the file unreadable.
        """
        path = get_restart_ledger_path()
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("caretaker restart ledger unreadable (%s): %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[float]] = {}
        for unit, stamps in raw.items():
            if not isinstance(unit, str) or not isinstance(stamps, list):
                continue
            vals = [float(s) for s in stamps
                    if isinstance(s, (int, float)) and not isinstance(s, bool)]
            if vals:
                out[unit] = sorted(vals)[-MAX_LEDGER_ENTRIES_PER_UNIT:]
        return out

    @staticmethod
    def _prune(history: dict[str, list[float]], now: float) -> dict[str, list[float]]:
        """Drop records older than the window.

        Timestamps in the *future* (a clock that jumped backwards) are kept and
        counted: ``now - ts`` is negative, which is still inside the window. That
        is the fail-closed reading — a backwards clock must not hand a caller a
        fresh restart budget.
        """
        pruned: dict[str, list[float]] = {}
        for unit, stamps in history.items():
            keep = [s for s in stamps if now - s < RESTART_WINDOW_SECONDS]
            if keep:
                pruned[unit] = keep[-MAX_LEDGER_ENTRIES_PER_UNIT:]
        return pruned

    def _rate_limit_check(self, unit: str) -> tuple[bool, str, dict[str, list[float]]]:
        """Return ``(allowed, detail, pruned_history)`` for restarting *unit*."""
        now = time.time()
        with _LEDGER_LOCK:
            history = self._prune(self._read_ledger(), now)
        stamps = history.get(unit, [])

        if stamps:
            since = now - max(stamps)
            if since < MIN_RESTART_INTERVAL_SECONDS:
                wait = MIN_RESTART_INTERVAL_SECONDS - since
                return False, (
                    f"{unit} was restarted {since:.0f}s ago; minimum interval is "
                    f"{MIN_RESTART_INTERVAL_SECONDS:.0f}s (retry in {wait:.0f}s)"
                ), history

        if len(stamps) >= MAX_RESTARTS_PER_UNIT:
            return False, (
                f"{unit} already restarted {len(stamps)} times in the last "
                f"{RESTART_WINDOW_SECONDS / 60:.0f} min (max "
                f"{MAX_RESTARTS_PER_UNIT}); a crash loop needs a human, not "
                "another restart"
            ), history

        total = sum(len(v) for v in history.values())
        if total >= MAX_RESTARTS_GLOBAL:
            return False, (
                f"{total} restarts across all units in the last "
                f"{RESTART_WINDOW_SECONDS / 60:.0f} min (max "
                f"{MAX_RESTARTS_GLOBAL}); the appliance is unhealthy, not one unit"
            ), history

        return True, "", history

    def _record_restart(self, unit: str, history: dict[str, list[float]]) -> bool:
        """Append a restart record and persist it. False if it cannot be saved."""
        path = get_restart_ledger_path()
        now = time.time()
        with _LEDGER_LOCK:
            # Re-read under the lock so a concurrent writer is not lost.
            merged = self._prune(self._read_ledger() or history, now)
            merged.setdefault(unit, []).append(now)
            merged[unit] = merged[unit][-MAX_LEDGER_ENTRIES_PER_UNIT:]
            tmp = f"{path}.tmp"
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(merged, fh)
                os.replace(tmp, path)
                return True
            except OSError as exc:
                logger.error("caretaker cannot write restart ledger %s: %s", path, exc)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False

    def verify_update_pack(self, path: str) -> dict:
        """STUB: signed-update-pack verification hook (Caretaker holds the key)."""
        return {
            "ok": False,
            "stub": True,
            "path": path,
            "reason": "update-pack signature verification not yet implemented "
                      "(UPDATE_VERIFICATION_KEY reserved for CARETAKER scope)",
        }

    # ── request handling ────────────────────────────────────────────
    def handle(self, envelope: Envelope) -> dict:
        intent = envelope.intent
        p = envelope.payload or {}
        # A state-changing intent on a read-only envelope never went through
        # Sentinel's veto in ``bus.send``. Fail closed. (Mirrors steward.handle.)
        if intent in STATE_CHANGING_INTENTS and not envelope.state_changing:
            return {
                "ok": False,
                "requires_review": True,
                "error": f"caretaker: {intent!r} is state-changing; the envelope "
                         "must set state_changing=True so Sentinel reviews it",
            }

        if intent in ("health", "health_check", "watchdog"):
            return self.run_health_checks()
        if intent == "restart_service":
            # Not coerced with str(): a non-string must be refused, not
            # stringified into something that might pass validation.
            return self.restart_service(p.get("service", p.get("unit", "")))
        if intent == "verify_update_pack":
            return self.verify_update_pack(str(p.get("path", "")))
        return {"ok": False, "error": f"caretaker: unknown intent {intent!r}"}

    def health(self) -> dict:
        base = super().health()
        base["llm_independent"] = True
        base["restartable_units"] = sorted(RESTARTABLE_UNITS)
        base["self_unit"] = self._self_unit
        base["restart_policy"] = self.restart_policy()
        return base


__all__ = [
    "CaretakerAgent",
    "DENIED_UNITS",
    "RESTARTABLE_UNITS",
    "STATE_CHANGING_INTENTS",
    "ServiceControlError",
    "get_restart_ledger_path",
]
