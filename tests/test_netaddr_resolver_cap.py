"""The system resolver is bounded: a stalled DNS is "unresolvable" in ~3 s.

Every egress door resolves through ``netaddr.default_resolver``. With the
house internet unplugged, glibc's own timeouts can hold a lookup for 10 s or
more; the cap turns that into the same ResolutionError a failed lookup gives,
which every caller already handles. When DNS answers, nothing changes.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.security import netaddr                           # noqa: E402


def test_a_lookup_that_stalls_past_the_cap_is_unresolvable_in_about_the_cap(
        monkeypatch):
    monkeypatch.setattr(netaddr, "RESOLVE_TIMEOUT_S", 0.3)
    release = threading.Event()

    def stuck(*_a, **_k):
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    started = time.monotonic()
    try:
        with pytest.raises(netaddr.ResolutionError):
            netaddr.default_resolver("api.wheretheiss.at", 443)
        elapsed = time.monotonic() - started
    finally:
        release.set()  # let the straggler finish so it frees its slot
    assert elapsed < 1.5, elapsed


def test_the_real_cap_is_about_three_seconds():
    assert 2.0 <= netaddr.RESOLVE_TIMEOUT_S <= 3.5


def test_a_normal_answer_is_returned_unchanged(monkeypatch):
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "",
         ("2606:2800:220:1::1", 443, 0, 0)),
    ]
    seen = []

    def answer(host, port, *a, **k):
        seen.append((host, port, k.get("type")))
        return infos

    monkeypatch.setattr(socket, "getaddrinfo", answer)
    assert netaddr.default_resolver("example.test", 443) == [
        "93.184.216.34", "2606:2800:220:1::1"]
    assert seen == [("example.test", 443, socket.SOCK_STREAM)]


def test_a_failed_lookup_is_still_a_resolution_error(monkeypatch):
    def nx(*_a, **_k):
        raise socket.gaierror(socket.EAI_NONAME, "not known")

    monkeypatch.setattr(socket, "getaddrinfo", nx)
    with pytest.raises(netaddr.ResolutionError):
        netaddr.default_resolver("nowhere.invalid", 443)


def test_a_non_os_error_from_the_lookup_is_re_raised_unchanged(monkeypatch):
    class Tripwire(AssertionError):
        pass

    def boom(*_a, **_k):
        raise Tripwire("a lookup happened")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(Tripwire):
        netaddr.default_resolver("example.test", 443)


def test_stragglers_are_bounded(monkeypatch):
    monkeypatch.setattr(netaddr, "RESOLVE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(netaddr, "_PENDING_LOOKUPS",
                        threading.BoundedSemaphore(2))
    release = threading.Event()
    entered = []

    def stuck(*_a, **_k):
        entered.append(1)
        release.wait(5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    try:
        for _ in range(4):
            with pytest.raises(netaddr.ResolutionError):
                netaddr.default_resolver("example.test", 443)
        # Only two workers were ever started; the rest were refused at once.
        deadline = time.monotonic() + 2.0
        while len(entered) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        assert len(entered) == 2
    finally:
        release.set()


def test_ip_literals_and_localhost_resolve_while_every_slot_is_stalled(
        monkeypatch):
    """A long DNS outage fills the slots; LAN calls by address must not care."""
    monkeypatch.setattr(netaddr, "RESOLVE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(netaddr, "_PENDING_LOOKUPS",
                        threading.BoundedSemaphore(netaddr.MAX_PENDING_LOOKUPS))
    release = threading.Event()
    local = {
        "192.168.1.20": [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                          ("192.168.1.20", 80))],
        "::1": [(socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                 ("::1", 443, 0, 0))],
        "localhost": [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                       ("127.0.0.1", 443))],
    }
    seen = []

    def lookup(host, port, *a, **k):
        seen.append((host, k.get("flags", 0), threading.current_thread().name))
        if host in local:
            return local[host]
        release.wait(5)  # a name: stalls like DNS with the WAN unplugged
        return []

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    try:
        for _ in range(netaddr.MAX_PENDING_LOOKUPS):
            with pytest.raises(netaddr.ResolutionError):
                netaddr.default_resolver("api.wheretheiss.at", 443)
        # Every slot is now held by a stalled worker: a NAME fails at once.
        started = time.monotonic()
        with pytest.raises(netaddr.ResolutionError):
            netaddr.default_resolver("api.wheretheiss.at", 443)

        # ...and addresses and localhost still resolve, immediately, on the
        # caller's own thread, with the same answer as before.
        assert netaddr.default_resolver("192.168.1.20", 80) == ["192.168.1.20"]
        assert netaddr.default_resolver("::1", 443) == ["::1"]
        assert netaddr.default_resolver("localhost", 443) == ["127.0.0.1"]
        assert time.monotonic() - started < 0.5
    finally:
        release.set()

    me = threading.current_thread().name
    direct = [s for s in seen if s[0] in local]
    assert [(h, t) for h, _f, t in direct] == [
        ("192.168.1.20", me), ("::1", me), ("localhost", me)]
    assert direct[0][1] == socket.AI_NUMERICHOST
    assert direct[1][1] == socket.AI_NUMERICHOST
    assert direct[2][1] == 0


def test_a_real_ip_literal_resolves_without_any_dns():
    # Real getaddrinfo with AI_NUMERICHOST: parses, never queries a server.
    assert netaddr.default_resolver("127.0.0.1", 443) == ["127.0.0.1"]
