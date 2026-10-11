"""The box fires its own alarms, timers and reminders.

The guarantees these tests hold:

* firing happens server-side. A client-side timer only runs while its screen
  is mounted, so an alarm due while the kiosk shows another app would ring
  nowhere and stay ``status: "running"`` forever; the scheduler rings it or
  marks it "missed".
* "remind me to X on Monday" writes a row that the scheduler fires.
* "alarm 7" asks which half of the day is meant instead of committing to
  7:00 AM on a heuristic.
* "snooze" is in the vocabulary, so the LLM never picks its own duration for
  an utterance that names none.
* ``dismiss`` (soft stop) and ``cancel`` (hard delete) behave differently and
  say different things.
* a vaulted medication reminder is announced and logged by
  its label, never by the drug name.

Everything here runs on a fake clock and a fake announce sink — no thread, no
speaker, no waiting.
"""

import os
import sys
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai import alarm_scheduler, scheduler          # noqa: E402
from puffpai.alarm_scheduler import AlarmFlowSource     # noqa: E402
from puffpai.scheduler import (                          # noqa: E402
    CalendarSource, MedicationSource, Scheduler)


# ── doubles ────────────────────────────────────────────────────────────────
class FakeAppData:
    """Stands in for AppData: same get_data/set_data contract, no disk."""

    def __init__(self, docs=None):
        self.docs = docs or {}
        self.writes = 0

    def get_data(self, app):
        import copy
        return copy.deepcopy(self.docs.get(app, {}))

    def set_data(self, app, data):
        self.docs[app] = data
        self.writes += 1
        return True


class Clock:
    def __init__(self, start=None):
        self.t = start if start is not None else time.time()

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def sink():
    said = []
    return said, said.append


def _alarm(hour, minute, target_ms, *, repeat="once", status="running",
           desc="Alarm", item_id="a1"):
    return {"id": item_id, "mode": "alarm", "desc": desc, "status": status,
            "hour": hour, "minute": minute, "repeat": repeat,
            "targetMs": int(target_ms), "tot": 60, "rem": 60,
            "ts": int(target_ms) - 60000, "snoozed": 0}


def _timer(started_ms, total_seconds, *, desc="pasta", status="running",
           item_id="t1"):
    return {"id": item_id, "mode": "timer", "desc": desc, "status": status,
            "ts": int(started_ms), "tot": total_seconds, "rem": total_seconds}


def _sched(source, clock, sink_fn, tmp_path, **kw):
    return Scheduler([source], announce=sink_fn, clock=clock,
                     state_path=str(tmp_path / "scheduler.json"), **kw)


# ═══════════════════════════════════════════════════════════════════════════
# — a due alarm fires from the server, with no screen open anywhere
# ═══════════════════════════════════════════════════════════════════════════
class TestDueTimeFiring:

    def test_alarm_due_now_fires_and_is_marked(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 6, 59, 30).timestamp())
        target = datetime(2026, 9, 5, 7, 0).timestamp() * 1000
        data = FakeAppData({"alarmflow": {"items": [_alarm(7, 0, target)]}})
        sch = _sched(AlarmFlowSource(data), clock, say, tmp_path)

        assert sch.tick()["fired"] == []          # not due yet: silence
        assert said == []

        clock.advance(45)                          # 7:00:15
        out = sch.tick()

        assert len(out["fired"]) == 1
        assert said == ["Alarm: 7:00 AM."]
        item = data.docs["alarmflow"]["items"][0]
        assert item["status"] == "fired"
        assert item["fired_at"].startswith("2026-09-05T07:00")
        assert item["fired_ts"]

    def test_timer_expiry_fires(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 18, 0).timestamp())
        started = (clock.t - 590) * 1000           # a 10-minute timer, 10s left
        data = FakeAppData({"alarmflow": {"items": [_timer(started, 600)]}})
        sch = _sched(AlarmFlowSource(data), clock, say, tmp_path)

        assert sch.tick()["fired"] == []
        clock.advance(15)
        assert len(sch.tick()["fired"]) == 1
        assert said == ["Timer finished: pasta."]
        assert data.docs["alarmflow"]["items"][0]["status"] == "fired"

    def test_paused_and_stopped_items_never_fire(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 7, 0, 30).timestamp())
        past = (clock.t - 60) * 1000
        data = FakeAppData({"alarmflow": {"items": [
            _alarm(7, 0, past, status="stopped", item_id="a-stopped"),
            {"id": "t-paused", "mode": "timer", "desc": "bread",
             "status": "paused", "ts": None, "tot": 600, "rem": 120},
        ]}})
        out = _sched(AlarmFlowSource(data), clock, say, tmp_path).tick()
        assert out["fired"] == [] and out["missed"] == []
        assert said == []


