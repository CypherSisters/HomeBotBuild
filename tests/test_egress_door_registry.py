"""The egress-door registry.

The hazard these tests close: a tool in ``assistant_tools.py`` that reaches a
third-party cloud endpoint gated by nothing but
``get_network_status.has_internet`` fires while ``/system/status`` reports
``chat_allow_egress:false``. That is consent the owner never gave, to a party
the owner was never shown.

The properties asserted here, in the order they matter:

1. every door defaults **shut** in code, and stays shut when the config has no
   ``egress_doors`` section at all;
2. a shut door refuses **before a socket opens** — proven by making every
   socket primitive in the process raise;
3. a host the door does not declare is refused even when the door is open;
4. a shut door is still refused with ``chat_allow_egress`` ON — the master
   chat flip is not consent for any particular third-party service, and a door
   opens only under its own exact ``config_flag``;
5. redirects are re-validated per hop against the same host set;
6. no ``requests``/``urllib``/``httpx`` call exists in ``src/puffpai`` outside
   the registry, its declared LAN/loopback exceptions, or the Toolsmith's own
   hardened fetch.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.security import egress_doors as ed  # noqa: E402


SRC = Path(__file__).resolve().parents[1] / "src" / "puffpai"


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Every test starts from 'no config, no environment' — i.e. all shut."""
    for d in ed.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    ed.reset_cache()
    yield
    ed.reset_cache()


def _use_config(monkeypatch, payload: dict) -> None:
    """Make *payload* the WHOLE of this appliance's egress configuration.

    The overlay tier OUTRANKS the config file (``egress_doors._resolve_flag``),
    so a test that repoints only ``_raw_config`` is not testing what its name
    says: conftest's isolation fixture seeds the two Toolsmith doors into an
    overlay, and so would a developer who once ran the API locally. Both tiers
    are neutralised here so "no config and no env" really means every tier is
    silent.
    """
    written = json.dumps(payload)

    def fake_raw() -> dict:
        return json.loads(written)

    monkeypatch.setattr(ed, "_raw_config", fake_raw)
    monkeypatch.setattr(ed, "_raw_overlay", lambda: {})


class _SocketOpened(AssertionError):
    """Raised if anything in the process tries to reach the network."""


@pytest.fixture
def no_sockets(monkeypatch):
    """Make every socket primitive fatal, so 'refused' means 'never dialled'.

    This is the whole point of the row: the previous code called
    ``has_internet`` — which itself opens TCP connections to 1.1.1.1, 8.8.8.8
    and www.google.com — before it made any consent decision. A gate that runs
    after a connectivity probe has already egressed.
    """
    def boom(*_a, **_kw):
        raise _SocketOpened("a socket was opened before the door was checked")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    return boom


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, url=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url

    def json(self):
        return {}

    def raise_for_status(self):
        return None


@pytest.fixture
def fake_requests(monkeypatch):
    """A stand-in ``requests`` module that records calls instead of dialling."""
    calls: list[tuple[str, str]] = []
    responses: list[_FakeResponse] = []

    def request(method, url, **_kw):
        calls.append((method, url))
        return responses.pop(0) if responses else _FakeResponse(url=url)

    class _Session:
        """Stands in for the pinned session ``egress_request`` builds.

        The pin itself is transport-level and has its own tests
        (``test_security_hostname_not_destination.py``); here the point is only that the
        wrapper's door/host/redirect logic is exercised without a socket.
        """

        def request(self, method, url, **kw):
            # Through the module object, not the closure: tests repoint
            # ``fake_requests.request`` to simulate a transport failure.
            return mod.request(method, url, **kw)

        def close(self):
            return None

    monkeypatch.setattr(ed, "_pinned_session", lambda d: _Session())

    mod = types.ModuleType("requests")
    mod.request = request
    mod.compat = types.SimpleNamespace(
        urljoin=__import__("urllib.parse", fromlist=["urljoin"]).urljoin)
    monkeypatch.setitem(sys.modules, "requests", mod)
    mod.calls = calls          # type: ignore[attr-defined]
    mod.responses = responses  # type: ignore[attr-defined]
    return mod


# ── 1. default shut ──────────────────────────────────────────────────────────

def test_every_door_declares_default_off():
    """The invariant is a value, not a comment: no door ships open."""
    assert ed.DOORS, "the registry is empty"
    for d in ed.DOORS:
        assert d.default is False, f"door {d.id} ships open"


def test_every_door_refused_with_no_config_and_no_env(monkeypatch):
    _use_config(monkeypatch, {})
    for d in ed.DOORS:
        allowed, reason = ed.egress_allowed(d.id)
        assert allowed is False, f"door {d.id} opened by default: {reason}"


def test_unreadable_config_leaves_every_door_shut(monkeypatch):
    """A config that will not parse must not be read as consent."""
    monkeypatch.setattr(ed, "_config_path", lambda: "/nonexistent/puffpai.json")
    # The overlay outranks the config, so a seeded one would answer for the
    # doors this test is about. Silence that tier too: "unreadable config" only
    # means "every door shut" when nothing else has an opinion.
    monkeypatch.setattr(ed, "_raw_overlay", lambda: {})
    ed.reset_cache()
    for d in ed.DOORS:
        assert ed.egress_allowed(d.id)[0] is False


def test_junk_flag_value_is_not_consent(monkeypatch):
    _use_config(monkeypatch, {"egress_doors": {"weather": "maybe"}})
    allowed, reason = ed.egress_allowed("weather")
    assert allowed is False
    assert "not a flag value" in reason


def test_unknown_door_is_refused(monkeypatch):
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    assert ed.egress_allowed("no_such_door") == (
        False, "no egress door named 'no_such_door'")


# ── 2. refused before a socket opens ─────────────────────────────────────────

@pytest.mark.parametrize("door_id,url", [
    ("weather", "https://api.open-meteo.com/v1/forecast"),
    ("air_quality", "https://air-quality-api.open-meteo.com/v1/air-quality"),
    ("news_rss", "https://feeds.npr.org/1001/rss.xml"),
    ("geocode", "https://nominatim.openstreetmap.org/search"),
    ("geoip", "https://ipapi.co/json/"),
    ("radio_browser", "https://all.api.radio-browser.info/json/stations/search"),
])
def test_shut_door_refuses_without_opening_a_socket(monkeypatch, no_sockets,
                                                    door_id, url):
    _use_config(monkeypatch, {})
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get(door_id, url)
    assert excinfo.value.door_id == door_id
    assert excinfo.value.user_message  # something honest to show a person


