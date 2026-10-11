"""Media hub: the invariants that keep the Now Playing tile from lying.

:mod:`puffpai.media_hub` sits on three paths where a raised exception is worse
than a wrong answer:

* ``normalize_spotify_event`` is fed straight from librespot's ONEVENT hook.
  A 500 there makes librespot retry the same bad payload forever, so the
  function must swallow *every* shape — including ones no sane player sends.
* ``aggregate_sources`` rides along on ``/api/media/now-playing``; a corrupt
  ``mediahub.json`` must not take the Jellyfin payload down with it.
* ``search_stations`` walks a pool of volunteer mirrors that go dark and serve
  malformed rows; one bad row must not kill the LLM tool that called it.

Most cases here came out of an adversarial review of that code, so they are
regression guards rather than decoration. In particular:

* radio staleness needs BOTH freshness signals (the state write *and* the poll
  heartbeat) — either-alone was the bug;
* ``playing`` is a separate axis from ``active`` — a paused Spotify session is
  connected but must not show a live dot;
* a queued radio command past its TTL is expired on poll, not delivered, so a
  lost ack cannot start the radio by itself hours later.

No network and no real ``app_data``: every case that writes points
``$PUFFPAI_DATA_DIR`` at ``tmp_path`` first, and ``requests.get`` is patched
out. Timestamps are constructed, never slept for.

Run: py -3 -m pytest tests/test_media_hub.py -q
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from puffpai import app_data as app_data_mod
from puffpai import media_hub as mh

_ENV = "PUFFPAI_DATA_DIR"


@pytest.fixture(autouse=True)
def _fresh_radio_heartbeat(monkeypatch):
    """Clear the in-memory poll heartbeat around every case.

    ``_radio_last_seen_iso`` is a module global deliberately kept out of the
    JSON doc (it ticks every 4s for the appliance's lifetime). Left set, one
    test's ``touch_radio_last_seen()`` silently makes a later staleness case
    pass for the wrong reason.
    """
    monkeypatch.setattr(mh, "_radio_last_seen_iso", None)


@pytest.fixture(autouse=True)
def _no_docker_radio(monkeypatch):
    """Keep every case off docker.

    A radio STOP request also removes the server-side
    ``puffpai-radio`` container, so an unguarded case here would shell out to
    docker on the developer's machine (and block on its timeout when the
    daemon is down). Requested by name where a case also wants to be explicit.
    """
    from puffpai import radio_player
    monkeypatch.setattr(radio_player, "stop", lambda: True)
    monkeypatch.setattr(radio_player, "is_playing", lambda: False)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """An :class:`AppData` whose JSON files live in a throwaway directory."""
    monkeypatch.setenv(_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    return app_data_mod.AppData()


def _iso_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class _FakeAppData:
    """Minimal get_data stand-in for aggregate_sources' corrupt-doc cases."""

    def __init__(self, doc=None, raises=False):
        self._doc = doc
        self._raises = raises

    def get_data(self, app):
        if self._raises:
            raise RuntimeError("mediahub.json is unreadable")
        return self._doc


# ══════════════════════════════════════════════════════════════════════════════
#  normalize_spotify_event — must never raise
# ══════════════════════════════════════════════════════════════════════════════

