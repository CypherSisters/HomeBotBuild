"""Now-playing derived from the real player.

The failure mode: with a real ``mpv --ao=pulse <icecast url>`` process AND a
live Pulse sink-input both present (World Radio jazz, or TuneIn NPR),

    "whats playing right now" -> "There's nothing playing at the moment."
    "whats playing" -> "I'm not seeing any active media session..."

The first two phrasings below are the primary ones, verbatim.

Every source is faked here — no docker, no Pulse, no Jellyfin, no network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import now_playing as np  # noqa: E402

#: The real probes, captured before any fixture swaps them out.
_REAL_JELLYFIN = np._jellyfin_sources

#: The primary phrasings, exactly as typed.
PRIMARY_PHRASINGS = ("whats playing right now", "whats playing")
MORE_PHRASINGS = (
    "what's playing",
    "what is playing right now",
    "what's playing at the moment",
    "what song is this",
    "what am I listening to",
    "is anything playing",
)


class _Item:
    display = "Toes by Glass Animals"


class _Session:
    def __init__(self, paused=False):
        self.now_playing = _Item()
        self.device_name = "Living Room"
        self.is_paused = paused


class _Bridge:
    def __init__(self, sessions=()):
        self._sessions = list(sessions)

    def now_playing(self):
        return self._sessions


class _AppData:
    def __init__(self, doc):
        self._doc = doc

    def get_data(self, _name):
        return self._doc


def _radio_doc(name="Adroit Jazz Underground", status="playing"):
    return {"radio": {"status": status, "station": {"name": name}}}


@pytest.fixture
def silence(monkeypatch):
    """Every source off, Pulse empty — the honest 'nothing' baseline."""
    monkeypatch.setattr("puffpai.radio_player.is_playing", lambda: False)
    monkeypatch.setattr(np, "_jellyfin_sources", lambda: [])
    monkeypatch.setattr(np, "_pulse_sink_input_count", lambda: 0)
    return monkeypatch


# ── a live stream is never read as silence ───────────────────────────────────

@pytest.mark.parametrize("said", PRIMARY_PHRASINGS + MORE_PHRASINGS)
def test_live_world_radio_is_named_not_denied(said, silence, monkeypatch):
    """A live radio process must never read as silence."""
    monkeypatch.setattr("puffpai.radio_player.is_playing", lambda: True)
    monkeypatch.setattr(
        np, "_radio_station_name",
        lambda _ad: "Adroit Jazz Underground")

    reply = api._try_now_playing(said)
    assert reply, f"{said!r} must be claimed by the now-playing fastpath"
    low = reply.lower()
    assert "nothing is playing" not in low
    assert "no active media session" not in low
    assert "Adroit Jazz Underground" in reply       # the station
    assert np.RADIO_SOURCE in reply                 # the source


def test_live_tunein_stream_is_named(silence, monkeypatch):
    """A TuneIn station: same player, different station name."""
    monkeypatch.setattr("puffpai.radio_player.is_playing", lambda: True)
    monkeypatch.setattr(np, "_radio_station_name",
                        lambda _ad: "WOSU 89.7 NPR News")
    reply = api._try_now_playing("whats playing right now")
    assert "WOSU 89.7 NPR News" in reply
    assert "nothing is playing" not in reply.lower()


# ── each source, faked ───────────────────────────────────────────────────────

def test_radio_station_name_comes_from_mediahub(silence, monkeypatch):
    monkeypatch.setattr("puffpai.radio_player.is_playing", lambda: True)
    entries = np.observed_playback(_AppData(_radio_doc()))
    assert entries == [{"source": np.RADIO_SOURCE,
                        "label": "Adroit Jazz Underground"}]


def test_radio_name_ignores_the_kiosk_heartbeat(silence, monkeypatch):
    """A dead/stale kiosk must not turn audible radio into silence.

    ``media_hub.aggregate_sources`` gates the dashboard tile on
    ``radio_last_seen`` (a KIOSK poll heartbeat). now_playing must not: the
    kiosk's health says nothing about whether mpv is making sound.
    """
    monkeypatch.setattr("puffpai.radio_player.is_playing", lambda: True)
    doc = _radio_doc()
    doc["radio_last_seen"] = "2020-01-01T00:00:00+00:00"   # ancient
    doc["radio"]["updated_at"] = "2020-01-01T00:00:00+00:00"
    entries = np.observed_playback(_AppData(doc))
    assert entries and entries[0]["label"] == "Adroit Jazz Underground"


def test_jellyfin_session_is_named(silence, monkeypatch):
    monkeypatch.setattr(np, "_jellyfin_sources", _REAL_JELLYFIN)
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda: _Bridge([_Session()]))
    reply = np.describe_playback(_AppData({}))
    assert "Toes by Glass Animals" in reply
    assert np.JELLYFIN_SOURCE in reply


# ── the honesty backstop ─────────────────────────────────────────────────────

def test_unnameable_sound_is_admitted_not_denied(silence, monkeypatch):
    """A sink-input with no source claiming it: say so, never 'nothing'."""
    monkeypatch.setattr(np, "_pulse_sink_input_count", lambda: 1)
    reply = np.describe_playback(_AppData({}))
    assert "nothing is playing" not in reply.lower()
    assert "can't tell you what" in reply.lower()


def test_true_silence_still_says_nothing_is_playing(silence):
    assert np.observed_playback(_AppData({})) == []
    assert np.describe_playback(_AppData({})) == "Nothing is playing right now."


def test_radio_container_without_a_station_is_honest(silence, monkeypatch):
    """The mpv container is shared with the Jellyfin playlist queue."""
    monkeypatch.setattr("puffpai.radio_player.is_playing", lambda: True)
    entries = np.observed_playback(_AppData({"radio": {"status": "idle"}}))
    assert entries == [{"source": np.SPEAKERS_SOURCE, "label": None}]
    assert "can't tell you what" in np.describe_playback(
        _AppData({"radio": {"status": "idle"}})).lower()


def test_probe_failures_never_raise(silence, monkeypatch):
    """This answers a spoken question; it degrades, it does not traceback.

    When EVERY probe is unreadable the reply is an honest unknown, not
    "Nothing is playing right now.", which would be the same false negative
    this module exists to prevent, reached by a different road: with no
    working probe we do not know that nothing is playing, we know that we
    cannot tell.
    """
    def _boom(*_a, **_k):
        raise RuntimeError("docker is gone")

    monkeypatch.setattr("puffpai.radio_player.is_playing", _boom)
    monkeypatch.setattr(np, "_jellyfin_sources", _boom)
    monkeypatch.setattr(np, "_pulse_sink_input_count", _boom)
    reply = np.describe_playback(_AppData({}))
    assert reply == np.UNKNOWN_REPLY
    assert "nothing is playing" not in reply.lower()


# ── the fastpath declines what it should ─────────────────────────────────────

@pytest.mark.parametrize("said", PRIMARY_PHRASINGS)
def test_dispatcher_claims_the_primary_phrasings(said, silence, monkeypatch):
    """End-to-end through _try_instant_answer, the real dispatcher: no earlier
    fastpath may swallow the question and no later route may reach RAG."""
    monkeypatch.setattr("puffpai.radio_player.is_playing", lambda: True)
    monkeypatch.setattr(np, "_radio_station_name",
                        lambda _ad: "Adroit Jazz Underground")
    reply = api._try_instant_answer(said)
    assert reply == "Adroit Jazz Underground on World Radio."


@pytest.mark.parametrize("said", [
    "what's playing on spotify",
    "what's playing on jellyfin",
    "play some jazz radio",              # a play intent, not a status question
    "what's the weather",
    "what's for dinner",
    "stop the music",
])
def test_fastpath_declines_everything_else(said):
    assert api._try_now_playing(said) is None
