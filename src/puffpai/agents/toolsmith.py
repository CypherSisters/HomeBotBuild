"""Toolsmith — WF-7, the generic tool tier.

Every capability the team has today is a hand-coded intent: someone decided in
advance that "turn the lamp off" is a thing the house can do, and wrote the
branch. That does not scale to "check this page every morning and add it to my
list". This agent supplies the small set of *universal* tools a planner can
compose into new behaviour without anyone shipping a new intent — reach the
outside world, and keep notes between steps.

**Why this is not the Steward.** The Steward holds ``HA_TOKEN`` and the Jellyfin
key. Web content is attacker-controlled text: a page can carry instructions aimed
at whatever model reads it next. Executing that fetch inside the component that
holds the house credentials would put a prompt-injection surface and the ability
to unlock the front door in the same privilege domain. The Toolsmith is therefore
a *separate* domain — it reaches outward and holds **no** house or vault
credential at all (:data:`AgentScope.TOOLSMITH` maps to an empty allow-list, like
the Concierge). It can be talked into fetching a bad page; it cannot be talked
into opening a door, because it has never held the token that would.

Everything the Toolsmith returns from outside is stamped ``untrusted: True`` so
the layer that assembles a prompt can label it as data rather than instruction.
That label is the only honest thing this agent can say about a web page.

Four structural gates, none of them advisory:

* **Egress** — two independent consents, both required, both refusing before a
  socket opens. The owner's standing **egress door** (``web_fetch`` /
  ``web_search`` in :mod:`puffpai.security.egress_doors`, default shut, flipped
  from Settings > Egress) says whether this appliance may reach that class of
  destination at all; ``envelope.budget.allow_egress`` says whether THIS request
  was granted egress by its caller. The door is checked first — see
  :func:`_require_door` — and again at the top of :meth:`_fetch_url`, the one
  place both fetch paths reach the wire. Every HTTP hop is then charged to the
  budget with ``egress=True`` so the
  :class:`~puffpai.agents.envelope.Budget` primitive enforces the second consent
  a third time. Budgets default egress **off** and doors default shut, so the
  default is "cannot reach the network" twice over. Every hop leaves a
  content-free line in the egress ledger, allowed or refused.
* **SSRF**: an unvalidated ``announce`` URL is an SSRF primitive that
  reaches ``file:///`` and ``169.254.169.254``. The announce guard refuses by
  *name*; this one refuses by *resolved address*, on every redirect hop, so a
  public name whose A record points into the house LAN is rejected too.
* **Caller host scope** — an optional ``allowed_hosts`` payload entry the
  *caller* sets, enforced inside :meth:`_validate_url` and therefore on **every
  hop**. It exists because a ``Location:`` header is attacker-chosen content: a
  planned run whose scope permits only ``example.com``, with that host
  answering ``302 → https://attacker.example/``, would otherwise fetch the
  attacker and hand back its body. Every other check on this list passes,
  because each one asks "is this URL safe to open?" and none asks "is this a
  host the caller ever approved?". Absent means unrestricted (the pre-existing
  behaviour explicitly-budgeted callers rely on); present means enforced,
  **including an empty list, which permits nothing**.
* **Path containment** — the workspace is one directory, resolved through
  :func:`puffpai.config.resolve_state_dir` (never a package-relative path: the
  unit runs ``ProtectSystem=strict`` and a package-relative state dir fails
  *silently*). Every path is realpath'd and proven to be inside the
  realpath'd root, which is what defeats ``../``, absolute paths and symlinks in
  one check rather than three string tests.

A fifth gate arrived with ``workspace_exec``:

* **Isolation** — the generic-execution tool this module deferred for a long
  time ("sandboxed execution needs its own threat model... deferred on purpose,
  not forgotten"). That threat model is now written down, next to the code it
  governs: see the block above :data:`BWRAP`, which answers the four questions
  the deferral named — what the code may open, what it may reach, how it is
  killed, and what a compromised sandbox gets. Two properties are worth pulling
  up to here. It is **bubblewrap, not Docker**, because the service account is
  in the ``docker`` group and that group is root-equivalent: a Docker-backed
  sandbox would upgrade a prompt injection into root on the appliance. And it
  **fails closed** — no sandbox means no execution, never execution on the host.

``workspace_exec`` is the only tool here that is state-changing *and* has no
egress: ``--unshare-all`` removes the network namespace, so egress is not gated
for it, it is absent. Reaching the network still means ``web_fetch``, which is
SSRF-checked on every hop.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import shlex
import subprocess
from urllib.parse import urljoin, urlsplit

from puffpai.logging_config import get_logger
from puffpai.security import egress_doors, netaddr
from puffpai.security.secrets_provider import AgentScope, SecretsProvider
# Safe at module scope: statedir is a stdlib-only leaf that defers its
# puffpai.config import (and with it the secrets provider) to call time.
# _STATE_DIR_ENV is the env-var NAME only, and tests/test_statedir.py pins it to
# config.STATE_DIR_ENV, so reading it here cannot drift from canon.
from puffpai.statedir import _STATE_DIR_ENV, resolve_runtime_dir

from .base import BaseAgent
from .envelope import Envelope

logger = get_logger(__name__)


# ── intent classification ───────────────────────────────────────────
#
# Same contract as the Steward: declared here so the envelope *minter* can look
# up what needs ``state_changing=True``, and ``handle`` can refuse anything that
# arrived without it — i.e. anything that skipped Sentinel's veto in ``bus.send``.
STATE_CHANGING_INTENTS = frozenset({"workspace_write", "workspace_exec",
                                    "document_convert"})

#: Intents that leave the appliance. Kept separate from the state-changing set
#: because they are orthogonal questions: a fetch mutates nothing in the house
#: and still must not happen without an egress-carrying budget. ``web_search``
#: is here too: it reaches SearXNG, so it must ride an egress-carrying budget
#: exactly like ``web_fetch``, even though the host it talks to is local.
#: ``search_and_read`` is a fetch AND a search fused into one step, so it belongs
#: here for the same reason both halves do.
EGRESS_INTENTS = frozenset({"web_fetch", "web_search", "search_and_read"})

#: Everything this agent can do. Anything not here is an unknown intent.
TOOL_INTENTS = frozenset({"web_fetch", "web_search", "search_and_read",
                          "workspace_read", "workspace_write", "workspace_list",
                          "workspace_exec", "document_convert",
                          "registry_read"})


# ── web_fetch policy ────────────────────────────────────────────────

#: Only these two schemes ever open a socket. ``file``, ``data``, ``gopher``,
#: ``ftp``, ``javascript`` are refused outright — ``file:///etc/shadow`` is the
#: exact bug this agent exists not to repeat.
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

#: Ports a fetch may use. The address checks below are the real gate, so this is
#: belt-and-braces against pivoting at an unusual port on a public host; widen it
#: here if a build genuinely needs to read an API on another port.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})

MAX_URL_LEN = 2048
#: Redirect hops. Each one is re-validated from scratch, and each one is charged
#: to the budget as its own egress action.
MAX_REDIRECTS = 3
#: Entries a caller's ``allowed_hosts`` may carry. Bounds the work done per hop
#: and the size of the refusal message; a caller needing more than this is not
#: expressing a scope.
MAX_ALLOWED_HOSTS = 64
#: Raw bytes read off the wire before the connection is dropped, whatever the
#: server claims in Content-Length. A hostile endpoint cannot stream us to death.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: Extracted-text ceilings.
DEFAULT_MAX_CHARS = 20_000
HARD_MAX_CHARS = 100_000
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 30.0

#: Content types worth extracting text from. Anything else (images, archives,
#: ``application/octet-stream``) is refused rather than fed to a text extractor
#: that would emit binary noise into a prompt.
ALLOWED_CONTENT_PREFIXES = ("text/",)
ALLOWED_CONTENT_TYPES = frozenset({
    "application/xhtml+xml", "application/xml", "application/json",
    "application/rss+xml", "application/atom+xml", "application/javascript",
})

#: Name forms that always resolve inside the house. Refused by name as well as
#: by address, so a resolver that answers oddly cannot open a hole.
LOCAL_HOST_NAMES = frozenset({"localhost", "localhost.localdomain"})
LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa", ".lan")

#: A final DNS label that is really a number — decimal, octal (leading zero) or
#: hex. ``http://127.1/``, ``http://2130706433/`` and ``http://0x7f000001/`` are
#: all loopback to a permissive HTTP client. Most resolvers would hand these back
#: as loopback and the address check would catch them; refusing by shape as well
#: means the refusal does not depend on the resolver behaving.
_NUMERIC_LABEL_RE = re.compile(r"\A(?:0[xX][0-9a-fA-F]+|[0-9]+)\Z")

_UA = "PuffPAI-Toolsmith/1.0 (+local appliance; text extraction only)"


# ── web_search policy (SearXNG pinhole) ─────────────────────────────
#
# ``web_search`` reaches a SELF-HOSTED SearXNG instance the owner runs — by
# default at loopback, which :func:`reject_internal_address` deliberately blocks
# for ``web_fetch``. That is not a contradiction: the SSRF gate exists to stop a
# *user-supplied* URL from reaching the LAN, and the SearXNG target is not
# user-supplied at all. It is read from ``tools.searxng_url`` (owner config),
# there is no payload field that selects the host, redirects are not followed,
# and no code below relaxes the SSRF gate for anything else. The result URLs
# this returns are DATA, never fetched here; when the planner reads one it goes
# back through the full, unmodified ``web_fetch`` gate. So the one pinhole is:
# one method, one owner-fixed host, no redirects — and everything reachable via
# a search result still faces the complete SSRF checks.

#: The JSON search endpoint SearXNG exposes.
SEARXNG_SEARCH_PATH = "/search"
#: Fallback base if config cannot be read at all (matches ToolsConfig default).
_DEFAULT_SEARXNG_URL = "http://127.0.0.1:8888"

#: **The one loopback endpoint this module may reach, as `host:port`.**
#:
#: Every other outbound request the Toolsmith makes goes through a session
#: pinned to a validated public address (:func:`puffpai.security.netaddr.pinned_session`),
#: which refuses loopback. That is correct, but it would break ``web_search``,
#: because the documented SearXNG pinhole IS loopback.
#:
#: The allowance is this constant and nothing else. Not "loopback is fine here":
#: one address, one port, compared exactly. ``127.0.0.1:9999`` is refused,
#: ``127.0.0.1:11434`` (ollama) is refused, ``127.0.0.1:8000`` (this appliance's
#: own API) is refused. Derived from :data:`_DEFAULT_SEARXNG_URL` so the two
#: cannot drift apart.
#:
#: Pinning buys nothing here and is not missed: the target is a literal address,
#: so there is no name, no DNS answer and no rebinding window to close. What
#: makes this safe is that the endpoint is fixed in code, not that the socket is
#: pinned.
SEARXNG_LOOPBACK_ENDPOINT = _DEFAULT_SEARXNG_URL.split("//", 1)[1]
#: Results returned when the caller names no ``max_results``.
DEFAULT_SEARCH_RESULTS = 5
#: Ceiling on results, whatever the caller asks: a search is a lead-in to a
#: fetch, not a bulk crawl.
MAX_SEARCH_RESULTS = 10
#: Longest query accepted. A query longer than this is a mistake, not a search.
MAX_SEARCH_QUERY_LEN = 512
#: Bytes read from a SearXNG JSON reply before the connection is dropped. The
#: local instance is trusted but not infinitely so — a wedged process must not
#: stream the appliance to death.
MAX_SEARCH_BYTES = 2 * 1024 * 1024
#: Per-field text caps for the title/snippet that ride back into a prompt.
SEARCH_TITLE_MAX = 300
SEARCH_SNIPPET_MAX = 500
#: Fallback timeout if config is unreadable. Bounded, so search still degrades
#: rather than hangs. Kept in step with ToolsConfig.searxng_timeout's default:
#: a real SearXNG relay can return a full ~19KB result set at ~10s, so a 15s
#: ceiling would clamp the 20s config default back down to a value too tight
#: for that same call.
DEFAULT_SEARCH_TIMEOUT = 20.0
MAX_SEARCH_TIMEOUT = 25.0


# ── workspace policy ────────────────────────────────────────────────

#: Repo root — four directory levels up from ``src/puffpai/agents/toolsmith.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: Canonical default: the repo-root ``data`` directory, shared with
#: agent_events, caretaker, consent_gate and zone3/policy_engine, rather than
#: the package-relative ``src/puffpai/data``, for convergence. Still only
#: the *fallback*: ``<repo>/data`` is inside the checkout and equally read-only
#: under ``ProtectSystem=strict``, so a deployment MUST set ``paths.state_dir``
#: (absolute) or ``$PUFFPAI_DATA_DIR``.
#: See :func:`puffpai.config.resolve_state_dir`.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

#: The legacy default, ``src/puffpai/data``. Nothing is created here any
#: more; a workspace an earlier release left behind is adopted IN PLACE by
#: :func:`_resolve_workspace_root` — never copied, never moved — so a user's
#: tool output does not vanish from ``workspace_list`` after an upgrade.
_LEGACY_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

#: Directory name under the state dir. One root, so containment is one check.
WORKSPACE_DIRNAME = "workspace"


def _require_door(door_id: str) -> None:
    """The household's standing consent for this class of egress. No socket.

    Two gates guard every outbound call this agent makes, and they answer
    different questions:

    * **this one** — has the owner consented to this appliance reaching that
      class of destination *at all*? It is a standing setting, resolved from
      ``egress_doors`` (``env > owner overlay > config > default-shut``) and
      flipped from Settings > Egress;
    * ``Budget.allow_egress`` — was *this particular request* granted egress by
      its caller? It defaults False and is inherited, never minted, so a
      planned run cannot widen its own reach.

    Neither substitutes for the other, and both refuse before any socket
    opens. Checked FIRST — before the URL is even shape-checked — so a shut
    door produces the honest "[Egress door closed] …" sentence naming the
    switch, rather than a URL-validation error that tells the owner nothing
    about why their appliance would not go online.

    Fails closed on anything unexpected: a registry that will not import or
    resolve is not consent.
    """
    try:
        egress_doors.require_open(door_id)
    except egress_doors.EgressBlocked as blocked:
        raise EgressDenied(
            f"[Egress door closed] {blocked.user_message}") from blocked
    except Exception as exc:  # a broken registry must not read as "open"
        logger.warning("toolsmith: egress door %s unreadable: %s", door_id, exc)
        raise EgressDenied(
            f"[Egress door closed] The '{door_id}' egress door could not be "
            f"read, so I am treating it as shut.") from exc


def _is_searxng_loopback(base: str) -> bool:
    """Is *base* exactly :data:`SEARXNG_LOOPBACK_ENDPOINT`?

    Compared as ``host:port`` against the constant, with the port defaulted from
    the scheme — never "does this look local", never a prefix or suffix test.
    ``127.0.0.1:8888`` is the allowance; ``127.0.0.1:8000`` (this appliance's own
    API), ``127.0.0.1:11434`` (the model runtime) and ``localhost:8888`` are not,
    and neither is any name that might resolve to loopback, because a name is
    exactly the thing an allowance keyed to an address must not accept.
    """
    try:
        parts = urlsplit(str(base).strip())
    except ValueError:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None:
        port = 443 if scheme == "https" else 80
    host = (parts.hostname or "").strip().rstrip(".")
    return f"{host}:{port}" == SEARXNG_LOOPBACK_ENDPOINT


#: What the ledger writes instead of an internal address. A refusal is worth
#: recording; WHICH machine on the household's network someone tried to reach
#: is not, and a security log is the last place it should appear.
_REDACTED_HOST = "<internal-address>"


def _ledger_host(host: str) -> "tuple[str, str]":
    """``(host_for_the_ledger, address_class)`` for one URL's hostname.

    A hostname is recorded as it stands: it is the destination, and naming it is
    the whole point of the trail. An IP LITERAL is different, and splits in two.

    A public literal is still a destination the owner should be able to see, so
    it is recorded. A literal that is loopback, RFC 1918, link-local or any
    other internal class is replaced by :data:`_REDACTED_HOST`, and only its
    CLASS is kept. That is the residual risk this closes: ``fetch
    http://192.168.1.1/`` would otherwise leave the household's own router
    address sitting in ``security_logs/egress.jsonl``, and "the appliance
    refused a request to a private address" is the entire fact the owner
    needs.
    """
    if not host:
        return "", ""
    try:
        address_class = netaddr.classify(host)
    except netaddr.UnparseableAddress:
        return host[:200], ""   # an ordinary hostname
    if address_class is None:
        return host[:200], ""   # a public literal: a real destination
    return _REDACTED_HOST, address_class


def _fetch_ledger(url: str, ok: bool, *, detail: str = "",
                  status: int | None = None,
                  bytes_in: int | None = None,
                  reason_code: str = "",
                  address_class: str = "") -> None:
    """One content-free line in the egress ledger for one fetch hop.

    ``egress_request`` writes these for the doors it wraps; the Toolsmith opens
    its own sockets, so without this the ``web_fetch`` door would refuse
    loudly and succeed silently — a trail that only ever shows the appliance
    behaving is not a trail.

    **The query string is dropped**, deliberately and by construction:
    ``urlsplit().path`` excludes it, and on a household box a query string is
    exactly where a search term, a document name or a location would be.

    **No resolved or internal address is recorded, on any path.** The refusal
    messages this module raises name the host and the class and never the
    address it resolved to (:class:`puffpai.security.netaddr.AddressRefused`),
    and an internal literal in the URL itself is redacted to its class by
    :func:`_ledger_host`. Host, path, status, byte counts, a reason code and an
    address class are what is recorded — never a body, a header, a query or an
    address. Never raises: a ledger fault must not fail a fetch.
    """
    try:
        host, path = "", ""
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            path = (parts.path or "/")[:200]
        except Exception:
            pass
        raw_host, host = host, ""
        host, literal_class = _ledger_host(raw_host)
        if literal_class and raw_host:
            # The host was redacted, so the reason must not put it back. A
            # refusal for an internal LITERAL names that literal in its message
            # by design — "127.0.0.1 is loopback" is the readable refusal and
            # the caller typed it — but repeating it in the trail one field
            # over would make the redaction beside it decorative.
            detail = detail.replace(raw_host, _REDACTED_HOST)
        egress_doors.record_external("web_fetch", ok, detail=detail[:200],
                                     host=host, path=path, status=status,
                                     bytes_in=bytes_in,
                                     reason_code=reason_code,
                                     address_class=(address_class
                                                    or literal_class))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("toolsmith: egress ledger write failed: %s", exc)


def _resolve_workspace_root() -> str:
    """Absolute sandbox root, resolved lazily through the shared state helper.

    Never a package-relative path: the unit runs ``ProtectSystem=strict`` with
    ``ReadWritePaths=/data/puffpai``, so a package-relative state dir is not
    merely wrong, it fails *silently*: writes are lost with no error.

    The CONFIGURED tier stays a direct :func:`~puffpai.config.resolve_state_dir`
    call, because this module honours ``paths.state_dir`` and
    :func:`~puffpai.statedir.resolve_runtime_dir` always passes
    ``configured=None``. The degraded tier below it is the leaf. Reading
    ``$PUFFPAI_DATA_DIR`` RAW (no strip, no ``~`` expansion, no repo-root join
    for a relative value) would matter more here than anywhere else: the
    caller ``os.makedirs`` the answer, so a relative value (the documented
    ``"data"``) would not merely mis-resolve, it would CREATE a second sandbox
    under whatever directory the service started in. A workspace root is also the
    anchor of the ``realpath``-prefix containment check in :meth:`_safe_path`,
    so "which root" is a security-relevant answer, not a tidiness one.

    Legacy adoption probes the workspace directory ITSELF, not the parent data
    dir: ``<repo>/data`` exists in every checkout, so the canon's
    directory-level ``legacy=`` would never fall through to the legacy
    ``src/puffpai/data``. It fires only when nothing else decided the location
    — no configured path, no ``$PUFFPAI_DATA_DIR``, and no workspace at the new
    path — and it only points the sandbox at the old directory in place;
    nothing is copied or moved.

    Both suppressors test for an EXPLICIT CHOICE, not for a moved directory. An
    operator who points ``$PUFFPAI_DATA_DIR`` at the default data dir (absolute
    ``<repo>/data``, or the relative value ``"data"``, which joins to the repo
    root) resolves to the shipped directory: comparing paths would read that as
    "unset" and adopt, putting the sandbox back in the read-only code tree with
    an env var explicitly saying otherwise.
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
    except Exception as exc:  # config must never break a tool
        logger.warning("config unavailable for toolsmith workspace: %s", exc)
        configured = ""
        data_dir = resolve_runtime_dir(_DEFAULT_DATA_DIR)

    new = os.path.join(data_dir, WORKSPACE_DIRNAME)
    legacy = os.path.join(_LEGACY_DATA_DIR, WORKSPACE_DIRNAME)
    env_set = bool(os.environ.get(_STATE_DIR_ENV, "").strip())
    if (not configured and not env_set
            and os.path.normpath(legacy) != os.path.normpath(new)
            and not os.path.exists(new)
            and os.path.isdir(legacy)):
        logger.warning(
            "using legacy toolsmith workspace %s; move it to %s to silence this",
            legacy, new,
        )
        return os.path.realpath(legacy)
    return os.path.realpath(new)


