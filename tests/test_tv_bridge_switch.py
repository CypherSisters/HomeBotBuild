"""The free TV channels' Bridge switch, on every path that reaches them.

``tv_free_channels`` ships closed. These tests pin that:

* the kiosk player (``tv_player.play_free`` and ``POST /api/tv/native/play``)
  refuses while the switch is closed, starts no container, and answers the
  API with a 403 that names the switch;
* with the switch open, each player start is written to the egress ledger
  by host only;
* the cast relay records the hops it makes (allowed, refused, failed) and
  never writes a path, query or session token into the ledger; within one
  cast it coalesces repeats to one line per host and outcome per
  RELAY_LEDGER_QUIET_S, and no hop goes uncounted: a failed write, a
  forgotten key and a cast that ends all keep their count;
* ``allowed_upstream`` reads a path the way Jellyfin does, percent-decoded;
* the call-site scan in ``test_egress_door_registry`` sees calls made
  through a client object (``session.get``), not only ``requests.get``.

Docker, DNS and the network are never touched.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path

import pytest
from flask import Flask

from puffpai import tv_cast, tv_player
from puffpai.security import egress_doors, egress_ledger

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "puffpai"

PLUTO_URL = "https://jmp2.uk/plu-5ca671f215a62078d2ec0abf.m3u8"
M3U = f"""#EXTM3U
#EXTINF:-1 group-title="Comedy",Comedy Channel
{PLUTO_URL}
#EXTINF:-1 group-title="News",Plain Stream
https://example.com/live/master.m3u8?token=abc
"""
SESSION = "SESSIONTOKENsecretJWT"


class _Docker:
    def __init__(self):
        self.calls = []
        self.running = False

    def __call__(self, args, timeout=20):
        self.calls.append(list(args))
        out = ""
        if args[0] == "run":
            self.running = True
        elif args[0] in ("stop", "rm"):
            self.running = False
        elif args[0] == "ps":
            out = "abc123\n" if self.running else ""
        elif args[0] == "logs":
            out = "tvwindow: first frame after 7.0s\n"
        return subprocess.CompletedProcess(args, 0, out, "")


def _switch(monkeypatch, on: bool) -> None:
    if on:
        monkeypatch.setenv("PUFFPAI_EGRESS_TV_FREE_CHANNELS", "1")
    else:
        monkeypatch.delenv("PUFFPAI_EGRESS_TV_FREE_CHANNELS", raising=False)
    egress_doors.reset_cache()


def _ledger(kind: str | None = None) -> list:
    rows = [r for r in egress_ledger.read_records(limit=0)
            if r.get("door") == "tv_free_channels"]
    return [r for r in rows if kind is None or r.get("kind") == kind]


@pytest.fixture
def ledger(tmp_path):
    egress_ledger.set_path_override(str(tmp_path / "egress_ledger.jsonl"))
    yield
    egress_ledger.set_path_override(None)


@pytest.fixture
def player(tmp_path, monkeypatch, ledger):
    m3u = tmp_path / "iptv-unreliable.m3u"
    m3u.write_text(M3U, encoding="utf-8")
    monkeypatch.setattr(tv_player, "_FREE_ADS_M3U", str(m3u))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "gst-registry.bin").write_bytes(b"x")
    monkeypatch.setattr(tv_player, "_CACHE_DIR", str(cache))
    monkeypatch.setattr(tv_player.time, "sleep", lambda s: None)
    docker = _Docker()
    monkeypatch.setattr(tv_player, "_docker", docker)
    monkeypatch.setattr(tv_player, "jellyfin_channels", lambda: [
        {"id": "a" * 32, "number": "1", "name": "Movie Night"}])
    import puffpai.radio_player as rp
    monkeypatch.setattr(rp, "stop", lambda: True)
    monkeypatch.setattr(rp, "_resolve", lambda host: ["93.184.216.34"])
    _switch(monkeypatch, False)
    return docker


@pytest.fixture
def no_sockets(monkeypatch):
    """Every socket primitive raises, so a refusal means nothing was dialled."""
    import socket

    def boom(*_a, **_kw):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)


def _ran(docker) -> bool:
    return any(c[0] == "run" for c in docker.calls)


# ── the kiosk player ─────────────────────────────────────────────────────────

def test_play_free_refuses_while_the_switch_is_closed(player):
    for ch in tv_player.free_ad_channels():
        ok, why = tv_player.play_free(ch["id"])
        assert not ok
        assert why == egress_doors.door("tv_free_channels").settings_hint
    assert not _ran(player)
    refused = [r for r in _ledger() if r["result"] == "refused"]
    assert refused and not _ledger("player_start")


def test_refusal_names_the_switch_as_settings_shows_it(player):
    why = tv_player.play_free(tv_player.free_ad_channels()[0]["id"])[1]
    assert "'Free TV channels'" in why and "Bridge" in why
    assert "door" not in why.lower()
    card = (REPO / "dashboard/app/dashboard/settings/EgressDoorsCard.tsx"
            ).read_text(encoding="utf-8")
    assert 'tv_free_channels: "Free TV channels"' in card


def test_closed_switch_is_refused_before_the_id_is_looked_at(player):
    ok, why = tv_player.play_free("not-an-id")
    assert not ok and why == tv_player.free_channels_refusal()
    assert not _ran(player)


def test_an_unreadable_switch_refuses(player, monkeypatch):
    def broken(_door):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(egress_doors, "require_open", broken)
    assert tv_player.free_channels_refusal() == tv_player.FREE_CHANNELS_OFF
    assert not tv_player.play_free(tv_player.free_ad_channels()[0]["id"])[0]
    assert not _ran(player)


def test_home_channels_do_not_need_the_switch(player):
    ok, why = tv_player.play_jellyfin("a" * 32)
    assert ok, why


@pytest.fixture
def client(player):
    app = Flask(__name__)
    tv_player.register(app)
    return app.test_client()


def test_native_play_route_answers_403_naming_the_switch(client, player,
                                                       monkeypatch):
    monkeypatch.setattr(tv_player, "supported", lambda: True)
    fid = tv_player.free_ad_channels()[0]["id"]
    r = client.post("/api/tv/native/play", json={"free_id": fid})
    assert r.status_code == 403
    body = r.get_json()
    assert body == {"success": False, "reason": "bridge_closed",
                    "switches": ["tv_free_channels"],
                    "error": egress_doors.door("tv_free_channels").settings_hint}
    assert not _ran(player)


def test_closed_switch_is_named_before_the_hardware_check(client, player,
                                                         monkeypatch):
    monkeypatch.setattr(tv_player, "supported", lambda: False)
    fid = tv_player.free_ad_channels()[0]["id"]
    r = client.post("/api/tv/native/play", json={"free_id": fid})
    assert r.status_code == 403 and r.get_json()["reason"] == "bridge_closed"
    assert not _ran(player)


def test_open_switch_off_a_jetson_still_answers_503(client, player,
                                                    monkeypatch):
    monkeypatch.setattr(tv_player, "supported", lambda: False)
    _switch(monkeypatch, True)
    fid = tv_player.free_ad_channels()[0]["id"]
    r = client.post("/api/tv/native/play", json={"free_id": fid})
    assert r.status_code == 503 and r.get_json()["supported"] is False
    assert not _ran(player)


def test_native_play_route_plays_once_the_switch_opens(client, player,
                                                       monkeypatch):
    monkeypatch.setattr(tv_player, "supported", lambda: True)
    _switch(monkeypatch, True)
    fid = tv_player.free_ad_channels()[0]["id"]
    r = client.post("/api/tv/native/play", json={"free_id": fid})
    assert r.status_code == 200 and r.get_json()["success"] is True
    assert _ran(player)


def test_open_switch_records_each_start_by_host_only(player, monkeypatch):
    _switch(monkeypatch, True)
    pluto, plain = tv_player.free_ad_channels()
    assert tv_player.play_free(pluto["id"])[0]
    assert tv_player.play_free(plain["id"])[0]
    starts = _ledger("player_start")
    assert [r["host"] for r in starts] == ["boot.pluto.tv", "example.com"]
    assert all(r["result"] == "allowed" and r["path"] == "" for r in starts)
    text = json.dumps(starts)
    assert "token" not in text and "?" not in text and "jmp2.uk" not in text


def test_the_player_asks_the_switch_in_its_source():
    text = (SRC / "tv_player.py").read_text(encoding="utf-8")
    assert 'require_open("tv_free_channels")' in text
    play = text.split("def play_free(", 1)[1].split("\ndef ", 1)[0]
    assert play.index("free_channels_refusal()") < play.index("_start(")


# ── the cast relay ───────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, answers):
        self.answers = list(answers)
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def relay(monkeypatch, ledger):
    from puffpai.security import netaddr
    monkeypatch.setattr(netaddr, "validated_addresses",
                        lambda host, port, posture=None, resolver=None:
                        ["93.184.216.34"])
    _switch(monkeypatch, True)


def test_relay_records_every_hop_by_host(relay):
    first = (f"https://stitcher.pluto.tv/v2/stitch/hls/channel/abc/"
             f"master.m3u8?jwt={SESSION}")
    session = _Session([
        _Resp(302, {"Location": "https://cdn.example.org/v.m3u8?sig=1"}),
        _Resp(200, {"Content-Length": "512"}),
    ])
    r, final = tv_cast._relay_get(session, first)
    assert r.status_code == 200 and final.startswith("https://cdn.example.org/")
    rows = _ledger()
    assert [(x["host"], x["status"]) for x in rows] == [
        ("stitcher.pluto.tv", 302), ("cdn.example.org", 200)]
    assert rows[1]["bytes_in"] == 512
    text = json.dumps(rows)
    assert SESSION not in text and "jwt" not in text and "sig=" not in text
    assert all(x["path"] == "" for x in rows)


def test_relay_records_a_failed_fetch(relay):
    session = _Session([OSError("boom https://cdn.example.org/x?jwt=1")])
    with pytest.raises(tv_cast.CastError) as err:
        tv_cast._relay_get(session, "https://cdn.example.org/x.ts?jwt=1")
    assert err.value.code == "upstream_down"
    (row,) = _ledger()
    assert row["kind"] == "request_failed" and row["host"] == "cdn.example.org"
    assert "jwt" not in json.dumps(row)


def test_relay_records_an_indoor_address_as_refused(relay, monkeypatch):
    from puffpai.security import netaddr

    def indoors(host, port, posture=None, resolver=None):
        raise netaddr.AddressRefused("private", "is a private address", host)
    monkeypatch.setattr(netaddr, "validated_addresses", indoors)
    session = _Session([])
    with pytest.raises(tv_cast.CastError):
        tv_cast._relay_get(session, "https://lan.example.org/x.ts")
    assert session.urls == []
    (row,) = _ledger()
    assert row["result"] == "refused" and row["kind"] == "address_check"
    assert row["address_class"] == "private"


def test_relay_refuses_with_the_switch_closed_before_any_fetch(relay,
                                                               monkeypatch):
    _switch(monkeypatch, False)
    session = _Session([])
    with pytest.raises(tv_cast.CastError) as err:
        tv_cast._relay_get(session, "https://cdn.example.org/x.ts")
    assert err.value.code == "door_closed"
    assert err.value.message == egress_doors.door("tv_free_channels").settings_hint
    assert session.urls == []


def test_the_registry_wrapper_refuses_this_switch_even_when_open(
        monkeypatch, ledger, no_sockets):
    """The video hosts come from playlists, so the wrapper cannot check them.

    It refuses the switch outright, before any socket, rather than treat the
    one fixed host as covering the rest. The relay above is the only way out.
    """
    _switch(monkeypatch, True)
    with pytest.raises(egress_doors.EgressBlocked) as err:
        egress_doors.egress_get("tv_free_channels", "https://boot.pluto.tv/v4/start")
    assert "dynamic hosts" in err.value.reason
    (row,) = _ledger()
    assert row["result"] == "refused"


@pytest.mark.parametrize("url", [
    "https://cdn%2eexample.org/x.ts",
    "https://cdn.example.org%2f@evil.example/x.ts",
])
def test_relay_refuses_an_encoded_host(url):
    assert not tv_cast.relay_url_ok(url)


# ── the relay's ledger lines are coalesced per cast ──────────────────────────

SEG = "https://cdn.example.org/seg/{}.ts?sig=abc"


class _Streamed(_Resp):
    def __init__(self, body=b""):
        super().__init__(200, {"Content-Length": "188"})
        self.body = body

    def iter_content(self, _size):
        yield self.body


class _Always:
    """A session that answers 200 to everything (a long-running stream)."""

    def __init__(self, body=b""):
        self.urls = []
        self.body = body

    def get(self, url, **kw):
        self.urls.append(url)
        return _Streamed(self.body)


@pytest.fixture
def clock(monkeypatch):
    """The coalescing clock, moved by hand, and a fresh, empty key map."""
    now = [1000.0]
    monkeypatch.setattr(tv_cast, "_relay_clock", lambda: now[0])
    monkeypatch.setattr(tv_cast, "_relay_ledger_seen", OrderedDict())
    return now


def test_first_hop_to_a_host_is_recorded(relay, clock):
    tv_cast._relay_get(_Always(), SEG.format(1), scope=tv_cast.new_relay_scope())
    (row,) = _ledger()
    assert row["host"] == "cdn.example.org" and row["result"] == "allowed"
    assert row["kind"] == "request" and row["status"] == 200
    assert "more" not in row["reason"]


def test_repeat_hops_inside_the_window_are_coalesced(relay, clock):
    scope = tv_cast.new_relay_scope()
    session = _Always()
    for i in range(200):          # 200 segments, 2 s apart: under 7 minutes
        tv_cast._relay_get(session, SEG.format(i), scope=scope)
        clock[0] += 2.0
    assert len(session.urls) == 200            # every fetch still happened
    assert len(_ledger()) == 1


def test_a_new_host_or_outcome_or_cast_is_recorded(relay, clock):
    scope = tv_cast.new_relay_scope()
    session = _Always()
    tv_cast._relay_get(session, SEG.format(1), scope=scope)
    tv_cast._relay_get(session, "https://ads.example.net/ad/1.ts", scope=scope)
    with pytest.raises(tv_cast.CastError):
        tv_cast._relay_get(_Session([OSError("x")]), SEG.format(2), scope=scope)
    tv_cast._relay_get(session, SEG.format(3), scope=tv_cast.new_relay_scope())
    assert [(r["host"], r["kind"]) for r in _ledger()] == [
        ("cdn.example.org", "request"), ("ads.example.net", "request"),
        ("cdn.example.org", "request_failed"), ("cdn.example.org", "request")]


def test_a_line_after_the_window_carries_the_count(relay, clock):
    scope = tv_cast.new_relay_scope()
    session = _Always()
    tv_cast._relay_get(session, SEG.format(0), scope=scope)
    for i in range(1, 6):
        clock[0] += 10.0
        tv_cast._relay_get(session, SEG.format(i), scope=scope)
    assert len(_ledger()) == 1
    # The window runs from the key's last LINE, so a steady stream gets one
    # line per window.
    clock[0] = 1000.0 + tv_cast.RELAY_LEDGER_QUIET_S
    tv_cast._relay_get(session, SEG.format(6), scope=scope)
    rows = _ledger()
    assert len(rows) == 2
    assert rows[1]["reason"] == "GET cdn.example.org, plus 5 more since the last line"
    clock[0] += tv_cast.RELAY_LEDGER_QUIET_S - 1
    tv_cast._relay_get(session, SEG.format(7), scope=scope)
    assert len(_ledger()) == 2


def test_hops_with_no_scope_are_never_coalesced(relay, clock):
    """The Pluto session call has no cast scope yet; it is written every time."""
    session = _Always()
    for _ in range(3):
        tv_cast._relay_get(session, "https://boot.pluto.tv/v4/start?x=1")
    assert [r["host"] for r in _ledger()] == ["boot.pluto.tv"] * 3
    assert len(tv_cast._relay_ledger_seen) == 0


def test_the_key_map_is_capped_and_a_forgotten_key_is_written_again(
        relay, clock, monkeypatch):
    monkeypatch.setattr(tv_cast, "RELAY_LEDGER_KEYS_MAX", 8)
    scope = tv_cast.new_relay_scope()
    session = _Always()
    for i in range(50):
        tv_cast._relay_get(session, f"https://h{i}.example.org/s.ts", scope=scope)
    assert len(tv_cast._relay_ledger_seen) == 8
    assert len(_ledger()) == 50
    tv_cast._relay_get(session, "https://h0.example.org/s.ts", scope=scope)
    assert len(_ledger()) == 51
    assert len(tv_cast._relay_ledger_seen) == 8


def test_an_evicted_key_writes_its_open_count(relay, clock, monkeypatch):
    monkeypatch.setattr(tv_cast, "RELAY_LEDGER_KEYS_MAX", 2)
    scope = tv_cast.new_relay_scope()
    session = _Always()
    for i in range(4):                         # 1 line, 3 counted
        tv_cast._relay_get(session, SEG.format(i), scope=scope)
    tv_cast._relay_get(session, "https://a.example.net/1.ts", scope=scope)
    tv_cast._relay_get(session, "https://b.example.net/1.ts", scope=scope)
    rows = [r for r in _ledger() if r["host"] == "cdn.example.org"]
    assert [r["reason"] for r in rows] == [
        "GET cdn.example.org", "GET cdn.example.org: 3 more since the last line"]
    assert all(r["path"] == "" for r in rows)


def test_a_cast_that_ends_writes_its_open_counts(relay, clock):
    scope = tv_cast.new_relay_scope()
    session = _Always()
    for i in range(5):
        tv_cast._relay_get(session, SEG.format(i), scope=scope)
    tv_cast.flush_relay_ledger(scope)
    assert [r["reason"] for r in _ledger()] == [
        "GET cdn.example.org", "GET cdn.example.org: 4 more since the last line"]
    assert not any(k[0] == scope for k in tv_cast._relay_ledger_seen)


def test_a_write_that_raises_does_not_mark_the_key_seen(relay, clock,
                                                       monkeypatch):
    scope = tv_cast.new_relay_scope()
    session = _Always()
    real = egress_doors.record_external
    calls = []

    def flaky(*a, **kw):
        calls.append(kw.get("detail"))
        if len(calls) == 1:
            raise OSError("ledger disk full")
        return real(*a, **kw)
    monkeypatch.setattr(egress_doors, "record_external", flaky)
    tv_cast._relay_get(session, SEG.format(0), scope=scope)   # write raises
    assert _ledger() == []
    clock[0] += 2.0
    tv_cast._relay_get(session, SEG.format(1), scope=scope)   # due at once
    (row,) = _ledger()
    assert row["reason"] == "GET cdn.example.org, plus 1 more since the last line"
    clock[0] += 2.0
    tv_cast._relay_get(session, SEG.format(2), scope=scope)   # now coalesced
    assert len(_ledger()) == 1


def test_racing_first_hops_write_exactly_one_line(relay, clock):
    scope = tv_cast.new_relay_scope()
    start = threading.Barrier(16)

    def hop():
        start.wait()
        tv_cast._relay_record(True, "cdn.example.org", "GET cdn.example.org",
                              scope=scope)
    threads = [threading.Thread(target=hop) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_ledger()) == 1
    tv_cast.flush_relay_ledger(scope)
    assert _ledger()[-1]["reason"] == \
        "GET cdn.example.org: 15 more since the last line"


def test_a_running_cast_coalesces_through_fetch(relay, clock):
    """End to end through CastManager.fetch: the cast's own scope is used."""
    mgr = tv_cast.CastManager(registry=object(), clock=lambda: 0.0)
    cast = tv_cast.Cast("relay", "media_player.tv", "TV", "192.168.1.20",
                        "p" * 12, "Free channel")
    cast.cid = "c" * 43
    cast.upstream = "https://stitcher.example.org/master.m3u8?jwt=" + SESSION
    cast.relay_scope = tv_cast.new_relay_scope()
    playlist = (b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                b"#EXTINF:6.0,\nhttps://cdn.example.org/seg/1.ts\n")
    cast._http = _Always(playlist)
    mgr._by_cid[tv_cast._cid_key(cast.cid)] = cast
    for _ in range(5):
        status, _ctype, body = mgr.fetch(cast, None, None)
        assert status == 200 and b"/cast/" in body
        served = [ln for ln in body.decode().splitlines()
                  if ln.startswith("/cast/")]
        key, ext = served[0].rsplit("/", 1)[1].split(".")
        status, _ctype, seg = mgr.fetch(cast, key, ext)
        assert status == 200
        b"".join(seg)
        clock[0] += 6.0
    rows = _ledger()
    assert [r["host"] for r in rows] == ["stitcher.example.org",
                                         "cdn.example.org"]
    assert SESSION not in json.dumps(rows)
    assert all(r["path"] == "" for r in rows)


