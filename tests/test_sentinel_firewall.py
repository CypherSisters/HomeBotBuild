"""Tests for the Sentinel's firewall actuator (orchestration Phase 3b).

Covers ``block_device`` / ``unblock_device``: the Python-side IP-rejection matrix
(never trust the Node-RED flow alone), the "auto-block only what the Device
Sentinel flagged as new and unknown" policy, the always-confirm unblock gate,
the state_changing classification, the consent-gate review, and egress metering.

Pure in-process; no Node-RED, no firewall, no sockets.
Run: pytest tests/test_sentinel_firewall.py -q
"""

from __future__ import annotations

import pytest

import puffpai.security.consent_gate as cg
from puffpai.agents.envelope import Budget, BudgetExceeded, Envelope
from puffpai.agents.sentinel_agent import (
    CONFIRM_REQUIRED_INTENTS,
    FIREWALL_CONSENT_TARGET,
    STATE_CHANGING_INTENTS,
    SentinelAgent,
)
from puffpai.security.audit_log import AuditLog


OWN_IP = "192.168.50.40"
GATEWAY = "172.16.0.1"
TARGET_IP = "192.168.50.50"
PROTECTED = {OWN_IP, GATEWAY}


# ── stubs ───────────────────────────────────────────────────────────

class StubAutomation:
    """AutomationClient stand-in recording every webhook call."""

    def __init__(self, ok=True, data=None):
        self.calls: list[tuple[str, dict]] = []
        self._ok = ok
        self._data = data if data is not None else {"success": True}

    def call(self, name, payload, **kw):
        self.calls.append((name, dict(payload)))
        return self._ok, self._data


class StubDeviceSentinel:
    """DeviceSentinel stand-in returning canned alerts."""

    def __init__(self, alerts=None):
        self._alerts = alerts or []

    def status(self):
        return {"running": True}

    def list_alerts(self, include_acknowledged=False):
        return list(self._alerts)


def new_device_alert(ip=TARGET_IP, acknowledged=False, source="ip"):
    return {
        "id": "abc123",
        "source": source,
        "device_key": "AA:BB:CC:DD:EE:FF",
        "label": "unknown-device",
        "summary": f"New device on your network: unknown-device at {ip}. "
                   "It has been blocked.",
        "quarantined": True,
        "quarantine_detail": f"firewall blocked {ip}",
        "acknowledged": acknowledged,
    }


@pytest.fixture
def consent(tmp_path, monkeypatch):
    """Isolate the consent store + review log, and approve firewall control."""
    monkeypatch.setattr(cg, "REVIEW_LOG", str(tmp_path / "reviews.jsonl"))
    store = cg.ConsentStore(path=str(tmp_path / "consent.json"))
    monkeypatch.setattr(cg, "_store", store)
    monkeypatch.setattr(cg.ConsentStore, "_legacy_cache", set())
    store.approve(FIREWALL_CONSENT_TARGET, by="owner")
    return store


@pytest.fixture
def audit(tmp_path):
    return AuditLog(log_file=str(tmp_path / "audit.jsonl"))


def sentinel(audit, *, alerts=None, ok=True, protected=PROTECTED):
    auto = StubAutomation(ok=ok)
    agent = SentinelAgent(
        audit=audit,
        device_sentinel=StubDeviceSentinel(alerts),
        automation_client=auto,
        protected_ips=protected,
    )
    return agent, auto


def env(intent, payload=None, *, actions=3, state_changing=True,
        origin="concierge", egress=True) -> Envelope:
    return Envelope(
        intent=intent,
        payload=payload or {},
        requester="owner",
        origin_agent=origin,
        budget=Budget(max_actions=actions, max_seconds=30.0, allow_egress=egress),
        state_changing=state_changing,
    )


def human_env(intent, payload=None, **kw) -> Envelope:
    return env(intent, payload, origin="owner", **kw)