# ═══════════════════════════════════════════════════════════════════════════
# — the missed state (the box was off when it came due)
# ═══════════════════════════════════════════════════════════════════════════
class TestMissedAfterDowntime:

    def test_alarm_due_during_downtime_is_marked_missed_and_announced_once(
            self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 7, 0).timestamp())
        target = clock.t * 1000
        data = FakeAppData({"alarmflow": {"items": [_alarm(7, 0, target)]}})
        state = str(tmp_path / "scheduler.json")

        # Ran at 06:00, then the box went down.
        before = Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                           state_path=state)
        clock.t = datetime(2026, 9, 5, 6, 0).timestamp()
        before.tick()
        assert said == []

        # Back up at 07:40 — forty minutes after the alarm should have rung.
        clock.t = datetime(2026, 9, 5, 7, 40).timestamp()
        after = Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                          state_path=state)
        out = after.tick()

        assert len(out["missed"]) == 1 and out["fired"] == []
        assert said == ["You missed an alarm at 7:00 AM."]
        item = data.docs["alarmflow"]["items"][0]
        assert item["status"] == "missed"
        assert item["missed_at"].startswith("2026-09-05T07:40")

        #...and never again.
        clock.advance(30)
        assert after.tick()["missed"] == []
        assert len(said) == 1

    def test_a_merely_slow_tick_rings_rather_than_inventing_a_miss(
            self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 7, 0).timestamp())
        data = FakeAppData({"alarmflow": {
            "items": [_alarm(7, 0, clock.t * 1000)]}})
        sch = _sched(AlarmFlowSource(data), clock, say, tmp_path)
        clock.t -= 60
        sch.tick()                                  # we were demonstrably up
        clock.t += 60 + 100                         # 100s late: inside the grace
        out = sch.tick()
        assert len(out["fired"]) == 1 and out["missed"] == []
        assert said == ["Alarm: 7:00 AM."]


# ═══════════════════════════════════════════════════════════════════════════
# — restart safety
# ═══════════════════════════════════════════════════════════════════════════
class TestRestartSafety:

    def test_no_double_fire_across_a_restart(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 7, 0, 20).timestamp())
        target = datetime(2026, 9, 5, 7, 0).timestamp() * 1000
        data = FakeAppData({"alarmflow": {"items": [_alarm(7, 0, target)]}})
        state = str(tmp_path / "scheduler.json")

        first = Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                          state_path=state)
        assert len(first.tick()["fired"]) == 1

        # The process restarts: fresh Scheduler, fresh source, same files.
        clock.advance(20)
        second = Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                           state_path=state)
        out = second.tick()

        assert out["fired"] == [] and out["missed"] == []
        assert said == ["Alarm: 7:00 AM."]

    def test_a_write_that_failed_still_does_not_double_announce(
            self, tmp_path, sink):
        """The occurrence token is the belt to the status write's braces."""
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 7, 0, 20).timestamp())
        target = datetime(2026, 9, 5, 7, 0).timestamp() * 1000
        data = FakeAppData({"alarmflow": {"items": [_alarm(7, 0, target)]}})

        class Unwritable(FakeAppData):
            def set_data(self, app, payload):
                raise IOError("disk full")

        broken = Unwritable(dict(data.docs))
        state = str(tmp_path / "scheduler.json")
        Scheduler([AlarmFlowSource(broken)], announce=say, clock=clock,
                  state_path=state).tick()
        assert said == ["Alarm: 7:00 AM."]

        clock.advance(10)
        Scheduler([AlarmFlowSource(broken)], announce=say, clock=clock,
                  state_path=state).tick()
        assert len(said) == 1


