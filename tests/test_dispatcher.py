"""Tests for scheduled work — the Majordomo dispatcher (orchestration Phase 2).

Covers the five pieces that must hold with no Node-RED, no Ollama and no clock
dependency:

* due-time selection — one-shot ``run_at`` past/future, recurring ``daily_at``
  rollover across midnight, and the local→UTC conversion the queue stores.
* the queue as a crash-safe store — atomic rewrite, a stale ``.tmp`` left by a
  crash, and consecutive-failure disabling.
* dispatch shape — every task leaves through the bus as a budgeted envelope.
* the dispatcher thread — daemon, clean stop, silent idle ticks.
* the morning briefing end-to-end with a stubbed AutomationClient + stub bus.

Run: pytest tests/test_dispatcher.py -q
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from puffpai.agents import majordomo as mj
from puffpai.agents.dispatcher import TaskDispatcher, envelope_for
from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.majordomo import MajordomoAgent


# ── fixtures ────────────────────────────────────────────────────────

class StubBus:
    """Minimal AgentBus stand-in that records sends and returns a canned reply."""

    def __init__(self, reply=None, agents=("majordomo", "concierge")):
        self.reply = reply if reply is not None else {"ok": True}
        self.sent: list[tuple[Envelope, str]] = []
        self._agents = set(agents)

    def send(self, envelope, to):
        self.sent.append((envelope, to))
        return self.reply

    def get(self, name):
        return object() if name in self._agents else None


@pytest.fixture
def agent(tmp_path):
    """A Majordomo whose queue and briefing file live in a temp directory."""
    return MajordomoAgent(queue_path=str(tmp_path / "majordomo_tasks.jsonl"))


@pytest.fixture
def briefing_path(tmp_path, monkeypatch):
    path = tmp_path / "daily_briefing.json"
    monkeypatch.setattr(mj, "BRIEFING_PATH", str(path))
    return path


def _dispatcher(agent, bus, **kwargs):
    return TaskDispatcher(majordomo=agent, bus=bus, **kwargs)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── schedule parsing / due-time selection ───────────────────────────

def test_run_at_with_an_explicit_offset_is_stored_as_utc():
    assert mj.parse_run_at("2026-08-04T07:00:00+00:00") == _utc(2026, 8, 4, 7, 0)
    assert mj.parse_run_at("2026-08-04T07:00:00Z") == _utc(2026, 8, 4, 7, 0)


def test_naive_run_at_is_read_as_local_wall_clock():
    """A human types local time; the queue stores the UTC instant it means."""
    naive = datetime(2026, 8, 4, 7, 0)
    assert mj.parse_run_at("2026-08-04T07:00:00") == naive.astimezone().astimezone(
        timezone.utc)


@pytest.mark.parametrize("value", ["7:00", "24:00", "07:60", "0700", "", "noon"])
def test_bad_daily_at_is_rejected(value):
    with pytest.raises(ValueError):
        mj.parse_daily_at(value)


@pytest.mark.parametrize("value,expected", [
    ("00:00", (0, 0)), ("07:00", (7, 0)), ("23:59", (23, 59)),
])
def test_daily_at_parsing(value, expected):
    assert mj.parse_daily_at(value) == expected


def test_daily_at_picks_todays_slot_when_it_is_still_ahead():
    after = datetime(2026, 8, 4, 6, 0).astimezone()
    due = mj.next_daily_due(7, 0, after=after)
    local = due.astimezone()
    assert (local.hour, local.minute) == (7, 0)
    assert local.date() == after.date()
    assert due > after


def test_daily_at_rolls_over_to_tomorrow_once_the_slot_has_passed():
    after = datetime(2026, 8, 4, 7, 30).astimezone()
    due = mj.next_daily_due(7, 0, after=after)
    local = due.astimezone()
    assert (local.hour, local.minute) == (7, 0)
    assert local.date() == after.date() + timedelta(days=1)


def test_daily_at_rollover_across_midnight():
    """23:30 local, next slot 00:15 -> tomorrow, not 20 hours ago."""
    after = datetime(2026, 8, 4, 23, 30).astimezone()
    due = mj.next_daily_due(0, 15, after=after)
    local = due.astimezone()
    assert (local.hour, local.minute) == (0, 15)
    assert local.date() == after.date() + timedelta(days=1)
    assert timedelta(0) < due - after < timedelta(hours=1)


def test_a_slot_exactly_now_goes_to_the_next_day():
    """Strictly-after, so advancing a task never re-selects the same instant."""
    after = datetime(2026, 8, 4, 7, 0, 0).astimezone()
    due = mj.next_daily_due(7, 0, after=after)
    assert due.astimezone().date() == after.date() + timedelta(days=1)


def test_compute_next_due_requires_exactly_one_form():
    with pytest.raises(ValueError):
        mj.compute_next_due()
    with pytest.raises(ValueError):
        mj.compute_next_due(run_at="2026-08-04T07:00:00Z", daily_at="07:00")


def test_is_due_selects_only_live_scheduled_tasks():
    now = _utc(2026, 8, 4, 12, 0)
    past = {"status": "scheduled", "next_due": _utc(2026, 8, 4, 11, 0).isoformat()}
    future = {"status": "scheduled", "next_due": _utc(2026, 8, 4, 13, 0).isoformat()}
    assert mj.is_due(past, now) is True
    assert mj.is_due(future, now) is False
    assert mj.is_due({**past, "status": "disabled"}, now) is False
    assert mj.is_due({**past, "status": "done"}, now) is False
    assert mj.is_due({"status": "scheduled"}, now) is False
    assert mj.is_due({"status": "scheduled", "next_due": "not-a-time"}, now) is False


# ── the queue as a task store ───────────────────────────────────────

def test_create_task_writes_a_dispatchable_record(agent):
    record = agent.create_task(intent="daily_briefing", daily_at="07:00",
                               payload={"date": "2026-08-04"})
    assert record["status"] == "scheduled"
    assert record["schedule"] == {"daily_at": "07:00"}
    assert record["budget"] == mj.DEFAULT_TASK_BUDGET
    assert record["failure_count"] == 0
    assert mj.parse_run_at(record["next_due"]) > mj.utcnow_dt()
    assert agent.list_tasks() == [record]


def test_legacy_queue_lines_still_parse_and_are_never_dispatched(agent):
    """spawn_task_worker records predate the task shape: no id, no schedule."""
    agent.spawn_task_worker({"intent": "x", "target": "steward"})
    assert agent.list_queue() != []
    assert agent.list_tasks() == []


def test_oversized_payload_is_refused(agent):
    with pytest.raises(ValueError):
        agent.create_task(intent="x", daily_at="07:00",
                          payload={"blob": "y" * (mj.MAX_PAYLOAD_BYTES + 10)})


def test_update_task_rewrites_the_queue_without_duplicating(agent):
    a = agent.create_task(intent="a", daily_at="07:00")
    b = agent.create_task(intent="b", daily_at="08:00")
    agent.update_task(a["id"], status="disabled", last_error="boom")

    tasks = {t["id"]: t for t in agent.list_tasks()}
    assert len(tasks) == 2
    assert tasks[a["id"]]["status"] == "disabled"
    assert tasks[a["id"]]["last_error"] == "boom"
    assert tasks[b["id"]]["status"] == "scheduled"


def test_update_of_an_unknown_id_is_a_no_op(agent):
    agent.create_task(intent="a", daily_at="07:00")
    assert agent.update_task("ffffffffffff", status="disabled") is None
    assert agent.list_tasks()[0]["status"] == "scheduled"


def test_delete_task(agent):
    record = agent.create_task(intent="a", daily_at="07:00")
    assert agent.delete_task(record["id"]) is True
    assert agent.list_tasks() == []
    assert agent.delete_task(record["id"]) is False


def test_rewrite_is_atomic_and_leaves_no_temp_file(agent, tmp_path):
    record = agent.create_task(intent="a", daily_at="07:00")
    agent.update_task(record["id"], status="done")
    assert not os.path.exists(agent._queue_path + ".tmp")


def test_a_temp_file_left_by_a_crash_is_ignored_and_overwritten(agent):
    """Simulated crash: a half-written .tmp survives; the queue is intact."""
    record = agent.create_task(intent="a", daily_at="07:00")
    stale = agent._queue_path + ".tmp"
    with open(stale, "w", encoding="utf-8") as f:
        f.write('{"id": "deadbeefcafe", "intent": "half-writ')

    # The stale temp file is never read.
    assert [t["id"] for t in agent.list_tasks()] == [record["id"]]
    # ...and the next rewrite replaces it, then consumes it.
    agent.update_task(record["id"], status="done")
    assert not os.path.exists(stale)
    assert agent.list_tasks()[0]["status"] == "done"


def test_a_corrupt_line_does_not_poison_the_queue(agent):
    agent.create_task(intent="a", daily_at="07:00")
    with open(agent._queue_path, "a", encoding="utf-8") as f:
        f.write("{not json}\n")
    assert len(agent.list_tasks()) == 1


# ── dispatch goes through the bus ───────────────────────────────────

#: A one-shot ``run_at`` that ``create_task`` accepts. It must be in the future:
#: a stale one-shot is refused at creation now (see ``reject_stale_run_at``),
#: because a task whose moment already passed fires on the very next tick.
#: ``_due_task`` backdates ``next_due`` afterwards, which is what these tests
#: actually need — an already-due record, not a task created in the past.
_FUTURE_RUN_AT = "2099-01-01T00:00:00Z"


def _due_task(agent, **kwargs):
    """A task whose due time is already in the past."""
    record = agent.create_task(**kwargs)
    agent.update_task(record["id"],
                      next_due=_utc(2020, 1, 1, 0, 0).isoformat())
    return agent.get_task(record["id"])


def test_due_task_is_sent_through_the_bus_as_a_budgeted_envelope(agent):
    bus = StubBus()
    _due_task(agent, intent="run_workflow", target="majordomo", daily_at="07:00",
              payload={"webhook_name": "daily_briefing"},
              budget={"max_actions": 4, "max_seconds": 60, "allow_egress": True})

    assert _dispatcher(agent, bus).run_once() == 1

    envelope, to = bus.sent[0]
    assert to == "majordomo"
    assert envelope.intent == "run_workflow"
    assert envelope.origin_agent == "dispatcher"
    assert envelope.state_changing is True      # Sentinel review applies
    assert envelope.payload == {"webhook_name": "daily_briefing"}
    assert envelope.budget.max_actions == 4
    assert envelope.budget.max_seconds == 60.0
    assert envelope.budget.allow_egress is True


def test_egress_is_denied_unless_the_task_asked_for_it(agent):
    bus = StubBus()
    _due_task(agent, intent="x", daily_at="07:00")
    _dispatcher(agent, bus).run_once()
    envelope, _ = bus.sent[0]
    assert envelope.budget.allow_egress is False
    assert envelope.budget.max_actions == 4
    assert envelope.budget.max_seconds == 60.0


def test_envelope_for_defaults_are_fail_closed():
    envelope = envelope_for({"intent": "x"})
    assert envelope.budget.allow_egress is False
    assert envelope.state_changing is True


def test_a_future_task_is_not_dispatched(agent):
    bus = StubBus()
    agent.create_task(intent="x", daily_at="07:00")
    assert _dispatcher(agent, bus).run_once() == 0
    assert bus.sent == []


def test_one_shot_is_marked_done_and_never_runs_twice(agent):
    bus = StubBus()
    record = _due_task(agent, intent="x", run_at=_FUTURE_RUN_AT)
    dispatcher = _dispatcher(agent, bus)

    assert dispatcher.run_once() == 1
    assert dispatcher.run_once() == 0
    stored = agent.get_task(record["id"])
    assert stored["status"] == "done"
    assert stored["next_due"] is None
    assert stored["run_count"] == 1
    assert len(bus.sent) == 1


def test_recurring_task_advances_to_the_next_slot(agent):
    bus = StubBus()
    record = _due_task(agent, intent="x", daily_at="07:00")
    dispatcher = _dispatcher(agent, bus)

    assert dispatcher.run_once() == 1
    stored = agent.get_task(record["id"])
    assert stored["status"] == "scheduled"
    assert mj.parse_run_at(stored["next_due"]) > mj.utcnow_dt()
    # ...and the same tick does not fire it again.
    assert dispatcher.run_once() == 0


def test_the_advance_is_committed_before_the_send(agent):
    """At-most-once: a crash inside bus.send must not leave the task due."""
    seen: dict = {}

    class CrashingBus(StubBus):
        def send(self, envelope, to):
            seen["status_during_send"] = agent.get_task(record["id"])["status"]
            raise RuntimeError("bus exploded mid-dispatch")

    record = _due_task(agent, intent="x", run_at=_FUTURE_RUN_AT)
    dispatcher = _dispatcher(agent, CrashingBus())

    dispatcher.run_once()                    # the failure is contained
    # The advance was already persisted when the send blew up, so the task is
    # not due again — that is the at-most-once guarantee this test is about.
    assert seen["status_during_send"] == "done"
    assert agent.get_task(record["id"])["next_due"] is None
    assert dispatcher.run_once() == 0


# ── failure handling ────────────────────────────────────────────────

@pytest.mark.parametrize("reply,fragment", [
    ({"ok": False, "error": "flow unreachable"}, "flow unreachable"),
    ({"ok": False, "denied": True, "by": "sentinel", "verdict": "deny",
      "reason": "quarantined"}, "denied by sentinel"),
    ({"ok": False, "aborted": "budget", "reason": "no actions left"}, "aborted"),
])
def test_failed_replies_are_recorded_on_the_task(agent, reply, fragment):
    record = _due_task(agent, intent="x", daily_at="07:00")
    _dispatcher(agent, StubBus(reply=reply)).run_once()

    stored = agent.get_task(record["id"])
    assert stored["failure_count"] == 1
    assert fragment in stored["last_error"]
    assert stored["status"] == "scheduled"     # still retried tomorrow


def test_a_success_clears_the_failure_counter(agent):
    record = _due_task(agent, intent="x", daily_at="07:00")
    _dispatcher(agent, StubBus(reply={"ok": False, "error": "boom"})).run_once()
    assert agent.get_task(record["id"])["failure_count"] == 1

    agent.update_task(record["id"], next_due=_utc(2020, 1, 1).isoformat())
    _dispatcher(agent, StubBus(reply={"ok": True})).run_once()
    stored = agent.get_task(record["id"])
    assert stored["failure_count"] == 0
    assert stored["last_error"] is None


def test_a_task_is_disabled_after_n_consecutive_failures(agent):
    record = _due_task(agent, intent="x", daily_at="07:00")
    bus = StubBus(reply={"ok": False, "error": "boom"})
    dispatcher = _dispatcher(agent, bus, max_consecutive_failures=3)

    for _ in range(3):
        agent.update_task(record["id"], next_due=_utc(2020, 1, 1).isoformat())
        dispatcher.run_once()

    stored = agent.get_task(record["id"])
    assert stored["failure_count"] == 3
    assert stored["status"] == "disabled"
    # A disabled task is no longer selected, however overdue it looks.
    agent.update_task(record["id"], next_due=_utc(2020, 1, 1).isoformat())
    assert dispatcher.run_once() == 0
    assert len(bus.sent) == 3


def test_one_broken_task_does_not_stall_the_others(agent, monkeypatch):
    bus = StubBus()
    broken = _due_task(agent, intent="broken", daily_at="07:00")
    _due_task(agent, intent="fine", daily_at="08:00")

    original = agent.update_task

    def flaky(task_id, **fields):
        if task_id == broken["id"]:
            raise OSError("queue write failed")
        return original(task_id, **fields)

    monkeypatch.setattr(agent, "update_task", flaky)
    assert _dispatcher(agent, bus).run_once() == 1
    assert [e.intent for e, _ in bus.sent] == ["fine"]


def test_run_once_degrades_when_the_shared_bus_is_unavailable(agent, monkeypatch):
    """With no bus injected the dispatcher asks agent_events for the SHARED one."""
    from puffpai import agent_events
    _due_task(agent, intent="x", daily_at="07:00")
    monkeypatch.setattr(agent_events, "get_bus", lambda: None)
    assert TaskDispatcher(majordomo=agent).run_once() == 0


def test_run_once_is_a_no_op_without_a_majordomo():
    bus = StubBus(agents=())
    assert TaskDispatcher(bus=bus).run_once() == 0


# ── the thread ──────────────────────────────────────────────────────

def test_thread_is_daemon_and_stops_cleanly(agent):
    dispatcher = _dispatcher(agent, StubBus(), tick_seconds=0.01)
    dispatcher.start()
    try:
        assert dispatcher.status()["running"] is True
        assert dispatcher._thread.daemon is True
        dispatcher.start()  # idempotent
    finally:
        dispatcher.stop(timeout=2.0)
    assert dispatcher.status()["running"] is False


def test_idle_ticks_log_nothing(agent, caplog):
    agent.create_task(intent="x", daily_at="07:00")   # scheduled, not yet due
    dispatcher = _dispatcher(agent, StubBus())
    with caplog.at_level("INFO", logger="puffpai.agents.dispatcher"):
        for _ in range(5):
            assert dispatcher.run_once() == 0
    assert caplog.records == []


def test_a_dispatch_logs_exactly_one_info_line(agent, caplog):
    _due_task(agent, intent="x", daily_at="07:00")
    dispatcher = _dispatcher(agent, StubBus())
    with caplog.at_level("INFO", logger="puffpai.agents.dispatcher"):
        dispatcher.run_once()
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1
    assert "dispatching task" in infos[0].getMessage()


def test_the_tick_loop_survives_a_failing_run_once(agent, monkeypatch):
    dispatcher = _dispatcher(agent, StubBus(), tick_seconds=0.01)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(dispatcher, "run_once", boom)
    dispatcher.start()
    try:
        deadline = mj.utcnow_dt() + timedelta(seconds=2)
        while len(calls) < 2 and mj.utcnow_dt() < deadline:
            pass
    finally:
        dispatcher.stop(timeout=2.0)
    assert len(calls) >= 2      # it kept ticking


# ── morning briefing end-to-end ─────────────────────────────────────

class StubAutomation:
    """AutomationClient stand-in: records calls, returns a canned flow result."""

    def __init__(self, ok=True, data=None):
        self.calls: list[tuple[str, dict]] = []
        self._ok, self._data = ok, data if data is not None else {
            "weather": "92F and clear", "calendar": "dentist at 3",
            "news": ["a", "b"],
        }

    def call(self, webhook_name, payload):
        self.calls.append((webhook_name, payload))
        return self._ok, self._data


def test_seed_briefing_task_is_idempotent(agent):
    first = mj.seed_briefing_task("07:00", majordomo=agent)
    second = mj.seed_briefing_task("07:00", majordomo=agent)
    assert first["id"] == second["id"]
    assert len(agent.list_tasks()) == 1
    assert first["schedule"] == {"daily_at": "07:00"}
    assert first["budget"]["allow_egress"] is True
    assert first["intent"] == "daily_briefing"


def test_briefing_calls_the_flow_and_asks_the_concierge(agent, briefing_path):
    automation = StubAutomation()
    agent._automation = automation
    bus = StubBus(reply={"ok": True, "answer": "Good morning — it's hot."})
    agent.bind_bus(bus)

    envelope = Envelope(intent="daily_briefing", requester="owner",
                        budget=Budget(max_actions=4, max_seconds=180.0,
                                      allow_egress=True),
                        state_changing=True)
    reply = agent.handle(envelope)

    assert reply["ok"] is True
    assert reply["text"] == "Good morning — it's hot."
    assert automation.calls[0][0] == "daily_briefing"
    # The summary hop went through the bus, read-only, to the Concierge.
    sub, to = bus.sent[0]
    assert to == "concierge"
    # "summarize", NOT "ask": the payload is a report to write prose about, and
    # it quotes live device readings. As an "ask" the Concierge resolved those
    # readings into a household command and actuated one.
    assert sub.intent == "summarize"
    assert sub.origin_agent == "majordomo"
    assert sub.state_changing is False
    assert "92F and clear" in sub.payload["text"]
    # Stored for the dashboard/chat -- nothing is spoken (announce is Phase 3).
    stored = json.loads(briefing_path.read_text(encoding="utf-8"))
    assert stored["text"] == "Good morning — it's hot."
    assert stored["source"] == "nodered:daily_briefing"


def test_briefing_falls_back_to_the_flow_output_with_no_llm(agent, briefing_path):
    agent._automation = StubAutomation()
    agent.bind_bus(StubBus(reply={"ok": True, "answer": None}))

    reply = agent.handle(Envelope(intent="daily_briefing",
                                  budget=Budget(max_actions=4, allow_egress=True)))
    assert reply["ok"] is True
    assert "92F and clear" in reply["text"]


def test_briefing_reports_a_flow_failure_without_storing(agent, briefing_path):
    agent._automation = StubAutomation(ok=False, data="engine unreachable")
    agent.bind_bus(StubBus())

    reply = agent.handle(Envelope(intent="daily_briefing",
                                  budget=Budget(max_actions=4, allow_egress=True)))
    assert reply["ok"] is False
    assert "engine unreachable" in reply["error"]
    assert not briefing_path.exists()


def test_briefing_needs_an_egress_budget(agent, briefing_path):
    """The flow call is egress-class: a no-egress budget aborts it via the bus."""
    from puffpai.agents.envelope import BudgetExceeded
    agent._automation = StubAutomation()
    agent.bind_bus(StubBus())
    with pytest.raises(BudgetExceeded):
        agent.handle(Envelope(intent="daily_briefing",
                              budget=Budget(max_actions=4, allow_egress=False)))
    assert not briefing_path.exists()


def test_seeded_briefing_task_runs_end_to_end(agent, briefing_path):
    """Seed -> due -> dispatched -> flow called -> briefing stored."""
    automation = StubAutomation()
    agent._automation = automation

    class RoutingBus(StubBus):
        """Delivers to the real Majordomo, the way the real bus would."""

        def send(self, envelope, to):
            self.sent.append((envelope, to))
            if to == "majordomo":
                return agent.handle(envelope)
            return {"ok": True, "answer": "Good morning."}

    bus = RoutingBus()
    agent.bind_bus(bus)
    record = mj.seed_briefing_task("07:00", majordomo=agent)
    agent.update_task(record["id"], next_due=_utc(2020, 1, 1).isoformat())

    assert _dispatcher(agent, bus).run_once() == 1
    assert automation.calls[0][0] == "daily_briefing"
    assert json.loads(briefing_path.read_text(encoding="utf-8"))["text"] \
        == "Good morning."
    # ...and the task rolled forward rather than failing.
    stored = agent.get_task(record["id"])
    assert stored["status"] == "scheduled"
    assert stored["failure_count"] == 0


def test_read_briefing_degrades_to_empty(briefing_path):
    assert mj.read_briefing() == {}
    briefing_path.write_text("{not json}", encoding="utf-8")
    assert mj.read_briefing() == {}


# ── queue path resolution (deployment integrity) ────────────────────
#
# The appliance runs under ProtectSystem=strict, so a package-relative queue is
# read-only there: scheduled work would look accepted and never persist.

def _config_with_state_dir(tmp_path, state_dir):
    from puffpai.config import _build_config
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "puffpai_config.json").write_text(
        json.dumps({"paths": {"state_dir": str(state_dir)}}))
    return _build_config(str(tmp_path))


@pytest.fixture
def unresolved_queue(monkeypatch):
    monkeypatch.setattr(mj, "QUEUE_PATH", None)
    monkeypatch.setattr(mj, "BRIEFING_PATH", None)
    yield
    monkeypatch.setattr(mj, "QUEUE_PATH", None)
    monkeypatch.setattr(mj, "BRIEFING_PATH", None)


def test_absolute_state_dir_places_the_queue(tmp_path, monkeypatch, unresolved_queue):
    state_dir = tmp_path / "data" / "puffpai" / "state"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "ignored"))
    monkeypatch.setattr("puffpai.config._instance",
                        _config_with_state_dir(tmp_path, state_dir))

    assert mj.get_queue_path() == str(state_dir / "majordomo_tasks.jsonl")
    assert mj.get_briefing_path() == str(state_dir / "daily_briefing.json")

    record = MajordomoAgent().create_task(intent="x", daily_at="07:00")
    assert (state_dir / "majordomo_tasks.jsonl").is_file()
    assert MajordomoAgent().get_task(record["id"]) is not None


def test_env_var_places_the_queue_when_config_is_silent(
        tmp_path, monkeypatch, unresolved_queue):
    from puffpai.config import _build_config
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "puffpai_config.json").write_text("{}")
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("puffpai.config._instance", _build_config(str(tmp_path)))

    assert mj.get_queue_path() == str(tmp_path / "state" / "majordomo_tasks.jsonl")


# ── the config-unavailable fallback ─────────────────────────────────
#
# This branch fires exactly when config is degraded — which is when a queue of
# scheduled background work can least afford to land somewhere nothing reads
# back. It used to read $PUFFPAI_DATA_DIR raw (no strip, no "~" expansion, no
# repo-root join), so the documented relative value "data" put the queue beside
# whatever directory systemd started the service in, and "~/puff_state" made a
# literal "~" directory. It now goes through puffpai.statedir, which reaches the
# canon; these cases pin the three ways raw reading differed from it.

@pytest.fixture
def broken_config(monkeypatch):
    """Make ``get_config()`` raise, leaving only the statedir fallback."""
    def _raise(*_a, **_kw):
        raise RuntimeError("config is broken")
    monkeypatch.setattr("puffpai.config.get_config", _raise)


def test_config_failure_joins_a_relative_env_value_to_the_repo_root(
        monkeypatch, unresolved_queue, broken_config):
    """A relative value belongs to the repo, never to the process CWD."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")

    assert mj.get_queue_path() == os.path.join(
        mj._REPO_ROOT, "relstate", "majordomo_tasks.jsonl")