# ── classification ──────────────────────────────────────────────────

@pytest.mark.parametrize("intent", ["block_device", "unblock_device"])
def test_firewall_intents_are_declared_state_changing(intent):
    assert intent in STATE_CHANGING_INTENTS


def test_unblock_is_in_the_always_confirm_class():
    assert CONFIRM_REQUIRED_INTENTS == frozenset({"unblock_device"})


@pytest.mark.parametrize("intent", ["block_device", "unblock_device"])
def test_read_only_envelope_is_refused_with_no_webhook_call(intent, audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    reply = agent.handle(human_env(intent, {"ip": TARGET_IP, "confirmed": True},
                                   state_changing=False))
    assert reply["ok"] is False and reply["requires_review"] is True
    assert auto.calls == []


# ── IP rejection matrix ─────────────────────────────────────────────

BAD_IPS = [
    ("127.0.0.1", "loopback"),
    ("127.0.0.53", "loopback"),
    ("224.0.0.1", "multicast"),
    ("239.255.255.250", "multicast"),  # SSDP
    ("169.254.10.5", "link-local"),
    ("0.0.0.0", "unspecified"),
    ("255.255.255.255", "broadcast"),
    ("240.0.0.1", "reserved"),
    ("fe80::1", "IPv6"),
    ("2001:db8::1", "IPv6"),
    ("::1", "IPv6 loopback"),
    ("8.8.8.8", "public"),
    ("1.1.1.1", "public"),
    (OWN_IP, "the appliance itself"),
    (GATEWAY, "the default gateway"),
    ("192.168.50.256", "octet out of range"),
    ("192.0.2", "not an address"),
    ("192.168.50.50; reboot", "shell metacharacters"),
    ("192.168.50.50 || nc", "shell metacharacters"),
    ("$(hostname)", "command substitution"),
    ("192.168.50.50\n192.168.50.51", "newline"),
    ("", "empty"),
    ("   ", "whitespace"),
    ("not-an-ip", "garbage"),
]

NON_STRING_IPS = [None, 42, 3.5, True, ["192.168.50.50"], {"ip": "192.168.50.50"}]


@pytest.mark.parametrize("ip,why", BAD_IPS)
def test_block_rejects_bad_ip_before_any_call(ip, why, audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert(ip="192.168.50.50")])
    reply = agent.handle(human_env("block_device", {"ip": ip, "confirmed": True}))
    assert reply["ok"] is False, f"{ip} ({why}) should be refused"
    assert auto.calls == []


@pytest.mark.parametrize("ip,why", BAD_IPS)
def test_unblock_rejects_bad_ip_before_any_call(ip, why, audit, consent):
    agent, auto = sentinel(audit)
    reply = agent.handle(human_env("unblock_device", {"ip": ip, "confirmed": True}))
    assert reply["ok"] is False
    assert auto.calls == []


@pytest.mark.parametrize("ip", NON_STRING_IPS)
def test_non_string_ip_is_refused(ip, audit, consent):
    agent, auto = sentinel(audit)
    reply = agent.handle(human_env("block_device", {"ip": ip, "confirmed": True}))
    assert reply["ok"] is False
    assert auto.calls == []


@pytest.mark.parametrize("ip", ["192.168.50.50", "192.168.4.7", "172.16.9.9"])
def test_private_ipv4_is_accepted(ip, audit, consent):
    agent, auto = sentinel(audit)
    reply = agent.handle(human_env("block_device", {"ip": ip, "confirmed": True}))
    assert reply["ok"] is True and reply["executed"] is True
    assert auto.calls[0][1]["ip"] == ip


def test_block_refused_when_own_address_is_unknown(audit, consent):
    """Not knowing what we must not block is a refusal, not a free pass."""
    agent, auto = sentinel(audit, protected=set())
    reply = agent.handle(human_env("block_device",
                                   {"ip": TARGET_IP, "confirmed": True}))
    assert reply["ok"] is False
    assert "refusing to block" in reply["error"]
    assert auto.calls == []


