"""The box never announces an occurrence that came due before it existed.

The hazard: a medication added at 01:12 with a ``times: ["08:00"]`` daily
schedule. The calendar's occurrence feed is asked for the last 24 hours, so it
offers YESTERDAY's 08:00; a scheduler that rings any unfired past occurrence
would mark it ``fired`` and have the voice unit say "It's 8 AM -- time for your
morning pills." out loud, at 1:12 in the morning. The privacy contract holds (a
label, never a drug name) but the household is told a false time-of-day fact.

The rule these tests hold: an occurrence is eligible to ring only if its due
time is at or after the item's own ``created_at`` (or, for a row that carries
none, the first-seen watermark the scheduler persists) AND at or after the last
tick minus the missed window. Anything earlier is recorded as ``missed`` and
LOGGED, and is never spoken. The rest of the scheduler contract holds too: a
restart is quiet, a real future ring still rings, and one genuine outage still
produces exactly one missed record.

Fake clock, fake announce sink, fake medication names only.
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai import scheduler                            # noqa: E402
from puffpai.alarm_scheduler import AlarmFlowSource      # noqa: E402
from puffpai.scheduler import (                          # noqa: E402
    CalendarSource, MedicationSource, Scheduler)


class FakeAppData:
    """Same get_data/set_data contract as AppData, no disk."""

    def __init__(self, docs=None):
        self.docs = docs or {}

    def get_data(self, app):
        import copy
        return copy.deepcopy(self.docs.get(app, {}))

    def set_data(self, app, data):
        self.docs[app] = data
        return True


class FeedAppData(FakeAppData):
    """AppData WITH the calendar's occurrence feed, the production shape.

    ``cal_schedulable_reminders`` expands a medication's ``times``/``days``
    across the whole window it is handed, which is what puts yesterday's
    08:00 in front of the scheduler.
    """

    def cal_schedulable_reminders(self, from_dt, to_dt):
        out = []
        for med in (self.docs.get("calendar", {}).get("meds") or []):
            day = from_dt.date()
            while day <= to_dt.date():
                for stamp in (med.get("times") or []):
                    hour, minute = (int(p) for p in stamp.split(":"))
                    when = datetime.combine(day, datetime.min.time()).replace(
                        hour=hour, minute=minute)
                    if from_dt <= when <= to_dt:
                        out.append({
                            "kind": "medication",
                            "id": med["id"],
                            "occurrence_id": f"{med['id']}@{when.isoformat()}",
                            "when": when,
                            "display": med.get("label") or "medication",
                            "vaulted": bool(med.get("vaulted")),
                            "to": med.get("to"), "cat": "medical",
                            "rec": "daily", "source_id": med.get("med_id"),
                        })
                day += timedelta(days=1)
        return out


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def sink():
    said = []
    return said, said.append


def _med(created_at=None, times=("08:00",)):
    """A vaulted, label-only medication, daily at 08:00."""
    row = {"id": "m1", "med_id": "mv1", "kind": "medication", "vaulted": True,
           "name": "morning pills", "label": "morning pills",
           "dose": "as prescribed", "freq": "daily", "next": "8:00 AM",
           "times": list(times), "days": [0, 1, 2, 3, 4, 5, 6],
           "tz": "local", "status": "due", "refill": None}
    if created_at is not None:
        row["created_at"] = created_at
    return row


def _alarm(hour, minute, target_ms, *, item_id="a1", created_at=None):
    row = {"id": item_id, "mode": "alarm", "desc": "Alarm", "status": "running",
           "hour": hour, "minute": minute, "repeat": "once",
           "targetMs": int(target_ms), "tot": 60, "rem": 60,
           "ts": int(target_ms) - 60000, "snoozed": 0}
    if created_at is not None:
        row["created_at"] = created_at
    return row


# ═══════════════════════════════════════════════════════════════════════════
# The core scenario: added after today's due time
# ═══════════════════════════════════════════════════════════════════════════
class TestAddedAt0112:
    """Add a vaulted medication at 01:12 with an 08:00 daily schedule."""

    def _world(self, tmp_path, say, created_at="2026-09-06T01:12:00"):
        clock = Clock(datetime(2026, 9, 6, 1, 12, 30).timestamp())
        data = FeedAppData({"calendar": {"meds": [_med(created_at)]}})
        sch = Scheduler([MedicationSource(data, clock=clock)], announce=say,
                        clock=clock, state_path=str(tmp_path / "scheduler.json"))
        return clock, data, sch

    def test_nothing_is_announced_when_the_med_is_added_at_0112(
            self, tmp_path, sink):
        said, say = sink
        clock, data, sch = self._world(tmp_path, say)
        out = sch.tick()

        assert said == []                       # the whole point
        assert out["fired"] == [] and out["missed"] == []

    def test_yesterdays_dose_is_recorded_as_missed_not_fired(
            self, tmp_path, sink):
        said, say = sink
        clock, data, sch = self._world(tmp_path, say)
        out = sch.tick()

        assert len(out["silent"]) == 1
        assert out["silent"][0]["status"] == "missed"
        assert data.docs["calendar"]["meds"][0]["reminder_status"] == "missed"

    def test_the_next_real_0800_still_fires(self, tmp_path, sink):
        said, say = sink
        clock, data, sch = self._world(tmp_path, say)
        sch.tick()
        assert said == []

        clock.t = datetime(2026, 9, 6, 8, 0, 3).timestamp()
        out = sch.tick()

        assert len(out["fired"]) == 1 and out["silent"] == []
        assert said == ["It's 8 AM -- time for your morning pills."]

    def test_it_also_holds_for_a_row_that_carries_no_created_at(
            self, tmp_path, sink):
        """The first-seen watermark covers rows written before this fix."""
        said, say = sink
        clock, data, sch = self._world(tmp_path, say, created_at=None)
        out = sch.tick()

        assert said == []
        assert len(out["silent"]) == 1

        clock.t = datetime(2026, 9, 6, 8, 0, 3).timestamp()
        assert len(sch.tick()["fired"]) == 1
        assert said == ["It's 8 AM -- time for your morning pills."]

    def test_a_drug_name_is_never_spoken_on_either_path(self, tmp_path, sink):
        """The label-only rule still holds on the quiet path."""
        said, say = sink
        secret = "Zolpidraxamine 40mg"       # fake medication name
        clock = Clock(datetime(2026, 9, 6, 1, 12, 30).timestamp())
        row = _med("2026-09-06T01:12:00")
        row["name"] = secret
        data = FeedAppData({"calendar": {"meds": [row]}})
        sch = Scheduler([MedicationSource(data, clock=clock)], announce=say,
                        clock=clock, state_path=str(tmp_path / "s.json"))
        sch.tick()
        clock.t = datetime(2026, 9, 6, 8, 0, 3).timestamp()
        sch.tick()

        assert secret not in " ".join(said)


# ═══════════════════════════════════════════════════════════════════════════
# The behaviours the fix must NOT cost us
# ═══════════════════════════════════════════════════════════════════════════
class TestNothingElseBreaks:

    def test_a_due_time_five_minutes_out_fires_exactly_once(
            self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 6, 1, 12, 0).timestamp())
        target = datetime(2026, 9, 6, 1, 17, 0).timestamp() * 1000
        data = FakeAppData({"alarmflow": {"items": [
            _alarm(1, 17, target, created_at="2026-09-06T01:12:00")]}})
        state = str(tmp_path / "scheduler.json")
        sch = Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                        state_path=state)

        assert sch.tick()["fired"] == [] and said == []
        clock.advance(5 * 60 + 2)
        assert len(sch.tick()["fired"]) == 1
        assert said == ["Alarm: 1:17 AM."]

        clock.advance(30)
        sch.tick()
        assert len(said) == 1

    def test_a_restart_mid_window_does_not_ring_twice(self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 6, 7, 0, 20).timestamp())
        target = datetime(2026, 9, 6, 7, 0).timestamp() * 1000
        data = FakeAppData({"alarmflow": {"items": [_alarm(7, 0, target)]}})
        state = str(tmp_path / "scheduler.json")

        assert len(Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                             state_path=state).tick()["fired"]) == 1
        clock.advance(20)
        out = Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                        state_path=state).tick()

        assert out["fired"] == [] and out["missed"] == []
        assert said == ["Alarm: 7:00 AM."]

    def test_a_long_outage_still_leaves_exactly_one_missed_record(
            self, tmp_path, sink):
        said, say = sink
        clock = Clock(datetime(2026, 9, 6, 6, 0).timestamp())
        target = datetime(2026, 9, 6, 7, 0).timestamp() * 1000
        data = FakeAppData({"alarmflow": {"items": [_alarm(7, 0, target)]}})
        state = str(tmp_path / "scheduler.json")

        # Seen at 06:00, still armed. Then the box goes down for three hours.
        Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                  state_path=state).tick()
        assert said == []

        clock.t = datetime(2026, 9, 6, 9, 0).timestamp()
        sch = Scheduler([AlarmFlowSource(data)], announce=say, clock=clock,
                        state_path=state)
        out = sch.tick()

        assert len(out["missed"]) == 1 and out["silent"] == []
        assert said == ["You missed an alarm at 7:00 AM."]
        assert data.docs["alarmflow"]["items"][0]["status"] == "missed"

        clock.advance(60)
        sch.tick()
        assert len(said) == 1

    def test_a_reminder_created_after_its_own_due_date_stays_quiet(
            self, tmp_path, sink):
        """"remind me about the dentist yesterday" is not an announcement."""
        said, say = sink
        clock = Clock(datetime(2026, 9, 6, 1, 12).timestamp())
        data = FakeAppData({"calendar": {"tasks": [
            {"id": "t1", "title": "call the fake dentist", "status": "pending",
             "due": "2026-09-05T09:00:00",
             "created_at": "2026-09-06T01:11:00"}]}})
        out = Scheduler([CalendarSource(data, clock=clock)], announce=say,
                        clock=clock,
                        state_path=str(tmp_path / "s.json")).tick()

        assert said == []
        assert len(out["silent"]) == 1
        assert data.docs["calendar"]["tasks"][0]["reminder_status"] == "missed"


# ═══════════════════════════════════════════════════════════════════════════
# The helpers, on their own
# ═══════════════════════════════════════════════════════════════════════════
class TestHelpers:

    def test_created_ms_reads_an_iso_stamp(self):
        when = datetime(2026, 9, 6, 1, 12)
        assert scheduler.created_ms({"created_at": when.isoformat()}) == \
            int(when.timestamp() * 1000)

    def test_created_ms_reads_seconds_and_milliseconds(self):
        when = datetime(2026, 9, 6, 1, 12)
        assert scheduler.created_ms({"created_ts": when.timestamp()}) == \
            int(when.timestamp() * 1000)
        assert scheduler.created_ms({"created_ms": when.timestamp() * 1000}) == \
            int(when.timestamp() * 1000)

    def test_created_ms_is_none_when_the_row_does_not_say(self):
        assert scheduler.created_ms({"id": "m1"}) is None
        assert scheduler.created_ms(None) is None
        assert scheduler.created_ms({"created_at": "not a date"}) is None

    def test_the_item_key_ignores_the_per_occurrence_suffix(self):
        """A daily med must not get a fresh watermark every single day."""
        first = scheduler.occurrence_item_key(
            "medications", {"id": "m1@2026-09-05T08:00:00", "item": {}})
        second = scheduler.occurrence_item_key(
            "medications", {"id": "m1@2026-09-06T08:00:00", "item": {}})
        assert first == second == "medications:m1"

    def test_the_stored_row_id_wins_over_the_occurrence_id(self):
        assert scheduler.occurrence_item_key(
            "calendar", {"id": "x@y", "item": {"id": "t1"}}) == "calendar:t1"