def test_config_failure_expands_a_tilde_env_value(
        monkeypatch, unresolved_queue, broken_config):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")

    resolved = mj.get_queue_path()
    assert resolved == os.path.join(
        os.path.expanduser("~"), "puff_state", "majordomo_tasks.jsonl")
    assert "~" not in resolved


@pytest.mark.parametrize("env_value", ["", "   ", "\t\n"])
def test_config_failure_treats_a_blank_env_value_as_unset(
        env_value, monkeypatch, unresolved_queue, broken_config):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

    assert mj.get_queue_path() == os.path.join(
        mj._DEFAULT_DATA_DIR, "majordomo_tasks.jsonl")


def test_config_failure_strips_a_padded_env_value(
        tmp_path, monkeypatch, unresolved_queue, broken_config):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", f"  {tmp_path / 'state'}  ")

    assert mj.get_queue_path() == str(
        tmp_path / "state" / "majordomo_tasks.jsonl")


# ── the default's move to <repo>/data, and legacy adoption ──────────
#
# The env-unset default is the package-relative src/puffpai/data moved to
# <repo>/data, converging with agent_events, consent_gate,
# consent_requests and adguard_controller. It has to: the appliance unit's
# ReadWritePaths covers /opt/puffpai/data and never covered
# /opt/puffpai/src/puffpai/data, so a queue defaulting there could not persist
# scheduled work at all. A queue a dev box already holds at the old path must
# not be orphaned by the move, so the old FILE is adopted in place — read and
# rewritten where it already is, never copied, never moved. Adoption is
# suppressed by any explicit choice (paths.state_dir or $PUFFPAI_DATA_DIR),
# tested as "is it set", never as "did the directory move".

