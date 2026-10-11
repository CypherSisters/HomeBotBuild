"""
PuffPAI Home Assistant Bridge
==============================
Single source of truth for all Home Assistant communication.

puff puff pass

Every component (API server, assistant tools, MCP server, dashboard) should
go through this module instead of making direct HA REST calls.

Usage:
    from puffpai.ha_bridge import get_bridge

    bridge = get_bridge()
    bridge.list_devices(domain="light")
    bridge.control_device("light.bedroom", "turn_on", brightness_pct=80)
    bridge.get_automations()
    bridge.create_automation({...})
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from puffpai.config import get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class HABridgeError(Exception):
    """Raised when an HA operation fails."""
    pass


# ---------------------------------------------------------------------------
# Input validation for anything that becomes part of a URL
# ---------------------------------------------------------------------------
# Every value below is interpolated into an *authenticated* request path. HA
# entity ids are a narrow, well-defined shape (domain.object_id, lowercase
# ASCII), so we allow-list rather than escape-and-hope: a caller-supplied
# "../../auth/providers" would otherwise be resolved by requests into a
# token-authenticated GET against an arbitrary HA endpoint.

# ``\Z`` rather than ``$``: ``$`` also matches before a trailing newline, which
# would let "sensor.foo\n" through.
ENTITY_ID_RE = re.compile(r"\A[a-z_][a-z0-9_]*\.[a-z0-9_]+\Z")
# domain / service / other single path segments (no dot, no slash)
SEGMENT_RE = re.compile(r"\A[a-z_][a-z0-9_]*\Z")
# ISO-ish timestamps used by the history endpoint
TIMESTAMP_RE = re.compile(r"\A[0-9A-Za-z:.+-]{1,40}\Z")

_MAX_ECHOED_INPUT = 16


def _echo(value: Any) -> str:
    """Short, sanitised rendering of rejected input for the error message.

    Never returns more than :data:`_MAX_ECHOED_INPUT` characters and strips
    anything non-printable, so a rejected value cannot smuggle control
    characters into logs.
    """
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    cleaned = "".join(ch for ch in value[:_MAX_ECHOED_INPUT] if ch.isprintable())
    suffix = "..." if len(value) > _MAX_ECHOED_INPUT else ""
    return f"{cleaned!r}{suffix}"


def is_valid_entity_id(value: Any) -> bool:
    """True when *value* is a canonical HA entity id (``domain.object_id``)."""
    return isinstance(value, str) and ENTITY_ID_RE.match(value) is not None


def validate_entity_id(value: Any, *, kind: str = "entity_id") -> str:
    """Return *value* unchanged if it is a canonical entity id, else raise."""
    if not is_valid_entity_id(value):
        raise HABridgeError(f"invalid {kind}: {_echo(value)}")
    return value


def validate_segment(value: Any, *, kind: str = "segment") -> str:
    """Return *value* unchanged if it is a safe single URL path segment."""
    if not isinstance(value, str) or SEGMENT_RE.match(value) is None:
        raise HABridgeError(f"invalid {kind}: {_echo(value)}")
    return value


def validate_timestamp(value: Any, *, kind: str = "timestamp") -> str:
    """Return *value* unchanged if it looks like a bare ISO timestamp."""
    if (not isinstance(value, str) or TIMESTAMP_RE.match(value) is None
            or ".." in value):
        raise HABridgeError(f"invalid {kind}: {_echo(value)}")
    return value


def quote_segment(value: str) -> str:
    """Percent-encode one path segment (belt and braces behind the validators).

    Valid entity ids and domains pass through untouched (``.`` and ``_`` are
    never encoded); anything a future validator gap lets through cannot escape
    its segment.
    """
    return quote(value, safe="")


def _assert_no_traversal(endpoint: str) -> None:
    """Last line of defence: reject dot segments in an assembled endpoint."""
    path = endpoint.split("?", 1)[0]
    if any(seg in ("..", ".") for seg in path.split("/")):
        raise HABridgeError(f"invalid HA endpoint: {_echo(endpoint)}")


# ---------------------------------------------------------------------------
# The always-confirm class — single source of truth
# ---------------------------------------------------------------------------
# Membership is a property of the TARGET, not of the intent, tool or HTTP route
# that reaches it, and it is enforced *here* rather than in each caller. That
# placement is the whole point: this module is the last thing between the
# appliance and the wire, so a caller that forgets the gate — a new LLM tool, a
# new route, a future agent — still cannot actuate.
#
# Previously the gate lived one layer up, in the Steward's ``_GuardedHA``. Three
# live callers never went through the Steward at all: the legacy PuffPAIAgent's
# ``ha_control_device`` / ``ha_run_scene`` LLM tools, and the ``/ha/control``,
# ``/ha/service`` and ``/ha/scene`` HTTP routes. Moving the policy down here
# closes all of them at once and makes a fourth one impossible to forget.

#: The always-confirm authority. In each of these domains the wrong actuation is
#: a physical-safety or physical-security event rather than an inconvenience:
#:
#: * ``camera`` — ``camera.turn_off`` disables the household's own witness, and
#:   nothing else in the system notices that it went quiet. Camera *reads* go
#:   through ``camera_proxy`` (:meth:`HABridge.get_camera_image`), not a service
#:   call, so gating this domain costs no read capability at all.
#: * ``cover`` — garage doors, gates and shutters share this domain with
#:   motorised blinds, and nothing in an entity id distinguishes them, so the
#:   whole domain is gated rather than guessing which covers are a way in.
#: * ``lock`` — unlocking a door is the canonical never-autonomous action.
#: * ``alarm_control_panel`` — disarming the house alarm.
#: * ``valve`` — water and gas shutoffs; opening one unattended is property
#:   damage at best.
#: * ``water_heater`` — scald temperature and legionella risk sit behind this.
#: * ``siren`` — the original always-confirm class.
#:
#: Deliberately NOT here: ``climate`` (setpoints are clamped upstream),
#: ``light``, ``switch``, ``media_player``. A ``switch`` entity wired to a gate
#: motor is a residual this cannot see — an entity id does not say what is on
#: the other end of the relay.
SAFETY_CRITICAL_DOMAINS = frozenset({
    "siren", "lock", "alarm_control_panel", "cover", "valve", "water_heater",
    "camera",
})

#: The keyword callers use to present a human confirmation. Named once so the
#: bridge, the Steward's proxy and the tests cannot drift on the spelling.
CONFIRMATION_KEYWORD = "human_confirmed"


class ConfirmationRequired(HABridgeError):
    """An always-confirm actuation was reached without a human confirmation.

    Raised *before* the request leaves the appliance. It is an exception rather
    than a return value on purpose: a caller that forgets to check still cannot
    actuate, because the refusal happens underneath it.

    Subclasses :class:`HABridgeError` deliberately. Every existing caller
    already has an ``except HABridgeError`` arm, so a path that has not been
    taught about confirmation degrades into a clean refusal rather than an
    unhandled 500 — and either way the actuation does not happen.
    """

    def __init__(self, proposal: dict, reason: str) -> None:
        super().__init__(reason)
        self.proposal = proposal
        self.reason = reason


class _HumanConfirmation:
    """The unforgeable proof-of-human token. One instance: :data:`HUMAN_CONFIRMED`.

    Why an object identity and not ``human_confirmed=True``:

    The callers that must never be able to confirm are the ones whose arguments
    come from outside the appliance — an LLM emitting a tool call, and an HTTP
    client posting JSON. Both can supply *values*: a string, a number, ``true``.
    Neither can supply a *reference to a specific Python object that exists only
    inside this process*. ``json.loads`` has no syntax for it and a tool-call
    argument is decoded JSON.

    That distinction is load-bearing here, not theoretical:

    * ``/ha/control`` forwards every unrecognised body key into
      ``control_device(**extra)``, so ``{"entity_id": "camera.hall",
      "action": "turn_off", "human_confirmed": true}`` really does arrive at
      this parameter. With a bool contract that request would have actuated.
      It binds ``True``, which is not :data:`HUMAN_CONFIRMED`, so it is refused
      and logged as an attempted spoof.
    * ``/ha/service/<domain>/<service>`` forwards the whole body the same way.

    Only in-process Python that has imported this module can pass the token, and
    code that can import it can already call ``requests`` directly — so the
    token draws the line exactly where it is meaningful: at the boundary between
    the appliance's own code and text that arrived from outside it.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # never render as something JSON-ish
        return "<HUMAN_CONFIRMED>"


#: Pass ``human_confirmed=HUMAN_CONFIRMED`` to actuate something in
#: :data:`SAFETY_CRITICAL_DOMAINS`. Only pass it when you are holding real
#: evidence of a human decision (a confirmed envelope from a human origin, or a
#: server-side confirmation stamped from an authenticated interactive session).
#: A ``confirmed: true`` in a request body is not evidence of a human.
HUMAN_CONFIRMED = _HumanConfirmation()


def is_human_confirmation(value: Any, *, where: str = "") -> bool:
    """True only for :data:`HUMAN_CONFIRMED` itself.

    Anything else is *not* a confirmation. A truthy non-token is logged at
    ``error``: nothing in the appliance passes a bare ``True``, so seeing one
    means a caller forwarded external input into the confirmation parameter,
    which is the exact spoof this design exists to defeat. Only the type name is
    logged — the value came from outside and does not belong in the log.
    """
    if value is HUMAN_CONFIRMED:
        return True
    if value is False or value is None:
        return False
    logger.error(
        "ha_bridge: refusing a non-token confirmation (%s) on %s — only the "
        "in-process HUMAN_CONFIRMED token counts as human confirmation. This "
        "usually means caller-supplied input reached the %s parameter.",
        type(value).__name__, where or "an actuating call", CONFIRMATION_KEYWORD)
    return False


def entity_domain(entity_id: Any) -> str:
    """The HA domain of *entity_id*, or ``""`` when it has none.

    Deliberately does not validate — a malformed id yields ``""`` and is caught
    by :func:`validate_entity_id` further down. This is only used to classify.
    """
    text = str(entity_id or "")
    if "." not in text:
        return ""
    return text.split(".", 1)[0].strip().lower()


def as_targets(entity_id: Any) -> list[Any]:
    """*entity_id* as a list, whether one target was given or several."""
    if isinstance(entity_id, (list, tuple, set)):
        return list(entity_id)
    return [entity_id]