def test_require_open_refuses_without_a_socket(monkeypatch, no_sockets):
    _use_config(monkeypatch, {})
    with pytest.raises(ed.EgressBlocked):
        ed.require_open("radio_browser")


# ── 3. only with its own flag, and only to its own hosts ─────────────────────

def test_door_opens_only_with_its_flag(monkeypatch, fake_requests):
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    resp = ed.egress_get("weather", "https://api.open-meteo.com/v1/forecast")
    assert resp.status_code == 200
    assert fake_requests.calls == [
        ("GET", "https://api.open-meteo.com/v1/forecast")]
    # Its neighbour stays shut: one flag opens one door.
    assert ed.egress_allowed("air_quality")[0] is False


def test_open_door_still_refuses_an_undeclared_host(monkeypatch, no_sockets):
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", "https://attacker.example/feed.xml")
    assert "not declared by door" in excinfo.value.reason


def test_open_door_refuses_a_plain_http_downgrade(monkeypatch, no_sockets):
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("weather", "http://api.open-meteo.com/v1/forecast")
    assert "not https" in excinfo.value.reason


def test_open_door_refuses_credentials_in_the_url(monkeypatch, no_sockets):
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    with pytest.raises(ed.EgressBlocked):
        ed.egress_get("weather", "https://user:pw@api.open-meteo.com/v1/forecast")


def test_environment_can_force_a_door_shut(monkeypatch, no_sockets):
    """An operator must be able to shut a door from the unit file."""
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    monkeypatch.setenv("PUFFPAI_EGRESS_WEATHER", "0")
    assert ed.egress_allowed("weather")[0] is False
    with pytest.raises(ed.EgressBlocked):
        ed.egress_get("weather", "https://api.open-meteo.com/v1/forecast")


# ── 4. a door opens under its own flag and under nothing else ───────────

def test_a_door_is_refused_even_with_chat_allow_egress_on(monkeypatch,
                                                          no_sockets):
    """The master chat flip is not consent for a specific service.

    Both the config key and the environment variable that the chat/agent budget
    reads are turned ON here, and three *other* doors are open. The shut door
    must still refuse, because it does not consult them: consent to "this chat
    turn may egress" is not consent to "talk to this particular host".
    """
    monkeypatch.setenv("PUFFPAI_CHAT_ALLOW_EGRESS", "1")
    _use_config(monkeypatch, {
        "agents": {"chat_allow_egress": True},
        "egress_doors": {"weather": True, "news_rss": True, "web_search": True},
    })
    allowed, _ = ed.egress_allowed("radio_browser")
    assert allowed is False
    for url in (
        "https://all.api.radio-browser.info/json/stations/search",
        "https://de1.api.radio-browser.info/json/stations/search",
    ):
        with pytest.raises(ed.EgressBlocked):
            ed.egress_get("radio_browser", url)


def test_a_door_opens_only_under_its_own_config_flag(monkeypatch):
    """One flag opens one door, by its exact ``config_flag`` name.

    The door *id* is not a flag, and neither is anything that merely resembles
    one. A mistyped or copy-pasted key leaves the door shut: consent is given
    to a named flag, not to a family resemblance.
    """
    d = ed.door("radio_browser")
    near_misses = {d.id + "_api": True, "third_party_" + d.id: True,
                   d.id.split("_")[0]: True}
    assert d.config_flag not in near_misses
    _use_config(monkeypatch, {"egress_doors": near_misses})
    assert ed.egress_allowed(d.id)[0] is False
    _use_config(monkeypatch, {"egress_doors": {d.config_flag: True}})
    assert ed.egress_allowed(d.id)[0] is True


def test_every_third_party_door_names_its_party(monkeypatch):
    """A door onto someone else's service has to say whose, in its own row.

    ``third_party`` plus ``consent_kind="owner_flag_third_party"`` are the
    disclosure mechanism: an owner cannot consent to a party they were never
    told about, so the status row a UI renders carries the name and the
    "what leaves" sentence together.
    """
    _use_config(monkeypatch, {})
    rows = {r["id"]: r for r in ed.door_status()}
    for d in ed.DOORS:
        assert d.id in rows
        assert rows[d.id]["data_sent"], d.id
        if d.third_party:
            assert d.third_party.strip()
            assert rows[d.id]["third_party"] == d.third_party


# ── 5. redirects ─────────────────────────────────────────────────────────────

def test_redirect_off_the_door_hosts_is_refused(monkeypatch, fake_requests):
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    fake_requests.responses.append(_FakeResponse(
        status_code=302, headers={"Location": "https://attacker.example/x"}))
    with pytest.raises(ed.EgressBlocked) as excinfo:
        ed.egress_get("news_rss", "https://feeds.npr.org/1001/rss.xml")
    assert "redirect hop" in excinfo.value.reason
    # One hop attempted, and the attacker host was never dialled.
    assert fake_requests.calls == [("GET", "https://feeds.npr.org/1001/rss.xml")]


def test_redirect_within_the_door_hosts_is_followed(monkeypatch, fake_requests):
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    fake_requests.responses.append(_FakeResponse(
        status_code=301, headers={"Location": "https://feeds.npr.org/1001/rss"}))
    resp = ed.egress_get("news_rss", "https://feeds.npr.org/1001/rss.xml")
    assert resp.status_code == 200
    assert len(fake_requests.calls) == 2


# ── 6. status + ledger ───────────────────────────────────────────────────────

def test_door_status_lists_every_door_with_its_state(monkeypatch):
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    rows = ed.door_status()
    assert {r["id"] for r in rows} == set(ed.door_ids())
    by_id = {r["id"]: r for r in rows}
    assert by_id["weather"]["open"] is True
    assert by_id["radio_browser"]["open"] is False
    # Doors this registry does not enforce are listed and labelled, not hidden.
    assert by_id["weather"]["enforced"] is True
    for row in rows:
        assert row["data_sent"], f"{row['id']} does not say what leaves"
        assert "last_used" in row


