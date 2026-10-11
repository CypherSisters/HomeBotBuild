"""Address policy — the one place that says whether a socket may open.

Why this module exists
----------------------
Two parts of this appliance reach hosts a *caller* chose, and both must defend
against it.

``agents/toolsmith.py`` knows the caller picks the host: it resolves the name,
checks every answer with :mod:`ipaddress`, and refuses loopback, link-local,
private and IPv6-mapped spellings before a socket opens.

``security/egress_doors.py`` declares hostnames chosen by us,
``api.weather.gov`` and eleven friends, and a host check that is only a
string comparison against the door's declared tuple is safe exactly as long
as we own both sides of the comparison. The moment a *manifest* supplies
the hostname (the pack tier), the pack author owns both sides: they publish
``feed.example.invalid A 192.168.1.1``, every declared check passes, and the
appliance opens a socket onto the household LAN with the card still saying
"nothing about you leaves". No DNS-rebinding race is needed; one static A
record does it.

The control lives here, and both modules call it, so there is one
implementation to review and one to keep correct.

What "acceptable" means
-----------------------
:data:`PUBLIC` — the posture every door and every fetch uses today — means the
address must be **globally routable**. That is a positive test
(``ipaddress.is_global``) rather than a blocklist, because a blocklist of
``127.0.0.1`` and ``10.*`` never covers ``0.0.0.0``, ``[::1]``,
``100.64.0.0/10``, ``::ffff:192.168.0.1`` or the next range someone invents.
The named ranges below are checked *as well*, and first, so the refusal can say
which kind of address it was in words rather than only "not globally routable",
and so the answer does not move when a Python release changes what
``is_private`` covers.

:data:`LAN` is the mirror image, reserved for a door that is explicitly declared
to reach the household's own network (``Door.lan``). It accepts RFC 1918 and
unique-local addresses **only**, and refuses a public one — a door that says it
talks to your router must not silently become a door to the internet. Nothing
declares it today; the posture exists so that the day something does, the check
is already written and already tested.

Pinning
-------
Validating the answer to a DNS query and then handing the *name* to an HTTP
client that resolves it again is a time-of-check/time-of-use hole: a hostile
resolver answers public the first time and private the second.
:func:`pinned_session` closes it. It returns a ``requests.Session`` whose
sockets can only be opened to an address this module has just validated, while
the TLS SNI, the certificate check and the ``Host`` header keep the *hostname* —
so pinning costs nothing in correctness and the server cannot tell.

The session refuses rather than degrades: if the pinning machinery cannot be
installed on the transport in front of it, no session is returned and the call
is refused. An unpinned connection that believes it is pinned is worse than one
that knows it is not.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ipaddress
import socket
import threading

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Posture: the address must be globally routable. Everything ships with this.
PUBLIC = "public"

#: Posture: the address must be on the household's own network (RFC 1918 or
#: unique-local) and must NOT be public. Only for a door with ``lan=True``.
LAN = "lan"

#: Posture: the household's own network OR the appliance itself (loopback).
#:
#: Strictly wider than :data:`LAN` and deliberately a SEPARATE name rather than
#: a loosening of it. ``LAN`` means "your router, not my own API server", and
#: the doors depend on exactly that; this posture is for the calls that
#: legitimately target a service running on this box -- the appliance's own
#: AdGuard Home on ``127.0.0.1:3000`` is the whole reason it exists. It is
#: still a closed set: public, link-local (cloud metadata), CGNAT, multicast,
#: reserved and documentation addresses are refused here exactly as under LAN.
HOUSE = "house"

#: Port assumed when a caller does not name one. Every door is https.
DEFAULT_PORT = 443


class AddressRefused(Exception):
    """An address is not one this appliance may open a socket to.

    Deliberately NOT an :class:`OSError` or a :class:`ValueError`: this is
    raised from inside a urllib3 connection factory, and both of those are
    caught-and-retried by the connection pool. A refusal that gets retried is a
    refusal that eventually succeeds.

    ``address_class`` is a member of :data:`ADDRESS_CLASSES` — a closed
    vocabulary, safe to write into the ledger and to show an owner.

    **The resolved address is not in the message.** ``str(exc)`` names the
    SUBJECT — the hostname that was looked up, or the literal the caller typed —
    and the class, and stops there; the address lives in ``.address`` and
    nothing logs it. This is structural on purpose. With the address
    interpolated into the message, every caller that put that message in a
    ledger line, a reply or a traceback would write the household's own network
    topology into a file nobody thinks of as sensitive. Making the default-safe
    thing the only easy thing is cheaper than auditing each caller forever.
    """

    def __init__(self, address_class: str, description: str,
                 subject: str = "", address: str = "") -> None:
        super().__init__(f"{subject} {description}".strip())
        self.address_class = address_class
        self.description = description
        self.subject = subject
        #: The address that failed. For a debugger, never for a log.
        self.address = address


class UnparseableAddress(ValueError):
    """The text handed in is not an address at all."""


class ResolutionError(Exception):
    """A hostname could not be resolved, so nothing may be assumed about it."""


# ── the vocabulary ───────────────────────────────────────────────────────────

#: ``class -> description``. The description is appended to a location string
#: ("``evil.example.com → 10.0.0.1``") to make a refusal that explains itself.
#: The first seven strings are load-bearing: ``agents/toolsmith.py`` has raised
#: exactly these words since the SSRF hardening landed and its battery asserts
#: on the fragments. Do not reword them without changing those tests.
ADDRESS_CLASSES: dict[str, str] = {
    "loopback": "is loopback",
    "link_local": "is link-local (cloud metadata range)",
    "multicast": "is multicast",
    "unspecified": "is unspecified",
    "private": "is a private/internal address",
    "reserved": "is reserved",
    "not_global": "is not globally routable",
    "cgnat": "is in the carrier-grade NAT range",
    "ula": "is a unique-local address",
    "documentation": "is in a documentation-only range",
    "benchmark": "is in a benchmark-only range",
    "public": "is a public address, and this door may only reach this house",
}

#: Ranges named explicitly rather than left to ``is_private``/``is_global``.
#: Two reasons. One: what those two attributes cover has moved between Python
#: releases (100.64.0.0/10 and the documentation ranges among them), and a
#: security answer that changes when the interpreter is upgraded is not an
#: answer. Two: naming them lets the refusal say *which* range, which is the
#: difference between an owner understanding a refusal and filing a bug.
_NAMED_RANGES: tuple[tuple[str, object], ...] = (
    ("cgnat", ipaddress.ip_network("100.64.0.0/10")),
    ("ula", ipaddress.ip_network("fc00::/7")),
    ("documentation", ipaddress.ip_network("192.0.2.0/24")),
    ("documentation", ipaddress.ip_network("198.51.100.0/24")),
    ("documentation", ipaddress.ip_network("203.0.113.0/24")),
    ("documentation", ipaddress.ip_network("2001:db8::/32")),
    ("benchmark", ipaddress.ip_network("198.18.0.0/15")),
    ("benchmark", ipaddress.ip_network("2001:2::/48")),
)

#: Checked after the named ranges, outer loop on the property so that an
#: IPv4-mapped loopback — which is *both* loopback and private — is refused with
#: the more specific of the two words.
_ATTRIBUTE_CHECKS: tuple[tuple[str, str], ...] = (
    ("is_loopback", "loopback"),
    ("is_link_local", "link_local"),
    ("is_multicast", "multicast"),
    ("is_unspecified", "unspecified"),
    ("is_private", "private"),
    ("is_reserved", "reserved"),
)

#: Classes a ``lan=True`` door may reach. Loopback is NOT one of them: "this
#: door talks to your router" is not consent to talk to the appliance's own
#: API server, and a link-local address is the cloud-metadata shape whatever
#: the door claims.
_LAN_ALLOWED = ("private", "ula")

#: Classes a :data:`HOUSE` caller may reach: the LAN set plus this machine.
#: Loopback is listed here and NOWHERE else, so "may this reach 127.0.0.1"
#: has exactly one answer in this tree and it is written down.
_HOUSE_ALLOWED = _LAN_ALLOWED + ("loopback",)


def parse(text) -> object | None:
    """The address *text* names, or ``None`` when it is not an address.

    The scope id of an IPv6 literal (``fe80::1%eth0``) is dropped first, so a
    link-local address cannot smuggle itself past the parser by carrying one.
    """
    try:
        return ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return None


def unwrap(addr) -> list:
    """*addr* and every address it encodes.

    ``::ffff:127.0.0.1`` and ``2002:7f00:0001::`` are loopback wearing a hat;
    checking only the outer IPv6 object misses both, and a Teredo address
    carries two more.
    """
    seen = [addr]
    for attr in ("ipv4_mapped", "sixtofour"):
        inner = getattr(addr, attr, None)
        if inner is not None:
            seen.append(inner)
    teredo = getattr(addr, "teredo", None)
    if teredo:
        seen.extend(teredo)
    return seen


def _in_network(addr, network) -> bool:
    """``addr in network``, False across address families instead of raising."""
    try:
        return addr in network
    except TypeError:
        return False


def classify(text) -> str | None:
    """The class of address *text* names, or ``None`` when it is public.

    ``None`` is the only acceptable answer under :data:`PUBLIC`, and it is
    reached only when every address the argument encodes is globally routable —
    a positive test, not the absence of a blocklist hit.

    Raises :class:`UnparseableAddress` when *text* is not an address, because
    "we could not tell" must never read as "it was fine".
    """
    addr = parse(text)
    if addr is None:
        raise UnparseableAddress(f"{text!r} is not an address this can check")
    candidates = unwrap(addr)

    for name, network in _NAMED_RANGES:
        if any(_in_network(candidate, network) for candidate in candidates):
            return name
    for attribute, name in _ATTRIBUTE_CHECKS:
        if any(getattr(candidate, attribute, False) for candidate in candidates):
            return name
    if not all(getattr(candidate, "is_global", False)
               for candidate in candidates):
        return "not_global"
    return None


def describe(address_class: str) -> str:
    """The plain-English half of a refusal for *address_class*."""
    return ADDRESS_CLASSES.get(address_class, "is not an address this may reach")


def check(text, *, posture: str = PUBLIC, host: str = "") -> None:
    """Refuse *text* unless it is acceptable under *posture*. Returns None.

    *host*, when given, is the NAME that was looked up, and it becomes the
    subject of the refusal: ``"evil.example.com is a private/internal address"``.
    The address it resolved to is carried on the exception as ``.address`` and
    is deliberately absent from the message — see :class:`AddressRefused`. When
    no *host* is given, *text* was a literal the caller typed and it is its own
    subject, because refusing "127.0.0.1" without saying "127.0.0.1" would be
    unreadable and discloses nothing the caller did not already write.
    """
    address_class = classify(text)
    addr = parse(text)
    shown = str(addr) if addr is not None else str(text)
    subject = host or shown

    if posture in (LAN, HOUSE):
        allowed = _HOUSE_ALLOWED if posture == HOUSE else _LAN_ALLOWED
        if address_class in allowed:
            return
        # A public address behind a door that promised to stay indoors is the
        # failure this posture exists to catch, and it deserves its own word.
        refused = address_class or "public"
        raise AddressRefused(refused, describe(refused), subject, shown)

    if address_class is not None:
        raise AddressRefused(address_class, describe(address_class),
                             subject, shown)


# ── resolution ───────────────────────────────────────────────────────────────

#: Ceiling on one system lookup. glibc's own default (5 s, two attempts, per
#: nameserver) turns a house with its internet unplugged into a 10 s+ stall
#: before any door can say "I couldn't reach it". A lookup that has not
#: answered by now is treated exactly like one that failed: unresolvable.
RESOLVE_TIMEOUT_S = 3.0

#: Ceiling on lookups still running in the background after their caller gave
#: up (see :func:`default_resolver`). Past it, new lookups are refused at once
#: rather than piling up threads while DNS is down.
MAX_PENDING_LOOKUPS = 32

_PENDING_LOOKUPS = threading.BoundedSemaphore(MAX_PENDING_LOOKUPS)


def _system_lookup(host: str, port: int):
    """``socket.getaddrinfo`` bounded by :data:`RESOLVE_TIMEOUT_S`.

    ``getaddrinfo`` cannot be interrupted from Python, so it runs on a daemon
    worker thread and the caller waits at most the cap. A worker that is still
    stuck when the caller gives up is left to finish on its own: it returns
    when the C library's own resolver timeout fires, drops its result into a
    dict nothing reads any more, releases its slot and exits. It holds no lock
    the caller needs and, being a daemon, never blocks interpreter shutdown.
    :data:`MAX_PENDING_LOOKUPS` bounds how many such stragglers can exist.

    Whatever the worker raised is re-raised here unchanged, so the only
    behaviour that differs from a direct call is the time limit.
    """
    slots = _PENDING_LOOKUPS  # release the very object we acquired
    if not slots.acquire(blocking=False):
        raise socket.gaierror(socket.EAI_AGAIN, "too many lookups pending")
    box: dict = {}
    done = threading.Event()

    def _work():
        try:
            box["infos"] = socket.getaddrinfo(host, port,
                                              type=socket.SOCK_STREAM)
        except BaseException as exc:  # handed to the caller, raised there
            box["error"] = exc
        finally:
            done.set()
            slots.release()

    try:
        worker = threading.Thread(target=_work, name="puffpai-dns",
                                  daemon=True)
        worker.start()
    except BaseException:
        slots.release()
        raise
    if not done.wait(RESOLVE_TIMEOUT_S):
        raise socket.timeout("lookup timed out")
    if "error" in box:
        raise box["error"]
    if "infos" not in box:  # pragma: no cover - the worker always sets one
        raise socket.gaierror(socket.EAI_FAIL, "lookup produced no answer")
    return box["infos"]


def _local_lookup_flags(host) -> int | None:
    """``getaddrinfo`` flags for a host that needs no DNS, or None.

    An IP literal (anything :func:`ipaddress.ip_address` accepts, scope id
    included) gets ``AI_NUMERICHOST``: the C library then parses it and never
    queries a server, and the answer is the same as without the flag.
    ``localhost`` is answered from the hosts file / the local stub. Both run on
    the calling thread and take no slot, so a DNS outage that has filled every
    slot with stalled lookups cannot stop LAN calls by address (lan_http to
    AdGuard, loopback services). Bracketed IPv6 is NOT treated as a literal:
    ``urlsplit().hostname`` and urllib3 both strip the brackets before calling
    here, and a bracketed string is not something ``getaddrinfo`` parses as
    numeric, so it keeps today's path and today's answer.
    """
    if not isinstance(host, str) or not host:
        return None
    if host.lower() == "localhost":
        return 0
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return socket.AI_NUMERICHOST


def default_resolver(host: str, port: int) -> list[str]:
    """Every address *host* resolves to, as strings. Never returns empty.

    Bounded by :data:`RESOLVE_TIMEOUT_S`; a lookup that runs past it raises
    :class:`ResolutionError`, the same as one that failed. IP literals and
    ``localhost`` skip the worker (see :func:`_local_lookup_flags`).
    """
    try:
        flags = _local_lookup_flags(host)
        if flags is None:
            infos = _system_lookup(host, port)
        elif flags:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                       flags=flags)
        else:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ResolutionError(f"cannot resolve {host!r} ({exc})") from None
    addresses = [info[4][0] for info in infos if info and info[4]]
    if not addresses:
        raise ResolutionError(f"cannot resolve {host!r} (no addresses)")
    return addresses


def validated_addresses(host: str, port: int = DEFAULT_PORT, *,
                        posture: str = PUBLIC, resolver=None) -> tuple[str, ...]:
    """Resolve *host* and prove that **every** answer is acceptable.

    Every one, not the first: a name with a public A record and a loopback AAAA
    record is a name whose address the *client* picks, so one bad answer in the
    set is a bad name. An empty answer set is a refusal too — "we could not
    resolve it" is not "it was fine".

    Returns the addresses in resolver order so a caller can pin to them.
    """
    resolve = resolver or default_resolver
    addresses = list(resolve(host, port) or [])
    if not addresses:
        raise ResolutionError(f"cannot resolve {host!r} (no addresses)")
    for address in addresses:
        check(address, posture=posture, host=host)
    return tuple(addresses)


# ── pinning ──────────────────────────────────────────────────────────────────

def _pinned_adapter_classes(policy):
    """Build the urllib3 pool/connection classes that honour *policy*.

    *policy* is ``(host, port) -> [address, ...]``: it resolves, validates and
    raises :class:`AddressRefused` itself. Built per session so each one closes
    over its own posture.

    The whole pin is the override of ``_new_conn``. urllib3 builds its socket
    there from ``self._dns_host``; everything that identifies the peer — the
    SNI server name, the certificate hostname check, the ``Host`` header — is
    taken from ``self.host`` in ``connect()`` and in ``putrequest()``, which
    this does not touch. So the connection goes to an address we validated and
    the TLS handshake is still the handshake for the name we asked for.
    """
    from urllib3 import connectionpool
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.util import connection as urllib3_connection

    def _new_conn(self):
        if getattr(self, "proxy", None) is not None:
            # A proxy means the socket goes somewhere other than the host we
            # validated, and this appliance configures none. Refuse rather than
            # silently un-pin.
            raise AddressRefused(
                "not_global", "cannot be reached through a proxy on this "
                              "appliance", str(getattr(self, "host", "")), "")
        port = self.port or DEFAULT_PORT
        addresses = policy(self.host, port)
        last: Exception | None = None
        for address in addresses:
            try:
                return urllib3_connection.create_connection(
                    (address, port), self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options)
            except OSError as exc:  # try the next validated address
                last = exc
        raise last if last is not None else OSError(
            f"no validated address for {self.host}")

    pinned_http = type("PinnedHTTPConnection", (HTTPConnection,),
                       {"_new_conn": _new_conn})
    pinned_https = type("PinnedHTTPSConnection", (HTTPSConnection,),
                        {"_new_conn": _new_conn})
    return {
        "http": type("PinnedHTTPConnectionPool",
                     (connectionpool.HTTPConnectionPool,),
                     {"ConnectionCls": pinned_http}),
        "https": type("PinnedHTTPSConnectionPool",
                      (connectionpool.HTTPSConnectionPool,),
                      {"ConnectionCls": pinned_https}),
    }


def make_pinned_adapter(policy):
    """A ``requests`` adapter whose sockets can only reach validated addresses.

    Raises :class:`RuntimeError` if the pin cannot be installed — a transport
    this module does not recognise must not be used unpinned while the caller
    believes otherwise.
    """
    from requests.adapters import HTTPAdapter

    pools = _pinned_adapter_classes(policy)

    class PinnedHTTPAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            manager = self.poolmanager
            if not hasattr(manager, "pool_classes_by_scheme"):
                raise RuntimeError(
                    "this urllib3 has no pool_classes_by_scheme; the "
                    "address pin cannot be installed")
            manager.pool_classes_by_scheme = dict(pools)

        def proxy_manager_for(self, *args, **kwargs):
            raise AddressRefused(
                "not_global",
                "cannot be reached through a proxy on this appliance", "", "")

    adapter = PinnedHTTPAdapter()

    # Prove the pin, do not assume it. Checking that the attribute was SET only
    # says this urllib3 still has the field; it does not say the pool manager
    # still consults it. So ask the manager for a pool and look at what it hands
    # back. No socket is opened — connection_from_url builds and caches a pool
    # object, nothing more — and the cache is dropped again immediately.
    #
    # This is the load-bearing half of failing closed. A version of urllib3 that
    # kept the field and stopped reading it would otherwise sail through the
    # attribute check and return an UNPINNED session that every caller believes
    # is pinned, which is strictly worse than refusing.
    #
    # ``connection_from_host`` rather than ``connection_from_url``: a URL
    # literal in ``src/puffpai`` is a fetch target as far as
    # ``test_egress_door_registry.test_every_wan_hostname_in_the_tree_belongs_to_a_door``
    # is concerned, and that test is right to say so. This is not a target. The
    # name is in the RFC 6761 ``.invalid`` TLD, which is guaranteed never to
    # resolve, and no request is ever made through the pool this returns.
    try:
        probe = adapter.poolmanager.connection_from_host(
            "pin-selftest.invalid", port=443, scheme="https")
        pinned_ok = type(probe).ConnectionCls is pools["https"].ConnectionCls
        adapter.poolmanager.clear()
    except Exception as exc:
        raise RuntimeError(
            f"the address pin could not be verified on this urllib3: "
            f"{type(exc).__name__}") from None
    if not pinned_ok:
        raise RuntimeError("the address pin did not install on this urllib3")
    return adapter


def pinned_session(*, posture: str = PUBLIC, resolver=None):
    """A ``requests.Session`` that can only connect to validated addresses.

    The caller still validates before it calls: this exists to close the window
    *between* that check and the socket, not to replace it. Refusing here is the
    rebinding case, and it is the reason the check cannot simply be "look it up
    once and trust the client to look it up the same way".
    """
    import requests

    def policy(host: str, port: int) -> tuple[str, ...]:
        return validated_addresses(host, port, posture=posture,
                                   resolver=resolver)

    adapter = make_pinned_adapter(policy)
    session = requests.Session()
    # Mount over BOTH schemes, including the default adapters requests installs
    # in Session.__init__, so nothing can fall through to an unpinned one.
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # No environment trust. ``requests`` otherwise reads HTTP_PROXY/HTTPS_PROXY
    # and ``~/.netrc`` for every request: the first would route the socket
    # somewhere this module never validated (the adapter refuses a proxy, so
    # that direction merely fails), and the second would attach whatever
    # credential the netrc holds for the host — to a page a pack, an LLM or a
    # redirect chose. Neither is anything a door needs.
    session.trust_env = False

    # No cookie jar. A session is reused across fetches, and a jar makes the
    # appliance carry an identifier from one page to the next and hand it back
    # to a site that set it. The doors send household data only where the card
    # says they do.
    try:
        from http.cookiejar import DefaultCookiePolicy
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    except Exception:  # pragma: no cover - transport specific
        pass
    return session


# ── binds, as opposed to destinations ────────────────────────────────────────
#
# Everything above judges where this appliance may CONNECT. This last pair
# judges what it may LISTEN on, which is the mirror-image question and the one
# that decides whether the rest of the house — or the rest of the internet, on a
# box with a forwarded port — can reach the API at all.
#
# It lives here rather than in either entry point because there are two of them:
# ``start_api.py`` (the service) and ``api_server.py``'s ``__main__`` (what a
# developer runs). Only the first warned, so the entry point most likely to be
# started by hand with ``PUFFPAI_API_BIND=0.0.0.0`` was the silent one.

#: Bind addresses that reach ONLY this host. Everything else is the LAN (or
#: wider), including "" and "0.0.0.0", which are every interface.
_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})


def is_loopback_bind(host) -> bool:
    """True when binding *host* exposes the API to this machine only.

    Anything unrecognised is treated as NOT loopback: a bind address that
    cannot be classified must produce the warning, never suppress it. That is
    the whole design rule here — this function's answer only ever silences a
    message, so the failure that matters is a false ``True``.
    """
    text = str(host or "").strip().lower()
    if text in _LOOPBACK_BINDS:
        return True
    # 127.0.0.0/8 in full, so 127.0.1.1 (Debian's own hostname address) and
    # friends are recognised as host-only too.
    return text.startswith("127.") and text.count(".") == 3


def is_loopback_peer(text) -> bool:
    """True when *text* is the address of a client on THIS machine.

    Distinct from :func:`is_loopback_bind`, which answers a question about a
    LISTEN address and therefore has to treat ``""``/``0.0.0.0`` as the LAN.
    This one grades a PEER address (``request.remote_addr``) and is the
    predicate behind the first-run setup-code exemption, so it fails closed in
    the other direction: anything it cannot parse as a loopback IP is NOT
    loopback. A hostname is never loopback here either — peers arrive as
    addresses, and accepting the string "localhost" would let a forwarded-header
    value assert locality with a word instead of an address.

    IPv4-mapped IPv6 (``::ffff:127.0.0.1``) is unwrapped, because that is how a
    dual-stack listener reports a v4 loopback client.
    """
    raw = str(text or "").strip().strip("[]")
    if not raw:
        return False
    if "%" in raw:                      # scoped IPv6, e.g. fe80::1%eth0
        raw = raw.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_loopback)


#: The one sentence both entry points print. Takes ``host`` and ``port``.
NON_LOOPBACK_BIND_WARNING = (
    "SECURITY: binding %s:%d - NOT loopback. Every host that can reach this "
    "address can reach the backend, and the API key is the only control in "
    "front of it (this build ships no host packet filter). Set "
    "PUFFPAI_API_BIND=127.0.0.1 unless an off-box client really needs it; "
    "prefer one interface address over 0.0.0.0.")


__all__ = [
    "NON_LOOPBACK_BIND_WARNING", "is_loopback_bind",
    "is_loopback_peer",
    "ADDRESS_CLASSES",
    "AddressRefused",
    "DEFAULT_PORT",
    "HOUSE",
    "LAN",
    "PUBLIC",
    "ResolutionError",
    "UnparseableAddress",
    "check",
    "classify",
    "default_resolver",
    "describe",
    "make_pinned_adapter",
    "parse",
    "pinned_session",
    "unwrap",
    "validated_addresses",
]