def service_call_targets(entity_id: Any, service_data: Any = None, *,
                         service_domain: Any = None,
                         service: Any = None) -> list[Any]:
    """Every entity a service call names, whichever way it names it.

    A caller can put the target in three places and HA honours all of them: the
    ``entity_id`` argument, an ``entity_id`` key in the service data, and the
    modern ``target: {entity_id: …}`` block. Only the first was being classified,
    so ``/ha/service/homeassistant/turn_off`` with
    ``{"target": {"entity_id": "camera.hall"}}`` presented no camera to the gate
    at all — the service domain is ``homeassistant`` and the argument is empty.

    There is a fourth place, and it is a whole scene body inline in the request:
    ``scene.apply`` takes ``entities: {"camera.hall": "off"}`` and actuates every
    entity that mapping names, while naming none of them as a target. Its keys
    are entity ids, so they are folded in and classified like any other target —
    without which ``scene.apply`` carried the domains ``{"scene"}``, which is
    never safety-critical, and was a scene activation that skipped
    :func:`scene_member_domains` entirely by never storing a scene.

    ``scene.create`` takes the same ``entities:`` key and is deliberately *not*
    folded in: it writes a scene definition, it does not actuate. Applying that
    scene later comes back through here as ``scene.turn_on``, where the stored
    member list is expanded.

    Otherwise deliberately *not* a general walk of the service data: a ``notify``
    message that happens to contain the words "camera.hall" is not a camera
    actuation, and a gate that refuses notifications is a gate people route
    around.
    """
    targets: list[Any] = [] if entity_id is None else list(as_targets(entity_id))
    data = service_data if isinstance(service_data, dict) else {}
    if data.get("entity_id") is not None:
        targets.extend(as_targets(data["entity_id"]))
    target = data.get("target")
    if isinstance(target, dict) and target.get("entity_id") is not None:
        targets.extend(as_targets(target["entity_id"]))
    if (str(service_domain or "").strip().lower() == "scene"
            and str(service or "").strip().lower() == "apply"):
        entities = data.get("entities")
        if isinstance(entities, dict):
            targets.extend(entities.keys())
    return [t for t in targets if t]


def touched_domains(service_domain: Any, entity_id: Any) -> set[str]:
    """Every HA domain a call would reach: the service's and its targets'.

    Both matter. ``call_service("camera", "turn_off", entity_id=None)`` names no
    entity (HA applies it to every camera), and ``call_service("homeassistant",
    "turn_off", entity_id="camera.hall")`` names a benign service domain.
    """
    found: set[str] = set()
    if service_domain:
        found.add(str(service_domain).strip().lower())
    targets = (entity_id if isinstance(entity_id, (list, tuple, set))
               else [entity_id])
    for target in targets:
        domain = entity_domain(target)
        if domain:
            found.add(domain)
    return found


def scene_member_domains(reader: Any, entity_id: Any) -> tuple[set[str], bool]:
    """Domains a scene would actuate, and whether we could really tell.

    A scene is a wrapper: ``scene.movie_night`` is not itself safety-critical,
    but it can contain ``camera.hall``. HA publishes a scene's members as the
    ``entity_id`` attribute of its state, so the check is one cheap read.

    Returns ``(domains, members_known)``. When the read fails or the attribute is
    missing the members are *unknown*, and unknown fails closed — the caller
    turns ``members_known=False`` into a refusal, because "I could not find out
    what this would do" must not be treated as "it does nothing dangerous".

    *reader* is anything with ``get_entity_state`` — the bridge itself, or the
    Steward's envelope-bound proxy over it.
    """
    domains = touched_domains(None, entity_id)
    if entity_domain(entity_id) != "scene":
        # Not a scene at all (a script, or an actuator addressed directly).
        # Nothing to expand; classify on the entity itself.
        return domains, True
    try:
        raw = reader.get_entity_state(entity_id) or {}
        members = (raw.get("attributes") or {}).get("entity_id")
    except Exception as exc:
        logger.warning("scene %s: membership unreadable (%s); treating as "
                       "always-confirm", entity_id, exc)
        return domains, False
    if isinstance(members, str):
        members = [members]
    if not isinstance(members, (list, tuple)):
        logger.warning("scene %s: no member list in HA state; treating as "
                       "always-confirm", entity_id)
        return domains, False
    for member in members:
        domain = entity_domain(member)
        if domain:
            domains.add(domain)
    return domains, True


#: How long a *fetched automation or script config* may be reused (seconds).
#: This caches content read from HA, never a verdict: the policy is re-applied
#: from the cached config on every single call, so a decision can never be
#: replayed and a confirmation can never be "still valid" from an earlier
#: request.
STORED_CONFIG_TTL_SECONDS = 15.0

#: Historical name, kept so existing callers and tests keep working.
AUTOMATION_CONFIG_TTL_SECONDS = STORED_CONFIG_TTL_SECONDS

#: Keys inside an automation action step whose *value* decides what gets
#: actuated. HA renamed the service key from ``service:`` to ``action:`` in
#: 2024.8 and both spellings still load, so both are here; ``service_template``
#: is the pre-2021 spelling and is still found in the wild.
_ACTION_DISPATCH_KEYS = frozenset({
    "service", "action", "service_template", "entity_id", "target",
    "device_id", "area_id", "floor_id", "label_id", "entity_id_template",
})

#: Targeting selectors that name something *other* than an entity. An action
#: with ``target: {area_id: garage}`` reaches whatever happens to be in that
#: area, which this appliance cannot enumerate without the device and area
#: registries — so seeing one means the action set is not knowable from the
#: config alone.
_OPAQUE_TARGET_KEYS = frozenset({
    "device_id", "area_id", "floor_id", "label_id",
})

#: Service domains that do **not** cap what a call can reach.
#:
#: For every real entity domain the service domain is itself a bound:
#: ``light.turn_on`` reaches lights and only lights, however it is targeted, so
#: an area-targeted one is knowable enough — the worst case is "every light in
#: the garage". These four are domain-agnostic by design and reach anything at
#: all: ``homeassistant.turn_on/turn_off/toggle`` dispatches to whatever domain
#: the resolved target happens to be in, and ``script`` / ``scene`` /
#: ``automation`` run stored instructions whose contents are the actuation.
_UNBOUNDED_SERVICE_DOMAINS = frozenset({
    "homeassistant", "script", "scene", "automation",
})


def _service_domain_bounds_targets(service_domain: Any) -> bool:
    """Whether the *service* domain alone caps what a call can actuate.

    True for a real entity domain that is not one of the domain-agnostic four.
    Anything this appliance does not recognise as an entity domain — a Z-Wave or
    ZHA integration service, say, which takes ``device_id``/``area_id`` and
    reconfigures whatever hardware is there, locks included — is treated as
    **not** bounding. Over-matching costs a confirmation; under-matching costs
    the gate.
    """
    name = str(service_domain or "").strip().lower()
    if not name or name in _UNBOUNDED_SERVICE_DOMAINS:
        return False
    # Unioned here rather than at import so the rule sits next to the keys it
    # serves; the domain sets themselves are defined with the data types below.
    return name in (CONTROLLABLE_DOMAINS | SENSOR_DOMAINS
                    | SAFETY_CRITICAL_DOMAINS)


def request_opaque_selectors(service_data: Any) -> list[str]:
    """The opaque target selectors a *live* service call carries.

    :data:`_OPAQUE_TARGET_KEYS` applied to the request instead of to a stored
    config. HA accepts these both at the top level of the service data and
    inside the modern ``target:`` block, so both are read. A selector present
    but empty (``{"area_id": None}``) names nothing and is ignored.
    """
    data = service_data if isinstance(service_data, dict) else {}
    found: list[str] = []
    for holder in (data, data.get("target")):
        if not isinstance(holder, dict):
            continue
        for raw_key, value in holder.items():
            name = str(raw_key).strip().lower()
            if name in _OPAQUE_TARGET_KEYS and value and name not in found:
                found.append(name)
    return sorted(found)


def unbounded_request_detail(service_domain: Any, service: Any,
                             service_data: Any) -> str:
    """Why this call's target set is not knowable, or ``""`` when it is.

    The live-request half of a rule the stored-config walk already applies:
    :func:`automation_action_domains` treats ``target: {area_id: garage}`` inside
    a saved automation as not-knowable, because this appliance cannot enumerate
    an area without the device and area registries. The identical selector in
    the *request* was not checked at all, so the only cap left on what got
    actuated was the service domain — and for the domain-agnostic services
    (:data:`_UNBOUNDED_SERVICE_DOMAINS`) there is no cap. ``script.turn_on`` with
    ``target: {area_id: garage}`` ran every script in the garage; the same shape
    on ``homeassistant.turn_on`` actuated a camera or a cover there with nothing
    checked.

    Deliberately **not** every area-targeted call. ``light.turn_on`` by area is
    ordinary dashboard traffic and prompting for it would teach people to click
    through confirmations, which is how a gate stops meaning anything. Only the
    combination — an opaque selector *and* a service domain that bounds nothing
    — is refused.
    """
    selectors = request_opaque_selectors(service_data)
    if not selectors or _service_domain_bounds_targets(service_domain):
        return ""
    name = f"{str(service_domain or '?').strip()}.{str(service or '?').strip()}"
    return (f"this call targets by {', '.join(selectors)} rather than by named "
            f"entities, and {name} is not bounded to one entity domain, so "
            f"what it would actuate is not knowable here — name the entities "
            f"to actuate, or confirm it")


# ---------------------------------------------------------------------------
# A call that names no target at all
# ---------------------------------------------------------------------------
# The selector rule above answers "this call names a set I cannot enumerate".
# It does not answer the shape underneath it: a call that names *nothing*.
# ``light.turn_off`` with empty service data is not a narrow call — HA reads a
# missing target as "the whole domain" and turns off every light in the house.
# The domains handed to the gate are then just ``{"light"}``, which is not
# safety-critical, so nothing prompted. The same shape reaches
# ``homeassistant.turn_off`` (every entity there is), ``climate.set_temperature``
# (every thermostat), ``vacuum.start``, ``button.press``.
#
# So the rule is inverted from the selector one: untargeted is refused by
# default and the exemptions are a short allow-list, each one written down with
# the reason it actuates nothing. Over-matching costs a confirmation;
# under-matching costs the gate.

#: Service *names* that are exempt in whatever domain they appear, because they
#: re-read configuration rather than actuate anything:
#:
#: * ``reload`` — ``automation.reload``, ``scene.reload``, ``script.reload``,
#:   ``template.reload`` and friends. Naming no entity is the *only* way to
#:   spell them; they re-read YAML. Honest residual: a reload re-arms the
#:   automations it loads, and an automation that then fires inside HA does not
#:   come back through this bridge. That is the pre-existing shape of automation
#:   in this house, not something this exemption creates, and refusing every
#:   config reload would make editing the house's own YAML a confirmation loop.
#: * ``reload_all`` — the ``homeassistant`` domain's reload-everything.
#: * ``reload_config_entry`` — restarts one integration's config entry.
#: * ``reload_core_config`` — re-reads ``configuration.yaml``'s core block.
#: * ``check_config`` — a pure validation pass; it writes nothing at all.
#:
#: Matched as exact names against this set, never as a substring: a service
#: called ``reload_and_open_the_garage`` must not inherit the exemption.
_NON_ACTUATING_SERVICES = frozenset({
    "reload", "reload_all", "reload_config_entry", "reload_core_config",
    "check_config",
})