# ═══════════════════════════════════════════════════════════════════════════
# — rings, then gives up (and a repeating alarm survives)
# ═══════════════════════════════════════════════════════════════════════════
class TestRingTimeout:

    def test_a_repeating_alarm_rolls_to_its_next_occurrence(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 7, 0, 5).timestamp())
        target = datetime(2026, 9, 5, 7, 0).timestamp() * 1000
        data = FakeAppData({"alarmflow": {
            "items": [_alarm(7, 0, target, repeat="daily")]}})
        sch = _sched(AlarmFlowSource(data), clock, say, tmp_path,
                     ring_timeout_seconds=300)
        sch.tick()
        assert data.docs["alarmflow"]["items"][0]["status"] == "fired"

        clock.advance(400)                          # nobody dismissed it
        sch.tick()
        item = data.docs["alarmflow"]["items"][0]
        assert item["status"] == "running"
        rolled = datetime.fromtimestamp(item["targetMs"] / 1000)
        assert (rolled.hour, rolled.minute) == (7, 0)
        assert rolled.date() == datetime(2026, 9, 6).date()

        #...and tomorrow it rings again: a new occurrence, a new token.
        clock.t = rolled.timestamp() + 5
        assert len(sch.tick()["fired"]) == 1
        assert said == ["Alarm: 7:00 AM.", "Alarm: 7:00 AM."]

    def test_a_one_shot_alarm_stops(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 5, 7, 0, 5).timestamp())
        data = FakeAppData({"alarmflow": {"items": [
            _alarm(7, 0, datetime(2026, 9, 5, 7, 0).timestamp() * 1000)]}})
        sch = _sched(AlarmFlowSource(data), clock, say, tmp_path,
                     ring_timeout_seconds=300)
        sch.tick()
        clock.advance(400)
        sch.tick()
        assert data.docs["alarmflow"]["items"][0]["status"] == "stopped"


# ═══════════════════════════════════════════════════════════════════════════
# — calendar reminders fire too
# ═══════════════════════════════════════════════════════════════════════════
class TestCalendarReminders:

    def test_a_reminder_due_in_two_minutes_fires_exactly_once(self, tmp_path, sink):
        said, say = sink
        now = datetime(2026, 9, 7, 8, 58)
        clock = Clock(now.timestamp())
        data = FakeAppData({"calendar": {"tasks": [
            {"id": "t1", "title": "take the bins out", "status": "pending",
             "due": (now + timedelta(minutes=2)).isoformat()}]}})
        sch = _sched(CalendarSource(data, clock=clock), clock, say, tmp_path)

        assert sch.tick()["fired"] == []
        clock.advance(125)
        assert len(sch.tick()["fired"]) == 1
        assert said == ["Reminder: take the bins out."]
        row = data.docs["calendar"]["tasks"][0]
        assert row["reminder_status"] == "fired"
        assert row["status"] == "pending"      # the family's field, untouched

        clock.advance(60)
        assert sch.tick()["fired"] == []
        assert len(said) == 1

    def test_a_weekly_event_fires_its_NEXT_occurrence(self, tmp_path, sink):
        said, say = sink
        # The series started weeks ago; today is the 2026-09-07 occurrence.
        first = datetime(2026, 8, 10, 17, 30)
        clock = Clock(datetime(2026, 9, 7, 17, 30, 10).timestamp())
        data = FakeAppData({"calendar": {"events": [
            {"id": "e1", "title": "soccer practice", "start": first.isoformat(),
             "rec": "weekly", "remind": 0}]}})
        sch = _sched(CalendarSource(data, clock=clock), clock, say, tmp_path)

        out = sch.tick()
        assert len(out["fired"]) == 1
        assert said == ["Reminder: soccer practice."]
        fired_at = datetime.fromtimestamp(out["fired"][0]["due_ms"] / 1000)
        assert fired_at == datetime(2026, 9, 7, 17, 30)

        clock.advance(120)
        assert sch.tick()["fired"] == []       # not again this week
        # Next week it is a different occurrence, so it fires again.
        clock.t = datetime(2026, 9, 14, 17, 30, 10).timestamp()
        assert len(sch.tick()["fired"]) == 1

    def test_a_reminder_missed_while_down_is_announced_once_on_start(
            self, tmp_path, sink):
        said, say = sink
        due = datetime(2026, 9, 7, 9, 0)
        clock = Clock(datetime(2026, 9, 7, 8, 0).timestamp())
        data = FakeAppData({"calendar": {"tasks": [
            {"id": "t1", "title": "call the vet", "status": "pending",
             "due": due.isoformat()}]}})
        state = str(tmp_path / "scheduler.json")
        Scheduler([CalendarSource(data, clock=clock)], announce=say,
                  clock=clock, state_path=state).tick()
        assert said == []

        clock.t = datetime(2026, 9, 7, 11, 0).timestamp()      # back after 3h
        sch = Scheduler([CalendarSource(data, clock=clock)], announce=say,
                        clock=clock, state_path=state)
        out = sch.tick()
        assert len(out["missed"]) == 1
        assert said == ["You missed a reminder at call the vet."]
        assert data.docs["calendar"]["tasks"][0]["reminder_status"] == "missed"

        clock.advance(60)
        sch.tick()
        assert len(said) == 1

    def test_plain_events_and_task_mirrors_are_not_announced(self, tmp_path, sink):
        """The family calendar is not a public-address system."""
        said, say = sink
        now = datetime(2026, 9, 7, 9, 0, 30)
        clock = Clock(now.timestamp())
        start = now.replace(second=0).isoformat()
        data = FakeAppData({"calendar": {"events": [
            {"id": "e1", "title": "dentist", "start": start},          # no opt-in
            {"id": "te1", "title": "bins", "start": start, "src": "task",
             "taskId": "t1", "remind": 0},                             # a mirror
        ], "tasks": [
            {"id": "t1", "title": "bins", "status": "done",
             "due": start},                                            # completed
        ]}})
        out = _sched(CalendarSource(data, clock=clock), clock, say, tmp_path).tick()
        assert out["fired"] == [] and said == []


