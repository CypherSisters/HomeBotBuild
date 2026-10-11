"""Sentinel — WF-3 privacy & security enforcement (the veto + audit sidecar).

Sentinel is the only agent with veto authority. The bus hands it every
``state_changing`` envelope *before* delivery; :meth:`review` returns a
:class:`~puffpai.agents.envelope.ReviewVerdict` and the bus blocks on a deny.

The verdict reuses the existing, tested policy engine in
``puffpai.security.consent_gate`` (per-device consent, blast radius,
safety-critical domains, external egress) rather than re-implementing policy:

* ``payload["targets"]`` — a resolved entity list → :func:`gate_targets`.
* ``payload["automation"]`` — an LLM-authored automation dict → :func:`sentinel_review`.
* ``payload["egress"]`` truthy but ``budget.allow_egress`` false → deny.
* any target currently quarantined by the :class:`DeviceSentinel` → deny.

Evolves from the existing Sentinel/DeviceSentinel; also wraps DeviceSentinel
status so the team can read quarantine/monitoring state through one agent.

Sentinel also owns the team's one network-policy actuator:
:meth:`block_device` / :meth:`unblock_device`, which reach the host firewall
only through the loopback Node-RED ``block-device`` flow. Policy:

* **block** may run autonomously *only* for an address the DeviceSentinel has
  itself flagged as a new, unknown device. Anything else is a proposal the
  household has to confirm.
* **unblock** is the always-confirm class, like the Steward's siren: an agent can
  never restore a device's network access on its own.
* both are ``state_changing`` and egress-classed, and both are reviewed — see
  :meth:`_review_own_action` for why that review has to happen here rather than
  in ``bus.send``.

Credentials: AdGuard/firewall passwords + the audit-ledger write key
(``AgentScope.SENTINEL``). No outbound/cloud credentials.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket

from puffpai.logging_config import get_logger
from puffpai.security.audit_log import AuditLog
from puffpai.security.consent_gate import gate_targets, sentinel_review
from puffpai.security.secrets_provider import AgentScope, SecretsProvider

from .base import BaseAgent
from .envelope import Envelope, ReviewVerdict

logger = get_logger(__name__)


# ── intent classification (mirrors steward.py) ──────────────────────
#: Intents that mutate network policy. The minter must set
#: ``state_changing=True``; :meth:`SentinelAgent.handle` refuses them otherwise.
STATE_CHANGING_INTENTS = frozenset({"block_device", "unblock_device"})

#: Never autonomous. Proposing is always allowed; executing
#: needs a human-originated confirmation on the envelope.
CONFIRM_REQUIRED_INTENTS = frozenset({"unblock_device"})

#: ``origin_agent`` values that represent a human entry point rather than an
#: agent. Agents stamp their own ``self.name`` when they mint an envelope, so an
#: agent cannot place itself in this set without lying about its identity.
HUMAN_ORIGINS = frozenset({"human", "user", "owner", "api", "dashboard", "voice"})

#: The consent-ledger target for "the agent team may change firewall policy".
#: The blocked device itself is deliberately *not* the consent target: we are not
#: controlling that device, we are changing our own network policy, and keying on
#: the device would invert the gate (an unknown, unconsented device — exactly the
#: one worth blocking — would be the one the gate refuses to let us block).
#: Approve it once with ``POST /sentinel/consent/requests/firewall.puffpai``
#: ``{"approve": true}``; until then every agent-initiated block is denied, which
#: is the fail-closed default.
FIREWALL_CONSENT_TARGET = "firewall.puffpai"

#: Node-RED webhook name (see :data:`puffpai.automation_client.KNOWN_WEBHOOKS`).
FIREWALL_WEBHOOK = "block_device"

#: Extra addresses that must never be blocked, beyond the ones detected at
#: runtime. Comma-separated IPv4 list; useful for the HA host or a NAS whose
#: address the appliance cannot derive.
PROTECTED_IPS_ENV = "PUFFPAI_PROTECTED_IPS"

MAX_REASON_LEN = 200

#: Payload keys that name entities an envelope would act on. The consent gate
#: keys on entity ids, so the review has to find them whatever the payload calls
#: them: the automation-authoring path writes ``targets``, every Steward
#: actuator writes ``entity_id``. Deliberately excludes ``target``, which in a
#: Majordomo ``enqueue`` payload is an *agent name*, not an entity.
TARGET_PAYLOAD_KEYS = ("targets", "entity_id", "entity_ids")


def collect_targets(payload) -> list[str]:
    """Every entity id *payload* would act on, under any of its known key names.

    Accepts a bare string or any sequence of strings per key; non-strings and
    blanks are dropped and the result is deduplicated, so a caller that passes
    both ``entity_id`` and a ``targets`` list naming the same entity is gated
    once. Callers that already populate ``targets`` are unaffected.
    """
    if not isinstance(payload, dict):
        return []
    found: set[str] = set()
    for key in TARGET_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            value = (value,)
        elif not isinstance(value, (list, tuple, set, frozenset)):
            continue
        for item in value:
            if isinstance(item, str) and item.strip():
                found.add(item.strip())
    return sorted(found)


class FirewallPolicyError(ValueError):
    """A block/unblock was refused before anything left the appliance."""


class SentinelAgent(BaseAgent):
    """Policy reviewer + security-state reporter."""

    name = "sentinel"
    scope = AgentScope.SENTINEL

    def __init__(self, *, secrets: SecretsProvider | None = None,
                 audit: AuditLog | None = None, device_sentinel=None,
                 automation_client=None, protected_ips=None) -> None:
        super().__init__(secrets=secrets)
        self._audit = audit or AuditLog()
        self._device_sentinel = device_sentinel  # injected in tests; lazy otherwise
        self._automation = automation_client     # ditto
        self._protected_ips = (
            {str(ip) for ip in protected_ips} if protected_ips is not None else None)

    # ── policy review (the veto) ────────────────────────────────────
    def review(self, envelope: Envelope, target: str | None = None) -> ReviewVerdict:
        """Rule on a state-changing envelope. Called by the bus, not via send().

        Returns allowed=False to structurally block. Every verdict is audited.
        """
        payload = envelope.payload or {}
        reasons: list[str] = []
        verdict = "allow"

        # 1. Resolved target entities → consent gate.
        # Collected by *meaning*, not by one key name: the automation-authoring
        # path says ``targets`` while every Steward actuator says ``entity_id``,
        # and keying only on the former meant control_device, set_climate,
        # list_add, announce and siren were reviewed unconditionally — the veto
        # we rely on never looked at what they would touch. See collect_targets.
        targets = collect_targets(payload)
        if targets:
            gate = gate_targets(
                list(targets),
                name=str(payload.get("name", envelope.intent)),
                task_text=str(payload.get("task_text", ""))[:200],
                authored_by=envelope.origin_agent,
            )
            if gate["action"] == "deny":
                verdict = "deny"
                reasons.extend(gate["reasons"])
            elif gate["action"] == "flag":
                if verdict != "deny":
                    verdict = "flag"
                reasons.extend(gate["reasons"])

            quarantined = self._quarantined_entities()
            hit = [t for t in targets if t in quarantined]
            if hit:
                verdict = "deny"
                reasons.append(f"target(s) quarantined by DeviceSentinel: {hit[:5]}")

        # 2. LLM-authored automation dict → the schema-aware review.
        auto = payload.get("automation")
        if isinstance(auto, dict):
            r = sentinel_review(auto, payload.get("states"),
                                task_text=str(payload.get("task_text", "")))
            if r["action"] == "deny":
                verdict = "deny"
                reasons.extend(r["reasons"])
            elif r["action"] == "flag" and verdict != "deny":
                verdict = "flag"
                reasons.extend(r["reasons"])

        # 3. Egress must be budgeted.
        if payload.get("egress") and not envelope.budget.allow_egress:
            verdict = "deny"
            reasons.append("egress requested but envelope budget forbids egress")

        allowed = verdict != "deny"
        self._audit.log(
            action="sentinel_review",
            user=envelope.requester,
            result=("allow" if allowed else "deny"),
            details=f"{envelope.origin_agent}->{target}:{envelope.intent}",
            extra={"verdict": verdict, "reasons": reasons,
                   "envelope": envelope.audit_view()},
        )
        return ReviewVerdict(allowed=allowed, verdict=verdict,
                             reason="; ".join(reasons) or "ok")

    # ── request handling (non-veto) ─────────────────────────────────
    def handle(self, envelope: Envelope) -> dict:
        intent = envelope.intent
        p = envelope.payload or {}

        # A network-policy change that arrived on a read-only envelope was never
        # declared for review. Fail closed. (Mirrors steward.handle.)
        if intent in STATE_CHANGING_INTENTS and not envelope.state_changing:
            return {
                "ok": False,
                "requires_review": True,
                "error": f"sentinel: {intent!r} is state-changing; the envelope "
                         "must set state_changing=True so it is reviewed",
            }

        if intent in ("device_status", "status", "security_status"):
            return {"ok": True, "status": self._sentinel_status()}
        if intent in ("alerts", "list_alerts"):
            return {"ok": True, "alerts": self._sentinel_alerts()}
        if intent == "review":
            v = self.review(envelope, target="sentinel")
            return {"ok": True, "allowed": v.allowed, "verdict": v.verdict,
                    "reason": v.reason}
        try:
            if intent == "block_device":
                return self._block_device(envelope, p)
            if intent == "unblock_device":
                return self._unblock_device(envelope, p)
        except FirewallPolicyError as exc:
            return {"ok": False, "error": f"sentinel: {exc}"}
        return {"ok": False, "error": f"sentinel: unknown intent {intent!r}"}

    # ── firewall actuation via the loopback Node-RED flow ───────────
    def _block_device(self, envelope: Envelope, p: dict) -> dict:
        """Block an IPv4 address at the host firewall.

        Autonomous only for an address the DeviceSentinel has flagged as a new,
        unknown device (quarantine policy). Any other address needs the same
        three-condition human gate the Steward uses for sirens. An unauthorised
        call is not an error: it returns the exact proposal so Concierge can ask
        the household, and makes no HTTP request.
        """
        ip = self._validated_ip(p.get("ip"))
        reason = self._clean_reason(p.get("reason"))
        proposal = {"action": "block", "ip": ip, "reason": reason}

        if not self._human_confirmed(envelope, p):
            flagged, why = self._is_new_and_unknown(ip)
            if not flagged:
                return {
                    "ok": True, "executed": False, "requires_confirmation": True,
                    "proposal": proposal,
                    "reason": "auto-block is limited to devices the Device "
                              f"Sentinel flagged as new and unknown ({why}); "
                              "anything else needs payload confirmed=True from a "
                              f"human origin (got {envelope.origin_agent!r})",
                }
            proposal["authorized_by"] = "device_sentinel_new_unknown"

        return self._call_firewall_flow(envelope, proposal)

    def _unblock_device(self, envelope: Envelope, p: dict) -> dict:
        """Restore a blocked address — **always-confirm class**.

        Never autonomous, whatever the DeviceSentinel thinks: undoing a
        quarantine is the household's call, not an agent's. Execution requires
        all three, and the agent team can satisfy only the first two on its own:

        1. ``state_changing=True`` on the envelope (checked in :meth:`handle`),
        2. ``payload["confirmed"] is True``,
        3. ``origin_agent`` in :data:`HUMAN_ORIGINS` — a human entry point.
           Agents stamp their own ``name`` as ``origin_agent``, so an agent
           minting its own confirmation lands outside this set and is refused.
        """
        ip = self._validated_ip(p.get("ip"))
        reason = self._clean_reason(p.get("reason"))
        proposal = {"action": "unblock", "ip": ip, "reason": reason}

        if not self._human_confirmed(envelope, p):
            return {
                "ok": True, "executed": False, "requires_confirmation": True,
                "proposal": proposal,
                "reason": "unblock is always-confirm: needs payload confirmed=True "
                          f"from a human origin (got {envelope.origin_agent!r})",
            }
        return self._call_firewall_flow(envelope, proposal)

    def _call_firewall_flow(self, envelope: Envelope, proposal: dict) -> dict:
        """Review, meter, then POST to the Node-RED ``block-device`` webhook."""
        verdict = self._review_own_action(envelope)
        if not verdict.allowed:
            return {"ok": False, "denied": True, "by": self.name,
                    "verdict": verdict.verdict, "reason": verdict.reason,
                    "proposal": proposal}

        # Egress-classed: leaving the process at all is metered, and a budget
        # that forbids egress raises BudgetExceeded, which the bus audits.
        envelope.budget.consume(1, egress=True)

        ok, data = self._automation_client().call(FIREWALL_WEBHOOK, dict(proposal))
        if not ok:
            logger.warning("firewall %s of %s failed: %s",
                           proposal["action"], proposal["ip"], data)
            return {"ok": False, "executed": False, **proposal,
                    "error": f"automation engine: {data}"}
        return {"ok": True, "executed": True, **proposal, "result": data}

    def _review_own_action(self, envelope: Envelope) -> ReviewVerdict:
        """Run the policy review on Sentinel's *own* state-changing envelope.

        ``AgentBus.send`` skips review when the recipient *is* the reviewer, so
        an envelope addressed to ``sentinel`` reaches :meth:`handle` unreviewed.
        Self-review is weaker than an independent one — but it still runs the
        consent gate, the quarantine check and the egress-budget check, and it
        writes the same audit record, which is strictly better than the veto
        silently not applying. Populating ``targets`` is what engages the gate.

        Both keys are *overwritten*, never merged: a caller that supplied
        ``targets: []`` would otherwise switch the consent gate off, and one that
        supplied ``egress: False`` would skip the egress check.
        """
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        payload["targets"] = [FIREWALL_CONSENT_TARGET]
        payload["egress"] = True
        envelope.payload = payload
        return self.review(envelope, target=self.name)

    def _automation_client(self):
        if self._automation is None:
            from puffpai.automation_client import AutomationClient
            self._automation = AutomationClient()
        return self._automation

    # ── firewall input validation (never trust the flow alone) ──────
    def _validated_ip(self, value) -> str:
        """Return a safe, blockable IPv4 string or raise.

        The Node-RED flow validates too; this is the Python-side copy so a
        caller that ever reaches the firewall by another route is still checked.
        Rejected: non-strings, anything with a shell metacharacter, non-IPv4
        (every firewall rule in this project is v4), loopback, multicast,
        link-local, unspecified, broadcast, reserved, non-private addresses, and
        the appliance's own address or its default gateway.
        """
        from puffpai.firewall_manager import validate_ip

        try:
            addr = validate_ip(value)
        except ValueError as exc:
            raise FirewallPolicyError(str(exc)) from None
        if not isinstance(addr, ipaddress.IPv4Address):
            raise FirewallPolicyError(
                f"only IPv4 addresses can be blocked, got {addr}")
        if not addr.is_private:
            raise FirewallPolicyError(
                f"{addr} is not a private LAN address; this firewall manages "
                "local device policy only")

        protected = self._protected_addresses()
        if not protected:
            raise FirewallPolicyError(
                "cannot determine the appliance's own address or gateway; "
                "refusing to block anything rather than risk cutting the "
                f"appliance off (set ${PROTECTED_IPS_ENV} to override)")
        if str(addr) in protected:
            raise FirewallPolicyError(
                f"{addr} is infrastructure (appliance or gateway); blocking it "
                "would take the network down")
        return str(addr)

    @staticmethod
    def _clean_reason(value) -> str:
        if not isinstance(value, str) or not value.strip():
            return "PuffPAI Sentinel"
        cleaned = "".join(ch if ch.isprintable() else " " for ch in value)
        return " ".join(cleaned.split())[:MAX_REASON_LEN] or "PuffPAI Sentinel"

    def _protected_addresses(self) -> set[str]:
        """Addresses that must never be blocked. Cached; never raises."""
        if self._protected_ips is not None:
            return self._protected_ips
        found: set[str] = set()
        for raw in os.environ.get(PROTECTED_IPS_ENV, "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                found.add(str(ipaddress.IPv4Address(raw)))
            except ValueError:
                logger.warning("ignoring bad entry in $%s", PROTECTED_IPS_ENV)
        own = self._own_ipv4()
        if own:
            found.add(own)
        gw = self._default_gateway()
        if gw:
            found.add(gw)
        self._protected_ips = found
        return found

    @staticmethod
    def _own_ipv4() -> str | None:
        """This host's default-route source address. Sends no packets."""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return sock.getsockname()[0]
        except Exception as exc:
            logger.debug("own-address detection failed: %s", exc)
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    @staticmethod
    def _default_gateway() -> str | None:
        """IPv4 default gateway from ``/proc/net/route`` (Linux). None elsewhere."""
        try:
            with open("/proc/net/route", encoding="utf-8") as fh:
                next(fh, None)  # header
                for line in fh:
                    parts = line.split()
                    if len(parts) > 2 and parts[1] == "00000000":
                        raw = int(parts[2], 16)
                        octets = [(raw >> shift) & 0xFF for shift in (0, 8, 16, 24)]
                        return ".".join(str(o) for o in octets)
        except Exception as exc:
            logger.debug("gateway detection unavailable: %s", exc)
        return None

    @staticmethod
    def _human_confirmed(envelope: Envelope, p: dict) -> bool:
        return (p.get("confirmed") is True
                and envelope.origin_agent in HUMAN_ORIGINS)

    def _is_new_and_unknown(self, ip: str) -> tuple[bool, str]:
        """Has the DeviceSentinel itself flagged *ip* as a new, unknown device?

        The evidence has to come from the monitor, never from the caller's
        payload — an agent asserting "this device is unknown" would make the
        policy self-certifying. Matching is on the DeviceSentinel's own
        unacknowledged alert text, anchored so ``10.0.0.5`` does not match
        ``10.0.0.50``.
        """
        alerts = self._sentinel_alerts()
        if not alerts:
            return False, "no open Device Sentinel alerts"
        pattern = re.compile(rf"(?<![\d.]){re.escape(ip)}(?![\d.])")
        for alert in alerts:
            if not isinstance(alert, dict) or alert.get("source") != "ip":
                continue
            if alert.get("acknowledged"):
                continue
            blob = f"{alert.get('summary', '')} {alert.get('quarantine_detail', '')}"
            if pattern.search(blob):
                return True, f"alert {alert.get('id')}"
        return False, f"{ip} matches no open new-device alert"

    # ── DeviceSentinel wrapper (lazy, guarded) ──────────────────────
    def _sentinel(self):
        if self._device_sentinel is None:
            try:
                from puffpai.device_sentinel import get_sentinel
                self._device_sentinel = get_sentinel()
            except Exception as exc:  # monitoring optional; never crash review
                logger.debug("device sentinel unavailable: %s", exc)
                self._device_sentinel = None
        return self._device_sentinel

    def _sentinel_status(self) -> dict:
        ds = self._sentinel()
        if ds is None:
            return {"available": False}
        try:
            status = ds.status()
            status["available"] = True
            return status
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    def _sentinel_alerts(self) -> list:
        ds = self._sentinel()
        if ds is None:
            return []
        try:
            return ds.list_alerts()
        except Exception:
            return []

    def _quarantined_entities(self) -> set[str]:
        ds = self._sentinel()
        if ds is None:
            return set()
        found: set[str] = set()
        try:
            status = ds.status() or {}
            for key in ("quarantined", "quarantine", "blocked"):
                vals = status.get(key)
                if isinstance(vals, (list, tuple, set)):
                    found.update(str(v) for v in vals)
        except Exception:
            pass
        return found

    def health(self) -> dict:
        base = super().health()
        base["reviewer"] = True
        base["monitoring_available"] = self._sentinel() is not None
        return base


__all__ = [
    "CONFIRM_REQUIRED_INTENTS",
    "TARGET_PAYLOAD_KEYS",
    "collect_targets",
    "FIREWALL_CONSENT_TARGET",
    "FirewallPolicyError",
    "HUMAN_ORIGINS",
    "STATE_CHANGING_INTENTS",
    "SentinelAgent",
]