def test_last_used_is_recorded_on_an_allowed_call(monkeypatch, fake_requests,
                                                  tmp_path):
    monkeypatch.setattr(ed, "_ledger_path",
                        lambda: str(tmp_path / "egress_doors.json"))
    ed._LAST_USED.clear()
    ed._ledger_loaded = False
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    ed.egress_get("weather", "https://api.open-meteo.com/v1/forecast")
    used = ed.last_used("weather")
    assert used and used["result"] == "allowed"
    assert "api.open-meteo.com" in used["detail"]
    # And nothing resembling a query string or payload is kept.
    assert "?" not in used["detail"]


# ── 7. no call site outside the registry ─────────────────────────────────────

#: Outbound-call constructs this test hunts for.
_CALL_RE = re.compile(
    # Every alias `import requests as X` uses in this tree, plus the bare name.
    r"(?<![\w.])(?:requests|_requests|_req|_rq|_ha_req)\s*\.\s*"
    r"(?:get|post|put|patch|delete|head|request|Session)\s*\("
    r"|(?<![\w.])urllib\.request\.|(?<![\w.])urlopen\s*\("
    r"|(?<![\w.])httpx\s*\.|(?<![\w.])aiohttp\s*\."
    # WebSockets. A ws:// connection is an outbound socket that carries a
    # credential and stays open, which is MORE exposure than a GET, not less —
    # and until this pattern existed the guard could not see one at all. The
    # bare ``create_connection(`` arm catches both the websocket library's
    # function and ``socket.create_connection``: raw TCP to a host chosen at
    # runtime leaves the house just as thoroughly as an HTTP call does.
    r"|(?<![\w.])websocket\s*\.\s*create_connection"
    r"|(?<![\w.])websockets\s*\."
    r"|(?<![\w.])create_connection\s*\("
)

#: Every remaining match, with the reason it is not an egress door.
#:
#: A call belongs here ONLY if it cannot leave the house: loopback, a LAN
#: address the owner configured, or a namespace with no network at all. Adding
#: a WAN host to this list instead of to ``DOORS`` is the exact mistake this
#: test exists to catch, so each entry names where it goes.
_NOT_EGRESS: dict[str, str] = {
    # The registry's own wrapper and its tests.
    "security/egress_doors.py": "the wrapper itself",
    # The wrapper's transport. It has no host of its own: it builds the pinned
    # requests.Session that egress_request hands a URL already checked against
    # a door, and its whole job is to refuse an address that door may not
    # reach. A call site here cannot introduce a destination.
    "security/netaddr.py": "the wrapper's pinned transport (no host of its own)",
    # The house-local wrapper. It has no host of its own: a caller hands it a
    # URL and it RESOLVES that host and refuses every address that is not
    # loopback, RFC 1918 or unique-local (netaddr posture HOUSE) before a
    # socket opens, then re-validates inside the pinned connection factory. A
    # call site here cannot introduce a destination outside this house, which
    # is the property the three AdGuard readers below rely on.
    "security/lan_http.py": "the house-local wrapper — refuses any address "
                            "that is not loopback/RFC1918/ULA (no host of "
                            "its own)",
    # The Toolsmith: its own budget + per-hop SSRF validator.
    # Registered as the 'web_fetch' door; enforcement stays there.
    "agents/toolsmith.py": "Toolsmith hardened fetch (door: web_fetch)",
    # Local model runtime — 127.0.0.1:11434.
    "api_server.py": "ollama loopback + LAN service probes + SearXNG loopback",
    "agent_graph.py": "ollama loopback",
    "inference/ollama_backend.py": "ollama loopback",
    "embeddings.py": "ollama loopback",
    "ingestion.py": "ollama loopback",
    "vision.py": "ollama loopback",
    "hatch.py": "ollama loopback probe",
    "ui/chat_view.py": "ollama loopback (desktop GUI)",
    "ui/onboarding.py": "ollama loopback (desktop GUI)",
    "ui/settings_view.py": "ollama loopback (desktop GUI)",
    "ui/main_window.py": "ollama loopback (desktop GUI)",
    "service.py": "own health endpoint on 127.0.0.1",
    "voice/service.py": "own brain endpoint on 127.0.0.1",
    # LAN devices the owner configured.
    "ha_bridge.py": "Home Assistant on the LAN",
    "ha_events.py": "Home Assistant websocket on the LAN",
    "media_bridge.py": "Jellyfin/Calibre on the LAN",
    "zigbee_manager.py": "Zigbee2MQTT on the LAN",
    "automation_client.py": "Node-RED on the LAN",
    "network_scanner.py": "Node-RED webhook on the LAN (CLI utility)",
    "zone3/adguard_controller.py": "AdGuard on the LAN",
    "zone3/pihole_controller.py": "Pi-hole on the LAN",
    "agents/caretaker.py": "LAN/loopback reachability probe",
    "offline_guard.py": "LAN/loopback reachability only (is_reachable refuses "
                        "an internet address before a socket opens); the "
                        "connectivity probe itself dials through "
                        "egress_doors.probe_reachable",
    # The build engine's listed exits and guards. Each checks, pins and logs
    # what it sends, or (the two guards) dials nothing at all.
    "security/pinned_net.py": "the pinned transport the listed exits use "
                              "(no host of its own)",
    "source_fetch.py": "a listed exit: every request goes through a door "
                       "(egress_request) and is checked and logged there",
    "security/lan_guard.py": "the home-network guard: builds proxy-free "
                             "clients for marked calls, dials nothing",
    "security/bridge_guard.py": "the Bridge's connect-time backstop: wraps "
                                "client libraries, dials nothing",
}

#: A line marked ``# lan-egress: <why>`` is a call to this computer or the home
#: network. security.lan_guard holds it to that at run time (an internet
#: address from a marked line is refused before a packet is sent) and the test
#: run fails on one (tests/conftest.py), so the marker is a check that runs,
#: not a comment.
_LAN_MARKER = "# lan-" "egress:"

