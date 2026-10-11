"""The dashboard's own idle state must not erase the
appliance's station, and a month-old Spotify row must not claim the tile.

The failure to prevent: voice-started radio shows on the Now Playing tile, and
vanishes the moment the owner REFRESHES the dashboard, while the stream keeps
playing. The kiosk bridge posts ``mediahub_report_radio`` with ITS OWN browser
player's state on mount and on every heartbeat, with no ``source``; in a
single ``radio`` slot the display's "idle" would overwrite the appliance's
"playing".

So there are two slots. ``radio`` is appliance-owned (server-side players only);
display reports and kiosk acks land in ``radio_display``. Plus a freshness gate
on the stored Spotify row, which has no heartbeat at all.
"""

from datetime import datetime, timedelta, timezone

import pytest

from puffpai import app_data as app_data_mod
from puffpai import media_hub as mh

_ENV = "PUFFPAI_DATA_DIR"


@pytest.fixture(autouse=True)
def _fresh_radio_heartbeat(monkeypatch):
    monkeypatch.setattr(mh, "_radio_last_seen_iso", None)


@pytest.fixture(autouse=True)
def _no_container_probe(monkeypatch):
    """The probe shells out to docker. Every case here decides for itself."""
    monkeypatch.setattr(mh, "_appliance_probe", {"at": None, "live": False})
    monkeypatch.setattr(mh, "_appliance_radio_live", lambda: False)


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


def _bridge_report(app, status="idle", station=None):
    """Exactly what KioskMediaBridge posts: no source, often no station."""
    return app.mediahub_report_radio(status, station=station)


# ══════════════════════════════════════════════════════════════════════════════
# 1: a dashboard refresh mid-stream
# ══════════════════════════════════════════════════════════════════════════════