class TestSpotifyEventNeverRaises:
    """librespot retries a payload that 500s, forever. Nothing may escape."""

    @pytest.mark.parametrize("payload", [
        None,
        "",
        "just a string",
        [],
        ["a", "b"],
        42,
        0,
        {},
        {"event": None},
        {1: "x", (2, 3): "y", None: "z"},          # non-string keys
        {"event": "track_changed", "name": {"nested": "title"}},
        {"event": "track_changed", "artists": [{"name": "A"}, {"name": "B"}]},
    ], ids=["none", "empty-str", "str", "empty-list", "list", "int", "zero",
            "empty-dict", "null-event", "nonstr-keys", "dict-title",
            "list-of-dict-artists"])
    def test_hostile_shapes_return_a_state_dict(self, payload):
        state = mh.normalize_spotify_event(payload)
        assert set(state) >= {"active", "playing", "user", "track",
                              "last_event", "updated_at", "debug"}

    def test_a_2000_deep_dict_does_not_escape(self):
        deep = {}
        cursor = deep
        for _ in range(2000):
            cursor["n"] = {}
            cursor = cursor["n"]
        state = mh.normalize_spotify_event({"event": "playing", "meta": deep})
        assert state["last_event"] == "playing"

    def test_a_200kb_string_value_does_not_escape(self):
        state = mh.normalize_spotify_event(
            {"event": "track_changed", "name": "x" * 200_000})
        assert state["track"]["title"] == "x" * 200_000

    def test_a_self_referential_dict_does_not_escape(self):
        """json.dumps has a ``default=str`` fallback, but a circular reference
        defeats it — the bare ``except`` around the debug capture is what keeps
        this from becoming a 500."""
        payload = {}
        payload["x"] = payload
        state = mh.normalize_spotify_event(payload)
        assert isinstance(state["debug"]["last_raw"], str)

    def test_a_non_dict_previous_is_ignored(self):
        state = mh.normalize_spotify_event({"event": "playing"},
                                           previous="not a dict")
        assert state["track"] is None


class TestSpotifyArtistShapes:
    """The real librespot shapes: a list, or one newline-joined string."""

    def test_artist_list_joins_with_commas(self):
        state = mh.normalize_spotify_event(
            {"event": "track_changed", "name": "Song", "artists": ["A", "B"]})
        assert state["track"]["artists"] == "A, B"

    def test_newline_joined_artists_normalize_identically(self):
        state = mh.normalize_spotify_event(
            {"event": "track_changed", "name": "Song", "artists": "A\nB"})
        assert state["track"]["artists"] == "A, B"

    def test_a_single_artist_survives_unchanged(self):
        state = mh.normalize_spotify_event(
            {"event": "track_changed", "name": "Song", "artists": "Solo Act"})
        assert state["track"]["artists"] == "Solo Act"


class TestSpotifyKeyAliasing:
    """The ONEVENT hook shape has changed between librespot releases."""

    @pytest.mark.parametrize("key", ["PLAYER_EVENT", "player_event", "event",
                                     "event_name", "type"])
    def test_event_key_aliases(self, key):
        assert mh.normalize_spotify_event({key: "paused"})["last_event"] == "paused"

    @pytest.mark.parametrize("key", ["NAME", "name", "title", "track_name",
                                     "item_name"])
    def test_title_key_aliases(self, key):
        state = mh.normalize_spotify_event({"event": "track_changed",
                                            key: "Blue in Green"})
        assert state["track"]["title"] == "Blue in Green"

    def test_event_values_are_case_insensitive(self):
        state = mh.normalize_spotify_event({"PLAYER_EVENT": "Session_Disconnected"})
        assert state["last_event"] == "session_disconnected"
        assert state["active"] is False

    def test_user_key_aliases(self):
        state = mh.normalize_spotify_event({"event": "playing",
                                            "USER_NAME": "alex"})
        assert state["user"] == "alex"


class TestSpotifyCarryForward:
    """Track metadata only arrives on track_changed."""

    @staticmethod
    def _playing_track():
        return mh.normalize_spotify_event({
            "PLAYER_EVENT": "track_changed", "NAME": "Blue in Green",
            "ARTISTS": "Miles Davis\nBill Evans", "album": "Kind of Blue",
            "uri": "spotify:track:abc", "user_name": "alex",
        })

    def test_a_bare_playing_event_keeps_the_previous_track(self):
        prev = self._playing_track()
        state = mh.normalize_spotify_event({"event": "playing"}, previous=prev)
        assert state["track"] == prev["track"]
        assert state["user"] == "alex"

    def test_a_volume_change_keeps_the_track_and_the_active_flag(self):
        prev = self._playing_track()
        state = mh.normalize_spotify_event({"event": "volume_changed"},
                                           previous=prev)
        assert state["track"] == prev["track"]
        assert state["active"] is True

    def test_session_disconnected_blanks_track_and_user(self):
        prev = self._playing_track()
        state = mh.normalize_spotify_event({"event": "session_disconnected"},
                                           previous=prev)
        assert state["track"] is None
        assert state["user"] is None
        assert state["active"] is False