# ═══════════════════════════════════════════════════════════════════════════
# A vaulted medication is announced by its label, never its name
# ═══════════════════════════════════════════════════════════════════════════
class TestVaultedLabels:

    def test_vaulted_row_announces_and_logs_the_label_only(
            self, tmp_path, sink, caplog):
        said, say = sink
        secret = "Zolpidraxamine 40mg"        # fake med name
        now = datetime(2026, 9, 7, 8, 0, 20)
        clock = Clock(now.timestamp())
        data = FakeAppData({"calendar": {"meds": [
            {"id": "m1", "vaulted": True, "label": "morning pills",
             "title": secret, "name": secret, "dose": "1 tablet",
             "freq": "daily", "next": "8:00 AM"}]}})
        sch = _sched(MedicationSource(data, clock=clock), clock, say, tmp_path)

        with caplog.at_level("DEBUG"):
            out = sch.tick()

        assert len(out["fired"]) == 1
        assert said == ["It's 8 AM -- time for your morning pills."]
        assert secret not in " ".join(said)
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert secret not in logged
        assert "m1" in logged
        assert out["fired"][0]["label"] == "morning pills"
        assert secret not in str(out["fired"][0]["sentence"])

    def test_a_long_outage_does_not_read_the_same_dose_out_three_times(self):
        """Three missed daily doses is one sentence, not a chant."""
        occ = [{"kind": "medication", "label": "morning pills"}] * 3
        assert scheduler.missed_sentence(occ) == \
            "You missed a medication at morning pills."

    def test_a_vaulted_row_with_no_label_still_says_nothing_identifying(self):
        assert scheduler.safe_label(
            {"vaulted": True, "title": "Zolpidraxamine 40mg"},
            scheduler.VAULTED_FALLBACK_LABEL) == "medication"

    def test_an_unvaulted_row_keeps_its_title(self):
        assert scheduler.safe_label({"title": "call the vet"}) == "call the vet"

    def test_a_vaulted_missed_reminder_is_also_label_only(self, tmp_path, sink):
        said, say = sink
        secret = "Zolpidraxamine 40mg"
        clock = Clock(datetime(2026, 9, 7, 7, 0).timestamp())
        data = FakeAppData({"calendar": {"meds": [
            {"id": "m1", "vaulted": True, "label": "morning pills",
             "name": secret, "freq": "daily", "next": "8:00 AM"}]}})
        state = str(tmp_path / "scheduler.json")
        Scheduler([MedicationSource(data, clock=clock)], announce=say,
                  clock=clock, state_path=state).tick()
        clock.t = datetime(2026, 9, 7, 10, 0).timestamp()
        out = Scheduler([MedicationSource(data, clock=clock)], announce=say,
                        clock=clock, state_path=state).tick()
        assert len(out["missed"]) == 1
        assert said == ["You missed a medication at morning pills."]
        assert secret not in " ".join(said)


