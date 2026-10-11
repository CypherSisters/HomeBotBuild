"""A station started server-side must reach the Now Playing tile.

The bug: ``_radio_last_seen`` preferred the in-process poll marker over the
persisted ``radio_last_seen`` unconditionally. A voice/chat play
(``_try_tunein_radio`` -> ``_radio_play_serverside`` -> ``mediahub_report_radio``)
writes a fresh persisted stamp but runs no kiosk poll, so a module marker left
over from an earlier poll aged past 45s made ``aggregate_sources`` call the
brand-new station "stale" and the tile hid the row. Starting the same station
from the dashboard worked, because that path touched the marker.

Four structural fixes, all tested here:

* ``media_hub._radio_last_seen`` now returns the NEWER of the two signals.
* ``AppData.mediahub_report_radio`` touches the in-process marker itself, so
  every server-side writer refreshes the heartbeat at the source rather than
  at one call site, and records WHO is playing (``source``).
* Appliance-owned state that ages past the gate is verified against the mpv
  container instead of hidden: a voice play reports once and nothing
  re-reports it, so the row used to vanish at 45s mid-stream.
* ``tool_radio_stop`` reports ``idle`` itself instead of only queuing a kiosk
  ack, so a voice stop clears the row immediately.

Nothing about the 45s thresholds or the tile changed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from puffpai import app_data as app_data_mod
from puffpai import media_hub as mh

_ENV = "PUFFPAI_DATA_DIR"


@pytest.fixture(autouse=True)
def _fresh_radio_heartbeat(monkeypatch):
    """Same discipline as tests/test_media_hub.py: the module global is not
    test-local, so one case's touch must not decide the next one."""
    monkeypatch.setattr(mh, "_radio_last_seen_iso", None)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    return app_data_mod.AppData()


def _iso_ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _station(name="KEXP"):
    return {"name": name, "url_resolved": "https://stream.example/kexp",
            "stationuuid": "uuid-1", "codec": "MP3", "bitrate": 128}


# ══════════════════════════════════════════════════════════════════════════════
# 1 — the reported bug, end to end over the real doc
# ══════════════════════════════════════════════════════════════════════════════

class TestServerSidePlayReachesTheTile:
    """The exact failure: a viewer that stopped polling 5 minutes ago."""

    def test_a_stale_in_process_heartbeat_no_longer_hides_a_fresh_play(
            self, app, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))

        app.mediahub_report_radio("playing", station=_station(),
                                  source="appliance")

        radio = mh.aggregate_sources(app)["radio"]
        assert radio["stale"] is False, \
            "the voice-started station must not be gated out as stale"
        assert radio["status"] == "playing"
        assert radio["station"]["name"] == "KEXP"

    def test_the_report_itself_refreshes_the_in_process_heartbeat(
            self, app, monkeypatch):
        """Belt and braces: even a reader that ignored the persisted field
        would now see a fresh marker, because the writer touched it."""
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))

        app.mediahub_report_radio("playing", station=_station())

        assert mh._age_seconds(mh._radio_last_seen_iso) < 5

    def test_it_is_still_fresh_a_minute_later_without_any_poll(
            self, app, monkeypatch):
        """The bug's window: the tile re-polls at 60s and nothing else has
        happened. Both signals are stamped at play time, so the row survives
        only as long as the 20s re-report keeps them fresh — here we assert the
        gate is not tripped by the *heartbeat* signal alone."""
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))
        app.mediahub_report_radio("playing", station=_station())

        doc = app.get_data("mediahub")
        assert mh._age_seconds(doc["radio_last_seen"]) < 5
        assert mh._age_seconds(mh._radio_last_seen(doc)) < 5


# ══════════════════════════════════════════════════════════════════════════════
# 2 — _radio_last_seen picks the newer signal, in both orders
# ══════════════════════════════════════════════════════════════════════════════