class TestSpotifyPlayingIsSeparateFromActive:
    """A paused session is connected but not audible — two axes, not one."""

    def test_paused_is_active_but_not_playing(self):
        state = mh.normalize_spotify_event({"event": "paused"})
        assert state["active"] is True
        assert state["playing"] is False

    def test_track_changed_is_both(self):
        state = mh.normalize_spotify_event({"event": "track_changed",
                                            "name": "Song"})
        assert state["active"] is True
        assert state["playing"] is True

    def test_stopped_is_neither(self):
        state = mh.normalize_spotify_event({"event": "stopped"})
        assert state["active"] is False
        assert state["playing"] is False

    @pytest.mark.parametrize("was_playing", [True, False])
    def test_an_unlisted_event_inherits_the_previous_playing_flag(self, was_playing):
        prev = {"active": True, "playing": was_playing, "track": None}
        state = mh.normalize_spotify_event({"event": "some_future_event"},
                                           previous=prev)
        assert state["playing"] is was_playing


class TestSpotifyDebugCapture:
    def test_last_raw_is_capped(self):
        state = mh.normalize_spotify_event({"event": "playing",
                                            "blob": "y" * 100_000})
        assert len(state["debug"]["last_raw"]) == mh.MAX_DEBUG_BYTES


# ══════════════════════════════════════════════════════════════════════════════
#  aggregate_sources — never raises, always the same key set
# ══════════════════════════════════════════════════════════════════════════════

_SOURCE_KEYS = {"radio", "radio_display", "spotify",
                "jellyfin"}


class TestAggregateNeverRaises:
    @pytest.mark.parametrize("doc", [
        None, "not a dict", 7, [], ["radio"],
        {"radio": "corrupt"}, {"radio": [1, 2]}, {"spotify": "x"},
        {"radio": {"status": "playing", "updated_at": "not-a-timestamp"}},
        {"radio_last_seen": 12345},
    ], ids=["none", "str", "int", "list", "list-of-str", "radio-str",
            "radio-list", "spotify-str", "bad-timestamp", "bad-last-seen"])
    def test_corrupt_docs_still_yield_every_source(self, doc):
        out = mh.aggregate_sources(_FakeAppData(doc))
        assert set(out) == _SOURCE_KEYS

    def test_a_raising_app_data_reads_as_an_empty_doc(self):
        """The inner try around get_data absorbs the read failure, so this
        lands on the normal path with an empty doc rather than the outer
        catch-all — either way, every key present and nothing playing."""
        out = mh.aggregate_sources(_FakeAppData(raises=True))
        assert set(out) == _SOURCE_KEYS
        assert out["radio"] == {"status": "idle", "station": None,
                                "stale": False}

    @pytest.mark.parametrize("doc", [None, {}, {"radio": "corrupt"},
                                     {"radio": [1, 2]}])
    def test_radio_always_carries_status_and_station(self, doc):
        """Every path returns one shape. A consumer keying off
        radio["status"] must not get a KeyError because the doc was
        missing or corrupt on this particular call."""
        radio = mh.aggregate_sources(_FakeAppData(doc))["radio"]
        assert radio["status"] == "idle"
        assert radio["station"] is None
        assert radio["stale"] is False

    @pytest.mark.parametrize("doc", [None, {}, {"spotify": "corrupt"}])
    def test_spotify_always_carries_its_flags(self, doc):
        spotify = mh.aggregate_sources(_FakeAppData(doc))["spotify"]
        assert spotify["active"] is False
        assert spotify["playing"] is False
        assert spotify["track"] is None

    def test_a_non_list_jellyfin_argument_counts_zero(self):
        out = mh.aggregate_sources(_FakeAppData({}), jellyfin_sessions="oops")
        assert out["jellyfin"]["session_count"] == 0

    def test_jellyfin_sessions_are_counted(self):
        out = mh.aggregate_sources(_FakeAppData({}), jellyfin_sessions=[1, 2, 3])
        assert out["jellyfin"]["session_count"] == 3


