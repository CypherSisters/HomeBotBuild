"""Voice/chat "stop the tv" for the kiosk's full-screen TV player.

The TV phrases claim the turn only while the player is on; otherwise they
fall through exactly as before (so "turn off the tv" still reaches Home
Assistant). "stop jellyfin" also stops the player. Plain music/radio stop
phrases keep their old replies. Nothing here touches docker or audio.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api          # noqa: E402
from puffpai import radio_player          # noqa: E402
from puffpai import tv_player             # noqa: E402

TV_PHRASES = [
    "stop the tv", "turn off the tv", "stop the channel", "stop watching",
    "turn the tv off", "Stop the TV.", "please turn off the television",
    "puff, stop the tv", "stop watching tv", "turn the tv off please",
]
NOT_TV = [
    "stop", "stop the music", "stop the radio", "pause the music",
    "turn off the kitchen light", "stop the timer", "what's on tv",
    "stop watching me", "turn off everything",
]


@pytest.fixture
def tv(monkeypatch):
    state = {"on": True, "stops": 0}

    def _stop():
        state["stops"] += 1
        state["on"] = False
        return True

    monkeypatch.setattr(tv_player, "is_playing", lambda: state["on"])
    monkeypatch.setattr(tv_player, "stop", _stop)
    return state


@pytest.mark.parametrize("phrase", TV_PHRASES)
def test_tv_phrases_stop_the_player_when_on(tv, phrase):
    assert api._try_tv_stop(phrase) == "Okay, the TV is off."
    assert tv["stops"] == 1


@pytest.mark.parametrize("phrase", TV_PHRASES)
def test_tv_phrases_fall_through_when_player_is_off(tv, phrase):
    tv["on"] = False
    assert api._try_tv_stop(phrase) is None
    assert tv["stops"] == 0


@pytest.mark.parametrize("phrase", NOT_TV)
def test_other_phrases_are_not_claimed(tv, phrase):
    assert api._try_tv_stop(phrase) is None
    assert tv["stops"] == 0


def test_stop_jellyfin_stops_the_player(tv, monkeypatch):
    monkeypatch.setattr(radio_player, "stop", lambda: True)

    class _Bridge:
        def now_playing(self):
            return []
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge", lambda: _Bridge())
    assert api._try_media_stop("stop jellyfin") == "Okay, the TV is off."
    assert tv["stops"] == 1


def test_stop_jellyfin_with_tv_off_keeps_old_reply(tv, monkeypatch):
    tv["on"] = False
    monkeypatch.setattr(radio_player, "stop", lambda: True)

    class _Bridge:
        def now_playing(self):
            return []
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge", lambda: _Bridge())
    assert api._try_media_stop("stop jellyfin") == "Stopped the media server playback."
    assert tv["stops"] == 0


def test_dispatcher_routes_stop_the_tv_to_the_player(tv, monkeypatch):
    """Through _try_instant_answer, with the TV on."""
    assert api._try_instant_answer("stop the tv") == "Okay, the TV is off."
    assert tv["stops"] == 1


def test_bypass_helper_matches(tv):
    assert api._try_media_bypass_rules("turn the tv off") == "Okay, the TV is off."


def test_music_stop_reply_unchanged_and_tv_stopped_quietly(tv, monkeypatch):
    """A plain "stop the music" keeps its old sentence (the bare stop also
    ends the TV player, as before)."""
    for mod, name in (("puffpai.radio_player", "stop"),
                      ("puffpai.media_hub", "tool_radio_stop"),
                      ("puffpai.spotify_control", "tool_spotify_pause")):
        monkeypatch.setattr(f"{mod}.{name}", lambda *a, **k: "ok")
    monkeypatch.setattr(api, "_media_sources_still_playing", lambda: [])
    assert api._try_media_stop("stop the music") == "Okay \u2014 stopping any music or radio."
