"""Propose → owner approves → PuffPAI writes it, with a borrowed credential.

The goal: a person should not have to hand-write Home Assistant configuration
for every new device. If short scripts are needed to satisfy security
requirements, PuffPAI works that out and does it, not the user.

The constraint that makes it hard: the token PuffPAI carries all day belongs to
a deliberately NON-ADMIN Home Assistant user (a hardening measure), and Home
Assistant only lets administrators create scripts or automations
(``POST /api/config/script/config/<object_id>``) or call ``zha.permit``. Making
the everyday user an admin would meet the goal and destroy the hardening:
every tool call the model can emit would then be able to rewrite the house's
configuration.

So this module implements the third option, the tiered-capability shape from
the OpenClaw permission-taxonomy study — *a capability is unlocked per action
by the owner, never held permanently*:

    1. **Propose.** :func:`propose_ha_change` writes ONE consent request whose
       reason text contains the EXACT JSON that will be written, so the owner
       approves a specific artifact, not a category. It actuates nothing and
       touches no credential.
    2. **Consent.** The owner answers on one of the two human surfaces (chat
       ``sentinel_approve_device``, dashboard
       ``/sentinel/consent/requests/<entity_id>``), which record the verdict in
       the durable consent queue.
    3. **Borrow.** :func:`apply_approved_change` re-reads that durable record,
       demands the in-process :data:`~puffpai.ha_bridge.HUMAN_CONFIRMED` token,
       and only then asks
       :func:`~puffpai.security.ha_token_source.resolve_ha_admin_token` for the
       elevated credential — which exists only while the owner's vault is
       unlocked with their PIN (vault-SESSION tier, never the boot-unlocked
       service tier).
    4. **Perform, verify, audit, drop.** The write goes out on a THROWAWAY
       ``requests.Session`` that is closed in a ``finally``; the everyday
       bridge's session and token are never touched. The new entity is then
       verified through the EVERYDAY bridge (proving the restricted user can
       actually see it), an ``ha_provisioned`` activity event is filed with the
       entity id and no payload, and the local reference to the admin token
       goes out of scope. Nothing caches it.

What this is NOT
----------------
* **Not a delete or an edit.** v1 is CREATE-ONLY. If the object id already
  exists, the change is refused unless ``overwrite=True`` was part of what the
  owner approved. A model that can get "modify" approved can rewrite an
  existing automation the owner trusts; "create ``script.puffpai_*``" is a much
  smaller thing to reason about, and it is all Zigbee pairing needs.
* **Not reachable by an agent.** No tool, fastpath, planner step, or HTTP body
  can call :func:`apply_approved_change`: it requires a Python object identity
  that cannot be expressed in JSON, plus a fresh human-resolved record in the
  queue, plus an unlocked vault. The Steward's ``ha_provision_propose`` tool
  reaches :func:`propose_ha_change` and stops there.
* **Not a place to run code.** Payloads naming ``shell_command``,
  ``python_script`` or ``hassio`` services are refused outright
  (:data:`FORBIDDEN_SERVICE_DOMAINS`): those are arbitrary code execution on
  the Home Assistant host, and "the owner saw the YAML" is not a good enough
  control for that.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

# Same import discipline as puffpai.zigbee_pairing: consent_requests is a
# stdlib-only leaf and is safe at module scope; anything that reaches config or
# the secrets provider (ha_bridge, ha_token_source, requests) is imported at
# call time, because this module is imported from the API server.
from puffpai.security import consent_requests as _creq

logger = logging.getLogger(__name__)


# ── the consent question ────────────────────────────────────────────

#: Action kind in the consent queue. One kind for every provisioning change, so
#: a surface that has not been taught about a new ``kind`` still renders and
#: still routes the approval to :func:`apply_approved_change`.
PROVISION_ACTION = "ha_provision"

#: Only a human surface's answer counts. Same set, and the same reasoning, as
#: :data:`puffpai.zigbee_pairing.HUMAN_RESOLVERS`; restated rather than imported
#: because zigbee_pairing imports THIS module (importing back would be a cycle).
HUMAN_RESOLVERS = frozenset({"user_via_chat", "user_via_ui", "user_via_voice"})

#: An approval older than this is not a mandate for a write happening now.
APPROVAL_FRESHNESS_SECONDS = 300.0

#: An unanswered provisioning proposal ages out. It is a standing offer to
#: change the house's configuration; it should not survive the week.
REQUEST_TTL_SECONDS = 6 * 60 * 60


# ── what may be provisioned ─────────────────────────────────────────
#
# ``input_boolean`` and the other helper domains are deliberately absent: Home
# Assistant exposes helpers only over the WebSocket config-flow API
# (``config/helpers``/config-entry flows), not over a REST config endpoint like
# scripts and automations, so there is nothing here to POST to. Helpers stay a
# manual step until that is built against the WebSocket API and reviewed on its
# own merits.

#: kind -> (config endpoint template, reload service, entity domain)
SUPPORTED_KINDS: dict[str, tuple[str, str, str]] = {
    "script": ("config/script/config/{object_id}", "script/reload", "script"),
    "automation": ("config/automation/config/{object_id}",
                   "automation/reload", "automation"),
}

#: Service domains a provisioned payload may never name. These execute
#: arbitrary code (or reconfigure the supervisor) on the Home Assistant host;
#: an owner skimming a proposal is not an adequate control for that.
FORBIDDEN_SERVICE_DOMAINS = frozenset({
    "shell_command", "python_script", "hassio", "supervisor",
})

#: Object ids are HA slugs. Also the path segment of the config URL, so this
#: doubles as the traversal guard.
_OBJECT_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

#: A provisioned artifact is a few lines of YAML. A payload larger than this is
#: not something an owner read and understood in a consent banner.
MAX_PAYLOAD_BYTES = 8192

#: Owner kill switch. Default ON: the whole point is that PuffPAI does this
#: without the owner doing it by hand, and every path is already gated on an
#: explicit approval plus an unlocked vault. Set ``PUFFPAI_HA_PROVISIONING=0``
#: (or ``agents.ha_provisioning_enabled: false``) to close the door entirely.
ENABLED_ENV = "PUFFPAI_HA_PROVISIONING"
ENABLED_CONFIG_KEY = "ha_provisioning_enabled"

#: Bounded verification through the everyday bridge after a reload.
VERIFY_ATTEMPTS = 3
VERIFY_DELAY_SECONDS = 1.0

#: Network timeout for the two admin calls. Short: both are local.
HTTP_TIMEOUT_SECONDS = 15

_LOCK = threading.RLock()

#: ``resolved_at`` stamps already spent on a write. An approval is a one-shot
#: mandate — without this, one approval could be replayed into several writes
#: inside :data:`APPROVAL_FRESHNESS_SECONDS`.
_spent_approvals: set[str] = set()


def approval_spent(stamp) -> bool:
    """Spent in this process, or persisted as spent before a restart
    (:func:`puffpai.ha_pending_apply.was_used`). Fails closed."""
    if isinstance(stamp, str) and stamp in _spent_approvals:
        return True
    try:
        from puffpai import ha_pending_apply as _hpa
        return _hpa.was_used(stamp)
    except Exception as exc:
        logger.warning("ha provisioning: used-approval check failed; "
                       "refusing: %s", type(exc).__name__)
        return True


def _spend(stamp) -> None:
    """Mark spent in memory AND on disk. Caller holds ``_LOCK``."""
    _spent_approvals.add(stamp)
    try:
        from puffpai import ha_pending_apply as _hpa
        _hpa.note_used(stamp)
    except Exception as exc:                        # pragma: no cover
        logger.warning("ha provisioning: could not persist a used approval: "
                       "%s", type(exc).__name__)


def enabled() -> bool:
    """True unless the owner closed the door. Fail-safe toward ON, because the
    gates that matter (human approval, unlocked vault) are elsewhere and are
    not optional."""
    try:
        from puffpai.agents.preferences import flag
        return flag(ENABLED_ENV, ENABLED_CONFIG_KEY, default=True)
    except Exception as exc:
        logger.debug("ha provisioning flag unavailable: %s", type(exc).__name__)
        return True


def reset_state() -> None:
    """Forget spent approvals. For tests only."""
    with _LOCK:
        _spent_approvals.clear()
        try:
            import os
            from puffpai import ha_pending_apply as _hpa
            os.remove(_hpa._used_path())
        except Exception:
            pass


# ── validation (a trust boundary: the payload is model-written) ─────

class ProvisioningError(ValueError):
    """A proposal that must not become a consent request."""


def _validate_kind(kind) -> str:
    text = str(kind or "").strip().lower()
    if text not in SUPPORTED_KINDS:
        raise ProvisioningError(
            "I can only create Home Assistant scripts and automations "
            f"({', '.join(sorted(SUPPORTED_KINDS))}); Home Assistant has no "
            f"config API I can use for {text or 'that'}.")
    return text


def _validate_object_id(object_id) -> str:
    text = str(object_id or "").strip().lower()
    if not _OBJECT_ID_RE.fullmatch(text):
        raise ProvisioningError(
            "a Home Assistant object id is lowercase letters, digits and "
            "underscores only")
    return text


def _walk_services(node):
    """Yield every ``service:``/``action:`` value inside a payload."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("service", "action") and isinstance(value, str):
                yield value
            else:
                yield from _walk_services(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_services(item)


def _validate_payload(kind: str, payload) -> dict:
    if not isinstance(payload, dict) or not payload:
        raise ProvisioningError("the change has to be a non-empty object")
    try:
        rendered = json.dumps(payload, indent=2, sort_keys=True,
                              ensure_ascii=False)
    except (TypeError, ValueError):
        raise ProvisioningError("that change isn't something I can write as "
                                "Home Assistant config")
    if len(rendered.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ProvisioningError("that change is too large to put in front of "
                                "you for approval")
    if kind == "script" and not isinstance(payload.get("sequence"), list):
        raise ProvisioningError("a script needs a 'sequence' list")
    if kind == "automation":
        _validate_automation(payload, rendered)
    for service in _walk_services(payload):
        domain = service.split(".", 1)[0].strip().lower()
        if domain in FORBIDDEN_SERVICE_DOMAINS:
            raise ProvisioningError(
                f"I won't create something that calls {domain}: that runs "
                f"arbitrary code on the Home Assistant host, and no approval "
                f"banner is a good enough check on that.")
    return payload


# ── the automation allowlist ────────────────────────────────────────
#
# An approved automation runs unattended, forever, with Home Assistant's own
# authority. So what PuffPAI may create is a closed shape enforced HERE, in the
# one function both propose and apply call, not in a prompt:
#
# * actions: plain service calls to light/switch/fan turn_on|turn_off|toggle
#   and scene.turn_on, targeted by entity_id only (no area/device/label
#   targets, which name things nobody can see in the banner);
# * triggers: time (a literal clock time), sun, state;
# * conditions: state, time, sun;
# * no templates anywhere, no blueprints, no variables, no nested control flow.
#
# Scripts keep the older rule (forbidden code-execution domains only): the only
# scripts this appliance proposes are the two fixed Zigbee permit scripts.

AUTOMATION_ACTION_DOMAINS = frozenset({"light", "switch", "fan", "scene"})
AUTOMATION_SERVICES = frozenset({"turn_on", "turn_off", "toggle"})
AUTOMATION_TRIGGER_PLATFORMS = frozenset({"time", "sun", "state"})
AUTOMATION_CONDITIONS = frozenset({"state", "time", "sun"})
AUTOMATION_STATE_DOMAINS = frozenset({"binary_sensor", "light", "switch", "fan"})
_AUTOMATION_TOP_KEYS = frozenset({
    "alias", "description", "mode", "id", "trigger", "triggers", "condition",
    "conditions", "action", "actions", "max", "max_exceeded"})
_TRIGGER_KEYS = frozenset({"platform", "trigger", "at", "event", "offset",
                           "entity_id", "to", "from", "for", "id", "alias",
                           "enabled"})
_CONDITION_KEYS = frozenset({"condition", "entity_id", "state", "for", "after",
                             "before", "after_offset", "before_offset",
                             "weekday", "alias", "enabled"})
_ACTION_KEYS = frozenset({"service", "action", "target", "data", "entity_id",
                          "alias", "enabled"})
_ACTION_DATA_KEYS = frozenset({"brightness_pct", "brightness", "transition",
                               "color_temp_kelvin", "kelvin", "percentage"})
_ENTITY_RE = re.compile(r"[a-z_]{1,64}\.[a-z0-9_]{1,128}")
_AUTOMATION_ID_RE = re.compile(r"automation\.[a-z0-9_]{1,128}")
#: A switch whose id, name or device class reads like one of these can open the
#: house or the radio. It never goes into an automation.
_RISKY_SWITCH_RE = re.compile(
    r"(?<![a-z])(?:garage|doors?|locks?|gates?|alarms?|permit|pair|pairing|"
    r"join|joining|opener|siren|valves?|security|arm|armed|disarm)(?![a-z])",
    re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
_OFFSET_RE = re.compile(r"^-?\d{1,2}:[0-5]\d:[0-5]\d$")


def _as_list(value, what: str) -> list:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise ProvisioningError(f"the automation's {what} must be a list")


def _entity_ids(value, what: str) -> list[str]:
    ids = [value] if isinstance(value, str) else value
    if not isinstance(ids, list) or not ids:
        raise ProvisioningError(f"the automation's {what} must name entities")
    for eid in ids:
        if not isinstance(eid, str) or not _ENTITY_RE.fullmatch(eid):
            raise ProvisioningError(f"{eid!r} is not an entity id")
    return ids


def _validate_automation(payload: dict, rendered: str) -> None:
    """Enforce the closed automation shape. Raises ProvisioningError."""
    if "{{" in rendered or "{%" in rendered:
        raise ProvisioningError("I don't create automations with templates in "
                                "them; they can do things the banner can't show.")
    extra = set(payload) - _AUTOMATION_TOP_KEYS
    if extra:
        raise ProvisioningError("I only create plain automations (unexpected "
                                f"{', '.join(sorted(extra))})")
    if payload.get("mode", "single") not in ("single", "restart", "queued"):
        raise ProvisioningError("an automation I create runs in single, "
                                "restart or queued mode")
    if "max" in payload and not (isinstance(payload["max"], int)
                                 and not isinstance(payload["max"], bool)
                                 and 1 <= payload["max"] <= 10):
        raise ProvisioningError("max must be 1 to 10")
    if not (_as_list(payload.get("trigger"), "triggers")
            or _as_list(payload.get("triggers"), "triggers")):
        raise ProvisioningError("an automation needs a trigger")
    if not (_as_list(payload.get("action"), "actions")
            or _as_list(payload.get("actions"), "actions")):
        raise ProvisioningError("an automation needs an action")
    for key in ("trigger", "triggers"):
        for trig in _as_list(payload.get(key), "triggers"):
            if not isinstance(trig, dict) or set(trig) - _TRIGGER_KEYS:
                raise ProvisioningError("unsupported trigger shape")
            if "platform" in trig and "trigger" in trig:
                raise ProvisioningError("a trigger names its type once")
            platform = trig.get("platform") or trig.get("trigger")
            if platform not in AUTOMATION_TRIGGER_PLATFORMS:
                raise ProvisioningError(
                    f"I only create automations triggered by a time, the sun "
                    f"or a sensor state, not {platform!r}")
            if platform == "time" and not (isinstance(trig.get("at"), str)
                                           and _CLOCK_RE.fullmatch(trig["at"])):
                raise ProvisioningError("a time trigger needs a clock time")
            if platform == "sun":
                if trig.get("event") not in ("sunset", "sunrise"):
                    raise ProvisioningError("a sun trigger is sunset or sunrise")
                off = trig.get("offset", "0:00:00")
                if not (isinstance(off, str) and _OFFSET_RE.fullmatch(off)):
                    raise ProvisioningError("a sun offset must be HH:MM:SS")
            if platform == "state":
                for eid in _entity_ids(trig.get("entity_id"), "trigger"):
                    if eid.split(".", 1)[0] not in AUTOMATION_STATE_DOMAINS:
                        raise ProvisioningError(
                            f"I don't watch {eid} in an automation")
    for key in ("condition", "conditions"):
        for cond in _as_list(payload.get(key), "conditions"):
            if not isinstance(cond, dict) or set(cond) - _CONDITION_KEYS \
                    or cond.get("condition") not in AUTOMATION_CONDITIONS:
                raise ProvisioningError("unsupported condition")
            if cond.get("condition") == "state":
                for eid in _entity_ids(cond.get("entity_id"), "condition"):
                    if eid.split(".", 1)[0] not in AUTOMATION_STATE_DOMAINS:
                        raise ProvisioningError(f"I don't check {eid} in an "
                                                f"automation")
    for key in ("action", "actions"):
        for step in _as_list(payload.get(key), "actions"):
            if not isinstance(step, dict) or set(step) - _ACTION_KEYS:
                raise ProvisioningError("each action must be a plain service "
                                        "call")
            if "service" in step and "action" in step:
                raise ProvisioningError("an action names its service once")
            service = step.get("service") or step.get("action")
            if not isinstance(service, str) or service.count(".") != 1:
                raise ProvisioningError("each action must be a plain service "
                                        "call")
            domain, name = service.split(".", 1)
            if domain not in AUTOMATION_ACTION_DOMAINS \
                    or name not in AUTOMATION_SERVICES \
                    or (domain == "scene" and name != "turn_on"):
                raise ProvisioningError(
                    f"an automation I create may only turn lights, switches, "
                    f"fans or scenes on or off, not call {service}")
            target = step.get("target")
            if target is not None and (not isinstance(target, dict)
                                       or set(target) - {"entity_id"}):
                raise ProvisioningError("actions must target named entities "
                                        "only")
            ids = (target or {}).get("entity_id", step.get("entity_id"))
            for eid in _entity_ids(ids, "action"):
                if eid.split(".", 1)[0] != domain:
                    raise ProvisioningError(f"{service} can't target {eid}")
                if domain == "switch" and _RISKY_SWITCH_RE.search(eid):
                    raise ProvisioningError(_risky_switch_reason(eid))
            data = step.get("data")
            if data is not None:
                if not isinstance(data, dict) or set(data) - _ACTION_DATA_KEYS \
                        or not all(isinstance(v, (int, float))
                                   and not isinstance(v, bool)
                                   for v in data.values()):
                    raise ProvisioningError("unsupported action data")


def _risky_switch_reason(name: str) -> str:
    return (f"I won't put {name} in an automation: it looks like a garage, "
            f"door, lock, gate, alarm or pairing control.")


def _action_targets(payload: dict, domain: str) -> list:
    out = []
    for key in ("action", "actions"):
        for step in _as_list(payload.get(key), "actions"):
            service = str(step.get("service") or step.get("action") or "")
            if service.startswith(domain + "."):
                ids = (step.get("target") or {}).get("entity_id",
                                                      step.get("entity_id"))
                out.extend([ids] if isinstance(ids, str) else list(ids or []))
    return out


def _automation_targets_safe(bridge, payload: dict) -> str | None:
    """Scenes can carry locks and alarms. Refuse a scene target whose members
    are outside light/switch/fan. Fail closed when membership can't be read.
    Returns a refusal sentence, or None when safe."""
    scenes = []
    for key in ("action", "actions"):
        for step in _as_list(payload.get(key), "actions"):
            service = str(step.get("service") or step.get("action") or "")
            if service.startswith("scene."):
                ids = (step.get("target") or {}).get("entity_id",
                                                      step.get("entity_id"))
                scenes.extend([ids] if isinstance(ids, str) else list(ids or []))
    switches = _action_targets(payload, "switch")
    if not scenes and not switches:
        return None
    if bridge is None:
        return ("I couldn't read Home Assistant to check those devices, so I "
                "haven't made it.")
    try:
        try:
            states = bridge.get_all_states(force=True)
        except TypeError:
            states = bridge.get_all_states()
    except Exception:
        return "I couldn't read what that scene does, so I haven't made it."
    by_id = {s.get("entity_id"): s for s in states or [] if isinstance(s, dict)}
    for sw in switches:
        attrs = (by_id.get(sw) or {}).get("attributes") or {}
        label = " ".join(str(x) for x in (sw, attrs.get("friendly_name") or "",
                                          attrs.get("device_class") or ""))
        if sw not in by_id or _RISKY_SWITCH_RE.search(label):
            return _risky_switch_reason(attrs.get("friendly_name") or sw)
    for scene in scenes:
        members = ((by_id.get(scene) or {}).get("attributes") or {}).get("entity_id")
        if not isinstance(members, list):
            return f"I couldn't read what {scene} does, so I haven't made it."
        bad = [m for m in members if str(m).split(".", 1)[0]
               not in ("light", "switch", "fan")]
        if bad:
            return (f"{scene} also controls {', '.join(bad[:3])}, so I won't put "
                    f"it in an automation.")
    return None


def _validate_replaces(replaces, new_entity_id: str) -> list[str]:
    out = []
    for eid in replaces or []:
        if not isinstance(eid, str) or not _AUTOMATION_ID_RE.fullmatch(eid):
            raise ProvisioningError(f"{eid!r} is not an automation I can switch off")
        if eid != new_entity_id and eid not in out:
            out.append(eid)
    return out[:4]


def render_payload(payload: dict) -> str:
    """The exact text the owner is shown and the exact object that is written.

    JSON with sorted keys: it is valid YAML (YAML 1.2 is a JSON superset), it
    needs no extra dependency, and — the load-bearing part — it is generated
    from the same dict that is POSTed, so the banner cannot show one thing
    while a different thing is written.
    """
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def entity_id_for(kind: str, object_id: str) -> str:
    return f"{SUPPORTED_KINDS[kind][2]}.{object_id}"


# ── piece 1: propose (writes a question; actuates nothing) ──────────

#: Must appear verbatim in every proposal so the owner is never surprised by
#: "approved but nothing happened".
VAULT_SENTENCE = ("This requires the vault to be unlocked — approve it with "
                  "the vault open and I'll do it straight away.")


def propose_ha_change(kind: str, object_id: str, payload: dict, reason: str,
                      requested_by: str = "steward", *,
                      overwrite: bool = False,
                      replaces: list | None = None,
                      bridge=None, conversation_id: str | None = None,
                      batch: str | None = None) -> dict:
    """File ONE consent request to create a Home Assistant object. **Writes nothing to HA.**

    The reason text carries the exact rendered payload, so the durable record
    the owner approved names the artifact, not a category. Idempotent per
    ``(entity_id, action)`` via
    :func:`~puffpai.security.consent_requests.create_request`: proposing the
    same thing twice refreshes the open question instead of stacking banners.
    """
    if not enabled():
        return {"ok": False, "proposed": False,
                "error": "ha provisioning is switched off",
                "message": ("Making changes in Home Assistant is switched off "
                            "on this appliance.")}

    kind = _validate_kind(kind)
    object_id = _validate_object_id(object_id)
    payload = _validate_payload(kind, payload)
    entity_id = entity_id_for(kind, object_id)
    replaces = _validate_replaces(replaces, entity_id)         if kind == "automation" else []
    if kind == "automation" and (_action_targets(payload, "scene")
                                 or _action_targets(payload, "switch")):
        try:
            live = _everyday_bridge(bridge)
        except Exception:
            live = None
        refusal = _automation_targets_safe(live, payload)
        if refusal:
            raise ProvisioningError(refusal)
    rendered = render_payload(payload)
    why = " ".join(str(reason or "").split())[:400]
    switch_off = (f" After creating it I will switch OFF {', '.join(replaces)} "
                  f"(not delete it)." if replaces else "")

    text = (
        f"Create a new Home Assistant {kind}, {entity_id}. "
        f"{why} "
        f"I can't do this with my everyday Home Assistant account — it isn't "
        f"an administrator, on purpose. With your approval I'll use the "
        f"administrator credential you stored in the vault, once, for this one "
        f"change, and then drop it. {VAULT_SENTENCE}{switch_off} "
        f"This is exactly what I will write:\n{rendered}")

    req = _creq.create_request(
        entity_id=entity_id,
        label=f"Home Assistant {kind}: {object_id}",
        action=PROVISION_ACTION,
        reason=text,
        requested_by=str(requested_by or "steward")[:40],
        context={"kind": kind, "object_id": object_id, "payload": payload,
                 "overwrite": bool(overwrite), "replaces": replaces,
                 "conversation_id": (str(conversation_id)[:80]
                                     if conversation_id else None),
                 "batch": (str(batch)[:40] if batch else None)},
        ttl_seconds=REQUEST_TTL_SECONDS,
    )
    logger.info("ha provisioning: proposed %s (awaiting the owner)", entity_id)
    return {"ok": True, "proposed": True, "executed": False,
            "awaiting_approval": True, "request": req,
            "entity_id": entity_id, "kind": kind,
            "message": (f"I've proposed a new Home Assistant {kind}, "
                        f"{entity_id}. Approve it on the dashboard (or say "
                        f"yes here) with the vault unlocked and I'll create "
                        f"it.")}


# ── piece 2: apply (the ONLY writer) ────────────────────────────────

def _recent_approval(preferred: dict | None = None,
                     max_age: float | None = None) -> dict | None:
    """The durable record of a human approving a provisioning change, or None.

    Same control as :func:`puffpai.zigbee_pairing._recent_approval`: re-read the
    queue rather than trust the caller, so the mandate is a persisted,
    human-resolved, fresh, unspent record — not an argument someone passed in.
    *preferred* only breaks ties; it is matched against the queue and never
    used in its place.
    """
    now = datetime.now(timezone.utc)
    best: dict | None = None
    best_at = None
    for req in _creq._load():
        if not isinstance(req, dict):
            continue
        if req.get("action") != PROVISION_ACTION:
            continue
        if req.get("status") != "approved":
            continue
        if req.get("resolved_by") not in HUMAN_RESOLVERS:
            continue
        stamp = req.get("resolved_at")
        if not isinstance(stamp, str) or approval_spent(stamp):
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        limit = APPROVAL_FRESHNESS_SECONDS
        if max_age is not None and preferred is not None:
            # A longer window only for ONE named record (an approval kept
            # waiting for the vault PIN, puffpai.ha_pending_apply).
            limit = max(limit, float(max_age))
        if (now - when).total_seconds() > limit:
            continue
        if preferred is not None:
            # The record the caller asked about, or nothing: never a different
            # fresh approval that happens to be in the queue.
            if preferred.get("resolved_at") == stamp \
                    and preferred.get("entity_id") == req.get("entity_id"):
                return req
            continue
        if best_at is None or when > best_at:
            best, best_at = req, when
    return best


def _ha_base_url() -> str:
    from puffpai.config import get_config
    return str(get_config().ha.url or "").rstrip("/")


def _everyday_bridge(bridge=None):
    if bridge is not None:
        return bridge
    from puffpai.ha_bridge import get_bridge
    return get_bridge()


def _entity_exists(bridge, entity_id: str) -> bool:
    """Does the EVERYDAY account already see this entity? Read-only."""
    try:
        try:
            states = bridge.get_all_states(force=True)
        except TypeError:                      # a double without the keyword
            states = bridge.get_all_states()
    except Exception as exc:
        logger.debug("ha provisioning: state read failed: %s",
                     type(exc).__name__)
        return False
    for raw in states or []:
        if isinstance(raw, dict) and raw.get("entity_id") == entity_id:
            return True
    return False


def _admin_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def apply_approved_change(request: dict | None = None, *, bridge=None,
                          human_confirmed=False,
                          vault_session_id: str | None = None,
                          freshness_seconds: float | None = None) -> dict:
    """Perform an APPROVED Home Assistant change. The only writer in the tree.

    Reachable from exactly two places, both of which have just recorded a
    human's answer with :func:`puffpai.security.consent_requests.resolve`:
    ``assistant_tools.sentinel_approve_device`` (chat) and
    ``api_server.sentinel_consent_resolve`` (dashboard).

    Three independent gates, and all three must hold:

    * *human_confirmed* must be the in-process
      :data:`puffpai.ha_bridge.HUMAN_CONFIRMED` object. JSON has no syntax for
      an object identity, so no tool-call argument and no HTTP body can supply
      it — the same control the actuation path uses.
    * :func:`_recent_approval` must find a fresh, human-resolved, unspent
      record in the durable queue, and the payload written is the one in THAT
      record, never one passed in by the caller.
    * :func:`~puffpai.security.ha_token_source.resolve_ha_admin_token` must
      return a credential, which happens only while the vault is unlocked.

    Never raises. Returns a dict with a ``message`` a person can act on.
    """
    from puffpai.ha_bridge import is_human_confirmation

    if not is_human_confirmation(human_confirmed,
                                 where="ha_provisioning.apply_approved_change"):
        logger.warning("ha provisioning: refused — no in-process human "
                       "confirmation token")
        return {"ok": False, "created": False,
                "error": "human confirmation token required",
                "message": ("I can't make that Home Assistant change — it has "
                            "to come from you approving it, not from me.")}

    if not enabled():
        return {"ok": False, "created": False,
                "error": "ha provisioning is switched off",
                "message": "Making changes in Home Assistant is switched off."}

    approval = _recent_approval(request, freshness_seconds)
    if approval is None:
        logger.warning("ha provisioning: refused — no fresh human approval on "
                       "record for %s", PROVISION_ACTION)
        return {"ok": False, "created": False,
                "error": "no approved Home Assistant change on record",
                "message": ("I don't have your approval on record for that "
                            "Home Assistant change, so I haven't made it.")}

    context = approval.get("context") or {}
    if context.get("kind") == AUTOMATION_OFF_KIND:
        return _apply_automation_off(approval, context, bridge,
                                     vault_session_id)
    try:
        kind = _validate_kind(context.get("kind"))
        object_id = _validate_object_id(context.get("object_id"))
        payload = _validate_payload(kind, context.get("payload"))
    except ProvisioningError as exc:
        logger.warning("ha provisioning: approved record is not applicable: %s",
                       exc)
        return {"ok": False, "created": False, "error": str(exc),
                "message": f"I can't apply that change: {exc}"}
    overwrite = bool(context.get("overwrite"))
    entity_id = entity_id_for(kind, object_id)
    try:
        replaces = _validate_replaces(context.get("replaces"), entity_id)             if kind == "automation" else []
    except ProvisioningError as exc:
        return {"ok": False, "created": False, "error": str(exc),
                "message": f"I can't apply that change: {exc}"}

    # The vault check comes BEFORE the approval is spent: "approved but the
    # vault is locked" must leave the owner able to unlock and ask again,
    # rather than burning their yes on a call that did nothing.
    from puffpai.security.ha_token_source import resolve_ha_admin_token
    resolution = resolve_ha_admin_token(vault_session_id)
    if not resolution.token:
        logger.info("ha provisioning: approved for %s but the elevated "
                    "credential is not available", entity_id)
        return {"ok": False, "created": False, "vault_locked": True,
                "entity_id": entity_id,
                "error": "elevated credential unavailable",
                "message": (f"You approved it, but the vault is locked, so I "
                            f"can't reach the Home Assistant administrator "
                            f"credential. {resolution.detail}. Unlock it and "
                            f"ask me again and I'll finish.")}

    stamp = approval.get("resolved_at")
    with _LOCK:
        if isinstance(stamp, str) and approval_spent(stamp):
            return {"ok": False, "created": False,
                    "error": "approval already used",
                    "message": "I've already made that change."}

    everyday = None
    try:
        everyday = _everyday_bridge(bridge)
    except Exception as exc:
        logger.debug("ha provisioning: everyday bridge unavailable: %s",
                     type(exc).__name__)

    if kind == "automation":
        refusal = (_automation_targets_safe(everyday, payload)
                   if everyday is not None else
                   ("I couldn't read Home Assistant to check that scene, so I "
                    "haven't made it." if "scene." in json.dumps(payload) else None))
        if refusal:
            return {"ok": False, "created": False, "entity_id": entity_id,
                    "error": "scene target refused", "message": refusal}

    # Create-only (v1). An id that already exists is refused unless the owner
    # approved an overwrite, because rewriting an automation the household
    # already relies on is a different decision from adding a new one.
    if not overwrite and everyday is not None \
            and _entity_exists(everyday, entity_id):
        logger.warning("ha provisioning: refused — %s already exists",
                       entity_id)
        return {"ok": False, "created": False, "already_exists": True,
                "entity_id": entity_id,
                "error": f"{entity_id} already exists",
                "message": (f"{entity_id} already exists in Home Assistant, "
                            f"and I only ever create new ones — I won't "
                            f"overwrite something that's already there.")}

    endpoint, reload_service, _domain = SUPPORTED_KINDS[kind]
    base = _ha_base_url()
    config_url = f"{base}/api/{endpoint.format(object_id=object_id)}"
    reload_url = f"{base}/api/services/{reload_service}"

    import requests

    # A THROWAWAY session. The everyday bridge's session and token are never
    # touched: it keeps its own Authorization header, and this one dies in the
    # finally below along with the only reference to the elevated credential.
    admin_token = resolution.token
    switched_off: list[str] = []
    session = requests.Session()
    try:
        session.headers.update(_admin_headers(admin_token))

        if not overwrite:
            try:
                probe = session.get(config_url, timeout=HTTP_TIMEOUT_SECONDS)
                if probe.status_code == 200:
                    body = probe.json() if probe.content else None
                    if isinstance(body, dict) and body:
                        logger.warning("ha provisioning: refused — %s already "
                                       "configured", entity_id)
                        return {"ok": False, "created": False,
                                "already_exists": True, "entity_id": entity_id,
                                "error": f"{entity_id} already exists",
                                "message": (
                                    f"{entity_id} already exists in Home "
                                    f"Assistant, and I only ever create new "
                                    f"ones.")}
            except Exception as exc:
                # A probe that cannot answer is not evidence of absence, but it
                # is also not evidence of presence; the POST below is the real
                # decision point and HA itself is authoritative.
                logger.debug("ha provisioning: existence probe inconclusive: "
                             "%s", type(exc).__name__)

        # Spent HERE, after every refusal above (scene/switch safety, create-
        # only, the HA-side existence probe): a refused change leaves the
        # owner's yes intact, and a second use races to this one point.
        with _LOCK:
            if isinstance(stamp, str):
                if approval_spent(stamp):
                    return {"ok": False, "created": False,
                            "error": "approval already used",
                            "message": "I've already made that change."}
                _spend(stamp)
        try:
            resp = session.post(config_url, json=payload,
                                timeout=HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.error("ha provisioning: write to %s failed: %s", entity_id,
                         type(exc).__name__)
            return {"ok": False, "created": False, "entity_id": entity_id,
                    "error": type(exc).__name__,
                    "message": ("I couldn't reach Home Assistant to make that "
                                "change.")}
        if resp.status_code >= 400:
            detail = _http_detail(resp)
            logger.error("ha provisioning: HA refused to create %s (HTTP %s)",
                         entity_id, resp.status_code)
            return {"ok": False, "created": False, "entity_id": entity_id,
                    "error": f"HTTP {resp.status_code}",
                    "message": _write_refusal_message(resp.status_code, detail)}

        try:
            session.post(reload_url, json={}, timeout=HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("ha provisioning: %s reload failed: %s",
                           reload_service, type(exc).__name__)

        # "Replace" never deletes: the approved record names the old
        # automations, and they are switched OFF, which the owner can undo.
        for old_id in replaces:
            try:
                off = session.post(f"{base}/api/services/automation/turn_off",
                                   json={"entity_id": old_id},
                                   timeout=HTTP_TIMEOUT_SECONDS)
                if getattr(off, "status_code", 500) < 400:
                    switched_off.append(old_id)
            except Exception as exc:
                logger.warning("ha provisioning: could not switch off %s: %s",
                               old_id, type(exc).__name__)
    finally:
        try:
            session.close()
        finally:
            # Drop every reference this frame holds to the credential. The
            # Resolution is frozen, so the local is the only one we own.
            admin_token = None
            del admin_token
            resolution = None
            del resolution

    verified = False
    if everyday is not None:
        for attempt in range(max(1, VERIFY_ATTEMPTS)):
            if _entity_exists(everyday, entity_id):
                verified = True
                break
            if attempt + 1 < max(1, VERIFY_ATTEMPTS):
                time.sleep(VERIFY_DELAY_SECONDS)

    logger.info("ha provisioning: created %s via vault admin credential",
                entity_id)
    _log_event(entity_id, kind, verified)

    alias = str(payload.get("alias") or entity_id)
    if verified:
        message = (f"Done — {entity_id} now exists in Home Assistant, and my "
                   f"everyday account can see it.")
        if kind == "automation":
            message = f"Done: '{alias}' is in Home Assistant and turned on."
    else:
        message = (f"I created {entity_id} in Home Assistant, but it hasn't "
                   f"shown up in my everyday account's view yet. Give Home "
                   f"Assistant a moment and ask me again.")
    if replaces:
        if len(switched_off) == len(replaces):
            message += f" I switched off {', '.join(replaces)}."
        else:
            missed = [r for r in replaces if r not in switched_off]
            message += (f" I couldn't switch off {', '.join(missed)}, so it is "
                        f"still on.")
    return {"ok": True, "created": True, "verified": verified,
            "entity_id": entity_id, "kind": kind, "message": message,
            **({"switched_off": switched_off} if replaces else {})}


def _http_detail(resp) -> str:
    """A short, bounded rendering of an HA error body. Never the request."""
    try:
        return " ".join(str(resp.text or "")[:200].split())
    except Exception:
        return ""


def _write_refusal_message(status: int, detail: str) -> str:
    if status in (401, 403):
        return ("Home Assistant rejected the administrator credential in the "
                "vault. It may have been revoked — create a new long-lived "
                "token from an administrator account and store it in the "
                "vault again.")
    if status == 404:
        return ("Home Assistant doesn't offer that configuration API — this "
                "version may not support creating that kind of object over "
                "the API.")
    return f"Home Assistant refused the change (HTTP {status}). {detail}".strip()


def _log_event(entity_id: str, kind: str, verified: bool) -> None:
    """File the ``ha_provisioned`` activity event. Payload-free, never raises.

    The task label is the entity id and the result carries only the kind and
    whether it verified: the script body is in the consent record the owner
    approved, and it has no business in the activity feed as well.
    """
    try:
        _creq.log_agent_activity(
            "steward", "ha_provisioned", entity_id,
            "completed" if verified else "unverified",
            {"kind": kind, "verified": bool(verified)})
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("ha provisioning: activity log failed: %s",
                     type(exc).__name__)


# ── switching an automation OFF (never deleting it) ─────────────────
#
# "Delete the automation that turns off the sofa lights" is answered with the
# one thing PuffPAI will do: switch it off, under the same consent path as a
# create (fresh human approval, HUMAN_CONFIRMED, vault-session credential).
# It stays in Home Assistant; the owner can switch it back on there.

AUTOMATION_OFF_KIND = "automation_off"


def propose_automation_off(entity_id: str, name: str,
                           conversation_id: str | None = None) -> dict:
    """File ONE consent request to switch an existing automation off."""
    if not enabled():
        return {"ok": False, "proposed": False,
                "message": "Making changes in Home Assistant is switched off."}
    if not isinstance(entity_id, str) or not _AUTOMATION_ID_RE.fullmatch(entity_id):
        raise ProvisioningError(f"{entity_id!r} is not an automation")
    label = " ".join(str(name or entity_id).split())[:80]
    req = _creq.create_request(
        entity_id=entity_id,
        label=f"Switch off automation: {label}",
        action=PROVISION_ACTION,
        reason=(f"Switch OFF the Home Assistant automation '{label}' "
                f"({entity_id}). It is not deleted: it stays in Home Assistant "
                f"and can be switched back on there. {VAULT_SENTENCE}"),
        requested_by="steward",
        context={"kind": AUTOMATION_OFF_KIND, "target": entity_id,
                 "name": label,
                 "conversation_id": (str(conversation_id)[:80]
                                     if conversation_id else None)},
        ttl_seconds=REQUEST_TTL_SECONDS,
    )
    return {"ok": True, "proposed": True, "request": req,
            "entity_id": entity_id}


def void_approval(record: dict | None) -> None:
    """Spend an approval WITHOUT using it (a cancelled PIN prompt)."""
    stamp = (record or {}).get("resolved_at")
    if isinstance(stamp, str):
        with _LOCK:
            _spend(stamp)


def _apply_automation_off(approval: dict, context: dict, bridge,
                          vault_session_id) -> dict:
    target = context.get("target")
    name = context.get("name") or target
    if not isinstance(target, str) or not _AUTOMATION_ID_RE.fullmatch(target) \
            or approval.get("entity_id") != target:
        return {"ok": False, "created": False, "error": "bad target",
                "message": "I can't apply that change: the target is not an "
                           "automation."}
    from puffpai.security.ha_token_source import resolve_ha_admin_token
    resolution = resolve_ha_admin_token(vault_session_id)
    if not resolution.token:
        return {"ok": False, "created": False, "vault_locked": True,
                "entity_id": target, "error": "elevated credential unavailable",
                "message": "The vault is locked, so I can't reach the Home "
                           "Assistant administrator credential."}
    stamp = approval.get("resolved_at")
    with _LOCK:
        if isinstance(stamp, str):
            if approval_spent(stamp):
                return {"ok": False, "created": False,
                        "error": "approval already used",
                        "message": "I've already made that change."}
            _spend(stamp)
    import requests
    admin_token = resolution.token
    session = requests.Session()
    try:
        session.headers.update(_admin_headers(admin_token))
        try:
            resp = session.post(f"{_ha_base_url()}/api/services/automation/turn_off",
                                json={"entity_id": target},
                                timeout=HTTP_TIMEOUT_SECONDS)
            status = getattr(resp, "status_code", 500)
        except Exception as exc:
            logger.warning("ha provisioning: switch-off failed: %s",
                           type(exc).__name__)
            status = 599
    finally:
        try:
            session.close()
        finally:
            admin_token = None
            del admin_token
            resolution = None
            del resolution
    if status >= 400:
        return {"ok": False, "created": False, "entity_id": target,
                "error": f"HTTP {status}",
                "message": f"Home Assistant didn't switch '{name}' off "
                           f"(HTTP {status})."}
    _log_event(target, AUTOMATION_OFF_KIND, True)
    return {"ok": True, "created": False, "switched_off": [target],
            "entity_id": target,
            "message": f"Done. '{name}' is switched off. It is still in Home "
                       f"Assistant if you want it back."}


# ── admin-direct Zigbee permit ──────────────────────────────────────
#
# When the permit SCRIPT runs, PuffPAI cannot tell whether Home Assistant
# opened the radio, because every way of looking (the
# script's steps, its trace) is admin-only and the everyday account is not an
# admin. With the vault open, the owner's approved pairing can instead borrow
# the admin credential for exactly ONE fixed call, ``zha.permit``, and HA's own
# answer becomes the proof. This is not a general service call: the service,
# the URL and the body shape are constants here, and the only variable is a
# bounded integer.

ADMIN_PERMIT_MAX_SECONDS = 254
ADMIN_PERMIT_URL = "/api/services/zha/permit"


def admin_permit_join(duration, *, human_confirmed=False,
                      vault_session_id: str | None = None) -> dict:
    """Open (or, with 0, close) the Zigbee pairing window as the HA admin.

    Gates, all required:

    * *human_confirmed* is the in-process HUMAN_CONFIRMED object (callers:
      :func:`puffpai.zigbee_pairing.complete_approved_pairing`, after it has
      re-read a fresh human approval, and its own watcher for the close);
    * the elevated credential resolves, which only happens while the owner's
      vault is open (vault-session tier).

    Returns ``{"ok", "attempted", "vault_locked", "duration", "error"}``.
    ``attempted`` is False when nothing was sent (locked, refused, bad
    duration), so the caller can fall back to the permit script. Never raises.
    Never logs or returns the token.
    """
    from puffpai.ha_bridge import is_human_confirmation

    if not is_human_confirmation(human_confirmed,
                                 where="ha_provisioning.admin_permit_join"):
        return {"ok": False, "attempted": False, "vault_locked": False,
                "duration": None, "error": "human confirmation token required"}
    if not enabled():
        return {"ok": False, "attempted": False, "vault_locked": False,
                "duration": None, "error": "ha provisioning is switched off"}
    if isinstance(duration, bool) or not isinstance(duration, int) \
            or not (0 <= duration <= ADMIN_PERMIT_MAX_SECONDS):
        return {"ok": False, "attempted": False, "vault_locked": False,
                "duration": None, "error": "duration must be 0-254 seconds"}

    try:
        from puffpai.security.ha_token_source import resolve_ha_admin_token
        resolution = resolve_ha_admin_token(vault_session_id)
    except Exception as exc:
        logger.debug("admin permit: credential lookup failed: %s",
                     type(exc).__name__)
        resolution = None
    if resolution is None or not resolution.token:
        return {"ok": False, "attempted": False, "vault_locked": True,
                "duration": duration, "error": "elevated credential unavailable"}

    import requests

    admin_token = resolution.token
    session = requests.Session()
    try:
        session.headers.update(_admin_headers(admin_token))
        try:
            resp = session.post(f"{_ha_base_url()}{ADMIN_PERMIT_URL}",
                                json={"duration": int(duration)},
                                timeout=HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("admin permit: HA unreachable: %s",
                           type(exc).__name__)
            return {"ok": False, "attempted": True, "vault_locked": False,
                    "duration": duration, "error": type(exc).__name__}
        status = getattr(resp, "status_code", 500)
    finally:
        try:
            session.close()
        finally:
            admin_token = None
            del admin_token
            resolution = None
            del resolution

    if status >= 400:
        logger.warning("admin permit: HA refused zha.permit (HTTP %s)", status)
        return {"ok": False, "attempted": True, "vault_locked": False,
                "duration": duration, "error": f"HTTP {status}"}
    logger.info("admin permit: HA accepted zha.permit duration=%s via the "
                "vault admin credential", duration)
    try:
        _creq.log_agent_activity("steward", "ha_admin_service", "zha.permit",
                                 "completed", {"duration": int(duration)})
    except Exception:                               # pragma: no cover
        pass
    return {"ok": True, "attempted": True, "vault_locked": False,
            "duration": duration, "error": None}


# ── the first customer: the two Zigbee permit scripts ───────────────
#
# ``zha.permit`` is admin-only, but ``script.turn_on`` is not, and a script runs
# with Home Assistant's own authority rather than the caller's. So two scripts
# that do exactly one thing each — open the window for 120 s, and close it —
# turn an admin-only capability into a pair of named, auditable, single-purpose
# buttons the restricted everyday user may press. That is a strictly smaller
# grant than making the everyday user an administrator, which would hand it
# every service in the house plus configuration rights.

ZIGBEE_PERMIT_OBJECT_ID = "puffpai_zigbee_permit"
ZIGBEE_PERMIT_CLOSE_OBJECT_ID = "puffpai_zigbee_permit_close"

#: Matches :data:`puffpai.zigbee_pairing.DEFAULT_PAIRING_SECONDS`. Restated as a
#: literal because it is baked into a script body that lives in Home Assistant
#: after this module has forgotten about it.
ZIGBEE_PERMIT_SECONDS = 120


def zigbee_permit_payload(duration: int = ZIGBEE_PERMIT_SECONDS) -> dict:
    return {
        "alias": "PuffPAI: open Zigbee pairing window",
        "mode": "single",
        "sequence": [
            {"service": "zha.permit", "data": {"duration": int(duration)}},
        ],
    }


def zigbee_permit_close_payload() -> dict:
    return {
        "alias": "PuffPAI: close Zigbee pairing window",
        "mode": "single",
        "sequence": [
            {"service": "zha.permit", "data": {"duration": 0}},
        ],
    }


def propose_zigbee_permit_scripts(duration: int = ZIGBEE_PERMIT_SECONDS
                                  ) -> dict:
    """Propose the two scoped permit scripts. Idempotent, actuates nothing.

    Called from :func:`puffpai.zigbee_pairing.complete_approved_pairing` when
    Home Assistant refuses ``zha.permit`` for the restricted user. Filing twice
    refreshes the two open questions rather than stacking banners, because
    ``create_request`` is idempotent per ``(entity_id, action)``.
    """
    why = ("Home Assistant only lets administrators open a Zigbee pairing "
           "window, and my everyday account isn't one (on purpose). A script "
           "runs with Home Assistant's own authority, so this one line lets me "
           "open the window without me holding administrator rights all day.")
    close_why = ("The matching close script, so I can shut the pairing window "
                 "the moment it isn't needed instead of waiting it out.")

    results = []
    batch = "permit-scripts"            # offered together: "approve both" works
    for object_id, payload, reason in (
        (ZIGBEE_PERMIT_OBJECT_ID, zigbee_permit_payload(duration), why),
        (ZIGBEE_PERMIT_CLOSE_OBJECT_ID, zigbee_permit_close_payload(),
         close_why),
    ):
        try:
            results.append(propose_ha_change("script", object_id, payload,
                                             reason, requested_by="steward",
                                             batch=batch))
        except ProvisioningError as exc:           # pragma: no cover - defensive
            logger.warning("ha provisioning: could not propose %s: %s",
                           object_id, exc)
            results.append({"ok": False, "proposed": False, "error": str(exc)})

    proposed = [r for r in results if r.get("proposed")]
    return {"ok": len(proposed) == 2, "proposed": len(proposed),
            "requests": [r.get("request") for r in proposed],
            "entity_ids": [r.get("entity_id") for r in proposed]}


# ── verifying the elevated credential ─────────────
#
# Storing the credential proves nothing: the owner can paste a token minted
# from the WRONG Home Assistant, from a non-admin account, or one they have
# since revoked, and the only evidence is a 401 buried in an approval that has
# already been spent. These two helpers answer "is the thing in the vault worth
# anything?" *before* a change is approved, and they answer it in plain words.
#
# Both run on a THROWAWAY session, exactly like the writer above: the everyday
# bridge's session and credential are never touched.

#: Object id used to probe admin rights. Nothing is created: a GET against a
#: script config that does not exist answers 404 for an administrator and 401
#: for everyone else, so the *status code* is the whole answer.
ADMIN_PROBE_OBJECT_ID = "__puffpai_probe__"


def verify_admin_credential(token: str) -> dict:
    """Ask Home Assistant what *token* is actually worth. Never raises.

    Returns ``{"reachable", "valid", "admin", "status"}``:

    * ``reachable`` -- Home Assistant answered at all;
    * ``valid`` -- ``GET /api/config`` returned 200, so the token authenticates;
    * ``admin`` -- the admin-only script-config endpoint answered 404 (absent
      object, but the endpoint let us in) rather than 401/403. ``None`` when it
      could not be established;
    * ``status`` -- the HTTP code from ``GET /api/config``.

    Nothing here logs the token, and nothing is written in Home Assistant.
    """
    import requests

    base = _ha_base_url()
    out: dict = {"reachable": False, "valid": False, "admin": None,
                 "status": None}
    session = requests.Session()
    try:
        session.headers.update(_admin_headers(token))
        try:
            resp = session.get(f"{base}/api/config",
                               timeout=HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("ha credential verify: Home Assistant did not "
                           "answer (%s)", type(exc).__name__)
            return out
        out["reachable"] = True
        out["status"] = resp.status_code
        if resp.status_code != 200:
            logger.warning("ha credential verify: stored credential rejected "
                           "by Home Assistant (HTTP %s)", resp.status_code)
            return out
        out["valid"] = True

        probe_url = (f"{base}/api/config/script/config/"
                     f"{ADMIN_PROBE_OBJECT_ID}")
        try:
            probe = session.get(probe_url, timeout=HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.debug("ha credential verify: admin probe inconclusive: %s",
                         type(exc).__name__)
            return out
        code = probe.status_code
        if code == 404 or code < 400:
            out["admin"] = True
        elif code in (401, 403):
            out["admin"] = False
        else:
            logger.debug("ha credential verify: admin probe returned HTTP %s",
                         code)
        return out
    finally:
        try:
            session.close()
        finally:
            token = None


def verify_message(ha: dict) -> str:
    """One sentence a person can act on, from :func:`verify_admin_credential`."""
    if not ha.get("reachable"):
        return ("Home Assistant did not answer, so the stored credential "
                "could not be checked.")
    if not ha.get("valid"):
        status = ha.get("status")
        return (f"Stored token is rejected by Home Assistant ({status}) — it "
                f"is not valid for this Home Assistant. Make a new long-lived "
                f"token from an administrator account and store it again.")
    admin = ha.get("admin")
    if admin is True:
        return "Stored token is valid and the account is an administrator."
    if admin is False:
        return ("Stored token is valid, but the account is not an "
                "administrator — it cannot create scripts, so approved Home "
                "Assistant changes will still fail.")
    return ("Stored token is valid, but I could not establish whether the "
            "account is an administrator.")
