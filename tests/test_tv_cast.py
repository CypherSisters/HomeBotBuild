"""TV casting ("Cast to..."): Home Assistant as the TV registry + the cast proxy.

Pins the cast security rules (HA is the device registry, link once, no PINs):
  1. cast ids are token_urlsafe(32), one channel + the TV's IP, revoked on
     Stop / unfetched / idle / 4 h;
  2. the proxy only reaches this channel on the box's own Jellyfin and serves
     0 matches for api_key|ApiKey|token|8096; nothing sent to HA carries one;
  3. only /cast/ is public; control routes need the key; the cast list is only
     HA apple_tv/roku media_players with a resolvable house-LAN IP;
  6. /cast/ access lines and urllib3 URL debug lines are dropped.
Plus "More free channels": off by default, a user switch, Apple TV only.
"""

import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

import pytest
from flask import Flask

from puffpai import tv_cast, tv_cast_routes

BASE = "http://192.0.2.10:8096"
CH = "7d1fca4b78434084820f674ae381f005"
CH_DASH = tv_cast.dashed_guid(CH)
APPLE = "media_player.living_room_tv"
APPLE_IP = "192.0.2.21"
ROKU = "media_player.bedroom_tv"
ROKU_IP = "192.0.2.22"
PLUTO = "https://jmp2.uk/plu-5ca525b650be2571e3943c63.m3u8"

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=8000000,CODECS=\"avc1.64001f,mp4a.40.2\"\n"
    f"main.m3u8?DeviceId=x&PlaySessionId=p&ApiKey=SECRETKEY&api_key=SECRETKEY\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=1\n"
    "http://evil.example/steal.m3u8\n"
)
VARIANT = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:3\n"
    "#EXT-X-MAP:URI=\"hls1/main/init.mp4?ApiKey=SECRETKEY\"\n"
    "#EXT-X-KEY:METHOD=AES-128,URI=\"http://evil.example/key\"\n"
    "#EXTINF:3.0,\n"
    "hls1/main/0.ts?ApiKey=SECRETKEY&runtimeTicks=0\n"
    "#EXTINF:3.0,\n"
    f"{BASE}/videos/{CH_DASH}/hls1/main/1.ts?api_key=SECRETKEY\n"
    "#EXTINF:3.0,\n"
    f"{BASE}/videos/{CH_DASH}/../../Users/x.ts\n"
    "#EXTINF:3.0,\n"
    f"{BASE}/Videos/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/hls1/main/2.ts\n"
)


class FakeResp:
    def __init__(self, status, body=b""):
        self.status_code = status
        self._body = body
        self.content = body

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def json(self):
        return json.loads(self._body)

    def close(self):
        pass


class FakeJF:
    def __init__(self):
        self.base = BASE
        self.calls = []
        self.closed = []

    def request(self, method, pq, **kw):
        self.calls.append((method, pq))
        path = pq.split("?")[0]
        if path.endswith("master.m3u8"):
            return FakeResp(200, MASTER.encode())
        if path.endswith("main.m3u8"):
            return FakeResp(200, VARIANT.encode())
        if path.endswith(".ts") or path.endswith(".mp4"):
            return FakeResp(200, b"\x47" * 1000)
        return FakeResp(404)

    def channels(self):
        return [{"id": CH, "name": "Horror", "number": "4"}]

    def open_stream(self, channel_id):
        return {"master": f"/videos/{CH_DASH}/master.m3u8?DeviceId=x&LiveStreamId=l",
                "live_stream_id": "l", "play_session_id": "p"}

    def close_stream(self, info):
        self.closed.append(info)


# ── a fake Home Assistant: registries, states, service calls ──

DEVICES = [
    {"id": "d-atv", "name": "Living room TV", "name_by_user": None, "model": "Apple TV 4K",
     "connections": [["mac", "02:00:00:AB:00:01"]]},
    {"id": "d-roku", "name": "Bedroom TV", "name_by_user": None, "model": "4800X",
     "connections": [["mac", "02:00:00:00:00:02"]]},
    {"id": "d-ma", "name": "Office", "model": "AirPlay", "connections": []},
    {"id": "d-far", "name": "Far", "model": "Apple TV", "connections": [["mac", "aa:aa:aa:aa:aa:aa"]]},
]
ENTITIES = [
    {"entity_id": APPLE, "platform": "apple_tv", "device_id": "d-atv"},
    {"entity_id": "remote.living_room_tv", "platform": "apple_tv", "device_id": "d-atv"},
    {"entity_id": ROKU, "platform": "roku", "device_id": "d-roku"},
    # Music Assistant's AirPlay AUDIO player with the same name as an Apple TV
    {"entity_id": "media_player.office_speaker", "platform": "music_assistant", "device_id": "d-ma"},
    {"entity_id": "media_player.old_atv", "platform": "apple_tv", "device_id": "d-atv",
     "disabled_by": "user"},
    {"entity_id": "media_player.far", "platform": "apple_tv", "device_id": "d-far"},
]
AVAHI = [
    {"name": "Office", "ip": APPLE_IP, "deviceid": "02:00:00:ab:00:01", "model": "AppleTV11,1"},
    {"name": "Guest room TV", "ip": "192.0.2.23", "deviceid": "02:00:00:00:00:03", "model": "AppleTV5,3"},
    {"name": "Bedroom TV.", "ip": ROKU_IP, "deviceid": "", "model": "4800X"},
]
NEIGH = {"02:00:00:00:00:02": ROKU_IP, "aa:aa:aa:aa:aa:aa": "198.51.100.9"}


class FakeHA:
    def __init__(self):
        self.calls = []
        self.states = {APPLE: "off", ROKU: "idle", "media_player.office_speaker": "idle",
                       "media_player.far": "on"}
        self.on_play = None
        self.fail = None

    def get_all_states(self):
        return [{"entity_id": e, "state": s, "attributes": {}} for e, s in self.states.items()]

    def get_entity_state(self, eid):
        return {"entity_id": eid, "state": self.states.get(eid, "unknown")}

    def call_service(self, domain, service, entity_id=None, **data):
        self.calls.append((domain, service, entity_id, data))
        if self.fail:
            raise self.fail
        if service == "play_media" and self.on_play:
            self.on_play(entity_id, data)
        return []