class TestAggregateRadioStaleness:
    """BOTH freshness signals are required — either-alone was the bug.

    The heartbeat alone only proves the display process is alive (a reloaded
    kiosk polls immediately but starts idle); the state write alone goes silent
    when the kiosk dies mid-stream.
    """

    @staticmethod
    def _doc(status, state_age, seen_age=None):
        doc = {"radio": {"status": status, "station": {"name": "KEXP"},
                         "updated_at": _iso_ago(state_age)}}
        if seen_age is not None:
            doc["radio_last_seen"] = _iso_ago(seen_age)
        return doc

    @pytest.mark.parametrize("status", ["playing", "connecting", "paused"])
    def test_both_signals_fresh_is_not_stale(self, status):
        out = mh.aggregate_sources(_FakeAppData(self._doc(status, 5, seen_age=5)))
        assert out["radio"]["stale"] is False

    @pytest.mark.parametrize("status", ["playing", "connecting", "paused"])
    def test_an_old_state_write_is_stale_even_with_a_live_heartbeat(self, status):
        doc = self._doc(status, mh.RADIO_STALE_SECONDS + 30, seen_age=1)
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is True

    @pytest.mark.parametrize("status", ["playing", "connecting", "paused"])
    def test_a_missing_heartbeat_is_stale_even_with_a_fresh_state_write(self, status):
        doc = self._doc(status, 1)  # no radio_last_seen, module global cleared
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is True

    def test_an_old_heartbeat_is_stale_even_with_a_fresh_state_write(self):
        doc = self._doc("playing", 1, seen_age=mh.RADIO_STALE_SECONDS + 30)
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is True

    def test_a_missing_updated_at_is_stale(self):
        doc = {"radio": {"status": "playing"},
               "radio_last_seen": _iso_ago(1)}
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is True

    @pytest.mark.parametrize("seen_age", [None, 1, 9999])
    def test_idle_is_never_stale(self, seen_age):
        doc = self._doc("idle", 9999, seen_age=seen_age)
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is False

    def test_the_in_memory_heartbeat_satisfies_the_second_signal(self):
        """touch_radio_last_seen() is the hot path: the poll heartbeat is kept
        out of the JSON doc so a 4s tick does not rewrite flash."""
        doc = self._doc("playing", 1)  # no persisted radio_last_seen at all
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is True

        mh.touch_radio_last_seen()
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is False

    def test_the_persisted_field_is_the_cold_start_fallback(self):
        """A freshly restarted backend has no in-memory heartbeat yet."""
        assert mh._radio_last_seen_iso is None
        doc = self._doc("playing", 1, seen_age=2)
        assert mh.aggregate_sources(_FakeAppData(doc))["radio"]["stale"] is False

    def test_the_source_doc_is_not_mutated(self):
        doc = self._doc("playing", 1, seen_age=1)
        mh.aggregate_sources(_FakeAppData(doc))
        assert "stale" not in doc["radio"]


class TestAggregateSpotifyProjection:
    def test_the_raw_debug_payload_is_stripped(self):
        doc = {"spotify": {"active": True, "playing": True,
                           "debug": {"last_raw": "{...}"}}}
        out = mh.aggregate_sources(_FakeAppData(doc))
        assert "debug" not in out["spotify"]
        assert out["spotify"]["active"] is True

    def test_stripping_does_not_mutate_the_stored_doc(self):
        doc = {"spotify": {"active": True, "debug": {"last_raw": "{...}"}}}
        mh.aggregate_sources(_FakeAppData(doc))
        assert "debug" in doc["spotify"]

    def test_an_unreadable_doc_yields_no_debug_key_either(self):
        out = mh.aggregate_sources(_FakeAppData(raises=True))
        assert "debug" not in out["spotify"]


# ══════════════════════════════════════════════════════════════════════════════
#  search_stations — mirrors are unreliable and must never reach the network here
# ══════════════════════════════════════════════════════════════════════════════

class _FakeResponse:
    #: The egress-door wrapper follows redirects itself, so it reads these two
    #: before it reads the body.
    status_code = 200
    headers: dict = {}

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _station(**over):
    row = {"stationuuid": "u1", "name": "KEXP", "url_resolved": "https://s/1",
           "country": "United States", "countrycode": "US", "codec": "MP3",
           "bitrate": 128, "tags": "indie", "votes": 900,
           "homepage": "https://kexp.org"}
    row.update(over)
    return row