#: Per-LINE exemptions, for files that hold BOTH a loopback call and the WAN
#: calls this row converted. assistant_tools.py is the file the Azure door was
#: hiding in, so it does not get a whole-file pass: only the exact loopback
#: lines below are exempt, and any other outbound call in it fails the test.
_NOT_EGRESS_LINES: dict[str, dict[str, str]] = {
    "assistant_tools.py": {
        'resp = _requests.post(': "ollama loopback (quiz generation)",
        'import requests as _rq': "ollama loopback (JSON extraction helper)",
        'r = _rq.post(f"{url}/api/chat", json=payload, timeout=45)':
            "ollama loopback (JSON extraction helper)",
    },
    # The Bridge's two readers. They take per-LINE entries rather than the
    # whole-file pass their LAN/loopback neighbours above get, because both
    # files are young and growing and both are in the business of describing
    # devices — exactly the shape of code that one day wants to ask a vendor
    # database on the internet what an OUI belongs to. A whole-file pass would
    # let that land silently. These two lines name the AdGuard control
    # endpoint they read; any other outbound call in either file still fails.
    # bridge_inventory.py's AdGuard read goes through puffpai.security.lan_http
    # (see security/lan_http.py above), so these two files carry no requests
    # entries: the house-local promise is a check that runs. What is left is
    # the Home Assistant WEBSOCKET, which _CALL_RE sees.
    "bridge_inventory.py": {
        'return websocket.create_connection(url, timeout=_HA_WS_TIMEOUT)':
            "Home Assistant's websocket on the LAN (config "
            "`home_assistant.url`, for example 192.168.1.140), the "
            "read-only device/entity/area registries, via ha_events.ws_url_for "
            "and ha_events._resolve_ha for the credential; same destination as "
            "ha_events.py above",
    },
    # HA provisioning under consent. Same destination as
    # ha_bridge.py — the Home Assistant the owner configured, on the LAN —
    # but this file carries the ELEVATED admin credential from the vault, so
    # it deliberately does NOT get the whole-file pass its everyday neighbour
    # has: only the throwaway sessions below are exempt, and any other
    # outbound call added to this file fails the test. The two lines are the
    # script/automation writer and the script-existence probe; both build a
    # session whose every request is f"{_ha_base_url}/api/..." off config
    # `home_assistant.url`.
    "ha_provisioning.py": {
        'session = requests.Session()':
            "Home Assistant on the LAN (config `home_assistant.url`) — a "
            "throwaway session carrying the vault-held admin credential, "
            "same destination as ha_bridge.py; the credential dies with the "
            "session and no other host is ever built from it",
    },
    # The coding model. Ollama on this box and nowhere else: the call is
    # reached only after is_loopback(url) — and a non-loopback Ollama URL is
    # a REFUSAL (CodeResult(error="non_loopback")), not a fallback — so a
    # household's source code cannot be posted off the box by pointing config
    # `ollama.url` somewhere else.
    # The planning engine's model calls. ``self._http`` is the ``requests``
    # module (or a test double); every URL is built on config `ollama.url`,
    # the same destination as inference/ollama_backend.py above.
    "agents/engine.py": {
        'resp = self._http.get(f"{self._url}/api/tags", timeout=5)':
            "ollama at config `ollama.url` (model inventory)",
        'resp = self._http.post(f"{self._url}/api/chat", json=body, timeout=timeout)':
            "ollama at config `ollama.url` (chat)",
        'resp = self._http.post(f"{self._url}/api/chat", json=body,':
            "ollama at config `ollama.url` (chat retry without think)",
    },
    # TV casting. Two calls through client objects, and only two:
    "tv_cast.py": {
        # Jellyfin on this box. The base comes from validate_local_base(),
        # which refuses any host that is not loopback or an address bound to
        # this machine, and allowed_upstream() keeps every path inside the
        # one channel being cast.
        'return self._http.request(method, self.base + path_query,':
            "Jellyfin on this box (validate_local_base)",
        # The free-channel relay. This one leaves the house, through the
        # tv_free_channels switch enforced at the call site: _relay_get asks
        # require_open("tv_free_channels") before every hop, records every
        # hop with record_external, and connects over netaddr's pinned
        # PUBLIC-posture session. tests/test_tv_bridge_switch.py pins both.
        'r = session.get(url, stream=True, timeout=timeout, allow_redirects=False)':
            "tv_free_channels switch (require_open + record_external per hop)",
    },
    "inference/code_model.py": {
        'response = requests.post(f"{url}/api/chat", json=payload,':
            "ollama loopback (coding model) — guarded by is_loopback(url), "
            "which refuses rather than falls back",
    },
    # The wizard's brain check. Ollama at config `ollama.url` — loopback on a
    # single-box install, a LAN address when the owner points it at another
    # machine. brain_health gets per-LINE entries rather than the whole-file
    # pass its ollama neighbours have, because one of its two calls is the ONLY
    # place in this tree that asks another daemon to fetch from the internet on
    # our behalf, and that fact must not be able to hide behind a filename.
    "brain_health.py": {
        'resp = requests.get(f"{base}/api/tags", timeout=timeout)':
            "Ollama /api/tags at config `ollama.url` (loopback, or the LAN "
            "address the owner configured) — an inventory read of what is "
            "already installed; it fetches nothing",
        'resp = requests.post(f"{base}/api/pull",':
            "Ollama /api/pull at config `ollama.url` (loopback, or the LAN "
            "address the owner configured). OUR socket cannot leave the house "
            "— but the reply is a progress stream for a download OLLAMA then "
            "makes from registry.ollama.ai, so the BOX does egress here even "
            "though this process does not. That download now has its own door "
            "— `model_pull`, named for registry.ollama.ai and not for the "
            "Ollama URL — and pull_model() asks "
            "egress_allowed('model_pull') before this POST. The line stays "
            "exempt because the socket it opens really is the LAN/loopback "
            "one; the gate is at the call site, not in egress_request",
    },
    # The wizard's Home Assistant credential probe. Same destination as
    # ha_bridge.py — the HA the owner typed at the /setup/ha step — but this
    # line carries a candidate long-lived token, and setup_ops is a large file
    # that does many other things, so it gets the line and not the file.
    # _check_lan_posture() has already refused a non-LAN `ha.url` before any
    # write; this probe is the same URL, one step earlier.
    "setup_ops.py": {
        'resp = requests.get(':
            "Home Assistant /api/config on the LAN (the URL the owner entered "
            "at the /setup/ha step, checked by security.lan_posture before the "
            "token is written) — validates the credential before saving it",
    },
    # The web-search STATUS probe. SearXNG on 127.0.0.1:8888: both probes run
    # only when _is_loopback_base(base) (which defers to the Toolsmith's own
    # predicate), so a configured non-loopback search box is reported from
    # config WITHOUT being dialled — answering "can you search the web?" must
    # never itself be egress. The opener is built with ProxyHandler({}) so an
    # http_proxy in the unit environment cannot turn the loopback probe into
    # an outbound request, and _NoRedirect stops a 3xx from becoming a new
    # destination. These four lines are the whole urllib surface in the file;
    # any other outbound call added here fails the test.
    "websearch_status.py": {
        'opener = urllib.request.build_opener(':
            "SearXNG loopback :8888 (proxy-free opener, gated on "
            "_is_loopback_base)",
        'urllib.request.ProxyHandler({}),':
            "SearXNG loopback :8888 — the proxy bypass itself",
        'request = urllib.request.Request(':
            "SearXNG loopback :8888 (GET /healthz and GET /config)",
        'class _NoRedirect(urllib.request.HTTPRedirectHandler):':
            "SearXNG loopback :8888 — the redirect refusal, not a call",
    },
    # `puffpai` CLI nudging the running backend to rescan apps. The URL is a
    # literal http://127.0.0.1:<api_server.port>, nothing an operator edits.
    "cli.py": {
        'request = urllib.request.Request(':
            "this box's own backend, http://127.0.0.1:<port>/apps/rescan",
        'with urllib.request.urlopen(request, timeout=2) as response:':
            "this box's own backend, the same fixed loopback request",
    },
    # Prove's live check talks to the owner's own backend. check_local_url()
    # refuses, before this line, any address whose host does not resolve to
    # loopback, private or link-local space, and _NoRedirects stops a 3xx
    # from turning into a new destination.
    # The build engine's calls to the apps it runs on this computer. Each line
    # below builds a request or a redirect refusal; the line that dials it is
    # marked ``# lan-egress:`` and checked to 127.0.0.1 first.
    "assemble/server/app.py": {
        'req = urllib.request.Request(url, data=data, method=method, headers=send)':
            "the assembled app's gateway on 127.0.0.1 (built here, sent by "
            "the marked opener below it)",
    },
    "build/api.py": {
        'class _Stay(urllib.request.HTTPRedirectHandler):':
            "the redirect refusal for the 127.0.0.1 frame check, not a call",
        'with opener.open(urllib.request.Request(url, method="GET"), timeout=3) as resp:':
            "an app on 127.0.0.1 (host checked just above), through the "
            "marked proxy-free opener",
    },
    "build/toolsmith_run.py": {
        'req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})':
            "an app on 127.0.0.1 (checked by the caller), sent by the marked "
            "opener below it",
    },
    "integrate/page_reader.py": {
        'req = urllib.request.Request(':
            "the headless browser's DevTools on 127.0.0.1, sent by the "
            "marked opener",
    },
    "integrate/run_here.py": {
        'class _NoRedirect(urllib.request.HTTPRedirectHandler):':
            "the redirect refusal for calls to an app on 127.0.0.1, not a call",
        'req = urllib.request.Request(url, data=data, method=method,':
            "an app on 127.0.0.1 (the URL is built on LOOPBACK), sent by the "
            "marked opener",
    },
    "prove_live.py": {
        'class _NoRedirects(urllib.request.HTTPRedirectHandler):':
            "the redirect refusal for the local backend probe, not a call",
        'request = urllib.request.Request(base + path, data=payload,':
            "the owner's backend, loopback or LAN only (check_local_url)",
        'opener = urllib.request.build_opener(_NoRedirects)  # lan-egress: '
        'the owner\'s own backend, loopback or LAN only, checked by '
        'check_local_url above':
            "the owner's backend, loopback or LAN only (check_local_url)",
    },
}