MAX_FILE_BYTES = 1024 * 1024
MAX_WORKSPACE_FILES = 500
MAX_REL_PATH_LEN = 255
MAX_PATH_DEPTH = 8
MAX_LIST_ENTRIES = 500


# ── workspace_exec policy ───────────────────────────────────────────
#
# THREAT MODEL (the one the module docstring deferred; see "Deliberately not
# implemented: run_code"). Answering its four questions explicitly:
#
# *What may the code open?*  Only the workspace. The sandbox root is built from
# scratch — read-only binds of the interpreter/runtime, a private ``/tmp``, and
# a single read-write bind of the Toolsmith workspace at :data:`EXEC_WORKDIR`.
# ``/etc``, ``/opt``, ``/data``, ``$HOME`` and every house credential simply do
# not exist inside. Verified on the appliance: ``/etc/shadow``, ``/opt`` and
# ``/data`` are all "No such file or directory" from inside the jail.
#
# *What may it reach?*  Nothing. ``--unshare-all`` includes the network
# namespace, so there is no interface to talk to and no DNS to resolve with.
# This is why ``workspace_exec`` is deliberately NOT in :data:`EGRESS_INTENTS`:
# egress is not gated here, it is *absent*. Code that wants the network must go
# through ``web_fetch``, which is SSRF-checked hop by hop.
#
# *How is it killed?*  A wall-clock timeout kills the process group, and
# ``--die-with-parent`` means a sandbox cannot outlive the agent even if the
# timeout path is skipped. Output is capped, so a process that cannot be starved
# of time can still not be used to exhaust memory through a pipe.
#
# *What does a compromised sandbox reach?*  The workspace, and nothing else.
# That is the whole point of the next paragraph.
#
# WHY NOT DOCKER. Docker is the obvious backend and it is the wrong one *on this
# appliance*: the service account is in the ``docker`` group, and Docker group
# membership is root-equivalent — anything that can reach the socket can run
# ``docker run -v /:/host --privileged`` and own the host. Handing this agent a
# Docker-backed "sandbox" would therefore convert a prompt-injection foothold
# into root, which is strictly worse than having no exec tool at all. Bubblewrap
# needs no daemon, no socket and no group membership that grants privilege: it
# is unprivileged (``/usr/bin/bwrap`` is NOT setuid here, so it uses unprivileged
# user namespaces and stays compatible with the unit's ``NoNewPrivileges=true``).
# The Docker socket is not bound into the jail, so the group membership the
# service happens to hold is unreachable from inside.
#
# FAIL CLOSED. If ``bwrap`` is missing or the jail will not start, this tool
# refuses. It never degrades to running the command on the host — an isolation
# failure must cost a capability, never silently cost the isolation.