#: Whole service *domains* that only carry a message and never move anything in
#: the house. Kept deliberately short — every entry here is a hole by design,
#: and a *domain* entry is a hole of an unusual shape: it is name-agnostic, so
#: it exempts every service HA has in the domain today **and every one it adds
#: tomorrow**. That is the same widening this file refuses for service names
#: (``reload_and_open_the_garage`` must not inherit ``reload``'s pass), so a
#: domain belongs here only when its service names genuinely cannot be
#: enumerated in advance. A bounded domain goes in
#: :data:`_NON_ACTUATING_DOMAIN_SERVICES` below instead.
#:
#: * ``notify`` — pushes text to a phone or the HA UI. This one *has* to be
#:   domain-wide: the service name **is the destination**
#:   (``notify.mobile_app_pixel``, ``notify.persistent_notification``, whatever
#:   the notifiers in this particular install happen to be called), so the set
#:   of names is per-install, changes when a phone is added, and is unknowable
#:   from here. A name allow-list would refuse the notifier the household
#:   actually has, and the file's standing doctrine applies: a gate that refuses
#:   notifications is a gate people route around. This appliance's own
#:   ``notify.*`` calls are untargeted by construction for the same reason.
#: * ``persistent_notification`` — ``create``/``dismiss``/``dismiss_all`` write
#:   and clear cards in HA's sidebar. The blast radius is a list of messages.
#:   Unlike ``notify`` this domain *is* bounded — HA defines exactly those three
#:   — so it is a candidate for the name-scoped treatment ``shopping_list`` gets
#:   below. It is left domain-wide in this pass deliberately and not by
#:   oversight: the asymmetry is known, and tightening it is the next move here,
#:   not an accident to be discovered later.
#:
#: Considered and deliberately **left out**, because nothing in this appliance
#: makes untargeted calls into them and an unused exemption is a free hole:
#: ``tts`` (its speak services take a media player, so a targeted call is the
#: normal one), ``logbook``, ``system_log``. ``todo`` is left out from the other
#: side of the same rule: it is the *other* branch of ``_list_add``, every
#: ``todo.*`` service takes the list's own ``todo.*`` entity, and so an
#: untargeted one is a caller's mistake rather than the only spelling there is.
#: Over-matching costs a confirmation.
_NON_ACTUATING_SERVICE_DOMAINS = frozenset({
    "notify", "persistent_notification",
})

#: Domains whose exemption is worth an *enumerated set of service names* rather
#: than the whole domain: ``{service_domain: frozenset(service names)}``. Same
#: allowance as :data:`_NON_ACTUATING_SERVICE_DOMAINS`, bounded to the services
#: actually reasoned about, so the domain cannot grow a pass it was never
#: granted.
#:
#: * ``shopping_list`` — the eight services listed below are HA's whole
#:   ``shopping_list`` surface, and every one of them operates on the one global
#:   list the integration owns. The integration exposes no ``entity_id``
#:   parameter at all, so naming no target is the *only* way to spell any of
#:   them — untargeted here is not a sloppy call, it is the call. The blast
#:   radius is a list of words; nothing in this domain moves, heats, unlocks or
#:   watches anything. The appliance's own ``StewardAgent._list_add`` reaches it
#:   on every "add milk to the shopping list", so gating it puts a confirmation
#:   in front of the single most ordinary thing a household assistant does —
#:   which is precisely how a confirmation stops meaning anything. All eight are
#:   exempt rather than only the additive ones: they all reach the same text,
#:   and gating ``clear_completed_items`` while ``add_item`` sails through would
#:   only beg the question why.
#:
#:   *Name-scoped rather than domain-wide, on purpose.* The eight are a closed,
#:   documented set, so the exemption can be spelled as exactly that set and no
#:   more. Written as a domain it would also cover whatever HA adds to
#:   ``shopping_list`` next — a hypothetical ``shopping_list.press`` or
#:   ``shopping_list.open_cover`` would have inherited the pass silently, with no
#:   test going red. Forward-tightness is cheap here and there is no reason to
#:   spend it.
#:
#:   *Who can actually reach the destructive ones.* Not the model:
#:   ``shopping_list.<service>`` has no model-facing spelling. Every HA actuation
#:   tool in :mod:`puffpai.assistant_tools` is entity-bound or domain-fixed —
#:   ``ha_control_device`` restricts the action to ``turn_on``/``turn_off``/
#:   ``toggle`` and runs :func:`validate_entity_id`, deriving the service domain
#:   from the entity id; ``ha_set_light``/``ha_set_temperature`` are pinned to
#:   ``light``/``climate``; ``ha_run_scene`` goes through
#:   :meth:`HABridge.activate_scene`, which also derives the domain from a
#:   validated entity id; and the legacy ``ha_api_call`` shim — the one
#:   tool-layer path that reached :meth:`HABridge._request` beneath the gate —
#:   has been deleted outright, so no such path remains. No tool accepts a free
#:   ``(domain, service)`` pair, and the Steward's ``list_add`` intent can only
#:   ever emit ``shopping_list.add_item``.
#:   What *is* reachable is the HTTP surface: any holder of the appliance's API
#:   key can ``POST /ha/service/shopping_list/clear_completed_items`` and have it
#:   pass here with nothing asked. That is the honest reachability statement, and
#:   it is the same key that already reaches every other route on the box.
#:
#:   *Honest residual, accepted rather than hidden:*
#:   ``clear_completed_items``, ``complete_all`` and ``remove_item`` destroy list
#:   *data* unasked. That is the same trade already made for
#:   ``persistent_notification.dismiss_all``, and it is an inconvenience, not the
#:   physical-safety or physical-security event this gate's authority is drawn
#:   around (:data:`SAFETY_CRITICAL_DOMAINS`). A lost shopping list is retyped;
#:   an opened lock is not.
#:
#: Matched as exact ``(domain, name)`` membership, never as a prefix or a
#: substring — ``shopping_list_extra.add_item`` and ``light.shopping_list``
#: inherit nothing.
_NON_ACTUATING_DOMAIN_SERVICES: dict[str, frozenset[str]] = {
    "shopping_list": frozenset({
        "add_item", "remove_item", "complete_item", "incomplete_item",
        "complete_all", "incomplete_all", "clear_completed_items", "sort",
    }),
}