def test_the_relay_passes_its_cast_scope_in_the_source():
    text = (SRC / "tv_cast.py").read_text(encoding="utf-8")
    fetch = text.split("    def fetch(", 1)[1].split("\n    def ", 1)[0]
    assert "scope=cast.relay_scope" in fetch
    attach = text.split("    def _attach_relay(", 1)[1].split("\n    def ", 1)[0]
    assert "cast.relay_scope = new_relay_scope()" in attach
    revoke = text.split("    def revoke(", 1)[1].split("\n    def ", 1)[0]
    assert "flush_relay_ledger(cast.relay_scope)" in revoke


def test_the_window_the_bridge_page_states_matches_the_constant():
    minutes = int(tv_cast.RELAY_LEDGER_QUIET_S // 60)
    assert minutes == 10
    assert f"at most once every {minutes} minutes" in \
        egress_doors.door("tv_free_channels").notes


# ── allowed_upstream reads the path as Jellyfin does ─────────────────────────

BASE = "http://192.168.1.41:8096"
CH = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("path", [
    f"/videos/{CH}/%2e%2e/%2e%2e/System/Info",
    f"/videos/{CH}/%2E%2E/%2E%2E/Users",
    f"/videos/{CH}%2f..%2fSystem/Info",
    f"/videos/{CH}/..%5c..%5cSystem",
    f"/videos/{CH}/%252e%252e/System",
    f"/videos/{CH}/a%00b.ts",
    f"/videos/{CH}/%ff.ts",
    f"/videos/{CH}/%2f/x.ts",
])
def test_allowed_upstream_refuses_encoded_escapes(path):
    assert tv_cast.allowed_upstream(BASE + path, BASE, CH) is None


def test_allowed_upstream_refuses_an_encoded_host():
    assert tv_cast.allowed_upstream(
        f"http://192.168.1.41%3a8096/videos/{CH}/x.ts", BASE, CH) is None


def test_allowed_upstream_still_allows_the_channel():
    got = tv_cast.allowed_upstream(
        f"{BASE}/videos/{CH}/hls1/main/0.ts?api_key=k&x=1", BASE, CH)
    assert got == f"/videos/{CH}/hls1/main/0.ts?x=1"
    assert tv_cast.allowed_upstream(
        f"{BASE}/videos/{CH}/seg%20one.ts", BASE, CH) == \
        f"/videos/{CH}/seg%20one.ts"


# ── the call-site scan sees client objects ───────────────────────────────────

def _registry_scan():
    spec = importlib.util.spec_from_file_location(
        "egress_registry_scan_for_tv", REPO / "tests" / "test_egress_door_registry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scan():
    return _registry_scan()


@pytest.mark.parametrize("source", [
    "import requests\ns = requests.Session()\ns.get('https://x.example')\n",
    "import requests\nwith requests.Session() as s:\n    s.post('https://x.example')\n",
    "import httpx\nc = httpx.Client()\nc.request('GET', 'https://x.example')\n",
    "import aiohttp\nasync def f():\n"
    "    async with aiohttp.ClientSession() as s:\n"
    "        await s.get('https://x.example')\n",
    "from requests import Session\nclass A:\n    def __init__(self):\n"
    "        self._s = Session()\n    def go(self):\n"
    "        return self._s.get('https://x.example')\n",
    "import requests\nclass A:\n    def __init__(self, http=None):\n"
    "        self._http = http or requests\n    def go(self):\n"
    "        return self._http.get('https://x.example')\n",
    "from puffpai.security import netaddr\n"
    "def fetch(session, url):\n    return session.get(url)\n"
    "def main():\n    fetch(netaddr.pinned_session(), 'https://x.example')\n",
    "import requests\ndef make():\n    return requests.Session()\n"
    "def main():\n    make().get('https://x.example')\n",
])
def test_scan_catches_calls_through_a_client_object(scan, source):
    assert scan._client_call_offenders(source), source


@pytest.mark.parametrize("source", [
    "from flask import session\ndef f():\n    return session.get('user')\n",
    "def f(session: dict):\n    return session.get('k')\n",
    "_sessions = {}\ndef f(k):\n    return _sessions.get(k)\n",
    "import requests\nrequests.get('https://x.example')\n",
])
def test_scan_leaves_non_clients_and_module_calls_alone(scan, source):
    assert scan._client_call_offenders(source) == []


def test_scan_follows_an_exempt_session_no_further(scan):
    source = ("import requests\ndef f():\n"
              "    session = requests.Session()\n"
              "    return session.get('http://ha.lan/api')\n")
    assert scan._client_call_offenders(source)
    assert scan._client_call_offenders(
        source, {"session = requests.Session()"}) == []


def test_scan_sees_the_relay_fetch_and_its_allowlist_entry_is_needed(scan):
    text = (SRC / "tv_cast.py").read_text(encoding="utf-8")
    lines = {line for _n, line in scan._client_call_offenders(text)}
    relay = ('r = session.get(url, stream=True, timeout=timeout, '
             'allow_redirects=False)')
    assert relay in lines
    assert relay in scan._NOT_EGRESS_LINES["tv_cast.py"]
    assert "tv_cast.py" not in scan._NOT_EGRESS
    relay_src = text.split("def _relay_get(", 1)[1].split("\ndef ", 1)[0]
    assert 'require_open("tv_free_channels")' in relay_src
    assert relay_src.index('require_open("tv_free_channels")') < \
        relay_src.index("session.get(")
    assert "_relay_record(True" in relay_src


# ── the kiosk player's own address check ─────────────────────────────────────

def test_play_free_refuses_a_plain_http_channel(player, monkeypatch, tmp_path):
    m3u = tmp_path / "plain.m3u"
    m3u.write_text("#EXTM3U\n#EXTINF:-1 group-title=\"News\",Plain\n"
                   "http://example.com/live/master.m3u8\n", encoding="utf-8")
    monkeypatch.setattr(tv_player, "_FREE_ADS_M3U", str(m3u))
    _switch(monkeypatch, True)
    ok, why = tv_player.play_free(tv_player.free_ad_channels()[0]["id"])
    assert not ok and "https" in why
    assert not _ran(player)
    assert not _ledger("player_start")


def test_tvplay_url_mode_accepts_https_only():
    text = (REPO / "docker/tvplayer/tvplay.sh").read_text(encoding="utf-8")
    url_mode = text.split("\n  url)\n", 1)[1].split(";;\n", 1)[0]
    assert 'case "$ARG" in https://*) ;;' in url_mode
    assert "http://*" not in url_mode
