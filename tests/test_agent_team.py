"""Tests for the six-agent product team (puffpai.agents).

Covers the load-bearing security properties from PUFFPAI_PRODUCT_AGENT_TEAM_PLAN:
scope enforcement, Sentinel's structural veto, budget exhaustion, the kid-profile
Zone-2 lock, roster/bus construction, and the LLM-independent Caretaker.

Pure in-process; no HA, Ollama, ChromaDB, or Node-RED required.
Run: pytest tests/test_agent_team.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents import (
    AgentBus,
    Budget,
    BudgetExceeded,
    Envelope,
    build_team,
    roster_summary,
)
from puffpai.agents.archivist import ArchivistAgent
from puffpai.agents.base import BaseAgent
from puffpai.agents.caretaker import CaretakerAgent
from puffpai.agents.majordomo import MajordomoAgent
from puffpai.security.audit_log import AuditLog
from puffpai.security.secrets_provider import AgentScope, SecretAccessDenied


# ── fixtures ────────────────────────────────────────────────────────

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


# ── roster / construction ───────────────────────────────────────────

def test_build_team_constructs_the_roster_and_wires_reviewer(audit):
    bus = build_team(audit=audit)
    assert sorted(bus.agents().keys()) == [
        "archivist", "caretaker", "concierge", "majordomo", "sentinel",
        "steward", "toolsmith",
    ]
    health = bus.health()
    assert health["ok"] is True
    assert health["reviewer"] == "sentinel"
    assert health["reviewer_wired"] is True


def test_roster_summary_matches_plan():
    summary = {e["name"]: e for e in roster_summary()}
    assert set(summary) == {
        "concierge", "steward", "sentinel", "archivist", "majordomo",
        "caretaker", "toolsmith",
    }
    assert summary["concierge"]["scope"] == "concierge"
    assert summary["archivist"]["evolves_from"].startswith("(new")


# ── scope enforcement ───────────────────────────────────────────────

def test_concierge_holds_no_secrets(audit):
    bus = build_team(audit=audit)
    concierge = bus.get("concierge")
    assert concierge.scope is AgentScope.CONCIERGE
    with pytest.raises(SecretAccessDenied):
        concierge.secret("HA_TOKEN")
    with pytest.raises(SecretAccessDenied):
        concierge.secret("JELLYFIN_API_KEY")


def test_steward_is_allowed_ha_and_media_names(audit):
    bus = build_team(audit=audit)
    steward = bus.get("steward")
    # Allowed names must not raise (value may resolve to None in test env).
    steward.secret("HA_TOKEN")
    steward.secret("JELLYFIN_API_KEY")
    # But steward must NOT be allowed the vault/automation credentials.
    with pytest.raises(SecretAccessDenied):
        steward.secret("NODERED_API_TOKEN")
    with pytest.raises(SecretAccessDenied):
        steward.secret("VAULT_SERVICE_KEY")


def test_archivist_majordomo_caretaker_scopes(audit):
    bus = build_team(audit=audit)
    bus.get("archivist").secret("VAULT_SERVICE_KEY")   # allowed
    bus.get("majordomo").secret("NODERED_API_TOKEN")   # allowed
    bus.get("caretaker").secret("SERVICE_CONTROL_KEY")  # allowed
    with pytest.raises(SecretAccessDenied):
        bus.get("archivist").secret("HA_TOKEN")
    with pytest.raises(SecretAccessDenied):
        bus.get("majordomo").secret("HA_TOKEN")


# ── Sentinel veto ───────────────────────────────────────────────────

def test_veto_blocks_state_change_to_steward(consent, audit, monkeypatch):
    bus = build_team(audit=audit)
    steward = bus.get("steward")
    reached = {"handle": False}
    monkeypatch.setattr(steward, "handle",
                        lambda env: reached.__setitem__("handle", True) or {"ok": True})

    env = Envelope(
        intent="control_device",
        payload={"entity_id": "light.kitchen", "action": "turn_on",
                 "targets": ["light.kitchen"]},
        requester="owner", origin_agent="concierge",
        state_changing=True, budget=Budget(max_actions=3),
    )
    reply = bus.send(env, to="steward")

    assert reply["ok"] is False and reply["denied"] is True
    assert reply["by"] == "sentinel"
    assert "not consented" in reply["reason"]
    assert reached["handle"] is False  # never reached the actuator

    # denial is audited
    denials = [e for e in AuditLog(log_file=audit._log_file).query(action="bus_veto")]
    assert denials and denials[0]["result"] == "denied"


def test_veto_allows_consented_target(consent, audit, monkeypatch):
    consent.approve("light.kitchen", by="test", zone="zone3")
    bus = build_team(audit=audit)
    steward = bus.get("steward")
    reached = {"handle": False}
    monkeypatch.setattr(steward, "handle",
                        lambda env: reached.__setitem__("handle", True) or {"ok": True})

    env = Envelope(
        intent="control_device",
        payload={"entity_id": "light.kitchen", "action": "turn_on",
                 "targets": ["light.kitchen"]},
        requester="owner", origin_agent="concierge",
        state_changing=True, budget=Budget(max_actions=3),
    )
    reply = bus.send(env, to="steward")
    assert reply.get("ok") is True
    assert reached["handle"] is True


def test_bus_fails_closed_without_reviewer(audit):
    """A state-changing send with no reviewer registered is denied."""
    bus = AgentBus(audit=audit, reviewer_name="sentinel")

    class _Sink(BaseAgent):
        name = "steward"
        scope = AgentScope.STEWARD

        def handle(self, envelope):
            return {"ok": True, "reached": True}

    bus.register(_Sink())
    env = Envelope(intent="control_device", payload={"targets": ["light.x"]},
                   state_changing=True)
    reply = bus.send(env, to="steward")
    assert reply["ok"] is False and reply["denied"] is True
    assert "no policy reviewer" in reply["reason"]


# ── budget ──────────────────────────────────────────────────────────

def test_budget_exhaustion_aborts_via_bus(audit):
    bus = AgentBus(audit=audit)

    class _Greedy(BaseAgent):
        name = "greedy"
        scope = AgentScope.SYSTEM

        def handle(self, envelope):
            envelope.budget.consume(1)
            envelope.budget.consume(1)  # over budget -> BudgetExceeded
            return {"ok": True}

    bus.register(_Greedy())
    env = Envelope(intent="work", budget=Budget(max_actions=1),
                   state_changing=False)
    reply = bus.send(env, to="greedy")
    assert reply["ok"] is False and reply["aborted"] == "budget"

    aborts = AuditLog(log_file=audit._log_file).query(action="bus_budget_abort")
    assert aborts and aborts[0]["result"] == "aborted"


def test_budget_forbids_unpermitted_egress():
    b = Budget(max_actions=5, allow_egress=False)
    with pytest.raises(BudgetExceeded):
        b.consume(1, egress=True)
    # actions were not spent on the failed egress attempt
    assert b.actions_used == 0


# ── Archivist zone / privacy enforcement ────────────────────────────

class _FakeRAGResponse:
    vault_count = 0
    knowledge_count = 2

    def context_string(self, max_results: int = 8):
        return "kb-context"


class _FakeRAG:
    def __init__(self):
        self.calls = []

    def query(self, q, n_results=5, authenticated=False,
              knowledge_only=False, vault_only=False):
        self.calls.append({"authenticated": authenticated,
                           "knowledge_only": knowledge_only})
        return _FakeRAGResponse()


def test_kid_profile_gets_zone2_only():
    rag = _FakeRAG()
    arch = ArchivistAgent(rag=rag)
    env = Envelope(intent="retrieve",
                   payload={"query": "photosynthesis", "profile": "child",
                            "vault_session_id": "sess"},  # session ignored for kid
                   requester="kiddo")
    reply = arch.handle(env)
    assert reply["zones_queried"] == ["knowledge"]
    assert reply["authenticated"] is False
    assert rag.calls[-1] == {"authenticated": False, "knowledge_only": True}


class _FakeVault:
    """Minimal vault stand-in exposing only is_unlocked(session_id).

    Mirrors the real Vault contract: an unlocked session id verifies, anything
    else (bogus/expired/locked) does not.
    """

    def __init__(self, unlocked_ids=()):
        self._unlocked = set(unlocked_ids)

    def is_unlocked(self, session_id):
        return session_id in self._unlocked


def test_adult_with_unlocked_session_gets_vault():
    # Fail-closed contract: Zone-1 requires a VERIFIED-unlocked vault session,
    # not just a non-empty vault_session_id string. Inject a vault that confirms
    # "sess" is unlocked.
    rag = _FakeRAG()
    arch = ArchivistAgent(rag=rag, vault=_FakeVault(unlocked_ids={"sess"}))
    env = Envelope(intent="retrieve",
                   payload={"query": "my prescriptions", "profile": "owner",
                            "vault_session_id": "sess"},
                   requester="owner")
    reply = arch.handle(env)
    assert "vault" in reply["zones_queried"]
    assert rag.calls[-1] == {"authenticated": True, "knowledge_only": False}


def test_adult_with_unverifiable_session_stays_zone2():
    # A session id is present but there is NO vault to verify it against, so the
    # archivist cannot prove the vault is unlocked -> Zone-2 only (fail closed).
    rag = _FakeRAG()
    arch = ArchivistAgent(rag=rag)  # no vault injected
    env = Envelope(intent="retrieve",
                   payload={"query": "my prescriptions", "profile": "owner",
                            "vault_session_id": "sess"},
                   requester="owner")
    reply = arch.handle(env)
    assert reply["zones_queried"] == ["knowledge"]
    assert rag.calls[-1] == {"authenticated": False, "knowledge_only": True}


def test_adult_with_locked_session_stays_zone2():
    # Vault present but the session is NOT unlocked (expired/bogus) -> Zone-2.
    rag = _FakeRAG()
    arch = ArchivistAgent(rag=rag, vault=_FakeVault(unlocked_ids=set()))
    env = Envelope(intent="retrieve",
                   payload={"query": "my prescriptions", "profile": "owner",
                            "vault_session_id": "sess"},
                   requester="owner")
    reply = arch.handle(env)
    assert reply["zones_queried"] == ["knowledge"]
    assert rag.calls[-1]["authenticated"] is False


def test_adult_without_session_stays_zone2():
    rag = _FakeRAG()
    arch = ArchivistAgent(rag=rag, vault=_FakeVault(unlocked_ids={"sess"}))
    env = Envelope(intent="retrieve",
                   payload={"query": "weather", "profile": "owner"},
                   requester="owner")
    reply = arch.handle(env)
    assert reply["zones_queried"] == ["knowledge"]
    assert rag.calls[-1]["authenticated"] is False


# ── Majordomo scoped spawning ───────────────────────────────────────

def test_majordomo_spawn_task_worker_is_scoped_and_queued(tmp_path):
    qpath = str(tmp_path / "queue.jsonl")
    majordomo = MajordomoAgent(queue_path=qpath)
    strict = Budget(max_actions=1, max_seconds=10.0, allow_egress=False)
    env = majordomo.spawn_task_worker(
        {"intent": "daily_briefing", "target": "steward",
         "payload": {"room": "kitchen"}, "requester": "owner"},
        budget=strict,
    )
    assert env.origin_agent == "majordomo"
    assert env.budget.allow_egress is False
    assert env.budget.max_actions == 1
    # persisted to the queue
    rows = [json.loads(l) for l in open(qpath, encoding="utf-8")]
    assert rows[-1]["envelope_id"] == env.id
    assert rows[-1]["target"] == "steward"


# ── Caretaker: deterministic, LLM-independent ───────────────────────

def test_caretaker_health_runs_with_everything_down(monkeypatch):
    import puffpai.agents.caretaker as ct_mod

    def _always_fail(*a, **k):
        raise OSError("unreachable")

    monkeypatch.setattr(ct_mod.socket, "create_connection", _always_fail)
    caretaker = CaretakerAgent(config=object())  # empty config -> no chromadb path
    report = caretaker.run_health_checks()
    assert report["ok"] is True          # the sweep itself always succeeds
    assert report["healthy"] == 0        # nothing reachable
    assert report["total"] == 5
    services = {c["service"] for c in report["checks"]}
    assert services == {"ollama", "chromadb", "api_server", "nodered", "jellyfin"}


def test_caretaker_refuses_to_restart_itself():
    """Phase 3b: restart_service is real, but never for the caller's own unit.

    ``api_server`` aliases to ``puffpai-api``, the unit this process runs in.
    Full service-control coverage lives in tests/test_caretaker_service_control.py.
    """
    caretaker = CaretakerAgent(config=object())
    result = caretaker.restart_service("api_server")
    assert result["ok"] is False
    assert result["refused"] == "self_restart"
    assert result["unit"] == "puffpai-api"


# ── Envelope audit redaction ────────────────────────────────────────

def test_audit_view_redacts_payload_values():
    env = Envelope(intent="vault_read",
                   payload={"pin": "1234-secret", "filename": "profiles.json"})
    view = env.audit_view()
    assert set(view["payload_keys"]) == {"pin", "filename"}
    blob = json.dumps(view)
    assert "1234-secret" not in blob  # values never leak into the ledger
