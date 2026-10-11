"""HTTP that cannot leave the house.

Why this module exists
----------------------
Three call sites read the appliance's own AdGuard Home over plain
``requests.get`` — ``bridge_ledger`` (query log), ``bridge_inventory`` (client
list) and ``zone3/adguard_controller`` (everything else). Each of them builds
its URL from ``config zone3.adguard_url``, which is a *string an operator
edits*. Nothing between that string and the socket asserted that it still named
this house. Change one character in a config file — or restore a config from a
different box, or let a hostname there resolve to something new — and three
"loopback only" readers become three outbound calls carrying the household's
AdGuard credential, with no door, no consent and no ledger line.

``tests/test_egress_door_registry.py`` recorded the promise ("AdGuard on the LAN")
as a comment in a test exemption list. This module makes it a check that runs.

What it does
------------
:func:`lan_get` / :func:`lan_post` resolve the URL's host, prove that **every**
address it resolves to is loopback, RFC 1918 or unique-local
(:func:`puffpai.security.netaddr.check` under the ``HOUSE`` posture), and then
issue the request through :func:`~puffpai.security.netaddr.pinned_session`,
which re-validates inside the connection factory. The pre-check is the
readable refusal; the pinned session is what closes the DNS-rebinding window
between the check and the socket.

What it deliberately does not do
--------------------------------
* **No redirects.** ``allow_redirects`` defaults to False. A LAN service that
  answers 302 to a public URL is exactly the escape this module exists to stop,
  and no AdGuard control endpoint redirects.
* **No proxies, no netrc, no cookie jar.** Inherited from ``pinned_session``.
* **It is not a door.** Doors are for the internet. A call here is a claim that
  the destination is inside the house, and the claim is enforced rather than
  documented.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from puffpai.logging_config import get_logger
from puffpai.security import netaddr

logger = get_logger(__name__)

#: Schemes a house-local call may use. ``file:``, ``ftp:`` and friends are not
#: refused by an address check, because they never reach one.
_SCHEMES = ("http", "https")

#: Default port per scheme, for the resolve-and-pin step.
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: The posture every call here runs under. Named once so a reader does not have
#: to check each function.
POSTURE = netaddr.HOUSE

__all__ = ["POSTURE", "lan_request", "lan_get", "lan_post", "validate_url"]


def validate_url(url: str) -> tuple[str, int]:
    """Prove *url* names a host inside this house. Returns ``(host, port)``.

    Raises :class:`~puffpai.security.netaddr.AddressRefused` when any address
    the host resolves to is outside the house, :class:`ValueError` when the URL
    is not an http(s) URL with a host, and
    :class:`~puffpai.security.netaddr.ResolutionError` when the name cannot be
    resolved — "we could not tell" is a refusal, never a pass.
    """
    parts = urlsplit(str(url or ""))
    scheme = (parts.scheme or "").lower()
    if scheme not in _SCHEMES:
        raise ValueError(f"{scheme or 'no'}: is not an http(s) URL")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:                       # a port that is not a number
        raise ValueError("URL has an invalid port") from None
    # Validates EVERY answer, not the first: a name with one private and one
    # public address is a name whose address the client picks.
    netaddr.validated_addresses(host, port, posture=POSTURE)
    return host, port


def lan_request(method: str, url: str, **kwargs):
    """``requests``-shaped call that refuses any destination off this house.

    ``kwargs`` are passed through to ``requests`` unchanged except that
    ``allow_redirects`` defaults to False (see the module docstring) and
    ``timeout`` defaults to 10 seconds — an unbounded read against a hung LAN
    service is a hang in whatever thread called this.
    """
    host, _port = validate_url(url)
    kwargs.setdefault("allow_redirects", False)
    kwargs.setdefault("timeout", 10)
    session = netaddr.pinned_session(posture=POSTURE)
    try:
        return session.request(str(method).upper(), url, **kwargs)
    finally:
        try:
            session.close()
        except Exception:                    # pragma: no cover - transport
            logger.debug("lan_http session close failed for %s", host)


def lan_get(url: str, **kwargs):
    """GET a house-local URL. See :func:`lan_request`."""
    return lan_request("GET", url, **kwargs)


def lan_post(url: str, **kwargs):
    """POST to a house-local URL. See :func:`lan_request`."""
    return lan_request("POST", url, **kwargs)
