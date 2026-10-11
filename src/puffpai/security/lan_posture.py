"""LAN posture - the ``island`` half of the egress contract, enforced.

:mod:`puffpai.security.egress_doors` closes the doors to the public internet.
This module closes the hole underneath them: a *configured* service URL that
points somewhere it should not.

The ``island`` posture promises "your home network is allowed, the wider
internet is not". Nothing checked that promise against the addresses in the
config, so ``ollama.url = "https://someone-elses-box.example.com"`` was
unrestricted egress that no door saw - and it carries the worst payload in the
product, because every prompt in the household goes to the brain. Same for
``ha.url`` (a long-lived Home Assistant token in an Authorization header),
Node-RED, the media servers and the AdGuard control API (which carries the
household's AdGuard password).

BuildBot has the same module. Two things are deliberately different here.

**It does not re-implement address classification.** BuildBot's copy carried
its own ``ipaddress`` logic. This build already has
:mod:`puffpai.security.netaddr` - the module the Toolsmith's SSRF gate and
:mod:`puffpai.security.lan_http` run on - so every literal is judged there and
every name is resolved with its resolver. One vocabulary, one place to fix.

**Link-local and the odd reserved ranges are PUBLIC here, not private.**
BuildBot folded ``is_link_local`` and ``is_reserved`` in with RFC 1918.
``netaddr`` calls ``169.254.0.0/16`` what it is - the cloud-metadata shape - and
refuses it under every posture, and this module keeps that verdict. The
restrictive direction is the safe one for a check that decides whether a
credential may be sent somewhere.

Two postures, two rules
-----------------------
``sealed``
    Only this machine. Loopback (``127.0.0.1``, ``::1``, ``localhost``) and
    nothing else. **Home Assistant on another host requires ``island``** - that
    is not a bug, it is what ``sealed`` means.
``island`` / ``enumerated``
    Loopback plus the home network: RFC 1918, unique-local IPv6 (``fc00::/7``),
    and names that live inside a house - ``.local``, ``.lan``, ``.home.arpa``,
    ``.internal``, and single-label names - provided they resolve there.
    ``enumerated`` is a superset of ``island``: it adds named WAN doors, it does
    not loosen the LAN.

Two checks, because one of them must not touch the network
----------------------------------------------------------
:func:`classify_host` is a **name-form** check: it reads literal addresses and
judges names by their shape. It performs no DNS and cannot block, which is why
it is the one that runs at config load. Startup must not hang on a resolver.

:func:`classify_host_resolving` resolves and checks **every** address the name
answers with. It is used where a definitive answer matters and a short wait is
acceptable: the wizard step that writes a URL, and ``/system/status``. It is
what catches a single-label name that a search domain quietly sends off-site.

Absence is not a limitation
---------------------------
An **empty** URL is "not configured" and is never a violation - a household
without Home Assistant is a finished build, not a broken one. An
**unresolvable** name is a warning, not a violation, unless its shape was
already public: a ``.local`` box that is merely switched off must not read as a
posture breach, while a registered-looking domain that answers nothing today
says nothing about what it answers tomorrow.

What "refuse" means, per surface
--------------------------------
* **The wizard write path** refuses outright, with one plain sentence. Nothing
  is running against the value yet and the person is standing right there, so
  refusing the write is free - and the common case never reaches the other two
  surfaces.
* **Config load** cannot refuse by raising: a bad string in a config file must
  not make the appliance unbootable. It *neutralises* instead, which is the
  fail-closed action that still leaves a working box - an optional integration
  is forced ``enabled = False``, and a URL with no enable flag is reset to its
  safe default rather than left pointing off-site. Every neutralisation is
  recorded and logged.
* **``/system/status``** publishes what was found, so a neutralisation is
  visible rather than silent.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from puffpai.logging_config import get_logger
from puffpai.security import netaddr

logger = get_logger(__name__)

#: Default resolve timeout. Short on purpose: this runs behind a status route.
RESOLVE_TIMEOUT = 2.0

#: Name suffixes that name something inside a house. A name ending in one of
#: these is presumed LAN by the name-form check and still verified by the
#: resolving check.
_HOUSE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain",
                   ".home", ".intranet", ".private")

_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost")

#: Classification results.
LOOPBACK = "loopback"
PRIVATE = "private"
LAN_PRESUMED = "lan_presumed"
PUBLIC = "public"
UNRESOLVABLE = "unresolvable"
UNPARSEABLE = "unparseable"

#: ``netaddr`` class -> the verdict here. Anything NOT in this table is
#: :data:`PUBLIC`, which is the whole point of writing it as an allowlist:
#: ``None`` (globally routable), ``link_local`` (cloud metadata), ``cgnat``,
#: ``documentation``, ``benchmark``, ``multicast``, ``reserved`` and
#: ``not_global`` all land in the refused column without being enumerated, and
#: a class added to ``netaddr`` later starts out refused rather than allowed.
_FROM_NETADDR: dict[str, str] = {
    "loopback": LOOPBACK,
    # 0.0.0.0 means "this machine" to a connecting client on every stack that
    # resolves it at all, so it is judged with loopback rather than being a
    # third thing that slips between the two rules.
    "unspecified": LOOPBACK,
    "private": PRIVATE,
    "ula": PRIVATE,
}

#: What each posture permits a configured service URL to be. An unknown posture
#: is not in the table and :func:`allowed_classifications` answers ``sealed``.
_ALLOWED: dict[str, frozenset[str]] = {
    "sealed": frozenset({LOOPBACK}),
    "island": frozenset({LOOPBACK, PRIVATE, LAN_PRESUMED}),
    "enumerated": frozenset({LOOPBACK, PRIVATE, LAN_PRESUMED}),
}


class PostureViolation(ValueError):
    """A configured URL is not allowed at this build's run posture.

    ``user_message`` is written to be shown to a person verbatim: it names the
    URL, the posture, and what to do instead. Callers that surface errors over
    HTTP read that attribute; ``str(exc)`` is the operator form.
    """

    def __init__(self, service: str, url: str, posture: str, kind: str,
                 user_message: str, detail: str = ""):
        super().__init__(
            f"{service}: {url} violates posture {posture!r} ({kind})")
        self.service = service
        self.url = url
        self.posture = posture
        self.kind = kind
        self.user_message = user_message
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"service": self.service, "url": self.url,
                "posture": self.posture, "classification": self.kind,
                "detail": self.detail, "message": self.user_message}


@dataclass(frozen=True)
class LanService:
    """One configured LAN destination, and how to neutralise it."""

    id: str
    #: Dotted path as it appears in config, for the message.
    config_path: str
    #: What talks to it, and what it would leak.
    stakes: str
    #: Config section carrying an ``enabled`` flag we can force False, if any.
    enable_section: Optional[str] = None
    #: Safe value to fall back to when there is no enable flag.
    safe_default: Optional[str] = None


#: Every LAN destination this build reads out of config. A new URL field in
#: ``config.py`` belongs here.
SERVICES: tuple[LanService, ...] = (
    LanService(
        id="ollama", config_path="ollama.url",
        stakes="Every prompt anyone in the household types goes to this "
               "address. A public host here is the worst leak in the product.",
        safe_default="http://localhost:11434"),
    LanService(
        id="home_assistant", config_path="ha.url",
        stakes="A long-lived Home Assistant token is sent to this address in "
               "an Authorization header, along with the state of the house.",
        enable_section="ha"),
    LanService(
        id="nodered", config_path="nodered.url",
        stakes="Workflow payloads, which carry whatever the workflow touches.",
        safe_default="http://localhost:1880"),
    LanService(
        id="n8n", config_path="n8n.url",
        stakes="Workflow payloads, which carry whatever the workflow touches.",
        safe_default=""),
    LanService(
        id="adguard", config_path="zone3.adguard_url",
        stakes="The AdGuard admin credential, and every DNS query the "
               "household makes.",
        enable_section="zone3"),
    LanService(
        id="searxng", config_path="tools.searxng_url",
        stakes="Every search term anybody in the house types.",
        safe_default="http://127.0.0.1:8888"),
    LanService(
        id="jellyfin", config_path="media_server.jellyfin_url",
        stakes="A Jellyfin API key and what the household is watching.",
        enable_section="media_server"),
    LanService(
        id="calibre_web", config_path="media_server.calibre_web_url",
        stakes="What the household is reading.",
        enable_section="media_server"),
    # Two the wizard can write that this build has no config section for. They
    # are listed so the write path judges them under their own name and stakes
    # rather than falling back to a bare service id; _urls_from_config finds
    # nothing for them, so they are never neutralised at load.
    LanService(
        id="kiwix", config_path="kiwix.url",
        stakes="Every knowledge-pack query.",
        safe_default=""),
    LanService(
        id="dashboard", config_path="dashboard.url",
        stakes="The address the owner is handed off to. A public one sends "
               "them, and their session, somewhere this build does not run.",
        safe_default=""),
)

_BY_ID: dict[str, LanService] = {s.id: s for s in SERVICES}


def service(service_id: str) -> Optional[LanService]:
    """The service row with this id, or None."""
    return _BY_ID.get(str(service_id).strip().lower())


def service_ids() -> tuple[str, ...]:
    return tuple(s.id for s in SERVICES)


# ── address + name classification ────────────────────────────────────────────

def _classify_address(addr_text: str) -> str:
    """Classify one literal address. Returns '' for a non-address.

    The verdict comes from :func:`puffpai.security.netaddr.classify`, which
    already unwraps IPv4-mapped, 6to4 and Teredo addresses - ``::ffff:127.0.0.1``
    is loopback wearing a costume, and judging only the outer object misses it.
    """
    try:
        address_class = netaddr.classify(addr_text)
    except netaddr.UnparseableAddress:
        return ""
    if address_class is None:
        return PUBLIC                      # globally routable, positively
    return _FROM_NETADDR.get(address_class, PUBLIC)


def host_of(url: str) -> str:
    """The lower-case hostname in *url*, or '' if there is not one."""
    try:
        parts = urlsplit(str(url).strip())
        # Touch .port too: an unbracketed IPv6 literal
        # ("http://2001:db8::1:11434") parses with hostname "2001" and an
        # invalid port, and "2001" is a single label, which this module would
        # otherwise read as a LAN name. A malformed URL must fail closed, not
        # land in the most permissive class.
        parts.port
        host = parts.hostname or ""
    except ValueError:
        return ""
    if ":" in host and not host.startswith("["):
        # urlsplit strips the brackets from a real IPv6 literal, so a colon
        # surviving here means the address was not bracketed.
        if netaddr.parse(host) is None:
            return ""
    return host.strip().rstrip(".").lower()


def classify_host(host: str) -> str:
    """Classify a host by literal address or by name form. Opens no socket.

    This is the check that runs at config load, so it must never block. A name
    it cannot judge from its shape comes back :data:`LAN_PRESUMED` only when the
    shape says "inside a house"; anything that looks like a registered domain is
    :data:`PUBLIC`, because guessing permissively is how the hole got there.
    """
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return UNPARSEABLE
    literal = _classify_address(host)
    if literal:
        return literal
    if host in _LOOPBACK_NAMES:
        return LOOPBACK
    if host.endswith(_HOUSE_SUFFIXES):
        return LAN_PRESUMED
    if "." not in host:
        # A single-label name is a LAN name in every normal setup. A search
        # domain can still send it off-site, which is precisely what the
        # resolving check exists to catch.
        return LAN_PRESUMED
    return PUBLIC


#: Replaces the resolver for every call that does not name one. Exists for the
#: same reason ``egress_doors.set_resolver_override`` does: the test suite must
#: not make real DNS queries, because a household resolver that answers a
#: wildcard for ``homeassistant.local`` would turn a name-form question into a
#: flaky network one. Production never sets it.
_resolver_override = None


def set_resolver_override(resolver) -> None:
    """Install (or clear, with ``None``) the process-wide resolver."""
    global _resolver_override
    _resolver_override = resolver


def _resolve(host: str, port: int) -> list[str]:
    """Every address *host* answers with, through ``netaddr``'s resolver."""
    return list(netaddr.default_resolver(host, port))


