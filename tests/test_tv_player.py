"""Tests for puffpai.tv_player, the kiosk's hardware-decoded TV player.

Docker is never called: ``_docker`` is replaced with a recorder. What is
pinned here is what matters for safety:

* the Jellyfin key never reaches the docker command line (the config file is
  mounted read-only instead, and only for Jellyfin channels);
* callers pick channels by id; a URL in the request is never played;
* Pluto channels play by Pluto channel id only (no address, no token);
* the container is least privilege (9 device nodes, no capabilities, no
  privilege gain, read-only root and registry cache);
* ids are validated before anything runs;
* stop always tries to remove the container, even if docker misbehaves.
"""

import hashlib
import subprocess

import pytest
from flask import Flask

from puffpai import tv_player

PLUTO_ID = "5ca671f215a62078d2ec0abf"
M3U = """#EXTM3U
#EXTINF:-1 tvg-chno="100" tvg-id="X" group-title="Comedy",Comedy Central (Pluto)
https://jmp2.uk/plu-5ca671f215a62078d2ec0abf.m3u8
#EXTINF:-1 tvg-id="Y",No Group Channel
https://example.com/live/master.m3u8
#EXTINF:-1 group-title="Local",Sneaky Local
http://127.0.0.1:8000/secret.m3u8
#EXTINF:-1 group-title="Comedy",Pluto Direct
https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv/v2/stitch/hls/channel/5ca671f215a62078d2ec0abf/master.m3u8
"""
class _Recorder:
    def __init__(self, running_after_run=True):
        self.calls = []
        self.running = False
        self.running_after_run = running_after_run

    def __call__(self, args, timeout=20):
        self.calls.append(list(args))
        out = ""
        if args[0] == "run":
            self.running = self.running_after_run
        elif args[0] in ("stop", "rm"):
            self.running = False
        elif args[0] == "ps":
            out = "abc123\n" if self.running else ""
        elif args[0] == "logs":
            out = "tvwindow: first frame after 7.0s\n"
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    m3u = tmp_path / "iptv-unreliable.m3u"
    m3u.write_text(M3U, encoding="utf-8")
    monkeypatch.setattr(tv_player, "_FREE_ADS_M3U", str(m3u))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "gst-registry.bin").write_bytes(b"x")
    monkeypatch.setattr(tv_player, "_CACHE_DIR", str(cache))
    monkeypatch.setattr(tv_player, "supported", lambda: True)
    monkeypatch.setattr(tv_player.time, "sleep", lambda s: None)
    rec = _Recorder()
    monkeypatch.setattr(tv_player, "_docker", rec)
    monkeypatch.setattr(tv_player, "jellyfin_channels", lambda: [
        {"id": "a" * 32, "number": "1", "name": "Movie Night"}])
    import puffpai.radio_player as rp
    monkeypatch.setattr(rp, "stop", lambda: True)
    monkeypatch.setattr(rp, "_resolve", lambda host: ["93.184.216.34"])
    # Free channels play only with their Bridge switch open; the closed case
    # is pinned in tests/test_tv_bridge_switch.py.
    from puffpai.security import egress_doors
    monkeypatch.setenv("PUFFPAI_EGRESS_TV_FREE_CHANNELS", "1")
    egress_doors.reset_cache()
    return rec


def _run_call(rec):
    return next(c for c in rec.calls if c[0] == "run")


def test_free_list_parses_names_groups_and_stable_ids(env):
    chans = tv_player.free_ad_channels()
    assert [c["name"] for c in chans] == [
        "Comedy Central (Pluto)", "No Group Channel", "Sneaky Local", "Pluto Direct"]
    assert chans[0]["group"] == "Comedy" and chans[1]["group"] == "Other"
    assert chans[0]["id"] == hashlib.sha1(
        b"https://jmp2.uk/plu-5ca671f215a62078d2ec0abf.m3u8").hexdigest()[:12]


def test_play_free_runs_url_mode_without_config_mount(env):
    fid = tv_player.free_ad_channels()[1]["id"]
    ok, why = tv_player.play_free(fid)
    assert ok, why
    run = _run_call(env)
    assert run[-3:] == [tv_player.IMAGE, "url", "https://example.com/live/master.m3u8"]
    assert not any("media.json" in a for a in run)
    assert tv_player.status()["name"] == "No Group Channel"


@pytest.mark.parametrize("idx", [0, 3])
def test_pluto_plays_by_channel_id_only(env, idx):
    """The old relay and Pluto's own address both play by id; no address or
    token reaches the command line (the player starts its own session)."""
    ch = tv_player.free_ad_channels()[idx]
    ok, why = tv_player.play_free(ch["id"])
    assert ok, why
    run = _run_call(env)
    assert run[-3:] == [tv_player.IMAGE, "pluto", PLUTO_ID]
    assert not any("pluto.tv" in a or "jmp2" in a or "jwt" in a for a in run)


@pytest.mark.parametrize("url", [
    "https://evil.example/channel/5ca671f215a62078d2ec0abf/x.m3u8",
    "https://pluto.tv.evil.example/v2/channel/5ca671f215a62078d2ec0abf/m",
    "http://jmp2.uk/plu-5ca671f215a62078d2ec0abf.m3u8",
])
def test_pluto_id_only_for_real_pluto_addresses(url):
    assert tv_player.pluto_id(url) is None


