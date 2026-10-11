"""The only way out of the jail, and the only place a skill's reach is decided.

A jailed skill has no network namespace, so it cannot make a request; it can
only ASK the host to. This module is that host side. Every answer it gives is
the result of a decision it made, never a pass-through:

* **Host set first, before anything resolves a name.** The URL is checked
  against the manifest's declared hosts and against the door's own flag before
  :func:`puffpai.security.egress_doors.egress_request` is called at all. A shut
  door is a refusal with no DNS query behind it, which is the difference between
  "we blocked it" and "we told the world what it wanted".
* **GET only, https only, query capped, request count capped, response capped.**
  A skill's declared limits may go under these and never over.
* **One ledger line per request**, written by the door layer, content-free.

Two brokers, one protocol. :class:`LiveBroker` goes through the door;
:class:`FixtureBroker` answers only from ``tests_fixtures.json`` and refuses
every other URL, which is what makes "no network in tests" a property rather
than a convention.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Ceiling on the whole URL and on its query string. A skill's outbound channel
#: is a GET, and a GET's only payload is its URL: cap the URL and you have
#: capped how much a compromised skill can push out through an approved host.
#: The whole URL, and separately its query. The URL ceiling is the larger of
#: the two on purpose: the query cap is the primary limit, and a URL cap
#: at the same number would swallow it (a 2 KB query is always a >2 KB URL), so
#: the narrower rule would never be the one that fired or the one that explained
#: itself.
MAX_URL_BYTES = 4096
MAX_QUERY_BYTES = 2048

#: ``(connect, read)`` for ``requests``. The connect half is short on purpose:
#: with the house internet down, a dropped SYN is the common case and someone
#: is standing there waiting for an answer. 3.05 rather than 3 follows the
#: ``requests`` docs (just over a TCP retransmit window). The read half is per
#: socket read, not for the whole body.
FETCH_TIMEOUT_S = (3.05, 7.0)

#: Refusal text for a TLS failure. Public so dispatch can say something true
#: about it rather than "you didn't give permission".
TLS_REFUSAL = "the secure connection to that host could not be verified"

#: Prefixes on entries in :attr:`Broker.failures`. Closed vocabulary; the rest
#: of an entry is an exception TYPE name or an HTTP status, never a message.
FAILURE_NETWORK = "network"
FAILURE_SERVICE = "service"
FAILURE_SETUP = "setup"


class BrokerRefusal(Exception):
    """A request the broker will not make. Carries the sentence for the log."""


class BrokerFailure(Exception):
    """A request the broker was allowed to make that did not get an answer.

    Kept apart from :class:`BrokerRefusal` on purpose. A refusal means the
    skill reached for something the owner did not approve, and it switches the
    skill off. A failure means an APPROVED host could not be reached or did not
    answer properly (no internet, DNS down, a 5xx, a 429). That says nothing
    about the skill, and switching it off would turn a lost cable into a
    permission change the owner has to undo.
    """


def _is_tls_error(exc) -> bool:
    """True when *exc*, or anything it wraps, is a TLS/certificate failure.

    Checked BEFORE the transport test because ``requests.exceptions.SSLError``
    is a ``ConnectionError`` and ``ssl.SSLError`` is an ``OSError``: without
    this, a certificate that failed to verify would be filed as "offline".
    """
    import ssl
    tls_types: tuple = (ssl.SSLError, ssl.CertificateError)
    try:
        import requests
        tls_types += (requests.exceptions.SSLError,)
    except Exception:  # pragma: no cover - requests is a runtime dependency
        pass
    try:
        import urllib3
        tls_types += (urllib3.exceptions.SSLError,)
    except Exception:  # pragma: no cover
        pass
    current, depth = exc, 0
    while current is not None and depth < 6:
        if isinstance(current, tls_types):
            return True
        if isinstance(getattr(current, "reason", None), tls_types):
            return True
        args = getattr(current, "args", ()) or ()
        if args and isinstance(getattr(args[0], "reason", None), tls_types):
            return True
        current = current.__cause__ or current.__context__
        depth += 1
    return False


def _transport_failure(exc) -> str:
    """The failure entry for *exc*, or ``""`` when it is not a plain transport
    failure.

    Honest scope: ``requests.RequestException`` subclasses ``OSError``, so the
    ``OSError`` test below files EVERY ``requests`` error as a failure, not
    only the connection and timeout ones named in the tuple. The ones that
    are not transport failures cannot reach this today:

    * ``TooManyRedirects``: ``egress_request`` passes ``allow_redirects=False``
      and runs its own hop loop, which raises ``EgressBlocked`` (a refusal)
      past ``MAX_REDIRECTS``.
    * ``InvalidURL`` / ``MissingSchema`` / ``InvalidSchema``: :meth:`Broker.check`
      and the door's own URL check validate scheme, host and port first.
    * ``ProxyError``: the pinned session sets ``trust_env = False`` and its
      adapter refuses any proxy with ``AddressRefused`` (a refusal).
    * ``ContentDecodingError``: only from a garbled compressed body sent by
      an already-approved host, which is that host misbehaving, not the
      skill reaching anywhere new.

    TLS errors (also ``OSError``/``ConnectionError`` subclasses) are caught
    first and stay refusals. A new ``requests`` call path that could raise one
    of the above for a policy reason must be classified explicitly here.
    """
    if _is_tls_error(exc):
        return ""
    from puffpai.security import egress_doors as _doors
    from puffpai.security import netaddr as _netaddr
    if isinstance(exc, _doors.EgressBlocked):
        # Only "the name did not resolve". Every other EgressBlocked (shut
        # door, undeclared host, private or unparseable address, pin refusal)
        # is the door saying no, and stays a refusal.
        reason = str(getattr(exc, "reason", "") or "")
        if reason.startswith(_doors.REASON_UNRESOLVABLE):
            return f"{FAILURE_NETWORK}: unresolvable"
        return ""
    if isinstance(exc, _netaddr.ResolutionError):
        # The pinned adapter resolves again at connect time; a lookup that
        # fails THERE surfaces as this rather than as EgressBlocked.
        return f"{FAILURE_NETWORK}: unresolvable"
    transport: tuple = (OSError,)
    try:
        import requests
        transport += (requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout,
                      requests.exceptions.ChunkedEncodingError)
    except Exception:  # pragma: no cover
        pass
    if isinstance(exc, transport):
        return f"{FAILURE_NETWORK}: {type(exc).__name__}"
    return ""


def _clean_host(value) -> str:
    return value.strip().rstrip(".").lower() if isinstance(value, str) else ""


class Broker:
    """Shared policy. Subclasses answer :meth:`_deliver`, never :meth:`handle`."""

    def __init__(self, skill_id: str, hosts, limits: dict | None = None):
        self.skill_id = str(skill_id)
        self.hosts = tuple(dict.fromkeys(_clean_host(h) for h in (hosts or ())
                                         if _clean_host(h)))
        limits = limits or {}
        self.max_requests = int(limits.get("max_requests", 4))
        self.max_response_bytes = int(limits.get("max_response_bytes", 256 * 1024))
        self.count = 0
        self.refusals: list = []
        #: Transport failures on approved requests. See :class:`BrokerFailure`.
        self.failures: list = []

    # ── policy ──────────────────────────────────────────────────────

    def check(self, url: str) -> str:
        """Validate one URL against the skill's declared reach. Returns the host."""
        if not isinstance(url, str) or not url:
            raise BrokerRefusal("that is not a URL")
        if len(url.encode("utf-8", "replace")) > MAX_URL_BYTES:
            raise BrokerRefusal("that URL is longer than a skill may send")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise BrokerRefusal(
                f"{parts.scheme or 'that'} is not https; skills reach https "
                f"hosts and nothing else")
        if parts.username or parts.password:
            raise BrokerRefusal("a URL with credentials in it is refused")
        if parts.port not in (None, 443):
            raise BrokerRefusal(f"port {parts.port} is not 443")
        host = _clean_host(parts.hostname)
        if not host or host not in self.hosts:
            raise BrokerRefusal(
                f"{host or 'that host'} is not one of the hosts you approved "
                f"for this skill ({', '.join(self.hosts) or 'none'})")
        if len(parts.query.encode("utf-8", "replace")) > MAX_QUERY_BYTES:
            raise BrokerRefusal("that query string is longer than a skill may send")
        return host

    def handle(self, message: dict) -> dict:
        """Answer one ``{"t": "fetch"}`` frame. Never raises."""
        url = message.get("url")
        kind = "text" if message.get("kind") == "text" else "json"
        try:
            if self.count >= self.max_requests:
                raise BrokerRefusal(
                    f"this skill declared {self.max_requests} requests and has "
                    f"used them")
            host = self.check(url)
            self.count += 1
            body = self._deliver(url, host)
        except BrokerRefusal as exc:
            self.refusals.append(str(exc))
            logger.warning("skills: refused a request for %s: %s",
                           self.skill_id, exc)
            return {"ok": False, "error": str(exc)}
        except BrokerFailure as exc:
            self.failures.append(str(exc))
            logger.warning("skills: request for %s got no answer (%s)",
                           self.skill_id, exc)
            return {"ok": False,
                    "error": "that host could not be reached just now"}
        except Exception as exc:
            failure = _transport_failure(exc)
            if failure:
                self.failures.append(failure)
                logger.warning("skills: request for %s could not reach the "
                               "network (%s)", self.skill_id, failure)
                return {"ok": False,
                        "error": "that host could not be reached just now"}
            if _is_tls_error(exc):
                # Fail closed. A certificate that does not verify is what a
                # man in the middle, a captive portal or a hijacked name looks
                # like from here, and none of those is "just offline". It
                # stays a refusal, so the skill is switched off (door shut)
                # until the owner looks.
                self.refusals.append(TLS_REFUSAL)
                logger.warning("skills: TLS failure for %s: %s",
                               self.skill_id, type(exc).__name__)
                return {"ok": False, "error": TLS_REFUSAL}
            self.refusals.append(type(exc).__name__)
            logger.warning("skills: request for %s failed: %s",
                           self.skill_id, type(exc).__name__)
            return {"ok": False, "error": f"the request failed ({type(exc).__name__})"}

        if len(body) > self.max_response_bytes:
            self.refusals.append("response over the declared size")
            return {"ok": False,
                    "error": ("the answer was bigger than this skill declared "
                              "it would read")}
        text = body.decode("utf-8", "replace")
        if kind == "text":
            return {"ok": True, "text": text}
        try:
            return {"ok": True, "json": json.loads(text)}
        except ValueError:
            return {"ok": False, "error": "that host did not answer with JSON"}

    def _deliver(self, url: str, host: str) -> bytes:  # pragma: no cover
        raise NotImplementedError


