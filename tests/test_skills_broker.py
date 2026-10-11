"""The broker: the only way out of the jail, and every refusal it makes.

The first test is the one that matters most. A shut door must be a refusal with
**no DNS query behind it** -- "we blocked it" and "we told the world what it
wanted" are different outcomes, and only the first one is acceptable. It is
proved here by handing the broker a fake door layer that records every call: a
shut door must produce zero ``egress_request`` calls.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai.skills import broker as B                         # noqa: E402
from skillkit import load_example                              # noqa: E402

HOST = "api.wheretheiss.at"
URL = f"https://{HOST}/v1/satellites/25544"
LIMITS = {"max_requests": 2, "max_response_bytes": 1024}


class FakeResponse:
    def __init__(self, body=b"{}", status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def close(self):
        pass


class FakeDoors:
    """Stands in for ``security.egress_doors``. Records everything."""

    def __init__(self, *, open_=True, response=None):
        self.open = open_
        self.response = response or FakeResponse(b'{"ok": true}')
        self.requests = []
        self.allowed_checks = []

    def skill_door_id(self, skill_id, index=0):
        return f"skill:{skill_id}:{index}"

    def egress_allowed(self, door_id):
        self.allowed_checks.append(door_id)
        return (True, "") if self.open else (False, "the owner has not opened it")

    def egress_request(self, door_id, method, url, **kwargs):
        self.requests.append((door_id, method, url, kwargs))
        return self.response


def live(doors, hosts=(HOST,), limits=None):
    return B.LiveBroker("iss_position", hosts, limits or LIMITS, egress=doors)


# ── the door ────────────────────────────────────────────────────────

def test_a_shut_door_refuses_before_anything_resolves_a_name():
    doors = FakeDoors(open_=False)
    answer = live(doors).handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False
    assert "shut" in answer["error"]
    assert doors.requests == [], (
        "the broker called out through a shut door; a refusal must cost zero "
        "network, DNS included")
    assert doors.allowed_checks, "the door was never consulted at all"


def test_an_open_door_goes_through_egress_request_and_nothing_else():
    doors = FakeDoors(response=FakeResponse(json.dumps({"latitude": 1}).encode()))
    answer = live(doors).handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer == {"ok": True, "json": {"latitude": 1}}
    assert len(doors.requests) == 1
    door_id, method, url, kwargs = doors.requests[0]
    assert door_id == "skill:iss_position:0"
    assert method == "GET" and url == URL
    assert kwargs.get("timeout") == B.FETCH_TIMEOUT_S


# ── the host set ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,needle", [
    ("https://evil.example.com/x", "not one of the hosts"),
    ("http://api.wheretheiss.at/x", "not https"),
    ("ftp://api.wheretheiss.at/x", "not https"),
    ("https://api.wheretheiss.at:8443/x", "port 8443"),
    ("https://user:pw@api.wheretheiss.at/x", "credentials"),
    ("https://127.0.0.1/x", "not one of the hosts"),
    ("https://api.wheretheiss.at.evil.example.com/x", "not one of the hosts"),
    ("", "not a URL"),
])
def test_urls_the_broker_will_not_fetch(url, needle):
    doors = FakeDoors()
    answer = live(doors).handle({"t": "fetch", "url": url, "kind": "json"})
    assert answer["ok"] is False and needle in answer["error"]
    assert doors.requests == []


def test_a_host_absent_from_the_manifest_is_refused_even_with_the_door_open():
    """The manifest's host set is the approval; the door is the enforcement.
    Both, and in that order."""
    doors = FakeDoors()
    answer = live(doors, hosts=()).handle({"t": "fetch", "url": URL,
                                           "kind": "json"})
    assert answer["ok"] is False and doors.requests == []


# ── the counters and the caps ───────────────────────────────────────

def test_the_request_counter_is_enforced():
    doors = FakeDoors()
    broker = live(doors)
    for _ in range(LIMITS["max_requests"]):
        assert broker.handle({"t": "fetch", "url": URL, "kind": "json"})["ok"]
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False and "used them" in answer["error"]
    assert len(doors.requests) == LIMITS["max_requests"]


def test_a_response_over_the_declared_size_is_refused_not_truncated():
    big = b'{"x": "' + b"A" * (LIMITS["max_response_bytes"] + 64) + b'"}'
    doors = FakeDoors(response=FakeResponse(big))
    answer = live(doors).handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False and "bigger" in answer["error"]


def test_a_declared_content_length_over_the_cap_is_refused_early():
    doors = FakeDoors(response=FakeResponse(b"{}", headers={"Content-Length": "999999"}))
    answer = live(doors).handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False and "bigger" in answer["error"]


def test_a_query_string_longer_than_the_cap_is_refused():
    doors = FakeDoors()
    url = f"https://{HOST}/v1?q=" + "a" * (B.MAX_QUERY_BYTES + 10)
    answer = live(doors).handle({"t": "fetch", "url": url, "kind": "json"})
    assert answer["ok"] is False and "query" in answer["error"]
    assert doors.requests == []


def test_a_url_longer_than_the_cap_is_refused():
    doors = FakeDoors()
    url = f"https://{HOST}/" + "a" * (B.MAX_URL_BYTES + 10)
    answer = live(doors).handle({"t": "fetch", "url": url, "kind": "json"})
    assert answer["ok"] is False and "longer" in answer["error"]


def test_an_error_status_is_a_refusal_not_a_body():
    # 4xx other than 429. (5xx and 429 are failures, not refusals: see the
    # transport-failure tests below.)
    doors = FakeDoors(response=FakeResponse(b"nope", status=404))
    broker = live(doors)
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False and "404" in answer["error"]
    assert "nope" not in json.dumps(answer)
    assert broker.refusals and broker.failures == []


def test_a_non_json_answer_to_fetch_json_is_a_refusal():
    doors = FakeDoors(response=FakeResponse(b"<html>hello</html>"))
    answer = live(doors).handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False and "JSON" in answer["error"]


def test_a_transport_failure_names_only_the_type():
    class Boom(FakeDoors):
        def egress_request(self, *a, **k):
            raise ConnectionError(f"failed to reach {URL}?secret=hunter2")

    broker = live(Boom())
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False
    assert "hunter2" not in answer["error"]
    # Only the TYPE is kept, and it is kept as a failure, not a refusal.
    assert broker.failures == ["network: ConnectionError"]
    assert "hunter2" not in json.dumps(broker.failures)
    assert broker.refusals == []


# ── transport failures are not refusals ─────────────────────────────

def _raising(exc):
    class Raising(FakeDoors):
        def egress_request(self, *a, **k):
            self.requests.append(a)
            raise exc
    return Raising()


def _unresolvable():
    from puffpai.security import egress_doors as ed
    return ed.EgressBlocked(
        "skill:iss_position:0",
        f"{ed.REASON_UNRESOLVABLE}: host {HOST!r} does not resolve",
        "could not look it up")


def _requests_exc(name):
    requests = pytest.importorskip("requests")
    return getattr(requests.exceptions, name)("boom")


@pytest.mark.parametrize("make_exc", [
    lambda: _requests_exc("ConnectionError"),
    lambda: _requests_exc("ConnectTimeout"),
    lambda: _requests_exc("ReadTimeout"),
    lambda: TimeoutError("timed out"),
    _unresolvable,
    lambda: __import__("puffpai.security.netaddr",
                       fromlist=["ResolutionError"]).ResolutionError("x"),
], ids=["connection_error", "connect_timeout", "read_timeout",
        "socket_timeout", "unresolvable_egress_blocked", "resolution_error"])
def test_a_transport_failure_lands_in_failures_not_refusals(make_exc):
    broker = live(_raising(make_exc()))
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False
    assert broker.refusals == []
    assert len(broker.failures) == 1
    assert broker.failures[0].startswith(B.FAILURE_NETWORK)


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_a_server_error_or_rate_limit_is_a_failure_not_a_refusal(status):
    doors = FakeDoors(response=FakeResponse(b"busy", status=status))
    broker = live(doors)
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False and "busy" not in json.dumps(answer)
    assert broker.refusals == []
    assert broker.failures == [f"{B.FAILURE_SERVICE}: {status}"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418])
def test_other_client_errors_stay_refusals(status):
    doors = FakeDoors(response=FakeResponse(b"no", status=status))
    broker = live(doors)
    broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert broker.refusals and broker.failures == []


def test_a_shut_door_is_still_a_refusal_not_a_failure():
    doors = FakeDoors(open_=False)
    broker = live(doors)
    broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert broker.refusals and broker.failures == []
    assert doors.requests == []


def test_an_undeclared_host_is_still_a_refusal_not_a_failure():
    doors = FakeDoors()
    broker = live(doors)
    broker.handle({"t": "fetch", "url": "https://evil.example.net/x",
                   "kind": "json"})
    assert broker.refusals and broker.failures == []
    assert doors.requests == []


def test_a_private_address_is_still_a_refusal_not_a_failure():
    from puffpai.security import egress_doors as ed
    exc = ed.EgressBlocked(
        "skill:iss_position:0",
        f"{ed.REASON_RESOLVED_PRIVATE} (private): host {HOST!r} resolves to "
        f"an address this appliance may not open a socket to (initial url)",
        "points indoors")
    broker = live(_raising(exc))
    broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert broker.refusals and broker.failures == []


def test_a_pin_refusal_is_still_a_refusal_not_a_failure():
    from puffpai.security import netaddr
    broker = live(_raising(netaddr.AddressRefused(
        "loopback", "is a loopback address", HOST, "")))
    broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert broker.refusals and broker.failures == []


def test_the_request_count_cap_is_still_a_refusal_after_failures():
    broker = live(_raising(_requests_exc("ConnectionError")),
                  limits={"max_requests": 1, "max_response_bytes": 1024})
    broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert len(broker.failures) == 1 and len(broker.refusals) == 1


@pytest.mark.parametrize("make_exc", [
    lambda: _requests_exc("SSLError"),
    lambda: __import__("ssl").SSLCertVerificationError("bad cert"),
], ids=["requests_ssl_error", "ssl_cert_verification"])
def test_a_tls_failure_is_a_refusal_never_offline(make_exc):
    # A requests SSLError IS a ConnectionError and an ssl.SSLError IS an
    # OSError; both must still be refusals, because an unverifiable
    # certificate is what tampering looks like.
    broker = live(_raising(make_exc()))
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert broker.failures == []
    assert broker.refusals == [B.TLS_REFUSAL]
    assert answer["error"] == B.TLS_REFUSAL


def test_a_tls_error_wrapped_inside_a_connection_error_is_still_tls():
    requests = pytest.importorskip("requests")
    import ssl
    try:
        try:
            raise ssl.SSLError("handshake")
        except ssl.SSLError as inner:
            raise requests.exceptions.ConnectionError("wrapped") from inner
    except requests.exceptions.ConnectionError as outer:
        exc = outer
    broker = live(_raising(exc))
    broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert broker.failures == [] and broker.refusals == [B.TLS_REFUSAL]


def test_the_fetch_timeout_is_a_short_connect_and_read_pair():
    doors = FakeDoors()
    live(doors).handle({"t": "fetch", "url": URL, "kind": "json"})
    timeout = doors.requests[0][3]["timeout"]
    assert isinstance(timeout, tuple) and len(timeout) == 2
    connect, read = timeout
    assert connect <= 3.5 and read <= 8.0


# ── fixture mode: no network in tests, by construction ──────────────

def test_fixture_mode_answers_only_what_the_fixtures_can():
    _manifest, _source, fixtures = load_example()
    broker = B.FixtureBroker("iss_position", (HOST,), LIMITS, fixtures=fixtures)
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is True
    assert answer["json"]["latitude"] == pytest.approx(37.148923)


def test_fixture_mode_refuses_an_unknown_url_on_an_allowed_host():
    _manifest, _source, fixtures = load_example()
    broker = B.FixtureBroker("iss_position", (HOST,), LIMITS, fixtures=fixtures)
    answer = broker.handle({"t": "fetch", "url": f"https://{HOST}/v1/other",
                            "kind": "json"})
    assert answer["ok"] is False and "canned" in answer["error"]


def test_fixture_mode_has_no_door_and_no_transport():
    """The fixture broker cannot reach the network even if asked nicely: it
    has no door id, no session and no ``egress_request``."""
    broker = B.FixtureBroker("iss_position", (HOST,), LIMITS, fixtures={})
    assert not hasattr(broker, "door_id")
    answer = broker.handle({"t": "fetch", "url": URL, "kind": "json"})
    assert answer["ok"] is False
