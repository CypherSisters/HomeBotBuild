"""PuffPAI provisions Home Assistant for the owner — propose, consent, borrow.

The owner's requirement was "I can't be doing this for every
new device I want to add"; the standing hardening is that PuffPAI's everyday
Home Assistant account is NOT an administrator. This file locks in the shape
that satisfies both:

* a proposal is a consent request carrying the EXACT configuration;
* the write happens only with a fresh human approval on the durable record, the
  in-process HUMAN_CONFIRMED token, and an UNLOCKED vault;
* the elevated credential is borrowed on a throwaway session and dropped — the
  everyday bridge never sees it;
* v1 creates and never overwrites;
* nothing logs the credential or the payload.

Run: py -m pytest tests/test_ha_provisioning.py -q
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re

import pytest

ADMIN_TOKEN = "ADMIN-TOKEN-" + "b" * 40
EVERYDAY_TOKEN = "EVERYDAY-TOKEN-" + "c" * 40

#: A string that appears ONLY inside a proposed payload, so a log line that
#: leaked the payload is unmistakable.
PAYLOAD_MARKER = "marker_payload_9z7q"


# ── doubles ─────────────────────────────────────────────────────────

class StubBridge:
    """The EVERYDAY bridge: reads states, carries the everyday credential."""

    def __init__(self, entities=()):
        self.entities = list(entities)
        self._token = EVERYDAY_TOKEN
        self._session = object()          # identity we assert is never reused
        self.state_reads = 0

    def get_all_states(self, *, force=False):
        self.state_reads += 1
        return [{"entity_id": e, "attributes": {}} for e in self.entities]


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = b"{}" if body is not None else b""
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


class FakeSession:
    """A throwaway ``requests.Session``. Records everything it is asked to do."""

    instances: list["FakeSession"] = []
    on_post = None            # test hook: (url, payload) -> None
    post_status = 200

    def __init__(self):
        self.headers: dict = {}
        self.calls: list[tuple] = []
        self.closed = False
        FakeSession.instances.append(self)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        return FakeResponse(404)

    def post(self, url, json=None, **kwargs):
        self.calls.append(("POST", url, json))
        if FakeSession.on_post is not None:
            FakeSession.on_post(url, json)
        return FakeResponse(FakeSession.post_status, {})

    def close(self):
        self.closed = True

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.on_post = None
        cls.post_status = 200


def script_payload(alias=PAYLOAD_MARKER, service="zha.permit"):
    return {"alias": alias, "mode": "single",
            "sequence": [{"service": service, "data": {"duration": 120}}]}


# ── fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def prov(tmp_path, monkeypatch):
    """Isolated consent queue + activity feed, same shape as the zigbee tests."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFFPAI_HA_PROVISIONING", "1")

    import puffpai.agent_activity as aa
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr
    import puffpai.ha_provisioning as hp

    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    hp.reset_state()

    monkeypatch.setattr(hp, "_ha_base_url", lambda: "http://ha.test")
    monkeypatch.setattr(hp, "VERIFY_ATTEMPTS", 1)
    monkeypatch.setattr(hp, "VERIFY_DELAY_SECONDS", 0.0)

    import requests
    FakeSession.reset()
    monkeypatch.setattr(requests, "Session", FakeSession)

    yield hp, cr, tmp_path

    hp.reset_state()
    FakeSession.reset()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()


def _unlocked(monkeypatch, hp, token=ADMIN_TOKEN):
    """Make ``resolve_ha_admin_token`` behave like an unlocked vault."""
    from puffpai.security import ha_token_source as hts
    monkeypatch.setattr(
        hts, "resolve_ha_admin_token",
        lambda session_id=None: hts.Resolution(token, hts.SOURCE_VAULT_SESSION,
                                               "unlocked vault session"))


def _locked(monkeypatch):
    from puffpai.security import ha_token_source as hts
    monkeypatch.setattr(
        hts, "resolve_ha_admin_token",
        lambda session_id=None: hts.Resolution(None, None,
                                               hts.ADMIN_LOCKED_DETAIL))


def _confirm():
    from puffpai.ha_bridge import HUMAN_CONFIRMED
    return HUMAN_CONFIRMED