# ``_CALL_RE`` scans for websockets, so bridge_inventory's HA websocket is a
# real ``_NOT_EGRESS_LINES`` entry above rather than a note in prose.
# ``ha_events.py`` and ``zigbee_manager.py`` carry the same kind of destination
# under their whole-file entries in ``_NOT_EGRESS``.



# -- 8. the house-local wrapper ----------------------------------------------
#
# Doors govern the internet. The three AdGuard readers are the other half:
# calls that promise NEVER to reach it. A comment in ``_NOT_EGRESS`` cannot
# keep that promise when the destination comes from
# ``config zone3.adguard_url``, a string an operator edits. These tests pin the
# check that does.

class TestLanHttpRefusesAnythingOffTheHouse:

    def test_a_public_host_is_refused(self, monkeypatch):
        from puffpai.security import lan_http, netaddr
        monkeypatch.setattr(netaddr, "default_resolver",
                            lambda host, port: ["93.184.216.34"])
        with pytest.raises(netaddr.AddressRefused) as excinfo:
            lan_http.lan_get("http://example.com")
        # The refusal names the HOST, never the address it resolved to.
        assert "example.com" in str(excinfo.value)
        assert "93.184.216.34" not in str(excinfo.value)

    def test_a_public_host_is_refused_before_a_socket_opens(self, monkeypatch,
                                                            no_sockets):
        """``no_sockets`` makes every socket primitive fatal, so a pass here
        means the refusal happened without dialling anything."""
        from puffpai.security import lan_http, netaddr
        monkeypatch.setattr(netaddr, "default_resolver",
                            lambda host, port: ["93.184.216.34"])
        with pytest.raises(netaddr.AddressRefused):
            lan_http.lan_get("http://example.com")

    def test_one_public_answer_among_private_ones_is_refused(self,
                                                             monkeypatch):
        """The rebinding shape: a name whose address the CLIENT picks."""
        from puffpai.security import lan_http, netaddr
        monkeypatch.setattr(
            netaddr, "default_resolver",
            lambda host, port: ["192.168.1.140", "93.184.216.34"])
        with pytest.raises(netaddr.AddressRefused):
            lan_http.lan_get("http://adguard.lan/control/status")

    def test_a_link_local_metadata_address_is_refused(self, monkeypatch):
        from puffpai.security import lan_http, netaddr
        monkeypatch.setattr(netaddr, "default_resolver",
                            lambda host, port: ["169.254.169.254"])
        with pytest.raises(netaddr.AddressRefused):
            lan_http.lan_get("http://metadata.internal/")

    def test_a_name_that_will_not_resolve_is_refused(self, monkeypatch):
        """"We could not tell" is a refusal, never a pass."""
        from puffpai.security import lan_http, netaddr

        def no_answer(host, port):
            raise netaddr.ResolutionError("nope")

        monkeypatch.setattr(netaddr, "default_resolver", no_answer)
        with pytest.raises(netaddr.ResolutionError):
            lan_http.lan_get("http://adguard.lan/")

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd", "ftp://192.168.1.5/x", "not-a-url", "http://",
    ])
    def test_a_non_http_url_is_refused(self, url):
        from puffpai.security import lan_http
        with pytest.raises(ValueError):
            lan_http.lan_get(url)

    def test_loopback_is_allowed(self, monkeypatch):
        """The appliance's own AdGuard. Mocked at the transport, so nothing
        dials: what is asserted is that the ADDRESS CHECK let it through."""
        from puffpai.security import lan_http
        seen = []

        class _Resp:
            status_code = 200

        def fake_request(self, method, url, **kw):
            seen.append((method, url, kw))
            return _Resp()

        monkeypatch.setattr("requests.Session.request", fake_request)
        resp = lan_http.lan_get("http://127.0.0.1:3000/control/status")
        assert resp.status_code == 200
        assert seen[0][0] == "GET"
        assert seen[0][1] == "http://127.0.0.1:3000/control/status"
        # Redirects are OFF by default: a LAN service that answers 302 to a
        # public URL is exactly the escape this module exists to stop.
        assert seen[0][2]["allow_redirects"] is False

    def test_an_rfc1918_address_is_allowed(self, monkeypatch):
        from puffpai.security import lan_http
        monkeypatch.setattr("requests.Session.request",
                            lambda self, method, url, **kw: "ok")
        assert lan_http.lan_post("http://192.168.1.140:8123/api/x",
                                 json={}) == "ok"

    def test_lan_posture_alone_would_refuse_loopback(self):
        """HOUSE is deliberately a SEPARATE posture, not a loosened LAN.

        A door declared ``lan=True`` must still not be able to reach the
        appliance's own API server; only these house-local callers may.
        """
        from puffpai.security import netaddr
        with pytest.raises(netaddr.AddressRefused):
            netaddr.check("127.0.0.1", posture=netaddr.LAN)
        assert netaddr.check("127.0.0.1", posture=netaddr.HOUSE) is None
        for public in ("93.184.216.34", "169.254.169.254", "100.64.0.1"):
            with pytest.raises(netaddr.AddressRefused):
                netaddr.check(public, posture=netaddr.HOUSE)