#: The sandbox helper. Absolute on purpose: resolving via ``$PATH`` would let a
#: poisoned environment choose the "sandbox" that is supposed to contain it.
BWRAP = "/usr/bin/bwrap"

#: Where the workspace is mounted inside the jail, and the command's cwd.
EXEC_WORKDIR = "/work"

#: Read-only binds that make up the runtime. Deliberately does NOT include
#: ``/etc``: nothing here needs it, and it is where the interesting secrets are.
#: Missing sources are skipped rather than fatal — ``/lib64`` does not exist on
#: this appliance's arm64 userland, and a hard-coded bind list that assumes it
#: would make the tool refuse everything on the very platform it ships on.
EXEC_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")

EXEC_DEFAULT_TIMEOUT = 30.0
#: Ceiling for both sandboxed tools. Above the document default, so a slow
#: first-run LibreOffice conversion is not clamped to under its own budget.
EXEC_MAX_TIMEOUT = 300.0
#: Output cap. Truncated rather than refused: a command that legitimately prints
#: a lot should still return its first page of output.
EXEC_MAX_OUTPUT_BYTES = 256 * 1024


# ── document_convert policy ─────────────────────────────────────────
#
# Documents get their OWN jail rather than widening the exec jail. LibreOffice
# needs a handful of ``/etc`` entries that a shell command has no business
# seeing, and the cheapest way to keep ``workspace_exec`` minimal is to not make
# it carry LibreOffice's dependencies. Two tools, two bind sets, same jail
# builder.
#
# Each added path was proven necessary by bisection on the appliance, and each
# is configuration rather than secret: font config, LibreOffice's own config,
# the *public* account table (``/etc/passwd`` — hashes live in ``/etc/shadow``,
# which is NOT bound and stays unreachable), the NSS and loader configs, and the
# timezone. Whole-``/etc`` also works and is deliberately not used.
DOC_RO_BINDS = ("/etc/fonts", "/etc/libreoffice", "/etc/passwd",
                "/etc/nsswitch.conf", "/etc/ld.so.cache", "/etc/localtime")

#: LibreOffice keeps a per-user profile and will sulk without a writable one.
#: Pointed inside the workspace so it is bounded by the same containment as
#: everything else, and so a wedged profile can be deleted like any other file.
DOC_PROFILE_DIRNAME = ".loprofile"

#: Formats the caller may convert *to*. An allow-list because ``--convert-to``
#: takes a filter name, and an unchecked one is a string the caller chose that
#: LibreOffice will happily interpret.
DOC_TARGET_FORMATS = frozenset({
    "docx", "odt", "pdf", "txt", "rtf", "html",
    "xlsx", "ods", "csv", "pptx", "odp",
})

#: LibreOffice is slow to start, and slower on its first run (it builds the
#: profile). This is a conversion budget, not a shell budget.
DOC_DEFAULT_TIMEOUT = 120.0


# ── registry_read policy (the legacy-tool bridge) ───────────────────
#
# WHY THIS EXISTS. The planner can only compose over ``CATALOGUE``, which holds
# (agent, intent) pairs the BUS can dispatch. But ~134 of this appliance's tools
# are LangChain tools in ``assistant_tools`` — ``news.headlines`` is served by
# ``auto_news_summary``, not by a majordomo intent — so a plan step could not
# reach them at all. That is why "add a NASA news feed" produced a menu instead
# of a composition: web_search worked, the feed-add accepted a URL, and nothing
# could put the two together.
#
# Audited against every dispatchable intent, the catalogue has nothing safe
# left to add, so this gap cannot be closed with more entries. One
# read-only bridge closes it instead of ~100 hand-written pairs.
#
# WHY A GENERIC DISPATCHER IS ACCEPTABLE HERE. Only because it is boxed in on
# four sides, each enforced in code and tested:
#
#   1. READ only. Anything the registry marks WRITE or READWRITE is refused.
#      A read can return the wrong answer; it cannot actuate the house.
#   2. Information domain only — the owning agent must be one of
#      :data:`BRIDGE_AGENTS`. Never steward, sentinel or caretaker.
#   3. The vault is denied BY ID, not by access level, so a future edit that
#      flips a vault capability to READ cannot quietly open it. Zone-1 keeps its
#      own PIN-gated consent path.
#   4. The tool name must be one the REGISTRY declares. A name the model
#      invented is refused before anything is looked up, so this is not a
#      free-form call into the process.
#
# It is deliberately NOT state-changing: a state-changing bridge would be a way
# to reach an actuator without the Steward's entity resolution or Drawbridge's
# review, which is exactly the "route around a denial" the planner's contract
# tests exist to prevent.

#: Agents whose read-only capabilities the bridge may reach.
BRIDGE_AGENTS = frozenset({"toolsmith", "archivist", "majordomo"})

#: Denied by id regardless of declared access. Zone-1 is PIN-gated and owns its
#: own consent flow; it must never be reachable through a generic dispatcher.
BRIDGE_DENIED_CAPS = frozenset({"vault.secrets", "vault.display"})

#: The bridge may not invoke itself, directly or as a chain.
BRIDGE_SELF_CAP = "utility.registry_read"

#: Registry access level that alone qualifies a capability.
BRIDGE_READ = "read"

#: Bridged tool output is model-facing text; cap it like any other read.
BRIDGE_MAX_OUTPUT = 20_000

#: Path components that are never a legitimate relative path inside a sandbox.
#: The realpath containment check below is the authority; these are refused
#: early so the *reason* returned to a caller says what was wrong.
_REJECTED_COMPONENTS = frozenset({"", ".", ".."})

#: Windows reserved device names — writing ``NUL`` or ``COM1`` opens a device,
#: not a file, and this repo already carries a stray ``NUL`` from exactly that.
_WINDOWS_DEVICES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


class ToolsmithInputError(ValueError):
    """Bad payload — rejected before any socket opens or any file is touched."""


class EgressDenied(PermissionError):
    """A tool that must leave the appliance ran on a budget that forbids it."""


# ── HTML → text (dependency-free) ───────────────────────────────────
#
# Deliberately not a parser. A parser would be better at fidelity and worse at
# safety review: this is ~15 lines whose failure mode is "kept too much text",
# which the untrusted label already covers. What it must not do is emit script
# bodies or CSS into something a model will read as prose.

_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_DROP_ELEMENT_RE = re.compile(
    r"(?is)<(script|style|noscript|template|svg|canvas|head)\b[^>]*>.*?</\1\s*>")
_UNCLOSED_DROP_RE = re.compile(
    r"(?is)<(script|style)\b[^>]*>.*\Z")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_BLOCK_BREAK_RE = re.compile(
    r"(?is)</(?:p|div|li|tr|section|article|h[1-6]|blockquote|pre)\s*>"
    r"|<(?:br|hr)\s*/?>")
_TAG_RE = re.compile(r"(?s)<[^>]*>")
#: Any horizontal whitespace, including the non-breaking space HTML is full of.
_SPACES_RE = re.compile("[^\\S\\n]+")
_BLANKS_RE = re.compile(r"\n\s*\n\s*\n+")