class TestNewestSignalWins:
    def test_a_fresh_file_beats_a_stale_in_process_value(self, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))
        picked = mh._radio_last_seen({"radio_last_seen": _iso_ago(2)})
        assert mh._age_seconds(picked) < 5

    def test_a_fresh_in_process_value_beats_a_stale_file(self, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(2))
        picked = mh._radio_last_seen({"radio_last_seen": _iso_ago(300)})
        assert mh._age_seconds(picked) < 5

    def test_no_signal_at_all_is_none(self):
        assert mh._radio_last_seen({}) is None
        assert mh._radio_last_seen(None) is None
        assert mh._radio_last_seen("not a doc") is None
        assert mh._radio_last_seen({"radio_last_seen": None}) is None

    def test_only_one_signal_present_is_returned_unchanged(self, monkeypatch):
        assert mh._radio_last_seen({"radio_last_seen": "2026-09-11T00:00:00+00:00"}) \
            == "2026-09-11T00:00:00+00:00"
        monkeypatch.setattr(mh, "_radio_last_seen_iso", "2026-09-11T01:00:00+00:00")
        assert mh._radio_last_seen({}) == "2026-09-11T01:00:00+00:00"

    def test_an_unparseable_file_stamp_does_not_beat_a_real_one(self, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(2))
        picked = mh._radio_last_seen({"radio_last_seen": "yesterday-ish"})
        assert mh._age_seconds(picked) < 5

    def test_an_unparseable_in_process_stamp_does_not_beat_a_real_one(
            self, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", "sometime")
        picked = mh._radio_last_seen({"radio_last_seen": _iso_ago(2)})
        assert mh._age_seconds(picked) < 5

    def test_two_unparseable_stamps_never_raise(self, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", "sometime")
        assert mh._age_seconds(mh._radio_last_seen({"radio_last_seen": "??"})) is None

    def test_a_garbage_stamp_still_reads_as_stale_through_the_aggregate(
            self, app, monkeypatch):
        """Unparseable is not fresh: the gate must still fire."""
        monkeypatch.setattr(mh, "_radio_last_seen_iso", "sometime")
        monkeypatch.setattr(mh, "_appliance_radio_live", lambda: False)
        app.mediahub_report_radio("playing", station=_station(),
                                  source="appliance")
        doc = app.get_data("mediahub")
        doc["radio_last_seen"] = "also nonsense"
        app.set_data("mediahub", doc)
        monkeypatch.setattr(mh, "_radio_last_seen_iso", "sometime")
        assert mh.aggregate_sources(app)["radio"]["stale"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 3 — stop clears the tile-facing state without waiting for a kiosk ack
# ══════════════════════════════════════════════════════════════════════════════

class TestStop:
    def test_reporting_idle_takes_the_row_off_the_tile(self, app, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))
        app.mediahub_report_radio("playing", station=_station(),
                                  source="appliance")
        assert mh.aggregate_sources(app)["radio"]["status"] == "playing"

        app.mediahub_report_radio("idle", source="appliance")

        radio = mh.aggregate_sources(app)["radio"]
        assert radio["status"] != "playing"
        assert radio["station"] is None
        assert radio["stale"] is False  # idle is never stale

    def test_the_stop_tool_reports_idle_server_side(self, app, monkeypatch):
        """``tool_radio_stop`` only queued a command for the kiosk; a voice stop
        with no dashboard left the killed station on the tile until it aged
        out."""
        from puffpai import radio_player
        monkeypatch.setattr(radio_player, "stop", lambda *a, **k: (True, ""))

        app.mediahub_report_radio("playing", station=_station(),
                                  source="appliance")
        assert mh.aggregate_sources(app)["radio"]["status"] == "playing"

        out = mh.tool_radio_stop(app)

        assert "Stopping internet radio" in out
        assert mh.aggregate_sources(app)["radio"]["status"] == "idle"

    def test_the_stop_message_still_reflects_the_real_display(
            self, app, monkeypatch):
        """The idle report is written AFTER the message is built, so it must
        not make a dead kiosk look online."""
        from puffpai import radio_player
        monkeypatch.setattr(radio_player, "stop", lambda *a, **k: (True, ""))

        out = mh.tool_radio_stop(app)
        assert "hasn't checked in recently" in out


# ══════════════════════════════════════════════════════════════════════════════
# 4 — the dashboard heartbeat path is unchanged
# ══════════════════════════════════════════════════════════════════════════════

class TestHeartbeatOnlyPathUnchanged:
    def test_a_poll_alone_still_supplies_only_the_second_signal(self, app):
        """A polling kiosk that has reported nothing is still not "playing"."""
        app.mediahub_poll_command()
        assert mh.aggregate_sources(app)["radio"]["status"] == "idle"
        assert mh.aggregate_sources(app)["radio"]["stale"] is False

    def test_an_old_state_write_is_still_stale_under_a_live_poll(
            self, app, monkeypatch):
        monkeypatch.setattr(mh, "_appliance_radio_live", lambda: False)
        app.mediahub_report_radio("playing", station=_station(),
                                  source="appliance")
        doc = app.get_data("mediahub")
        doc["radio"]["updated_at"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
        app.set_data("mediahub", doc)

        app.mediahub_poll_command()  # heartbeat fresh on both signals
        assert mh.aggregate_sources(app)["radio"]["stale"] is True, \
            "a live display does not make a silent stream current"

    def test_both_signals_old_is_stale(self, app, monkeypatch):
        monkeypatch.setattr(mh, "_appliance_radio_live", lambda: False)
        app.mediahub_report_radio("playing", station=_station(),
                                  source="appliance")
        doc = app.get_data("mediahub")
        doc["radio"]["updated_at"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
        doc["radio_last_seen"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
        app.set_data("mediahub", doc)
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))

        assert mh.aggregate_sources(app)["radio"]["stale"] is True

    def test_the_queued_play_request_still_shows_immediately(self, app, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))
        app.mediahub_radio_request("play", station=_station())
        radio = mh.aggregate_sources(app)["radio"]
        assert radio["status"] == "connecting" and radio["stale"] is False

    def test_the_stale_threshold_is_untouched(self):
        assert mh.RADIO_STALE_SECONDS == 45


# ══════════════════════════════════════════════════════════════════════════════
# 5 — the appliance verifies its own stream instead of waiting on a heartbeat
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _fresh_appliance_probe(monkeypatch):
    """The probe caches for 10s; no case may inherit another's answer."""
    monkeypatch.setattr(mh, "_appliance_probe", {"at": None, "live": False})


class TestApplianceOwnedPlaybackSurvivesTheWindow:
    """A voice play reports its state ONCE. Nothing re-reports it the way the
    browser bridge does, so at 45s the state-age half of the gate fired and the
    tile hid a station that was still audibly playing."""

    @staticmethod
    def _aged(app, seconds):
        doc = app.get_data("mediahub")
        doc["radio"]["updated_at"] = _iso_ago(seconds)
        doc["radio_last_seen"] = _iso_ago(seconds)
        app.set_data("mediahub", doc)

    def test_a_live_container_keeps_the_row_past_45_seconds(
            self, app, monkeypatch):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        self._aged(app, mh.RADIO_STALE_SECONDS + 30)
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))
        monkeypatch.setattr(mh, "_appliance_radio_live", lambda: True)

        radio = mh.aggregate_sources(app)["radio"]
        assert radio["stale"] is False
        assert radio["status"] == "playing"

    def test_a_dead_container_still_goes_stale(self, app, monkeypatch):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        self._aged(app, mh.RADIO_STALE_SECONDS + 30)
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))
        monkeypatch.setattr(mh, "_appliance_radio_live", lambda: False)

        assert mh.aggregate_sources(app)["radio"]["stale"] is True

    def test_dashboard_owned_state_never_consults_the_container(
            self, app, monkeypatch):
        """A browser-played station must keep the pure heartbeat gate: the
        appliance container says nothing about what a phone is doing."""
        called = []
        monkeypatch.setattr(
            mh, "_appliance_radio_live", lambda: called.append(1) or True)

        app.mediahub_report_radio("playing", station=_station())  # no source
        doc = app.get_data("mediahub")
        # It lands in its own slot, never the appliance's.
        assert doc["radio_display"]["source"] == "display"
        doc["radio_display"]["updated_at"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
        doc["radio_last_seen"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
        app.set_data("mediahub", doc)
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))

        assert mh.aggregate_sources(app)["radio_display"]["stale"] is True
        assert called == []

    def test_a_fresh_row_never_pays_for_the_probe(self, app, monkeypatch):
        called = []
        monkeypatch.setattr(
            mh, "_appliance_radio_live", lambda: called.append(1) or True)
        app.mediahub_report_radio("playing", station=_station(), source="appliance")

        assert mh.aggregate_sources(app)["radio"]["stale"] is False
        assert called == [], "the docker probe is a fallback, not a per-poll cost"


