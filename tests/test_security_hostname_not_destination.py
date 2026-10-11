"""A declared hostname is not a destination.

The attack this module closes::

    A door check that compares ``host not in d.hosts`` as a STRING and then
    hands the URL to ``requests``, which resolves DNS itself, has no
    ``getaddrinfo``, no private/loopback/link-local rejection and no
    connection pinning. It is safe only while every hostname is chosen by the
    project. A manifest-declared host, or a compromised DNS answer for a
    declared one, resolves to ``192.168.x`` or ``169.254.169.254`` and every
    check passes.

    "doors": [{ "id": "astro_news", "hosts": ["feed.spacepack.example"],
                "data_sent": "Nothing about you. Just the request." }]

    Author sets feed.spacepack.example A -> 192.168.1.1. Or 127.0.0.1. Or
    169.254.169.254. Every check passes. The owner sees a card that says
    *nothing about you leaves*.

What is asserted here, in order of importance:

1. a declared host that resolves to each forbidden class is refused, and the
   refusal happens with **every socket primitive in the process made fatal**;
2. a redirect from a genuinely public host onto one that resolves privately is
   refused at that hop;
3. the connection is **pinned** to the validated address while the TLS server
   name and the ``Host`` header keep the hostname, closing the TOCTOU
   window that validation alone does not close;
4. one bad address in a multi-answer set refuses the whole name;
5. the ledger records ``resolved_private`` and the address CLASS, and never the
   address;
6. the twelve real doors still resolve public and still work.
"""

from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.security import egress_doors as ed  # noqa: E402
from puffpai.security import egress_ledger as el  # noqa: E402
from puffpai.security import netaddr  # noqa: E402


#: One address per class the door layer must refuse. Every one of these is a
#: perfectly legal DNS answer that a hostname we approved could return.
FORBIDDEN = {
    "loopback": "127.0.0.1",
    "loopback_v6": "::1",
    "loopback_mapped": "::ffff:127.0.0.1",
    "rfc1918_10": "192.168.50.140",
    "rfc1918_192": "192.168.1.1",
    "rfc1918_172": "172.16.4.9",
    "link_local": "169.254.169.254",
    "unspecified": "0.0.0.0",
    "multicast": "224.0.0.1",
    "cgnat": "100.64.0.1",
    "ula": "fd00::1",
    "documentation_v4": "203.0.113.7",
    "documentation_v4b": "198.51.100.7",
    "documentation_v4c": "192.168.50.7",
    "documentation_v6": "2001:db8::1",
    "benchmark": "198.18.0.1",
}

#: A globally routable literal, so an "allowed" path in these tests is allowed
#: for the right reason.
PUBLIC = "93.184.216.34"

#: A host the news door really declares, so the string check passes and the
#: address check is the only thing left standing between us and the socket.
DECLARED_URL = "https://feeds.npr.org/1001/rss.xml"


# ── harness ──────────────────────────────────────────────────────────────────

def _use_config(monkeypatch, payload: dict) -> None:
    written = json.dumps(payload)
    monkeypatch.setattr(ed, "_raw_config", lambda: json.loads(written))
    monkeypatch.setattr(ed, "_raw_overlay", lambda: {})


class _SocketOpened(AssertionError):
    """Raised if anything in the process tries to reach the network."""


@pytest.fixture
def no_sockets(monkeypatch):
    """Every socket primitive is fatal, so 'refused' means 'never dialled'.

    ``socket.getaddrinfo`` is fatal too. That is deliberate and it is why every
    test in this file injects its own resolver: the address check must not fall
    back to the system resolver, and a test that quietly did would be testing
    the household's DNS rather than this code.
    """
    def boom(*_a, **_kw):
        raise _SocketOpened("a socket was opened before the door was checked")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    return boom


#: The real transport builder, captured at import — before conftest's suite-wide
#: guard replaces it. The guard exists so no OTHER test dials the wire through a
#: door; these tests are the ones where the real pin IS the thing under test.
_REAL_PINNED_SESSION = ed._pinned_session


@pytest.fixture
def real_pin(monkeypatch):
    """Undo conftest's no-wire guard for a test that exercises the real pin."""
    monkeypatch.setattr(ed, "_pinned_session", _REAL_PINNED_SESSION)


@pytest.fixture
def no_transport(monkeypatch):
    """Building a transport at all is fatal.

    ``no_sockets`` alone does not separate the two halves of this fix: the
    pinned session refuses a bad address too, so a test that only asserted "no
    socket opened" would still pass with the pre-connect check deleted. This
    fixture is what proves the refusal happened in:func:`_check_url` — before
    any client existed to be pinned — which is the property the door layer owes
    every future caller, including one whose transport this module does not
    build.
    """
    def boom(_d):
        raise _SocketOpened("a transport was built before the door was checked")

    monkeypatch.setattr(ed, "_pinned_session", boom)
    return boom


@pytest.fixture
def resolves(monkeypatch):
    """Install a resolver: ``resolves({"feeds.npr.org": "127.0.0.1"})``."""
    def install(table):
        def resolver(host, port):
            answer = table.get(host, table.get("*", []))
            return [answer] if isinstance(answer, str) else list(answer)
        ed.set_resolver_override(resolver)
        return resolver
    yield install
    ed.set_resolver_override(None)


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, url=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.request = types.SimpleNamespace(body=None)

    def json(self):
        return {}

    def raise_for_status(self):
        return None


@pytest.fixture
def transport(monkeypatch):
    """A stand-in for the pinned session, recording what it was asked to dial.

    Substituted at ``egress_doors._pinned_session`` rather than by replacing the
    ``requests`` module, so that the pin's OWN behaviour is tested where it
    lives (see the pinning section at the bottom) and this fixture only proves
    what the wrapper decided.
    """
    calls: list = []
    responses: list = []

    class _Session:
        def request(self, method, url, **kw):
            calls.append((method, url))
            return responses.pop(0) if responses else _FakeResponse(url=url)

        def close(self):
            return None

    monkeypatch.setattr(ed, "_pinned_session", lambda d: _Session())
    return types.SimpleNamespace(calls=calls, responses=responses)


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """A per-test ledger and snapshot, so the trail can be read back."""
    monkeypatch.setattr(ed, "_ledger_path",
                        lambda: str(tmp_path / "egress_doors.json"))
    el.set_path_override(str(tmp_path / "egress.jsonl"))
    ed._LAST_USED.clear()
    ed._LAST_REFUSED.clear()
    ed._ledger_loaded = False
    yield tmp_path
    el.set_path_override(None)


# ── 1. a declared host that points indoors ───────────────────────────────────

@pytest.mark.parametrize("label,address", sorted(FORBIDDEN.items()))
def test_a_declared_host_resolving_indoors_is_refused_before_any_socket(
        monkeypatch, no_sockets, no_transport, resolves, label, address):
    """The DNS-rebinding attack, one forbidden class at a time.

    ``feeds.npr.org`` is a host the ``news_rss`` door genuinely declares, so
    ``host in d.hosts`` passes, the string check that alone is not a control.
    Everything after that is the resolve-and-pin layer.
    """
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": address})

    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", DECLARED_URL)

    assert ed.REASON_RESOLVED_PRIVATE in excinfo.value.reason, label
    assert "feeds.npr.org" in excinfo.value.user_message


def test_the_refusal_names_no_resolved_address_anywhere(
        monkeypatch, no_sockets, no_transport, resolves, ledger):
    """The class, never the address. A household's own addressing is a fact
    about the household and must not end up in a security log."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": "192.168.44.7"})

    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", DECLARED_URL)

    assert "192.168.44.7" not in excinfo.value.reason
    assert "192.168.44.7" not in excinfo.value.user_message
    blob = json.dumps(el.read_records(limit=50))
    assert "192.168.44.7" not in blob
    assert "44.7" not in blob
    assert json.dumps(ed.last_refused("news_rss")).count("192.168") == 0
    # And not in the chained traceback either: AddressRefused's own message
    # names the address, so the refusal must not carry it as __context__.
    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None


def test_the_ledger_records_the_reason_code_and_the_address_class(
        monkeypatch, no_sockets, no_transport, resolves, ledger):
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": "169.254.169.254"})

    with pytest.raises(ed.EgressBlocked):
        ed.egress_get("news_rss", DECLARED_URL)

    row = el.read_records(limit=50)[-1]
    assert row["door"] == "news_rss"
    assert row["result"] == "refused"
    assert row["kind"] == "address_check"
    assert row["reason_code"] == ed.REASON_RESOLVED_PRIVATE
    assert row["address_class"] == "link_local"
    assert row["host"] == "feeds.npr.org"

    refused = ed.last_refused("news_rss")
    assert refused["result"] == "refused"
    assert refused["reason_code"] == ed.REASON_RESOLVED_PRIVATE
    assert refused["address_class"] == "link_local"


def test_the_owner_facing_ledger_shows_the_class_and_still_no_address(
        monkeypatch, no_sockets, no_transport, resolves, ledger):
    """``recent_public`` is what ``GET /system/egress/ledger`` returns."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": "10.0.0.9"})
    with pytest.raises(ed.EgressBlocked):
        ed.egress_get("news_rss", DECLARED_URL)

    public = el.recent_public(limit=10)[-1]
    assert public["reason_code"] == ed.REASON_RESOLVED_PRIVATE
    assert public["address_class"] == "private"
    assert "host" not in public and "path" not in public
    assert "10.0.0.9" not in json.dumps(public)


def test_one_bad_address_among_several_refuses_the_whole_name(
        monkeypatch, no_sockets, no_transport, resolves):
    """A public A record and a loopback AAAA record: the client picks."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": [PUBLIC, "127.0.0.1"]})
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", DECLARED_URL)
    assert "loopback" in excinfo.value.reason


def test_a_host_that_does_not_resolve_is_refused_not_attempted(
        monkeypatch, no_sockets, no_transport, resolves):
    """"We could not look it up" is not "it was fine"."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})

    def dead(host, port):
        raise netaddr.ResolutionError(f"cannot resolve {host!r}")

    ed.set_resolver_override(dead)
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", DECLARED_URL)
    assert ed.REASON_UNRESOLVABLE in excinfo.value.reason


def test_an_empty_resolver_answer_is_refused(monkeypatch, no_sockets,
                                             no_transport, resolves):
    """An empty answer must not mean "no addresses to check", i.e. no check."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": []})
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", DECLARED_URL)
    assert ed.REASON_UNRESOLVABLE in excinfo.value.reason


def test_a_shut_door_is_refused_without_even_a_dns_query(
        monkeypatch, no_sockets, no_transport):
    """Order: the flag decision runs BEFORE the lookup.

    No resolver is installed here, so ``socket.getaddrinfo`` is the fatal one
    from ``no_sockets``. Reaching it at all fails this test — which is the
    point: a shut door must not leak the name it was asked for to the
    household's resolver.
    """
    _use_config(monkeypatch, {})
    with pytest.raises(ed.EgressBlocked):
        ed.egress_get("news_rss", DECLARED_URL)


def test_an_undeclared_host_is_refused_without_a_dns_query(
        monkeypatch, no_sockets, no_transport):
    """Same ordering for the cheaper checks: a host outside the door's set is
    refused before it is looked up, because a lookup for an attacker-chosen
    name is itself a signal leaving the house."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", "https://attacker.example/feed.xml")
    assert "not declared by door" in excinfo.value.reason


# ── 2. redirects ─────────────────────────────────────────────────────────────

def test_a_redirect_onto_a_host_that_resolves_privately_is_refused_at_the_hop(
        monkeypatch, resolves, transport):
    """Hop one is a genuinely public host the door declares. Hop two is another
    host the door declares — whose DNS answer is the router."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": PUBLIC, "www.theverge.com": "192.168.1.1"})
    transport.responses.append(_FakeResponse(
        status_code=302,
        headers={"Location": "https://www.theverge.com/rss/index.xml"}))

    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", DECLARED_URL)

    assert ed.REASON_RESOLVED_PRIVATE in excinfo.value.reason
    assert "redirect hop" in excinfo.value.reason
    assert transport.calls == [("GET", DECLARED_URL)], \
        "the redirect target was dialled anyway"


def test_a_redirect_between_two_public_declared_hosts_is_followed(
        monkeypatch, resolves, transport):
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"*": PUBLIC})
    transport.responses.append(_FakeResponse(
        status_code=301,
        headers={"Location": "https://www.theverge.com/rss/index.xml"}))
    resp = ed.egress_get("news_rss", DECLARED_URL)
    assert resp.status_code == 200
    assert len(transport.calls) == 2


# ── 3. the port ──────────────────────────────────────────────────────────────

def test_a_declared_host_on_another_port_is_refused(monkeypatch, no_sockets,
                                                    no_transport, resolves):
    """A door declares a name, and the declaration the owner read is about the
    service on 443. ``https://feeds.npr.org:8123/`` is a different service."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": PUBLIC})
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", "https://feeds.npr.org:8123/1001/rss.xml")
    assert "is not 443" in excinfo.value.reason


def test_the_declared_port_spelled_out_is_allowed(monkeypatch, resolves,
                                                  transport):
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": PUBLIC})
    assert ed.egress_get("news_rss",
                         "https://feeds.npr.org:443/1001/rss.xml"
                         ).status_code == 200


# ── 4. the pin ───────────────────────────────────────────────────────────────

def test_the_pin_dials_the_validated_address_and_keeps_the_hostname():
    """The resolve-then-connect TOCTOU, closed.

    The socket goes to the address we validated; the TLS server name, the
    certificate check and the ``Host`` header all still come from
    ``connection.host``, which is the hostname. Asserted on the connection
    object itself rather than through a live handshake, because the property is
    "these two differ in the right direction" and that is visible here.
    """
    dialled: list = []

    class _FakeSocket:
        def settimeout(self, *_a):
            return None

        def close(self):
            return None

    def fake_create_connection(address, *_a, **_kw):
        dialled.append(address)
        return _FakeSocket()

    from urllib3.util import connection as urllib3_connection
    original = urllib3_connection.create_connection
    urllib3_connection.create_connection = fake_create_connection
    try:
        pools = netaddr._pinned_adapter_classes(
            lambda host, port: [PUBLIC])
        connection = pools["https"].ConnectionCls(host="feeds.npr.org",
                                                  port=443)
        connection._new_conn()
    finally:
        urllib3_connection.create_connection = original

    assert dialled == [(PUBLIC, 443)], "the socket did not go to the pin"
    assert connection.host == "feeds.npr.org", "SNI/Host lost the hostname"


def test_the_pin_refuses_when_the_second_lookup_answers_privately():
    """Rebinding: our check said public, the client's lookup says the router."""
    answers = iter([[PUBLIC], ["192.168.1.1"]])

    session = netaddr.pinned_session(resolver=lambda h, p: next(answers))
    # First answer is consumed by the caller's own validation in real code; here
    # the session's own policy runs on connect and sees the second.
    next(answers)
    with pytest.raises(netaddr.AddressRefused) as excinfo:
        session.get("https://feeds.npr.org/1001/rss.xml", timeout=2)
    assert excinfo.value.address_class == "private"


def test_the_wrapper_refuses_a_rebind_between_the_check_and_the_socket(
        monkeypatch, real_pin, ledger):
    """End to end: public for our lookup, the router for the client's.

    The whole reason validation alone is not the fix. The resolver answers
    ``93.184.216.34`` the first time — so ``_check_url`` approves the host — and
    ``192.168.1.1`` every time after, which is what the transport asks. Nothing
    is stubbed below the wrapper here: the real pinned session is built and the
    real adapter refuses.
    """
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    answers = [[PUBLIC]]

    def rebinding(host, port):
        return answers.pop(0) if answers else ["192.168.1.1"]

    ed.set_resolver_override(rebinding)
    try:
        with pytest.raises(ed.EgressBlocked) as excinfo:
            ed.egress_get("news_rss", DECLARED_URL, timeout=2)
    finally:
        ed.set_resolver_override(None)

    assert not answers, "the pre-connect check never ran"
    assert ed.REASON_RESOLVED_PRIVATE in excinfo.value.reason
    assert excinfo.value.__context__ is None
    assert "192.168.1.1" not in json.dumps(el.read_records(limit=50))

    row = el.read_records(limit=50)[-1]
    assert row["reason_code"] == ed.REASON_RESOLVED_PRIVATE
    assert row["address_class"] == "private"


def test_a_pinned_session_carries_no_cookie_jar_and_no_environment_trust():
    """Two things a plain ``requests.Session`` does that a door must not.

    ``trust_env`` reads ``HTTPS_PROXY`` and ``~/.netrc`` on every request: the
    first routes the socket somewhere never validated, the second attaches a
    stored credential to whatever host a redirect chose. A cookie jar makes the
    appliance carry an identifier from one fetch to the next and hand it back to
    the site that set it.
    """
    session = netaddr.pinned_session(resolver=lambda h, p: [PUBLIC])
    assert session.trust_env is False
    policy = session.cookies.get_policy()
    for domain in ("feeds.npr.org", "example.com", ".attacker.example"):
        assert policy.is_not_allowed(domain), f"{domain} could set a cookie"


def test_the_adapter_proves_the_pin_rather_than_assuming_it(monkeypatch):
    """Fail-closed only works if the check cannot pass while the pin is absent.

    Asserting the attribute was SET would say this urllib3 still has the field,
    not that its pool manager still reads it. So the adapter asks the manager
    for a pool and looks at what comes back — and a version that kept the field
    and stopped consulting it must be refused, not silently un-pinned.
    """
    adapter = netaddr.make_pinned_adapter(lambda h, p: [PUBLIC])
    pool = adapter.poolmanager.connection_from_url("https://feeds.npr.org/")
    assert pool.ConnectionCls.__name__ == "PinnedHTTPSConnection"

    # And the failure it exists to catch: a pool manager that accepts the
    # field and hands back a stock pool anyway. Simulated at the manager,
    # because that is the thing whose behaviour is being trusted.
    import urllib3.connectionpool
    import urllib3.poolmanager

    monkeypatch.setattr(
        urllib3.poolmanager.PoolManager, "connection_from_host",
        lambda self, host, **kw:
            urllib3.connectionpool.HTTPSConnectionPool("stock.invalid"))
    with pytest.raises(RuntimeError, match="did not install"):
        netaddr.make_pinned_adapter(lambda h, p: [PUBLIC])


def test_a_pinned_session_mounts_over_both_schemes():
    """Nothing may fall through to the unpinned adapter requests installs."""
    session = netaddr.pinned_session(resolver=lambda h, p: [PUBLIC])
    for prefix in ("https://", "http://"):
        assert type(session.adapters[prefix]).__name__ == "PinnedHTTPAdapter"


def test_the_wrapper_refuses_when_the_pin_cannot_be_built(monkeypatch,
                                                          real_pin, resolves):
    """Fail closed. An unpinned connection that believes it is pinned is worse
    off than one that knows it is not, so a transport this code cannot pin is a
    transport it does not use."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    resolves({"feeds.npr.org": PUBLIC})

    def unavailable(**_kw):
        raise RuntimeError("the address pin did not install on this urllib3")

    monkeypatch.setattr(netaddr, "pinned_session", unavailable)
    with pytest.raises(RuntimeError):
        ed.egress_get("news_rss", DECLARED_URL)


# ── 5. the LAN posture ───────────────────────────────────────────────────────

def test_a_lan_door_accepts_a_private_address_and_refuses_a_public_one():
    """No door declares ``lan`` today. The posture is tested anyway, so the
    first one that does cannot be added on an untested branch."""
    for good in ("192.168.1.10", "10.0.0.4", "172.16.5.5", "fd00::5"):
        netaddr.check(good, posture=netaddr.LAN)
    for bad in (PUBLIC, "8.8.8.8", "127.0.0.1", "169.254.169.254"):
        with pytest.raises(netaddr.AddressRefused):
            netaddr.check(bad, posture=netaddr.LAN)


def test_no_door_ships_with_the_lan_posture():
    assert [d.id for d in ed.DOORS if d.lan] == []


# ── 6. the twelve real doors still work ──────────────────────────────────────

# A door with dynamic hosts is enforced at its own call site, and this
# wrapper refuses it on purpose (it cannot check hosts a playlist names).
# tests/test_tv_bridge_switch.py pins that refusal.
REAL_DOORS = [(d.id, d.hosts[0]) for d in ed.DOORS
              if d.hosts and not d.dynamic_hosts]


@pytest.mark.parametrize("door_id,host", REAL_DOORS)
def test_every_real_door_still_opens_to_a_public_answer(monkeypatch, resolves,
                                                       transport, door_id,
                                                       host):
    """The check is a refusal for the hostile case and a no-op for ours."""
    d = ed.door(door_id)
    _use_config(monkeypatch, {"egress_doors": {d.config_flag: True}})
    resolves({"*": PUBLIC})
    resp = ed.egress_get(door_id, f"https://{host}/probe")
    assert resp.status_code == 200
    assert transport.calls == [("GET", f"https://{host}/probe")]


def test_every_declared_host_is_a_name_and_not_an_address():
    """A door that declared a literal would sail past the resolver check by
    resolving to itself. None does; this keeps it that way."""
    for d in ed.DOORS:
        for host in d.hosts:
            assert netaddr.parse(host) is None, f"{d.id} declares a literal"


# ── 7. the shared classifier, and the Toolsmith that still uses it ───────────

@pytest.mark.parametrize("label,address", sorted(FORBIDDEN.items()))
def test_the_shared_classifier_refuses_every_forbidden_class(label, address):
    assert netaddr.classify(address) is not None, label


@pytest.mark.parametrize("address", [
    "93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111",
])
def test_the_shared_classifier_passes_public_addresses(address):
    assert netaddr.classify(address) is None


def test_an_unparseable_answer_is_not_read_as_acceptable():
    with pytest.raises(netaddr.UnparseableAddress):
        netaddr.classify("not-an-address")


def test_every_class_has_a_description():
    for name in netaddr.ADDRESS_CLASSES:
        assert netaddr.describe(name)


def test_the_toolsmith_still_raises_its_own_wording(tmp_path):
    """The classifier moved; the Toolsmith's refusals did not change a word.

    ``agents/toolsmith.py`` hands these strings back to a caller and its SSRF
    battery asserts on the fragments, so the relocation is only correct if the
    wording survived it.
    """
    from puffpai.agents.toolsmith import (ToolsmithInputError,
                                          reject_internal_address)
    cases = [
        ("127.0.0.1", "is loopback"),
        ("169.254.169.254", "is link-local (cloud metadata range)"),
        ("224.0.0.1", "is multicast"),
        ("0.0.0.0", "is unspecified"),
        ("192.168.50.140", "is a private/internal address"),
        ("::ffff:127.0.0.1", "is loopback"),
    ]
    for address, fragment in cases:
        with pytest.raises(ToolsmithInputError) as excinfo:
            reject_internal_address(address)
        assert str(excinfo.value).startswith("web_fetch: ")
        assert fragment in str(excinfo.value)

    with pytest.raises(ToolsmithInputError) as excinfo:
        reject_internal_address("192.168.50.140", host="evil.example.com")
    assert "evil.example.com" in str(excinfo.value)

    with pytest.raises(ToolsmithInputError) as excinfo:
        reject_internal_address("banana")
    assert "is not an address this can check" in str(excinfo.value)

    reject_internal_address(PUBLIC)  # a public one still passes


# ── 8. the SearXNG loopback pinhole, and the ledger's last address leak ──────
#
# Two properties:
#
# 1. The pinned session cannot be the Toolsmith's default transport for every
# call: the documented SearXNG pinhole is http://127.0.0.1:8888, and a pinned
# session refuses loopback, correctly, and fatally for web_search and
# search_and_read.
# 2. A refused fetch must not write the raw address into the ledger's `host`
# field, and must carry a reason_code.

from puffpai.agents.toolsmith import (  # noqa: E402
    SEARXNG_LOOPBACK_ENDPOINT, ToolsmithAgent, ToolsmithInputError,
    _REDACTED_HOST, _fetch_ledger, _is_searxng_loopback,
    reject_internal_address)


@pytest.fixture
def agent(tmp_path):
    return ToolsmithAgent(workspace_root=str(tmp_path / "ws"))


class TestSearXNGLoopholeIsExactlyOneEndpoint:
    """The allowance is a constant, not a category."""

    def test_the_documented_endpoint_is_allowed(self):
        assert SEARXNG_LOOPBACK_ENDPOINT == "127.0.0.1:8888"
        for url in ("http://127.0.0.1:8888", "http://127.0.0.1:8888/",
                    "https://127.0.0.1:8888"):
            assert _is_searxng_loopback(url), url

    @pytest.mark.parametrize("url,why", [
        ("http://127.0.0.1:8000", "this appliance's own API"),
        ("http://127.0.0.1:11434", "the model runtime"),
        ("http://127.0.0.1:9999", "some other local port"),
        ("http://127.0.0.1", "loopback on the default port"),
        ("http://[::1]:8888", "the IPv6 spelling of loopback"),
        ("http://127.1:8888", "an abbreviated spelling of loopback"),
        ("http://localhost:8888", "a NAME that resolves to loopback"),
        ("http://192.168.1.1:8888", "the router"),
        ("http://10.0.0.5:8888", "a LAN host"),
        ("http://evil.example.com:8888", "a public name on the same port"),
        ("file:///etc/passwd", "not even http"),
    ])
    def test_everything_else_is_refused(self, url, why):
        assert not _is_searxng_loopback(url), f"{url} allowed: {why}"

    def test_the_allowance_selects_a_plain_session_only_for_that_endpoint(
            self, agent):
        """And every other search target still gets the pinned transport."""
        plain = agent._search_client("http://127.0.0.1:8888")
        assert type(plain.adapters["https://"]).__name__ == "HTTPAdapter", \
            "the one allowed endpoint should NOT be pinned; there is no name"
        assert plain.trust_env is False
        agent._http = None
        pinned = agent._search_client("http://127.0.0.1:8000")
        assert type(pinned.adapters["https://"]).__name__ == \
            "PinnedHTTPAdapter", "a non-exception target must stay pinned"

    def test_an_injected_transport_still_wins(self, tmp_path):
        sentinel = object()
        a = ToolsmithAgent(workspace_root=str(tmp_path / "w"), http=sentinel)
        assert a._search_client("http://127.0.0.1:8888") is sentinel
        assert a._search_client("http://127.0.0.1:8000") is sentinel


class TestSearXNGReachesLoopbackForReal:
    """A live socket on 127.0.0.1:8888. The unit checks above cannot catch a
    transport that is chosen correctly and then still refuses at connect."""

    @staticmethod
    def _serve(port):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = _json.dumps({"results": [
                    {"title": "T", "url": "https://example.com/a",
                     "content": "snippet"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                return

        server = HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @staticmethod
    def _envelope():
        from puffpai.agents.envelope import Budget, Envelope
        return Envelope(intent="web_search", payload={"query": "kexp"},
                        budget=Budget(max_actions=5, max_seconds=30.0,
                                      allow_egress=True))

    @pytest.fixture
    def doors_open(self, monkeypatch):
        _use_config(monkeypatch, {"egress_doors": {"web_search": True,
                                                   "web_fetch": True}})

    def test_web_search_reaches_the_loopback_searxng(self, tmp_path,
                                                     doors_open):
        port = int(SEARXNG_LOOPBACK_ENDPOINT.split(":")[1])
        try:
            server = self._serve(port)
        except OSError:  # pragma: no cover - something already holds the port
            pytest.skip(f"127.0.0.1:{port} is in use")
        try:
            a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"))
            reply = a.handle(self._envelope())
        finally:
            server.shutdown()
            server.server_close()
        assert reply["ok"] is True, reply.get("error")
        assert reply["count"] == 1

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8000",
                                     "http://127.0.0.1:11434",
                                     "http://192.168.1.50:8888"])
    def test_the_api_and_ollama_ports_are_still_refused(self, tmp_path,
                                                        doors_open, url):
        """The two loopback services that must never be reachable this way,
        plus a LAN host, all through the same method that now reaches 8888."""
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"),
                           searxng_url=url, searxng_timeout=3)
        reply = a.handle(self._envelope())
        assert reply["ok"] is False
        # And it says it REFUSED, not that it is offline: telling the owner to
        # restart a container that is fine is the failure this module exists
        # to make hard.
        assert reply.get("offline") is not True, reply
        assert "may open a socket to" in reply["error"]


class TestTheTrailNeverCarriesAnInternalAddress:
    """No refusal, ledger row or projection carries an internal address."""

    def test_a_resolved_address_is_not_in_the_refusal_at_all(self):
        with pytest.raises(ToolsmithInputError) as excinfo:
            reject_internal_address("192.168.50.140", host="evil.example.com")
        message = str(excinfo.value)
        assert "evil.example.com" in message      # the destination: kept
        assert "private" in message               # the reason: kept
        assert "192.168.50.140" not in message       # the address: gone
        assert excinfo.value.address_class == "private"
        assert excinfo.value.reason_code == ed.REASON_RESOLVED_PRIVATE

    @pytest.mark.parametrize("host,expected_class", [
        ("192.168.1.1", "private"),
        ("127.0.0.1", "loopback"),
        ("169.254.169.254", "link_local"),
        ("10.0.0.7", "private"),
    ])
    def test_an_internal_literal_in_the_url_is_redacted_to_its_class(
            self, ledger, host, expected_class):
        _fetch_ledger(f"http://{host}/latest/meta-data/", False,
                      detail="refused", reason_code=ed.REASON_RESOLVED_PRIVATE)
        row = el.read_records(limit=10)[-1]
        assert row["host"] == _REDACTED_HOST
        assert row["address_class"] == expected_class
        assert row["reason_code"] == ed.REASON_RESOLVED_PRIVATE
        assert host not in json.dumps(el.read_records(limit=10))

    def test_an_ordinary_hostname_is_still_recorded(self, ledger):
        """Redaction is for internal addressing, not for destinations."""
        _fetch_ledger("https://example.com/page", True, status=200)
        row = el.read_records(limit=10)[-1]
        assert row["host"] == "example.com"
        assert row["path"] == "/page"

    def test_a_public_literal_is_still_recorded(self, ledger):
        """A public address IS a destination the owner should be able to see."""
        _fetch_ledger(f"https://{PUBLIC}/page", True, status=200)
        assert el.read_records(limit=10)[-1]["host"] == PUBLIC

    def test_a_refused_fetch_writes_no_address_and_a_reason_code(
            self, monkeypatch, tmp_path, ledger):
        """End to end through the real fetch loop, with a name that resolves
        into the house, the case that would otherwise put an address in the
        ledger."""
        _use_config(monkeypatch, {"egress_doors": {"web_fetch": True}})
        from puffpai.agents.envelope import Budget, Envelope

        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"),
                           resolver=lambda h, p: ["192.168.50.140"],
                           http=object())
        reply = a.handle(Envelope(
            intent="web_fetch", payload={"url": "https://evil.example.com/"},
            budget=Budget(max_actions=5, max_seconds=30.0, allow_egress=True)))

        assert reply["ok"] is False
        assert "192.168.50.140" not in reply["error"]
        row = el.read_records(limit=10)[-1]
        assert row["host"] == "evil.example.com"
        assert row["reason_code"] == ed.REASON_RESOLVED_PRIVATE
        assert row["address_class"] == "private"
        assert "192.168.50.140" not in json.dumps(el.read_records(limit=20))

    @pytest.mark.parametrize("url", [
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8123/api/states",
        "http://10.0.0.7/",
    ])
    def test_the_reason_field_does_not_put_the_literal_back(self, monkeypatch,
                                                            tmp_path, ledger,
                                                            url):
        """Redacting `host` and leaving the same address one field over in
        `reason` would make the redaction decorative. Whole-record check."""
        _use_config(monkeypatch, {"egress_doors": {"web_fetch": True}})
        from puffpai.agents.envelope import Budget, Envelope

        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"), http=object())
        a.handle(Envelope(intent="web_fetch", payload={"url": url},
                          budget=Budget(max_actions=5, max_seconds=30.0,
                                        allow_egress=True)))
        literal = urlsplit(url).hostname
        blob = json.dumps(el.read_records(limit=10))
        assert literal not in blob, blob
        assert _REDACTED_HOST in blob

    def test_even_the_searxng_success_line_names_no_address(self, ledger):
        """The one allowed loopback endpoint is a constant and discloses
        nothing — but a trail whose rule has an exception is a trail whose
        next exception will not be a constant."""
        from puffpai.agents.toolsmith import _ledger_host
        host, address_class = _ledger_host("127.0.0.1")
        assert host == _REDACTED_HOST
        assert address_class == "loopback"

    def test_the_owner_facing_projection_carries_the_class(self, ledger):
        _fetch_ledger("http://192.168.1.1/", False, detail="refused",
                      reason_code=ed.REASON_RESOLVED_PRIVATE)
        public = el.recent_public(limit=5)[-1]
        assert public["address_class"] == "private"
        assert public["reason_code"] == ed.REASON_RESOLVED_PRIVATE
        assert "192.168" not in json.dumps(public)


# ── 9. one agent, both intents, in both orders ───────────────────────────────
#
# ToolsmithAgent is a process singleton. If `_client` lazily cached the pinned
# session into `self._http` (the INJECTION slot) while `_search_client` reads
# that slot to mean "a transport was injected, use it", the first web_fetch in
# the process would hand every later web_search a pinned session, which
# refuses loopback, and search would be dead until restart:
#
# configured SearXNG at http://127.0.0.1:8888 refused: loopback
#
# Section 8 cannot catch this: every test there uses a FRESH agent for one
# intent. The bug only exists in the sequence, so these tests exercise the
# sequence: both orders, one instance, one real loopback server.


class TestOneAgentServesBothIntentsInAnyOrder:
    """The transport a call gets is decided by that call, never by what ran
    before it."""

    @pytest.fixture
    def searxng(self):
        """A real HTTP server on the one allowed loopback endpoint."""
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = _json.dumps({"results": [
                    {"title": "T", "url": "https://example.com/a",
                     "content": "snippet"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                return

        port = int(SEARXNG_LOOPBACK_ENDPOINT.split(":")[1])
        try:
            server = HTTPServer(("127.0.0.1", port), Handler)
        except OSError:  # pragma: no cover - something already holds the port
            pytest.skip(f"127.0.0.1:{port} is in use")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield server
        server.shutdown()
        # server_close as well as shutdown: shutdown stops serve_forever
        # but leaves the listening socket bound, and HTTPServer sets
        # allow_reuse_address, so the NEXT test binds the same port beside a
        # dead listener and its connections go to whichever accepts. That is a
        # test-timeout that reads exactly like the bug under test.
        server.server_close()

    @pytest.fixture
    def doors_open(self, monkeypatch):
        _use_config(monkeypatch, {"egress_doors": {"web_search": True,
                                                   "web_fetch": True}})

    @staticmethod
    def _search(agent):
        from puffpai.agents.envelope import Budget, Envelope
        return agent.handle(Envelope(
            intent="web_search", payload={"query": "kexp"},
            budget=Budget(max_actions=5, max_seconds=30.0, allow_egress=True)))

    @staticmethod
    def _fetch(agent, url="https://example.com/"):
        from puffpai.agents.envelope import Budget, Envelope
        return agent.handle(Envelope(
            intent="web_fetch", payload={"url": url},
            budget=Budget(max_actions=5, max_seconds=30.0, allow_egress=True)))

    def test_a_fetch_does_not_poison_the_next_search(self, tmp_path,
                                                     doors_open, searxng):
        """THE sequence: a fetch first, then a search.

        The fetch is against a name that resolves into the LAN, so it is
        refused and opens no socket — the point is that it runs `_client` and
        builds the pinned session, which is all it took to break search.
        """
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"),
                           resolver=lambda h, p: ["192.168.50.140"])

        assert self._search(a)["ok"] is True, "search broken before any fetch"

        fetched = self._fetch(a, "https://evil.example.com/")
        assert fetched["ok"] is False          # refused, as it should be
        assert a._http_pinned is not None, "the fetch did not build the pin"

        after = self._search(a)
        assert after["ok"] is True, after.get("error")
        assert after["count"] == 1

    def test_a_search_does_not_poison_the_next_fetch(self, tmp_path,
                                                     doors_open, searxng):
        """The reverse order: the loopback session must not become the fetch
        transport either, or a fetch would lose its pin."""
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"),
                           resolver=lambda h, p: ["192.168.50.140"])

        assert self._search(a)["ok"] is True
        assert a._http_search is not None, "the search did not build a session"

        refused = self._fetch(a, "https://evil.example.com/")
        assert refused["ok"] is False
        assert "private" in refused["error"], refused["error"]
        assert type(a._client().adapters["https://"]).__name__ == \
            "PinnedHTTPAdapter", "the fetch transport lost its pin"

    def test_the_two_caches_never_become_the_same_object(self, tmp_path,
                                                         doors_open, searxng):
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"),
                           resolver=lambda h, p: ["192.168.50.140"])
        self._search(a)
        self._fetch(a, "https://evil.example.com/")
        assert a._http_search is not a._http_pinned
        assert a._http is None, \
            "the injection slot was written to; it is a policy input, not a cache"
        assert a._search_client(f"http://{SEARXNG_LOOPBACK_ENDPOINT}") \
            is a._http_search
        assert a._client() is a._http_pinned

    def test_repeated_alternation_stays_correct(self, tmp_path, doors_open,
                                                searxng):
        """Not just the first crossing: the choice must be per call, forever."""
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"),
                           resolver=lambda h, p: ["192.168.50.140"])
        for _ in range(3):
            assert self._search(a)["ok"] is True
            assert self._fetch(a, "https://evil.example.com/")["ok"] is False

    def test_the_search_transport_is_never_the_pinned_one(self, tmp_path):
        """Directly, without the network: the loopback endpoint must not be
        able to receive the pinned session no matter what is cached."""
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"))
        a._client()                                   # build the pin first
        chosen = a._search_client(f"http://{SEARXNG_LOOPBACK_ENDPOINT}")
        assert chosen is not a._http_pinned
        assert type(chosen.adapters["https://"]).__name__ == "HTTPAdapter"

    def test_a_non_loopback_search_target_still_gets_the_pin(self, tmp_path):
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"))
        assert a._search_client("http://127.0.0.1:8000") is a._client()
        assert a._search_client("https://searx.example.com") is a._client()


class TestARefusedSearchIsLoudNotPlausible:
    """A refusal must not read as 'the internet is down'."""

    @staticmethod
    def _search(agent):
        from puffpai.agents.envelope import Budget, Envelope
        return agent.handle(Envelope(
            intent="web_search", payload={"query": "kexp"},
            budget=Budget(max_actions=5, max_seconds=30.0, allow_egress=True)))

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8000",
                                     "http://127.0.0.1:11434",
                                     "http://192.168.1.50:8888"])
    def test_a_refused_address_is_status_search_failed_reason_refused(
            self, monkeypatch, tmp_path, url):
        _use_config(monkeypatch, {"egress_doors": {"web_search": True,
                                                   "web_fetch": True}})
        a = ToolsmithAgent(workspace_root=str(tmp_path / "ws"),
                           searxng_url=url, searxng_timeout=3)
        reply = self._search(a)

        assert reply["ok"] is False
        assert reply["status"] == "search_failed"
        assert reply["reason"] == "refused"
        # NOT the offline wording. Sending the owner to restart a container
        # that is fine is the dishonest-failure mode this module exists to
        # prevent, and "refused" and "unreachable" are different facts.
        assert reply.get("offline") is not True
        assert "offline" not in reply["error"].lower()
        assert "isn't running" not in reply["error"]
        assert "may open a socket to" in reply["error"]
        assert SEARXNG_LOOPBACK_ENDPOINT in reply["error"]

    def test_a_refusal_is_distinguishable_from_an_unreachable_service(
            self, monkeypatch, tmp_path):
        """Both fail; the caller must be able to tell WHICH."""
        _use_config(monkeypatch, {"egress_doors": {"web_search": True,
                                                   "web_fetch": True}})
        refused = self._search(ToolsmithAgent(
            workspace_root=str(tmp_path / "a"),
            searxng_url="http://127.0.0.1:8000", searxng_timeout=3))
        # Nothing is listening on the allowed endpoint in this test.
        unreachable = self._search(ToolsmithAgent(
            workspace_root=str(tmp_path / "b"),
            searxng_url=f"http://{SEARXNG_LOOPBACK_ENDPOINT}",
            searxng_timeout=3))

        assert refused["reason"] == "refused"
        assert unreachable["reason"] in ("unreachable", "timeout")
        assert refused["status"] == unreachable["status"] == "search_failed"