def lan(ip):
    return ip.startswith("192.0.2.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_TV_CAST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PUFFPAI_EGRESS_TV_FREE_CHANNELS", raising=False)
    monkeypatch.setattr(tv_cast, "local_ip_for", lambda ip: "192.0.2.10")
    monkeypatch.setattr(tv_cast, "CAST_START_WAIT_S", 3)
    monkeypatch.setattr(tv_cast, "HA_PLAY_TIMEOUT_S", 1)
    jf = FakeJF()
    ha = FakeHA()
    reg = tv_cast.TvRegistry(ha=ha, registries=lambda: (DEVICES, ENTITIES),
                             avahi=lambda: AVAHI, neigh=lambda: NEIGH, lan_check=lan)
    mgr = tv_cast.CastManager(jellyfin_factory=lambda: jf, registry=reg)
    mgr._ensure_reaper = lambda: None
    monkeypatch.setattr(tv_cast, "_manager", mgr)
    app = Flask(__name__)
    tv_cast_routes.register(app)
    client = app.test_client()

    def tv_fetches(entity_id, data):
        """What the TV does after HA's play_media: fetch the URL it was given."""
        url = data["media_content_id"]
        if url.startswith("http://192.0.2.10:8000/cast/"):
            ip = APPLE_IP if entity_id == APPLE else ROKU_IP
            r = client.get(urlsplit(url).path, environ_base={"REMOTE_ADDR": ip})
            assert r.status_code == 200
    ha.on_play = tv_fetches
    return mgr, jf, ha, client


def _wait_cleanup():
    time.sleep(0.2)


def _open(mgr, ip="127.0.0.1"):
    return mgr.open_proxy_cast(APPLE, "Living room TV", ip, CH, "Horror")


def _walk(client, cast, ip="127.0.0.1"):
    """Fetch master, variant and every segment like a TV would."""
    eb = {"REMOTE_ADDR": ip}
    bodies = []
    r = client.get(f"/cast/{cast.cid}/index.m3u8", environ_base=eb)
    assert r.status_code == 200
    bodies.append(r.data.decode())
    variants = [l for l in r.data.decode().splitlines() if l.startswith("/cast/")]
    assert len(variants) == 1
    r = client.get(variants[0], environ_base=eb)
    assert r.status_code == 200
    bodies.append(r.data.decode())
    segs = [l for l in r.data.decode().splitlines() if l.startswith("/cast/")]
    segs += re.findall(r'URI="(/cast/[^"]+)"', r.data.decode())
    codes = [client.get(s, environ_base=eb).status_code for s in segs]
    return bodies, segs, codes


# ── the registry: Home Assistant decides which TVs are allowed ──

def test_list_is_only_ha_apple_tv_and_roku_media_players(env):
    mgr, jf, ha, client = env
    out = mgr.registry.public_list(mgr, refresh=True)
    ids = {d["id"]: d for d in out["devices"]}
    assert set(ids) == {APPLE, ROKU, "media_player.far"}
    assert "media_player.office_speaker" not in ids             # Music Assistant audio player
    assert "media_player.old_atv" not in ids          # disabled in HA
    assert ids[APPLE]["kind"] == "appletv" and ids[ROKU]["kind"] == "roku"
    assert ids[APPLE]["available"] and ids[ROKU]["available"]
    # its MAC only resolves to an address outside the house: not castable
    assert ids["media_player.far"]["available"] is False
    assert "ip" not in json.dumps(out) and APPLE_IP not in json.dumps(out)


def test_ip_comes_from_the_ha_device_mac(env):
    mgr, jf, ha, client = env
    snap = mgr.registry.snapshot(refresh=True)
    by = {t["entity_id"]: t for t in snap["tvs"]}
    assert (by[APPLE]["ip"], by[APPLE]["ip_source"]) == (APPLE_IP, "mdns-mac")
    assert (by[ROKU]["ip"], by[ROKU]["ip_source"]) == (ROKU_IP, "arp-mac")


def test_no_mac_match_fails_closed_even_when_a_name_matches(env):
    """An HA device whose MAC isn't seen on this box is never cast to.

    Here HA's "Office" device has no MAC on record, and an Apple TV named "Office"
    IS on the network: a name match would have picked its address. It must not.
    """
    mgr, jf, ha, client = env
    ENTITIES.append({"entity_id": "media_player.office_atv", "platform": "apple_tv",
                     "device_id": "d-ma"})
    ha.states["media_player.office_atv"] = "idle"
    try:
        mgr.registry.invalidate()
        out = mgr.registry.public_list(mgr, refresh=True)
        office = next(d for d in out["devices"] if d["id"] == "media_player.office_atv")
        assert office["available"] is False
        assert "won't cast to it" in office["why"]
        with pytest.raises(tv_cast.CastError) as e:
            mgr.start("media_player.office_atv", CH)
        assert e.value.code == "tv_unavailable" and "won't cast" in e.value.message
        assert not [c for c in ha.calls if c[1] == "play_media"]
    finally:
        ENTITIES.pop()
        ha.states.pop("media_player.office_atv", None)


def test_unlinked_apple_tvs_get_a_plain_hint(env):
    mgr, jf, ha, client = env
    out = mgr.registry.public_list(mgr, refresh=True)
    assert out["unlinked"] == ["Guest room TV"]
    assert "Guest room TV is on the network but not linked yet" in out["hint"]
    assert "Add integration, Apple TV" in out["hint"]
    assert "\u2014" not in out["hint"]


def test_unreachable_in_ha_is_listed_but_not_castable(env):
    mgr, jf, ha, client = env
    ha.states[APPLE] = "unavailable"
    mgr.registry.invalidate()
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(APPLE, CH)
    assert e.value.code == "tv_unavailable"
    assert not any(c[1] == "play_media" for c in ha.calls)