def _is_exactly_zero(value: Any) -> bool:
    """True only for the integer zero, spelled as an ``int`` or a digit string.

    Strict on purpose — this decides whether an actuation is a *disarm*, so
    every ambiguity resolves to "no":

    * ``bool`` is tested first because ``True`` is an ``int`` and ``False == 0``
      is ``True``. ``duration=False`` is a caller error, not a close.
    * ``None`` / missing is not zero. HA defaults a missing ``zha.permit``
      duration to a sixty-second **open**, so treating absence as zero would
      exempt the exact call this gate exists to stop.
    * A string must be ASCII digits only. ``str.isdigit()`` alone accepts
      superscripts and other Unicode digit forms; ``"0"`` and ``"00"`` pass,
      ``"0x0"``, ``" 0 0"`` and ``"abc"`` do not. Leading/trailing whitespace is
      stripped because the LLM tool layer passes durations as text.
    * Floats are rejected: ``0.4`` is not a close and is not an open either.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and all(ch in "0123456789" for ch in text) and int(text) == 0
    return False


#: The disarm-direction table: ``(service_domain, service)`` → a predicate over
#: the service data that is true only when the call *reduces* exposure.
#:
#: A table rather than an ``if`` so the next service whose open/close distinction
#: lives in an argument has one obvious place to go, and so the audit question
#: "what can this appliance do untargeted and unasked?" has a single list to
#: read.
#:
#: * ``zha.permit`` — ZHA has no separate close service, so ``duration=0`` *is*
#:   the close of the Zigbee pairing window, and it names no entity because HA
#:   routes it to the coordinator (see :mod:`puffpai.zigbee_manager`). Reducing
#:   exposure never needs permission, and a gate that can refuse the close is a
#:   gate that can hold a pairing window open.
#:   ``ZigbeeManager.close_permit_join`` reaches the bridge with no token at
#:   all, by design, and this is what keeps it working. Every other spelling —
#:   a missing ``duration``, ``60``, ``False``, ``"abc"`` — is not the close and
#:   is gated. The *open* direction is gated twice over: once in
#:   ``ZigbeeManager.open_permit_join``, which passes the token down when a
#:   person has confirmed, and now here for anything that reaches
#:   ``zha.permit`` without going through that method at all.
_DISARM_DIRECTION: dict[tuple[str, str], Any] = {
    ("zha", "permit"): lambda data: _is_exactly_zero(data.get("duration")),
}


def permit_close_script_entity() -> str:
    """The one ``script.*`` entity the owner declared to be the pairing-window
    close, or ``""``.

    Read from config at call time (never cached, never model-supplied) so that
    emptying ``ha.zigbee_permit_close_script`` removes the exemption below with
    no code change. Any failure to read config returns ``""`` — no entity, no
    exemption.
    """
    try:
        from puffpai.config import get_config
        value = getattr(get_config().ha, "zigbee_permit_close_script", "")
    except Exception:  # pragma: no cover - config is unreadable
        return ""
    return str(value or "").strip()


def is_permit_close_script_call(service_domain: Any, service: Any, targets: Any,
                                service_data: Any) -> bool:
    """True only for "trigger the household's Zigbee-pairing *close* script".

    Why this exists. ``zha.permit`` is admin-only in Home Assistant and this
    appliance's HA user is deliberately not an administrator, so the close
    reaches HA as ``script.turn_on`` on a script the admin wrote. That is a
    :data:`_WRAPPER_DOMAINS` target, and
    :meth:`HABridge._expand_stored_targets` cannot read a script's stored
    config as a non-admin either — so it fails closed with ``force=True`` and
    the *close* becomes a confirmation-gated call. A gate that can block the
    close is a gate that can hold a pairing window open, which is exactly what
    :data:`_DISARM_DIRECTION` exists to prevent for ``zha.permit duration: 0``.

    This is the same statically-declared knowledge, one door over: the owner
    named this entity in config as the thing that closes the window. It is
    **not** a substitute for :data:`HUMAN_CONFIRMED` — it only clears the
    "I cannot read this script's steps" uncertainty, and only for this one
    entity id. Every other refusal still applies: a safety-critical domain
    found in a *readable* config still refuses, and the untargeted, unbounded
    and broadcast checks run afterwards, untouched.

    All of these must hold, or it is not the close:

    * the call is exactly ``script.turn_on``;
    * it names exactly one target, and that target is the configured close
      script (a non-empty config value; ``""`` disables this entirely);
    * it carries no other service data — no ``variables``, which an HA script
      can interpolate into the action it performs.
    """
    if str(service_domain or "").strip().lower() != "script":
        return False
    if str(service or "").strip().lower() != "turn_on":
        return False
    configured = permit_close_script_entity()
    if not configured:
        return False
    named = [str(t) for t in as_targets(targets)]
    if named != [configured]:
        return False
    extra = {k: v for k, v in (service_data or {}).items()
             if k != "entity_id"} if isinstance(service_data, dict) else {}
    return not extra


def _untargeted_blast_radius(service_domain: str) -> str:
    """What an untargeted call in *service_domain* reaches, said plainly.

    A refusal a household cannot read is a refusal people learn to click
    through, so the reason names the scope rather than the rule.
    """
    if service_domain == "homeassistant":
        return "every entity in the house"
    # Unioned at call time rather than at import so the rule sits next to the
    # text it serves; the domain sets themselves are defined with the data
    # types below. Same arrangement as :func:`_service_domain_bounds_targets`.
    if service_domain in (CONTROLLABLE_DOMAINS | SENSOR_DOMAINS
                          | SAFETY_CRITICAL_DOMAINS):
        return f"every {service_domain} HA has"
    return f"whatever the {service_domain} integration controls"


def untargeted_request_detail(service_domain: Any, service: Any,
                              targets: Any, service_data: Any) -> str:
    """Why this call sweeps a whole domain, or ``""`` when it names a target.

    Untargeted means all of:

    * :func:`service_call_targets` found nothing — no ``entity_id`` argument, no
      ``entity_id`` in the service data, no ``target: {entity_id: …}``, no
      ``scene.apply`` ``entities:`` mapping;
    * :func:`request_opaque_selectors` found nothing either. When a selector
      *is* present the call does name a set, and
      :func:`unbounded_request_detail` is the policy that governs it — an
      area-targeted ``light.turn_on`` is ordinary dashboard traffic and stays
      allowed;
    * the service is not a per-script service. ``POST services/script/goodnight``
      names its target in the service rather than in an argument, and
      :meth:`HABridge._expand_stored_targets` already reconstructs
      ``script.goodnight``, classifies its steps, and fails closed when it
      cannot read them.

    Everything else untargeted is refused unless it is on one of the three short
    allow-lists — :data:`_NON_ACTUATING_SERVICES` (a service name, in whatever
    domain), :data:`_NON_ACTUATING_SERVICE_DOMAINS` (a whole domain, whatever
    the service), :data:`_NON_ACTUATING_DOMAIN_SERVICES` (named services within
    one domain) — or is the disarming direction of a
    service in :data:`_DISARM_DIRECTION`. An integration domain this appliance
    does not recognise is refused rather than guessed at: over-matching costs a
    confirmation, under-matching costs the gate.
    """
    if targets:
        return ""
    if request_opaque_selectors(service_data):
        return ""

    domain = str(service_domain or "").strip().lower()
    name = str(service or "").strip().lower()
    data = service_data if isinstance(service_data, dict) else {}

    # ``HABridge._GENERIC_SCRIPT_SERVICES`` is read at call time; the class is
    # defined below this function, the same way the domain sets are.
    if (domain == "script" and name
            and name not in HABridge._GENERIC_SCRIPT_SERVICES):
        return ""

    if name in _NON_ACTUATING_SERVICES:
        return ""
    if domain in _NON_ACTUATING_SERVICE_DOMAINS:
        return ""
    # Exact membership in the domain's own set. ``.get`` returning an empty
    # frozenset is what keeps an unlisted domain from being an ``in`` against
    # something truthy, and the set is never treated as a prefix list.
    if name in _NON_ACTUATING_DOMAIN_SERVICES.get(domain, frozenset()):
        return ""
    disarms = _DISARM_DIRECTION.get((domain, name))
    if disarms is not None and disarms(data):
        return ""

    label = f"{domain or '?'}.{name or '?'}"
    return (f"untargeted {label} names no entity and reaches "
            f"{_untargeted_blast_radius(domain)} — name the entities to "
            f"actuate, or confirm the house-wide sweep")


# ---------------------------------------------------------------------------
# The same sweep, spelled out loud
# ---------------------------------------------------------------------------
# The rule above catches a call that names *nothing*. It cannot catch the one
# that names *everything*: ``entity_id: "all"`` is HA's explicit broadcast, and
# it is a perfectly ordinary named target — :func:`service_call_targets` returns
# ``["all"]``, so the untargeted check stands down by construction and
# ``light.turn_off entity_id="all"`` reached the wire with nothing asked. The
# blast radius is identical to the untargeted form; only the spelling differs.
# So it gets the identical refusal, under its own name.


def _is_broadcast_target(value: Any) -> bool:
    """Whether *value* is HA's broadcast target rather than an entity.

    HA's ``ENTITY_MATCH_ALL`` is the exact lowercase string ``"all"``. The match
    here is looser — ``.strip().lower()`` — under the file's standing rule that
    over-matching costs a confirmation and under-matching costs the gate. A
    caller who writes ``"All"`` or ``" all "`` means the sweep; HA would expand
    that spelling to nothing, so prompting for it costs one confirmation on a
    call that was going to do nothing anyway, while the strict-only reading
    would hand the sweep a spelling that walks past the gate.

    ``"none"`` — HA's other magic value, ``ENTITY_MATCH_NONE`` — is deliberately
    **not** matched. It names zero entities, HA expands it to nothing, and the
    call moves nothing; its existing classification (a named target, so the call
    reaches the wire) is kept exactly as it is. Non-strings never match.
    """
    return isinstance(value, str) and value.strip().lower() == "all"


def broadcast_request_detail(service_domain: Any, service: Any,
                             targets: Any) -> str:
    """Why this call is the house-wide sweep by name, or ``""`` when it is not.

    :func:`untargeted_request_detail` refuses a call that names no target,
    because HA reads a missing target as the whole domain. ``entity_id: "all"``
    reaches exactly the same set and is refused for exactly the same reason —
    but it is a *named* target, so that check stands down on it by construction:
    it requires every naming place to be empty, and this one requires ``"all"``
    to be named. The two are mutually exclusive, and between them the sweep is
    covered however it is spelled.

    *targets* is :func:`service_call_targets`' output, which already folds every
    place a call can name a target — the ``entity_id`` argument, an ``entity_id``
    key in the service data, the modern ``target: {entity_id: …}`` block, and a
    ``scene.apply`` ``entities:`` mapping's keys. That is why this function does
    not re-read the service data: doing so would be a second, divergent reading
    of the same question.

    Deliberately **no allow-list**. Nothing in this appliance needs to spell
    ``"all"``: the one caller that did — ``_agent_task_lights`` in
    :mod:`puffpai.api_server` — now enumerates the lights it means, so the
    good-morning, leaving-home and control-lights routines still sweep the
    house's lights, over a call that names each one. There is no longer any
    caller left to exempt. An unused exemption is a free hole.

    Two things this rule does not touch, both on purpose:

    * the ``zha.permit`` disarm close (``duration=0``) names no entity at all,
      so it never meets this rule and the close path stays ungated — a gate that
      can refuse the close is a gate that can hold a pairing window open;
    * a real entity whose object id is literally ``all`` (``light.all``) is not
      the broadcast — it is one lamp with an unfortunate name — and passes
      untouched.
    """
    if not any(_is_broadcast_target(t) for t in as_targets(targets)):
        return ""
    domain = str(service_domain or "").strip().lower()
    name = str(service or "").strip().lower()
    label = f"{domain or '?'}.{name or '?'}"
    return (f'{label} names entity_id "all" — HA\'s explicit broadcast — and '
            f"reaches {_untargeted_blast_radius(domain)}; name the entities to "
            f"actuate, or confirm the house-wide sweep")


#: Jinja markers. A template in a dispatch position means the service or the
#: target is computed at run time and is not knowable now.
_TEMPLATE_MARKERS = ("{{", "{%")

#: Domains that are themselves *stored instructions*. An automation whose action
#: runs ``script.nightly`` defers again, one level further out, and expanding it
#: would need another fetch per level. Treated as not-knowable rather than
#: benign — the same rule this whole gate exists to apply.
#:
#: **Chained deferral is deliberately not resolved.** A script that calls an
#: automation (or the reverse) is refused here rather than expanded one level
#: with a recursion cap. Three reasons, in order of weight:
#:
#: 1. A step may pass ``variables:`` into the callee, and a callee whose own
#:    steps are templated on those variables is *not* determinable from its
#:    stored config alone — so one-level expansion would produce a confident
#:    "known" verdict that is simply wrong. A wrong "known" is a silent bypass;
#:    a refusal is a prompt. Those failure modes are not comparable.
#: 2. The cap has to fail closed anyway, so expansion only converts *some*
#:    refusals into approvals while adding a fetch per level, a cycle guard and
#:    a depth counter to the one code path that must stay readable.
#: 3. The refusal is already actionable: it names the exact callee token, so a
#:    person can inspect that entity directly and confirm once.
_OPAQUE_CALLEE_DOMAINS = frozenset({"script", "scene", "automation"})

#: Where an automation keeps its actions, and where a script keeps its steps.
#: HA renamed automation ``action:`` → ``actions:`` and script ``sequence:`` →
#: ``actions:`` in 2024.10; every spelling still loads, so every spelling is
#: read. First key present wins.
_AUTOMATION_BLOCK_KEYS = ("actions", "action")
_SCRIPT_BLOCK_KEYS = ("sequence", "actions", "action")

#: A ``<domain>.<object>`` token anywhere in a string leaf.
_DOMAIN_TOKEN_RE = re.compile(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+")


def _stored_action_domains(config: Any, *, block_keys: tuple[str, ...],
                           noun: str, block_label: str,
                           step_noun: str) -> tuple[set[str], bool, str]:
    """Domains a stored instruction's steps would reach, and whether we can tell.

    The shared body of :func:`automation_action_domains` and
    :func:`script_sequence_domains`. One walk, one set of failure reasons, one
    place to fix: an automation and a script differ only in where HA keeps the
    steps and in what a person calls them, never in what makes a step dangerous.

    Returns ``(domains, known, why)``. ``known`` is False when the config was
    readable but its steps are not fully determinable; ``why`` then says what
    made them indeterminable.

    Only the **steps** are examined. An automation's triggers and conditions do
    not actuate, so one that *fires on* ``cover.garage_door`` opening and only
    turns a light on is correctly treated as benign.

    Within the block the walk is deliberately crude — every string leaf is
    scanned for a ``<domain>.<object>`` token — which is what makes nested
    ``choose`` / ``if`` / ``repeat`` / ``parallel`` / ``sequence`` structures
    work without enumerating them: an unrecognised wrapper is still walked, and
    a domain named anywhere under it is still found. Over-matching costs a
    confirmation prompt. Under-matching costs the gate.
    """
    if not isinstance(config, dict):
        return set(), False, f"the stored {noun} config is not a mapping"

    for key in block_keys:
        if key in config:
            steps = config[key]
            break
    else:
        return set(), False, f"the stored config has no {block_label} block"

    if steps is None or isinstance(steps, (int, float, bool)):
        return set(), False, f"the {noun}'s {block_label} block is not a sequence"

    domains: set[str] = set()
    unknown: list[str] = []

    def note(reason: str) -> None:
        if reason not in unknown:
            unknown.append(reason)

    def scan(text: str, *, dispatch: bool) -> None:
        for token in _DOMAIN_TOKEN_RE.findall(text):
            domain = token.split(".", 1)[0]
            domains.add(domain)
            if domain in _OPAQUE_CALLEE_DOMAINS:
                note(f"one {step_noun} runs another stored instruction "
                     f"({token}), whose own contents are not expanded here — "
                     f"inspect {token} directly, or confirm to run it")
        if dispatch and any(marker in text for marker in _TEMPLATE_MARKERS):
            note(f"a template decides which service or entity a {step_noun} "
                 f"calls")

    def walk(node: Any, *, dispatch: bool = False) -> None:
        if isinstance(node, str):
            scan(node, dispatch=dispatch)
        elif isinstance(node, dict):
            for key, value in node.items():
                name = str(key).strip().lower()
                if name in _OPAQUE_TARGET_KEYS:
                    note(f"one {step_noun} targets by {name} rather than by "
                         f"named entities, so its real targets are not knowable "
                         f"here")
                walk(value, dispatch=dispatch or name in _ACTION_DISPATCH_KEYS)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item, dispatch=dispatch)
        # numbers / bools / None name nothing.

    # A single-step instruction stores the step as a bare mapping rather than a
    # list, and a degenerate one can be a bare string; both are walked. The
    # string case starts in dispatch position because the whole block is then
    # the service name.
    walk(steps, dispatch=isinstance(steps, str))
    return domains, not unknown, "; ".join(unknown)


def automation_action_domains(config: Any) -> tuple[set[str], bool, str]:
    """Domains an automation's *actions* would reach, and whether we can tell.

    The automation analogue of :func:`scene_member_domains`: classify by what
    the thing would *do*, not by the domain it happens to live in.
    ``automation.trigger`` on an automation whose action is ``camera.turn_off``
    is a camera actuation with a delay. See :func:`_stored_action_domains` for
    the walk and the return shape.
    """
    return _stored_action_domains(
        config, block_keys=_AUTOMATION_BLOCK_KEYS, noun="automation",
        block_label="action", step_noun="action")


def script_sequence_domains(config: Any) -> tuple[set[str], bool, str]:
    """Domains a script's *sequence* would reach, and whether we can tell.

    ``script.*`` is the identical problem to ``automation.*`` one door over.
    :func:`scene_member_domains` returns early for a script — a script has no
    member list, so there was nothing to expand and it was classified as the
    harmless ``script`` domain. But ``script.goodnight`` may well run
    ``camera.turn_off``, and running it is that actuation with the instruction
    written earlier. Same rule, same walk: see :func:`_stored_action_domains`.
    """
    return _stored_action_domains(
        config, block_keys=_SCRIPT_BLOCK_KEYS, noun="script",
        block_label="sequence", step_noun="step")


#: Stored-instruction domains and the bridge method that resolves each one.
#: Looked up rather than branched so that adding a third kind is a line here
#: and not another ``elif`` in four call sites.
_STORED_INSTRUCTION_RESOLVERS = {
    "automation": "automation_actuation_domains",
    "script": "script_actuation_domains",
}


def stored_instruction_domains(reader: Any,
                               entity_id: Any) -> tuple[set[str], bool, str]:
    """What actuating *entity_id* would really reach, expanding wrappers.

    The single dispatcher every door goes through. Three cases:

    * ``scene.*`` — expand the member list (:func:`scene_member_domains`);
    * ``script.*`` / ``automation.*`` — expand the stored steps, by asking
      *reader* for the resolver named in :data:`_STORED_INSTRUCTION_RESOLVERS`;
    * anything else — an actuator addressed directly; classify it as itself.

    Returns ``(domains, known, detail)``, where *detail* is a clause ready to
    hand :func:`enforce_confirmation`.

    *reader* is anything with the relevant methods — the bridge itself, or the
    Steward's envelope-bound proxy over it. A *reader* that does not have the
    resolver at all (an injected test double, a future non-``HABridge``
    backend) fails **closed**: "this object cannot tell me what that would do"
    is not "that does nothing dangerous".
    """
    domains = touched_domains(None, entity_id)
    domain = entity_domain(entity_id)

    if domain == "scene":
        found, known = scene_member_domains(reader, entity_id)
        return (found, known,
                "" if known else "cannot enumerate this scene's members")

    resolver_name = _STORED_INSTRUCTION_RESOLVERS.get(domain or "")
    if resolver_name is None:
        return domains, True, ""

    if not is_valid_entity_id(entity_id):
        return domains, False, (
            f"cannot inspect this {domain}'s steps — "
            f"{_echo(entity_id)} is not a well-formed entity id")

    resolver = getattr(reader, resolver_name, None)
    if not callable(resolver):
        logger.warning("%s: %r cannot resolve stored steps; treating as "
                       "always-confirm", entity_id, type(reader).__name__)
        return domains, False, (
            f"cannot inspect this {domain}'s steps — the house connection in "
            f"use has no way to read its stored config")
    try:
        return resolver(entity_id)
    except HABridgeError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("%s: resolving stored steps failed (%s); treating as "
                       "always-confirm", entity_id, exc)
        return domains, False, (
            f"cannot inspect this {domain}'s steps "
            f"({type(exc).__name__})")


def enforce_confirmation(domains, *, service: str, entity_id: Any,
                         confirmation: Any, force: bool = False,
                         detail: str = "") -> None:
    """Refuse an always-confirm actuation that carries no human confirmation.

    The one place the policy is applied. :class:`HABridge` calls it on the way
    to the wire; the Steward's ``_GuardedHA`` calls it too so that an injected
    test double or a future non-``HABridge`` backend cannot silently remove the
    gate. Same function, same verdict — the policy exists once.

    Args:
        domains: the HA domains this call would really touch, derived from the
            arguments (:func:`touched_domains`), never from a caller-supplied
            label.
        force: refuse regardless of *domains* — used when the set could not be
            determined (an unreadable scene member list).
    """
    critical = set(domains) & SAFETY_CRITICAL_DOMAINS
    if not critical and not force:
        return
    if is_human_confirmation(confirmation, where=service):
        return
    why = (", ".join(sorted(critical)) if critical
           else "targets this appliance could not enumerate")
    reason = (f"{service} would actuate the always-confirm class ({why}) and "
              "carries no human confirmation")
    if detail:
        reason = f"{reason}: {detail}"
    raise ConfirmationRequired(
        {"service": service, "entity_id": entity_id,
         "domains": sorted(critical)},
        reason)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

CONTROLLABLE_DOMAINS = frozenset([
    "light", "switch", "fan", "cover", "climate", "media_player",
    "lock", "vacuum", "camera", "scene", "automation", "script",
    "siren", "remote", "number", "select", "button",
])

SENSOR_DOMAINS = frozenset([
    "sensor", "binary_sensor", "weather", "sun", "zone", "person",
])

COLOR_TEMP_PRESETS = {
    "warm": 400,
    "neutral": 300,
    "cool": 180,
    "daylight": 153,
}


@dataclass
class DeviceInfo:
    """Normalised representation of an HA entity."""
    entity_id: str
    domain: str
    friendly_name: str
    state: str
    attributes: dict[str, Any]
    last_changed: str
    last_updated: str
    area_id: str | None = None
    area_name: str | None = None

    @property
    def is_controllable(self) -> bool:
        return self.domain in CONTROLLABLE_DOMAINS

    @property
    def is_on(self) -> bool:
        return self.state in ("on", "playing", "open", "unlocked", "heat", "cool", "auto")


@dataclass
class Room:
    """A room / area with its devices."""
    area_id: str
    name: str
    devices: list[DeviceInfo] = field(default_factory=list)


@dataclass
class AutomationInfo:
    """HA automation entity with extra metadata."""
    entity_id: str
    friendly_name: str
    state: str  # "on" or "off"
    last_triggered: str | None
    mode: str | None  # single, restart, queued, parallel


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

class _StateCache:
    """Thread-safe TTL cache for HA state data."""

    def __init__(self, ttl: float = 5.0):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._timestamps: dict[str, float] = {}

    def get(self, key: str) -> Any | None:
        with self._lock:
            ts = self._timestamps.get(key, 0)
            if time.monotonic() - ts < self._ttl:
                return self._data.get(key)
        return None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._timestamps[key] = time.monotonic()

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
                self._timestamps.clear()
            else:
                self._data.pop(key, None)
                self._timestamps.pop(key, None)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

def _resolve_ha_resolution(*, refresh: bool = False):
    """The one HA-token lookup, as a Resolution (token + why). Never raises."""
    try:
        from puffpai.security.ha_token_source import resolve_ha_token
        return resolve_ha_token(refresh=refresh)
    except Exception as exc:  # the bridge must degrade, never crash on import
        logger.debug("HA token source unavailable: %s", type(exc).__name__)

        class _Unavailable:
            token = None
            source = None
            detail = "the Home Assistant token source could not be loaded"
        return _Unavailable()


def _resolve_ha_token() -> str | None:
    return _resolve_ha_resolution().token


class HABridge:
    """Unified Home Assistant client with caching and device registry."""

    #: Advertises that this object applies the always-confirm gate itself, so a
    #: wrapper (the Steward's ``_GuardedHA``) knows it may hand the
    #: :data:`HUMAN_CONFIRMED` token down. A test double that does not declare
    #: this never receives the token — it would land in the double's ``**kwargs``
    #: and be recorded as if it were HA service data.
    ENFORCES_CONFIRMATION = True

    def __init__(self, url: str | None = None, token: str | None = None):
        cfg = get_config()
        self._url = (url or cfg.ha.url).rstrip("/")
        # The token comes from ha_token_source, never from
        # cfg.ha.token or config/home_assistant_config.json. An explicit token
        # argument still wins (tests and the setup wizard pass one).
        self._token = token or _resolve_ha_token()
        self._enabled = cfg.ha.enabled
        self._cache = _StateCache(ttl=5.0)
        self._session = requests.Session()
        self._session.headers.update({
            # No token (strict mode at a cold boot) -> an EMPTY header, never
            # the string "Bearer None". _ensure_token() fills it in on the
            # first request after an unlock, and refuses before that.
            "Authorization": f"Bearer {self._token}" if self._token else "",
            "Content-Type": "application/json",
        })
        self._session.timeout = 10

    def _ensure_token(self) -> None:
        """Re-resolve the token if we do not have one, then fail closed.

        Why this is at request time and not only at construction: on a
        strict-mode appliance the vault is locked at boot, so the bridge is
        built with no credential. Re-resolving here means the first HA call
        after the owner unlocks their vault succeeds -- no restart, no stale
        singleton. When there is still no token we refuse
        BEFORE opening a socket, with a message that says which of the two
        states we are in and never contains the credential.
        """
        if self._token:
            return
        resolution = _resolve_ha_resolution(refresh=True)
        if resolution.token:
            self._token = resolution.token
            self._session.headers["Authorization"] = f"Bearer {self._token}"
            return
        raise HABridgeError(
            "Home Assistant credentials are locked in the vault: "
            + resolution.detail)

    # -- low-level request ----------------------------------------------------

    def _request(
        self,
        endpoint: str,
        method: str = "GET",
        data: Any = None,
        timeout: int | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make an authenticated request to the HA REST API.

        Raises HABridgeError on any failure.
        """
        if not self._enabled:
            raise HABridgeError("Home Assistant integration is disabled")
        self._ensure_token()

        _assert_no_traversal(endpoint)
        url = f"{self._url}/api/{endpoint}"
        kwargs: dict[str, Any] = {
            "timeout": timeout or 10,
            "stream": stream,
        }

        try:
            if method == "GET":
                resp = self._session.get(url, **kwargs)
            elif method == "POST":
                resp = self._session.post(url, json=data, **kwargs)
            elif method == "DELETE":
                resp = self._session.delete(url, **kwargs)
            else:
                raise HABridgeError(f"Unsupported HTTP method: {method}")

            resp.raise_for_status()
            return resp

        except requests.exceptions.ConnectionError:
            raise HABridgeError(f"Cannot connect to Home Assistant at {self._url}")
        except requests.exceptions.Timeout:
            raise HABridgeError("Home Assistant request timed out")
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            body = ""
            try:
                body = exc.response.text[:200]
            except Exception:
                pass
            raise HABridgeError(f"HA returned HTTP {status}: {body}")

    # -- connection -----------------------------------------------------------

    def is_connected(self) -> bool:
        """Check whether HA is reachable."""
        try:
            resp = self._request("")
            return resp.ok
        except HABridgeError:
            return False

    def get_config(self) -> dict[str, Any]:
        """Return the HA /api/config payload."""
        cached = self._cache.get("config")
        if cached is not None:
            return cached
        data = self._request("config").json()
        self._cache.set("config", data)
        return data

    def get_status(self) -> dict[str, Any]:
        """Connection status summary for API consumers."""
        try:
            config = self.get_config()
            return {
                "connected": True,
                "ha_url": self._url,
                "ha_version": config.get("version", "unknown"),
                "location_name": config.get("location_name", ""),
            }
        except HABridgeError as exc:
            return {
                "connected": False,
                "ha_url": self._url,
                "error": str(exc),
            }

    # -- state ----------------------------------------------------------------

    def get_all_states(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Return all entity states (raw HA format). Cached for 5 s."""
        if not force:
            cached = self._cache.get("states")
            if cached is not None:
                return cached
        states = self._request("states").json()
        self._cache.set("states", states)
        return states

    def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        """Return a single entity's state.

        Raises :class:`HABridgeError` before any HTTP call when *entity_id* is
        not a canonical HA entity id — this path is reachable from pushed
        events, so an unvalidated id here is an authenticated read primitive
        against arbitrary HA endpoints.
        """
        validate_entity_id(entity_id)
        return self._request(f"states/{quote_segment(entity_id)}").json()

    # -- devices --------------------------------------------------------------

    def _parse_device(self, raw: dict[str, Any], area_map: dict[str, str] | None = None,
                      area_index: dict[str, str] | None = None) -> DeviceInfo:
        """Convert a raw HA state dict into a DeviceInfo.

        *area_index* (``entity_id -> area_id``, from the registries) is the
        authority. ``attributes.area_id`` stays as a fallback only because a
        template entity can be written with one by hand; Home Assistant itself
        never puts an area there, so reading only the attribute leaves every
        device unassigned.
        """
        eid = raw.get("entity_id", "")
        attrs = raw.get("attributes", {})
        domain = eid.split(".")[0] if "." in eid else "unknown"
        area_id = (area_index or {}).get(eid) or attrs.get("area_id")
        area_name = None
        if area_id and area_map:
            area_name = area_map.get(area_id, area_id)
        return DeviceInfo(
            entity_id=eid,
            domain=domain,
            friendly_name=attrs.get("friendly_name", eid),
            state=raw.get("state", "unknown"),
            attributes=attrs,
            last_changed=raw.get("last_changed", ""),
            last_updated=raw.get("last_updated", ""),
            area_id=area_id,
            area_name=area_name,
        )

    def list_devices(
        self,
        domain: str | None = None,
        controllable_only: bool = True,
    ) -> list[DeviceInfo]:
        """List devices, optionally filtered by domain."""
        states = self.get_all_states()
        area_map, area_index = self._registry_areas()
        devices = []
        for raw in states:
            dev = self._parse_device(raw, area_map, area_index)
            if controllable_only and dev.domain not in CONTROLLABLE_DOMAINS:
                continue
            if domain and dev.domain != domain:
                continue
            devices.append(dev)
        return devices

    def get_device(self, entity_id: str) -> DeviceInfo:
        """Get full info for a single device."""
        raw = self.get_entity_state(entity_id)
        area_map, area_index = self._registry_areas()
        return self._parse_device(raw, area_map, area_index)

    def find_devices(self, query: str) -> list[DeviceInfo]:
        """Search devices by partial name or entity_id match."""
        query_lower = query.lower()
        states = self.get_all_states()
        area_map, area_index = self._registry_areas()
        matches = []
        for raw in states:
            eid = raw.get("entity_id", "").lower()
            fname = raw.get("attributes", {}).get("friendly_name", "").lower()
            if query_lower in eid or query_lower in fname:
                matches.append(self._parse_device(raw, area_map, area_index))
        return matches

    # -- device control -------------------------------------------------------

    #: Services in the ``script`` domain that are *not* the name of a script.
    #: Everything else in that domain is: HA registers ``script.<object_id>``
    #: as a service per script, so ``POST services/script/goodnight`` runs
    #: ``script.goodnight`` while naming no entity at all. Without this set that
    #: call carries the domains ``{"script"}``, which is never safety-critical,
    #: and the whole gate is one URL away from being irrelevant.
    _GENERIC_SCRIPT_SERVICES = frozenset({
        "turn_on", "turn_off", "toggle", "reload",
    })

    #: Wrapper domains whose targets have to be expanded before they can be
    #: classified. Each one is an instruction that names *other* entities:
    #: ``scene.goodnight`` may contain ``cover.garage_door``,
    #: ``script.goodnight`` may run ``camera.turn_off``, and neither ``scene``
    #: nor ``script`` nor ``automation`` is itself safety-critical.
    _WRAPPER_DOMAINS = frozenset({"scene", "script", "automation"})

    def _expand_stored_targets(self, service_domain: Any, service: Any,
                               entity_id: Any,
                               domains: set[str]) -> tuple[bool, str]:
        """Fold in what any wrapper target would itself actuate.

        Called from :meth:`call_service`, the choke point, so the gate covers
        **every** route to firing a stored instruction — not just
        :meth:`trigger_automation` or :meth:`activate_scene`. Without this,
        ``POST /ha/service/automation/trigger {"entity_id": "automation.x"}``,
        ``POST /ha/service/script/turn_on {"entity_id": "script.x"}``,
        ``POST /ha/service/script/x`` (the per-script service, which names no
        entity at all) and ``POST /ha/control {"entity_id": "script.x",
        "action": "turn_on"}`` all reach the same actuation through the generic
        passthrough — doors immediately next to the one being closed.

        Mutates *domains* in place and returns ``(force, detail)`` for
        :func:`enforce_confirmation`. A call with no wrapper target does no work
        and makes no request.
        """
        targets = [t for t in as_targets(entity_id)
                   if entity_domain(t) in self._WRAPPER_DOMAINS
                   and is_valid_entity_id(t)]

        # ``script.<object_id>`` invoked as a *service*: the script is named by
        # the service, not by any entity_id, so reconstruct the entity it means.
        name = str(service or "").strip().lower()
        if (str(service_domain or "").strip().lower() == "script"
                and name and name not in self._GENERIC_SCRIPT_SERVICES):
            implied = f"script.{name}"
            if is_valid_entity_id(implied) and implied not in targets:
                targets.append(implied)

        if not targets:
            return False, ""

        force = False
        reasons: list[str] = []
        seen: set[str] = set()
        for target in targets:
            if target in seen:
                continue
            seen.add(target)
            found, known, why = stored_instruction_domains(self, target)
            domains |= found
            if not known:
                force = True
                if why and why not in reasons:
                    reasons.append(why)
        return force, "; ".join(reasons)

    #: Historical name, kept for callers that reached for it directly.
    def _expand_automation_targets(self, entity_id: Any,
                                   domains: set[str]) -> tuple[bool, str]:
        return self._expand_stored_targets(None, None, entity_id, domains)

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | list[str] | None = None,
        *,
        human_confirmed: Any = False,
        **service_data: Any,
    ) -> list[dict[str, Any]]:
        """Call an HA service and return the resulting state array.

        Invalidates the state cache after the call.

        **This is the choke point.** Every mutating helper on this class —
        :meth:`control_device`, :meth:`set_light`, :meth:`set_climate`,
        :meth:`activate_scene`, :meth:`trigger_automation`,
        :meth:`toggle_automation` — reaches HA through here, so the
        always-confirm gate applied here covers all of them and anything added
        later. The domains are re-derived from *domain* and from every place the
        request names a target (:func:`service_call_targets`), never from what
        the caller says the call is for — and a request that names its targets in
        a way this appliance cannot resolve at all
        (:func:`unbounded_request_detail`), or names no target whatsoever and so
        sweeps a whole domain (:func:`untargeted_request_detail`), or names that
        same whole-domain sweep out loud as ``entity_id: "all"``
        (:func:`broadcast_request_detail`), is refused rather than guessed at.

        Args:
            human_confirmed: pass :data:`HUMAN_CONFIRMED` — the token object,
                not ``True`` — to actuate a domain in
                :data:`SAFETY_CRITICAL_DOMAINS`. Defaults to refusing. The
                parameter is keyword-only and declared explicitly so that it is
                consumed here and never forwarded to HA as service data.
        """
        validate_segment(domain, kind="service domain")
        validate_segment(service, kind="service name")

        targets = service_call_targets(entity_id, service_data,
                                       service_domain=domain, service=service)
        domains = touched_domains(domain, targets)
        force, detail = self._expand_stored_targets(
            domain, service, targets, domains)
        if force and is_permit_close_script_call(domain, service, targets,
                                                 service_data):
            # The disarming direction of the pairing window, reached through
            # the owner-declared script because ``zha.permit`` is admin-only.
            # Clears *only* the unreadable-steps uncertainty, and only here,
            # before the untargeted/unbounded/broadcast checks below — a
            # safety-critical domain in ``domains`` still refuses.
            logger.info("ha: %s is the configured Zigbee pairing close "
                        "script; treating as the disarm direction (%s)",
                        targets, detail or "steps unreadable")
            force, detail = False, ""
        # A call that targets by area/device/floor/label names no entity this
        # appliance can resolve, so the domains above are only as good as the
        # service domain's own bound. When there is no such bound, the target
        # set is not knowable and unknown fails closed.
        unbounded = unbounded_request_detail(domain, service, service_data)
        if unbounded:
            force = True
            # Fact first, then whatever the wrapper expansion could not tell.
            detail = "; ".join(d for d in (unbounded, detail) if d)
        # And the shape underneath that one: a call that names *no* target at
        # all is not a narrow call, it is the whole domain. Mutually exclusive
        # with the check above by construction — that one needs a selector
        # present, this one needs every naming place empty.
        untargeted = untargeted_request_detail(domain, service, targets,
                                               service_data)
        if untargeted:
            force = True
            detail = "; ".join(d for d in (untargeted, detail) if d)
        # The same sweep spelled out loud: entity_id "all" is HA's explicit
        # broadcast — a *named* target the untargeted check stands down for, with
        # the identical blast radius. Mutually exclusive with that check by
        # construction: it needs every naming place empty, this needs "all" named.
        broadcast = broadcast_request_detail(domain, service, targets)
        if broadcast:
            force = True
            detail = "; ".join(d for d in (broadcast, detail) if d)
        enforce_confirmation(
            domains, service=f"{domain}.{service}", entity_id=entity_id,
            confirmation=human_confirmed, force=force, detail=detail)

        data: dict[str, Any] = dict(service_data)
        if entity_id is not None:
            data["entity_id"] = entity_id

        resp = self._request(
            f"services/{quote_segment(domain)}/{quote_segment(service)}",
            method="POST", data=data,
        )
        self._cache.invalidate("states")

        try:
            return resp.json()
        except Exception:
            return []

    def control_device(
        self,
        entity_id: str,
        action: str,
        *,
        human_confirmed: Any = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """High-level device control (turn_on/turn_off/toggle + params).

        Returns a result dict with ``success``, ``entity_id``, ``action``.
        Extra kwargs are passed as service_data (brightness_pct, temperature, etc.).

        The always-confirm gate is applied by :meth:`call_service` below, on the
        domain *derived from the entity id* — which is the whole reason a gate
        keyed on anything the caller says is not a gate. ``human_confirmed`` is
        declared explicitly so a caller that splats external input into
        ``**kwargs`` (``/ha/control`` does exactly that) binds it here, where
        :func:`is_human_confirmation` rejects a bare ``True``, instead of having
        it silently forwarded to HA as service data.
        """
        action = action.lower().strip()
        if action not in ("turn_on", "turn_off", "toggle"):
            raise HABridgeError(
                f"Unknown action {_echo(action)}. Use turn_on, turn_off, or toggle.")

        validate_entity_id(entity_id)
        domain = entity_id.split(".")[0]
        self.call_service(domain, action, entity_id=entity_id,
                          human_confirmed=human_confirmed, **kwargs)
        return {"success": True, "entity_id": entity_id, "action": action}

    def set_light(
        self,
        entity_id: str,
        brightness_pct: int | None = None,
        color_temp: str | int | None = None,
        rgb_color: tuple[int, int, int] | None = None,
    ) -> dict[str, Any]:
        """Set light properties (brightness, colour temperature, rgb)."""
        validate_entity_id(entity_id)
        data: dict[str, Any] = {}
        if brightness_pct is not None:
            data["brightness_pct"] = max(0, min(100, brightness_pct))
        if color_temp is not None:
            if isinstance(color_temp, str):
                data["color_temp"] = COLOR_TEMP_PRESETS.get(color_temp.lower(), 300)
            else:
                data["color_temp"] = color_temp
        if rgb_color is not None:
            data["rgb_color"] = list(rgb_color)

        self.call_service("light", "turn_on", entity_id=entity_id, **data)
        return {"success": True, "entity_id": entity_id, **data}

    def set_climate(
        self,
        entity_id: str,
        temperature: float | None = None,
        hvac_mode: str | None = None,
    ) -> dict[str, Any]:
        """Set thermostat temperature and/or mode."""
        validate_entity_id(entity_id)
        data: dict[str, Any] = {}
        if temperature is not None:
            data["temperature"] = temperature
        if hvac_mode is not None:
            data["hvac_mode"] = hvac_mode

        self.call_service("climate", "set_temperature", entity_id=entity_id, **data)
        return {"success": True, "entity_id": entity_id, **data}

    # -- rooms / areas --------------------------------------------------------

    def _registry_areas(self) -> tuple[dict[str, str], dict[str, str]]:
        """``(area_id -> name, entity_id -> area_id)`` from HA's registries.

        Neither map can be built over REST: ``config/area_registry/list`` is a
        **WebSocket-only** command, and asking REST for it fails on every call
        (``/ha/rooms`` would report one room called "Unassigned" holding the
        whole house). The registries are pulled
        over the WebSocket by ``bridge_inventory`` and resolved by
        :mod:`puffpai.home_topology`; this reads that, so the blueprint, the
        rooms route and the voice tools cannot disagree about a room.

        Never raises: no area information is a degraded page, not an outage.
        """
        cached = self._cache.get("area_registry")
        if cached is not None:
            return cached
        try:
            from puffpai import home_topology
            snapshot = home_topology.registry_snapshot()
            names = home_topology.area_names(snapshot["areas"])
            # The owner's own rooms and placements go through the *same*
            # resolver the blueprint uses, so a device they dragged onto the
            # map is in that room here too. Without this, /ha/home and
            # /ha/rooms answered "which room is the floor lamp in" differently
            # the moment a placement disagreed with the hub.
            rooms, placements = home_topology.layout_rooms_and_placements()
            for area_id, name in rooms.items():
                names.setdefault(area_id, name)
            result = (names,
                      home_topology.entity_area_ids(
                          snapshot["devices"], snapshot["entities"],
                          placements, set(names)))
        except Exception as exc:
            logger.info("HA area registry unavailable: %s", exc)
            result = ({}, {})
        self._cache.set("area_registry", result)
        return result

    def _get_area_map(self) -> dict[str, str]:
        """Return {area_id: area_name} mapping. Cached."""
        return self._registry_areas()[0]

    def get_rooms(self) -> list[Room]:
        """Return all HA areas with their devices grouped in."""
        area_map, area_index = self._registry_areas()
        states = self.get_all_states()

        rooms: dict[str, Room] = {}
        unassigned = Room(area_id="unassigned", name="Unassigned")

        for raw in states:
            dev = self._parse_device(raw, area_map, area_index)
            if not dev.is_controllable and dev.domain not in SENSOR_DOMAINS:
                continue

            area = dev.area_id
            if area and area in area_map:
                if area not in rooms:
                    rooms[area] = Room(area_id=area, name=area_map[area])
                rooms[area].devices.append(dev)
            else:
                unassigned.devices.append(dev)

        result = sorted(rooms.values(), key=lambda r: r.name)
        if unassigned.devices:
            result.append(unassigned)
        return result

    # -- automations ----------------------------------------------------------

    def get_automations(self) -> list[AutomationInfo]:
        """Return all automation.* entities."""
        states = self.get_all_states()
        automations = []
        for raw in states:
            eid = raw.get("entity_id", "")
            if not eid.startswith("automation."):
                continue
            attrs = raw.get("attributes", {})
            automations.append(AutomationInfo(
                entity_id=eid,
                friendly_name=attrs.get("friendly_name", eid),
                state=raw.get("state", "unknown"),
                last_triggered=attrs.get("last_triggered"),
                mode=attrs.get("current", None),
            ))
        return automations

    # -- what an automation would actually DO ---------------------------------

    #: Shape of the ``id`` HA puts in an automation entity's attributes, and
    #: which is also the path segment of the editor config API. UI-created
    #: automations get a millisecond timestamp; ours get ``puffpai_<hex>``.
    #: Anything outside this shape is refused rather than fetched — the value
    #: comes back from HA, but it is still interpolated into an authenticated
    #: request path, and a fetch we refuse to make fails closed anyway.
    _AUTOMATION_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

    #: A script's editor key is its object_id, validated by HA as ``cv.slug``
    #: — *not* an ``id`` attribute, which scripts do not have. So the key comes
    #: from the entity id itself and is re-checked against the slug shape here
    #: before it is interpolated into an authenticated request path.
    _SCRIPT_KEY_RE = re.compile(r"\A[a-z0-9_]{1,64}\Z")

    def _stored_config_cache(self) -> _StateCache:
        """The short-TTL store for fetched automation and script configs.

        Built lazily so a test double that skips ``__init__`` still works, and
        kept separate from ``_cache`` so the 5 s state TTL and this one can move
        independently.

        **Caches config, never a decision.** Keys are namespaced by kind
        (``automation:<id>`` / ``script:<object_id>``) so an automation id and a
        script object_id that happen to be the same string cannot collide and
        hand one entity the other's steps. Every call re-runs the classifier and
        :func:`enforce_confirmation` over the cached bytes, so no verdict and no
        confirmation is ever replayed from this cache.
        """
        cache = getattr(self, "_automation_cache", None)
        if cache is None:
            cache = _StateCache(ttl=STORED_CONFIG_TTL_SECONDS)
            self._automation_cache = cache
        return cache

    #: Historical name; the store is shared with scripts now.
    _automation_config_cache = _stored_config_cache

    def _fetch_automation_config(self, entity_id: str) -> tuple[dict | None, str]:
        """The stored config for an automation entity, or why it is unreadable.

        Two reads: the entity's state (for its ``id`` attribute, which is the
        only link from ``automation.<slug>`` to the editor store) and then
        ``config/automation/config/<id>`` — the same endpoint
        :meth:`create_automation` writes to.

        Every failure path returns ``(None, reason)`` and the caller turns that
        into a refusal. A YAML-defined automation with no ``id:``, an editor API
        that is disabled, an automation that predates the store, a transport
        error — all of them mean *this appliance does not know what firing this
        would do*, and that is not the same as knowing it is harmless.
        """
        cache = self._stored_config_cache()
        try:
            state = self.get_entity_state(entity_id) or {}
        except HABridgeError as exc:
            # Our own message (HA unreachable, integration disabled, bad id) —
            # worth surfacing verbatim, since it says what to fix.
            return None, f"its state could not be read ({str(exc)[:120]})"
        except Exception as exc:
            return None, f"its state could not be read ({type(exc).__name__})"

        auto_id = (state.get("attributes") or {}).get("id")
        if isinstance(auto_id, int):
            auto_id = str(auto_id)
        if not isinstance(auto_id, str) or not auto_id.strip():
            return None, ("it has no editor id, so it is defined in YAML and "
                          "its actions are not exposed by the API")
        auto_id = auto_id.strip()
        if not self._AUTOMATION_ID_RE.match(auto_id):
            return None, "its editor id is not a shape this appliance will fetch"

        key = f"automation:{auto_id}"
        cached = cache.get(key)
        if isinstance(cached, dict):
            return cached, ""

        try:
            config = self._request(
                f"config/automation/config/{quote_segment(auto_id)}").json()
        except HABridgeError as exc:
            return None, f"the automation editor API did not return it ({exc})"
        except Exception as exc:
            return None, (f"its stored config could not be parsed "
                          f"({type(exc).__name__})")

        if not isinstance(config, dict):
            return None, "the automation editor API returned no config object"
        cache.set(key, config)
        return config, ""

    def automation_actuation_domains(
        self, entity_id: str,
    ) -> tuple[set[str], bool, str]:
        """Domains firing this automation would reach, and whether we can tell.

        Returns ``(domains, known, detail)``. ``detail`` is a whole clause ready
        to hand :func:`enforce_confirmation`, and it distinguishes the two ways
        this can fail closed — the config could not be *fetched* at all, versus
        the config was read but its actions are not fully determinable — because
        those need different fixes from whoever reads the refusal.
        """
        domains = touched_domains(None, entity_id)  # {"automation"}
        config, why = self._fetch_automation_config(entity_id)
        if config is None:
            logger.warning("automation %s: cannot inspect actions (%s); "
                           "treating as always-confirm", entity_id, why)
            return domains, False, (
                f"cannot inspect this automation's actions — {why}")

        found, known, reason = automation_action_domains(config)
        domains |= found
        if not known:
            logger.warning("automation %s: actions not fully determinable (%s); "
                           "treating as always-confirm", entity_id, reason)
            return domains, False, (
                f"cannot fully determine this automation's actions — {reason}")
        return domains, True, ""

    # -- what a script would actually DO --------------------------------------

    def _fetch_script_config(self, entity_id: str) -> tuple[dict | None, str]:
        """The stored config for a script entity, or why it is unreadable.

        One read, not two: unlike an automation, a script has no ``id``
        attribute. HA keys ``scripts.yaml`` — and therefore
        ``config/script/config/<key>`` — by the script's **object_id**, the part
        of the entity id after the dot, validated by HA as a slug. So the key is
        derivable without touching the state machine at all, which also means a
        script that exists only in the editor store and has no live state is
        still inspectable.

        Every failure path returns ``(None, reason)`` and the caller turns that
        into a refusal. A script defined inline in ``configuration.yaml`` rather
        than in ``scripts.yaml`` 404s here, an editor API that is disabled
        errors here, a transport failure errors here — all of them mean *this
        appliance does not know what running this would do*, and that is not the
        same as knowing it is harmless.
        """
        if not is_valid_entity_id(entity_id) or "." not in str(entity_id):
            return None, "its entity id is not a shape this appliance will fetch"
        object_id = str(entity_id).split(".", 1)[1]
        if not self._SCRIPT_KEY_RE.match(object_id):
            return None, ("its object id is not the slug shape HA uses as the "
                          "script editor key")

        cache = self._stored_config_cache()
        key = f"script:{object_id}"
        cached = cache.get(key)
        if isinstance(cached, dict):
            return cached, ""

        try:
            config = self._request(
                f"config/script/config/{quote_segment(object_id)}").json()
        except HABridgeError as exc:
            return None, f"the script editor API did not return it ({exc})"
        except Exception as exc:
            return None, (f"its stored config could not be parsed "
                          f"({type(exc).__name__})")

        if not isinstance(config, dict):
            return None, "the script editor API returned no config object"
        cache.set(key, config)
        return config, ""

    def script_actuation_domains(
        self, entity_id: str,
    ) -> tuple[set[str], bool, str]:
        """Domains running this script would reach, and whether we can tell.

        The exact counterpart of :meth:`automation_actuation_domains`, and it
        exists for the same reason: ``script`` is not in
        :data:`SAFETY_CRITICAL_DOMAINS`, so without this a script whose sequence
        contains ``camera.turn_off`` sails through every gate — the domain the
        instruction *lives in* is harmless, the instruction is not.

        Returns ``(domains, known, detail)``, with *detail* distinguishing "the
        config could not be fetched" from "the config was read but its steps are
        not fully determinable", because those need different fixes from
        whoever reads the refusal.
        """
        domains = touched_domains(None, entity_id)  # {"script"}
        config, why = self._fetch_script_config(entity_id)
        if config is None:
            logger.warning("script %s: cannot inspect steps (%s); treating as "
                           "always-confirm", entity_id, why)
            return domains, False, (
                f"cannot inspect this script's steps — {why}")

        found, known, reason = script_sequence_domains(config)
        domains |= found
        if not known:
            logger.warning("script %s: steps not fully determinable (%s); "
                           "treating as always-confirm", entity_id, reason)
            return domains, False, (
                f"cannot fully determine this script's steps — {reason}")
        return domains, True, ""

    def trigger_automation(self, entity_id: str, *,
                           human_confirmed: Any = False) -> dict[str, Any]:
        """Trigger an automation manually.

        **Gated on what the automation would do, not on the domain it lives in.**
        ``automation`` is not in :data:`SAFETY_CRITICAL_DOMAINS`, so the gate in
        :meth:`call_service` sees ``automation.trigger`` and waves it through —
        which was a hole exactly as wide as :meth:`create_automation` was before
        it was gated. Creating an automation that turns a camera off is already
        refused; firing one that already exists is the same actuation with the
        instruction written earlier, so it is refused on the same terms.

        An automation whose actions cannot be read is refused too. "I could not
        find out what this would do" is not "it does nothing dangerous".
        """
        validate_entity_id(entity_id)
        domains, known, detail = self.automation_actuation_domains(entity_id)
        enforce_confirmation(
            domains, service="automation.trigger", entity_id=entity_id,
            confirmation=human_confirmed, force=not known, detail=detail)

        self.call_service("automation", "trigger", entity_id=entity_id,
                          human_confirmed=human_confirmed)
        return {"success": True, "entity_id": entity_id, "action": "triggered"}

    def toggle_automation(self, entity_id: str, enable: bool, *,
                          human_confirmed: Any = False) -> dict[str, Any]:
        """Enable or disable an automation.

        Gated on the *same* rule as :meth:`trigger_automation`: confirmation is
        required when the automation's actions touch the always-confirm class,
        or when they cannot be determined. Both directions, for different
        reasons:

        * **disabling** an automation whose actions are safety-critical is
          itself a security action — silently switching off the routine that
          closes the garage at night, or the one that re-arms a camera, leaves
          no trace in any camera or lock state, and nothing else in the system
          notices the safeguard is gone;
        * **enabling** one arms a stored instruction to actuate that class,
          which is the same thing :meth:`create_automation` is gated for.

        Deliberately *not* gating every automation on/off: a light-schedule
        automation is not a security event, and a gate that fires on everything
        gets clicked through, which is how a confirmation stops meaning
        anything. The line is drawn at the actions, same as everywhere else.

        "enable" calls ``turn_on`` / ``turn_off``, which are idempotent and
        mean what the argument says, never ``automation.toggle``, which *flips*
        state (asking to enable an already-enabled automation would disable
        it).
        """
        validate_entity_id(entity_id)
        domains, known, detail = self.automation_actuation_domains(entity_id)
        verb = "enable" if enable else "disable"
        enforce_confirmation(
            domains, service=f"automation.{verb}", entity_id=entity_id,
            confirmation=human_confirmed, force=not known, detail=detail)

        self.call_service("automation", "turn_on" if enable else "turn_off",
                          entity_id=entity_id, human_confirmed=human_confirmed)
        return {"success": True, "entity_id": entity_id, "enabled": enable}

    @staticmethod
    def _automation_domains(config: Any) -> set[str]:
        """Every HA domain named anywhere in an automation config.

        Deliberately crude: it walks the whole structure and treats *any* string
        leaf shaped like ``<domain>.<object>`` as naming that domain, so it sees
        ``service: camera.turn_off``, ``entity_id: cover.garage_door``, a target
        block, and a domain named inside a template string alike. Over-matching
        here costs a confirmation prompt; under-matching costs the gate.
        """
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, str):
                for token in re.findall(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", node):
                    found.add(token.split(".", 1)[0])
            elif isinstance(node, dict):
                for key, value in node.items():
                    walk(key)
                    walk(value)
            elif isinstance(node, (list, tuple, set)):
                for item in node:
                    walk(item)

        walk(config)
        return found

    def create_automation(self, config: dict[str, Any], *,
                          human_confirmed: Any = False) -> dict[str, Any]:
        """Create a new automation via the HA config API.

        ``config`` should follow the HA automation schema:
        {
            "alias": "My Automation",
            "description": "...",
            "trigger": [...],
            "condition": [...],
            "action": [...]
        }

        HA requires POST to config/automation/config/<unique_id>.

        **Gated, because this is deferred actuation.** It POSTs straight to the
        config API rather than through :meth:`call_service`, so the choke point
        above does not see it — yet an automation whose action is
        ``camera.turn_off`` plus a trigger that fires immediately (or a follow-up
        :meth:`trigger_automation`, which only ever looks like the benign
        ``automation`` domain) actuates the always-confirm class with no human
        anywhere in the loop. Writing the instruction is gated on the same terms
        as performing it.
        """
        import uuid

        enforce_confirmation(
            self._automation_domains(config),
            service="config/automation (deferred actuation)",
            entity_id=config.get("alias") if isinstance(config, dict) else None,
            confirmation=human_confirmed,
            detail="an automation naming a safety-critical domain is a stored "
                   "instruction to actuate it later")

        auto_id = f"puffpai_{uuid.uuid4().hex[:12]}"
        resp = self._request(
            f"config/automation/config/{auto_id}", method="POST", data=config
        )
        self._cache.invalidate("states")
        # A write to the editor store makes every cached config potentially
        # stale. Cheap to drop; the TTL is 15 s anyway.
        self._stored_config_cache().invalidate()
        try:
            return {"success": True, "id": auto_id, **resp.json()}
        except Exception:
            return {"success": True, "id": auto_id, "message": "Automation created"}

    # -- scenes & scripts -----------------------------------------------------

    def get_scenes(self) -> list[DeviceInfo]:
        """Return all scene.* and script.* entities."""
        states = self.get_all_states()
        area_map = self._get_area_map()
        return [
            self._parse_device(raw, area_map)
            for raw in states
            if raw.get("entity_id", "").startswith(("scene.", "script."))
        ]

    def activate_scene(self, entity_id: str, *,
                       human_confirmed: Any = False) -> dict[str, Any]:
        """Activate a scene or script.

        Two things make this more than a ``call_service`` wrapper:

        1. It derives the service domain from the entity id and calls
           ``<domain>.turn_on``, so it will cheerfully "activate"
           ``camera.hall`` — an actuation dressed up as a scene. The gate in
           :meth:`call_service` catches that.
        2. A scene *and a script* are wrappers: ``scene.goodnight`` is not
           itself safety-critical but may contain ``cover.garage_door``, and
           ``script.goodnight`` may *run* ``camera.turn_off``. Both need
           expanding before the call, which is what
           :func:`stored_instruction_domains` does — a scene whose members
           cannot be read and a script whose steps cannot be read both fail
           closed rather than being assumed harmless.

        The script half was the hole this method had left open longest: this
        endpoint accepts ``script.*`` by design (see :meth:`get_scenes`), and
        :func:`scene_member_domains` returns early for a non-scene with
        ``known=True``, so a script arrived here already classified as the
        harmless ``script`` domain and went straight out to the wire.
        """
        validate_entity_id(entity_id)
        domains, known, detail = stored_instruction_domains(self, entity_id)
        enforce_confirmation(
            domains, service=f"{entity_domain(entity_id) or 'scene'}.turn_on",
            entity_id=entity_id, confirmation=human_confirmed,
            force=not known, detail=detail)

        domain = entity_id.split(".")[0]
        self.call_service(domain, "turn_on", entity_id=entity_id,
                          human_confirmed=human_confirmed)
        return {"success": True, "entity_id": entity_id, "action": "activated"}

    # -- history --------------------------------------------------------------

    def get_history(
        self,
        entity_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[Any]:
        """Get state history for an entity."""
        validate_entity_id(entity_id)
        params = (f"filter_entity_id={quote_segment(entity_id)}"
                  "&minimal_response&no_attributes")
        endpoint = "history/period"
        if start:
            validate_timestamp(start, kind="start time")
            endpoint = f"history/period/{quote(start, safe=':+')}"
        if end:
            validate_timestamp(end, kind="end time")
            params += f"&end_time={quote_segment(end)}"

        resp = self._request(f"{endpoint}?{params}")
        return resp.json()

    # -- camera ---------------------------------------------------------------

    def get_camera_image(self, entity_id: str) -> tuple[bytes, str]:
        """Return (image_bytes, content_type) for a camera entity."""
        validate_entity_id(entity_id)
        resp = self._request(f"camera_proxy/{quote_segment(entity_id)}",
                             stream=True)
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        return resp.content, content_type

    # -- automation (Node-RED) webhook helpers --------------------------------

    def trigger_automation_workflow(self, webhook_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Trigger an automation flow by webhook name via the PuffPAI client."""
        try:
            from puffpai.automation_client import AutomationClient
            client = AutomationClient()
            ok, data = client.call(webhook_name, payload or {})
            return {"success": ok, "data": data}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: HABridge | None = None
_lock = threading.Lock()


def get_bridge(*, _force_reload: bool = False) -> HABridge:
    """Return the global HABridge singleton."""
    global _instance
    if _instance is None or _force_reload:
        with _lock:
            if _instance is None or _force_reload:
                _instance = HABridge()
    return _instance