# ── auto-block policy ───────────────────────────────────────────────

def test_agent_may_autoblock_a_flagged_new_device(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    e = env("block_device", {"ip": TARGET_IP, "reason": "unknown device"})
    reply = agent.handle(e)

    assert reply["ok"] is True and reply["executed"] is True
    assert reply["authorized_by"] == "device_sentinel_new_unknown"
    assert auto.calls == [("block_device", {
        "action": "block", "ip": TARGET_IP, "reason": "unknown device",
        "authorized_by": "device_sentinel_new_unknown"})]


def test_agent_may_not_autoblock_an_unflagged_address(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert(ip="192.168.50.99")])
    reply = agent.handle(env("block_device", {"ip": TARGET_IP}))

    assert reply["ok"] is True and reply["executed"] is False
    assert reply["requires_confirmation"] is True
    assert reply["proposal"] == {"action": "block", "ip": TARGET_IP,
                                 "reason": "PuffPAI Sentinel"}
    assert auto.calls == []


def test_autoblock_matching_is_not_a_prefix_match(audit, consent):
    """192.168.50.5 must not be authorised by an alert about 192.168.50.50."""
    agent, auto = sentinel(audit, alerts=[new_device_alert(ip="192.168.50.50")])
    reply = agent.handle(env("block_device", {"ip": "192.168.50.5"}))
    assert reply["executed"] is False
    assert auto.calls == []


def test_acknowledged_alert_no_longer_authorises_an_autoblock(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert(acknowledged=True)])
    reply = agent.handle(env("block_device", {"ip": TARGET_IP}))
    assert reply["executed"] is False
    assert auto.calls == []


def test_non_ip_alert_does_not_authorise_an_autoblock(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert(source="usb")])
    reply = agent.handle(env("block_device", {"ip": TARGET_IP}))
    assert reply["executed"] is False
    assert auto.calls == []


def test_no_monitoring_means_no_autoblock(audit, consent):
    agent, auto = sentinel(audit, alerts=[])
    reply = agent.handle(env("block_device", {"ip": TARGET_IP}))
    assert reply["executed"] is False
    assert "no open Device Sentinel alerts" in reply["reason"]
    assert auto.calls == []


def test_human_confirmation_blocks_an_unflagged_address(audit, consent):
    agent, auto = sentinel(audit, alerts=[])
    reply = agent.handle(human_env("block_device",
                                   {"ip": TARGET_IP, "confirmed": True}))
    assert reply["ok"] is True and reply["executed"] is True
    assert auto.calls[0][1]["action"] == "block"


# ── unblock is always human-confirmed ───────────────────────────────