def test_only_listed_entities_can_be_cast_to(env):
    mgr, jf, ha, client = env
    for bad in ("media_player.office_speaker", "light.kitchen", "media_player.nope", "x; rm"):
        with pytest.raises(tv_cast.CastError):
            mgr.start(bad, CH)
    assert ha.calls == []


# ── casting through HA ──

def test_cast_goes_through_ha_play_media_with_a_clean_proxy_url(env):
    mgr, jf, ha, client = env
    view = mgr.start(APPLE, CH)
    assert view["state"] == "playing"
    (domain, service, entity, data), = [c for c in ha.calls if c[1] == "play_media"]
    assert (domain, entity, data["media_content_type"]) == ("media_player", APPLE, "video")
    url = data["media_content_id"]
    assert re.fullmatch(r"http://192\.0\.2\.10:8000/cast/[A-Za-z0-9_-]{43}/index\.m3u8", url)
    for c in ha.calls:
        assert not tv_cast.FORBIDDEN_RE.search(json.dumps(c)), c
    assert "/cast/" not in json.dumps(view)
    mgr.stop(None)


def test_roku_gets_play_on_roku_hls(env):
    mgr, jf, ha, client = env
    mgr.start(ROKU, CH)
    (_, _, entity, data), = [c for c in ha.calls if c[1] == "play_media"]
    assert entity == ROKU and data["media_content_type"] == "url"
    assert data["extra"]["format"] == "hls"
    mgr.stop(None)


def test_link_once_then_no_code_for_cast_channel_change_stop_and_recast(env):
    mgr, jf, ha, client = env
    mgr.start(APPLE, CH)
    mgr.start(APPLE, CH)                 # channel change
    mgr.stop(APPLE)
    _wait_cleanup()
    mgr.start(APPLE, CH)                 # came back
    mgr.stop(None)
    _wait_cleanup()
    services = [c[1] for c in ha.calls]
    assert services.count("play_media") == 3
    assert set(services) <= {"play_media", "media_stop"}
    assert not hasattr(mgr, "pair_start") and not hasattr(tv_cast, "credential_store")


def test_stop_revokes_the_id_and_tells_the_tv(env):
    mgr, jf, ha, client = env
    mgr.start(APPLE, CH)
    url = [c for c in ha.calls if c[1] == "play_media"][0][3]["media_content_id"]
    mgr.stop(APPLE)
    r = client.get(urlsplit(url).path, environ_base={"REMOTE_ADDR": APPLE_IP})
    assert r.status_code == 404
    _wait_cleanup()
    assert ("media_player", "media_stop", APPLE, {}) in ha.calls


def test_channel_change_kills_the_old_id(env):
    mgr, jf, ha, client = env
    mgr.start(APPLE, CH)
    first = [c for c in ha.calls if c[1] == "play_media"][0][3]["media_content_id"]
    mgr.start(APPLE, CH)
    assert client.get(urlsplit(first).path,
                      environ_base={"REMOTE_ADDR": APPLE_IP}).status_code == 404
    assert len(mgr._by_cid) == 1
    mgr.stop(None)


def test_ha_failure_revokes_and_says_so(env):
    mgr, jf, ha, client = env
    ha.fail = RuntimeError("HA returned HTTP 500: echo of http://192.0.2.10:8000/cast/x")
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(APPLE, CH)
    assert e.value.code == "ha_refused"
    assert "error 500" in e.value.message and "/cast/" not in e.value.message
    assert mgr._by_cid == {} and mgr._by_device == {}


def test_tv_that_never_fetches_is_a_clear_failure(env):
    mgr, jf, ha, client = env
    ha.on_play = None
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(APPLE, CH)
    assert e.value.code == "tv_silent"
    assert mgr._by_cid == {}


def test_ha_timeout_counts_as_handed_over(env):
    mgr, jf, ha, client = env
    orig = ha.on_play

    def slow(entity_id, data):
        orig(entity_id, data)
        raise RuntimeError("Home Assistant request timed out")
    ha.on_play = slow
    assert mgr.start(APPLE, CH)["state"] == "playing"
    mgr.stop(None)


def test_ha_probe_from_another_ip_cannot_use_the_id(env):
    """HA's apple_tv integration probes the URL from the HA host first."""
    mgr, jf, ha, client = env
    mgr.start(APPLE, CH)
    url = [c for c in ha.calls if c[1] == "play_media"][0][3]["media_content_id"]
    assert client.get(urlsplit(url).path,
                      environ_base={"REMOTE_ADDR": "192.0.2.5"}).status_code == 404
    mgr.stop(None)


# ── "More free channels": relayed through /cast/, door tv_free_channels ──

PLUTO_ID = "5ca525b650be2571e3943c63"
PLUTO_LIST_URL = (f"https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv"
                  f"/v2/stitch/hls/channel/{PLUTO_ID}/master.m3u8")
SESSION = "SESSIONTOKENsecretJWT"
STITCHER = "https://cfd-v6.prd.pluto.tv"
P_MASTER = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#PLUTO-SESSION:id=SESSIONTOKENsecretJWT\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
    f"{STITCHER}/v2/stitch/hls/channel/{PLUTO_ID}/2000/playlist.m3u8?jwt={SESSION}\n")
P_VARIANT = (
    "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:7\n"
    "#EXT-X-DATERANGE:ID=ad1,X-TOKEN=\"abc\"\n"
    "#EXTINF:6.0,\nhttps://siloh.pluto.tv/seg/1.ts?sig=abc\n"
    "#EXT-X-DISCONTINUITY\n"
    "#EXTINF:6.0,\nhttps://ads.example.org/seg/ad1\n"
    "#EXTINF:6.0,\nhttps://lan.example.org/seg/evil.ts\n"
    "#EXTINF:6.0,\nhttp://plain.example.org/seg/2.ts\n"
    "#EXTINF:6.0,\nhttps://192.0.2.5/seg/3.ts\n")