def extract_text(body: str, content_type: str = "") -> str:
    """Best-effort readable text from a fetched body.

    Non-HTML bodies (JSON, plain text) come back whitespace-normalised only —
    reformatting them would corrupt data the caller asked for verbatim.
    """
    lowered = (content_type or "").lower()
    is_markup = ("html" in lowered or "xml" in lowered
                 or bool(re.search(r"(?i)<\s*(?:html|body|div|p)\b", body[:4096])))
    if not is_markup:
        return _normalise_whitespace(body)

    title_match = _TITLE_RE.search(body)
    title = _normalise_whitespace(_TAG_RE.sub(" ", title_match.group(1))) \
        if title_match else ""

    text = _COMMENT_RE.sub(" ", body)
    text = _DROP_ELEMENT_RE.sub(" ", text)
    # A truncated body can leave a <script> open; everything after it is code,
    # not prose, so drop the tail rather than let it through detagged.
    text = _UNCLOSED_DROP_RE.sub(" ", text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _normalise_whitespace(text)
    if title and not text.startswith(title):
        text = f"{title}\n\n{text}" if text else title
    return text


def _normalise_whitespace(text: str) -> str:
    """Collapse runs of spaces/blank lines and drop non-printables.

    Control characters are stripped because this text ends up in a prompt and in
    the event log; an ANSI escape or a lone ``\\r`` there is a rendering bug at
    best and log injection at worst.
    """
    cleaned = "".join(
        ch if (ch.isprintable() or ch in "\n\t") else " " for ch in text)
    cleaned = cleaned.replace("\t", " ")
    cleaned = _SPACES_RE.sub(" ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines())
    return _BLANKS_RE.sub("\n\n", cleaned).strip()


# ── address policy ──────────────────────────────────────────────────

#: Ledger ``reason_code`` for "this fetch was refused because its host is, or
#: resolves to, an address this appliance may not open a socket to". Same code
#: the egress-door registry writes (``egress_doors.REASON_RESOLVED_PRIVATE``),
#: because an owner reading the trail should not have to learn that the two
#: halves of the same control spell the same refusal differently.
_REASON_RESOLVED_PRIVATE = egress_doors.REASON_RESOLVED_PRIVATE


def reject_internal_address(text: str, *, host: str = "") -> None:
    """Refuse any address that is not a globally routable public one.

    The policy lives in :mod:`puffpai.security.netaddr` and is shared with the
    egress-door registry, which needed exactly this check and did not have it
    (a door whose hostname comes from a manifest is a door whose author picks
    the address behind it). This function is the Toolsmith's spelling of it: it
    keeps the ``web_fetch:`` prefix and the exact wording its own refusals have
    used since the SSRF hardening landed, because those strings are what the
    tool hands back to a caller and what its test battery asserts on.

    Checked with :mod:`ipaddress` on the *resolved* address rather than against a
    list of strings, because a blocklist of ``127.0.0.1``/``10.*`` never covers
    ``0.0.0.0``, ``[::1]``, ``100.64.0.0/10`` or the next range someone invents.
    ``is_global`` is the positive test; loopback/private/link-local/multicast are
    named individually only so the refusal explains itself.
    """
    try:
        netaddr.check(text, posture=netaddr.PUBLIC, host=host)
    except netaddr.UnparseableAddress:
        raise ToolsmithInputError(
            f"web_fetch: {text!r} is not an address this can check") from None
    except netaddr.AddressRefused as refused:
        # ``str(refused)`` names the HOST and the class, never the address it
        # resolved to — see netaddr.AddressRefused. That matters here more than
        # anywhere: this string is returned to the caller AND written into the
        # egress ledger by _fetch_ledger, and the ledger must not be where a
        # household's internal addressing gets written down.
        error = ToolsmithInputError(f"web_fetch: {refused}")
        # Carried so the ledger can record WHY in a closed vocabulary instead of
        # grepping a sentence.
        error.reason_code = _REASON_RESOLVED_PRIVATE
        error.address_class = refused.address_class
        raise error from None


def _default_resolver(host: str, port: int) -> list[str]:
    """Every address *host* resolves to. Injected in tests; never called there."""
    try:
        return netaddr.default_resolver(host, port)
    except netaddr.ResolutionError as exc:
        raise ToolsmithInputError(f"web_fetch: {exc}") from None


class ToolsmithAgent(BaseAgent):
    """Universal composable tools: reach the web, keep notes. No house keys.

    Args:
        secrets: shared provider (scope enforcement is identical either way).
        http: transport with ``.get(url, headers=, timeout=, allow_redirects=,
            stream=)``. Defaults to :mod:`requests`; injected by tests so every
            fetch path — including the SSRF refusals — runs with no network.
        resolver: ``(host, port) -> [address, ...]``. Injected so a test can
            prove that a *name* resolving into the LAN is refused without owning
            a domain that does.
        workspace_root: absolute sandbox root. Injected by tests; otherwise
            resolved through :func:`puffpai.config.resolve_state_dir`.
    """

    name = "toolsmith"
    scope = AgentScope.TOOLSMITH

    def __init__(self, *, secrets: SecretsProvider | None = None,
                 http=None, resolver=None, workspace_root: str | None = None,
                 searxng_url: str | None = None,
                 searxng_timeout: float | None = None,
                 tool_source: dict | None = None) -> None:
        super().__init__(secrets=secrets)
        #: The INJECTED transport, or None. Written here and nowhere
        #: else -- see the transports section below for why that matters.
        self._http = http
        #: Lazily-built caches, in their own slots so that neither can be
        #: mistaken for an injection nor handed to the other's call site.
        self._http_pinned = None
        self._http_search = None
        self._resolve = resolver or _default_resolver
        self._workspace_root = (os.path.realpath(workspace_root)
                                if workspace_root else None)
        # Injected by tests so ``web_search`` needs no config and no real
        # SearXNG; otherwise both are read from ``tools.*`` at call time.
        self._searxng_url = searxng_url
        self._searxng_timeout = searxng_timeout
        # name -> LangChain tool, injected by tests so the bridge needs no
        # api_server and no live model. None means "ask the running process".
        self._tool_source = tool_source

    # ── request handling ────────────────────────────────────────────
    def handle(self, envelope: Envelope) -> dict:
        intent = envelope.intent
        p = envelope.payload or {}

        # A state-changing intent that arrived on a read-only envelope never went
        # through Sentinel's veto in ``bus.send``. Fail closed. (Mirrors the
        # identical check at the top of ``steward.handle``.)
        if intent in STATE_CHANGING_INTENTS and not envelope.state_changing:
            return {
                "ok": False,
                "requires_review": True,
                "error": f"toolsmith: {intent!r} is state-changing; the envelope "
                         "must set state_changing=True so Drawbridge reviews it",
            }

        try:
            if intent == "web_fetch":
                return self._web_fetch(envelope, p)
            if intent == "web_search":
                return self._web_search(envelope, p)
            if intent == "search_and_read":
                return self._search_and_read(envelope, p)
            if intent == "workspace_write":
                return self._workspace_write(envelope, p)
            if intent == "workspace_read":
                return self._workspace_read(envelope, p)
            if intent == "workspace_list":
                return self._workspace_list(envelope, p)
            if intent == "workspace_exec":
                return self._workspace_exec(envelope, p)
            if intent == "document_convert":
                return self._document_convert(envelope, p)
            if intent == "registry_read":
                return self._registry_read(envelope, p)
            return {"ok": False, "error": f"toolsmith: unknown intent {intent!r}"}
        except EgressDenied as exc:
            return {"ok": False, "egress_denied": True,
                    "error": f"toolsmith: {exc}"}
        except KeyError as exc:
            return {"ok": False, "error": f"toolsmith: missing payload key {exc}"}
        except ToolsmithInputError as exc:
            return {"ok": False, "error": f"toolsmith: {exc}"}
        except OSError as exc:
            # Disk full, permission denied, a workspace that will not create.
            # Reported, never raised: a failing tool must not abort the bus.
            logger.warning("toolsmith %s failed: %s", intent, exc)
            return {"ok": False, "error": f"toolsmith: {intent} failed ({exc})"}

    # ── web_fetch ───────────────────────────────────────────────────
    def _web_fetch(self, envelope: Envelope, p: dict) -> dict:
        """Fetch a public URL and return its **text**, marked untrusted.

        The order of operations is the security property: every check that can
        refuse does so before a socket opens, and the checks that can only be
        made per-hop are re-run on every hop rather than once at the start.

        0. the owner's ``web_fetch`` egress door must be open
           (:func:`_require_door`) — the household's standing consent, checked
           before anything else so a shut door says so plainly;
        1. the budget must carry egress at all (:class:`EgressDenied`);
        2. the URL is validated — scheme, credentials, length, printability;
        3. the host must be in the caller's ``allowed_hosts``, when the caller
           supplied one. This is a *scope* check, not a safety check: the caller
           is saying which sites this particular request was ever authorised to
           read. It is separate from everything else here because a redirect
           target is chosen by the server, so the approval that covered hop one
           says nothing whatever about hop three — which is exactly how a run
           scoped to ``example.com`` was made to fetch ``attacker.example`` and
           hand back its body;
        4. the host is resolved and **every** resolved address must be globally
           routable (:func:`reject_internal_address`);
        5. the hop is charged to the budget as an egress action;
        6. the response's real peer address is re-checked where the transport
           exposes it, which is what narrows the DNS-rebinding window between
           our resolution and the client's;
        7. a redirect repeats 2-6 from scratch, at most :data:`MAX_REDIRECTS`
           times — a redirect to ``http://169.254.169.254/`` is the classic way
           past a validator that only ever looked at the URL it was handed.

        Returns raw *text*, not HTML: the caller gets prose, not a document that
        something downstream might render. ``untrusted: True`` is always set.
        """
        # The owner's switch, before the caller's grant and before the URL is
        # even shape-checked. _fetch_url re-checks it as the single home for
        # the wire path; this call is here so the refusal names the door rather
        # than failing on a malformed URL first.
        _require_door("web_fetch")

        budget = getattr(envelope, "budget", None)
        if budget is None or not getattr(budget, "allow_egress", False):
            # Explicit and legible. ``budget.consume(egress=True)`` below would
            # also stop this, and deliberately still runs — one gate that returns
            # a good error, one primitive that cannot be edited out of the path.
            raise EgressDenied(
                "web_fetch needs an envelope whose budget sets allow_egress=True; "
                "budgets default to no egress")

        url = self._clean_url(p.get("url"))
        # Read once, before the loop, so every hop is judged against the scope
        # the CALLER set rather than anything a response could influence.
        allowed_hosts = self._allowed_hosts(p.get("allowed_hosts"))
        max_chars = self._max_chars(p.get("max_chars"))
        timeout = self._timeout(p.get("timeout"))
        return self._fetch_url(url, allowed_hosts, budget, max_chars, timeout)

    def _fetch_url(self, url: str, allowed_hosts, budget, max_chars: int,
                   timeout: float) -> dict:
        """The redirect-following hop loop, extracted so it has exactly ONE home.

        ``_web_fetch`` and ``_search_and_read`` both reach the wire through here,
        so the SSRF property — scheme/credential/port validation, per-hop
        resolved-address checks, peer re-verification, redirect re-validation and
        the byte cap — is single-sourced and cannot drift between the two callers.

        *allowed_hosts* is the caller's host scope (see :meth:`_allowed_hosts`):
        ``None`` means unrestricted, and only the SCOPE check is skipped by it —
        every unconditional gate below still runs. ``_search_and_read`` passes
        ``None`` on purpose: its URL is DISCOVERED by the search, not pre-sealed,
        and it is safe precisely because these gates run regardless of scope.

        *url* must already be shape-checked (:meth:`_clean_url`); this loop
        re-cleans only the redirect targets it derives itself.

        The ``web_fetch`` door is checked HERE, not only in :meth:`_web_fetch`,
        because this is the one place both fetch paths reach the wire — so a
        future third caller cannot acquire an ungated fetch by calling the
        helper instead of the intent. It is the first statement in the hop
        loop, ahead of :meth:`_validate_url`, so a shut door refuses before any
        URL parsing, DNS resolution or socket, on the redirect hops as well as
        the first one: a door the owner closes mid-run closes on the next hop.
        """
        client = self._client()
        hops: list[str] = []
        for hop in range(MAX_REDIRECTS + 1):
            _require_door("web_fetch")
            # Every refusal below — a rejected scheme, an internal address, a
            # blown budget, a dead socket — leaves one ledger line and then
            # propagates unchanged. The gates themselves are untouched; only
            # the record of what they decided is new.
            try:
                parts = self._validate_url(url, allowed_hosts)
                budget.consume(1, egress=True)
                response = client.get(
                    url,
                    headers={"User-Agent": _UA,
                             "Accept": "text/html,text/plain,*/*"},
                    timeout=timeout, allow_redirects=False, stream=True)
                try:
                    self._verify_peer(response, parts.hostname or "")
                    status = int(getattr(response, "status_code", 0) or 0)
                    if status in (301, 302, 303, 307, 308):
                        location = (getattr(response, "headers", {}) or {}
                                    ).get("Location")
                        if not location:
                            raise ToolsmithInputError(
                                f"web_fetch: {status} redirect with no Location")
                        if hop >= MAX_REDIRECTS:
                            raise ToolsmithInputError(
                                f"web_fetch: more than {MAX_REDIRECTS} redirects")
                        hops.append(url)
                        # The hop happened: this host saw us. Recorded before we
                        # move on, so a chain shows every host it touched rather
                        # than only where it ended up.
                        _fetch_ledger(url, True, detail="redirect",
                                      status=status)
                        # Relative redirects are resolved against the
                        # *validated* current URL, then re-validated from the
                        # top of the loop.
                        url = self._clean_url(urljoin(url, str(location)))
                        redirected = True
                    else:
                        redirected = False
                        if status >= 400 or status == 0:
                            _fetch_ledger(url, True, detail="http error",
                                          status=status)
                            return {"ok": False, "url": url, "status": status,
                                    "error": f"toolsmith: web_fetch: HTTP {status}"}
                        content_type = self._content_type(response)
                        raw, over_cap = _read_capped(response,
                                                     MAX_RESPONSE_BYTES)
                finally:
                    closer = getattr(response, "close", None)
                    if callable(closer):
                        try:
                            closer()
                        except Exception:  # pragma: no cover - transport specific
                            pass
            except Exception as exc:
                # Only OUR OWN exception messages go in the trail. Every string
                # raised above by this module is audited to name a host, a
                # scheme or a port and never a whole URL — but a transport
                # exception is not ours: requests puts the full request URL,
                # query string and all, into ConnectionError's message, and
                # the query string is precisely what this ledger must not
                # record. For anything we did not write, the class name is the
                # whole of it.
                detail = (f"{type(exc).__name__}: {exc}"
                          if isinstance(exc, (ToolsmithInputError, EgressDenied))
                          else type(exc).__name__)
                _fetch_ledger(
                    url, False, detail=detail,
                    # Set by reject_internal_address, so an address refusal is
                    # a value in the trail and not a substring of a sentence.
                    reason_code=getattr(exc, "reason_code", "") or "",
                    address_class=getattr(exc, "address_class", "") or "")
                raise
            if redirected:
                continue

            _fetch_ledger(url, True, status=status, bytes_in=len(raw))
            body = raw.decode(_charset(content_type), errors="replace")
            text = extract_text(body, content_type)
            truncated = over_cap or len(text) > max_chars
            if len(text) > max_chars:
                text = text[:max_chars]
            return {
                "ok": True,
                "url": url,
                "redirects": hops,
                "status": status,
                "content_type": content_type,
                "bytes_read": len(raw),
                "text": text,
                "truncated": truncated,
                # The whole point of this agent's separation: whatever came back
                # is DATA. Downstream prompt assembly must label it, never splice
                # it in as instruction.
                "untrusted": True,
            }
        raise ToolsmithInputError(  # pragma: no cover - loop always returns first
            f"web_fetch: more than {MAX_REDIRECTS} redirects")

    # ── search_and_read (search → fetch, fused into one step) ────────
    def _search_and_read(self, envelope: Envelope, p: dict) -> dict:
        """Search SearXNG, then read the top result through the FULL SSRF gate.

        This exists so a PLANNED run can compose an online search into a
        workflow. ``web_search`` alone cannot: a URL it discovers is not in the
        run's sealed :class:`~puffpai.agents.planning_loop.EgressScope`, so a
        following ``web_fetch`` step would be refused; and steps do not see each
        other's results. Fusing the two into one step sidesteps both without
        widening any egress path:

        * The search half is the unchanged :meth:`_web_search` pinhole — one
          owner-fixed SearXNG host, no payload-chosen URL, no redirects followed.
        * The read half goes through :meth:`_fetch_url` with ``allowed_hosts=None``
          — which skips ONLY the caller-scope check (there is no pre-sealed scope
          for a URL discovered a moment ago) while EVERY unconditional SSRF gate
          still runs: scheme, credentials, port, per-hop resolved-address checks,
          peer re-verification, redirect re-validation. So a result pointing at
          ``169.254.169.254`` or a loopback host is still refused here, exactly as
          a separate ``web_fetch`` of it would be.

        The whole step is egress-gated like a fetch (it is in
        :data:`EGRESS_INTENTS`), it changes no durable state (absent from
        :data:`STATE_CHANGING_INTENTS`), and its returned page text is marked
        ``untrusted`` because it is web content.
        """
        budget = getattr(envelope, "budget", None)
        if budget is None or not getattr(budget, "allow_egress", False):
            raise EgressDenied(
                "search_and_read needs an envelope whose budget sets "
                "allow_egress=True; budgets default to no egress")

        # The search half — unchanged pinhole, and it re-checks egress itself.
        search = self._web_search(envelope, p)
        if not (isinstance(search, dict) and search.get("ok")):
            # Offline / HTTP error / bad query — surface the search's own reply
            # rather than inventing one.
            return search if isinstance(search, dict) else {
                "ok": False, "error": "toolsmith: search_and_read failed"}
        results = search.get("results") or []
        if not results:
            return {"ok": False, "query": search.get("query"),
                    "results": [], "untrusted": True,
                    "error": "toolsmith: search_and_read: no search results to "
                             "read"}

        chosen = results[0]
        url = self._clean_url(chosen.get("url"))
        max_chars = self._max_chars(p.get("max_chars"))
        timeout = self._timeout(p.get("timeout"))
        # allowed_hosts=None: the URL was DISCOVERED, so there is no caller scope
        # to enforce — the unconditional SSRF gates in _fetch_url are what keep
        # this safe, not a host allow-list.
        page = self._fetch_url(url, None, budget, max_chars, timeout)
        page = dict(page)
        page["query"] = search.get("query")
        page["title"] = chosen.get("title", "")
        # The read half returns web content: untrusted whether or not the fetch
        # itself set the flag (an error reply would not).
        page["untrusted"] = True
        return page

    # ── web_search (the SearXNG pinhole) ────────────────────────────
    def _web_search(self, envelope: Envelope, p: dict) -> dict:
        """Search the web via the owner's SELF-HOSTED SearXNG. Returns leads.

        Why this may reach a loopback host when :meth:`_web_fetch` may not — the
        pinhole, stated once so it can be reviewed:

        * The target is read from ``tools.searxng_url`` in the OWNER's config, a
          fixed value the user and the model can never influence. The only thing
          a request supplies is the search ``query`` — there is no payload field
          that chooses the host. So the SSRF risk ``reject_internal_address``
          exists to stop (a *user-supplied* URL reaching the LAN) is absent by
          construction, and this method therefore does not, and must not, call
          that gate for its own request.
        * Redirects are NOT followed (``allow_redirects=False``): a local
          instance cannot bounce this request to ``169.254.169.254`` or anywhere
          else. A 3xx from the fixed host is refused, not chased.
        * Nothing here relaxes any check for ``web_fetch`` or any other intent.
          The pinhole is one method reaching one owner-fixed host.

        The result URLs come back as DATA and are **never fetched here**. When
        the planner decides to read one, it calls ``web_fetch``, which runs the
        full, unmodified SSRF gate — so a result pointing at an internal or
        link-local address is still refused, and only globally routable hosts
        are ever retrieved.

        Still gated by egress: ``web_search`` is an :data:`EGRESS_INTENTS`
        member, so it refuses on a budget that does not carry ``allow_egress``,
        exactly like a fetch. Searching the web is an outbound activity even when
        the first hop is local, and it belongs behind the same opt-in.

        Gated by the ``web_search`` DOOR as well, for the same reason: the first
        hop is loopback and is not itself egress, but that container then
        queries upstream engines on the household's behalf, so the query leaves
        the house all the same. One switch turning off all web search — the
        chat tool's DuckDuckGo path and this one — is what an owner means when
        they turn web search off. The door is checked before the SearXNG
        address is even read, so a shut door opens no socket at all.
        """
        _require_door("web_search")

        budget = getattr(envelope, "budget", None)
        if budget is None or not getattr(budget, "allow_egress", False):
            raise EgressDenied(
                "web_search needs an envelope whose budget sets "
                "allow_egress=True; budgets default to no egress")

        query = self._search_query(p.get("query"))
        max_results = self._search_max(p.get("max_results"))
        base, timeout = self._searxng_settings()
        target = base + SEARXNG_SEARCH_PATH

        # Charged as an egress action — the Budget primitive re-checks egress a
        # second time, the same belt-and-braces the fetch path uses.
        budget.consume(1, egress=True)
        client = self._search_client(base)
        try:
            response = client.get(
                target,
                params={"q": query, "format": "json"},
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=timeout, allow_redirects=False)
        except netaddr.AddressRefused as refused:
            # NOT "offline". The appliance refused the destination, and saying
            # "SearXNG isn't running" about a request this code declined is the
            # dishonest-failure mode this whole module exists to make hard: the
            # owner would go restart a container that is already fine. Reached
            # when tools.searxng_url points somewhere other than the one
            # allowed loopback endpoint — at the router, say.
            logger.warning(
                "web_search: configured SearXNG at %s refused: %s",
                base, refused.address_class)
            try:
                egress_doors.record_external(
                    "web_search", False, detail="searxng address refused",
                    reason_code="refused",
                    address_class=getattr(refused, "address_class", "") or "")
            except Exception:  # pragma: no cover - defensive
                pass
            return {"ok": False, "query": query,
                    "status": "search_failed", "reason": "refused",
                    "error": "toolsmith: web_search: the configured search "
                             "service address is not one this appliance may "
                             "open a socket to, so nothing was sent. Only "
                             f"{SEARXNG_LOOPBACK_ENDPOINT} or a public host "
                             "is allowed for tools.searxng_url."}
        except Exception as exc:
            # Connection refused, DNS failure, read timeout — every "SearXNG is
            # not answering" case lands here and degrades to a legible reply the
            # agent can surface, rather than a traceback or a hang. A timeout is
            # distinguished from every other unreachable case (reason=timeout
            # vs reason=unreachable) so the caller can be honest about WHICH
            # happened instead of collapsing both into "offline".
            import requests
            is_timeout = isinstance(exc, requests.exceptions.Timeout)
            reason = "timeout" if is_timeout else "unreachable"
            logger.info("web_search: SearXNG at %s %s: %s",
                        base, "timed out" if is_timeout else "unreachable", exc)
            if is_timeout:
                error = (
                    "toolsmith: web_search: the search service timed out "
                    f"after {timeout:g}s, so I have not searched.")
            else:
                error = ("toolsmith: web_search couldn't reach the search "
                          "service — I'm offline, or SearXNG isn't running")
            try:
                # Content-free, matching the successful-call ledger line: no
                # URL, no query — just that this door's attempt failed and why.
                egress_doors.record_external(
                    "web_search", False,
                    detail="searxng timeout" if is_timeout else "searxng unreachable",
                    reason_code=reason)
            except Exception:  # pragma: no cover - defensive
                pass
            return {"ok": False, "offline": not is_timeout, "query": query,
                    "status": "search_failed", "reason": reason,
                    "timeout": timeout, "error": error}
        try:
            status = int(getattr(response, "status_code", 0) or 0)
            if status in (301, 302, 303, 307, 308):
                return {"ok": False, "query": query, "status": status,
                        "error": "toolsmith: web_search: the search service "
                                 "redirected; refusing to follow it"}
            if status >= 400 or status == 0:
                return {"ok": False, "query": query, "status": status,
                        "error": f"toolsmith: web_search: search service "
                                 f"returned HTTP {status}"}
            raw, _over = _read_capped(response, MAX_SEARCH_BYTES)
            # One content-free line so the door's "last used" is true. The
            # QUERY is never recorded — it is the whole of what leaves the
            # house on this path, and a ledger the owner reads must not be the
            # place it gets written down.
            try:
                # Through the same redaction the fetch path uses. The loopback
                # endpoint is a fixed constant and discloses nothing, but a
                # trail an owner can grep for "127.0.0.1" and get hits from is
                # a trail whose rule has an exception, and the next exception
                # will not be a constant. A public SearXNG is named normally.
                search_host, search_class = _ledger_host(
                    urlsplit(base).hostname or "")
                egress_doors.record_external(
                    "web_search", True, detail="searxng",
                    host=search_host,
                    path=SEARXNG_SEARCH_PATH, status=status,
                    bytes_in=len(raw), address_class=search_class)
            except Exception:  # pragma: no cover - defensive
                pass
        finally:
            closer = getattr(response, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - transport specific
                    pass

        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return {"ok": False, "query": query,
                    "error": "toolsmith: web_search: the search service "
                             "returned a response I couldn't read as JSON"}
        results = _parse_search_results(payload, max_results)
        return {
            "ok": True,
            "query": query,
            "results": results,
            "count": len(results),
            # Titles, snippets and URLs are attacker-influenceable web content:
            # anyone can rank a result. Downstream prompt assembly must label
            # this as data, never splice it in as instruction.
            "untrusted": True,
        }

    @staticmethod
    def _search_query(value) -> str:
        """Shape-check the one thing a caller supplies. No network."""
        if not isinstance(value, str):
            raise ToolsmithInputError("web_search: query must be a string")
        query = value.strip()
        if not query:
            raise ToolsmithInputError("web_search: query is empty")
        if len(query) > MAX_SEARCH_QUERY_LEN:
            raise ToolsmithInputError(
                f"web_search: query exceeds {MAX_SEARCH_QUERY_LEN} characters")
        # A control character would ride into the outbound request and the log.
        if any((not ch.isprintable()) and ch != " " for ch in query):
            raise ToolsmithInputError(
                "web_search: query contains control characters")
        return query

    @staticmethod
    def _search_max(value) -> int:
        """Bounded result count. Accepts int-like values (the planner coerces
        numeric params to strings, so ``"5"`` must work as well as ``5``)."""
        if value is None:
            return DEFAULT_SEARCH_RESULTS
        if isinstance(value, bool):
            raise ToolsmithInputError("web_search: max_results must be a number")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ToolsmithInputError(
                "web_search: max_results must be a number") from None
        if number < 1:
            raise ToolsmithInputError("web_search: max_results must be positive")
        return min(number, MAX_SEARCH_RESULTS)

    def _searxng_settings(self) -> tuple[str, float]:
        """The owner-configured SearXNG base URL and request timeout.

        Read from ``tools.searxng_url`` / ``tools.searxng_timeout`` (never from
        the payload), or from the values injected in the constructor for tests.
        Validated only for *shape* — an http(s) scheme and a host — because it is
        owner-set, not user-set; the whole point of the pinhole is that this
        value cannot be steered by a request.
        """
        url = self._searxng_url
        timeout = self._searxng_timeout
        if url is None or timeout is None:
            try:
                from puffpai.config import get_config
                tools = get_config().tools
                if url is None:
                    url = tools.searxng_url
                if timeout is None:
                    timeout = tools.searxng_timeout
            except Exception as exc:  # config must never break a tool
                logger.warning("config unavailable for web_search: %s", exc)
                if url is None:
                    url = _DEFAULT_SEARXNG_URL
                if timeout is None:
                    timeout = DEFAULT_SEARCH_TIMEOUT

        base = str(url or "").strip().rstrip("/")
        try:
            parts = urlsplit(base)
        except ValueError:
            parts = None
        if (parts is None or (parts.scheme or "").lower() not in ALLOWED_URL_SCHEMES
                or not parts.hostname):
            raise ToolsmithInputError(
                "web_search: tools.searxng_url is not a valid http(s) URL")

        try:
            seconds = float(timeout)
        except (TypeError, ValueError):
            seconds = DEFAULT_SEARCH_TIMEOUT
        if seconds <= 0:
            seconds = DEFAULT_SEARCH_TIMEOUT
        return base, min(seconds, MAX_SEARCH_TIMEOUT)

    # ── URL validation ──────────────────────────────────────────────
    @staticmethod
    def _clean_url(value) -> str:
        """Shape checks that need no network: type, length, printability."""
        if not isinstance(value, str):
            raise ToolsmithInputError("web_fetch: url must be a string")
        url = value.strip()
        if not url:
            raise ToolsmithInputError("web_fetch: url is empty")
        if len(url) > MAX_URL_LEN:
            raise ToolsmithInputError(
                f"web_fetch: url exceeds {MAX_URL_LEN} characters")
        if any(ch.isspace() or not ch.isprintable() for ch in url):
            raise ToolsmithInputError(
                "web_fetch: url contains whitespace or control characters")
        return url

    @staticmethod
    def _allowed_hosts(value) -> frozenset[str] | None:
        """Normalise a caller's host scope. ``None`` means **unrestricted**.

        The absent/empty distinction is the whole contract and must not be
        collapsed into a truthiness test:

        * **absent or ``None``** → unrestricted, which is what every
          explicitly-budgeted caller that predates this gate relies on
          (``majordomo.BRIEFING_BUDGET`` fetches without naming hosts);
        * **present** → enforced, and that includes ``[]``, which permits
          nothing at all. ``if allowed_hosts:`` would silently turn the most
          restrictive possible scope into no scope at all.

        A value that is not a list or tuple **refuses the fetch** rather than
        being ignored: a caller that meant to pass a scope and passed a string
        must not be quietly promoted to unrestricted.
        """
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            raise ToolsmithInputError(
                "web_fetch: allowed_hosts must be a list of hostnames "
                f"(got {type(value).__name__})")
        hosts = set()
        for entry in list(value)[:MAX_ALLOWED_HOSTS]:
            if not isinstance(entry, str):
                # Dropped, not fatal: one junk entry narrows the scope, which
                # is the safe direction. A junk *container* is fatal above.
                continue
            # Normalised exactly as ``_validate_url`` normalises the host it
            # compares against, or the comparison is unsound in both directions.
            host = entry.strip().lower().rstrip(".")
            if host:
                hosts.add(host)
        return frozenset(hosts)

    def _validate_url(self, url: str, allowed_hosts: frozenset[str] | None = None):
        """Full per-hop validation. Raises unless this exact URL is safe to open.

        Runs from scratch on every redirect hop because the only thing a first
        validation proves about hop three is nothing.

        *allowed_hosts* is the caller's scope (see :meth:`_allowed_hosts`);
        ``None`` leaves this method behaving exactly as it did before the scope
        gate existed.
        """
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port   # .port validates the port
        except ValueError as exc:
            raise ToolsmithInputError(f"web_fetch: unusable url ({exc})") from None

        scheme = (parts.scheme or "").lower()
        if scheme not in ALLOWED_URL_SCHEMES:
            raise ToolsmithInputError(
                f"web_fetch: url scheme must be one of "
                f"{sorted(ALLOWED_URL_SCHEMES)}, not {scheme!r}")
        if parts.username or parts.password or "@" in parts.netloc:
            # Credentials in a URL are handed to whichever host actually answers,
            # which after a redirect need not be the one the caller meant.
            raise ToolsmithInputError("web_fetch: url must not carry credentials")
        if not host:
            raise ToolsmithInputError("web_fetch: url has no host")

        host = host.lower().rstrip(".")

        # The caller's scope, ahead of the address checks below on purpose: an
        # out-of-scope host must not even be looked up, because a DNS query for
        # an attacker-chosen name is itself a signal leaving the appliance. This
        # ADDS a refusal; it reorders none of the checks that follow, all of
        # which still run in their original order for every host that passes.
        if allowed_hosts is not None and host not in allowed_hosts:
            raise ToolsmithInputError(
                f"web_fetch: {host} is not in the caller's allowed_hosts; a "
                "redirect cannot take this request somewhere the caller never "
                "approved")

        # A literal address is checked directly as well as through the resolver,
        # so the refusal does not depend on the resolver at all — and it is
        # checked FIRST, because the name heuristics below would otherwise
        # answer "127.0.0.1 is an obfuscated address" when the truthful and
        # more useful refusal is "that is loopback".
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            reject_internal_address(str(literal))
        else:
            self._reject_local_name(host)

        effective_port = port or (443 if scheme == "https" else 80)
        for address in self._resolve(host, effective_port) or []:
            reject_internal_address(address, host=host)
        if effective_port not in ALLOWED_PORTS:
            raise ToolsmithInputError(
                f"web_fetch: port {effective_port} is not in "
                f"{sorted(ALLOWED_PORTS)}")
        return parts

    @staticmethod
    def _reject_local_name(host: str) -> None:
        """Refuse names that can only ever mean 'inside this house'."""
        if host in LOCAL_HOST_NAMES or host.endswith(LOCAL_HOST_SUFFIXES):
            raise ToolsmithInputError(
                f"web_fetch: {host} is a local-network name")
        # Strict parsing failed, but a host whose last label is numeric is an
        # address in disguise: ``127.1``, ``2130706433`` and ``0x7f000001`` are
        # all loopback to a permissive client. A real DNS name never ends in an
        # all-numeric label, so this cannot reject a legitimate host.
        if _NUMERIC_LABEL_RE.match(host.rsplit(".", 1)[-1]):
            raise ToolsmithInputError(
                f"web_fetch: {host} is an abbreviated or obfuscated IP address")
        if "." not in host:
            raise ToolsmithInputError(
                f"web_fetch: {host} is a single-label intranet name")

    @staticmethod
    def _verify_peer(response, host: str) -> None:
        """Re-check the address the socket actually reached, where we can see it.

        **No longer the control.** Between our ``getaddrinfo`` and the HTTP
        client's own, a hostile resolver can change its answer — classic DNS
        rebinding — and this could only narrow that window by refusing after the
        fact. The complete fix, a connection pinned to the address we validated,
        landed in :func:`puffpai.security.netaddr.pinned_session` and is what
        :meth:`_client` now returns.

        It stays because it costs nothing and it still covers the one case the
        pin does not: an INJECTED transport. ``http=`` is a constructor
        argument, tests pass their own, and a future caller could pass a client
        this module did not build and cannot pin. Deleting a working check
        because the usual path no longer needs it is how the unusual path gets
        forgotten. Best effort by construction: a transport that does not expose
        its socket yields no verification.
        """
        peer = getattr(response, "peer_address", None)
        if peer is None:
            try:
                peer = response.raw._connection.sock.getpeername()[0]
            except Exception:
                return
        reject_internal_address(str(peer), host=host)

    @staticmethod
    def _content_type(response) -> str:
        headers = getattr(response, "headers", {}) or {}
        raw = str(headers.get("Content-Type") or headers.get("content-type") or "")
        content_type = raw.split(";", 1)[0].strip().lower()
        if not content_type:
            # No declared type: treat it as plain text rather than guess. The
            # size cap and the text extractor both hold regardless.
            return "text/plain"
        if (content_type in ALLOWED_CONTENT_TYPES
                or content_type.startswith(ALLOWED_CONTENT_PREFIXES)):
            return content_type
        raise ToolsmithInputError(
            f"web_fetch: content-type {content_type!r} is not text; this tool "
            "extracts prose and will not hand back binary")

    @staticmethod
    def _max_chars(value) -> int:
        if value is None:
            return DEFAULT_MAX_CHARS
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolsmithInputError("web_fetch: max_chars must be a number")
        number = int(value)
        if number < 1:
            raise ToolsmithInputError("web_fetch: max_chars must be positive")
        return min(number, HARD_MAX_CHARS)

    @staticmethod
    def _timeout(value) -> float:
        if value is None:
            return DEFAULT_TIMEOUT
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolsmithInputError("web_fetch: timeout must be a number")
        number = float(value)
        if number <= 0:
            raise ToolsmithInputError("web_fetch: timeout must be positive")
        return min(number, MAX_TIMEOUT)

    # -- transports --------------------------------------------------
    #
    # THREE slots, and the distinction between them is load-bearing:
    #
    #   self._http          the INJECTED transport. Constructor argument,
    #                       written in __init__ and never assigned again. When
    #                       a caller supplies one it wins everywhere.
    #   self._http_pinned   cache for the pinned session (every fetch).
    #   self._http_search   cache for the plain session that may reach the one
    #                       allowed loopback endpoint, and nothing else.
    #
    # They must NOT be one slot. If ``_client()`` lazily cached the pinned
    # session into ``self._http``, the INJECTION slot, and
    # ``_search_client()`` read that same slot to mean "a transport was
    # injected, use it", then, because ToolsmithAgent is a process singleton,
    # the first ``web_fetch`` in the process would permanently poison every
    # later ``web_search``: it would get the pinned session, which refuses
    # loopback, and search would die with "configured SearXNG at
    # http://127.0.0.1:8888 refused: loopback" until restart.
    #
    # A cache sharing a slot with a policy input is not a cache, it is a side
    # channel. Hence three names and one rule: a cache slot is read only by the
    # method that owns it, and which transport a call gets is decided by that
    # call's own inputs — never by what ran before it.

    def _client(self):
        """The transport for every fetch: **pinned** to validated addresses.

        A session from :func:`puffpai.security.netaddr.pinned_session`, which
        resolves each host through the same policy
        :func:`reject_internal_address` applies and opens the socket to an
        address that just passed it — while the TLS server name and the ``Host``
        header keep the hostname. That closes the window :meth:`_verify_peer`
        could only narrow.

        Connection reuse across fetches is safe and is in fact the stricter
        outcome: a pooled socket already points at the address that was
        validated when it was opened, and urllib3 keys its pools by host, so a
        later hostile DNS answer cannot redirect an existing one.

        **Never reaches loopback, and never becomes the search transport.**
        """
        if self._http is not None:
            return self._http
        if self._http_pinned is None:
            self._http_pinned = netaddr.pinned_session()
        return self._http_pinned

    def _search_client(self, base: str):
        """The transport for the ONE call that may reach loopback.

        :meth:`_client` is pinned, and a pinned session refuses loopback — which
        is right for every fetch and wrong for the documented SearXNG pinhole,
        whose whole point is that the owner runs the search service on this
        machine.

        The allowance is not "loopback is permitted for search". It is
        :data:`SEARXNG_LOOPBACK_ENDPOINT` — one address, one port, compared
        exactly — and **the configured base URL is the only thing that selects
        this transport.** Not what some earlier call happened to cache: the
        branch below is decided by *base* on every call, so no sequence of
        fetches and searches can change which transport a given *base* gets.
        This method never returns the pinned session for the loopback endpoint,
        and never returns the loopback session for anything else.

        A SearXNG configured anywhere else gets the pinned session and is judged
        like any other destination, so an owner who points ``tools.searxng_url``
        at the router does not get a hole; they get a refusal.

        An injected transport (``http=``) still wins, so tests are unaffected.
        """
        if self._http is not None:
            return self._http
        if not _is_searxng_loopback(base):
            # Not the exception: judged like every other destination.
            return self._client()
        if self._http_search is None:
            # Plain session, deliberately: there is no name to resolve, so
            # there is nothing for a pin to protect. Still no environment
            # trust and no cookie jar — a search service is not a place to
            # send a stored credential or carry an identifier.
            import requests
            session = requests.Session()
            session.trust_env = False
            try:
                from http.cookiejar import DefaultCookiePolicy
                session.cookies.set_policy(DefaultCookiePolicy(
                    allowed_domains=[]))
            except Exception:  # pragma: no cover - transport specific
                pass
            self._http_search = session
        return self._http_search

    # ── workspace ───────────────────────────────────────────────────
    def workspace_root(self) -> str:
        """Absolute sandbox root, resolved once per agent and created on use."""
        if self._workspace_root is None:
            self._workspace_root = _resolve_workspace_root()
        os.makedirs(self._workspace_root, exist_ok=True)
        return self._workspace_root

    def _safe_path(self, value, *, field: str = "path",
                   allow_root: bool = False) -> str:
        """Resolve *value* inside the workspace, or refuse.

        The containment proof is ``realpath(root/rel)`` starting with
        ``realpath(root) + sep``. That one test defeats ``../``, an absolute
        path, a Windows drive letter, a UNC path **and** a symlink planted inside
        the workspace that points out of it — three of which a string-prefix
        check on the un-resolved path would let through.

        The component checks above it exist for the error message, not for the
        security property; the realpath check would refuse all of them anyway.
        """
        if not isinstance(value, str):
            if value is None and allow_root:
                return self.workspace_root()
            raise ToolsmithInputError(f"{field} must be a string")
        raw = value.strip()
        if not raw:
            if allow_root:
                return self.workspace_root()
            raise ToolsmithInputError(f"{field} is empty")
        if "\x00" in raw:
            raise ToolsmithInputError(f"{field} contains a null byte")
        if len(raw) > MAX_REL_PATH_LEN:
            raise ToolsmithInputError(
                f"{field} exceeds {MAX_REL_PATH_LEN} characters")

        normalised = raw.replace("\\", "/")
        if normalised.startswith("/") or os.path.isabs(raw) or ":" in normalised:
            raise ToolsmithInputError(
                f"{field} must be relative to the workspace, not an absolute path")

        components = normalised.split("/")
        if len(components) > MAX_PATH_DEPTH:
            raise ToolsmithInputError(
                f"{field} is nested deeper than {MAX_PATH_DEPTH} levels")
        for component in components:
            if component in _REJECTED_COMPONENTS:
                raise ToolsmithInputError(
                    f"{field} may not contain empty, '.' or '..' components")
            if component.split(".", 1)[0].lower() in _WINDOWS_DEVICES:
                raise ToolsmithInputError(
                    f"{field} names a reserved device ({component!r})")

        root = self.workspace_root()
        resolved = os.path.realpath(os.path.join(root, *components))
        if resolved != root and not resolved.startswith(root + os.sep):
            raise ToolsmithInputError(
                f"{field} escapes the workspace sandbox")
        if resolved == root and not allow_root:
            raise ToolsmithInputError(f"{field} is the workspace root itself")
        return resolved

    def _workspace_write(self, envelope: Envelope, p: dict) -> dict:
        """Write UTF-8 text into the sandbox. The only state-changing tool here.

        Text only, and a size cap, because this is a notes drawer for a plan in
        progress — not a place to stage a binary, and not a place a runaway loop
        can fill a disk from.
        """
        path = self._safe_path(p.get("path"))
        content = p.get("content")
        if content is None:
            raise ToolsmithInputError("content is required")
        if not isinstance(content, str):
            raise ToolsmithInputError("content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ToolsmithInputError(
                f"content exceeds {MAX_FILE_BYTES} bytes")

        existed = os.path.exists(path)
        if not existed and self._count_files() >= MAX_WORKSPACE_FILES:
            raise ToolsmithInputError(
                f"workspace already holds {MAX_WORKSPACE_FILES} files; delete "
                "some before writing more")

        envelope.budget.consume(1)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return {"ok": True, "path": self._relative(path),
                "bytes": len(encoded), "created": not existed}

    def _workspace_exec(self, envelope: Envelope, p: dict) -> dict:
        """Run a shell command against the workspace, inside a bubblewrap jail.

        This is the generic primitive the enumerated-capability tier cannot
        provide: it is what lets a plan do a thing nobody wrote an intent for.
        The whole reason it is safe to offer is the jail, so the jail is built
        here rather than trusted from configuration — see the threat model above
        :data:`BWRAP` for what it does and does not contain, and for why this is
        NOT Docker-backed on this appliance.

        Three refusals, in order, none of them advisory:

        1. **Reviewed** — ``workspace_exec`` is in
           :data:`STATE_CHANGING_INTENTS`, so ``handle`` has already refused any
           envelope that did not carry ``state_changing=True``. An unreviewed
           command never reaches this method.
        2. **Isolable** — if :data:`BWRAP` is not present the tool refuses. It
           does **not** fall back to host execution; losing isolation must cost
           the capability, not the isolation.
        3. **Bounded** — wall-clock timeout, killed as a process group, with the
           output truncated to :data:`EXEC_MAX_OUTPUT_BYTES`.

        The result is stamped ``untrusted: True`` for the same reason a fetch is:
        a command's output is very often a file some earlier ``web_fetch`` wrote,
        so it carries the same prompt-injection risk one step later. Exit status
        is reported rather than raised — a command that fails is a *result*, and
        the planner needs to see the stderr to decide what to do next.
        """
        command = p.get("command")
        if command is None:
            raise ToolsmithInputError("command is required")
        if not isinstance(command, str):
            raise ToolsmithInputError("command must be a string")
        if not command.strip():
            raise ToolsmithInputError("command must not be empty")

        timeout = self._exec_timeout(p.get("timeout"))

        if not os.path.isfile(BWRAP) or not os.access(BWRAP, os.X_OK):
            # Fail closed. See threat model: never degrade to the host.
            return {
                "ok": False,
                "sandbox_unavailable": True,
                "error": f"toolsmith: workspace_exec needs the sandbox helper "
                         f"({BWRAP}) and it is not executable here; refusing "
                         "rather than running the command unsandboxed",
            }

        root = self.workspace_root()
        envelope.budget.consume(1)

        argv = self._jail_argv(root, command)

        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout,
                start_new_session=True,      # own process group, so the kill lands
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "timed_out": True, "untrusted": True,
                    "timeout": timeout,
                    "error": f"toolsmith: command exceeded {timeout:g}s and was "
                             "killed"}
        except OSError as exc:
            return {"ok": False, "sandbox_unavailable": True,
                    "error": f"toolsmith: sandbox failed to start ({exc}); "
                             "refusing to run the command unsandboxed"}

        stdout, out_trunc = self._cap_output(proc.stdout)
        stderr, err_trunc = self._cap_output(proc.stderr)
        return {
            "ok": proc.returncode == 0,
            "untrusted": True,
            "sandboxed": True,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": out_trunc or err_trunc,
        }

    def _registry_read(self, envelope: Envelope, p: dict) -> dict:
        """Invoke a registry-declared READ-ONLY tool by name.

        The bridge between the planner's closed catalogue and the legacy tool
        tier. See the policy block above :data:`BRIDGE_AGENTS` for why a generic
        dispatcher is admissible here and what boxes it in.

        Refusals are ordered cheapest-first and every one of them is a hard
        stop, not a warning. The tool is resolved from the registry *before* any
        live lookup, so an invented name never reaches the process.
        """
        name = p.get("tool")
        if not isinstance(name, str) or not name.strip():
            raise ToolsmithInputError("tool (a registry tool name) is required")
        name = name.strip()

        cap = self._bridge_capability(name)
        if cap is None:
            return {"ok": False, "error":
                    f"toolsmith: {name!r} is not a tool the capability registry "
                    "declares; the bridge only reaches declared tools"}

        refusal = self._bridge_refusal(cap, name)
        if refusal:
            return {"ok": False, "refused": True, "error": refusal}

        tool = self._live_tool(name)
        if tool is None:
            return {"ok": False, "error":
                    f"toolsmith: {name!r} is declared by the registry but is not "
                    "live in this process"}

        envelope.budget.consume(1)
        query = p.get("query")
        if query is not None and not isinstance(query, str):
            raise ToolsmithInputError("query must be a string")
        try:
            raw = tool.invoke(query if query is not None else "")
        except Exception as exc:
            # A failing tool is a result, not a reason to abort the bus.
            #
            # The exception STRING is deliberately not logged: a tool that
            # quotes the argument it choked on would put model-chosen text
            # from this turn into the journal, and on a vault-adjacent turn
            # that text can be the PIN. BRIDGE_DENIED_CAPS already keeps the
            # vault capabilities off this bridge, so this is the second fence
            # rather than the first. The reason still goes back to the caller
            # on the line below; only the durable copy is trimmed.
            logger.warning("registry_read %s failed: %s", name,
                           type(exc).__name__)
            return {"ok": False, "error": f"toolsmith: {name} failed ({exc})"}

        text = raw if isinstance(raw, str) else str(raw)
        truncated = len(text) > BRIDGE_MAX_OUTPUT
        return {"ok": True, "untrusted": True, "tool": name,
                "capability": cap.id, "text": text[:BRIDGE_MAX_OUTPUT],
                "truncated": truncated}

    @staticmethod
    def _bridge_capability(name: str):
        """The registry capability that declares *name*, or None."""
        from puffpai import capability_registry as registry
        for cap in registry.all_capabilities():
            if name in cap.all_tools:
                return cap
        return None

    @staticmethod
    def _bridge_refusal(cap, name: str) -> str | None:
        """Why this capability may not be bridged, or None when it may.

        Order is deliberate: identity first (a self-call is a loop, not a
        privilege question), then the vault by id, then domain, then access.
        Checking access last means the error a caller sees names the most
        specific reason rather than the most generic one.
        """
        if cap.id == BRIDGE_SELF_CAP:
            return "toolsmith: the bridge may not invoke itself"
        if cap.id in BRIDGE_DENIED_CAPS:
            return (f"toolsmith: {cap.id} is vault-scoped and is never reachable "
                    "through the bridge; it keeps its own PIN-gated path")
        if cap.agent not in BRIDGE_AGENTS:
            return (f"toolsmith: {name} belongs to {cap.agent!r}, which is "
                    "outside the information domain the bridge may reach")
        if cap.access != BRIDGE_READ:
            return (f"toolsmith: {cap.id} is {cap.access!r}; the bridge reaches "
                    "read-only capabilities and never a state-changer")
        return None

    def _live_tool(self, name: str):
        """Resolve a live LangChain tool by name, or None.

        Injected in tests. Otherwise taken from the API server's cached
        ``AssistantTools`` singleton — imported lazily because ``api_server``
        imports this module, so a module-level import would be circular.
        """
        if self._tool_source is not None:
            return self._tool_source.get(name)
        try:
            from puffpai.api_server import _get_assistant_tools
            return _get_assistant_tools().tool_dict.get(name)
        except Exception as exc:
            logger.debug("bridge could not reach the live tool set: %s", exc)
            return None

    @staticmethod
    def _jail_argv(root: str, command: str, *,
                   extra_ro_binds: tuple = ()) -> list:
        """Build the bubblewrap argv both sandboxed tools run inside.

        One builder, so there is a single place to audit what a jail contains
        and a single place a mistake could be made. *extra_ro_binds* is the only
        axis a caller may widen, and callers pass a module-level constant rather
        than anything derived from a payload — a caller-chosen bind would be a
        way to mount the secrets back in.
        """
        argv = [BWRAP,
                "--unshare-all",        # net namespace included: no egress, at all
                "--die-with-parent",    # cannot outlive the agent
                "--new-session",        # no shared tty -> no TIOCSTI injection
                "--clearenv"]
        for src in (*EXEC_RO_BINDS, *extra_ro_binds):
            # Skipped when absent: /lib64 has no arm64 counterpart, and a bind
            # list that assumes it would refuse every command on this hardware.
            if os.path.exists(src):
                argv += ["--ro-bind", src, src]
        argv += ["--proc", "/proc",
                 "--dev", "/dev",
                 "--tmpfs", "/tmp",
                 "--bind", root, EXEC_WORKDIR,   # the ONLY writable host path
                 "--chdir", EXEC_WORKDIR,
                 "--setenv", "PATH", "/usr/bin:/bin",
                 "--setenv", "HOME", EXEC_WORKDIR,
                 "--setenv", "TMPDIR", "/tmp",
                 "--", "/bin/sh", "-c", command]
        return argv

    def _document_convert(self, envelope: Envelope, p: dict) -> dict:
        """Convert a workspace document to another format, inside the jail.

        This is what makes "read my Word doc" and "give me that as a PDF" real
        without shipping a parser per format: LibreOffice already reads and
        writes ``docx``/``xlsx``/``pptx``/``odt``/``pdf``, and it is already on
        the appliance. It runs in the same bubblewrap jail as
        :meth:`_workspace_exec` plus :data:`DOC_RO_BINDS` — still no network,
        still no path outside the workspace, still killed on a timeout.

        The output filename is chosen by LibreOffice (same stem, new extension)
        and lands beside the input, so the caller can read it back with
        ``workspace_read`` or list it with ``workspace_list``.

        Note for callers: HTML *input* does not convert on this appliance's
        LibreOffice build (it reports a misleading "no export filter" error).
        HTML output is fine. Real document formats are the point here.
        """
        source = self._safe_path(p.get("path"))
        target = p.get("to")
        if not isinstance(target, str) or not target.strip():
            raise ToolsmithInputError("to (target format) is required")
        target = target.strip().lower().lstrip(".")
        if target not in DOC_TARGET_FORMATS:
            raise ToolsmithInputError(
                f"unsupported target format {target!r}; supported: "
                + ", ".join(sorted(DOC_TARGET_FORMATS)))
        if not os.path.isfile(source):
            return {"ok": False,
                    "error": f"toolsmith: no such workspace file: "
                             f"{self._relative(source)}"}

        timeout = self._exec_timeout(p.get("timeout"), default=DOC_DEFAULT_TIMEOUT)
        if not os.path.isfile(BWRAP) or not os.access(BWRAP, os.X_OK):
            return {"ok": False, "sandbox_unavailable": True,
                    "error": f"toolsmith: document_convert needs the sandbox "
                             f"helper ({BWRAP}) and it is not executable here; "
                             "refusing rather than converting unsandboxed"}

        root = self.workspace_root()
        envelope.budget.consume(1)

        # Relative to the jail's workdir, never the host path: the jail sees the
        # workspace at EXEC_WORKDIR and a host path would simply not exist.
        rel = os.path.relpath(source, root).replace(os.sep, "/")
        profile = f"file://{EXEC_WORKDIR}/{DOC_PROFILE_DIRNAME}"
        # ``--outdir`` is required, not cosmetic: without it LibreOffice writes
        # the result into the process's CWD rather than beside the input, so a
        # file in a subdirectory converts to the workspace root and the caller
        # is told nothing was produced. Pointed at the input's own directory so
        # the output lands where :func:`os.path.splitext` below expects it.
        #
        # No ``--`` separator: soffice does not accept one and answers with its
        # usage text instead of converting. ``shlex.quote`` carries the safety
        # here, on a path ``_safe_path`` has already proven is inside the
        # workspace.
        outdir = os.path.dirname(rel) or "."
        command = (f"soffice --headless -env:UserInstallation={profile} "
                   f"--convert-to {target} --outdir {shlex.quote(outdir)} "
                   f"{shlex.quote(rel)}")

        try:
            proc = subprocess.run(
                self._jail_argv(root, command, extra_ro_binds=DOC_RO_BINDS),
                capture_output=True, timeout=timeout, start_new_session=True)
        except subprocess.TimeoutExpired:
            return {"ok": False, "timed_out": True, "timeout": timeout,
                    "error": f"toolsmith: conversion exceeded {timeout:g}s and "
                             "was killed"}
        except OSError as exc:
            return {"ok": False, "sandbox_unavailable": True,
                    "error": f"toolsmith: sandbox failed to start ({exc}); "
                             "refusing to convert unsandboxed"}

        produced = os.path.splitext(source)[0] + "." + target
        if not os.path.isfile(produced):
            # LibreOffice exits 0 even when it refuses a filter, so the file on
            # disk is the only honest success signal.
            stderr, _ = self._cap_output(proc.stderr)
            stdout, _ = self._cap_output(proc.stdout)
            return {"ok": False,
                    "error": "toolsmith: conversion produced no output "
                             f"({(stderr or stdout or '').strip()[:200]})"}
        return {"ok": True, "untrusted": True, "sandboxed": True,
                "path": self._relative(produced),
                "bytes": os.path.getsize(produced),
                "format": target}

    @staticmethod
    def _exec_timeout(raw, *, default: float = EXEC_DEFAULT_TIMEOUT) -> float:
        """Clamp a caller's timeout into ``(0, EXEC_MAX_TIMEOUT]``.

        *default* differs per tool: a shell command should answer quickly, while
        LibreOffice needs longer, especially on its profile-building first run.
        """
        if raw is None:
            return min(default, EXEC_MAX_TIMEOUT)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ToolsmithInputError("timeout must be a number")
        if value != value or value <= 0:          # NaN or non-positive
            raise ToolsmithInputError("timeout must be positive")
        return min(value, EXEC_MAX_TIMEOUT)

    @staticmethod
    def _cap_output(raw: bytes) -> tuple[str, bool]:
        """Decode captured output, truncating past the cap.

        ``errors="replace"`` because a sandboxed command may emit arbitrary
        bytes, and a tool that raises on a stray byte is a tool that fails on
        the day it matters.
        """
        data = raw or b""
        truncated = len(data) > EXEC_MAX_OUTPUT_BYTES
        if truncated:
            data = data[:EXEC_MAX_OUTPUT_BYTES]
        return data.decode("utf-8", errors="replace"), truncated

    def _workspace_read(self, envelope: Envelope, p: dict) -> dict:
        """Read a sandbox file back as text.

        Marked ``untrusted`` like a fetch: the most likely author of a workspace
        file is a previous ``web_fetch``, so its content carries exactly the same
        prompt-injection risk one step later.
        """
        path = self._safe_path(p.get("path"))
        if not os.path.isfile(path):
            return {"ok": False,
                    "error": f"toolsmith: no such workspace file: "
                             f"{self._relative(path)}"}
        envelope.budget.consume(1)
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_FILE_BYTES)
        return {"ok": True, "path": self._relative(path), "content": content,
                "bytes": size, "truncated": size > len(content.encode("utf-8")),
                "untrusted": True}

    def _workspace_list(self, envelope: Envelope, p: dict) -> dict:
        """List sandbox files, optionally under *subdir*.

        ``subdir`` goes through the same containment check as any other path, so
        ``workspace_list`` cannot be used to enumerate ``/etc`` — a listing is a
        read, and a read of the wrong directory is still a disclosure.
        """
        root = self.workspace_root()
        base = self._safe_path(p.get("subdir"), field="subdir", allow_root=True)
        envelope.budget.consume(1)
        if not os.path.isdir(base):
            return {"ok": True, "root": root, "subdir": self._relative(base),
                    "files": [], "count": 0}

        files = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for filename in sorted(filenames):
                full = os.path.join(dirpath, filename)
                # ``os.walk`` does not follow directory symlinks, but a symlinked
                # FILE still reads through: skip anything whose realpath leaves
                # the root, so a listing can never advertise /etc/passwd.
                real = os.path.realpath(full)
                if real != root and not real.startswith(root + os.sep):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                files.append({"path": self._relative(full), "bytes": size})
                if len(files) >= MAX_LIST_ENTRIES:
                    return {"ok": True, "root": root,
                            "subdir": self._relative(base), "files": files,
                            "count": len(files), "truncated": True}
        return {"ok": True, "root": root, "subdir": self._relative(base),
                "files": files, "count": len(files)}

    def _relative(self, path: str) -> str:
        """Workspace-relative form, with forward slashes on every platform."""
        root = self.workspace_root()
        try:
            rel = os.path.relpath(path, root)
        except ValueError:  # pragma: no cover - different drive on Windows
            return path
        return "." if rel == "." else rel.replace(os.sep, "/")

    def _count_files(self) -> int:
        total = 0
        for _dirpath, _dirnames, filenames in os.walk(self.workspace_root()):
            total += len(filenames)
            if total >= MAX_WORKSPACE_FILES:
                break
        return total

    # ── health ──────────────────────────────────────────────────────
    def health(self) -> dict:
        base = super().health()
        base["tools"] = sorted(TOOL_INTENTS)
        # The Agent Team page renders this field verbatim, so it must agree
        # with the two sandboxed capabilities listed directly above it.
        base["sandbox"] = ("bubblewrap — no network, workspace only, "
                           "fails closed" if os.path.isfile(BWRAP)
                           else "unavailable — sandboxed tools will refuse")
        try:
            root = self.workspace_root()
            base["workspace"] = root
            base["workspace_writable"] = os.access(root, os.W_OK)
        except Exception as exc:
            base["workspace"] = None
            base["workspace_writable"] = False
            base["error"] = str(exc)
        return base


def _read_capped(response, limit: int) -> tuple[bytes, bool]:
    """Read at most *limit* bytes. Returns ``(body, hit_the_cap)``.

    Streams where the transport can, because ``Content-Length`` is a claim by the
    server and a hostile endpoint will simply lie about it.
    """
    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        buffer = bytearray()
        for chunk in iter_content(chunk_size=8192):
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) >= limit:
                return bytes(buffer[:limit]), True
        return bytes(buffer), False

    raw = getattr(response, "content", None)
    if raw is None:
        raw = getattr(response, "text", "") or ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    return bytes(raw[:limit]), len(raw) > limit


