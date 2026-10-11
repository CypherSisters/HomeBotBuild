"""The Sentinel veto must inspect the payload shape the Steward actually sends.

Every Steward actuator names its target ``payload["entity_id"]``. A consent
gate keyed only on ``payload["targets"]`` would pass ``control_device``,
``set_climate``, ``list_add``, ``announce`` and ``siren`` review
*unconditionally*: the veto the whole safety model rests on would never inspect
what they touch. Veto tests that hand-write ``targets`` alongside ``entity_id``
(a shape no real caller uses) cannot see that.

These tests drive the real bus with the real Sentinel and the payload shape the
Steward actually receives.

Pure in-process; no HA, no sockets.
Run: pytest tests/test_sentinel_veto_shape.py -q
"""

from __future__ import annotations

import pytest

from puffpai.agents import Budget, Envelope, build_team
from puffpai.agents.sentinel_agent import collect_targets
from puffpai.security.audit_log import AuditLog


# ── fixtures (mirror tests/test_agent_team.py) ──────────────────────

@pytest.fixture
def consent(tmp_path, monkeypatch):
    """Isolate the consent store + review log to a temp dir."""
    import puffpai.security.consent_gate as cg
    monkeypatch.setattr(cg, "REVIEW_LOG", str(tmp_path / "reviews.jsonl"))
    store = cg.ConsentStore(path=str(tmp_path / "consent.json"))
    monkeypatch.setattr(cg, "_store", store)
    monkeypatch.setattr(cg.ConsentStore, "_legacy_cache", set())
    return store


@pytest.fixture
def audit(tmp_path):
    return AuditLog(log_file=str(tmp_path / "audit.jsonl"))


def _steward_env(intent, payload):
    """The envelope shape a Steward actuator really receives — no ``targets``."""
    return Envelope(intent=intent, payload=payload, requester="owner",
                    origin_agent="concierge", state_changing=True,
                    budget=Budget(max_actions=3, allow_egress=False))


def _send(bus, monkeypatch, envelope, to="steward"):
    """Send through the real bus, recording whether the actuator was reached."""
    agent = bus.get(to)
    reached = {"handle": False}
    monkeypatch.setattr(agent, "handle",
                        lambda env: reached.__setitem__("handle", True)
                        or {"ok": True})
    return bus.send(envelope, to=to), reached


# ══════════════════════════════════════════════════════════════════════
#  The payload shape every Steward actuator really uses
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("intent,payload", [
    ("control_device", {"entity_id": "lock.front", "action": "unlock"}),
    ("control_device", {"entity_id": "light.kitchen", "action": "turn_on"}),
    ("activate_scene", {"entity_id": "scene.movie_night"}),
    ("set_climate", {"entity_id": "climate.hall", "temperature": 72}),
    ("siren", {"entity_id": "siren.alarm", "action": "turn_on"}),
    ("list_add", {"entity_id": "todo.shopping", "item": "milk"}),
    ("announce", {"entity_id": "assist_satellite.voice", "text": "hi"}),
])
def test_an_entity_id_only_envelope_is_actually_reviewed(
        consent, audit, monkeypatch, intent, payload):
    """These all reached the actuator with no consent check at all."""
    bus = build_team(audit=audit)
    reply, reached = _send(bus, monkeypatch, _steward_env(intent, payload))

    assert reply["ok"] is False and reply["denied"] is True
    assert reply["by"] == "sentinel"
    assert "not consented" in reply["reason"]
    assert reached["handle"] is False       # never reached the actuator


def test_the_proven_lock_unlock_is_denied_without_consent(consent, audit,
                                                          monkeypatch):
    """The end of the escalation chain: unlocking the front door."""
    bus = build_team(audit=audit)
    reply, reached = _send(bus, monkeypatch, _steward_env(
        "control_device", {"entity_id": "lock.front", "action": "unlock"}))

    assert reply["denied"] is True
    assert reached["handle"] is False
    denials = AuditLog(log_file=audit._log_file).query(action="bus_veto")
    assert denials and denials[0]["result"] == "denied"


def test_a_consented_entity_id_still_gets_through(consent, audit, monkeypatch):
    """The gate must engage, not seize: consent still allows the action."""
    consent.approve("light.kitchen", by="test", zone="zone3")
    bus = build_team(audit=audit)
    reply, reached = _send(bus, monkeypatch, _steward_env(
        "control_device", {"entity_id": "light.kitchen", "action": "turn_on"}))

    assert reply.get("ok") is True
    assert reached["handle"] is True