def classify_host_resolving(host: str, *, timeout: float = RESOLVE_TIMEOUT,
                            resolver=None, port: int = 0) -> tuple[str, str]:
    """Resolve *host* and classify **every** address it answers with.

    Returns ``(classification, detail)``. The worst answer wins: a name that
    resolves to one private address and one public address is :data:`PUBLIC`,
    because a client connects to whichever the stack hands it.

    A literal address is judged directly and never resolved. A name that
    resolves to nothing is :data:`UNRESOLVABLE`, which callers treat as a
    warning rather than a violation.

    *resolver* takes ``(host, port)`` - the same shape
    :mod:`puffpai.security.netaddr` and :mod:`puffpai.security.egress_doors`
    use, so one stub serves all three in a test.
    """
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return UNPARSEABLE, "no host in the URL"
    literal = _classify_address(host)
    if literal:
        return literal, host
    if host in _LOOPBACK_NAMES:
        return LOOPBACK, host

    previous = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        resolve = resolver or _resolver_override or _resolve
        addresses = resolve(host, port or netaddr.DEFAULT_PORT)
    except (OSError, netaddr.ResolutionError) as exc:
        return UNRESOLVABLE, f"{host} does not resolve ({type(exc).__name__})"
    finally:
        socket.setdefaulttimeout(previous)
    if not addresses:
        return UNRESOLVABLE, f"{host} resolves to nothing"

    seen = {addr: (_classify_address(addr) or PUBLIC) for addr in addresses}
    if PUBLIC in seen.values():
        offenders = sorted(a for a, k in seen.items() if k == PUBLIC)
        return PUBLIC, f"{host} resolves to {', '.join(offenders)}"
    if PRIVATE in seen.values():
        return PRIVATE, f"{host} resolves to {', '.join(sorted(seen))}"
    return LOOPBACK, f"{host} resolves to {', '.join(sorted(seen))}"