class TestTheAdGuardReadersUseIt:
    """All three call sites, checked in the source rather than trusted."""

    @pytest.mark.parametrize("rel", [
        "bridge_ledger.py", "bridge_inventory.py",
        "zone3/adguard_controller.py",
    ])
    def test_the_reader_goes_through_lan_http(self, rel):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "lan_http" in text, f"{rel} no longer uses the LAN wrapper"
        assert not re.search(r"(?<![\w.])requests\s*\.\s*(?:get|post)\s*\(",
                             text), f"{rel} has a bare requests call again"


#: HTTP client constructors. A name, attribute or parameter that holds what one
#: returns is a client, and so is anything bound to the ``requests``,
#: ``httpx`` or ``aiohttp`` module object itself (``self._http = requests``).
_CLIENT_MODULES = frozenset({"requests", "httpx", "aiohttp"})
_CLIENT_FACTORIES = frozenset({
    "requests.Session", "requests.session", "requests.sessions.Session",
    "httpx.Client", "httpx.AsyncClient", "aiohttp.ClientSession"})
#: Matched on the last dotted part, so ``netaddr.pinned_session(...)`` and a
#: ``from requests import Session`` both count.
_CLIENT_FACTORY_NAMES = frozenset({"Session", "ClientSession", "AsyncClient",
                                   "pinned_session", "_pinned_session"})
_CLIENT_TYPES = frozenset({"Session", "ClientSession", "AsyncClient", "Client"})
_CLIENT_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head",
                           "options", "request", "send", "stream",
                           "ws_connect"})