@pytest.fixture(autouse=True)
def _neutralized_legacy_state(monkeypatch, tmp_path):
    """Neutralise the legacy directory for EVERY case in this module.

    Adoption is opt-in per test — the cases that want it repoint the attribute
    at a file they seeded themselves. Without this, any case exercising the
    env-unset default would silently depend on whether this working tree
    happens to hold src/puffpai/data/majordomo_tasks.jsonl, and would flip its
    answer the day someone adds or deletes that file.
    """
    monkeypatch.setattr(mj, "_LEGACY_DATA_DIR", str(tmp_path / "absent_legacy"))


def _silent_config(monkeypatch, *, state_dir="", repo_root=None):
    """Install a config stub whose paths are exactly as given (blank = unset)."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(
        paths=SimpleNamespace(state_dir=state_dir),
        REPO_ROOT=repo_root or mj._REPO_ROOT,
    )
    monkeypatch.setattr("puffpai.config._instance", cfg)
    return cfg


def _seed_legacy(tmp_path, monkeypatch, *, record=None):
    """Seed a legacy queue under tmp_path and point the module at it.

    Returns ``(legacy_file, new_data_dir)``. The new data dir deliberately does
    not exist, which is the only state in which adoption may fire.
    """
    record = record or {"id": "aaaaaaaaaaaa", "intent": "historic",
                        "status": "done", "origin": "owner"}
    legacy_dir = tmp_path / "src" / "puffpai" / "data"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy = legacy_dir / "majordomo_tasks.jsonl"
    legacy.write_text(json.dumps(record) + "\n", encoding="utf-8")
    new_dir = tmp_path / "data"
    monkeypatch.setattr(mj, "_LEGACY_DATA_DIR", str(legacy_dir))
    monkeypatch.setattr(mj, "_DEFAULT_DATA_DIR", str(new_dir))
    return legacy, new_dir


def test_env_unset_lands_in_the_repo_root_data_dir(monkeypatch, unresolved_queue):
    """The default is <repo>/data — not the old package-relative src/puffpai/data."""
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _silent_config(monkeypatch)

    assert mj._DEFAULT_DATA_DIR == os.path.join(mj._REPO_ROOT, "data")
    assert mj.get_queue_path() == os.path.join(
        mj._REPO_ROOT, "data", "majordomo_tasks.jsonl")
    assert mj.get_briefing_path() == os.path.join(
        mj._REPO_ROOT, "data", "daily_briefing.json")


def test_legacy_queue_is_adopted_and_rewritten_in_place(
        tmp_path, monkeypatch, unresolved_queue):
    """Queued work is kept, and new tasks join it where it already lives.

    Nothing may be created at the new path — a copy would fork the queue (the
    dispatcher would run tasks from one and the API would show the other), and
    a move would take a migration decision out of the operator's hands.
    """
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert mj.get_queue_path() == str(legacy)

    record = MajordomoAgent().create_task(intent="fresh", daily_at="07:00")

    lines = legacy.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "the pre-existing queue must still be there"
    assert json.loads(lines[0])["intent"] == "historic"
    assert json.loads(lines[1])["id"] == record["id"]
    assert not new_dir.exists(), "nothing may be written at the new path"


def test_a_queue_at_the_new_path_wins_over_legacy(
        tmp_path, monkeypatch, unresolved_queue):
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    new_dir.mkdir()
    (new_dir / "majordomo_tasks.jsonl").write_text("", encoding="utf-8")
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert mj.get_queue_path() == str(new_dir / "majordomo_tasks.jsonl")


def test_adoption_is_per_file_not_per_directory(
        tmp_path, monkeypatch, unresolved_queue):
    """A queue at the old path is adopted even though <repo>/data exists.

    This is why adoption cannot be the canon's directory-level ``legacy=``:
    every checkout ships a <repo>/data, so a directory probe never fires.
    """
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    new_dir.mkdir()  # the directory exists; the queue file does not
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert mj.get_queue_path() == str(legacy)


def test_an_absolute_env_dir_suppresses_adoption(
        tmp_path, monkeypatch, unresolved_queue):
    legacy, _new_dir = _seed_legacy(tmp_path, monkeypatch)
    env_dir = tmp_path / "state"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(env_dir))
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    resolved = mj.get_queue_path()
    assert resolved == str(env_dir / "majordomo_tasks.jsonl")
    assert resolved != str(legacy)


@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_env_pointed_at_the_default_dir_still_suppresses_adoption(
        form, tmp_path, monkeypatch, unresolved_queue):
    """Rule: the env test is "is it set", never a comparison of resolved dirs.

    An operator who points $PUFFPAI_DATA_DIR at the default data dir — the
    absolute <repo>/data, or the relative "data", which joins to the repo root
    — resolves to the very directory the module ships with. Deciding by path
    comparison would read that as "unset", adopt, and then REWRITE the queue in
    the read-only code tree while an env var explicitly said otherwise.
    """
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    monkeypatch.setenv("PUFFPAI_DATA_DIR",
                       str(new_dir) if form == "absolute" else "data")
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    resolved = mj.get_queue_path()
    assert resolved == str(new_dir / "majordomo_tasks.jsonl")
    assert resolved != str(legacy)


def test_a_configured_state_dir_suppresses_adoption(
        tmp_path, monkeypatch, unresolved_queue):
    """paths.state_dir is an explicit choice too, even pointing at the default."""
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _silent_config(monkeypatch, state_dir=str(new_dir), repo_root=str(tmp_path))

    resolved = mj.get_queue_path()
    assert resolved == str(new_dir / "majordomo_tasks.jsonl")
    assert resolved != str(legacy)


@pytest.mark.parametrize("env_value", ["", "   ", "\t\n"])
def test_a_blank_env_value_counts_as_unset_for_adoption(
        env_value, tmp_path, monkeypatch, unresolved_queue):
    legacy, _new_dir = _seed_legacy(tmp_path, monkeypatch)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert mj.get_queue_path() == str(legacy)


def test_adoption_survives_a_broken_config(
        tmp_path, monkeypatch, unresolved_queue, broken_config):
    """The degraded tier adopts too — that is where losing the queue hurts most."""
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)

    assert mj.get_queue_path() == str(legacy)
    assert not new_dir.exists()


# ── config section ──────────────────────────────────────────────────

def test_dispatcher_config_defaults_and_overrides(tmp_path):
    from puffpai.config import _build_config
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "puffpai_config.json").write_text("{}")
    cfg = _build_config(str(tmp_path))
    assert (cfg.dispatcher.enabled, cfg.dispatcher.tick_seconds,
            cfg.dispatcher.max_consecutive_failures) == (True, 30, 5)

    (tmp_path / "config" / "puffpai_config.json").write_text(json.dumps(
        {"dispatcher": {"enabled": False, "tick_seconds": 5,
                        "max_consecutive_failures": 2}}))
    cfg = _build_config(str(tmp_path))
    assert cfg.dispatcher.enabled is False
    assert cfg.dispatcher.tick_seconds == 5
    assert cfg.dispatcher.max_consecutive_failures == 2


def test_start_dispatcher_is_disabled_by_config(monkeypatch):
    from puffpai.agents import dispatcher as disp

    class Cfg:
        class dispatcher:  # noqa: N801 - mirrors the config attribute name
            enabled = False

    monkeypatch.setattr(disp, "_dispatcher", None)
    monkeypatch.setattr("puffpai.config.get_config", lambda: Cfg)
    assert disp.start_dispatcher() is None