@pytest.fixture()
def fake_get(monkeypatch):
    """Replace the outbound call and record every one of them.

    The catalog search goes through
    ``security.egress_doors.egress_get``, which (a) refuses unless the
    ``radio_browser`` door is open and (b) reaches the wire via
    ``requests.request(method, url, ...)`` rather than ``requests.get``. So
    this fixture now opens that door for the duration of the test and patches
    both entry points. Tests asserting on ``calls`` still see ``(url, kwargs)``.

    Opening the door here is the point of the door, not a way around it:
    ``TestRadioDoor`` below covers the shut case.
    """
    from puffpai.security import egress_doors as _ed

    monkeypatch.setattr(
        _ed, "_raw_config",
        lambda: {"egress_doors": {"radio_browser": True}})
    monkeypatch.delenv(_ed.door("radio_browser").env_var, raising=False)
    _ed.reset_cache()

    calls = []

    class Recorder:
        responses = []

        def __call__(self, url, **kwargs):
            calls.append((url, kwargs))
            if not self.responses:
                raise AssertionError("unexpected extra HTTP call")
            outcome = self.responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(outcome)

    recorder = Recorder()
    recorder.calls = calls
    monkeypatch.setattr(requests, "get", recorder)
    monkeypatch.setattr(
        requests, "request",
        lambda _method, url, **kwargs: recorder(url, **kwargs))

    # Since the door layer resolves and PINS the address, egress_request
    # goes through a session it builds rather than ``requests.request``, so the
    # patch above no longer intercepts it. The transport is stubbed here; the
    # pin has its own tests in test_security_hostname_not_destination.py.
    class _Session:
        def request(self, _method, url, **kwargs):
            return recorder(url, **kwargs)

        def close(self):
            return None

    monkeypatch.setattr(_ed, "_pinned_session", lambda d: _Session())
    return recorder


class TestSearchStations:
    def test_an_empty_query_never_touches_the_network(self, fake_get):
        assert mh.search_stations("") == []
        assert mh.search_stations(None) == []
        assert mh.search_stations("   ") == []
        assert fake_get.calls == []

    def test_a_dead_first_mirror_falls_through_to_the_second(self, fake_get):
        fake_get.responses = [requests.RequestException("mirror down"),
                              [_station()]]
        out = mh.search_stations("kexp")
        assert [s["name"] for s in out] == ["KEXP"]
        assert len(fake_get.calls) == 2
        assert mh._RADIO_MIRRORS[1] in fake_get.calls[1][0]

    def test_a_non_list_body_is_skipped_not_returned(self, fake_get):
        fake_get.responses = [{"error": "rate limited"}, [_station()]]
        assert [s["name"] for s in mh.search_stations("kexp")] == ["KEXP"]

    def test_every_mirror_failing_returns_an_empty_list(self, fake_get):
        fake_get.responses = [requests.RequestException("down"),
                              requests.RequestException("down too")]
        assert mh.search_stations("kexp") == []

    @pytest.mark.parametrize("hls,kept", [
        ("1", False), (1, False), (True, False),
        ("true", True), ("yes", True), (None, True), (0, True), ("0", True),
    ])
    def test_hls_rows_are_filtered_without_a_type_error(self, fake_get, hls, kept):
        """Mirrors have served hls as "true"/"1"/1. A non-numeric value must
        not throw the whole search — and the LLM tool — on the floor."""
        fake_get.responses = [[_station(hls=hls)]]
        assert bool(mh.search_stations("kexp")) is kept

    def test_rows_with_no_resolved_url_are_dropped(self, fake_get):
        fake_get.responses = [[_station(url_resolved=""),
                               _station(url_resolved=None),
                               _station(name="Good")]]
        assert [s["name"] for s in mh.search_stations("kexp")] == ["Good"]

    def test_non_dict_rows_are_dropped(self, fake_get):
        fake_get.responses = [[None, "junk", 5, _station()]]
        assert len(mh.search_stations("kexp")) == 1

    def test_only_the_player_relevant_fields_survive(self, fake_get):
        fake_get.responses = [[_station(clickcount=99, lastcheckok=1)]]
        row = mh.search_stations("kexp")[0]
        assert set(row) == set(mh._STATION_FIELDS)
        assert row["bitrate"] == 128 and row["votes"] == 900

    def test_unparseable_numeric_fields_become_zero(self, fake_get):
        fake_get.responses = [[_station(bitrate="high", votes=None)]]
        row = mh.search_stations("kexp")[0]
        assert row["bitrate"] == 0 and row["votes"] == 0

    @pytest.mark.parametrize("limit,expected", [
        ("lots", 20), (None, 20), (0, 1), (-5, 1), (999, 50), ("7", 7), (7, 7),
    ])
    def test_a_bad_limit_is_coerced_instead_of_raising(self, fake_get, limit,
                                                        expected):
        fake_get.responses = [[]]
        mh.search_stations("kexp", limit=limit)
        assert fake_get.calls[0][1]["params"]["limit"] == expected