def test_unblock_without_confirmation_is_a_proposal_only(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    reply = agent.handle(env("unblock_device", {"ip": TARGET_IP}))
    assert reply["ok"] is True and reply["executed"] is False
    assert reply["requires_confirmation"] is True
    assert auto.calls == []


def test_an_agent_cannot_confirm_its_own_unblock(audit, consent):
    """confirmed=True from an agent origin is not a human confirmation."""
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    reply = agent.handle(env("unblock_device",
                             {"ip": TARGET_IP, "confirmed": True},
                             origin="concierge"))
    assert reply["executed"] is False
    assert auto.calls == []


def test_an_agent_cannot_unblock_by_naming_itself_a_human(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    reply = agent.handle(env("unblock_device",
                             {"ip": TARGET_IP, "confirmed": True},
                             origin="sentinel"))
    assert reply["executed"] is False
    assert auto.calls == []


@pytest.mark.parametrize("confirmed", [False, None, "true", 1, "yes"])
def test_unblock_needs_confirmed_exactly_true(confirmed, audit, consent):
    agent, auto = sentinel(audit)
    reply = agent.handle(human_env("unblock_device",
                                   {"ip": TARGET_IP, "confirmed": confirmed}))
    assert reply["executed"] is False
    assert auto.calls == []


def test_human_confirmed_unblock_executes(audit, consent):
    agent, auto = sentinel(audit)
    reply = agent.handle(human_env("unblock_device",
                                   {"ip": TARGET_IP, "confirmed": True}))
    assert reply["ok"] is True and reply["executed"] is True
    assert auto.calls == [("block_device", {
        "action": "unblock", "ip": TARGET_IP, "reason": "PuffPAI Sentinel"})]


# ── review + budget ─────────────────────────────────────────────────

def test_block_populates_targets_so_the_consent_gate_engages(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    e = env("block_device", {"ip": TARGET_IP})
    agent.handle(e)
    assert e.payload["targets"] == [FIREWALL_CONSENT_TARGET]
    assert e.payload["egress"] is True


@pytest.mark.parametrize("supplied", [[], "", None, ["light.kitchen"], "junk", 7])
def test_a_caller_cannot_switch_the_consent_gate_off(supplied, audit, consent):
    """targets/egress are overwritten, not merged — no opt-out via payload."""
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    e = env("block_device", {"ip": TARGET_IP, "targets": supplied, "egress": False})
    agent.handle(e)
    assert e.payload["targets"] == [FIREWALL_CONSENT_TARGET]
    assert e.payload["egress"] is True


def test_block_denied_when_firewall_control_is_not_consented(audit, consent):
    """Fail-closed default: the household has not approved agent firewall control."""
    consent.deny(FIREWALL_CONSENT_TARGET, by="owner")
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    reply = agent.handle(env("block_device", {"ip": TARGET_IP}))
    assert reply["ok"] is False and reply["denied"] is True
    assert "not consented" in reply["reason"]
    assert auto.calls == []


def test_block_is_metered_as_egress(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    e = env("block_device", {"ip": TARGET_IP})
    agent.handle(e)
    assert e.budget.actions_used == 1


def test_block_is_refused_by_a_budget_that_forbids_egress(audit, consent):
    """Two layers: the review denies it, and consume(egress=True) would raise."""
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    e = env("block_device", {"ip": TARGET_IP}, egress=False)
    reply = agent.handle(e)
    assert reply["ok"] is False and reply["denied"] is True
    assert "egress" in reply["reason"]
    assert auto.calls == []
    assert e.budget.actions_used == 0


def test_exhausted_action_budget_aborts_before_the_call(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    e = env("block_device", {"ip": TARGET_IP}, actions=1)
    e.budget.consume(1)
    with pytest.raises(BudgetExceeded):
        agent.handle(e)
    assert auto.calls == []


def test_webhook_failure_is_reported_not_raised(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()], ok=False)
    reply = agent.handle(env("block_device", {"ip": TARGET_IP}))
    assert reply["ok"] is False and reply["executed"] is False
    assert "automation engine" in reply["error"]


def test_reason_is_sanitized(audit, consent):
    agent, auto = sentinel(audit, alerts=[new_device_alert()])
    agent.handle(env("block_device",
                     {"ip": TARGET_IP, "reason": "bad\nguy\x00 " + "x" * 500}))
    reason = auto.calls[0][1]["reason"]
    assert "\n" not in reason and "\x00" not in reason
    assert len(reason) <= 200


# ── existing behaviour still intact ─────────────────────────────────

def test_unknown_intent_still_answers(audit, consent):
    agent, _ = sentinel(audit)
    assert agent.handle(env("nope", state_changing=False))["ok"] is False


def test_status_and_alerts_still_work(audit, consent):
    agent, _ = sentinel(audit, alerts=[new_device_alert()])
    assert agent.handle(env("security_status",
                            state_changing=False))["status"]["available"] is True
    assert len(agent.handle(env("alerts", state_changing=False))["alerts"]) == 1