class FakeHTTP:
    """The relay's pinned session, with a fake internet behind it."""

    def __init__(self, log):
        self.log = log

    def get(self, url, **kw):
        assert kw.get("allow_redirects") is False
        self.log.append(url)
        host = urlsplit(url).hostname
        if host == "boot.pluto.tv":
            body = json.dumps({"servers": {"stitcher": STITCHER},
                               "stitcherParams": "deviceType=web&sid=1",
                               "sessionToken": SESSION}).encode()
            return _R(200, body)
        if url.startswith(STITCHER) and "/master.m3u8" in url:
            assert f"jwt={SESSION}" in url
            return _R(200, P_MASTER.encode())
        if url.endswith("/playlist.m3u8?jwt=" + SESSION):
            return _R(302, b"", {"Location": "https://cfd-v6.prd.pluto.tv/v.m3u8"})
        if url == "https://cfd-v6.prd.pluto.tv/v.m3u8":
            return _R(200, P_VARIANT.encode())
        return _R(200, b"\x47" * 500)

    def close(self):
        pass


class _R(FakeResp):
    def __init__(self, status, body, headers=None):
        super().__init__(status, body)
        self.headers = headers or {}


@pytest.fixture
def relay(env, tmp_path, monkeypatch):
    mgr, jf, ha, client = env
    m3u = tmp_path / "set-aside.m3u"
    m3u.write_text(
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-chno="300" group-title="Comedy",Comedy Central\n'
        f"{PLUTO_LIST_URL}\n"
        '#EXTINF:-1 tvg-chno="301" group-title="News",Leaky\n'
        "https://cdn.example.com/x.m3u8?token=abc\n")
    monkeypatch.setenv("PUFFPAI_TV_CAST_PUBLIC_M3U", str(m3u))
    monkeypatch.setattr(tv_cast, "_public_cache", (0.0, 0.0, []))
    log = []
    mgr._relay_http_factory = lambda: FakeHTTP(log)
    from puffpai.security import netaddr

    def fake_validate(host, port, posture=None, resolver=None):
        if host == "lan.example.org":
            raise netaddr.AddressRefused("private")
        return ["93.184.216.34"]
    monkeypatch.setattr(netaddr, "validated_addresses", fake_validate)
    return mgr, jf, ha, client, log


def _open_door(monkeypatch, on=True):
    from puffpai.security import egress_doors
    monkeypatch.setenv("PUFFPAI_EGRESS_TV_FREE_CHANNELS", "1" if on else "0")
    egress_doors.reset_cache()


def test_free_channels_are_off_until_the_door_opens(relay, monkeypatch):
    mgr, jf, ha, client, log = relay
    monkeypatch.delenv("PUFFPAI_EGRESS_TV_FREE_CHANNELS", raising=False)
    from puffpai.security import egress_doors
    egress_doors.reset_cache()
    out = mgr.channels()
    assert [g["name"] for g in out["groups"]] == ["Live TV"]
    assert out["more_free_channels"]["enabled"] is False
    _open_door(monkeypatch)
    groups = {g["name"]: g for g in mgr.channels()["groups"]}
    pub = groups[tv_cast.PUBLIC_GROUP]["channels"]
    assert [c["name"] for c in pub] == ["Comedy Central"] and "url" not in pub[0]
    assert groups[tv_cast.PUBLIC_GROUP]["apple_tv_only"] is True


def test_the_door_is_declared_default_shut_and_honoured():
    from puffpai.security import egress_doors as ed
    d = ed.door("tv_free_channels")
    assert d is not None and d.default is False and d.enforcement == "external"
    assert "boot.pluto.tv" in d.hosts and d.dynamic_hosts
    assert "tv_free_channels" in ed._FLAG_HONOURED_EXTERNAL


def test_pluto_cast_is_relayed_and_the_token_never_leaves_the_box(relay, monkeypatch):
    mgr, jf, ha, client, log = relay
    _open_door(monkeypatch)
    pub = mgr.channels()["groups"][1]["channels"]
    mgr.start(APPLE, pub[0]["id"])
    (_, _, entity, data), = [c for c in ha.calls if c[1] == "play_media"]
    url = data["media_content_id"]
    # HA (and so its logbook/recorder) only sees the opaque proxy link
    assert re.fullmatch(r"http://192\.0\.2\.10:8000/cast/[A-Za-z0-9_-]{43}/index\.m3u8", url)
    assert "pluto" not in json.dumps(ha.calls) and SESSION not in json.dumps(ha.calls)
    assert log[0].startswith("https://boot.pluto.tv/v4/start?") and f"channelID={PLUTO_ID}" in log[0]
    cast = mgr._by_device[APPLE]
    # The TV walks the stream
    eb = {"REMOTE_ADDR": APPLE_IP}
    master = client.get(urlsplit(url).path, environ_base=eb).data.decode()
    var = [l for l in master.splitlines() if l.startswith("/cast/")]
    assert len(var) == 1
    variant = client.get(var[0], environ_base=eb).data.decode()
    segs = [l for l in variant.splitlines() if l.startswith("/cast/")]
    for body in (master, variant):
        assert SESSION not in body and "pluto" not in body.lower()
        assert not tv_cast.FORBIDDEN_RE.search(body), body
        assert "PLUTO-SESSION" not in body and "DATERANGE" not in body   # vendor tags dropped
        assert "http" not in body
    assert "#EXT-X-DISCONTINUITY" in variant
    # pluto + extension-less ad + LAN-named segments kept; plain-http and IP-literal dropped
    assert len(segs) == 3 and variant.count("#EXTINF") == 3
    codes = [client.get(s, environ_base=eb).status_code for s in segs]
    assert codes == [200, 200, 502]                 # the LAN-resolving name is refused at fetch
    assert all(not s.startswith(("http://", "https://10.")) for s in log)
    # Stop: the id dies and the upstream (with the token) is dropped from memory
    mgr.stop(APPLE)
    assert client.get(urlsplit(url).path, environ_base=eb).status_code == 404
    assert cast.upstream is None and cast.resources == {}


