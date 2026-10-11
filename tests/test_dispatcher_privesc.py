"""The dispatcher and Majordomo must resist privilege escalation.

The dispatcher and Steward tools must resist an escalation chain. Each test
here fails against a vulnerable implementation.

* **Enqueue laundering.** ``POST /agents/tasks`` refuses
  ``target``/``state_changing`` and clamps the budget, so the ``enqueue``
  intent must not re-enter ``create_task`` with all three caller-controlled.
  Otherwise a single message could schedule
  ``steward.control_device {lock.front, unlock}`` with ``max_actions: 999999,
  allow_egress: true``. The limits live at the point a task record is written,
  so the agent-facing door cannot exceed the owner-facing one.
* **Failures land on the record.** Otherwise a permanently broken schedule
  reports itself healthy and can never be disabled.
* **Stale one-shots.** A one-shot ``run_at`` in the past must not be accepted
  and fire on the next tick.

Pure in-process: no bus, no Node-RED, no HA, no sockets.
Run: pytest tests/test_dispatcher_privesc.py -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from puffpai.agents import majordomo as mj
from puffpai.agents.dispatcher import STATUS_FAILED, TaskDispatcher, _budget_from
from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.majordomo import (
    AGENT_ORIGIN_TARGETS,
    MAX_RUN_AT_BACKDATE_SECONDS,
    MAX_TASK_ACTIONS,
    MAX_TASK_SECONDS,
    TASK_ORIGIN_AGENT,
    TASK_ORIGIN_OWNER,
    MajordomoAgent,
    clamp_task_budget,
)


# ── fixtures ────────────────────────────────────────────────────────

class StubBus:
    """AgentBus stand-in that records sends and returns a canned reply."""

    def __init__(self, reply=None, agents=("majordomo", "concierge", "steward")):
        self.reply = reply if reply is not None else {"ok": True}
        self.sent: list[tuple[Envelope, str]] = []
        self._agents = set(agents)

    def send(self, envelope, to):
        self.sent.append((envelope, to))
        return self.reply

    def get(self, name):
        return object() if name in self._agents else None


class RaisingBus(StubBus):
    """A bus whose ``send`` blows up — a permanently unreachable recipient."""

    def send(self, envelope, to):
        self.sent.append((envelope, to))
        raise RuntimeError("recipient is on fire")


@pytest.fixture
def agent(tmp_path):
    return MajordomoAgent(queue_path=str(tmp_path / "majordomo_tasks.jsonl"))


def _enqueue(agent, payload, *, requester="owner", origin="concierge") -> dict:
    """Drive the agent-facing ``enqueue`` intent exactly as the bus would."""
    return agent.handle(Envelope(
        intent="enqueue",
        payload=payload,
        requester=requester,
        origin_agent=origin,
        budget=Budget(max_actions=4, max_seconds=60.0),
        state_changing=True,
    ))


def _soon() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _write_raw_task(agent, **fields) -> str:
    """Put a record on disk directly, bypassing ``create_task``.

    Models a queue file written before the ceiling existed, edited by hand, or
    produced by the exploit while it still worked.
    """
    record = {
        "id": "abcdef123456", "intent": "x", "target": "majordomo",
        "requester": "owner", "payload": {}, "state_changing": True,
        "budget": dict(mj.DEFAULT_TASK_BUDGET),
        "schedule": {"daily_at": "07:00"},
        "next_due": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
        "status": "scheduled", "failure_count": 0, "run_count": 0,
        "last_error": None, "last_run_at": None,
    }
    record.update(fields)
    agent._append_queue(record)
    return record["id"]


# ══════════════════════════════════════════════════════════════════════
#  The enqueue laundering chain
# ══════════════════════════════════════════════════════════════════════

def test_enqueue_cannot_schedule_an_actuator(agent):
    """THE exploit: one message scheduling steward.control_device on a lock.

    The ``enqueue`` intent used to pass ``target`` straight through, so this
    created a real task record that the dispatcher later executed.
    """
    reply = _enqueue(agent, {
        "target": "steward",
        "task_intent": "control_device",
        "task_payload": {"entity_id": "lock.front", "action": "unlock"},
        "run_at": _soon(),
        "budget": {"max_actions": 999999, "max_seconds": 99999,
                   "allow_egress": True},
        "state_changing": True,
    })

    assert reply["ok"] is False
    assert "steward" in reply["error"]
    assert agent.list_tasks() == []          # nothing reached the queue at all


@pytest.mark.parametrize("target", ["steward", "caretaker", "sentinel",
                                    "curator", "../majordomo"])
def test_enqueue_target_is_restricted_to_non_actuators(agent, target):
    reply = _enqueue(agent, {"target": target, "task_intent": "x",
                             "run_at": _soon()})
    assert reply["ok"] is False
    assert agent.list_tasks() == []


def test_enqueue_may_still_schedule_its_own_follow_up_work(agent):
    """The restriction must not break the legitimate use of the intent."""
    reply = _enqueue(agent, {"target": "majordomo", "task_intent": "reminder",
                             "task_payload": {"text": "milk"},
                             "run_at": _soon()})
    assert reply["ok"] is True
    task = reply["task"]
    assert task["target"] == "majordomo"
    assert task["origin"] == TASK_ORIGIN_AGENT
    assert task["state_changing"] is False
    assert task["budget"]["allow_egress"] is False


def test_enqueue_cannot_declare_itself_state_changing(agent):
    reply = _enqueue(agent, {"target": "majordomo", "task_intent": "x",
                             "run_at": _soon(), "state_changing": True})
    assert reply["ok"] is False
    assert "state_changing" in reply["error"]
    assert agent.list_tasks() == []


def test_enqueue_cannot_grant_itself_egress(agent):
    reply = _enqueue(agent, {"target": "majordomo", "task_intent": "x",
                             "run_at": _soon(),
                             "budget": {"allow_egress": True}})
    assert reply["ok"] is False
    assert "egress" in reply["error"]
    assert agent.list_tasks() == []


def test_enqueue_origin_cannot_be_spoofed_from_the_payload(agent):
    """``origin`` is hard-coded by ``handle``; the payload does not vote."""
    reply = _enqueue(agent, {"target": "steward", "task_intent": "x",
                             "run_at": _soon(), "origin": TASK_ORIGIN_OWNER,
                             "task_origin": TASK_ORIGIN_OWNER})
    assert reply["ok"] is False
    assert agent.list_tasks() == []


def test_an_unrecognised_origin_is_treated_as_an_agent(agent):
    """Fail-closed: an origin nobody defined gets the restrictive side."""
    with pytest.raises(ValueError):
        agent.create_task(intent="x", target="steward", run_at=_soon(),
                          origin="definitely-not-a-real-origin")


# ── the budget ceiling, enforced where records are written ──────────

def test_create_task_clamps_an_oversized_budget_for_every_origin(agent):
    """``_normalize_budget`` only coerced types; 999999 sailed through."""
    record = agent.create_task(
        intent="x", target="majordomo", run_at=_soon(),
        budget={"max_actions": 999999, "max_seconds": 99999,
                "allow_egress": True},
        origin=TASK_ORIGIN_OWNER)

    assert record["budget"]["max_actions"] == MAX_TASK_ACTIONS
    assert record["budget"]["max_seconds"] == MAX_TASK_SECONDS
    assert record["budget"]["allow_egress"] is True   # owner may grant egress


@pytest.mark.parametrize("spec,expected", [
    ({"max_actions": 999999}, {"max_actions": MAX_TASK_ACTIONS}),
    ({"max_actions": 0}, {"max_actions": 1}),
    ({"max_actions": -5}, {"max_actions": 1}),
    ({"max_seconds": 10 ** 9}, {"max_seconds": MAX_TASK_SECONDS}),
    ({"max_seconds": 0}, {"max_seconds": 1.0}),
    ({"max_seconds": float("nan")}, {"max_seconds": 60.0}),
    ({"max_seconds": float("inf")}, {"max_seconds": MAX_TASK_SECONDS}),
    # A bool is not a count: int(True) == 1 would have read as a valid budget.
    ({"max_actions": True}, {"max_actions": 4}),
    ({"max_actions": "999999"}, {"max_actions": 4}),
    ({"max_actions": None}, {"max_actions": 4}),
    # Only a literal True grants egress — a truthy string must not.
    ({"allow_egress": "false"}, {"allow_egress": False}),
    ({"allow_egress": 1}, {"allow_egress": False}),
])
def test_clamp_task_budget_matrix(spec, expected):
    got = clamp_task_budget(spec)
    for key, value in expected.items():
        assert got[key] == value, f"{spec} -> {got}"
    assert 1 <= got["max_actions"] <= MAX_TASK_ACTIONS
    assert 1.0 <= got["max_seconds"] <= MAX_TASK_SECONDS


def test_the_dispatcher_reclamps_a_budget_already_on_disk(agent):
    """A record the exploit already wrote must not be cashable after the fix."""
    _write_raw_task(agent, budget={"max_actions": 999999, "max_seconds": 99999,
                                   "allow_egress": True})
    bus = StubBus()
    TaskDispatcher(majordomo=agent, bus=bus).run_once()

    envelope, _ = bus.sent[0]
    assert envelope.budget.max_actions == MAX_TASK_ACTIONS
    assert envelope.budget.max_seconds == MAX_TASK_SECONDS


def test_budget_from_never_exceeds_the_ceiling():
    budget = _budget_from({"budget": {"max_actions": 10 ** 6,
                                      "max_seconds": 10 ** 6}})
    assert isinstance(budget, Budget)
    assert budget.max_actions == MAX_TASK_ACTIONS
    assert budget.max_seconds == MAX_TASK_SECONDS


def test_the_route_and_the_store_agree_on_the_ceiling():
    """One definition. If the route drifts above the store, enqueue laundering reopens."""
    from puffpai import api_server

    assert api_server._TASK_MAX_ACTIONS <= MAX_TASK_ACTIONS
    assert api_server._TASK_MAX_SECONDS <= MAX_TASK_SECONDS
    assert api_server._TASK_INTENT_MAX <= mj.MAX_TASK_INTENT_CHARS


def test_agent_origin_targets_exclude_every_actuator():
    for actuator in ("steward", "caretaker", "sentinel"):
        assert actuator not in AGENT_ORIGIN_TARGETS


# ── stored identifier fields are bounded ────────────────────────────

@pytest.mark.parametrize("field", ["intent", "target", "requester"])
def test_oversized_identifier_fields_are_refused(agent, field):
    kwargs = {"intent": "x", "target": "majordomo", "run_at": _soon()}
    kwargs[field] = "A" * 500
    with pytest.raises(ValueError):
        agent.create_task(**kwargs)


def test_control_characters_are_stripped_from_stored_fields(agent):
    record = agent.create_task(intent="dai\nly_brie\x00fing", target="majordomo",
                               run_at=_soon())
    assert "\n" not in record["intent"] and "\x00" not in record["intent"]


# ══════════════════════════════════════════════════════════════════════
#  A stale one-shot must not fire on the next tick
# ══════════════════════════════════════════════════════════════════════

def test_a_long_past_run_at_is_refused(agent):
    with pytest.raises(ValueError, match="in the past"):
        agent.create_task(intent="x", target="majordomo",
                          run_at="2020-01-01T00:00:00Z")


def test_a_slightly_past_run_at_is_accepted_as_clock_skew(agent):
    """The window exists for skew, not for scheduling in the past."""
    skewed = (datetime.now(timezone.utc)
              - timedelta(seconds=MAX_RUN_AT_BACKDATE_SECONDS // 2)).isoformat()
    record = agent.create_task(intent="x", target="majordomo", run_at=skewed)
    assert record["status"] == "scheduled"


def test_enqueue_cannot_backdate_a_one_shot_either(agent):
    reply = _enqueue(agent, {"target": "majordomo", "task_intent": "x",
                             "run_at": "2020-01-01T00:00:00Z"})
    assert reply["ok"] is False
    assert "past" in reply["error"]


def test_a_recurring_task_can_never_be_backdated(agent):
    """``daily_at`` always resolves forward, so it can never be stale."""
    record = agent.create_task(intent="x", target="majordomo", daily_at="07:00")
    assert mj.parse_run_at(record["next_due"]) > mj.utcnow_dt()


# ══════════════════════════════════════════════════════════════════════
#  Failures have to land on the record
# ══════════════════════════════════════════════════════════════════════

def test_a_raising_send_is_recorded_as_a_failure(agent):
    """(a) The outer per-task guard swallowed this: failure_count stayed 0."""
    task_id = _write_raw_task(agent)
    bus = RaisingBus()

    TaskDispatcher(majordomo=agent, bus=bus).run_once()

    stored = agent.get_task(task_id)
    assert stored["failure_count"] == 1
    assert stored["last_error"] and "RuntimeError" in stored["last_error"]


def test_a_permanently_raising_send_eventually_disables_the_task(agent):
    """The disable counter is decorative unless raises are counted."""
    task_id = _write_raw_task(agent)
    bus = RaisingBus()
    dispatcher = TaskDispatcher(majordomo=agent, bus=bus,
                                max_consecutive_failures=3)

    for _ in range(3):
        agent.update_task(task_id, next_due=datetime(
            2020, 1, 1, tzinfo=timezone.utc).isoformat())
        dispatcher.run_once()

    stored = agent.get_task(task_id)
    assert stored["failure_count"] == 3
    assert stored["status"] == "disabled"

    # ...and a disabled task is not selected again.
    agent.update_task(task_id, next_due=datetime(
        2020, 1, 1, tzinfo=timezone.utc).isoformat())
    assert dispatcher.run_once() == 0
    assert len(bus.sent) == 3


def test_a_raising_send_does_not_stall_the_rest_of_the_tick(agent):
    _write_raw_task(agent, id="aaaaaaaaaaaa")
    _write_raw_task(agent, id="bbbbbbbbbbbb")
    TaskDispatcher(majordomo=agent, bus=RaisingBus()).run_once()

    for task_id in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        assert agent.get_task(task_id)["failure_count"] == 1


def test_a_failed_one_shot_is_not_left_marked_done(agent):
    """(b) ``_advance`` writes ``done`` before the send; a failure must undo it."""
    task_id = _write_raw_task(agent, schedule={"run_at": "2020-01-01T00:00:00Z"})
    bus = StubBus(reply={"ok": False, "error": "flow unreachable"})

    TaskDispatcher(majordomo=agent, bus=bus).run_once()

    stored = agent.get_task(task_id)
    assert stored["status"] == STATUS_FAILED
    assert stored["status"] != "done"
    assert stored["failure_count"] == 1
    assert "flow unreachable" in stored["last_error"]


def test_a_one_shot_denied_by_sentinel_is_not_left_marked_done(agent):
    task_id = _write_raw_task(agent, schedule={"run_at": "2020-01-01T00:00:00Z"})
    bus = StubBus(reply={"ok": False, "denied": True, "by": "sentinel",
                         "verdict": "deny", "reason": "no consent"})

    TaskDispatcher(majordomo=agent, bus=bus).run_once()

    stored = agent.get_task(task_id)
    assert stored["status"] == STATUS_FAILED
    assert "denied by sentinel" in stored["last_error"]


def test_a_failed_one_shot_is_never_retried(agent):
    task_id = _write_raw_task(agent, schedule={"run_at": "2020-01-01T00:00:00Z"})
    bus = StubBus(reply={"ok": False, "error": "boom"})
    dispatcher = TaskDispatcher(majordomo=agent, bus=bus)

    dispatcher.run_once()
    assert dispatcher.run_once() == 0
    assert len(bus.sent) == 1
    assert agent.get_task(task_id)["next_due"] is None


def test_a_successful_one_shot_is_still_marked_done(agent):
    task_id = _write_raw_task(agent, schedule={"run_at": "2020-01-01T00:00:00Z"})
    TaskDispatcher(majordomo=agent, bus=StubBus()).run_once()
    assert agent.get_task(task_id)["status"] == "done"


def test_a_task_judged_unschedulable_is_never_dispatched(agent):
    """(c) ``_advance`` disabled it, then ``_dispatch`` sent it anyway."""
    task_id = _write_raw_task(agent, schedule={"daily_at": "99:99"})
    bus = StubBus()

    TaskDispatcher(majordomo=agent, bus=bus).run_once()

    assert bus.sent == []
    stored = agent.get_task(task_id)
    assert stored["status"] == "disabled"
    assert stored["run_count"] == 0      # it never ran, so it never counted