def _clean_search_text(value, limit: int) -> str:
    """Normalise one title/snippet for a prompt. Non-strings become ``""``.

    Runs through :func:`_normalise_whitespace` (which strips control characters
    and collapses whitespace) so an attacker-authored result cannot inject an
    ANSI escape or a newline-laden block into the text a model reads next.
    """
    if not isinstance(value, str):
        return ""
    return _normalise_whitespace(value)[:limit]


def _clean_search_url(value) -> str:
    """A result URL, printable and length-capped. ``""`` when unusable.

    Left as data: this is NOT validated against the SSRF gate here, because the
    URL is only returned, never opened. It becomes a fetch — with the full gate
    — only if the planner passes it to ``web_fetch`` on a later step.
    """
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value.strip() if ch.isprintable())
    return cleaned[:MAX_URL_LEN]


def _parse_search_results(payload, max_results: int) -> list[dict]:
    """SearXNG JSON → a short list of ``{title, url, snippet}``. Never raises.

    Defensive throughout: a malformed reply yields fewer results, never an
    exception. A result with no usable URL is dropped — a lead the planner
    cannot act on is noise.
    """
    results: list[dict] = []
    if not isinstance(payload, dict):
        return results
    raw = payload.get("results")
    if not isinstance(raw, list):
        return results
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = _clean_search_url(item.get("url"))
        if not url:
            continue
        results.append({
            "title": _clean_search_text(item.get("title"), SEARCH_TITLE_MAX),
            "url": url,
            "snippet": _clean_search_text(item.get("content"), SEARCH_SNIPPET_MAX),
        })
        if len(results) >= max_results:
            break
    return results


def _charset(content_type: str) -> str:
    """Charset for decoding, defaulting to UTF-8. Unknown names fall back."""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if not match:
        return "utf-8"
    try:
        "".encode(match.group(1))
    except LookupError:
        return "utf-8"
    return match.group(1)


__all__ = [
    "ToolsmithAgent",
    "ToolsmithInputError",
    "EgressDenied",
    "ALLOWED_CONTENT_TYPES",
    "ALLOWED_PORTS",
    "ALLOWED_URL_SCHEMES",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_SEARCH_RESULTS",
    "EGRESS_INTENTS",
    "HARD_MAX_CHARS",
    "MAX_FILE_BYTES",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "MAX_SEARCH_RESULTS",
    "MAX_WORKSPACE_FILES",
    "SEARXNG_SEARCH_PATH",
    "STATE_CHANGING_INTENTS",
    "TOOL_INTENTS",
    "WORKSPACE_DIRNAME",
    "extract_text",
    "reject_internal_address",
]