def test_container_is_least_privilege(env):
    tv_player.play_jellyfin("a" * 32)
    run = _run_call(env)
    devices = [run[i + 1] for i, a in enumerate(run) if a == "--device"]
    assert len(devices) == 9
    assert not any(x in d for d in devices
                   for x in ("dla", "pva", "nvenc", "dbg", "prof", "fb", "nvsciipc"))
    joined = " ".join(run)
    assert "--cap-drop ALL" in joined and "--read-only" in joined
    assert "--security-opt no-new-privileges" in joined
    assert any(a.endswith(":/cache:ro") for a in run)


def test_registry_is_built_once_when_missing(env, tmp_path):
    (tmp_path / "cache" / "gst-registry.bin").unlink()
    tv_player.play_jellyfin("a" * 32)
    runs = [c for c in env.calls if c[0] == "run"]
    assert runs[0][-1] == "nvv4l2decoder"
    assert any(a.endswith(":/cache:rw") for a in runs[0])
    assert runs[1][-2:] == ["jellyfin", "a" * 32]


def test_play_free_refuses_an_indoor_address(env):
    fid = tv_player.free_ad_channels()[2]["id"]
    ok, why = tv_player.play_free(fid)
    assert not ok and "inside this house" in why
    assert not any(c[0] == "run" for c in env.calls)


def test_play_jellyfin_mounts_config_read_only_and_never_passes_the_key(env, monkeypatch):
    ok, why = tv_player.play_jellyfin("a" * 32)
    assert ok, why
    run = _run_call(env)
    joined = " ".join(run)
    assert f"{tv_player._media_config()}:/run/puffpai/media.json:ro" in joined
    assert "api_key" not in joined.lower() and "token" not in joined.lower()
    assert run[-2:] == ["jellyfin", "a" * 32]


@pytest.mark.parametrize("bad", ["", "zz", "a" * 31, "../" + "a" * 29, "A" * 33])
def test_bad_ids_never_start_anything(env, bad):
    assert not tv_player.play_jellyfin(bad)[0]
    assert not tv_player.play_free(bad)[0]
    assert not any(c[0] == "run" for c in env.calls)


def test_unknown_channel_is_refused(env):
    assert tv_player.play_jellyfin("b" * 32) == (False, "that channel is not in the guide")


def test_player_that_exits_at_once_is_reported(env):
    env.running_after_run = False
    ok, why = tv_player.play_jellyfin("a" * 32)
    assert not ok and "did not start" in why


def test_stop_removes_even_when_docker_stop_raises(env, monkeypatch):
    calls = []

    def flaky(args, timeout=20):
        calls.append(args[0])
        if args[0] == "stop":
            raise subprocess.TimeoutExpired("docker", 15)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tv_player, "_docker", flaky)
    assert tv_player.stop() is True
    assert calls[:2] == ["stop", "rm"]


@pytest.fixture
def client(env):
    app = Flask(__name__)
    tv_player.register(app)
    return app.test_client()


def test_routes_play_by_id_only(client, env):
    r = client.post("/api/tv/native/play", json={"url": "https://evil.example/x.m3u8"})
    assert r.status_code == 400
    assert not any(c[0] == "run" for c in env.calls)
    r = client.post("/api/tv/native/play", json={"channel_id": "a" * 32})
    assert r.status_code == 200 and r.get_json()["phase"] == "playing"


def test_channels_route_hides_stream_urls(client):
    data = client.get("/api/tv/native/channels").get_json()
    assert data["channels"][0]["name"] == "Movie Night"
    assert all(set(c) == {"id", "name", "group"} for c in data["free_ads"])
    assert "jmp2.uk" not in str(data) and "pluto.tv" not in str(data)


def test_stop_route(client, env):
    r = client.post("/api/tv/native/stop")
    assert r.status_code == 200 and r.get_json()["phase"] == "off"


def test_off_a_jetson_the_player_says_unsupported_and_refuses_to_play(
        env, monkeypatch):
    from flask import Flask
    monkeypatch.setattr(tv_player, "supported", lambda: False)
    app = Flask(__name__)
    tv_player.register(app)
    c = app.test_client()
    prefix = tv_player.tv_bp.url_prefix or ""
    st = c.get(prefix + "/status").get_json()
    assert st["supported"] is False
    r = c.post(prefix + "/play", json={"channel_id": "a" * 32})
    assert r.status_code == 503
    assert r.get_json()["supported"] is False
    assert not any(call[0] == "run" for call in env.calls)


def test_supported_needs_the_tegra_decoder_and_the_host1x_node(monkeypatch,
                                                               tmp_path):
    monkeypatch.setattr(tv_player, "_LIB", str(tmp_path / "lib"))
    monkeypatch.setattr(tv_player, "_HOST1X_RENDER", str(tmp_path / "render"))
    monkeypatch.setattr(tv_player.sys, "platform", "linux")
    assert tv_player.supported() is False
    (tmp_path / "lib" / "nvidia").mkdir(parents=True)
    (tmp_path / "lib" / "nvidia" / "libnvv4l2.so").write_bytes(b"")
    assert tv_player.supported() is False
    (tmp_path / "render").write_bytes(b"")
    assert tv_player.supported() is True


def test_the_media_config_comes_from_the_install_config_dir(monkeypatch):
    import os
    from puffpai.config import get_config
    monkeypatch.setattr(tv_player, "_MEDIA_CONFIG", "")
    assert tv_player._media_config() == os.path.join(
        get_config().CONFIG_DIR, "media_server_config.json")
    assert "/opt/puffpai" not in tv_player._media_config()
