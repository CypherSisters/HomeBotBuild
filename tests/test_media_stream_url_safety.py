"""The stream URL a stranger's catalog returned is not allowed to be anything.

Station rows come from radio-browser and TuneIn and are handed to an mpv
container running with ``--network host``. These tests pin the three things
that stops: non-stream schemes, option injection into the mpv argv, and
targets inside the house.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai import radio_player                             # noqa: E402


@pytest.fixture
def spawned(monkeypatch):
    """Capture the docker argv instead of running it. ``play`` reports ok."""
    class _Calls(list):
        def run(self):
            """The last ``docker run`` argv (``stop()`` logs an rm first)."""
            return [c for c in self if c and c[0] == "run"][-1]

    calls = _Calls()

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _docker(args, timeout=None):
        calls.append(list(args))
        return _R()

    monkeypatch.setattr(radio_player, "_docker", _docker)
    monkeypatch.setattr(radio_player, "is_playing", lambda: True)
    # Public by default, so only the test's own refusals are under test.
    monkeypatch.setattr(radio_player, "_resolve", lambda host: ["93.184.216.34"])
    return calls


# ── schemes ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/shadow",
    "file:///data/puffpai/state/config/profiles.json",
    "smb://192.168.1.5/share/x.mp3",
    "av://v4l2:/dev/video0",
    "lavfi://amovie=/etc/passwd",
    "memory://whatever",
    "edl://!;file:///etc/hosts",
    "ytdl://ytsearch:cats",
    "ftp://example.com/x.mp3",
    "javascript:alert(1)",
    "//example.com/stream",
])
def test_only_http_schemes_reach_the_player(url, spawned):
    ok, detail = radio_player.play(url)
    assert ok is False
    assert "not a stream address" in detail
    assert not [c for c in spawned if c and c[0] == "run"], \
        "a non-stream scheme must never reach docker run"


def test_http_and_https_are_allowed(spawned):
    assert radio_player.play("http://stream.example.test/live",
                             verify=False)[0] is True
    assert radio_player.play("https://stream.example.test/live",
                             verify=False)[0] is True


# ── option injection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "--script=/tmp/evil.lua",
    "--ao=pcm --ao-pcm-file=/data/puffpai/state/leak.wav",
    "-v",
    "  --input-ipc-server=/tmp/sock",
])
def test_a_dashed_url_is_refused_before_docker(url, spawned):
    ok, _ = radio_player.play(url)
    assert ok is False
    assert not [c for c in spawned if c and c[0] == "run"]


def test_argv_separates_options_from_urls(spawned):
    radio_player.play("http://stream.example.test/live", verify=False)
    argv = spawned.run()
    assert "--" in argv, "mpv option parsing must be terminated before the URL"
    assert argv.index("--") < argv.index("http://stream.example.test/live")
    assert argv[-1] == "http://stream.example.test/live"


def test_jellyfin_queue_also_gets_the_separator(spawned):
    radio_player.play_urls(["http://jf.example.test/Audio/1/stream"],
                           header="X-Emby-Token: k")
    argv = spawned.run()
    assert argv.index("--") < argv.index("http://jf.example.test/Audio/1/stream")


def test_ytdl_and_script_loading_are_off(spawned):
    radio_player.play("http://stream.example.test/live", verify=False)
    argv = spawned.run()
    assert "--ytdl=no" in argv
    assert "--load-scripts=no" in argv
    assert "--no-config" in argv


# ── control characters ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://stream.example.test/live\n--script=/tmp/x.lua",
    "http://stream.example.test/li\x00ve",
    "http://stream.example.test/li\rve",
])
def test_control_characters_are_refused(url, spawned):
    ok, detail = radio_player.play(url)
    assert ok is False
    assert "control characters" in detail
    assert not [c for c in spawned if c and c[0] == "run"]


# ── indoors ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/api/apps/mediahub/action",
    "http://localhost:8000/",
    "http://[::1]:8000/",
    "http://192.168.1.1/admin",
    "http://10.0.0.5:8123/",
    "http://172.16.4.4/",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0:8000/",
    "http://puffpai.local:8096/",
    "http://box.lan/",
])
def test_indoor_targets_are_refused(url, spawned):
    ok, detail = radio_player.play(url)
    assert ok is False
    assert "inside this house" in detail
    assert not [c for c in spawned if c and c[0] == "run"]


def test_a_public_name_that_resolves_indoors_is_refused(monkeypatch, spawned):
    monkeypatch.setattr(radio_player, "_resolve", lambda host: ["127.0.0.1"])
    ok, detail = radio_player.play("http://rebind.example.test/live")
    assert ok is False
    assert "inside this house" in detail
    assert not [c for c in spawned if c and c[0] == "run"]


def test_one_indoor_address_among_several_is_enough_to_refuse(monkeypatch,
                                                              spawned):
    monkeypatch.setattr(radio_player, "_resolve",
                        lambda host: ["93.184.216.34", "10.1.2.3"])
    assert radio_player.play("http://mixed.example.test/live",
                             verify=False)[0] is False
    assert not [c for c in spawned if c and c[0] == "run"]


def test_a_name_that_does_not_resolve_is_allowed_through(monkeypatch, spawned):
    def _boom(host):
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(radio_player, "_resolve", _boom)
    # It reaches nothing, so there is nothing to refuse — mpv fails on its own.
    assert radio_player.play("http://dead.example/stream",
                             verify=False)[0] is True


def test_empty_url_still_refused(spawned):
    assert radio_player.play("", verify=False)[0] is False
    assert radio_player.play(None, verify=False)[0] is False
    assert not [c for c in spawned if c and c[0] == "run"]


# ── the choke point is the one every radio path uses ─────────────────────────

def test_favorite_and_candidate_rows_go_through_the_same_check(monkeypatch):
    """A saved favorite is replayed later; it gets the same refusal."""
    seen = []
    monkeypatch.setattr(radio_player, "_docker",
                        lambda args, timeout=None: seen.append(args))
    ok, detail = radio_player.play(
        "file:///data/puffpai/app_data/mediahub.json", verify=False)
    assert (ok, seen) == (False, [])
    assert "not a stream address" in detail