def _approve(cr, entity_id, by="user_via_ui"):
    return cr.resolve(entity_id, approved=True, by=by)


# ══════════════════════════════════════════════════════════════════════
#  1. Propose: the owner sees the exact thing that will be written
# ══════════════════════════════════════════════════════════════════════

def test_propose_files_a_consent_carrying_the_exact_configuration(prov):
    hp, cr, _ = prov
    payload = script_payload()

    out = hp.propose_ha_change("script", "puffpai_demo", payload,
                               "because zha.permit is admin-only")

    assert out["ok"] is True and out["proposed"] is True
    assert out["executed"] is False
    assert out["entity_id"] == "script.puffpai_demo"

    open_reqs = cr.list_open()
    assert [r["entity_id"] for r in open_reqs] == ["script.puffpai_demo"]
    req = open_reqs[0]
    assert req["action"] == "ha_provision"
    # The exact rendered payload, and the vault sentence, are in the reason the
    # owner reads — not a summary of them.
    assert hp.render_payload(payload) in req["reason"]
    assert "requires the vault to be unlocked" in req["reason"]
    assert PAYLOAD_MARKER in req["reason"]
    # The consent UI renders label/action/reason; all three are present.
    assert req["label"] and req["reason"] and req["action"]


def test_propose_is_idempotent_per_object(prov):
    hp, cr, _ = prov
    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    assert len(cr.list_open()) == 1


@pytest.mark.parametrize("kind", ["input_boolean", "sensor", "", "scene"])
def test_propose_refuses_kinds_home_assistant_has_no_config_api_for(prov, kind):
    hp, _, _ = prov
    with pytest.raises(hp.ProvisioningError):
        hp.propose_ha_change(kind, "thing", script_payload(), "why")


@pytest.mark.parametrize("object_id", [
    "../../etc/passwd", "Has Capitals", "with-dash", "", "a" * 65,
])
def test_propose_refuses_object_ids_that_are_not_slugs(prov, object_id):
    hp, _, _ = prov
    with pytest.raises(hp.ProvisioningError):
        hp.propose_ha_change("script", object_id, script_payload(), "why")


@pytest.mark.parametrize("service", [
    "shell_command.wipe", "python_script.run", "hassio.host_reboot",
])
def test_propose_refuses_payloads_that_execute_code_on_the_ha_host(prov,
                                                                   service):
    hp, cr, _ = prov
    with pytest.raises(hp.ProvisioningError):
        hp.propose_ha_change("script", "puffpai_demo",
                             script_payload(service=service), "why")
    assert cr.list_open() == []


def test_propose_touches_no_credential_and_makes_no_http_call(prov, monkeypatch):
    hp, _, _ = prov

    def explode(*_a, **_k):                # pragma: no cover - must not run
        raise AssertionError("propose must not read the elevated credential")

    from puffpai.security import ha_token_source as hts
    monkeypatch.setattr(hts, "resolve_ha_admin_token", explode)

    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    assert FakeSession.instances == []


# ══════════════════════════════════════════════════════════════════════
#  2. Apply: three gates, all of them
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spoofed", [True, "true", 1, "HUMAN_CONFIRMED"])
def test_apply_refuses_a_confirmation_that_json_could_carry(prov, monkeypatch,
                                                            spoofed):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    _approve(cr, "script.puffpai_demo")

    out = hp.apply_approved_change(bridge=StubBridge(),
                                   human_confirmed=spoofed)

    assert out["ok"] is False
    assert out["created"] is False
    assert FakeSession.instances == []