def test_closing_the_door_stops_a_running_relay(relay, monkeypatch):
    mgr, jf, ha, client, log = relay
    _open_door(monkeypatch)
    pub = mgr.channels()["groups"][1]["channels"]
    mgr.start(APPLE, pub[0]["id"])
    url = [c for c in ha.calls if c[1] == "play_media"][0][3]["media_content_id"]
    n = len(log)
    _open_door(monkeypatch, on=False)
    r = client.get(urlsplit(url).path, environ_base={"REMOTE_ADDR": APPLE_IP})
    assert r.status_code == 502 and len(log) == n          # no socket opened
    mgr.stop(None)


def test_free_channels_refuse_a_roku_and_a_closed_door(relay, monkeypatch):
    mgr, jf, ha, client, log = relay
    _open_door(monkeypatch)
    pub = mgr.channels()["groups"][1]["channels"]
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(ROKU, pub[0]["id"])
    assert e.value.code == "apple_tv_only"
    _open_door(monkeypatch, on=False)
    with pytest.raises(tv_cast.CastError):
        mgr.start(APPLE, pub[0]["id"])
    assert log == [] and not [c for c in ha.calls if c[1] == "play_media"]


def test_relay_url_rules():
    ok = ["https://siloh.pluto.tv/a.ts?sig=1", "https://cdn.example.org:443/a.ts"]
    bad = ["http://siloh.pluto.tv/a.ts", "https://192.0.2.5/a.ts", "https://[::1]/a.ts",
           "https://nas.local/a.ts", "https://cdn.example.org:8443/a.ts",
           "https://u:p@cdn.example.org/a.ts", "https://localhost/a.ts", "file:///etc/passwd",
           "https://cdn.example.org/a b.ts"]
    for u in ok:
        assert tv_cast.relay_url_ok(u), u
    for u in bad:
        assert not tv_cast.relay_url_ok(u), u


def test_public_url_rules():
    ok = [PLUTO_LIST_URL, "https://stream-us-east-1.getpublica.com/playlist.m3u8?network_id=50"]
    bad = ["http://jmp2.uk/plu-x.m3u8", "https://192.0.2.10/x.m3u8",
           "https://8.8.8.8/x.m3u8", "https://nas.local/x.m3u8",
           "https://user:pw@cdn.example.com/x.m3u8",
           "https://cdn.example.com:8443/x.m3u8",
           "https://cdn.example.com/x.m3u8?authToken=abc",
           "https://cdn.example.com/x.m3u8?Signature=abc&Expires=1",
           "https://cdn.example.com/x.m3u8?api_key=abc",
           "https://cdn.example.com/token/x.m3u8",
           "https://cdn.example.com/x .m3u8", "/etc/passwd", "file:///etc/passwd"]
    for u in ok:
        assert tv_cast.public_url_ok(u), u
    for u in bad:
        assert not tv_cast.public_url_ok(u), u


# ── the proxy itself ──

def test_proxied_playlists_carry_no_token_or_port(env):
    mgr, jf, ha, client = env
    cast = _open(mgr)
    bodies, segs, codes = _walk(client, cast)
    for body in bodies:
        assert not tv_cast.FORBIDDEN_RE.search(body), body
        assert "evil.example" not in body and "SECRETKEY" not in body
        assert "192.0.2.10" not in body and "/videos/" not in body.lower()
    assert codes == [200, 200, 200]
    assert len(segs) == 3 and bodies[1].count("#EXTINF") == 2
    assert "METHOD=AES-128" not in bodies[1]


def test_upstream_requests_never_carry_auth_params(env):
    mgr, jf, ha, client = env
    _walk(client, _open(mgr))
    for _, pq in jf.calls:
        assert "apikey" not in pq.lower() and "api_key" not in pq.lower()
        assert pq.lower().startswith(f"/videos/{CH_DASH}/")


def test_allowed_upstream_rejects_other_hosts_channels_and_traversal():
    ok = tv_cast.allowed_upstream(f"{BASE}/videos/{CH_DASH}/hls1/0.ts?ApiKey=k&a=1", BASE, CH)
    assert ok == f"/videos/{CH_DASH}/hls1/0.ts?a=1"
    for bad in (f"http://192.0.2.11:8096/videos/{CH_DASH}/0.ts",
                f"http://192.0.2.10:8097/videos/{CH_DASH}/0.ts",
                f"https://192.0.2.10:8096/videos/{CH_DASH}/0.ts",
                f"{BASE}/videos/{CH_DASH}/../x/0.ts",
                f"{BASE}/videos/{CH_DASH}//etc/0.ts",
                f"{BASE}/Users/{CH}/0.ts",
                f"http://user:pw@192.0.2.10:8096/videos/{CH_DASH}/0.ts"):
        assert tv_cast.allowed_upstream(bad, BASE, CH) is None, bad


def test_leak_guard_refuses_a_playlist_that_would_still_leak():
    with pytest.raises(tv_cast.CastError):
        tv_cast.rewrite_playlist("#EXTM3U\n#EXT-X-SESSION-DATA:VALUE=\"token\"\n",
                                 BASE + "/videos/x/master.m3u8", lambda u: None)
    with pytest.raises(tv_cast.CastError):
        tv_cast.rewrite_playlist("<html>", BASE, lambda u: None)


def test_new_token_never_contains_forbidden_text(monkeypatch):
    seq = iter(["abcTOKENxyz", "has8096in", "clean_value"])
    monkeypatch.setattr(tv_cast.secrets, "token_urlsafe", lambda n: next(seq))
    assert tv_cast.new_token(32) == "clean_value"


def test_cast_id_is_bound_to_the_device_ip(env):
    mgr, jf, ha, client = env
    cast = _open(mgr, ip=APPLE_IP)
    assert client.get(f"/cast/{cast.cid}/index.m3u8").status_code == 404
    assert client.get(f"/cast/{cast.cid}/index.m3u8",
                      environ_base={"REMOTE_ADDR": APPLE_IP}).status_code == 200