# ══════════════════════════════════════════════════════════════════════════════
#  AppData mediahub methods
# ══════════════════════════════════════════════════════════════════════════════

class TestReportRadio:
    @pytest.mark.parametrize("status", mh.VALID_RADIO_STATUS)
    def test_every_valid_status_is_accepted(self, app, status):
        assert app.mediahub_report_radio(status, source="appliance")["ok"] is True
        assert app.get_data("mediahub")["radio"]["status"] == status

    @pytest.mark.parametrize("status", ["bogus", "", None, 7, "play"])
    def test_a_bad_status_is_rejected(self, app, status):
        with pytest.raises(ValueError):
            app.mediahub_report_radio(status)

    def test_status_is_normalised_before_validation(self, app):
        app.mediahub_report_radio("  PLAYING ", source="appliance")
        assert app.get_data("mediahub")["radio"]["status"] == "playing"

    def test_a_report_refreshes_the_persisted_heartbeat(self, app):
        app.mediahub_report_radio("playing")
        assert app.get_data("mediahub")["radio_last_seen"]

    def test_a_station_without_a_url_is_rejected(self, app):
        with pytest.raises(ValueError):
            app.mediahub_report_radio("playing", station={"name": "KEXP"})

    def test_a_non_http_station_url_is_rejected(self, app):
        with pytest.raises(ValueError):
            app.mediahub_report_radio(
                "playing", station={"name": "x", "url_resolved": "file:///etc/passwd"})


class TestRadioRequest:
    def test_play_requires_a_station(self, app):
        with pytest.raises(ValueError):
            app.mediahub_radio_request("play")

    def test_stop_needs_no_station(self, app):
        command = app.mediahub_radio_request("stop")
        assert command["action"] == "stop" and command["station"] is None

    def test_an_unknown_action_is_rejected(self, app):
        with pytest.raises(ValueError):
            app.mediahub_radio_request("skip")

    def test_pause_is_a_synonym_for_stop(self, app, _no_docker_radio):
        """A live stream has no pause, and the dashboard's
        Pause button must still make the appliance shut up."""
        assert app.mediahub_radio_request("pause")["action"] == "stop"

    def test_each_command_gets_a_unique_id(self, app):
        first = app.mediahub_radio_request("stop")["id"]
        second = app.mediahub_radio_request("stop")["id"]
        assert first and second and first != second

    def test_play_sets_the_optimistic_connecting_status(self, app):
        app.mediahub_radio_request("play", station=_station())
        radio = app.get_data("mediahub")["radio"]
        assert radio["status"] == "connecting"
        assert radio["station"]["name"] == "KEXP"

    def test_stop_reports_the_appliance_idle(self, app, _no_docker_radio):
        """A stop request kills the mpv container, so "playing" would be a
        lie if the reported status were left alone.
        """
        app.mediahub_report_radio("playing", source="appliance")
        app.mediahub_radio_request("stop")
        assert app.get_data("mediahub")["radio"]["status"] == "idle"

    def test_a_second_request_supersedes_the_first(self, app):
        app.mediahub_radio_request("play", station=_station())
        second = app.mediahub_radio_request("stop")
        assert app.get_data("mediahub")["radio_command"]["id"] == second["id"]