def _client_call_offenders(source: str, exempt_lines=()) -> list:
    """``[(lineno, line)]`` for each HTTP call made through a client OBJECT.

    ``_CALL_RE`` reads one line at a time, so it sees ``requests.get(`` but not
    ``session.get(`` or ``self._http.request(``: the object's name says nothing
    about what it is. This pass follows the object instead. Within one module
    it marks every name, attribute and parameter that holds a client (built by
    a constructor above, handed back by a local function that returns one,
    passed as an argument to a local function, or bound to the client module
    itself), then reports each ``get/post/request/...`` call made on one.

    A binding on a line listed in *exempt_lines* is not followed, so a session
    whose creation is already an allowlisted LAN call covers the calls made on
    it. Calls straight on the module (``requests.get``) are left to
    ``_CALL_RE``.
    """
    import ast

    tree = ast.parse(source)
    lines = source.splitlines()
    exempt = {str(x).strip() for x in exempt_lines}
    aliases: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                aliases[a.asname or top] = a.name if a.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    parent: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    funcs: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(node)
    scope_kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

    def scope_of(node):
        while node in parent:
            node = parent[node]
            if isinstance(node, scope_kinds):
                return node
        return tree

    def dotted(node) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else ""
        return ""

    def callee(func) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    names: dict = {}      # scope -> names that hold a client
    attrs: set = set()    # attribute names that hold a client
    returners: set = set()  # local functions that hand one back

    def is_module(node) -> bool:
        return isinstance(node, ast.Name) and \
            aliases.get(node.id) in _CLIENT_MODULES

    def is_client(node, scope) -> bool:
        if isinstance(node, ast.Await):
            return is_client(node.value, scope)
        if isinstance(node, ast.BoolOp):
            return any(holds(v, scope) for v in node.values)
        if isinstance(node, ast.IfExp):
            return holds(node.body, scope) or holds(node.orelse, scope)
        if isinstance(node, ast.Call):
            d = dotted(node.func)
            if d in _CLIENT_FACTORIES or \
                    d.rsplit(".", 1)[-1] in _CLIENT_FACTORY_NAMES:
                return True
            if d == "getattr" and len(node.args) >= 2 and \
                    isinstance(node.args[1], ast.Constant) and \
                    node.args[1].value in attrs:
                return True
            return callee(node.func) in returners
        if isinstance(node, ast.Name):
            s = scope
            while True:
                if node.id in names.get(s, ()):
                    return True
                if s is tree:
                    return False
                s = scope_of(s)
        if isinstance(node, ast.Attribute):
            return node.attr in attrs
        return False

    def holds(node, scope) -> bool:
        return is_client(node, scope) or is_module(node)

    def bind(target, scope) -> bool:
        if isinstance(target, ast.Name):
            bucket = names.setdefault(scope, set())
            if target.id not in bucket:
                bucket.add(target.id)
                return True
        elif isinstance(target, ast.Attribute) and target.attr not in attrs:
            attrs.add(target.attr)
            return True
        return False

    def exempt_at(node) -> bool:
        lineno = getattr(node, "lineno", 0)
        return 0 < lineno <= len(lines) and lines[lineno - 1].strip() in exempt

    changed, passes = True, 0
    while changed and passes < 25:
        changed, passes = False, passes + 1
        for node in ast.walk(tree):
            scope = scope_of(node)
            if isinstance(node, ast.Assign) and not exempt_at(node) \
                    and holds(node.value, scope):
                for target in node.targets:
                    changed |= bind(target, scope)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) \
                    and node.value is not None and not exempt_at(node) \
                    and holds(node.value, scope):
                changed |= bind(node.target, scope)
            elif isinstance(node, (ast.With, ast.AsyncWith)) \
                    and not exempt_at(node):
                for item in node.items:
                    if item.optional_vars is not None and \
                            holds(item.context_expr, scope):
                        changed |= bind(item.optional_vars, scope)
            elif isinstance(node, ast.Return) and node.value is not None \
                    and isinstance(scope, (ast.FunctionDef,
                                           ast.AsyncFunctionDef)) \
                    and scope.name not in returners \
                    and is_client(node.value, scope):
                returners.add(scope.name)
                changed = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in (node.args.posonlyargs + node.args.args
                            + node.args.kwonlyargs):
                    ann = dotted(arg.annotation) if arg.annotation else ""
                    if ann.split(".")[0] in _CLIENT_MODULES and \
                            ann.rsplit(".", 1)[-1] in _CLIENT_TYPES:
                        changed |= bind(ast.Name(id=arg.arg), node)
            if isinstance(node, ast.Call):
                for fn in funcs.get(callee(node.func), ()):
                    params = fn.args.posonlyargs + fn.args.args
                    if isinstance(node.func, ast.Attribute) and params and \
                            params[0].arg in ("self", "cls"):
                        params = params[1:]
                    for i, value in enumerate(node.args):
                        if i < len(params) and holds(value, scope):
                            changed |= bind(ast.Name(id=params[i].arg), fn)
                    for kw in node.keywords:
                        if kw.arg and holds(kw.value, scope):
                            changed |= bind(ast.Name(id=kw.arg), fn)

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _CLIENT_VERBS \
                and not is_module(node.func.value) \
                and is_client(node.func.value, scope_of(node)):
            found.add((node.lineno, lines[node.lineno - 1].strip()))
    return sorted(found)