def test_apply_refuses_without_a_fresh_human_approval(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    # No resolve() at all: the request is still open.

    out = hp.apply_approved_change(bridge=StubBridge(),
                                   human_confirmed=_confirm())

    assert out["ok"] is False
    assert "no approved" in out["error"]
    assert FakeSession.instances == []


def test_apply_refuses_an_approval_an_agent_recorded(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    _approve(cr, "script.puffpai_demo", by="steward")     # not a human surface

    out = hp.apply_approved_change(bridge=StubBridge(),
                                   human_confirmed=_confirm())
    assert out["ok"] is False
    assert FakeSession.instances == []


def test_apply_spends_the_approval_exactly_once(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge()
    FakeSession.on_post = lambda url, payload: (
        bridge.entities.append("script.puffpai_demo")
        if "config/script" in url else None)

    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    approved = _approve(cr, "script.puffpai_demo")

    first = hp.apply_approved_change(request=approved, bridge=bridge,
                                     human_confirmed=_confirm())
    second = hp.apply_approved_change(request=approved, bridge=bridge,
                                      human_confirmed=_confirm())

    assert first["created"] is True
    assert second["created"] is False
    writes = [c for s in FakeSession.instances for c in s.calls
              if c[0] == "POST" and "config/script" in c[1]]
    assert len(writes) == 1


def test_apply_refuses_and_makes_no_call_when_the_vault_is_locked(prov,
                                                                  monkeypatch):
    hp, cr, _ = prov
    _locked(monkeypatch)
    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    approved = _approve(cr, "script.puffpai_demo")

    out = hp.apply_approved_change(request=approved, bridge=StubBridge(),
                                   human_confirmed=_confirm())

    assert out["ok"] is False
    assert out["created"] is False
    assert out["vault_locked"] is True
    assert "locked" in out["message"].lower()
    assert FakeSession.instances == [], "a locked vault must not open a socket"

    # And the owner's yes is NOT burned: unlocking and retrying finishes it.
    _unlocked(monkeypatch, hp)
    bridge = StubBridge()
    FakeSession.on_post = lambda url, payload: (
        bridge.entities.append("script.puffpai_demo")
        if "config/script" in url else None)
    retry = hp.apply_approved_change(request=approved, bridge=bridge,
                                     human_confirmed=_confirm())
    assert retry["created"] is True


# ══════════════════════════════════════════════════════════════════════
#  3. The write itself
# ══════════════════════════════════════════════════════════════════════

def test_apply_writes_reloads_verifies_and_files_the_event(prov, monkeypatch):
    hp, cr, tmp_path = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge()
    FakeSession.on_post = lambda url, payload: (
        bridge.entities.append("script.puffpai_demo")
        if "config/script" in url else None)

    payload = script_payload()
    hp.propose_ha_change("script", "puffpai_demo", payload, "why")
    approved = _approve(cr, "script.puffpai_demo")

    out = hp.apply_approved_change(request=approved, bridge=bridge,
                                   human_confirmed=_confirm())

    assert out == {"ok": True, "created": True, "verified": True,
                   "entity_id": "script.puffpai_demo", "kind": "script",
                   "message": out["message"]}

    assert len(FakeSession.instances) == 1
    session = FakeSession.instances[0]
    assert session.closed is True, "the borrowed session must be closed"
    assert session.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"

    posts = [c for c in session.calls if c[0] == "POST"]
    assert posts[0][1] == "http://ha.test/api/config/script/config/puffpai_demo"
    assert posts[0][2] == payload
    assert posts[1][1] == "http://ha.test/api/services/script/reload"

    # Verification went through the EVERYDAY bridge, not the admin session.
    assert bridge.state_reads >= 1

    # The activity event names the entity and carries no payload.
    from puffpai import agent_activity as aa
    entries = [json.loads(line) for line in
               open(aa.get_activity_log_path(), encoding="utf-8")
               if line.strip()]
    provisioned = [e for e in entries if e.get("event") == "ha_provisioned"]
    assert len(provisioned) == 1
    assert provisioned[0]["task"] == "script.puffpai_demo"
    assert PAYLOAD_MARKER not in json.dumps(provisioned[0])
    assert ADMIN_TOKEN not in json.dumps(provisioned[0])


def test_apply_uses_the_automation_endpoints_for_automations(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge()
    # An automation needs a trigger and an action.
    payload = {"alias": PAYLOAD_MARKER,
               "trigger": [{"platform": "time", "at": "22:00:00"}],
               "action": [{"service": "light.turn_off",
                           "target": {"entity_id": ["light.kitchen"]}}]}

    hp.propose_ha_change("automation", "puffpai_demo", payload, "why")
    approved = _approve(cr, "automation.puffpai_demo")
    hp.apply_approved_change(request=approved, bridge=bridge,
                             human_confirmed=_confirm())

    urls = [c[1] for c in FakeSession.instances[0].calls if c[0] == "POST"]
    assert urls[0].endswith("/api/config/automation/config/puffpai_demo")
    assert urls[1].endswith("/api/services/automation/reload")


def test_apply_is_create_only(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge(entities=["script.puffpai_demo"])

    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    approved = _approve(cr, "script.puffpai_demo")
    out = hp.apply_approved_change(request=approved, bridge=bridge,
                                   human_confirmed=_confirm())

    assert out["ok"] is False
    assert out["already_exists"] is True
    assert "already exists" in out["message"]
    writes = [c for s in FakeSession.instances for c in s.calls
              if c[0] == "POST"]
    assert writes == []


def test_apply_overwrites_only_when_the_owner_approved_an_overwrite(prov,
                                                                    monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge(entities=["script.puffpai_demo"])

    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why",
                         overwrite=True)
    approved = _approve(cr, "script.puffpai_demo")
    out = hp.apply_approved_change(request=approved, bridge=bridge,
                                   human_confirmed=_confirm())
    assert out["created"] is True


def test_the_payload_written_is_the_one_the_owner_approved(prov, monkeypatch):
    """A caller cannot smuggle a different body past the approval."""
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge()

    approved_payload = script_payload()
    hp.propose_ha_change("script", "puffpai_demo", approved_payload, "why")
    approved = _approve(cr, "script.puffpai_demo")

    tampered = dict(approved)
    tampered["context"] = dict(approved["context"])
    tampered["context"]["payload"] = script_payload(alias="not_what_you_saw")

    hp.apply_approved_change(request=tampered, bridge=bridge,
                             human_confirmed=_confirm())

    body = [c[2] for c in FakeSession.instances[0].calls
            if c[0] == "POST" and "config/script" in c[1]][0]
    assert body == approved_payload


def test_an_ha_refusal_of_the_admin_credential_is_named(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    FakeSession.post_status = 403

    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    approved = _approve(cr, "script.puffpai_demo")
    out = hp.apply_approved_change(request=approved, bridge=StubBridge(),
                                   human_confirmed=_confirm())

    assert out["ok"] is False
    assert "revoked" in out["message"]
    assert FakeSession.instances[0].closed is True


# ══════════════════════════════════════════════════════════════════════
#  4. The everyday credential is never involved
# ══════════════════════════════════════════════════════════════════════

def test_the_everyday_bridge_is_never_given_the_admin_token(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge()
    everyday_session = bridge._session

    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    approved = _approve(cr, "script.puffpai_demo")
    hp.apply_approved_change(request=approved, bridge=bridge,
                             human_confirmed=_confirm())

    assert bridge._token == EVERYDAY_TOKEN
    assert bridge._session is everyday_session
    assert bridge._session not in FakeSession.instances


def test_nothing_logs_the_credential_or_the_payload(prov, monkeypatch, caplog):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    bridge = StubBridge()
    FakeSession.on_post = lambda url, payload: (
        bridge.entities.append("script.puffpai_demo")
        if "config/script" in url else None)

    with caplog.at_level(logging.DEBUG):
        hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
        approved = _approve(cr, "script.puffpai_demo")
        hp.apply_approved_change(request=approved, bridge=bridge,
                                 human_confirmed=_confirm())

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert ADMIN_TOKEN not in blob
    assert EVERYDAY_TOKEN not in blob
    assert PAYLOAD_MARKER not in blob
    # ...and the audit line that SHOULD be there, is.
    assert "created script.puffpai_demo via vault admin credential" in blob


# ══════════════════════════════════════════════════════════════════════
#  5. The credential tier
# ══════════════════════════════════════════════════════════════════════

def test_the_admin_record_is_not_a_boot_secret():
    from puffpai.security import ha_token_source as hts
    from puffpai.security import vault as vault_mod
    assert hts.ADMIN_VAULT_FILENAME not in vault_mod.SERVICE_SECRET_FILES, (
        "the elevated credential must never be readable without the owner's "
        "PIN")


def test_the_allowlist_invariant_is_enforced_at_call_time(monkeypatch):
    """If a future edit adds it to the boot tier, everything refuses loudly."""
    from puffpai.security import ha_token_source as hts
    from puffpai.security import vault as vault_mod
    monkeypatch.setattr(vault_mod, "SERVICE_SECRET_FILES",
                        frozenset({"ha_token.json",
                                   hts.ADMIN_VAULT_FILENAME}))
    with pytest.raises(RuntimeError):
        hts._refuse_unattended_admin_read(None)


class FakeVault:
    def __init__(self, record=None, session="sid-1"):
        self.record = record
        self.session = session
        self.written = {}
        self._vault_dir = None

    def _first_live_session_id(self):
        return self.session

    def read_secure_session(self, filename, session_id):
        if self.session is None:
            raise RuntimeError("locked")
        return self.record

    def write_secure_session(self, filename, data, session_id):
        self.written[filename] = data


def test_resolve_admin_token_returns_the_token_only_when_unlocked(monkeypatch):
    from puffpai.security import ha_token_source as hts
    from puffpai.security import ha_vault

    vault = FakeVault(record={"token": ADMIN_TOKEN})
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)
    res = hts.resolve_ha_admin_token()
    assert res.token == ADMIN_TOKEN
    assert res.source == hts.SOURCE_VAULT_SESSION

    vault.session = None
    locked = hts.resolve_ha_admin_token()
    assert locked.token is None
    assert locked.locked is True
    assert "unlock" in locked.detail.lower()


def test_a_missing_admin_record_says_so_in_plain_words(monkeypatch):
    from puffpai.security import ha_token_source as hts
    from puffpai.security import ha_vault
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: FakeVault(record={}))
    res = hts.resolve_ha_admin_token()
    assert res.token is None
    assert "administrator account" in res.detail


@pytest.mark.parametrize("bad", ["", "short", "has space " + "a" * 40, None,
                                 123, "a" * 5000])
def test_storing_a_value_that_is_not_a_credential_is_refused(monkeypatch, bad):
    from puffpai.security import ha_token_source as hts
    from puffpai.security import ha_vault
    vault = FakeVault()
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)
    with pytest.raises(ValueError):
        hts.store_ha_admin_token(bad, "sid-1")
    assert vault.written == {}


def test_storing_the_admin_token_writes_the_session_tier_record(monkeypatch,
                                                                caplog):
    from puffpai.security import ha_token_source as hts
    from puffpai.security import ha_vault
    vault = FakeVault()
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)

    with caplog.at_level(logging.DEBUG):
        assert hts.store_ha_admin_token(ADMIN_TOKEN, "sid-1") is True

    assert vault.written[hts.ADMIN_VAULT_FILENAME]["token"] == ADMIN_TOKEN
    assert ADMIN_TOKEN not in "\n".join(r.getMessage() for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════
#  6. The Zigbee self-heal
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def paired(tmp_path, monkeypatch):
    """Same isolation as tests/test_zigbee_pairing.py's ``paired`` fixture."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFFPAI_HA_PROVISIONING", "1")

    import puffpai.agent_activity as aa
    import puffpai.ha_provisioning as hp
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr
    import puffpai.zigbee_pairing as zp

    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()
    zp._session = None
    zp._spent_approvals.clear()
    hp.reset_state()

    yield zp, cr, hp

    zp._session = None
    zp._spent_approvals.clear()
    hp.reset_state()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    zp.reset_cache()


def _refusing_actuator(error="HA returned HTTP 403: Forbidden"):
    from tests.test_zigbee_open_surfacing import RefusingActuator
    return RefusingActuator(error)


def test_an_admin_refusal_proposes_both_permit_scripts(paired):
    zp, cr, hp = paired
    zp.request_pairing("pair my new bulbs", duration=1)
    cr.resolve(zp.PAIRING_ENTITY_ID, approved=True, by="user_via_chat")

    out = zp.complete_approved_pairing(_refusing_actuator())

    assert out["opened"] is False
    assert out["proposed_scripts"] is True
    proposals = [r for r in cr.list_open() if r["action"] == "ha_provision"]
    assert sorted(r["entity_id"] for r in proposals) == [
        "script.puffpai_zigbee_permit", "script.puffpai_zigbee_permit_close"]
    # Each proposal shows the owner the one line it will create.
    bodies = " ".join(r["reason"] for r in proposals)
    assert '"service": "zha.permit"' in bodies
    assert '"duration": 120' in bodies and '"duration": 0' in bodies

    assert "approve them on the dashboard" in out["message"].lower()
    assert "vault unlocked" in out["message"].lower()


def test_the_self_heal_is_idempotent_on_repeat(paired):
    zp, cr, hp = paired
    for _ in range(3):
        zp.request_pairing("pair my new bulbs", duration=1)
        cr.resolve(zp.PAIRING_ENTITY_ID, approved=True, by="user_via_chat")
        zp.complete_approved_pairing(_refusing_actuator())

    proposals = [r for r in cr.list_open() if r["action"] == "ha_provision"]
    assert len(proposals) == 2


def test_a_non_admin_refusal_proposes_nothing(paired):
    zp, cr, hp = paired
    zp.request_pairing("pair my new bulbs", duration=1)
    cr.resolve(zp.PAIRING_ENTITY_ID, approved=True, by="user_via_chat")

    out = zp.complete_approved_pairing(
        _refusing_actuator("HA returned HTTP 400: entity not found"))

    assert out["proposed_scripts"] is False
    assert [r for r in cr.list_open() if r["action"] == "ha_provision"] == []


def test_the_self_heal_does_not_promote_the_everyday_account(paired):
    """The old advice was 'make PuffPAI an admin'. That must not be the ask."""
    zp, cr, hp = paired
    zp.request_pairing("pair my new bulbs", duration=1)
    cr.resolve(zp.PAIRING_ENTITY_ID, approved=True, by="user_via_chat")
    message = zp.complete_approved_pairing(_refusing_actuator())["message"]

    assert "deliberately not" in message.lower()
    assert "two small scoped scripts" in message.lower()


# ══════════════════════════════════════════════════════════════════════
#  7. No agent can reach the writer
# ══════════════════════════════════════════════════════════════════════

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "src", "puffpai")


def _sources():
    for root, _dirs, files in os.walk(_SRC):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    yield path, handle.read()


def test_only_the_two_approval_surfaces_call_the_writer():
    callers = set()
    for path, text in _sources():
        if os.path.basename(path) == "ha_provisioning.py":
            continue
        if re.search(r"\bapply_approved_change\s*\(", text):
            callers.add(os.path.basename(path))
    # The LLM tool in assistant_tools cannot approve. ha_pending_apply
    # finishes ONLY records a human approved (button/chat/voice) while the
    # vault was locked, on the owner's next full unlock, through the same
    # gates.
    assert callers == {"api_server.py", "ha_pending_apply.py"}, (
        "apply_approved_change may only be reached from the consent-resolution "
        f"surfaces; found {sorted(callers)}")


def test_the_steward_tool_proposes_and_never_applies():
    with open(os.path.join(_SRC, "assistant_tools.py"), encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("def ha_provision_propose(")
    body = text[start:text.index("def ha_run_scene(", start)]
    assert "propose_ha_change" in body
    assert "apply_approved_change" not in body
    assert "PROPOSES ONLY" in body


def test_the_capability_is_declared_state_changing_and_proposal_only():
    from puffpai import capability_registry as reg
    cap = next(c for c in reg.CAPABILITIES if c.id == "home.provisioning")
    assert cap.access == reg.WRITE
    assert cap.agent == "steward"
    assert cap.authority_tool == "ha_provision_propose"
    assert "proposal-only" in cap.note.lower()


def test_the_writer_is_switched_off_by_the_owner_flag(prov, monkeypatch):
    hp, cr, _ = prov
    _unlocked(monkeypatch, hp)
    hp.propose_ha_change("script", "puffpai_demo", script_payload(), "why")
    approved = _approve(cr, "script.puffpai_demo")

    monkeypatch.setenv("PUFFPAI_HA_PROVISIONING", "0")
    out = hp.apply_approved_change(request=approved, bridge=StubBridge(),
                                   human_confirmed=_confirm())
    assert out["ok"] is False
    assert FakeSession.instances == []