def test_expiry_and_idle_revoke(env):
    mgr, jf, ha, client = env
    now = [1000.0]
    mgr._clock = lambda: now[0]
    a = _open(mgr)
    a.created, a.expires = now[0], now[0] + tv_cast.CAST_MAX_AGE_S
    now[0] += tv_cast.CAST_FIRST_FETCH_S + 1
    mgr.reap_once()
    assert mgr.lookup(a.cid, "127.0.0.1") is None
    b = _open(mgr)
    b.created, b.expires = now[0], now[0] + tv_cast.CAST_MAX_AGE_S
    assert mgr.lookup(b.cid, "127.0.0.1") is b
    now[0] += tv_cast.CAST_IDLE_S + 1
    mgr.reap_once()
    assert mgr.lookup(b.cid, "127.0.0.1") is None
    c = _open(mgr)
    c.expires = now[0] - 1
    assert mgr.lookup(c.cid, "127.0.0.1") is None


def test_unknown_ids_keys_and_exts_are_404(env):
    mgr, jf, ha, client = env
    cast = _open(mgr)
    assert client.get("/cast/" + "A" * 43 + "/index.m3u8").status_code == 404
    assert client.get(f"/cast/{cast.cid}/r/{'B' * 16}.ts").status_code == 404
    assert client.get(f"/cast/{cast.cid}/r/{'B' * 16}.php").status_code == 404
    assert client.get("/cast/short/index.m3u8").status_code == 404


def test_keys_from_one_cast_do_not_work_on_another(env):
    mgr, jf, ha, client = env
    a = _open(mgr)
    _, segs, _ = _walk(client, a)
    b = mgr.open_proxy_cast("media_player.other", "Other", "127.0.0.1", CH, "Horror")
    assert client.get(segs[0].replace(a.cid, b.cid)).status_code == 404


def test_lookup_is_hash_keyed_and_digest_compared(env, monkeypatch):
    mgr, jf, ha, client = env
    cast = _open(mgr)
    assert all(isinstance(k, bytes) and len(k) == 32 for k in mgr._by_cid)
    calls = []
    real = tv_cast.hmac.compare_digest
    monkeypatch.setattr(tv_cast.hmac, "compare_digest",
                        lambda a, b: calls.append(1) or real(a, b))
    assert mgr.lookup(cast.cid, "127.0.0.1") is cast and calls
    near = cast.cid[:-1] + ("A" if cast.cid[-1] != "A" else "B")
    assert mgr.lookup(near, "127.0.0.1") is None


def test_simultaneous_starts_cannot_exceed_the_cap_or_orphan_an_id(env, monkeypatch):
    mgr, jf, ha, client = env
    gate = threading.Event()
    real_open = jf.open_stream

    def slow_open(channel_id):
        gate.wait(2)
        return real_open(channel_id)
    jf.open_stream = slow_open
    monkeypatch.setattr(mgr, "stop", lambda *a, **k: 0)
    results = []

    def go():
        try:
            mgr.start(APPLE, CH)
            results.append("ok")
        except tv_cast.CastError as e:
            results.append(e.code)
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(8)
    assert sorted(results) == ["busy", "ok"]
    assert len(mgr._by_cid) == 1 and len(mgr._by_device) == 1
    tv_cast.CastManager.stop(mgr, None)


def test_cap_is_checked_and_claimed_atomically(env, monkeypatch):
    mgr, jf, ha, client = env
    monkeypatch.setattr(tv_cast, "MAX_ACTIVE_CASTS", 1)
    mgr.start(APPLE, CH)
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(ROKU, CH)
    assert e.value.code == "too_many" and len(mgr._by_cid) == 1
    mgr.stop(None)


# ── public surface and logging ──

def test_only_the_proxy_is_public():
    from puffpai import api_server
    from puffpai.security import endpoint_gate
    public = {e for e in api_server._PUBLIC_ENDPOINTS if "cast" in e}
    assert public == {"castproxy.master", "castproxy.resource"}
    assert public <= set(endpoint_gate.PUBLIC_ENDPOINT_JUSTIFICATIONS)
    rules = {r.endpoint for r in api_server.app.url_map.iter_rules()}
    assert {"tvcast.cast", "tvcast.stop", "tvcast.devices", "castproxy.master"} <= rules
    assert not any(r.startswith("tvcast.pair") or r in ("tvcast.forget", "tvcast.roku_code")
                   for r in rules)
    client = api_server.app.test_client()
    for path in ("/tvcast/stop", "/tvcast/cast"):
        assert client.post(path, json={}, environ_base={"REMOTE_ADDR": "192.0.2.30"}).status_code == 401
    assert client.get("/tvcast/devices", environ_base={"REMOTE_ADDR": "192.0.2.30"}).status_code == 401


def test_cast_lines_are_dropped_from_the_access_log():
    from puffpai.logging_config import _AccessLogCastFilter
    f = _AccessLogCastFilter()
    rec = logging.LogRecord("werkzeug", logging.INFO, "", 0, '%s "%s" %s %s', None, None)
    rec.args = ("GET /cast/abcdef/index.m3u8 HTTP/1.1", "200", "-")
    assert f.filter(rec) is False
    styled = logging.LogRecord("werkzeug", logging.INFO, "", 0, '"%s" %s %s',
                               ("\x1b[33mGET /cast/" + "A" * 43 + "/index.m3u8 HTTP/1.1\x1b[0m",
                                "404", "-"), None)
    assert f.filter(styled) is False
    ok = logging.LogRecord("werkzeug", logging.INFO, "", 0, "%s %s %s",
                           ("GET /health HTTP/1.1", "200", "-"), None)
    assert f.filter(ok) is True


def test_urllib3_debug_lines_never_reach_the_log(caplog):
    import http.server
    import requests

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tv_cast.quiet_urllib3_debug()
    caplog.set_level(logging.DEBUG)
    for name in tv_cast.URLLIB3_URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        assert requests.get(f"http://127.0.0.1:{srv.server_port}/videos/{CH_DASH}/m.m3u8"
                            "?LiveStreamId=SECRETLIVE", timeout=5).status_code == 200
    finally:
        srv.shutdown()
        for name in tv_cast.URLLIB3_URL_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETLIVE" not in text and "/videos/" not in text


# ── the library: Movies, Shows, Music ──