class TestPollCommand:
    def test_a_fresh_command_is_delivered_and_left_queued(self, app):
        queued = app.mediahub_radio_request("play", station=_station())
        polled = app.mediahub_poll_command()
        assert polled["id"] == queued["id"]
        # Not cleared: the player acks once the stream opens, so a poll racing
        # a browser reload must not drop the request on the floor.
        assert app.get_data("mediahub")["radio_command"]["id"] == queued["id"]

    def test_an_expired_command_is_dropped_not_delivered(self, app):
        """A lost ack must not start the radio by itself at the next reload."""
        app.mediahub_radio_request("play", station=_station())
        doc = app.get_data("mediahub")
        doc["radio_command"]["issued_at"] = _iso_ago(
            mh.RADIO_COMMAND_TTL_SECONDS + 60)
        app.set_data("mediahub", doc)

        assert app.mediahub_poll_command() is None
        assert app.get_data("mediahub")["radio_command"] is None

    def test_a_command_with_an_unparseable_issued_at_is_dropped(self, app):
        doc = app.get_data("mediahub")
        doc["radio_command"] = {"id": "x1", "action": "play",
                                "issued_at": "whenever"}
        app.set_data("mediahub", doc)
        assert app.mediahub_poll_command() is None

    @pytest.mark.parametrize("junk", ["corrupt", 42, ["play"], {"id": "x"}])
    def test_a_corrupt_command_slot_does_not_raise(self, app, junk):
        doc = app.get_data("mediahub")
        doc["radio_command"] = junk
        app.set_data("mediahub", doc)
        assert app.mediahub_poll_command() is None

    def test_an_empty_queue_polls_none(self, app):
        assert app.mediahub_poll_command() is None

    def test_polling_refreshes_the_in_memory_heartbeat(self, app):
        assert mh._radio_last_seen_iso is None
        app.mediahub_poll_command()
        assert mh._radio_last_seen_iso is not None
        assert mh.radio_display_online(app) is True


class TestAckCommand:
    def test_a_matching_id_clears_the_command(self, app):
        queued = app.mediahub_radio_request("play", station=_station())
        result = app.mediahub_ack_command(queued["id"], "playing",
                                          station=_station())
        assert result["cleared"] is True
        assert app.get_data("mediahub")["radio_command"] is None
        # An ack is a DISPLAY report and lands in its own slot.
        assert app.get_data("mediahub")["radio_display"]["status"] == "playing"

    def test_a_stale_ack_does_not_clear_a_newer_command(self, app):
        stale = app.mediahub_radio_request("stop")
        current = app.mediahub_radio_request("play", station=_station())

        result = app.mediahub_ack_command(stale["id"], "idle")
        assert result["cleared"] is False
        assert app.get_data("mediahub")["radio_command"]["id"] == current["id"]

    @pytest.mark.parametrize("cmd_id", ["", None, "   "])
    def test_an_empty_id_never_clears(self, app, cmd_id):
        queued = app.mediahub_radio_request("stop")
        assert app.mediahub_ack_command(cmd_id, "idle")["cleared"] is False
        assert app.get_data("mediahub")["radio_command"]["id"] == queued["id"]

    def test_acking_with_no_command_queued_is_harmless(self, app):
        assert app.mediahub_ack_command("ghost", "idle")["cleared"] is False

    def test_a_corrupt_command_slot_does_not_raise(self, app):
        doc = app.get_data("mediahub")
        doc["radio_command"] = "corrupt"
        app.set_data("mediahub", doc)
        assert app.mediahub_ack_command("x1", "idle")["cleared"] is False

    def test_a_bad_status_is_rejected(self, app):
        queued = app.mediahub_radio_request("stop")
        with pytest.raises(ValueError):
            app.mediahub_ack_command(queued["id"], "exploded")
        # ...and the rejection must not have eaten the command.
        assert app.get_data("mediahub")["radio_command"]["id"] == queued["id"]