class LiveBroker(Broker):
    """Through the door, or not at all."""

    def __init__(self, skill_id: str, hosts, limits=None, *, door_id=None,
                 egress=None):
        super().__init__(skill_id, hosts, limits)
        from puffpai.security import egress_doors as _doors
        self._doors = egress or _doors
        self.door_id = door_id or self._doors.skill_door_id(self.skill_id, 0)

    def _deliver(self, url: str, host: str) -> bytes:
        allowed, reason = self._doors.egress_allowed(self.door_id)
        find = getattr(self._doors, "door", None)
        if not allowed and find is not None and find(self.door_id) is None:
            # No door by that name in this process: a registration fault on
            # our side, not the owner saying no. Nothing is contacted either
            # way (egress_request would refuse an unknown door), but it must
            # not switch the skill off or clear the owner's flag.
            logger.warning("skills: door %s for %s is not registered; this is "
                           "a registration fault, not a refusal",
                           self.door_id, self.skill_id)
            raise BrokerFailure(f"{FAILURE_SETUP}: door not registered")
        if not allowed:
            # Refused BEFORE egress_request, so nothing resolves the name. The
            # door layer would refuse too; asking first is what makes "shut
            # door, no DNS" testable without a network.
            raise BrokerRefusal(
                f"the door for this skill is shut, so I did not contact "
                f"{host} ({reason})")
        resp = self._doors.egress_request(
            self.door_id, "GET", url, timeout=FETCH_TIMEOUT_S, stream=True)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            try:
                resp.close()
            except Exception:  # pragma: no cover
                pass
            if status >= 500 or status == 429:
                # The approved host was reached and is having a bad moment.
                # Not the skill's doing, so not a reason to switch it off.
                raise BrokerFailure(f"{FAILURE_SERVICE}: {status}")
            raise BrokerRefusal(f"{host} answered {status}")
        declared = getattr(resp, "headers", {}) or {}
        try:
            length = int(declared.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > self.max_response_bytes:
            raise BrokerRefusal(
                "that answer is bigger than this skill declared it would read")
        return self._read_capped(resp)

    def _read_capped(self, resp) -> bytes:
        cap = self.max_response_bytes
        iterator = getattr(resp, "iter_content", None)
        if iterator is None:
            return bytes(getattr(resp, "content", b"") or b"")[:cap + 1]
        chunks: list = []
        total = 0
        for chunk in iterator(8192):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                break
        try:
            resp.close()
        except Exception:  # pragma: no cover
            pass
        return b"".join(chunks)[:cap + 1]


class FixtureBroker(Broker):
    """Canned answers only. Anything not in the fixtures is a refusal."""

    def __init__(self, skill_id: str, hosts, limits=None, *, fixtures=None):
        super().__init__(skill_id, hosts, limits)
        self.responses = dict((fixtures or {}).get("responses") or {})

    def _deliver(self, url: str, host: str) -> bytes:
        canned = self.responses.get(url)
        if canned is None:
            raise BrokerRefusal(
                f"there is no canned answer for {url} and tests never reach "
                f"the network")
        if "json" in canned:
            return json.dumps(canned["json"]).encode("utf-8")
        return str(canned.get("text", "")).encode("utf-8")


__all__ = ["FAILURE_NETWORK", "FAILURE_SERVICE", "FAILURE_SETUP",
           "FETCH_TIMEOUT_S",
           "MAX_QUERY_BYTES", "MAX_URL_BYTES", "TLS_REFUSAL", "Broker",
           "BrokerFailure", "BrokerRefusal", "FixtureBroker", "LiveBroker"]
