"""An internal agent-to-agent message is prose, never a household command.

Two defects, both live on the branch these tests were written against, both
reachable from the Majordomo's ordinary morning briefing.

1. ACTUATION. ``majordomo._summarize_briefing`` asked the Concierge to write
   prose about a report whose BODY quotes device readings — "Hallway Thermostat
   reads 68." Sent as ``intent="ask"``, that text went through the Concierge's
   normal intent resolution, which read the sentence as a request and dispatched
   ``set_climate`` at 68F to ``climate.hallway_thermostat``, state_changing.
   Nobody asked for a temperature change; a background summarisation job moved
   the setpoint. No clarification is involved — it is purely the report being
   read as an instruction.

2. THE PENDING-ASK SLOT. ``_converse`` claimed the pending clarification for any
   non-blank text regardless of who sent it, so the same briefing consumed a
   human's half-finished conversation and ``_human_only`` prefixed their earlier
   request onto the briefing prompt.

The guard is an origin ALLOWLIST (:data:`RESOLVING_ORIGINS`), deliberately not
``HUMAN_ORIGINS``: the real chat front door stamps ``"api_server"``, which is
outside ``HUMAN_ORIGINS`` on purpose so the Steward's siren gate stays shut.
Both halves of that are pinned below — internal envelopes stay inert AND real
user messages keep working, because a guard that broke chat would be worse than
the bug.

Pure in-process; no HA, no sockets, no LLM.
Run: pytest tests/test_concierge_internal_origin.py -q
"""

from __future__ import annotations

import time

import pytest

from puffpai.agents.concierge import (PROSE_ONLY_INTENTS, RESOLVING_ORIGINS,
                                      ConciergeAgent)
from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.steward import HUMAN_ORIGINS

HOUSE = [
    {"entity_id": "light.porch_light", "name": "Porch Light", "state": "off"},
    {"entity_id": "light.kitchen_light", "name": "Kitchen Light", "state": "on"},
    {"entity_id": "climate.hallway_thermostat",
     "name": "Hallway Thermostat", "state": "heat"},
]

#: The real briefing prompt shape: an instruction to summarise, then a report
#: body quoting readings. The "68" the old code actuated on is in the BODY.
BRIEFING = ("Summarize this household morning report in a few warm, plain "
            "sentences:\nPorch Light is off. Kitchen Light is on. "
            "Hallway Thermostat reads 68.")


class StubBus:
    """Records every envelope the Concierge sends; answers device lists."""

    def __init__(self):
        self.sent: list[tuple[str, Envelope]] = []

    def get(self, name):
        return object() if name in ("steward", "sentinel", "archivist",
                                    "majordomo") else None

    def send(self, envelope, to):
        self.sent.append((to, envelope))
        if envelope.intent == "list_devices":
            return {"ok": True, "devices": list(HOUSE)}
        return {"ok": True}

    def actuating(self) -> list[Envelope]:
        return [e for _, e in self.sent if e.state_changing]


class StubEngine:
    """Stands in for the LLM: records what it was asked, returns prose."""

    def __init__(self, answer="It's a calm morning."):
        self.answer = answer
        self.seen: list[str] = []

    def run(self, text, history=None):
        self.seen.append(text)
        return self.answer


def concierge(engine=None) -> tuple[ConciergeAgent, StubBus]:
    bus = StubBus()
    agent = ConciergeAgent(engine=engine)
    agent.bind_bus(bus)
    return agent, bus


def briefing_envelope(intent="summarize") -> Envelope:
    """The Majordomo's real summary hop."""
    return Envelope(
        intent=intent,
        payload={"text": BRIEFING, "mode": "chat"},
        requester="owner",
        origin_agent="majordomo",
        budget=Budget(max_actions=2, max_seconds=45.0, allow_egress=False),
        state_changing=False,
    )


def human_envelope(text, intent="chat") -> Envelope:
    """What the dashboard/voice front door actually sends."""
    return Envelope(
        intent=intent,
        payload={"text": text},
        requester="owner",
        origin_agent="api_server",
        budget=Budget(max_actions=6, max_seconds=20.0),
    )


def pending(agent, question="Which porch light did you mean?") -> None:
    agent._pending_ask = {
        "goal": "turn on the porch light",
        "request": "turn on the porch light",
        "question": question,
        "ts": time.monotonic(),
    }


# ── defect 1: the briefing must not actuate ─────────────────────────

@pytest.mark.parametrize("intent", ["summarize", "ask", "chat", "user_message"])
def test_a_briefing_from_the_majordomo_never_actuates(intent):
    """The regression, at every intent the Concierge converses on.

    ``summarize`` is what the Majordomo sends now; the other three are pinned
    because the ORIGIN check has to hold on its own. A future caller that
    reaches for "ask" again must not re-open this.
    """
    agent, bus = concierge()
    reply = agent.handle(briefing_envelope(intent))

    assert bus.actuating() == [], (
        f"internal {intent!r} actuated: "
        f"{[(e.intent, e.payload) for e in bus.actuating()]}")
    assert reply["intent"] is None
    assert reply["targets"] == []
    assert reply["handled"] is False


def test_the_thermostat_reading_in_the_report_body_is_not_a_setpoint():
    """Name the exact observed failure: set_climate 68F on the hallway unit."""
    agent, bus = concierge()
    agent.handle(briefing_envelope())

    climate = [e for _, e in bus.sent if e.intent == "set_climate"]
    assert climate == [], f"briefing produced a setpoint change: {climate}"
    assert not any("climate.hallway_thermostat" in str(e.payload)
                   for _, e in bus.sent)