# ═══════════════════════════════════════════════════════════════════════════
# The duck policy wraps the announcement (an alarm is an ADDRESSED event)
# ═══════════════════════════════════════════════════════════════════════════
class TestVoiceDrainDucks:

    @staticmethod
    def _fake_announcer(records):
        class _Ann:
            cursor = ""

            @staticmethod
            def pending_speech():
                return [r for r in records if r["ts"] > _Ann.cursor]

            @staticmethod
            def recent(limit=20):
                return list(reversed(records))[:limit]

            @staticmethod
            def read_cursor():
                return _Ann.cursor

            @classmethod
            def write_cursor(cls, ts):
                cls.cursor = ts
        return _Ann

    class _Duck:
        def __init__(self):
            self.calls = []

        def on_addressed(self):
            self.calls.append("duck")

        def restore(self):
            self.calls.append("restore")

    def test_announcement_ducks_speaks_then_restores(self):
        from puffpai.voice import service
        ann = self._fake_announcer([{"ts": "2026-09-05T07:00:00+00:00",
                                     "text": "Alarm: 7:00 AM.",
                                     "speak": True, "source": "scheduler"}])
        duck = self._Duck()
        spoken = []

        count = service.drain_announcements(
            tts=None, ann=ann, duck=duck,
            speaker=lambda _tts, text: spoken.append(text))

        assert count == 1
        assert spoken == ["Alarm: 7:00 AM."]
        assert duck.calls == ["duck", "restore"]

    def test_the_duck_is_restored_even_when_speech_blows_up(self):
        from puffpai.voice import service
        ann = self._fake_announcer([{"ts": "2026-09-05T07:00:00+00:00",
                                     "text": "Alarm: 7:00 AM.", "speak": True}])
        duck = self._Duck()

        def _explode(_tts, _text):
            raise RuntimeError("piper is down")

        assert service.drain_announcements(
            tts=None, ann=ann, duck=duck, speaker=_explode) == 0
        assert duck.calls == ["duck", "restore"]

    def test_nothing_pending_means_no_duck_at_all(self):
        from puffpai.voice import service
        ann = self._fake_announcer([])
        duck = self._Duck()
        assert service.drain_announcements(tts=None, ann=ann, duck=duck,
                                           speaker=lambda *_: None) == 0
        assert duck.calls == []

    def test_an_announcement_is_spoken_once(self):
        from puffpai.voice import service
        ann = self._fake_announcer([{"ts": "2026-09-05T07:00:00+00:00",
                                     "text": "Alarm: 7:00 AM.", "speak": True}])
        duck = self._Duck()
        spoken = []
        say = lambda _t, text: spoken.append(text)      # noqa: E731
        service.drain_announcements(tts=None, ann=ann, duck=duck, speaker=say)
        service.drain_announcements(tts=None, ann=ann, duck=duck, speaker=say)
        assert spoken == ["Alarm: 7:00 AM."]


# ═══════════════════════════════════════════════════════════════════════════
# The announcer's local-speaker queue
# ═══════════════════════════════════════════════════════════════════════════
class TestAnnouncerPendingSpeech:

    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path, monkeypatch):
        from puffpai import announcer
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        announcer.reset_cache()
        yield
        announcer.reset_cache()

    def test_a_queued_announcement_is_pending_until_the_cursor_moves(self):
        from puffpai import announcer
        announcer.announce("Alarm: 7:00 AM.", source="scheduler")
        pending = announcer.pending_speech()
        assert [p["text"] for p in pending] == ["Alarm: 7:00 AM."]
        announcer.write_cursor(pending[0]["ts"])
        assert announcer.pending_speech() == []

    def test_stale_announcements_are_not_read_out_on_a_late_start(self):
        from datetime import timezone
        from puffpai import announcer
        announcer.announce("Alarm: 7:00 AM.", source="scheduler")
        much_later = datetime.now(timezone.utc) + timedelta(hours=4)
        assert announcer.pending_speech(now=much_later) == []


# ═══════════════════════════════════════════════════════════════════════════
# / / contract mismatch 2 — the words themselves
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def api():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
    import puffpai.api_server as mod
    return mod