def test_a_safety_critical_entity_needs_its_own_approval(consent, audit,
                                                         monkeypatch):
    """Approving a light must not carry over to the lock in the same house."""
    consent.approve("light.kitchen", by="test", zone="zone3")
    bus = build_team(audit=audit)
    reply, reached = _send(bus, monkeypatch, _steward_env(
        "control_device", {"entity_id": "lock.front", "action": "unlock"}))

    assert reply["denied"] is True
    assert reached["handle"] is False


def test_a_list_of_entity_ids_is_gated_as_a_whole(consent, audit, monkeypatch):
    """One consented entity in a list must not launder the rest."""
    consent.approve("light.kitchen", by="test", zone="zone3")
    bus = build_team(audit=audit)
    reply, reached = _send(bus, monkeypatch, _steward_env(
        "control_device", {"entity_id": ["light.kitchen", "lock.front"],
                           "action": "turn_on"}))

    assert reply["denied"] is True
    assert "lock.front" in reply["reason"]
    assert reached["handle"] is False


def test_an_existing_targets_caller_is_unaffected(consent, audit, monkeypatch):
    """The automation-authoring path already passed ``targets``; keep it working."""
    consent.approve("light.kitchen", by="test", zone="zone3")
    bus = build_team(audit=audit)
    env = Envelope(intent="control_device",
                   payload={"targets": ["light.kitchen"]},
                   requester="owner", origin_agent="concierge",
                   state_changing=True, budget=Budget(max_actions=3))
    reply, reached = _send(bus, monkeypatch, env)

    assert reply.get("ok") is True
    assert reached["handle"] is True


def test_both_keys_naming_the_same_entity_are_gated_once(consent, audit,
                                                         monkeypatch):
    consent.approve("light.kitchen", by="test", zone="zone3")
    bus = build_team(audit=audit)
    reply, reached = _send(bus, monkeypatch, _steward_env(
        "control_device", {"entity_id": "light.kitchen",
                           "targets": ["light.kitchen"], "action": "turn_on"}))

    assert reply.get("ok") is True
    assert reached["handle"] is True


# ══════════════════════════════════════════════════════════════════════
#  collect_targets itself
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("payload,expected", [
    ({}, []),
    ({"entity_id": "lock.front"}, ["lock.front"]),
    ({"entity_id": ["a.b", "c.d"]}, ["a.b", "c.d"]),
    ({"entity_ids": ("a.b",)}, ["a.b"]),
    ({"targets": "a.b"}, ["a.b"]),
    ({"targets": ["a.b"], "entity_id": "c.d"}, ["a.b", "c.d"]),
    # de-duplication and blank/garbage rejection
    ({"targets": ["a.b"], "entity_id": "a.b"}, ["a.b"]),
    ({"entity_id": "  a.b  "}, ["a.b"]),
    ({"entity_id": ["", "   ", None, 7, {"x": 1}, "a.b"]}, ["a.b"]),
    ({"entity_id": None}, []),
    ({"entity_id": 42}, []),
    ({"entity_id": {"nested": "a.b"}}, []),
    # ``target`` in an enqueue payload is an AGENT name, not an entity: it must
    # not be gated as one.
    ({"target": "steward"}, []),
])
def test_collect_targets_matrix(payload, expected):
    assert collect_targets(payload) == expected


def test_collect_targets_tolerates_a_non_dict_payload():
    for payload in (None, [], "lock.front", 7):
        assert collect_targets(payload) == []


# ══════════════════════════════════════════════════════════════════════
#  What this fix does NOT cover — documented, not asserted away
# ══════════════════════════════════════════════════════════════════════

def test_notify_user_names_no_entity_and_so_is_still_ungated(consent, audit,
                                                             monkeypatch):
    """``notify_user`` carries only a ``service``; there is no entity to gate.

    Recorded deliberately: normalising target keys cannot gate an intent that
    names no target. Bounding it needs a per-intent rule, not a payload key.
    """
    bus = build_team(audit=audit)
    reply, reached = _send(bus, monkeypatch, _steward_env(
        "notify_user", {"service": "persistent_notification",
                        "message": "hello"}))

    assert reply.get("ok") is True
    assert reached["handle"] is True