class TestTheRefreshNoLongerErasesTheStation:
    def test_a_bridge_idle_report_leaves_the_appliance_playing(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")

        _bridge_report(app, "idle")  # the dashboard refresh

        radio = mh.aggregate_sources(app)["radio"]
        assert radio["status"] == "playing", "the refresh erased the station"
        assert radio["station"]["name"] == "KEXP"
        assert radio["stale"] is False
        assert radio["source"] == "appliance"

    def test_a_kiosk_ack_of_idle_leaves_the_appliance_playing(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")

        app.mediahub_ack_command("no-such-command", "idle")

        radio = mh.aggregate_sources(app)["radio"]
        assert radio["status"] == "playing" and radio["stale"] is False

    def test_the_appliance_stopping_does_clear_the_row(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        app.mediahub_report_radio("idle", source="appliance")

        radio = mh.aggregate_sources(app)["radio"]
        assert radio["status"] == "idle"
        assert radio["station"] is None
        assert radio["stale"] is False

    def test_the_stop_tool_still_clears_the_row(self, app, monkeypatch):
        from puffpai import radio_player
        monkeypatch.setattr(radio_player, "stop", lambda *a, **k: (True, ""))

        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        mh.tool_radio_stop(app)

        assert mh.aggregate_sources(app)["radio"]["status"] == "idle"


# ══════════════════════════════════════════════════════════════════════════════
# 2 — the two slots, at the writer
# ══════════════════════════════════════════════════════════════════════════════

class TestTheWriteSlots:
    def test_a_display_report_does_not_touch_the_appliance_row(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        before = app.get_data("mediahub")["radio"]

        _bridge_report(app, "idle")
        _bridge_report(app, "playing", station=_station("Some Browser Stream"))

        assert app.get_data("mediahub")["radio"] == before

    def test_a_display_report_writes_the_display_slot(self, app):
        _bridge_report(app, "playing", station=_station("Browser FM"))

        row = app.get_data("mediahub")["radio_display"]
        assert row["status"] == "playing"
        assert row["station"]["name"] == "Browser FM"
        assert row["source"] == "display"
        assert mh._age_seconds(row["updated_at"]) < 5

    def test_a_display_report_still_beats_the_heartbeat(self, app, monkeypatch):
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))
        _bridge_report(app, "idle")

        doc = app.get_data("mediahub")
        assert mh._age_seconds(doc["radio_last_seen"]) < 5
        assert mh._age_seconds(mh._radio_last_seen(doc)) < 5
        assert mh.radio_display_online(app) is True

    def test_an_ack_writes_the_display_slot_and_still_clears(self, app):
        queued = app.mediahub_radio_request("play", station=_station())
        before_radio = app.get_data("mediahub")["radio"]

        result = app.mediahub_ack_command(queued["id"], "playing",
                                          station=_station())

        assert result["cleared"] is True
        doc = app.get_data("mediahub")
        assert doc["radio_command"] is None
        assert doc["radio_display"]["status"] == "playing"
        assert doc["radio"] == before_radio

    @pytest.mark.parametrize("status", mh.VALID_RADIO_STATUS)
    def test_the_display_slot_keeps_status_validation(self, app, status):
        assert _bridge_report(app, status)["ok"] is True
        assert app.get_data("mediahub")["radio_display"]["status"] == status

    @pytest.mark.parametrize("bad", ["bogus", "", None, 7])
    def test_a_bad_display_status_is_still_rejected(self, app, bad):
        with pytest.raises(ValueError):
            _bridge_report(app, bad)
        with pytest.raises(ValueError):
            app.mediahub_ack_command("x", bad)

    def test_an_unknown_source_is_still_the_display(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="nonsense")
        doc = app.get_data("mediahub")
        assert doc["radio"] == {"status": "idle", "station": None,
                                "updated_at": None}, "the default skeleton"
        assert doc["radio_display"]["source"] == "display"


# ══════════════════════════════════════════════════════════════════════════════
# 3 — the two slots, at the aggregate (and the migration)
# ══════════════════════════════════════════════════════════════════════════════

class TestTheAggregateSlots:
    def test_the_display_state_is_exposed_separately(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        _bridge_report(app, "playing", station=_station("Browser FM"))

        out = mh.aggregate_sources(app)
        assert out["radio"]["station"]["name"] == "KEXP"
        assert out["radio_display"]["station"]["name"] == "Browser FM"

    def test_the_display_slot_is_always_present_and_shaped(self, app):
        out = mh.aggregate_sources(app)
        assert out["radio_display"] == {"status": "idle", "station": None,
                                        "stale": False}

    def test_the_display_slot_keeps_the_pure_heartbeat_gate(
            self, app, monkeypatch):
        """The mpv container says nothing about what a browser is doing."""
        called = []
        monkeypatch.setattr(
            mh, "_appliance_radio_live", lambda: called.append(1) or True)

        _bridge_report(app, "playing", station=_station())
        doc = app.get_data("mediahub")
        doc["radio_display"]["updated_at"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
        doc["radio_last_seen"] = _iso_ago(mh.RADIO_STALE_SECONDS + 30)
        app.set_data("mediahub", doc)
        monkeypatch.setattr(mh, "_radio_last_seen_iso", _iso_ago(300))

        assert mh.aggregate_sources(app)["radio_display"]["stale"] is True
        assert called == []

    def test_a_legacy_display_row_is_not_shown_as_the_appliance(self, app):
        """An older file shape: radio.source == display."""
        app.set_data("mediahub", {
            "radio": {"status": "playing", "station": _station("Old Row"),
                      "updated_at": _iso_ago(2), "source": "display"},
            "radio_last_seen": _iso_ago(2),
        })

        out = mh.aggregate_sources(app)
        assert out["radio"]["status"] == "idle"
        assert out["radio"]["station"] is None
        assert out["radio_display"]["station"]["name"] == "Old Row"

    def test_a_legacy_display_row_never_shadows_a_real_display_row(self, app):
        app.set_data("mediahub", {
            "radio": {"status": "playing", "station": _station("Old Row"),
                      "updated_at": _iso_ago(2), "source": "display"},
            "radio_display": {"status": "idle", "station": None,
                              "updated_at": _iso_ago(1), "source": "display"},
            "radio_last_seen": _iso_ago(1),
        })

        out = mh.aggregate_sources(app)
        assert out["radio"]["status"] == "idle"
        assert out["radio_display"]["status"] == "idle"

    def test_a_legacy_row_with_no_source_still_reads_as_the_appliance_slot(
            self, app):
        """Older files carry no source at all; they are the appliance's
        best available record and must not be thrown away."""
        app.set_data("mediahub", {
            "radio": {"status": "playing", "station": _station("Legacy"),
                      "updated_at": _iso_ago(2)},
            "radio_last_seen": _iso_ago(2),
        })

        assert mh.aggregate_sources(app)["radio"]["station"]["name"] == "Legacy"

    def test_the_source_doc_is_not_mutated(self, app):
        app.mediahub_report_radio("playing", station=_station(), source="appliance")
        _bridge_report(app, "idle")

        mh.aggregate_sources(app)
        doc = app.get_data("mediahub")
        assert "stale" not in doc["radio"]
        assert "stale" not in doc["radio_display"]


# ══════════════════════════════════════════════════════════════════════════════
# 4 — Spotify: an event stream with no heartbeat
# ══════════════════════════════════════════════════════════════════════════════

class TestSpotifyFreshness:
    @staticmethod
    def _doc(app, age):
        app.set_data("mediahub", {"spotify": {
            "active": True, "playing": True, "user": "alex",
            "track": {"title": "Blue in Green", "artists": "Miles Davis",
                      "album": None, "uri": "spotify:track:x"},
            "last_event": "playing", "updated_at": _iso_ago(age),
        }})

    def test_a_month_old_row_does_not_claim_the_tile(self, app):
        self._doc(app, 30 * 24 * 3600)  # a row thirty days old

        sp = mh.aggregate_sources(app)["spotify"]
        assert sp["active"] is False
        assert sp["playing"] is False
        assert sp["track"] is None
        assert sp["user"] == "alex", "the pairing label is not a playback claim"

    def test_a_fresh_row_is_passed_through(self, app):
        self._doc(app, 5)

        sp = mh.aggregate_sources(app)["spotify"]
        assert sp["active"] is True
        assert sp["playing"] is True
        assert sp["track"]["title"] == "Blue in Green"

    def test_the_boundary(self, app):
        self._doc(app, mh.SPOTIFY_STALE_SECONDS - 20)
        assert mh.aggregate_sources(app)["spotify"]["active"] is True

        self._doc(app, mh.SPOTIFY_STALE_SECONDS + 20)
        assert mh.aggregate_sources(app)["spotify"]["active"] is False

    def test_an_unreadable_stamp_is_passed_through(self, app):
        """We can only call something stale if we can measure its age; the
        writer always stamps, so this shape only comes from hand-edits."""
        app.set_data("mediahub", {"spotify": {"active": True, "playing": True,
                                              "updated_at": "whenever"}})
        assert mh.aggregate_sources(app)["spotify"]["active"] is True

    def test_the_stale_gate_does_not_mutate_the_stored_row(self, app):
        self._doc(app, 30 * 24 * 3600)
        mh.aggregate_sources(app)
        assert app.get_data("mediahub")["spotify"]["active"] is True

    def test_a_real_event_still_writes_the_stored_row_unchanged(self, app):
        app.mediahub_spotify_event({"event": "playing", "name": "alex"})
        stored = app.get_data("mediahub")["spotify"]
        assert stored["active"] is True
        assert mh._age_seconds(stored["updated_at"]) < 5
        assert mh.aggregate_sources(app)["spotify"]["active"] is True

    def test_the_window_is_far_longer_than_the_radio_gate(self):
        assert mh.SPOTIFY_STALE_SECONDS > mh.RADIO_STALE_SECONDS
        assert mh.RADIO_STALE_SECONDS == 45