class TestApplianceProbe:
    def test_it_reports_the_player_and_caches(self, monkeypatch):
        from puffpai import radio_player
        calls = []
        monkeypatch.setattr(
            radio_player, "is_playing", lambda: calls.append(1) or True)

        assert mh._appliance_radio_live() is True
        assert mh._appliance_radio_live() is True
        assert len(calls) == 1, "cached inside the TTL"

    def test_a_broken_docker_reads_as_not_playing_and_never_raises(
            self, monkeypatch):
        from puffpai import radio_player

        def _boom():
            raise RuntimeError("docker: command not found")

        monkeypatch.setattr(radio_player, "is_playing", _boom)
        assert mh._appliance_radio_live() is False

    def test_the_cache_expires(self, monkeypatch):
        from puffpai import radio_player
        calls = []
        monkeypatch.setattr(
            radio_player, "is_playing", lambda: calls.append(1) or True)

        mh._appliance_radio_live()
        mh._appliance_probe["at"] -= mh._APPLIANCE_PROBE_TTL_SECONDS + 1
        mh._appliance_radio_live()
        assert len(calls) == 2

    def test_the_ttl_is_well_inside_the_stale_gate(self):
        assert mh._APPLIANCE_PROBE_TTL_SECONDS < mh.RADIO_STALE_SECONDS


class TestSourceTagging:
    def test_the_stop_tool_reports_as_the_appliance(self, app, monkeypatch):
        from puffpai import radio_player
        monkeypatch.setattr(radio_player, "stop", lambda *a, **k: (True, ""))
        monkeypatch.setattr(radio_player, "is_playing", lambda: False)

        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        mh.tool_radio_stop(app)

        radio = app.get_data("mediahub")["radio"]
        assert radio["status"] == "idle" and radio["source"] == "appliance"

    def test_an_unknown_source_is_treated_as_the_display(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="nonsense")
        assert app.get_data("mediahub")["radio_display"]["source"] == "display"