class TestAmbiguousHourAsks:
    """'alarm 7' created a 7:00 AM alarm without ever asking."""

    @pytest.mark.parametrize("phrase", [
        "alarm 7",
        "set an alarm for 7",
        "set an alarm for 7:30",
        "set my alarm for 8",
    ])
    def test_an_ambiguous_hour_asks_instead_of_guessing(self, api, phrase):
        reply = api._try_alarm_clarify(phrase)
        assert reply is not None, phrase
        assert "AM or" in reply and "PM" in reply

    @pytest.mark.parametrize("phrase", [
        "set an alarm for 7am",
        "wake me up at 6:45 tomorrow morning",
        # "wake me up" names the morning by meaning.
        "wake me up at 6:45 tomorrow",
        "wake me up at 7",
        "set an alarm for noon",
        "set an alarm for 19:30",
        "set a timer for 7 minutes",
    ])
    def test_an_unambiguous_request_is_left_alone(self, api, phrase):
        assert api._try_alarm_clarify(phrase) is None

    def test_the_set_fastpath_itself_refuses_to_guess(self, api):
        reply = api._try_alarm_set("set an alarm for 7")
        assert reply is not None and "AM or" in reply

    def test_asking_writes_nothing(self, api, monkeypatch):
        """An ambiguous request must have NO side effect (contrast )."""
        called = []
        monkeypatch.setattr(api, "_get_assistant_tools",
                            lambda: called.append("tools"))
        api._try_alarm_clarify("alarm 7")
        assert called == []