def _relative_posix(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def test_no_outbound_call_site_outside_the_registry():
    """A new ``requests.get`` or ``session.get`` anywhere in src/puffpai fails.

    Two passes over every file: ``_CALL_RE`` line by line, then
    ``_client_call_offenders`` for calls made through a client object. Fixing
    a hit means adding a door (or, if it truly cannot leave the house, an entry
    in ``_NOT_EGRESS`` that says where it goes). Both are a decision a person
    makes on purpose.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = _relative_posix(path)
        if rel in _NOT_EGRESS:
            continue
        exempt_lines = _NOT_EGRESS_LINES.get(rel, {})
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _CALL_RE.search(line) and line.strip() not in exempt_lines \
                    and _LAN_MARKER not in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")
        for lineno, line in _client_call_offenders(text, exempt_lines):
            if line not in exempt_lines and _LAN_MARKER not in line:
                offenders.append(f"{rel}:{lineno}: {line[:90]}")
    assert not offenders, (
        "outbound call sites outside the egress-door registry:\n  "
        + "\n  ".join(offenders)
        + "\n\nAdd a door in src/puffpai/security/egress_doors.py and route "
          "the call through egress_get(), or record it in _NOT_EGRESS with "
          "the LAN/loopback destination it actually reaches.")


def _registrable(host: str) -> str:
    labels = str(host).strip().lower().strip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _skill_manifest_hosts(path) -> set:
    """The hosts declared by the skill package *path* lives in, if any.

    Empty for every file that is not part of a skill package, so this can only
    ever excuse a hostname that sits beside a manifest naming it -- which is
    exactly the text the owner's consent card is built from.
    """
    manifest = path.parent / "manifest.json"
    if path.name != "skill.py" or not manifest.is_file():
        return set()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if str(data.get("schema") or "") != "puffpai_skill/v1":
        return set()
    hosts = (data.get("permissions") or {}).get("network_hosts") or []
    return {str(h).strip().lower() for h in hosts if isinstance(h, str)}


def test_every_wan_hostname_in_the_tree_belongs_to_a_door():
    """No hardcoded public hostname without a door that declares it."""
    # A real hostname has a dot. This also skips the credential-shaped
    # placeholder in logging_config's redaction docstring.
    url_re = re.compile(r"https://([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")
    known = {h for d in ed.DOORS for h in d.hosts}
    # The build engine's switches ("What leaves your computer", kept in
    # outside_switches.json) name the websites each one reaches, and
    # docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md lists them.
    from puffpai import outside_services
    known |= {w for r in outside_services.rows()
              for w in r.get("websites") or [] if "." in w and " " not in w}
    # Documentation URLs, SSRF-test literals and placeholder hosts: these are
    # strings in comments and test fixtures, not fetch targets.
    ignore_suffixes = (".example", "example.com", "example.org", "w3.org",
                       "wiki.openzim.org",
                       "registry.npmjs.org", "github.com",
                       "raw.githubusercontent.com")
    # Hosts that appear only as text a person reads, never as a fetch target.
    # EXACT host match (not a suffix), keyed to the one file allowed to say it,
    # so a later fetch of the same host from anywhere else still fails here.
    display_only = {
        # A license link on the source preview card; source_fetch fetches
        # only a row's source_url, never its license_url.
        ("source_registry.py", "en.wikipedia.org"),
        # "Install it from https://ollama.com": advice shown when Ollama is
        # absent. The model download itself goes through the model_pull door.
        ("wizard/packages/brain/skill.py", "ollama.com"),
        # The build engine's links, comments and attributions: a page a
        # person may open, a licence's own address, a project's home.
        ("assemble/server/app.py", "www.youtube-nocookie.com"),  # frame-src in the app page's own header: the owner's browser loads it
        ("build/classify.py", "clawic.com"),  # an example in a docstring
        ("catalogs/ha_integrations.py", "www.home-assistant.io"),  # the integration's docs link
        ("catalogs/kiwix_opds.py", "library.kiwix.org"),  # a book's page link
        ("catalogs/knowledge_resolver.py", "library.kiwix.org"),  # a book's page link
        ("catalogs/searchers/knowledge.py", "library.kiwix.org"),  # a book's page link
        ("catalogs/openstax.py", "creativecommons.org"),  # a licence address prefix
        ("catalogs/wikibooks.py", "creativecommons.org"),  # a licence address
        ("catalogs/wikipedia_topics.py", "creativecommons.org"),  # a licence address
        ("catalogs/where_to_look.py", "creativecommons.org"),  # a licence address
        ("catalogs/where_to_look.py", "www.gnu.org"),  # a comment about licence terms
        ("catalogs/where_to_look.py", "sindresorhus.com"),  # an example in a docstring
        ("catalogs/searchers/npm.py", "www.npmjs.com"),  # a package's page link
        ("finding/sites.py", "www.npmjs.com"),  # a package's page link
        ("catalogs/searchers/openverse.py", "openverse.org"),  # a picture's page link
        ("catalogs/snapshot_build_code.py", "data-v2.hacs.xyz"),  # provenance of a shipped snapshot, built by a maintainer from a local folder
        ("deps.py", "ffmpeg.org"),  # where a person gets the program themself
        ("deps.py", "nodejs.org"),  # where a person gets the program themself
        ("deps.py", "git-scm.com"),  # where a person gets the program themself
        ("deps.py", "jqlang.org"),  # where a person gets the program themself
        ("deps.py", "docs.docker.com"),  # where a person gets the program themself
        ("start_cli.py", "nodejs.org"),  # "Get it from https://nodejs.org"
        ("skill_learn.py", "agentskills.io"),  # the skill format's home, in a docstring
        # Fetched, but behind a switch HomeBot does not offer, so never: the
        # locked box (box_install, a Windows-only part) and a live-card seed
        # (live cards are not in this build; a card's host would be its own
        # app switch).
        ("box/driver.py", "dl-cdn.alpinelinux.org"),
        ("feeds.py", "images-api.nasa.gov"),
    }
    strays: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = _relative_posix(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # A SKILL package is the one place a hostname is
        # allowed to exist without a door in `ed.DOORS`: a skill's door is built
        # from its own manifest at install time and registered into the separate
        # skill-door table. The invariant still holds -- the host must be
        # DECLARED where the owner's consent card reads it from -- so it is
        # checked against the manifest beside the code rather than waived.
        declared = _skill_manifest_hosts(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            for host in url_re.findall(line):
                host = host.rstrip(".").lower()
                if host in known or host.endswith(ignore_suffixes):
                    continue
                if (rel, host) in display_only:
                    continue
                if any(host == h or host == _registrable(h) for h in declared):
                    continue
                strays.append(f"{rel}:{lineno}: {host}")
    # omaps.app is the Organic Maps phone link in a directions answer: the
    # household's PHONE opens it; the appliance never fetches it.
    strays = [s for s in strays
              if not s.startswith("maps_directions.py:")
              or not s.endswith(": omaps.app")]
    assert not strays, (
        "public hostnames with no egress door:\n  " + "\n  ".join(strays))


# ── 8. the converted call sites still name their door ────────────────────────

@pytest.mark.parametrize("relpath,door_id,marker", [
    ("assistant_tools.py", "geoip", "ipapi.co"),
    ("assistant_tools.py", "geocode", "nominatim.openstreetmap.org"),
    ("assistant_tools.py", "weather", "api.weather.gov"),
    ("dashboard_widgets.py", "weather", "api.open-meteo.com"),
    ("dashboard_widgets.py", "air_quality", "air-quality-api.open-meteo.com"),
    ("media_hub.py", "radio_browser", "stations/search"),
    ("spotify_control.py", "spotify", "api.spotify.com"),
    ("maps_refresh.py", "map_data_updates", "download.geofabrik.de"),
    ("maps_refresh.py", "map_data_updates", "download1.graphhopper.com"),
])
def test_call_site_is_wired_to_its_door(relpath, door_id, marker):
    text = (SRC / relpath).read_text(encoding="utf-8")
    assert marker in text, f"{relpath} no longer contains {marker}"
    assert f'"{door_id}"' in text or f"'{door_id}'" in text, (
        f"{relpath} reaches {marker} but never names the {door_id} door")


def test_shut_door_message_never_invites_a_fabricated_answer():
    """Rule 3: a closed door produces an honest sentence, not silence."""
    for d in ed.DOORS:
        if d.enforcement != "registry":
            continue
        assert d.settings_hint, f"door {d.id} has no user-facing sentence"
        assert "off" in d.settings_hint.lower(), d.id