# ── posture ──────────────────────────────────────────────────────────────────

#: Posture the wizard answered in THIS run, set at Step 1 before anything is
#: written. An in-process override, never persisted from here: the manifest is
#: still the record, this is only how a run that has not written one yet knows
#: what it is building.
_active_posture: Optional[str] = None


def set_active_posture(posture: Any) -> str:
    """Declare the posture this wizard run is building. Returns what was set.

    Called from ``setup_ops.persist_posture`` - the one function both wizard
    faces go through - so every later write in the same run is judged against
    the answer the owner just gave, rather than against a manifest that does not
    exist yet. Normalised through
    :func:`puffpai.build_posture.assess_posture`, so a junk value becomes the
    safe default rather than being honoured.
    """
    global _active_posture
    from puffpai import build_posture

    if posture is None:
        _active_posture = None
        return ""
    raw = (posture.get("run_egress") if isinstance(posture, dict) else posture)
    _active_posture = str(
        build_posture.assess_posture(None, raw, None)["run_egress"])
    return _active_posture


def clear_active_posture() -> None:
    """Forget the run posture (end of a wizard run, and tests)."""
    global _active_posture
    _active_posture = None


def active_posture() -> Optional[str]:
    """The posture this run declared, or None."""
    return _active_posture


def posture_block_from_config_dir(config_dir: str) -> Optional[dict[str, Any]]:
    """The raw ``posture`` block on disk, or None when there is no manifest.

    The None matters: "no manifest" and "a manifest that says island" are not
    the same fact, and a first run is the first case. Collapsing them is how
    ``--run-egress sealed --ollama-url <public>`` would be accepted on a fresh
    box.
    """
    try:
        with open(os.path.join(config_dir, "build_manifest.json"),
                  "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("posture"), dict):
        return data["posture"]
    return None


def write_path_posture(config_dir: str = "") -> str:
    """The posture a WRITE must be judged against. Defaults to ``sealed``.

    Order: the posture this run declared, then the manifest if one exists, then
    ``sealed``. The last step is the fix for a first run: with no manifest on
    disk there is nothing that says what is being built, and "nothing known"
    must mean the most restrictive rule, not the default one. Refusing a write
    costs a person one clear message; accepting it bakes a public address into a
    box whose owner asked for ``sealed``.
    """
    from puffpai import build_posture

    if _active_posture:
        return _active_posture
    if not config_dir:
        try:
            from puffpai.config import get_config
            config_dir = get_config().CONFIG_DIR
        except Exception:
            return "sealed"
    block = posture_block_from_config_dir(config_dir)
    if block is None:
        return "sealed"
    return str(build_posture.assess_posture(
        block.get("build_network"), block.get("run_egress"),
        None)["run_egress"])


def posture_from_config_dir(config_dir: str) -> str:
    """The build's ``run_egress``, read straight off disk and normalised.

    Deliberately does NOT go through :func:`puffpai.build_manifest.read_manifest`
    or :func:`puffpai.config.get_config`: this is called *from inside* the config
    loader and either route would recurse. It reads the JSON and hands the values
    to :func:`puffpai.build_posture.assess_posture`, which is where the
    fail-closed normalisation already lives.
    """
    from puffpai import build_posture

    block = posture_block_from_config_dir(config_dir) or {}
    settled = build_posture.assess_posture(block.get("build_network"),
                                           block.get("run_egress"), None)
    return str(settled["run_egress"])


def current_posture() -> str:
    """The build's ``run_egress`` for callers outside the config loader."""
    try:
        from puffpai.config import get_config
        return posture_from_config_dir(get_config().CONFIG_DIR)
    except Exception as exc:
        logger.debug("posture unavailable for LAN check: %s", exc)
        from puffpai import build_posture
        return build_posture.DEFAULT_RUN_EGRESS


def allowed_classifications(posture: str) -> frozenset[str]:
    """What a service URL may be at this posture. Unknown posture fails shut."""
    return _ALLOWED.get(str(posture), _ALLOWED["sealed"])


# ── the check ────────────────────────────────────────────────────────────────

def _message(svc_path: str, url: str, host: str, posture: str, detail: str,
             stakes: str) -> str:
    """The sentence a person reads. Names the posture, never jargon-only."""
    where = detail if detail and detail != host else host
    if posture == "sealed":
        rule = ("This build's run posture is 'sealed', which allows nothing "
                "but this machine itself - 127.0.0.1, ::1 or localhost. "
                "Home Assistant, Ollama or a media server on any OTHER host, "
                "even one inside your house, needs the 'island' posture "
                "instead.")
        fix = ("Point it at localhost, or rebuild with the 'island' posture "
               "if this service really does run on another box.")
    else:
        rule = (f"This build's run posture is '{posture}', which allows your "
                f"home network and nothing beyond it.")
        fix = ("Point it at a private address (192.168.x.x, 10.x.x.x, "
               "172.16-31.x.x) or a .local / .lan name on your own network. "
               "There is no posture in this build that lets a core service "
               "live on the public internet.")
    return (f"{svc_path} is set to {url}, and {where} is not on your home "
            f"network. {rule} {stakes} {fix}")


def check_url(service_id: str, url: str, posture: str, *, resolve: bool = False,
              resolver=None, timeout: float = RESOLVE_TIMEOUT
              ) -> Optional[PostureViolation]:
    """Judge one configured URL against *posture*. Returns a violation or None.

    An empty URL is "not configured" and is never a violation. An
    :data:`UNRESOLVABLE` name is a warning, not a violation: a service that is
    switched off must not read as a posture breach.
    """
    url = (url or "").strip()
    if not url:
        return None
    svc = _BY_ID.get(service_id)
    path = svc.config_path if svc else service_id
    stakes = svc.stakes if svc else ""

    host = host_of(url)
    if not host:
        return PostureViolation(
            service_id, url, posture, UNPARSEABLE,
            f"{path} is set to {url!r}, which has no host in it. Nothing can "
            f"check where that points, so it is refused.")

    by_name = classify_host(host)
    if resolve:
        port = 0
        try:
            port = urlsplit(url).port or 0
        except ValueError:
            port = 0
        kind, detail = classify_host_resolving(host, timeout=timeout,
                                               resolver=resolver, port=port)
        if kind == UNRESOLVABLE:
            # Resolving may only ever make the verdict WORSE, never better. A
            # name that looks like a registered domain and answers nothing is
            # still a public name: an NXDOMAIN today says nothing about what it
            # answers tomorrow, and treating "cannot check" as "fine" is how a
            # resolver outage would become a way around this whole module.
            if by_name == PUBLIC:
                kind = PUBLIC
            else:
                # A .local name that is merely switched off is not a breach.
                return None
    else:
        kind, detail = by_name, host
    if kind in allowed_classifications(posture):
        return None
    if kind == UNPARSEABLE:
        return PostureViolation(
            service_id, url, posture, kind,
            f"{path} is set to {url!r}, which has no host in it.")
    if posture == "sealed" and kind in (PRIVATE, LAN_PRESUMED):
        # Not public - just not this machine. Say the right thing.
        return PostureViolation(
            service_id, url, posture, kind,
            f"{path} is set to {url}, which is on your home network but not "
            f"on this machine. This build's run posture is 'sealed', which "
            f"allows nothing outbound at all except this machine itself "
            f"(127.0.0.1 / localhost). Home Assistant, Ollama or a media "
            f"server on another host requires the 'island' posture. "
            f"Point it at localhost, or rebuild with 'island'.",
            detail=detail)
    return PostureViolation(
        service_id, url, posture, kind,
        _message(path, url, host, posture, detail, stakes), detail=detail)


def _urls_from_config(cfg: Any) -> list[tuple[str, str]]:
    """``(service_id, url)`` for every LAN service that is actually in play.

    A service with an ``enabled`` flag that is off is skipped. Its URL is
    usually just the shipped default - ``ha.url`` ships as
    ``http://homeassistant.local:8123``, which is a violation under ``sealed``
    on a box where Home Assistant was never set up - and reporting a breach for
    something nothing calls buries the ones that matter. Anything without an
    enable flag is always checked, because "configured" is the only signal those
    have.
    """
    out: list[tuple[str, str]] = []
    for svc in SERVICES:
        if svc.enable_section:
            block = getattr(cfg, svc.enable_section, None)
            if block is None or not getattr(block, "enabled", False):
                continue
        section, _, field = svc.config_path.partition(".")
        block = getattr(cfg, section, None)
        url = getattr(block, field, "") if block is not None else ""
        out.append((svc.id, str(url or "")))
    return out


def check_config(cfg: Any, posture: str, *, resolve: bool = False,
                 resolver=None, timeout: float = RESOLVE_TIMEOUT
                 ) -> list[PostureViolation]:
    """Every LAN-service violation on this config object, in table order."""
    found = []
    for service_id, url in _urls_from_config(cfg):
        violation = check_url(service_id, url, posture, resolve=resolve,
                              resolver=resolver, timeout=timeout)
        if violation is not None:
            found.append(violation)
    return found


# ── enforcement at config load ───────────────────────────────────────────────

def enforce(cfg: Any, posture: str) -> list[PostureViolation]:
    """Neutralise every violating service on *cfg* in place. Never raises.

    Called from the config loader, where raising would make a bad string in a
    config file enough to stop the appliance booting. Neutralising is the
    fail-closed action that still leaves a working box:

    * a service with an ``enabled`` flag is forced ``enabled = False``, so
      nothing calls it;
    * a service without one is reset to its safe default, so the address is
      gone rather than merely unused.

    Returns the violations, which the loader stores for ``/system/status``. The
    name-form check is used here and never the resolving one: config load must
    not block on a resolver.
    """
    violations = check_config(cfg, posture, resolve=False)
    for violation in violations:
        svc = _BY_ID.get(violation.service)
        if svc is None:
            continue
        if svc.enable_section:
            block = getattr(cfg, svc.enable_section, None)
            if block is not None and getattr(block, "enabled", False):
                block.enabled = False
                violation.detail = (violation.detail + "; "
                                    if violation.detail else "")
                violation.detail += f"{svc.enable_section}.enabled forced False"
        if svc.safe_default is not None:
            section, _, field = svc.config_path.partition(".")
            block = getattr(cfg, section, None)
            if block is not None and hasattr(block, field):
                setattr(block, field, svc.safe_default)
                violation.detail = (violation.detail + "; "
                                    if violation.detail else "")
                violation.detail += (f"{svc.config_path} reset to "
                                     f"{svc.safe_default!r}")
        logger.warning("LAN posture (%s): %s", posture, violation.user_message)
    return violations


# ── the wizard write path ────────────────────────────────────────────────────

def require_allowed(service_id: str, url: str, *, posture: Optional[str] = None,
                    resolve: bool = True, resolver=None,
                    timeout: float = RESOLVE_TIMEOUT) -> None:
    """Raise :class:`PostureViolation` if *url* may not be written. Else return.

    This is the surface that refuses outright, because refusing a write is
    free: nothing is running against the value yet, and the person is right
    there to read the message. Resolves by default - the wizard can afford two
    seconds, and it is the only place a single-label name gets checked before it
    lands in a file.

    The posture comes from :func:`write_path_posture`, which prefers the answer
    THIS run gave over anything on disk and falls back to ``sealed`` rather than
    to the ordinary default.
    """
    violation = check_url(
        service_id, url,
        posture if posture is not None else write_path_posture(),
        resolve=resolve, resolver=resolver, timeout=timeout)
    if violation is not None:
        raise violation


# ── /system/status ───────────────────────────────────────────────────────────

def posture_check(cfg: Any = None, *, resolve: bool = True, resolver=None,
                  timeout: float = RESOLVE_TIMEOUT) -> dict[str, Any]:
    """The ``posture_check`` block for ``/system/status``.

    ``lan_services`` is the string ``"ok"`` when every configured URL is where
    the posture says it may be, and otherwise the list of findings. Resolving
    here rather than trusting the name-form check is the point: this is where a
    single-label name that a search domain sends off-site becomes visible.
    """
    if cfg is None:
        try:
            from puffpai.config import get_config
            cfg = get_config()
        except Exception as exc:
            logger.debug("config unavailable for posture check: %s", exc)
            return {"posture": "unknown", "lan_services": "unknown"}
    # Derive the posture from the config object we were handed, not from the
    # global singleton: the two can be different objects, and reporting one
    # config's URLs against another config's posture is worse than useless.
    config_dir = getattr(cfg, "CONFIG_DIR", "")
    posture = (posture_from_config_dir(config_dir) if config_dir
               else current_posture())
    live = check_config(cfg, posture, resolve=resolve, resolver=resolver,
                        timeout=timeout)
    neutralised = load_time_violations()

    # ``lan_services`` reports the SITUATION, not the tidied-up object in
    # memory. Neutralising at load fixes the running process; it does not touch
    # the config file, so the bad URL is still there and will be read again at
    # every restart. Reporting "ok" because the in-memory copy is clean is the
    # same category of lie as an egress log that says enforced:false.
    findings = [dict(v.as_dict(), state="live") for v in live]
    seen = {(v["service"], v["url"]) for v in findings}
    for v in neutralised:
        entry = v.as_dict()
        if (entry["service"], entry["url"]) not in seen:
            findings.append(dict(entry, state="neutralised_at_load"))

    return {
        "posture": posture,
        "lan_services": "ok" if not findings else findings,
        "live_violations": [v.as_dict() for v in live],
        "neutralised": [v.as_dict() for v in neutralised],
        "checked": [s.config_path for s in SERVICES],
        "note": ("Under 'sealed' only this machine is allowed; under 'island' "
                 "and 'enumerated', your home network and nothing beyond it. "
                 "'lan_services' is \"ok\" only when nothing is violating AND "
                 "nothing had to be neutralised at startup: a neutralised entry "
                 "means the running process is safe but the address is still in "
                 "your config file and will be read again on the next restart."),
    }


# ── what the loader found, for anything that wants to report it ──────────────

_load_violations: list[PostureViolation] = []


def record_load_violations(violations: list[PostureViolation]) -> None:
    """Store what the config loader neutralised, for ``/system/status``."""
    global _load_violations
    _load_violations = list(violations)


def load_time_violations() -> list[PostureViolation]:
    """What the last config load neutralised."""
    return list(_load_violations)


__all__ = [
    "LOOPBACK", "PRIVATE", "LAN_PRESUMED", "PUBLIC", "UNRESOLVABLE",
    "UNPARSEABLE", "RESOLVE_TIMEOUT",
    "LanService", "PostureViolation", "SERVICES",
    "active_posture", "allowed_classifications", "check_config", "check_url",
    "classify_host", "classify_host_resolving", "clear_active_posture",
    "current_posture", "enforce", "host_of", "load_time_violations",
    "posture_check", "posture_from_config_dir", "record_load_violations",
    "require_allowed", "service", "service_ids", "set_active_posture",
    "write_path_posture",
]