class TestSnoozeIsDeclared:
    """'snooze' was not in the vocabulary, so the LLM invented 5 min."""

    class _Tools:
        def __init__(self):
            self.seen = []
            outer = self

            class _Tool:
                @staticmethod
                def invoke(payload):
                    outer.seen.append(payload)
                    return f"Snoozed for {payload['minutes']} minutes."

            self.tool_dict = {"app_snooze_alarm": _Tool()}

    def _patch_data(self, api, monkeypatch, items):
        import puffpai.app_data as app_data_mod

        class _AD:
            def get_data(self, app):
                return {"items": items}

        monkeypatch.setattr(app_data_mod, "AppData", lambda: _AD())

    def test_bare_snooze_uses_the_declared_nine_minutes(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(api, monkeypatch, [
            {"id": "a1", "mode": "alarm", "status": "fired", "fired_ts": 10}])
        reply = api._try_alarm_snooze("snooze", tools)
        assert reply == "Snoozed for 9 minutes."
        assert tools.seen == [{"identifier": "a1", "minutes": 9}]

    def test_a_stated_duration_wins(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(api, monkeypatch, [
            {"id": "a1", "mode": "alarm", "status": "fired", "fired_ts": 10}])
        api._try_alarm_snooze("snooze for 20 minutes", tools)
        assert tools.seen == [{"identifier": "a1", "minutes": 20}]

    def test_the_owner_can_change_the_default(self, api, monkeypatch):
        tools = self._Tools()
        monkeypatch.setenv("PUFFPAI_SNOOZE_MINUTES", "12")
        self._patch_data(api, monkeypatch, [
            {"id": "a1", "mode": "alarm", "status": "fired", "fired_ts": 10}])
        api._try_alarm_snooze("snooze", tools)
        assert tools.seen == [{"identifier": "a1", "minutes": 12}]

    def test_snooze_targets_what_is_actually_ringing(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(api, monkeypatch, [
            {"id": "a-old", "mode": "alarm", "status": "running", "ts": 5},
            {"id": "a-ringing", "mode": "alarm", "status": "fired",
             "fired_ts": 99},
        ])
        api._try_alarm_snooze("snooze", tools)
        assert tools.seen[0]["identifier"] == "a-ringing"

    def test_nothing_to_snooze_says_so(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(api, monkeypatch, [])
        assert "nothing to snooze" in api._try_alarm_snooze("snooze", tools)
        assert tools.seen == []

    def test_ordinary_english_is_not_a_snooze_when_nothing_is_ringing(
            self, api, monkeypatch):
        """"hold on a bit longer" must not be answered with an alarm."""
        tools = self._Tools()
        self._patch_data(api, monkeypatch, [
            {"id": "a1", "mode": "alarm", "status": "running", "ts": 5}])
        assert api._try_alarm_snooze("can you hold on a bit longer", tools) is None
        assert tools.seen == []

    def test_the_same_words_DO_snooze_what_is_ringing(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(api, monkeypatch, [
            {"id": "a1", "mode": "alarm", "status": "fired", "fired_ts": 10}])
        assert api._try_alarm_snooze("five more minutes", tools) == \
            "Snoozed for 5 minutes."

    def test_a_question_about_snoozing_is_not_a_snooze(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(api, monkeypatch, [
            {"id": "a1", "mode": "alarm", "status": "fired", "fired_ts": 1}])
        assert api._try_alarm_snooze("how long is the snooze", tools) is None


class TestDismissVersusCancel:
    """Contract mismatch 2: two verbs, two outcomes, one sentence."""

    class _Tools:
        def __init__(self):
            self.calls = []
            outer = self

            class _Del:
                @staticmethod
                def invoke(ident):
                    outer.calls.append(("delete", ident))
                    return "Deleted 1 item."

            class _Dis:
                @staticmethod
                def invoke(ident):
                    outer.calls.append(("dismiss", ident))
                    return "Dismissed: Alarm"

            self.tool_dict = {"app_delete_alarm": _Del(),
                              "app_dismiss_alarm": _Dis()}

    def _patch_data(self, monkeypatch, clocks, ids_at):
        import puffpai.app_data as app_data_mod

        class _AD:
            def alarm_active_clocks(self, mode='alarm'):
                return clocks

            def alarm_ids_at(self, hour, minute, mode='alarm'):
                return ids_at

            def alarm_latest_id(self, mode='alarm'):
                return next(iter(clocks), None)

        monkeypatch.setattr(app_data_mod, "AppData", lambda: _AD())

    def test_cancel_deletes_and_says_the_item_is_gone(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(monkeypatch, {"a1": (7, 0)}, ["a1"])
        reply = api._try_alarm_cancel("cancel the 7am alarm", tools)
        assert ("delete", "a1") in tools.calls
        assert "removed" in reply.lower()
        assert "dismiss" in reply.lower()      # the other verb is offered

    def test_dismiss_is_a_soft_stop_and_keeps_the_item(self, api, monkeypatch):
        tools = self._Tools()
        self._patch_data(monkeypatch, {}, [])
        reply = api._try_alarm_dismiss("dismiss the pasta timer", tools)
        assert tools.calls == [("dismiss", "pasta")]
        assert reply == "Dismissed: Alarm"


# ═══════════════════════════════════════════════════════════════════════════
# medications are their own source, with their own shape
# ═══════════════════════════════════════════════════════════════════════════
class TestMedicationSource:

    def test_a_medication_due_at_0800_fires_once_and_says_the_label_only(
            self, tmp_path, sink):
        said, say = sink
        secret = "Zolpidraxamine 40mg"          # fake med name
        clock = Clock(datetime(2026, 9, 7, 7, 59, 30).timestamp())
        data = FakeAppData({"calendar": {"meds": [
            {"id": "m1", "kind": "medication", "vaulted": True,
             "label": "morning pills", "name": secret, "dose": "1 tablet",
             "to": "mom", "freq": "daily", "next": "8:00 AM"}]}})
        sch = _sched(MedicationSource(data, clock=clock), clock, say, tmp_path)

        assert sch.tick()["fired"] == []           # 07:59:30 — not yet
        clock.advance(45)                          # 08:00:15 local
        out = sch.tick()

        assert len(out["fired"]) == 1
        assert out["fired"][0]["kind"] == "medication"
        assert said == ["It's 8 AM -- time for your morning pills."]
        assert secret not in " ".join(said)

        clock.advance(120)                         # and only once
        assert sch.tick()["fired"] == []
        assert len(said) == 1

    def test_a_daily_medication_is_armed_again_tomorrow(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 7, 8, 0, 10).timestamp())
        data = FakeAppData({"calendar": {"meds": [
            {"id": "m1", "label": "morning pills", "freq": "daily",
             "next": "8:00 AM"}]}})
        sch = _sched(MedicationSource(data, clock=clock), clock, say, tmp_path)
        assert len(sch.tick()["fired"]) == 1
        clock.t = datetime(2026, 9, 8, 8, 0, 10).timestamp()
        assert len(sch.tick()["fired"]) == 1
        assert len(said) == 2

    def test_medications_are_not_claimed_by_the_calendar_source(
            self, tmp_path, sink):
        """Three sources, three row shapes — nothing is fired twice."""
        said, say = sink
        clock = Clock(datetime(2026, 9, 7, 8, 0, 10).timestamp())
        data = FakeAppData({"calendar": {"meds": [
            {"id": "m1", "label": "morning pills", "freq": "daily",
             "next": "8:00 AM"}]}})
        both = [CalendarSource(data, clock=clock),
                MedicationSource(data, clock=clock)]
        out = Scheduler(both, announce=say, clock=clock,
                        state_path=str(tmp_path / "s.json")).tick()
        assert len(out["fired"]) == 1
        assert said == ["It's 8 AM -- time for your morning pills."]

    def test_exec_calendars_occurrence_feed_wins_when_the_box_has_it(
            self, tmp_path, sink):
        """exec-calendar's interface.

        ``cal_schedulable_reminders(from, to)`` returns one row per occurrence
        in naive LOCAL wall-clock, already expanded for recurrence, and its
        ``display`` is the only speakable field. When it exists it wins over
        the local derivation — which here is deliberately pointed at a
        different time, so the assertion can only pass via the feed.
        """
        said, say = sink
        secret = "Zolpidraxamine 40mg"
        chosen = datetime(2026, 9, 7, 20, 0)
        clock = Clock((chosen + timedelta(seconds=10)).timestamp())

        class WithFeed(FakeAppData):
            def cal_schedulable_reminders(self, from_dt=None, to_dt=None):
                return [
                    {"kind": "medication", "id": "m1", "when": chosen,
                     "occurrence_id": f"m1@{chosen.isoformat()}",
                     "display": "evening pills", "vaulted": True,
                     "label": "evening pills", "to": "mom", "cat": "medical",
                     "rec": "daily", "source_id": "m1"},
                    {"kind": "task", "id": "t9", "when": chosen,
                     "display": "not mine", "vaulted": False,
                     "source_id": "t9"},
                ]

        data = WithFeed({"calendar": {"meds": [
            {"id": "m1", "label": "evening pills", "name": secret,
             "vaulted": True, "freq": "daily",
             "next": "9:00 AM"}]}})          # deliberately NOT what fires
        out = _sched(MedicationSource(data, clock=clock), clock, say,
                     tmp_path).tick()

        assert len(out["fired"]) == 1        # the task row belongs to the
        assert out["fired"][0]["kind"] == "medication"   # other source
        assert out["fired"][0]["due_ms"] == int(chosen.timestamp() * 1000)
        assert out["fired"][0]["id"] == f"m1@{chosen.isoformat()}"
        assert said == ["It's 8 PM -- time for your evening pills."]
        assert secret not in " ".join(said)
        # The mark still lands on the stored row, found by source_id.
        assert data.docs["calendar"]["meds"][0]["reminder_status"] == "fired"

    def test_the_feeds_task_rows_belong_to_the_calendar_source(
            self, tmp_path, sink):
        said, say = sink
        when = datetime(2026, 9, 7, 20, 0)
        clock = Clock((when + timedelta(seconds=10)).timestamp())

        class WithFeed(FakeAppData):
            def cal_schedulable_reminders(self, from_dt=None, to_dt=None):
                return [{"kind": "task", "id": "t9", "when": when,
                         "display": "call the vet", "vaulted": False,
                         "source_id": "t9"}]

        data = WithFeed({"calendar": {"tasks": [
            {"id": "t9", "title": "call the vet", "status": "pending"}]}})
        out = _sched(CalendarSource(data, clock=clock), clock, say,
                     tmp_path).tick()
        assert len(out["fired"]) == 1
        assert said == ["Reminder: call the vet."]
        assert data.docs["calendar"]["tasks"][0]["status"] == "pending"

    def test_the_default_source_set_is_the_three_declared_ones(self,
                                                               monkeypatch):
        import puffpai.scheduler as sched_mod
        monkeypatch.setattr(sched_mod, "_shared_app_data",
                            lambda: FakeAppData({}))
        assert [s.name for s in sched_mod.build_default_sources()] == [
            "alarmflow", "calendar", "medications"]