MOVIE = "0123456789abcdef0123456789abcdef"
DIVX = "fedcba9876543210fedcba9876543210"
SONG = "00112233445566778899aabbccddeeff"
SHOW = "aaaabbbbccccddddeeeeffff00001111"


class LibJF(FakeJF):
    """FakeJF plus the tv-cast user's view of the library."""

    ITEMS = {
        MOVIE: {"Id": MOVIE, "Type": "Movie", "Name": "Heat", "ProductionYear": 1995,
                "Path": "/media/movies/Heat.mkv"},
        DIVX: {"Id": DIVX, "Type": "Movie", "Name": "Old One", "ProductionYear": 2001},
        SONG: {"Id": SONG, "Type": "Audio", "Name": "Song", "AlbumArtist": "Band"},
        SHOW: {"Id": SHOW, "Type": "Series", "Name": "A Show"},
    }

    def __init__(self):
        super().__init__()
        self.opened = []

    def item(self, item_id):
        if item_id not in self.ITEMS:
            raise tv_cast.CastError("bad_item", "That is not in your library.")
        return self.ITEMS[item_id]

    def open_item_stream(self, item_id, audio):
        self.opened.append((item_id, audio))
        root = "audio" if audio else "videos"
        return {"master": f"/{root}/{tv_cast.dashed_guid(item_id)}/master.m3u8?DeviceId=x",
                "live_stream_id": "", "play_session_id": "p" + item_id[:4],
                "video_transcode": item_id == DIVX}


@pytest.fixture
def lib(env, monkeypatch):
    mgr, _jf, ha, client = env
    jf = LibJF()
    mgr._jf_factory = lambda: jf
    return mgr, jf, ha, client


def _cast_url(ha):
    plays = [c for c in ha.calls if c[1] == "play_media"]
    return plays[-1][3]["media_content_id"]


def test_a_movie_casts_through_the_same_clean_proxy(lib):
    mgr, jf, ha, client = lib
    view = mgr.start(APPLE, item_id=MOVIE)
    assert view["media"] == "movie" and view["channel_name"] == "Heat"
    url = _cast_url(ha)
    assert re.fullmatch(r"http://192\.0\.2\.10:8000/cast/[A-Za-z0-9_-]{43}/index\.m3u8", url)
    assert ha.calls[-1][3]["media_content_type"] == "video"
    cast = mgr._by_device[APPLE]
    bodies, segs, codes = _walk(client, cast, ip=APPLE_IP)
    for body in bodies:
        assert not tv_cast.FORBIDDEN_RE.search(body) and "/videos/" not in body.lower()
        assert "SECRETKEY" not in body and "192.0.2.10" not in body
    assert codes and all(c == 200 for c in codes)
    for _, pq in jf.calls:
        assert pq.lower().startswith(f"/videos/{tv_cast.dashed_guid(MOVIE)}/")
    mgr.stop(APPLE)
    _wait_cleanup()
    assert client.get(segs[0], environ_base={"REMOTE_ADDR": APPLE_IP}).status_code == 404


def test_a_song_uses_audio_hls_and_only_to_an_apple_tv(lib):
    mgr, jf, ha, client = lib
    assert mgr.start(APPLE, item_id=SONG)["media"] == "song"
    assert jf.opened[-1] == (SONG, True)
    # HA's apple_tv sends "music" over RAOP, fetched by HA itself (wrong
    # address for the id). "video" makes the TV's own player fetch it.
    assert ha.calls[-1][3]["media_content_type"] == "video"
    _walk(client, mgr._by_device[APPLE], ip=APPLE_IP)
    for _, pq in jf.calls:
        assert pq.lower().startswith(f"/audio/{tv_cast.dashed_guid(SONG)}/")
    mgr.stop(None)
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(ROKU, item_id=SONG)
    assert e.value.code == "apple_tv_only"


def test_items_the_tv_cast_user_cannot_see_or_cannot_play_are_refused(lib):
    mgr, jf, ha, client = lib
    for bad in ("ffffffffffffffffffffffffffffffff", SHOW, "../../Users"):
        with pytest.raises(tv_cast.CastError):
            mgr.start(APPLE, item_id=bad)
    with pytest.raises(tv_cast.CastError):
        mgr.start(APPLE, channel_id="nope")
    assert not [c for c in ha.calls if c[1] == "play_media"]
    assert mgr._by_cid == {} and mgr._by_device == {}


def test_only_one_video_conversion_at_a_time(lib):
    mgr, jf, ha, client = lib
    mgr.start(APPLE, item_id=DIVX)
    assert mgr._by_device[APPLE].video_transcode
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(ROKU, item_id=DIVX)
    assert e.value.code == "transcode_busy"
    assert mgr.start(ROKU, item_id=MOVIE)["media"] == "movie"   # a remux is fine
    mgr.stop(APPLE)
    assert mgr.start(ROKU, item_id=DIVX)["media"] == "movie"
    mgr.stop(None)


def test_a_paused_movie_keeps_its_id_but_not_forever(lib):
    mgr, jf, ha, client = lib
    mgr.start(APPLE, item_id=MOVIE)
    movie = mgr._by_device[APPLE]
    now = [movie.last_fetch + 1.0]
    mgr._clock = lambda: now[0]
    movie.expires = now[0] + tv_cast.CAST_MAX_AGE_S
    now[0] += tv_cast.CAST_IDLE_S + 60
    mgr.reap_once()
    assert mgr.lookup(movie.cid, APPLE_IP) is movie
    now[0] += tv_cast.CAST_IDLE_LIBRARY_S + 1
    mgr.reap_once()
    assert mgr.lookup(movie.cid, APPLE_IP) is None


def test_ha_fast_500_names_the_airplay_link_and_never_the_body(env):
    mgr, jf, ha, client = env
    ha.fail = RuntimeError("HA returned HTTP 500: echo http://192.0.2.10:8000/cast/x token=y")
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(APPLE, CH)
    msg = e.value.message
    assert "AirPlay link" in msg and "error 500" in msg and "Living room TV" in msg
    assert "/cast/" not in msg and "token" not in msg