def test_the_briefing_reaches_no_agent_at_all():
    """Not merely 'nothing state-changing' — the resolver never runs.

    A read-only probe would still be the Concierge treating a report as a
    question about the house, and it still costs the envelope's budget.
    """
    agent, bus = concierge()
    agent.handle(briefing_envelope())
    assert bus.sent == []


# ── defect 2: the human's pending ask is untouchable ────────────────

def test_a_briefing_does_not_consume_a_humans_pending_clarification():
    agent, _ = concierge()
    pending(agent)
    assert agent.has_pending_ask()

    agent.handle(briefing_envelope())

    assert agent.has_pending_ask(), (
        "a background job spent the human's clarification slot")


def test_the_pending_ask_survives_intact_not_merely_present():
    """The stash must be the SAME stash — not refreshed, not rewritten."""
    agent, _ = concierge()
    pending(agent)
    before = dict(agent._pending_ask)

    agent.handle(briefing_envelope())

    assert agent._pending_ask == before


def test_the_human_can_still_answer_after_a_briefing_interleaves():
    """End to end: the person's turn still lands on their own question."""
    agent, bus = concierge()
    pending(agent)

    agent.handle(briefing_envelope())          # background job lands mid-conversation
    agent.handle(human_envelope("the front one"))

    assert not agent.has_pending_ask(), "the human's answer claimed the slot"


def test_the_briefing_text_is_never_folded_into_a_humans_request():
    """``_human_only`` must not prefix the human's goal onto agent prose."""
    engine = StubEngine()
    agent, _ = concierge(engine)
    pending(agent)

    agent.handle(briefing_envelope())

    assert engine.seen, "the engine should still have been asked for prose"
    assert "turn on the porch light" not in engine.seen[0]
    assert engine.seen[0] == BRIEFING


# ── the summarisation still has to work ─────────────────────────────

def test_the_majordomo_still_gets_its_prose_back():
    """Inert must not mean useless — the briefing is the whole point."""
    engine = StubEngine("Good morning — the house is quiet.")
    agent, _ = concierge(engine)

    reply = agent.handle(briefing_envelope())

    assert reply["ok"] is True
    assert reply["answer"] == "Good morning — the house is quiet."
    assert "Hallway Thermostat reads 68." in engine.seen[0]


def test_an_engine_less_box_degrades_to_no_answer_not_an_error():
    """Cold model / engine-less box: the Majordomo falls back to flat text."""
    agent, _ = concierge()
    reply = agent.handle(briefing_envelope())
    assert reply["ok"] is True
    assert reply["answer"] is None


# ── the guard must not break the real front door ────────────────────

def test_a_real_user_message_still_actuates():
    """The other half of the fix. A guard that broke chat would be worse."""
    agent, bus = concierge()
    reply = agent.handle(human_envelope("set the hallway thermostat to 72"))

    assert reply["intent"] == "set_climate"
    assert reply["targets"] == ["climate.hallway_thermostat"]
    assert reply["handled"] is True
    assert len(bus.actuating()) == 1


def test_a_human_reply_still_claims_the_pending_ask():
    agent, _ = concierge()
    pending(agent)
    agent.handle(human_envelope("the front one"))
    assert not agent.has_pending_ask()


@pytest.mark.parametrize("origin", sorted(HUMAN_ORIGINS))
def test_every_human_origin_still_resolves(origin):
    agent, bus = concierge()
    env = human_envelope("turn on the porch light")
    env.origin_agent = origin
    agent.handle(env)
    assert len(bus.actuating()) == 1, f"{origin!r} lost the ability to actuate"


# ── the predicate itself ────────────────────────────────────────────

def test_api_server_resolves_without_being_a_human_origin():
    """Why this is a second set and not a widened ``HUMAN_ORIGINS``.

    The chat front door is outside ``HUMAN_ORIGINS`` deliberately: the Steward's
    always-confirm siren gate keys on that set, so chat may propose a siren but
    never fire one. Widening it to fix resolution would hand chat the siren.
    """
    assert "api_server" in RESOLVING_ORIGINS
    assert "api_server" not in HUMAN_ORIGINS
    assert HUMAN_ORIGINS <= RESOLVING_ORIGINS


@pytest.mark.parametrize("origin", ["majordomo", "concierge", "steward",
                                    "sentinel", "archivist", "caretaker",
                                    "toolsmith", "dispatcher", "announcer"])
def test_no_team_agent_can_resolve(origin):
    """An agent stamps its own name, so it cannot lie its way into the set."""
    assert origin not in RESOLVING_ORIGINS


def test_the_guard_is_fail_closed_for_an_unknown_origin():
    """The ``Envelope`` default is ``"system"`` — an origin nobody chose.

    An allowlist means a sender nobody thought about is answered, not obeyed.
    """
    assert "system" not in RESOLVING_ORIGINS

    agent, bus = concierge()
    env = Envelope(intent="chat",
                   payload={"text": "set the hallway thermostat to 72"},
                   requester="owner",
                   budget=Budget(max_actions=6, max_seconds=20.0))
    assert env.origin_agent == "system"
    reply = agent.handle(env)

    assert bus.actuating() == []
    assert reply["intent"] is None


def test_summarize_is_inert_even_from_a_human_origin():
    """The intent is a second, independent brake on the same failure.

    A person's own message can quote a reading too ("here's what the report
    said..."), and a caller that labels its payload ``summarize`` is saying the
    text is material, whoever handed it over.
    """
    assert "summarize" in PROSE_ONLY_INTENTS

    agent, bus = concierge()
    reply = agent.handle(
        human_envelope("set the hallway thermostat to 72", intent="summarize"))

    assert bus.actuating() == []
    assert reply["intent"] is None