class TestSpotifyEventPersistence:
    def test_an_oversized_payload_is_rejected(self, app):
        with pytest.raises(ValueError, match="too large"):
            app.mediahub_spotify_event({"event": "track_changed",
                                        "name": "x" * 20_000})

    @pytest.mark.parametrize("payload", [None, "", [], 42, object(),
                                         {"blob": {1, 2, 3}},
                                         {"event": None}], ids=[
        "none", "empty-str", "list", "int", "object", "set-value", "null-event"])
    def test_garbage_payloads_are_stored_without_raising(self, app, payload):
        assert app.mediahub_spotify_event(payload)["ok"] is True
        assert "spotify" in app.get_data("mediahub")

    def test_the_normalised_state_lands_in_the_doc(self, app):
        app.mediahub_spotify_event({
            "PLAYER_EVENT": "track_changed", "NAME": "Blue in Green",
            "ARTISTS": "Miles Davis\nBill Evans", "user_name": "alex"})
        spotify = app.get_data("mediahub")["spotify"]
        assert spotify["active"] is True and spotify["playing"] is True
        assert spotify["track"]["artists"] == "Miles Davis, Bill Evans"
        assert spotify["user"] == "alex"

    def test_a_later_bare_event_carries_the_track_through_the_doc(self, app):
        app.mediahub_spotify_event({"event": "track_changed", "name": "Song"})
        app.mediahub_spotify_event({"event": "paused"})
        spotify = app.get_data("mediahub")["spotify"]
        assert spotify["track"]["title"] == "Song"
        assert spotify["playing"] is False and spotify["active"] is True

    def test_the_stored_doc_stays_json_serialisable(self, app):
        app.mediahub_spotify_event({"event": "playing", "blob": {1, 2, 3}})
        json.dumps(app.get_data("mediahub"))


# ══════════════════════════════════════════════════════════════════════════════
#  The two halves agreeing
# ══════════════════════════════════════════════════════════════════════════════

def test_a_real_report_plus_a_poll_reads_back_as_fresh(app, monkeypatch):
    """End to end over the real doc: the report writes one signal, the poll
    supplies the other, and only then is the tile allowed to say 'playing'."""
    monkeypatch.setattr(mh, "_appliance_radio_live", lambda: False)
    app.mediahub_report_radio("playing", station=_station(), source="appliance")
    assert mh.aggregate_sources(app)["radio"]["stale"] is False, \
        "the report persists radio_last_seen, so a cold backend is not stale"

    mh._radio_last_seen_iso = None
    doc = app.get_data("mediahub")
    doc["radio_last_seen"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
    app.set_data("mediahub", doc)
    assert mh.aggregate_sources(app)["radio"]["stale"] is True

    app.mediahub_poll_command()
    assert mh.aggregate_sources(app)["radio"]["stale"] is False


class TestRadioDoor:
    """The radio catalog is an egress door, shut by default.

    ``all.api.radio-browser.info`` is a public WAN host reached with the
    station or genre the household asked for, and it must go through the
    door registry, never with no gate at all. Two properties matter, and
    they pull in opposite directions, so both are pinned here:

    * with the door shut, nothing is dialled; and
    * the person is told *which switch is off* rather than "no station
      matched", which is a true-sounding sentence about the wrong thing.
    """

    @pytest.fixture()
    def shut_door(self, monkeypatch):
        from puffpai.security import egress_doors as _ed
        monkeypatch.setattr(_ed, "_raw_config", lambda: {"egress_doors": {}})
        monkeypatch.delenv(_ed.door("radio_browser").env_var, raising=False)
        _ed.reset_cache()

        def boom(*_a, **_kw):
            raise AssertionError("a socket was opened with the door shut")

        monkeypatch.setattr(requests, "get", boom)
        monkeypatch.setattr(requests, "request", boom)
        return _ed

    def test_the_door_ships_shut(self, shut_door):
        assert shut_door.egress_allowed("radio_browser")[0] is False

    def test_a_shut_door_dials_nothing_and_returns_no_stations(self, shut_door):
        assert mh.search_stations("kexp") == []
        assert mh.search_stations_by_tag("jazz") == []

    def test_the_spoken_reply_names_the_door_not_a_missing_station(
            self, shut_door, tmp_path, monkeypatch):
        """"Hey Puff, play some jazz radio" with the switch off."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        reply = mh.tool_radio_play("jazz")
        assert "radio-browser door" in reply
        assert "off" in reply
        # The failure mode this replaces: blaming the catalog for a shut door.
        assert "No internet radio station matched" not in reply
