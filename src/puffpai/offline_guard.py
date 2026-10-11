"""
PuffPAI Offline Guard
Decorators and utilities for graceful degradation when offline.

Connectivity probing and the egress contract
--------------------------------------------
:meth:`NetworkStatus.has_internet` is the appliance's "is the WAN up?" check,
and it is itself egress: it opens a TCP connection to somebody. Fixed probe
hosts such as ``1.1.1.1:53``, ``8.8.8.8:53`` or ``www.google.com:443`` would be
hosts nobody in the household chose, on a product whose claim is that it never
needs the cloud, contacted regardless of whether any egress door is open.

So it takes its targets from ``puffpai.security.egress_doors.probe_targets()``:
only hosts a door the owner has already opened contacts anyway. With every door
shut it opens **no sockets at all** and reports offline. See that module and
``docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md`` for the ``connectivity_probe`` door.

:meth:`NetworkStatus.is_reachable` is unchanged and ungated on purpose: its only
callers name loopback or a LAN address the owner configured (Ollama, Home
Assistant), which is not egress. Do not use it with a public host — use a door.

Usage:
    from puffpai.offline_guard import requires_internet, requires_local_service

    @requires_internet
    def web_search(query: str) -> str:
        ...  # Only runs if internet available

    @requires_local_service("localhost", 11434)
    def query_ollama(prompt: str) -> str:
        ...  # Only runs if Ollama is reachable
"""

from __future__ import annotations

import socket
import time
import functools
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

# Cache duration for connectivity checks (seconds)
_CACHE_DURATION: float = 30.0


class NetworkStatus:
    """Tracks overall network connectivity state with caching."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], tuple[bool, float]] = {}

    def is_reachable(self, host: str, port: int, timeout: float = 2.0) -> bool:
        """Check if a host:port is reachable, with caching."""
        key = (host, port)
        now = time.time()

        cached = self._cache.get(key)
        if cached and (now - cached[1]) < _CACHE_DURATION:
            return cached[0]

        reachable = self._check_connection(host, port, timeout)
        self._cache[key] = (reachable, now)
        return reachable

    def _check_connection(self, host: str, port: int, timeout: float) -> bool:
        """Actually attempt a TCP connection."""
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except (socket.timeout, socket.error, OSError):
            return False

    def has_internet(self, timeout: float = 3.0,
                     door_id: str | None = None) -> bool:
        """Is there a WAN link — asked WITHOUT contacting anyone new.

        Fixed public probe hosts (``1.1.1.1:53``, ``8.8.8.8:53``,
        ``www.google.com:443``) would be parties nobody in the household chose,
        on an appliance whose whole claim is that it never needs the cloud,
        contacted whether or not any egress door is open. A liveness probe is
        egress, and "egress only through enumerated doors" has to include it.

        It probes only hosts that a door the owner has **already opened**
        contacts anyway (``egress_doors.probe_targets``). Therefore:

        * with every door shut this opens **zero sockets** and returns False —
          which is also the correct answer for a box that may not go online;
        * it can disclose nothing that an open door does not already disclose
          to that same host;
        * ``egress_doors.connectivity_probe: false`` turns probing off outright.

        Pass *door_id* to probe the door you are about to use. Callers should
        still check their own door with ``require_open`` FIRST — this function
        deliberately does not do it for them, because "am I allowed" and "is the
        link up" are different questions and the consent one must come first.
        """
        for host, port, _door in self._probe_targets(door_id):
            if self.is_reachable(host, port, timeout=timeout):
                return True
        return False

    @staticmethod
    def _probe_targets(door_id: str | None):
        """Hosts the probe may contact, from the egress-door registry.

        Fails shut: if the registry cannot be imported or errors, the answer is
        "no targets", which makes :meth:`has_internet` report offline rather
        than fall back to dialling a public resolver.
        """
        try:
            from puffpai.security.egress_doors import probe_targets
            return probe_targets(door_id)
        except Exception:
            return ()

    def clear_cache(self) -> None:
        """Clear the connectivity cache."""
        self._cache.clear()


# Global singleton
_status: NetworkStatus = NetworkStatus()


def get_network_status() -> NetworkStatus:
    """Return the global NetworkStatus singleton."""
    return _status


def requires_internet(func: F | None = None, *, fallback_message: str | None = None) -> F | Callable[[F], F]:
    """Decorator: skip function if internet is unavailable.

    Returns a graceful degradation message instead of failing.
    """
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _status.has_internet():
                msg = fallback_message or (
                    f"[Offline] {fn.__name__} requires internet access, "
                    "which is currently unavailable. PuffPAI is running in "
                    "offline mode."
                )
                return msg
            return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]

    if func is not None:
        return decorator(func)
    return decorator  # type: ignore[return-value]


def requires_local_service(host: str, port: int, service_name: str | None = None) -> Callable[[F], F]:
    """Decorator factory: skip function if a local service is unreachable.

    Usage:
        @requires_local_service("localhost", 11434, "Ollama")
        def query_llm(prompt):
            ...
    """
    svc_name: str = service_name or f"{host}:{port}"

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _status.is_reachable(host, port):
                return (
                    f"[Offline] {fn.__name__} requires {svc_name} "
                    f"at {host}:{port}, which is currently unreachable."
                )
            return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator
