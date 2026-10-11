"""The keystone: a manifest's declared hosts are a door the box enforces.

A manifest's ``doors[].hosts[]`` is not only schema-checked at load: it is
registered with ``security/egress_doors``, so the enforced table knows every
app's hosts, and ``app_registry`` reports ``enforced`` only where that is true
(``declared_only`` otherwise). ``test_app_contract.py`` pins the label side;
this file pins the enforcement side.

What is asserted:

1. **Registration.** Every installed app's doors land in the enforced table as
   ``app:<app_id>:<n>``, forced shut, with the manifest's hosts, purpose and
   ``data_sent_plain`` carried through — and they leave again on uninstall.
2. **An app cannot open its own door.** No manifest key opens anything; the
   precedence chain (env > overlay > config > default) is the one every
   built-in door uses, unchanged, and a host set that CHANGES loses the
   owner's old consent rather than inheriting it.
3. **A shut door refuses**, before DNS and before a transport exists.
4. **An undeclared host refuses even when the door is open** — exact match,
   not a suffix, so ``evil-api.example.com`` does not ride in on
   ``api.example.com``.
5. **A declared host on 443 passes the pre-connect checks**, with the address
   validation and the pin still in front of it.
6. **The ledger names the door and never the query string.**
7. **The label reflects reality**: ``enforced`` for an app whose doors are in
   the table, ``declared_only`` for one whose are not.

No test here opens a socket. Every one injects its own resolver and stands a
recording transport in for the pinned session; ``no_sockets`` makes every
socket primitive in the process fatal so that "refused" can only mean "never
dialled".
"""

from __future__ import annotations

import socket
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import app_registry  # noqa: E402
from puffpai.security import egress_admin as ea  # noqa: E402
from puffpai.security import egress_doors as ed  # noqa: E402
from puffpai.security import egress_ledger as el  # noqa: E402

#: A globally routable literal, so an "allowed" path here is allowed for the
#: right reason rather than because the address check was skipped.
PUBLIC_ADDR = "93.184.216.34"

#: The host our synthetic app declares, and one that merely looks like it.
DECLARED_HOST = "api.example.com"
LOOKALIKE_HOST = "evil-api.example.com"

SHIPPED = ("alarmflow", "calendar", "freshtable", "fitfam", "trivia")


def _manifest(app_id: str, hosts, purpose="Fetch the thing.",
              data_sent="The thing you asked for.", **extra) -> dict:
    door = {"hosts": list(hosts), "purpose": purpose,
            "data_sent_plain": data_sent}
    door.update(extra)
    return {"id": app_id, "doors": [door]}


class _SocketOpened(AssertionError):
    """Raised if anything in the process tries to reach the network."""


@pytest.fixture
def no_sockets(monkeypatch):
    """Every socket primitive is fatal — including ``getaddrinfo``.

    Deliberate, and the reason every test below injects its own resolver: a
    test that quietly fell back to the system resolver would be testing this
    household's DNS instead of this code.
    """
    def boom(*_a, **_kw):
        raise _SocketOpened("a socket was opened before the door was checked")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    return boom


@pytest.fixture
def no_transport(monkeypatch):
    """Building a transport at all is fatal.

    This is what separates "refused before the socket" from "refused by the
    pin": a shut door and an undeclared host must be turned away before any
    client exists to be pinned.
    """
    def boom(_d):
        raise _SocketOpened("a transport was built before the door was checked")

    monkeypatch.setattr(ed, "_pinned_session", boom)
    return boom


@pytest.fixture
def resolves(monkeypatch):
    """Install a resolver: ``resolves({"api.example.com": PUBLIC_ADDR})``."""
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


@pytest.fixture
def transport(monkeypatch):
    """A stand-in for the pinned session that records what it was asked to dial."""
    calls: list = []

    class _Session:
        def request(self, method, url, **kw):
            calls.append((method, url))
            return _FakeResponse(url=url)

        def close(self):
            return None

    monkeypatch.setattr(ed, "_pinned_session", lambda _d: _Session())
    return calls


@pytest.fixture
def flags(monkeypatch):
    """Drive the config and overlay tiers directly: ``flags(config=..., overlay=...)``."""
    def install(config=None, overlay=None):
        monkeypatch.setattr(ed, "_raw_config",
                            lambda: {ed.CONFIG_SECTION: dict(config or {})})
        monkeypatch.setattr(ed, "_raw_overlay", lambda: dict(overlay or {}))
    install()
    return install


@pytest.fixture(autouse=True)
def restore_registration():
    """Every test owns the app-door table; the shipped apps come back after."""
    yield
    ed.clear_app_doors()
    app_registry.rescan()


@pytest.fixture
def ledger(tmp_path):
    """Point the durable ledger at a temp file and hand back a reader."""
    path = tmp_path / "egress.jsonl"
    el.set_path_override(str(path))
    el.forget_mode_check(str(path))
    yield lambda: [
        __import__("json").loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ] if path.exists() else []
    el.set_path_override(None)


# ── 1. registration ──────────────────────────────────────────────────────────

def test_every_shipped_app_registers_a_namespaced_door_that_is_shut():
    app_registry.rescan()
    ids = ed.door_ids()
    for app_id in SHIPPED:
        door_id = f"app:{app_id}:0"
        assert door_id in ids, f"{app_id} declared a door that never registered"
        d = ed.door(door_id)
        assert d is not None
        assert d.default is False
        assert ed.egress_allowed(door_id)[0] is False
        assert ed.app_of(d) == app_id


def test_the_shipped_apps_declare_no_host_so_their_doors_reach_nothing():
    """All five say ``hosts: []``. A door with no host refuses every address."""
    app_registry.rescan()
    for app_id in SHIPPED:
        d = ed.door(f"app:{app_id}:0")
        assert d.hosts == ()
        # Not offered as a switch either: a control that cannot change what
        # leaves the house is the same dishonesty as a lying status page.
        assert ea.toggleable(d)[0] is False
        row = [r for r in ed.door_status() if r["id"] == d.id][0]
        assert row["writable"] is False
        assert row["app"] == app_id


def test_registration_carries_purpose_and_data_sent_plain():
    ed.register_app_doors([("weatherpack",
                            _manifest("weatherpack", [DECLARED_HOST],
                                      purpose="Hourly forecast.",
                                      data_sent="Your postcode.")["doors"])])
    d = ed.door("app:weatherpack:0")
    assert d.purpose == "Hourly forecast."
    assert d.data_sent == "Your postcode."
    assert d.hosts == (DECLARED_HOST,)


def test_a_second_door_gets_its_own_index_and_its_own_switch():
    doors = [{"hosts": ["a.example.com"], "purpose": "one",
              "data_sent_plain": "nothing"},
             {"hosts": ["b.example.com"], "purpose": "two",
              "data_sent_plain": "nothing"}]
    ed.register_app_doors([("twodoor", doors)])
    assert ed.door("app:twodoor:0").hosts == ("a.example.com",)
    assert ed.door("app:twodoor:1").hosts == ("b.example.com",)
    assert (ed.door("app:twodoor:0").config_flag
            != ed.door("app:twodoor:1").config_flag)


def test_uninstalling_an_app_removes_its_door():
    """A rescan IS the uninstall, and a removed app must lose its door."""
    ed.register_app_doors([("goingaway", _manifest("goingaway",
                                                   [DECLARED_HOST])["doors"])])
    assert ed.door("app:goingaway:0") is not None
    ed.register_app_doors([])
    assert ed.door("app:goingaway:0") is None
    assert ed.egress_allowed("app:goingaway:0")[0] is False


def test_a_manifest_cannot_shadow_a_builtin_door():
    """Namespacing is the control; this proves it holds for every built-in id."""
    for builtin in ed.door_ids():
        if builtin.startswith(ed.APP_DOOR_PREFIX):
            continue
        ed.register_app_doors([(builtin.replace(":", "_"),
                                _manifest("x", ["evil.example.com"])["doors"])])
        assert ed.door(builtin) is ed._BY_ID[builtin], (
            f"an app door displaced the built-in {builtin!r}")


def test_an_unusable_door_block_registers_nothing_rather_than_something_partial():
    """Fail shut: a door this module cannot read is a door that does not exist."""
    for bad in ([{"hosts": "notalist", "purpose": "p", "data_sent_plain": "d"}],
                [{"hosts": ["*.example.com"], "purpose": "p",
                  "data_sent_plain": "d"}],
                [{"hosts": ["https://api.example.com"], "purpose": "p",
                  "data_sent_plain": "d"}],
                [{"hosts": ["api.example.com:8443"], "purpose": "p",
                  "data_sent_plain": "d"}],
                [{"hosts": ["api.example.com/path"], "purpose": "p",
                  "data_sent_plain": "d"}],
                ["not a dict"]):
        ed.register_app_doors([("badapp", bad)])
        assert ed.door("app:badapp:0") is None, bad


# ── 2. an app cannot open its own door ───────────────────────────────────────

def test_no_manifest_key_opens_a_door(flags):
    """``default``, ``open``, ``enabled`` in the manifest change nothing."""
    for key in ("default", "open", "enabled", "allow", "consent"):
        ed.register_app_doors([("pushy", _manifest("pushy", [DECLARED_HOST],
                                                   **{key: True})["doors"])])
        d = ed.door("app:pushy:0")
        assert d is not None and d.default is False
        assert ed.egress_allowed("app:pushy:0")[0] is False, key


@pytest.mark.parametrize("source,cfg,ovl,env,expected", [
    # default: nothing has an opinion -> shut
    ("default", None, None, None, False),
    # config tier alone
    ("config", True, None, None, True),
    ("config", False, None, None, False),
    # overlay beats config, in both directions
    ("overlay", False, True, None, True),
    ("overlay", True, False, None, False),
    # env beats everything, in both directions
    ("env", True, True, "0", False),
    ("env", False, False, "1", True),
])
def test_the_precedence_chain_is_the_one_every_other_door_uses(
        monkeypatch, flags, source, cfg, ovl, env, expected):
    ed.register_app_doors([("prec", _manifest("prec", [DECLARED_HOST])["doors"])])
    d = ed.door("app:prec:0")
    flags(config={} if cfg is None else {d.config_flag: cfg},
          overlay={} if ovl is None else {d.config_flag: ovl})
    if env is None:
        monkeypatch.delenv(d.env_var, raising=False)
    else:
        monkeypatch.setenv(d.env_var, env)
    assert ed.egress_allowed(d.id)[0] is expected
    assert ed.flag_source(d) == source


def test_a_junk_flag_leaves_an_app_door_shut(flags):
    ed.register_app_doors([("junk", _manifest("junk", [DECLARED_HOST])["doors"])])
    d = ed.door("app:junk:0")
    flags(overlay={d.config_flag: "sure, why not"})
    assert ed.egress_allowed(d.id)[0] is False


def test_changing_the_declared_hosts_loses_the_owners_old_consent(flags):
    """An app update that repoints a door does NOT inherit the owner's "on".

    The consent flag carries a digest of the host set it was given for, so a
    new host set is a new key that no tier has an opinion on — and the fallback
    is ``default``, which is shut. Consent does not survive a redefinition of
    what it was consent to.
    """
    ed.register_app_doors([("drift", _manifest("drift", [DECLARED_HOST])["doors"])])
    original = ed.door("app:drift:0")
    flags(overlay={original.config_flag: True})
    assert ed.egress_allowed("app:drift:0")[0] is True

    # The author ships an update pointing the same door somewhere else.
    ed.register_app_doors([("drift", _manifest("drift",
                                               ["someone-else.example.com"])["doors"])])
    moved = ed.door("app:drift:0")
    assert moved.config_flag != original.config_flag
    assert ed.egress_allowed("app:drift:0")[0] is False


# ── 3. a shut door refuses, before DNS and before a transport ────────────────

def test_a_shut_app_door_refuses_without_dns_or_a_transport(
        flags, no_sockets, no_transport):
    ed.register_app_doors([("shut", _manifest("shut", [DECLARED_HOST])["doors"])])
    with pytest.raises(ed.EgressBlocked) as caught:
        ed.app_egress_request("shut", 0, "GET", f"https://{DECLARED_HOST}/v1/x")
    assert caught.value.door_id == "app:shut:0"
    assert "shut" in caught.value.user_message.lower()


def test_an_app_that_is_not_installed_has_no_door_at_all(no_sockets, no_transport):
    ed.register_app_doors([])
    with pytest.raises(ed.EgressBlocked) as caught:
        ed.app_egress_request("ghost", 0, "GET", f"https://{DECLARED_HOST}/v1/x")
    assert caught.value.door_id == "app:ghost:0"
    assert "no egress door" in str(caught.value)


# ── 4. an undeclared host refuses even with the door open ────────────────────

def test_an_undeclared_host_is_refused_with_the_door_open(
        flags, resolves, no_sockets, no_transport):
    ed.register_app_doors([("open1", _manifest("open1", [DECLARED_HOST])["doors"])])
    d = ed.door("app:open1:0")
    flags(overlay={d.config_flag: True})
    resolves({"*": PUBLIC_ADDR})
    assert ed.egress_allowed(d.id)[0] is True

    with pytest.raises(ed.EgressBlocked) as caught:
        ed.app_egress_request("open1", 0, "GET",
                              f"https://{LOOKALIKE_HOST}/v1/x")
    assert "is not declared by door" in caught.value.reason
    assert LOOKALIKE_HOST in caught.value.user_message


def test_a_door_that_names_no_host_refuses_everything_even_when_open(
        flags, resolves, no_sockets, no_transport):
    """The shape all five shipped apps have. Opening it changes nothing."""
    ed.register_app_doors([("nowhere", _manifest("nowhere", [])["doors"])])
    d = ed.door("app:nowhere:0")
    flags(overlay={d.config_flag: True})
    resolves({"*": PUBLIC_ADDR})
    assert ed.egress_allowed(d.id)[0] is True
    with pytest.raises(ed.EgressBlocked):
        ed.app_egress_request("nowhere", 0, "GET",
                              f"https://{DECLARED_HOST}/v1/x")


@pytest.mark.parametrize("url", [
    "http://api.example.com/v1/x",            # not https
    "https://api.example.com:8443/v1/x",      # not 443
    "https://user:pw@api.example.com/v1/x",   # credentials in the url
    "https://api.example.com.evil.test/x",    # suffix game
    "https://xapi.example.com/x",             # prefix game
])
def test_the_shapes_a_door_never_covers(
        url, flags, resolves, no_sockets, no_transport):
    ed.register_app_doors([("shapes", _manifest("shapes", [DECLARED_HOST])["doors"])])
    d = ed.door("app:shapes:0")
    flags(overlay={d.config_flag: True})
    resolves({"*": PUBLIC_ADDR})
    with pytest.raises(ed.EgressBlocked):
        ed.app_egress_request("shapes", 0, "GET", url)


def test_a_declared_host_that_resolves_indoors_is_refused(
        flags, resolves, no_sockets, no_transport):
    """DNS rebinding, attempted through a manifest-declared host."""
    ed.register_app_doors([("indoors", _manifest("indoors", [DECLARED_HOST])["doors"])])
    d = ed.door("app:indoors:0")
    flags(overlay={d.config_flag: True})
    resolves({DECLARED_HOST: "192.168.1.1"})
    with pytest.raises(ed.EgressBlocked) as caught:
        ed.app_egress_request("indoors", 0, "GET", f"https://{DECLARED_HOST}/v1/x")
    assert caught.value.reason.startswith(ed.REASON_RESOLVED_PRIVATE)
    assert "192.168.1.1" not in caught.value.reason
    assert "192.168.1.1" not in caught.value.user_message


# ── 5. a declared host on 443 passes the pre-connect checks ──────────────────

def test_a_declared_public_host_on_443_reaches_the_transport(
        flags, resolves, no_sockets, transport):
    ed.register_app_doors([("good", _manifest("good", [DECLARED_HOST])["doors"])])
    d = ed.door("app:good:0")
    flags(overlay={d.config_flag: True})
    resolves({DECLARED_HOST: PUBLIC_ADDR})
    resp = ed.app_egress_get("good", 0, f"https://{DECLARED_HOST}/v1/x?q=secret")
    assert resp.status_code == 200
    assert transport == [("GET", f"https://{DECLARED_HOST}/v1/x?q=secret")]


def test_a_redirect_off_the_declared_host_is_refused_at_the_hop(
        flags, resolves, no_sockets, monkeypatch):
    ed.register_app_doors([("hop", _manifest("hop", [DECLARED_HOST])["doors"])])
    d = ed.door("app:hop:0")
    flags(overlay={d.config_flag: True})
    resolves({"*": PUBLIC_ADDR})

    class _Session:
        def request(self, method, url, **kw):
            return _FakeResponse(
                status_code=302,
                headers={"Location": f"https://{LOOKALIKE_HOST}/next"},
                url=url)

        def close(self):
            return None

    monkeypatch.setattr(ed, "_pinned_session", lambda _d: _Session())
    with pytest.raises(ed.EgressBlocked) as caught:
        ed.app_egress_get("hop", 0, f"https://{DECLARED_HOST}/v1/x")
    assert "redirect hop" in caught.value.reason


# ── 6. the ledger names the door, never the query ────────────────────────────

def test_the_ledger_names_the_door_and_drops_the_query(
        flags, resolves, no_sockets, transport, ledger):
    ed.register_app_doors([("led", _manifest("led", [DECLARED_HOST])["doors"])])
    d = ed.door("app:led:0")
    flags(overlay={d.config_flag: True})
    resolves({DECLARED_HOST: PUBLIC_ADDR})
    ed.app_egress_get("led", 0,
                      f"https://{DECLARED_HOST}/v1/x?token=hunter2&who=fakeperson")

    rows = [r for r in ledger() if r.get("door") == "app:led:0"]
    assert rows, "the allowed request wrote no ledger line"
    blob = __import__("json").dumps(rows)
    assert "hunter2" not in blob and "fakeperson" not in blob
    assert "?" not in rows[-1].get("path", "")
    assert rows[-1]["host"] == DECLARED_HOST


def test_a_refusal_is_ledgered_under_the_app_door_name(
        flags, no_sockets, no_transport, ledger):
    ed.register_app_doors([("refused", _manifest("refused",
                                                 [DECLARED_HOST])["doors"])])
    with pytest.raises(ed.EgressBlocked):
        ed.app_egress_get("refused", 0, f"https://{DECLARED_HOST}/v1/x?q=private")
    rows = [r for r in ledger() if r.get("door") == "app:refused:0"]
    assert rows and rows[-1]["result"] == "refused"
    assert "private" not in __import__("json").dumps(rows)


# ── 7. the label reflects reality ────────────────────────────────────────────

def test_a_registered_app_is_labelled_enforced():
    app_registry.rescan()
    assert app_registry.HOSTS_ENFORCEMENT == "enforced"
    for record in app_registry.ui_records(app_registry.current_manifests()):
        assert record["hosts_enforcement"] == "enforced"
        assert "refused" in record["hosts_notice"]
    assert app_registry.hosts_enforcement()[0] == "enforced"


def test_an_app_whose_doors_did_not_register_keeps_the_declared_only_line():
    """The honest label is not deleted; it is reserved for the case it describes."""
    app_registry.register_egress_doors([
        {"id": "cleanapp", "doors": [{"hosts": [DECLARED_HOST],
                                      "purpose": "p", "data_sent_plain": "d"}]},
        # A door this module refuses to build: the app declared one and got none.
        {"id": "brokenapp", "doors": [{"hosts": ["*.example.com"],
                                       "purpose": "p", "data_sent_plain": "d"}]},
    ])
    assert app_registry.hosts_enforcement("cleanapp")[0] == "enforced"
    assert app_registry.hosts_enforcement("brokenapp")[0] == "declared_only"
    assert "does not yet enforce" in app_registry.hosts_enforcement("brokenapp")[1]
    # The aggregate is the WEAKEST member, so no banner overclaims.
    assert app_registry.hosts_enforcement()[0] == "declared_only"


def test_the_pack_summary_takes_the_weakest_member():
    by_id = {
        "cleanapp": {"id": "cleanapp",
                     "doors": [{"hosts": [DECLARED_HOST], "purpose": "p",
                                "data_sent_plain": "d"}]},
        "brokenapp": {"id": "brokenapp",
                      "doors": [{"hosts": ["*.example.com"], "purpose": "p",
                                 "data_sent_plain": "d"}]},
    }
    app_registry.register_egress_doors(list(by_id.values()))
    good = app_registry.pack_privacy_summary(["cleanapp"], by_id)
    assert good["hosts_enforcement"] == "enforced"
    mixed = app_registry.pack_privacy_summary(["cleanapp", "brokenapp"], by_id)
    assert mixed["hosts_enforcement"] == "declared_only"


def test_status_lists_app_doors_beside_the_builtin_ones():
    """The dashboard egress card lists whatever the registry reports."""
    app_registry.rescan()
    rows = ed.door_status()
    by_id = {r["id"]: r for r in rows}
    assert "weather" in by_id and "app:trivia:0" in by_id
    row = by_id["app:trivia:0"]
    assert row["open"] is False
    assert row["enforcement"] == "registry"
    assert row["enforced"] is True
    assert row["default"] is False
    assert row["config_flag"].startswith(f"{ed.CONFIG_SECTION}.app_trivia_0_")
    assert by_id["weather"]["app"] is None


# ── 8. there is no OTHER app-originated path, and that is by construction ────
#
# "Enforced" is only worth the word if the enforced path is the only one. In
# this build it is, for a reason that is structural rather than diligent: a v1
# app is DECLARATIVE. It ships a manifest and no code, so it has no way to open
# a socket of its own — the box's own tools act on its data, and every one of
# those that reaches the wire does so through a BUILT-IN door
# (``assistant_tools.web_search``, ``radio_browser``, ``geoip``, ``geocode``,
# ``weather``), none of which is app-owned. ``app_egress_request`` therefore has
# no caller today: it is the door that stands ready for the first app manifest
# that declares a host, and these two tests are what keep that claim true.


@pytest.mark.parametrize("key", ["handler", "module", "script", "exec",
                                 "command", "entrypoint", "plugin", "run"])
def test_a_v1_app_cannot_carry_code_so_it_has_no_path_of_its_own(key):
    """The structural reason the enforced path is the only path.

    An app that could run code could open its own socket, and no door would
    see it. The registry refuses the shape outright — at the top level and
    inside a door block — which is what makes "every app-originated call goes
    through the door" a property rather than a hope.
    """
    assert app_registry.validate_manifest({"id": "codey", "name": "Codey",
                                           key: "anything"}) is not None
    assert app_registry.validate_manifest({
        "id": "codey", "name": "Codey",
        "doors": [{"hosts": [], "purpose": "p", "data_sent_plain": "d",
                   key: "anything"}]}) is not None


def test_the_app_path_is_the_wrapper_and_not_a_second_set_of_checks(monkeypatch):
    """``app_egress_request`` delegates; it never re-implements the decision.

    A parallel path is a second set of checks to get wrong. This pins the
    delegation so that a future edit adding "just a small shortcut" for apps
    fails here instead of quietly bypassing the shut-door check, the exact-host
    allowlist, the address validation or the pin.
    """
    seen: list = []
    monkeypatch.setattr(ed, "egress_request",
                        lambda *a, **kw: seen.append(a) or "sentinel")
    assert ed.app_egress_request("someapp", 2, "POST", "https://h/x") == "sentinel"
    assert seen == [("app:someapp:2", "POST", "https://h/x")]


def test_the_aggregate_label_does_not_overclaim_before_a_scan():
    """Vacuous truth is not honesty.

    With nothing registered yet, every per-app answer is ``declared_only``. An
    aggregate that returned ``enforced`` in that state would disagree with all
    of them and would tell a caller the box enforces something it has not yet
    looked at.
    """
    ed.clear_app_doors()
    app_registry.register_egress_doors([])
    assert app_registry.hosts_enforcement()[0] == "declared_only"
    assert app_registry.hosts_enforcement("anything")[0] == "declared_only"


# ── 9. a door names a HOST, never an address ────────────────────────────────
#
# A hostname-shape regex such as `_APP_HOST_RE` (and the mirrored
# `app_registry._HOST_RE`) accepts a dotted quad on its own, because a dotted
# quad is a perfectly legal hostname SHAPE. Without the literal check,
# `{"hosts": ["8.8.8.8"]}` would validate, register as a door, and once the
# owner opened it `app_egress_request` would put `https://8.8.8.8/x` on the
# transport.
#
# Why that matters even though the address policy still runs: the door layer's
# whole design is "a declared NAME is not a destination — resolve it and judge
# every answer". A literal lets a manifest name the destination directly, and
# the only thing left between it and the wire is the public/private class
# check. A public literal (such as 8.8.8.8) sails through it. The
# owner's card would read "this app talks to 8.8.8.8" with no name to look up,
# no resolution to judge, and no way for a future LAN posture to apply.

#: Every notation the platform resolver honours. Each is a way of writing an
#: address that `_APP_HOST_RE` alone would have taken for a name.
IP_LITERALS = [
    "8.8.8.8",                # a PUBLIC literal
    "127.0.0.1",              # dotted quad, loopback
    "169.254.169.254",        # link-local, the cloud-metadata classic
    "010.010.010.010",        # octal / zero-padded
    "0x08080808",             # hex, one part
    "0x7f.1",                 # hex mixed with shorthand
    "2130706433",             # a bare integer: 127.0.0.1
    "127.1",                  # shorthand, two parts
    "10.1.2",                 # shorthand, three parts
    "::1",                    # IPv6 loopback
    "[::1]",                  # IPv6, bracketed URL form
    "fd00::1",                # IPv6 unique-local
    "[2001:db8::1]",          # IPv6, bracketed and documentation-range
    "::ffff:127.0.0.1",       # IPv4-mapped IPv6
    "::ffff:8.8.8.8",         # IPv4-mapped IPv6, public
    "localhost",              # a bare single label is never a door host
]


def _lit_manifest(host: str) -> dict:
    """A manifest that is valid in every way EXCEPT the host under test.

    Built from the shipped shape so that a refusal can only be about the host.
    ``test_the_control_manifest_is_otherwise_valid`` proves that.
    """
    return {
        "schema": "puffpai_app/v1",
        "id": "litapp",
        "name": "Lit",
        "blurb": "A test app.",
        "version": "1.0.0",
        "surfaces": ["daily.games"],
        "doors": [{"hosts": [host], "purpose": "Fetch the thing.",
                   "data_sent_plain": "The thing you asked for."}],
    }


def test_the_control_manifest_is_otherwise_valid():
    """Without this, every refusal below could be about a missing field."""
    assert app_registry.validate_manifest(_lit_manifest(DECLARED_HOST)) is None


@pytest.mark.parametrize("literal", IP_LITERALS)
def test_a_manifest_cannot_declare_an_address_instead_of_a_name(literal):
    """Neither validator accepts it, and no door is registered for it."""
    assert app_registry._is_ip_literal(literal) or literal == "localhost"
    assert not app_registry._is_registrable_hostname(literal)
    assert not ed._is_registrable_hostname(literal)

    # The loader refuses the manifest outright...
    problem = app_registry.validate_manifest(_lit_manifest(literal))
    assert problem is not None and literal in problem

    #...and the enforcement point refuses to register it even if it somehow
    # arrived anyway. Two controls, neither relying on the other having run.
    ed.register_app_doors([("litapp", [{"hosts": [literal], "purpose": "p",
                                        "data_sent_plain": "d"}])])
    assert ed.door("app:litapp:0") is None


@pytest.mark.parametrize("literal", IP_LITERALS)
def test_the_address_form_never_reaches_the_transport(
        literal, flags, resolves, no_sockets, no_transport):
    """The end of the chain: no door, so no request, so no socket."""
    ed.register_app_doors([("litapp", [{"hosts": [literal], "purpose": "p",
                                        "data_sent_plain": "d"}])])
    resolves({"*": PUBLIC_ADDR})
    with pytest.raises(ed.EgressBlocked) as caught:
        ed.app_egress_get("litapp", 0, f"https://{literal.strip('[]')}/x")
    assert "no egress door" in str(caught.value)


def test_one_bad_host_refuses_the_whole_door_not_just_that_host():
    """A door mixing a name and a literal registers NOTHING.

    Dropping the literal and keeping the name would leave the owner an
    allowlist that is not the one the manifest declared, which is a quieter
    version of the same lie.
    """
    ed.register_app_doors([("mixed", [{"hosts": [DECLARED_HOST, "8.8.8.8"],
                                       "purpose": "p",
                                       "data_sent_plain": "d"}])])
    assert ed.door("app:mixed:0") is None


@pytest.mark.parametrize("name", [
    "api.example.com",
    "feeds.npr.org",
    "a.b.co",
    "api2.example.com",
    "xn--p1ai.example.com",   # ACE label, not the last one
    "example.xn--p1ai",       # an internationalised TLD IS registrable
])
def test_real_hostnames_still_register(name):
    """The fix must not cost a legitimate manifest its door."""
    assert app_registry._is_registrable_hostname(name)
    assert ed._is_registrable_hostname(name)
    ed.register_app_doors([("okapp", [{"hosts": [name], "purpose": "p",
                                       "data_sent_plain": "d"}])])
    assert ed.door("app:okapp:0").hosts == (name,)


def test_every_builtin_door_host_is_still_a_registrable_name():
    """The rule is not stricter than the doors this build already ships."""
    for d in ed.DOORS:
        for host in d.hosts:
            assert ed._is_registrable_hostname(host), f"{d.id} -> {host}"