def test_library_view_is_names_only():
    item = {"Id": MOVIE.upper(), "Type": "Movie", "Name": "Heat", "ProductionYear": 1995,
            "Path": "/media/movies/Heat.mkv", "MediaSources": [{"Path": "/x"}],
            "ServerId": "s"}
    v = tv_cast.library_view(item)
    assert v == {"id": MOVIE, "name": "Heat", "kind": "movie", "sub": "1995",
                 "playable": True, "apple_tv_only": False}
    ep = tv_cast.library_view({"Id": MOVIE, "Type": "Episode", "Name": "Pilot",
                               "ParentIndexNumber": 1, "IndexNumber": 2})
    assert ep["sub"] == "S1 E2" and ep["playable"]
    assert tv_cast.library_view({"Id": MOVIE, "Type": "Folder"}) is None
    assert tv_cast.library_view({"Id": "../x", "Type": "Movie"}) is None


def test_allowed_upstream_takes_this_items_audio_path_only():
    ok = tv_cast.allowed_upstream(f"{BASE}/audio/{tv_cast.dashed_guid(SONG)}/main.m3u8?ApiKey=K",
                                  BASE, SONG)
    assert ok and "ApiKey" not in ok
    assert tv_cast.allowed_upstream(f"{BASE}/audio/{tv_cast.dashed_guid(MOVIE)}/main.m3u8",
                                    BASE, SONG) is None
    assert tv_cast.allowed_upstream(f"{BASE}/Users/{SONG}/Items", BASE, SONG) is None


def test_clean_query():
    assert tv_cast.clean_query("  heat\x00\n ") == "heat"
    assert len(tv_cast.clean_query("x" * 500)) == tv_cast.QUERY_MAX


class _Http:
    def __init__(self, payload):
        self.payload = payload
        self.seen = []

    def request(self, method, url, headers=None, **kw):
        self.seen.append((method, url, headers))
        return FakeResp(200, json.dumps(self.payload).encode())


def test_library_queries_carry_the_token_in_a_header_only():
    http = _Http({"Items": [{"Id": MOVIE, "Type": "Movie", "Name": "Heat", "Path": "/p"}],
                  "TotalRecordCount": 1})
    jf = tv_cast.JellyfinCastClient(BASE, {"user_id": "u1", "access_token": "TOKENVALUE"}, http=http)
    out = jf.library("movies", "he at", 0)
    assert out["items"][0]["name"] == "Heat" and "Path" not in json.dumps(out)
    method, url, headers = http.seen[-1]
    assert "TOKENVALUE" not in url and "TOKENVALUE" in headers["Authorization"]
    assert "SearchTerm=he+at" in url and "IncludeItemTypes=Movie" in url
    jf.library("music", "x", 0)
    assert "IncludeItemTypes=MusicAlbum%2CAudio" in http.seen[-1][1]
    with pytest.raises(tv_cast.CastError):
        jf.library("comics-and-more", "", 0)
    jf.library("movies", "", 0, "H")
    assert "NameStartsWith=H" in http.seen[-1][1]
    with pytest.raises(tv_cast.CastError):
        jf.library("movies", "", 0, "%")


ANIME_LIB = "5ceaa5db75e643968ba414d266efec1c"
SHOWS_LIB = "eefe748a87814b729a2360b3530d407d"


class _ViewsHttp(_Http):
    """Answers /Views with the house's libraries, everything else with *payload*."""

    def __init__(self, payload, views):
        super().__init__(payload)
        self.views = views

    def request(self, method, url, headers=None, **kw):
        if url.endswith("/Views"):
            self.seen.append((method, url, headers))
            return FakeResp(200, json.dumps({"Items": self.views}).encode())
        return super().request(method, url, headers=headers, **kw)


def test_every_library_has_a_tab_anime_and_books_included():
    views = [{"Id": ANIME_LIB, "Name": "Anime", "CollectionType": "tvshows"},
             {"Id": SHOWS_LIB, "Name": "Shows", "CollectionType": "tvshows"}]
    http = _ViewsHttp({"Items": [{"Id": SHOW, "Type": "Series", "Name": "Harbor Lights"}],
                       "TotalRecordCount": 1}, views)
    jf = tv_cast.JellyfinCastClient(BASE, {"user_id": "u1", "access_token": "T"}, http=http)
    out = jf.library("anime", "", 0)
    url = http.seen[-1][1]
    assert out["items"][0]["kind"] == "show" and f"ParentId={ANIME_LIB}" in url
    assert "IncludeItemTypes=Series%2CMovie" in url
    jf.library("shows", "", 0)
    assert f"ParentId={SHOWS_LIB}" in http.seen[-1][1]   # Shows and Anime don't repeat
    jf.library("books", "dune", 0)
    assert "IncludeItemTypes=Book" in http.seen[-1][1] and "ParentId" not in http.seen[-1][1]
    # A missing library is an empty tab, never "everything".
    http.views = []
    assert jf.library("anime", "", 0)["items"] == []


def test_a_book_is_listed_but_never_cast(lib):
    book = {"Id": MOVIE, "Type": "Book", "Name": "Dune", "ProductionYear": 1965,
            "Path": "/mnt/media/books/x.epub"}
    v = tv_cast.library_view(book)
    assert v["kind"] == "book" and not v["playable"] and "Path" not in v
    mgr, jf, ha, client = lib
    jf.item = lambda item_id: book
    with pytest.raises(tv_cast.CastError) as e:
        mgr.start(APPLE, item_id=MOVIE)
    assert e.value.code == "bad_item"


def test_library_routes_answer_names_only(lib):
    mgr, jf, ha, client = lib
    jf.library = lambda kind, q, start, letter="": {"items": [tv_cast.library_view(LibJF.ITEMS[MOVIE])],
                                         "total": 1, "start": 0, "page": 60}
    r = client.get("/tvcast/library?kind=movies&q=heat")
    assert r.status_code == 200 and b"/media/" not in r.data and b"Heat" in r.data
    r = client.post("/tvcast/cast", json={"device": APPLE, "item": MOVIE})
    assert r.status_code == 200 and r.get_json()["cast"]["media"] == "movie"
    mgr.stop(None)
