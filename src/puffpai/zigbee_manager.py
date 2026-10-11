"""
PuffPAI Zigbee Manager
======================
Hardware-aware Zigbee onboarding for Home Assistant.

Responsibilities:
  1. Detect USB hardware plugged into the HA host (Pi) via HA's WebSocket
     ``usb/scan`` and the supervisor ``/hardware/info`` endpoints.
  2. Identify known Zigbee coordinators by VID/PID/product string.
  3. Drive HA's ZHA config-flow over REST to install the integration.
  4. Open a permit-join window so Zigbee end-devices (bulbs, switches) can pair.

Why a separate module:
  - ``ha_bridge`` is a thin REST wrapper. Config-flow is multi-step stateful.
  - USB discovery needs the WebSocket API, not REST.
  - Keeping the logic isolated keeps ha_bridge simple and testable.

Why this module still has to obey the bridge's gate
---------------------------------------------------
"Separate module" was read as "separate policy", and it was not. Two calls here
mutated the house while holding the HA token directly, so neither passed
:meth:`puffpai.ha_bridge.HABridge.call_service` — the choke point where the
always-confirm gate lives:

* :meth:`ZigbeeManager.open_permit_join` POSTed ``services/zha/permit``. Opening
  a pairing window means **any Zigbee device within radio range may join the
  network** for the duration. That is a physical-access event, not a state
  change, and it was reachable with no confirmation at all from an LLM tool
  (``assistant_tools.ha_pair_zigbee_devices``) whose argument is model-written text.
  That tool has since been retired; pairing now starts in
  :mod:`puffpai.zigbee_pairing`, which files a consent request and opens
  nothing itself.
* :meth:`ZigbeeManager.install_zha` POSTs the config-entry flow, which claims a
  USB serial device and forms a Zigbee network. The dongle match can be
  ``confidence="low"`` (the CP2102 VID/PID is shared with many non-Zigbee
  adapters), so an unconfirmed install can seize an unrelated serial device.

Both now refuse without a human confirmation, using the *same* policy function
and the *same* exception as every other actuation, so there is one policy in the
appliance rather than one per module:

* the verdict comes from :func:`puffpai.ha_bridge.enforce_confirmation`;
* the refusal is :class:`puffpai.ha_bridge.ConfirmationRequired`, which the API
  server already renders as a 409 with the proposal verbatim;
* the wire call for ``zha.permit`` goes through ``HABridge.call_service``, so
  it also inherits the choke point and anything added there later.

When HA answers 401: the admin-defined script fallback
------------------------------------------------------
``zha.permit`` is an **admin-only** service in Home Assistant, and this
appliance's HA user is deliberately not an administrator (least
privilege), so the direct call comes back 401 and no window ever opens. HA's
own sanctioned pattern for this is for the administrator to define a *script*
whose action and duration are fixed, and let the restricted user trigger it.
:meth:`ZigbeeManager._permit_via_script` does exactly that and nothing more:
only on a 401/403, only on the entity named in ``ha.zigbee_permit_script`` /
``ha.zigbee_permit_close_script``, only when that entity actually exists in
HA's state list, and — for the opening direction — only behind the same
:data:`~puffpai.ha_bridge.HUMAN_CONFIRMED` proof and the same
``call_service`` choke point. The window's length stops being PuffPAI's
argument and becomes the script's fixed value, which is a *narrowing*: no
caller, and no model, can choose it any more.

``zha`` is deliberately **not** claimed to be a safety-critical *domain*. The
risk is not "a zha entity changed state", it is "the network accepted a joiner",
which is a property of one service and one argument. So the gate is applied with
``force=True`` — refuse regardless of domain classification — and the *closing*
direction is exempt (see :meth:`ZigbeeManager.close_permit_join`): reducing
exposure never needs permission.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
import websocket  # websocket-client

from puffpai.config import get_config
from puffpai.ha_bridge import (
    ConfirmationRequired,
    HABridge,
    HABridgeError,
    enforce_confirmation,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pairing-window policy
# ---------------------------------------------------------------------------

#: The HA service that both opens and closes the Zigbee pairing window.
PERMIT_DOMAIN = "zha"
PERMIT_SERVICE = "permit"

#: What the refusal proposal calls this action. Same ``domain.service`` spelling
#: the ``/ha/confirm`` ticket flow uses for ``kind="service"``, so a ticket
#: minted against this string would bind to the right thing.
PERMIT_ACTION = f"{PERMIT_DOMAIN}.{PERMIT_SERVICE}"

#: HA's own schema for ``zha.permit``: ``duration`` is 0–254 seconds. ``0`` is
#: not "a very short window" — it is the **close**, and it is how a window
#: already open is ended early. There is no separate ``zha.close`` service, so
#: the open/close distinction lives in the argument and therefore has to be made
#: here rather than in a domain- or service-keyed table.
PERMIT_DURATION_MIN = 0
PERMIT_DURATION_MAX = 254

#: ``zha.permit`` names no entity — HA routes it to the coordinator. The refusal
#: proposal still needs a non-empty target string: ``POST /ha/confirm`` binds a
#: ticket to one ``(kind, entity_id, action)`` triple and rejects a null
#: ``entity_id``. This synthetic id names the thing actually being opened, so a
#: person reads "permit join on the pairing window", not "on nothing".
PAIRING_WINDOW_TARGET = "zha.pairing_window"

#: The sanctioned way a NON-ADMIN HA user reaches an admin-only service: the
#: administrator defines a script whose action is fixed (``zha.permit`` with a
#: fixed ``duration``) and the restricted user only gets to *trigger* it. This
#: appliance's HA user is deliberately not an administrator
#: (least-privilege hardening), so ``zha.permit`` comes back 401 and the window
#: never opens. The fallback below triggers the owner's script instead. It does
#: not widen anything: the admin chose the action and the duration, PuffPAI can
#: only pull the handle, and it can only pull it on the far side of the same
#: consent + HUMAN_CONFIRMED gate that guards ``zha.permit`` itself.
SCRIPT_DOMAIN = "script"
SCRIPT_TURN_ON = "turn_on"
SCRIPT_ACTION = f"{SCRIPT_DOMAIN}.{SCRIPT_TURN_ON}"

#: Where the window's length came from. ``zha.permit`` means this appliance
#: passed the number; ``ha_script`` means the household's script did, and the
#: appliance cannot change it.
DURATION_SOURCE_SERVICE = PERMIT_ACTION
DURATION_SOURCE_SCRIPT = "ha_script"

#: Fallback for the script window length when config carries nothing usable.
DEFAULT_SCRIPT_WINDOW_SECONDS = 120

#: The read-back that turns "HA said 200" into "the script actually ran".
#: ``script.turn_on`` returning 200 only proves HA *accepted* the call; a
#: script that failed to load answers 200 and does nothing (an "open" window
#: with zero joins). So the open direction polls the script entity's ``last_triggered`` and refuses to
#: claim a window unless it moved. Six reads, half a second apart — about
#: three seconds, well inside the shortest sensible pairing window.
PERMIT_READBACK_ATTEMPTS = 6
PERMIT_READBACK_INTERVAL = 0.5

#: Reason code + owner-facing words for "accepted, but never ran".
SCRIPT_NOT_TRIGGERED = "script_not_triggered"
SCRIPT_NOT_TRIGGERED_MESSAGE = (
    "Home Assistant accepted the request but the PuffPAI Zigbee permit script "
    "never ran — its last-triggered time didn't change. The script probably "
    "didn't load; check Settings → Automations & Scenes → Scripts for "
    "'PuffPAI Zigbee permit', or ask me to re-provision it.")


def is_ha_auth_refusal(error: Any) -> bool:
    """True when HA refused a call for *permission*, not for shape.

    Same reading :func:`puffpai.zigbee_pairing._refusal_message` does on the
    same strings (``"HA returned HTTP 401: 401: Unauthorized"``). Deliberately
    narrow: only an authorisation refusal reroutes to the script, so a 404
    (no ZHA at all) or a transport error still surfaces as itself rather than
    quietly firing a script.
    """
    text = str(error)
    lowered = text.lower()
    return ("401" in text or "403" in text
            or "unauthorized" in lowered or "forbidden" in lowered)


def permit_script_entity(*, closing: bool) -> str:
    """The configured permit-open / permit-close script entity, or ``""``.

    Read from config at call time and never from an argument: no caller, and
    certainly no model-written text, gets to name the script that runs. ``""``
    (the documented way to disable this) and any unreadable config both mean
    "there is no script", and the caller then refuses rather than guessing.
    """
    key = "zigbee_permit_close_script" if closing else "zigbee_permit_script"
    try:
        value = getattr(get_config().ha, key, "")
    except Exception:  # pragma: no cover - config unreadable
        return ""
    entity = str(value or "").strip()
    if not entity.startswith(f"{SCRIPT_DOMAIN}."):
        # Only a script entity may be triggered this way. A blank value, or a
        # value pointing at some other domain, disables the fallback.
        return ""
    return entity


def permit_script_seconds() -> int:
    """How long the open script's fixed window lasts, for the bounded watch.

    Only ever used to size PuffPAI's *watching*; it cannot lengthen or shorten
    the real window, which is whatever the admin wrote into the script.
    """
    try:
        raw = getattr(get_config().ha, "zigbee_permit_script_seconds",
                      DEFAULT_SCRIPT_WINDOW_SECONDS)
        seconds = int(raw)
    except Exception:
        return DEFAULT_SCRIPT_WINDOW_SECONDS
    if seconds < 1 or seconds > PERMIT_DURATION_MAX:
        return DEFAULT_SCRIPT_WINDOW_SECONDS
    return seconds


#: Installing ZHA is not an HA *service* at all — it POSTs the config-entry
#: flow — so it carries its own action label rather than pretending to be one.
INSTALL_ACTION = "zha.install_integration"

#: An IEEE 802.15.4 EUI-64: eight colon-separated hex octets.
_IEEE_RE = re.compile(r"^(?:[0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}$")


def _echo(value: Any) -> str:
    """A short, bounded rendering of a rejected argument for an error message.

    The value came from outside, so it is truncated and never interpolated at
    full length into a message that ends up in a log or an HTTP body.
    """
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def validate_permit_duration(value: Any) -> int:
    """*value* as a ZHA permit duration in seconds, or raise.

    Strict, and applied **before anything reaches the network** — a malformed
    duration must not cost a request, and must never be silently coerced into a
    window that stays open longer than the caller meant. ``bool`` is rejected
    explicitly because it is an ``int`` subclass and ``True`` would otherwise
    become a one-second pairing window.

    Raises:
        HABridgeError: message starts with ``"invalid "`` so the API server's
            error classifier renders it as a **400**, not a 502.
    """
    if isinstance(value, bool):
        raise HABridgeError(
            f"invalid permit duration: {_echo(value)} "
            f"(a boolean is not a number of seconds)")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and value.strip().isdigit():
        seconds = int(value.strip())
    else:
        raise HABridgeError(
            f"invalid permit duration: {_echo(value)} "
            f"(expected a whole number of seconds)")
    if not PERMIT_DURATION_MIN <= seconds <= PERMIT_DURATION_MAX:
        raise HABridgeError(
            f"invalid permit duration: {seconds} (ZHA accepts "
            f"{PERMIT_DURATION_MIN}-{PERMIT_DURATION_MAX} seconds)")
    return seconds


def validate_ieee(value: Any) -> Optional[str]:
    """*value* as a normalised EUI-64, ``None`` when absent, or raise.

    ``ieee`` picks which router relays the join, so it widens where in the house
    a device can join from. It is checked before the gate and before any
    request; an unrecognised value is refused rather than forwarded to HA.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise HABridgeError(f"invalid ieee address: {_echo(value)}")
    text = value.strip()
    if not text:
        return None
    if _IEEE_RE.match(text) is None:
        raise HABridgeError(
            f"invalid ieee address: {_echo(value)} "
            f"(expected eight colon-separated hex octets)")
    return text.lower()

# ---------------------------------------------------------------------------
# Known Zigbee coordinator USB IDs
# ---------------------------------------------------------------------------
# Source: zigpy/zha HA docs + ITEAD product pages.
# vid/pid are lowercase hex, no 0x prefix, no colons.

@dataclass
class DongleSpec:
    vid: str
    pid: str
    name: str
    radio_type: str          # ZHA radio_type value: znp, ezsp, deconz, zigate
    baudrate: int = 115200
    flow_control: str = "software"   # HA wants 'software' or 'hardware'
    # Substring to confirm via product/description if VID/PID is ambiguous
    product_hints: tuple[str, ...] = ()


KNOWN_DONGLES: list[DongleSpec] = [
    # Sonoff Zigbee 3.0 USB Dongle Plus — CC2652P (ZNP)
    DongleSpec("10c4", "ea60", "Sonoff ZBDongle-P (CC2652P)", "znp",
               baudrate=115200, product_hints=("sonoff zigbee", "zbdongle-p", "cc2652")),
    # Sonoff Zigbee 3.0 USB Dongle Plus-E — EFR32MG21 (EZSP)
    DongleSpec("1a86", "55d4", "Sonoff ZBDongle-E (EFR32MG21)", "ezsp",
               baudrate=115200, product_hints=("sonoff zigbee", "zbdongle-e", "efr32")),
    # dresden elektronik ConBee II
    DongleSpec("1cf1", "0030", "ConBee II", "deconz",
               baudrate=38400),
    # Nortek HUSBZB-1 (combo Z-Wave + Zigbee EmberZNet)
    DongleSpec("10c4", "8a2a", "HUSBZB-1", "ezsp",
               baudrate=57600),
    # Texas Instruments LAUNCHXL / CC2531 generic
    DongleSpec("0451", "16a8", "TI CC2531", "znp",
               baudrate=115200),
]

# VID ea60 under 10c4 (CP2102 bridge) is ambiguous — many non-Zigbee devices
# use CP2102. Require a product hint match OR an explicit override.
_AMBIGUOUS = {("10c4", "ea60")}


# ---------------------------------------------------------------------------
# Detection results
# ---------------------------------------------------------------------------

@dataclass
class DetectedDevice:
    """A USB device observed on the HA host."""
    vid: str
    pid: str
    serial_number: Optional[str]
    manufacturer: Optional[str]
    description: Optional[str]
    device_path: Optional[str]       # /dev/ttyUSB0 or /dev/serial/by-id/...
    subsystem: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_usb_serial(self) -> bool:
        return (self.device_path or "").startswith("/dev/tty") or \
               (self.device_path or "").startswith("/dev/serial/")


@dataclass
class ZigbeeMatch:
    """A detected device matched to a known Zigbee dongle spec."""
    device: DetectedDevice
    spec: DongleSpec
    confidence: str   # "high" (vid/pid + hint), "medium" (vid/pid only), "low"

    def summary(self) -> str:
        return f"{self.spec.name} on {self.device.device_path or '(unknown path)'}"


# ---------------------------------------------------------------------------
# WebSocket helper
# ---------------------------------------------------------------------------

class _HAWebSocket:
    """Thin context-manager around HA's WebSocket API for multi-message calls."""

    def __init__(self, url: str, token: str, timeout: float = 10.0):
        self._ws_url = url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        self._token = token
        self._timeout = timeout
        self._ws: Optional[websocket.WebSocket] = None
        self._next_id = 1

    def __enter__(self):
        self._ws = websocket.create_connection(self._ws_url, timeout=self._timeout)
        hello = json.loads(self._ws.recv())
        if hello.get("type") != "auth_required":
            raise ZigbeeManagerError(f"Unexpected WS greeting: {hello}")
        self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_resp = json.loads(self._ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise ZigbeeManagerError(f"HA WS auth failed: {auth_resp}")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        return False

    def call(self, type_: str, **extra) -> dict:
        assert self._ws is not None
        msg_id = self._next_id
        self._next_id += 1
        payload = {"id": msg_id, "type": type_, **extra}
        self._ws.send(json.dumps(payload))
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") == msg_id:
                return resp


# ---------------------------------------------------------------------------
# Main manager
# ---------------------------------------------------------------------------

class ZigbeeManager:
    """Coordinates hardware discovery, ZHA install, and permit-join."""

    #: Injectable only so tests do not really wait ~3 s; production never
    #: passes anything else.
    _readback_sleep = staticmethod(time.sleep)

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None,
                 *, bridge: Optional[Any] = None):
        cfg = get_config()
        self._url = (url or cfg.ha.url).rstrip("/")
        # One token source for the whole tree (see
        # security/ha_token_source.py), never cfg.ha.token directly.
        from puffpai.ha_bridge import _resolve_ha_token
        self._token = token or _resolve_ha_token() or ""
        self._rest = requests.Session()
        self._rest.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        })
        # Lazily built: constructing an HABridge reads config and opens a
        # session, and most of this class (discovery, matching) never needs one.
        self._bridge = bridge

    # -- the gate -------------------------------------------------------------

    def _bridge_for_service_calls(self) -> Any:
        """The :class:`HABridge` this manager actuates through.

        Actuating service calls go through the bridge rather than this class's
        own ``_rest`` session so they pass ``call_service`` — the choke point —
        and inherit whatever the gate there grows to cover.
        """
        if self._bridge is None:
            self._bridge = HABridge(url=self._url, token=self._token)
        return self._bridge

    @staticmethod
    def _gate(*, service: str, entity_id: Any, confirmation: Any,
              detail: str) -> None:
        """Refuse a Zigbee actuation that carries no human confirmation.

        The verdict is :func:`~puffpai.ha_bridge.enforce_confirmation`, the same
        function :class:`~puffpai.ha_bridge.HABridge` and the Steward call, so
        this cannot drift from the rest of the appliance. Only the reason text
        is Zigbee-specific.

        ``force=True`` because the risk here is not carried by a domain. Nothing
        in ``SAFETY_CRITICAL_DOMAINS`` describes "the network will accept a
        joiner" — that is a property of one service and one argument value — so
        a domain-keyed lookup would pass this call straight through. ``force``
        is exactly the "refuse regardless of *domains*" lever the shared policy
        already exposes, and a real :data:`~puffpai.ha_bridge.HUMAN_CONFIRMED`
        token still lifts it, so the semantics match every other gated call.

        ``{PERMIT_DOMAIN}`` is still passed as the domain set: it is honest
        about what is touched, and if ``zha`` is ever added to
        ``SAFETY_CRITICAL_DOMAINS`` the proposal starts naming it with no change
        here.

        Raises:
            ConfirmationRequired: with ``proposal = {service, entity_id,
                domains}`` — the shape ``_ha_error_response`` already renders as
                a 409, so no new envelope is introduced.
        """
        enforce_confirmation(
            {PERMIT_DOMAIN},
            service=service,
            entity_id=entity_id,
            confirmation=confirmation,
            force=True,
            detail=detail,
        )

    # -- the admin-defined script fallback ------------------------------------

    @staticmethod
    def _script_entity_exists(bridge: Any, entity_id: str) -> bool:
        """Is *entity_id* actually in HA's state list right now?

        Checked before the call, not after, so a missing or misspelt script is
        a refusal this appliance can explain rather than an opaque HA error —
        and so PuffPAI never POSTs a service call naming an entity that does
        not exist. Reads only; every failure is ``False`` (fail closed: an
        unreadable state list is not proof the script is there).
        """
        reader = getattr(bridge, "get_all_states", None)
        if callable(reader):
            try:
                for raw in reader() or []:
                    if isinstance(raw, dict) and raw.get("entity_id") == entity_id:
                        return True
            except Exception as exc:
                logger.warning("zigbee: could not read HA states to check %s "
                               "(%s); not calling it", entity_id, exc)
                return False
            return False
        # A reader that only does single-entity lookups (a Steward proxy, a
        # test double). Same fail-closed rule.
        for name in ("get_entity_state", "get_state"):
            single = getattr(bridge, name, None)
            if callable(single):
                try:
                    state = single(entity_id)
                except Exception:
                    return False
                return bool(state)
        return False

    @staticmethod
    def _script_state(bridge: Any, entity_id: str) -> dict:
        """The script entity's raw HA state, or ``{}`` if it can't be read.

        Uses the same everyday (non-admin) bridge the service call goes
        through — ``GET /api/states/<entity>`` — so the read-back needs no
        extra credential and no second HTTP client. Prefers the single-entity
        getter because the all-states reader is cached for 5 s, which is
        longer than this poll and would hand back a pre-call snapshot.
        """
        single = getattr(bridge, "get_entity_state", None) or \
            getattr(bridge, "get_state", None)
        if callable(single):
            try:
                state = single(entity_id)
            except Exception as exc:
                logger.debug("zigbee: could not read %s: %s", entity_id, exc)
                return {}
            return state if isinstance(state, dict) else {}
        reader = getattr(bridge, "get_all_states", None)
        if not callable(reader):
            return {}
        try:
            try:
                states = reader(force=True)
            except TypeError:
                states = reader()
            for raw in states or []:
                if isinstance(raw, dict) and raw.get("entity_id") == entity_id:
                    return raw
        except Exception as exc:
            logger.debug("zigbee: could not read %s: %s", entity_id, exc)
        return {}

    @classmethod
    def _script_trigger_stamp(cls, bridge: Any, entity_id: str) -> Optional[str]:
        """``attributes.last_triggered`` for *entity_id*, or ``None``."""
        attrs = (cls._script_state(bridge, entity_id) or {}).get("attributes")
        stamp = (attrs or {}).get("last_triggered") if isinstance(attrs, dict) \
            else None
        return str(stamp) if stamp else None

    @staticmethod
    def _script_is_running(state: dict) -> bool:
        """Did the script start, as seen mid-run?

        A short script can finish between the call and the first read, in
        which case only ``last_triggered`` proves it. A long one is caught
        here instead: HA sets ``state == "on"`` and ``attributes.current``
        (the number of running instances) while it runs.
        """
        if str(state.get("state") or "").lower() == "on":
            return True
        attrs = state.get("attributes")
        if isinstance(attrs, dict):
            try:
                return int(attrs.get("current") or 0) >= 1
            except (TypeError, ValueError):
                return False
        return False

    def _await_script_trigger(
        self, bridge: Any, entity_id: str, before: Optional[str],
    ) -> tuple[bool, Optional[str], bool]:
        """Poll *entity_id* until it proves it ran. Fail closed.

        Returns ``(ran, last_triggered, any_readable)``. ``ran`` is True when
        ``last_triggered`` moved off its pre-call value (HA only ever moves it
        forward, so "different and non-null" is "newer"), or when the script
        is caught mid-run. ``any_readable`` is True as soon as any attempt
        returns a real state — a readable entity that simply never advances
        is still proof of nothing, and that case fails closed exactly as
        before. Only when *every* attempt comes back unreadable (exception,
        ``None``, or otherwise no state at all) does the caller get to treat
        this as "we could not check" rather than "we checked and it failed".
        """
        any_readable = False
        for attempt in range(PERMIT_READBACK_ATTEMPTS):
            state = self._script_state(bridge, entity_id)
            if isinstance(state, dict) and state:
                any_readable = True
            attrs = state.get("attributes") if isinstance(state, dict) else None
            stamp = (attrs or {}).get("last_triggered") \
                if isinstance(attrs, dict) else None
            stamp = str(stamp) if stamp else None
            if stamp and stamp != before:
                return True, stamp, True
            if self._script_is_running(state):
                return True, stamp, True
            if attempt < PERMIT_READBACK_ATTEMPTS - 1:
                self._readback_sleep(PERMIT_READBACK_INTERVAL)
        return False, before, any_readable

    def _permit_via_script(self, bridge: Any, *, closing: bool,
                           human_confirmed: Any, error: str) -> Optional[dict]:
        """Retry a permission-refused ``zha.permit`` as the owner's HA script.

        Returns the ``open_permit_join`` result dict on success, or ``None``
        when this route is not available — in which case the caller returns the
        original HA error and its existing plain-words refusal. There is no
        third path: this never widens into "try something else".

        The open direction is gated **again** here, on ``script.turn_on`` and
        the script's own entity id, even though :meth:`open_permit_join`
        already refused an unconfirmed open before any network call. The second
        verdict costs nothing on the confirmed path and means the fallback
        cannot be reached without the token by any future caller that finds its
        way here, with a proposal that names the script a person would have to
        approve. The close direction stays ungated, exactly as
        :meth:`close_permit_join` is: shortening exposure never needs
        permission.
        """
        if not is_ha_auth_refusal(error):
            return None
        entity = permit_script_entity(closing=closing)
        if not entity:
            return None
        if not self._script_entity_exists(bridge, entity):
            logger.warning("zigbee: HA refused %s (%s) and the configured "
                           "%s script %s is not in HA's state list",
                           PERMIT_ACTION, error,
                           "close" if closing else "permit", entity)
            return None

        if not closing:
            self._gate(
                service=SCRIPT_ACTION,
                entity_id=entity,
                confirmation=human_confirmed,
                detail=(
                    f"Home Assistant refuses {PERMIT_ACTION} to this "
                    f"non-administrator user, so opening the pairing window "
                    f"means triggering the household's {entity}, whose window "
                    f"length is fixed by whoever wrote the script — that is "
                    f"still a window any Zigbee device in range may join "
                    f"through, so it needs a person"),
            )

        service_data: dict[str, Any] = {}
        # Same rule as the ``zha.permit`` call above: the token is handed down
        # only to a bridge that declares it enforces, and only for the gated
        # direction. The close presents no confirmation of any kind.
        if not closing and getattr(bridge, "ENFORCES_CONFIRMATION", False):
            service_data["human_confirmed"] = human_confirmed

        # Read the trigger stamp BEFORE the call, so "it moved" is a fact about
        # this call and not about some earlier run of the same script.
        before = None if closing else self._script_trigger_stamp(bridge, entity)

        try:
            bridge.call_service(SCRIPT_DOMAIN, SCRIPT_TURN_ON, entity,
                                **service_data)
        except ConfirmationRequired:
            if closing:
                # The bridge could not read the script's steps and refused.
                # Do not launder a confirmation nobody gave: leave the window
                # to its own timeout, which the script itself bounds.
                logger.warning("zigbee: the close script %s was refused by the "
                               "confirmation gate; the pairing window will "
                               "close on its own timeout", entity)
                return None
            raise
        except HABridgeError as exc:
            logger.warning("zigbee: the %s script %s failed: %s",
                           "close" if closing else "permit", entity, exc)
            return None

        seconds = PERMIT_DURATION_MIN if closing else permit_script_seconds()

        if not closing:
            # HA answering 200 means "call accepted", not "script body ran". A
            # script that failed to load answers 200 and does nothing, and the
            # owner is left power-cycling bulbs at a window that was never
            # open. Nothing is claimed until the entity says it ran.
            ran, last_triggered, any_readable = self._await_script_trigger(
                bridge, entity, before)
            if not ran and not any_readable:
                # Every read failed outright — this is not proof the script
                # didn't run, it's an inability to check at all. Fall back to
                # the earlier behaviour (open the window, start the
                # watcher) rather than refuse a window that may well be open,
                # but say plainly that it went unverified.
                logger.warning(
                    "zigbee: permit read-back unavailable (%s); proceeding "
                    "unverified", entity)
                logger.info(
                    "zigbee pairing: window opened via HA script %s (%ss, "
                    "fixed by the script); read-back unavailable, proceeding "
                    "unverified", entity, seconds)
                return {"success": True, "duration": seconds, "ieee": None,
                        "closed": False, "error": None,
                        "duration_source": DURATION_SOURCE_SCRIPT,
                        "script_entity": entity,
                        "verified": False, "readback": "unreadable",
                        "last_triggered": last_triggered}
            if not ran:
                logger.warning(
                    "zigbee: HA accepted %s on %s but the script never ran — "
                    "last_triggered before=%s after=%s; not opening a window",
                    SCRIPT_ACTION, entity, before, last_triggered)
                return {"success": False, "duration": seconds, "ieee": None,
                        "closed": False,
                        "error": f"{SCRIPT_NOT_TRIGGERED}: "
                                 f"{SCRIPT_NOT_TRIGGERED_MESSAGE}",
                        "reason": SCRIPT_NOT_TRIGGERED,
                        "duration_source": None, "script_entity": entity,
                        "verified": False, "last_triggered": last_triggered}
            # A moved last_triggered proves HA STARTED the script. It does
            # not prove zha.permit inside it opened the radio, so the log says
            # "started", never "verified".
            logger.info("zigbee pairing: pairing script STARTED via HA script "
                        "%s (%ss, fixed by the script); HA marked it triggered "
                        "%s -> %s; radio state not confirmed", entity, seconds,
                        before, last_triggered)
            return {"success": True, "duration": seconds, "ieee": None,
                    "closed": False, "error": None,
                    "duration_source": DURATION_SOURCE_SCRIPT,
                    "script_entity": entity,
                    "verified": True, "last_triggered": last_triggered}

        logger.info("zigbee pairing: window %s via HA script %s (%ss, fixed by "
                    "the script)", "closed", entity, seconds)
        return {"success": True, "duration": seconds, "ieee": None,
                "closed": closing, "error": None,
                "duration_source": DURATION_SOURCE_SCRIPT,
                "script_entity": entity}

    # -- discovery ------------------------------------------------------------

    def list_usb_devices(self) -> list[DetectedDevice]:
        """Enumerate USB hardware visible to HA.

        Combines two sources:
          - WS ``usb/scan`` — HA's view of integration-matched USB devices
          - WS ``hardware/info`` — raw hardware enumeration
        Duplicate entries (same vid/pid/serial) are merged.
        """
        devices: dict[tuple[str, str, str], DetectedDevice] = {}

        with _HAWebSocket(self._url, self._token) as ws:
            # usb/scan always exists in modern HA
            try:
                r = ws.call("usb/scan")
                for raw in (r.get("result") or []):
                    d = self._parse_usb_scan_entry(raw)
                    if d:
                        key = (d.vid, d.pid, d.serial_number or d.device_path or "")
                        devices[key] = d
            except Exception as exc:
                logger.warning("usb/scan failed: %s", exc)

            # hardware/info sometimes populates additional entries
            try:
                r = ws.call("hardware/info")
                items = (r.get("result") or {}).get("hardware", [])
                for raw in items:
                    d = self._parse_hardware_info_entry(raw)
                    if d:
                        key = (d.vid, d.pid, d.serial_number or d.device_path or "")
                        devices.setdefault(key, d)
            except Exception as exc:
                logger.debug("hardware/info failed: %s", exc)

        return list(devices.values())

    @staticmethod
    def _parse_usb_scan_entry(raw: dict) -> Optional[DetectedDevice]:
        vid = (raw.get("vid") or "").lower().lstrip("0x").zfill(4)
        pid = (raw.get("pid") or "").lower().lstrip("0x").zfill(4)
        if not vid or not pid or vid == "0000":
            return None
        return DetectedDevice(
            vid=vid,
            pid=pid,
            serial_number=raw.get("serial_number"),
            manufacturer=raw.get("manufacturer"),
            description=raw.get("description") or raw.get("name"),
            device_path=raw.get("dev_path") or raw.get("device"),
            subsystem="usb",
            raw=raw,
        )

    @staticmethod
    def _parse_hardware_info_entry(raw: dict) -> Optional[DetectedDevice]:
        attrs = raw.get("attributes", {}) or {}
        vid_raw = attrs.get("ID_VENDOR_ID") or raw.get("vid")
        pid_raw = attrs.get("ID_MODEL_ID") or raw.get("pid")
        if not vid_raw or not pid_raw:
            return None
        vid = str(vid_raw).lower().lstrip("0x").zfill(4)
        pid = str(pid_raw).lower().lstrip("0x").zfill(4)
        return DetectedDevice(
            vid=vid,
            pid=pid,
            serial_number=attrs.get("ID_SERIAL_SHORT"),
            manufacturer=attrs.get("ID_VENDOR") or raw.get("manufacturer"),
            description=attrs.get("ID_MODEL") or raw.get("name"),
            device_path=raw.get("device_path") or raw.get("usb_path"),
            subsystem=raw.get("subsystem") or "usb",
            raw=raw,
        )

    def find_zigbee_dongles(
        self,
        devices: Optional[list[DetectedDevice]] = None,
    ) -> list[ZigbeeMatch]:
        """Match connected USB devices against the known-Zigbee registry."""
        if devices is None:
            devices = self.list_usb_devices()

        matches: list[ZigbeeMatch] = []
        for dev in devices:
            for spec in KNOWN_DONGLES:
                if dev.vid != spec.vid or dev.pid != spec.pid:
                    continue

                # Build the text blob we'll search for product hints
                hint_blob = " ".join(str(x or "") for x in (
                    dev.description, dev.manufacturer, dev.serial_number,
                    dev.raw.get("product"),
                )).lower()

                if (spec.vid, spec.pid) in _AMBIGUOUS:
                    if spec.product_hints and any(h in hint_blob for h in spec.product_hints):
                        matches.append(ZigbeeMatch(dev, spec, "high"))
                    else:
                        # CP2102-class adapter with no Sonoff hint — still a candidate
                        matches.append(ZigbeeMatch(dev, spec, "low"))
                else:
                    confidence = "high" if (
                        spec.product_hints and any(h in hint_blob for h in spec.product_hints)
                    ) else "medium"
                    matches.append(ZigbeeMatch(dev, spec, confidence))
        return matches

    # -- ZHA installation -----------------------------------------------------

    def is_zha_installed(self) -> bool:
        """Return True if a ZHA config entry already exists."""
        try:
            with _HAWebSocket(self._url, self._token) as ws:
                r = ws.call("config_entries/get")
                entries = r.get("result") or []
                return any(e.get("domain") == "zha" for e in entries)
        except Exception as exc:
            logger.warning("is_zha_installed check failed: %s", exc)
            return False

    def install_zha(self, match: ZigbeeMatch, wait_seconds: int = 30, *,
                    human_confirmed: Any = False) -> dict:
        """Drive HA's ZHA config flow to completion for the given dongle.

        Returns ``{"success": bool, "flow_id": str|None, "entry_id": str|None, "step": str, "error": str|None}``.

        **Gated.** This creates a config entry: it claims a USB serial device on
        the HA host and forms a Zigbee network. ``find_zigbee_dongles`` can hand
        back a ``confidence="low"`` match — the CP2102 VID/PID (``10c4:ea60``)
        is shared with many adapters that are not Zigbee coordinators — so an
        unconfirmed install can seize an unrelated serial device (a Z-Wave
        stick, a UPS link, an inverter cable). It is reachable from an LLM tool
        (``assistant_tools.ha_add_zigbee_dongle``), so "the model decided" is not a
        sufficient authority for it.

        The gate runs **before the early-out reads**, so a refused call makes no
        network request at all. ``is_zha_installed()`` is available separately to
        any caller that wants the friendly "already set up" answer without
        proposing an install — ``ha_add_zigbee_dongle`` already calls it first.

        Args:
            human_confirmed: pass :data:`~puffpai.ha_bridge.HUMAN_CONFIRMED` —
                the token object, not ``True``. Keyword-only so it can never be
                filled positionally by a caller splatting external input.

        Raises:
            ConfirmationRequired: when no human confirmation is presented.
        """
        self._gate(
            service=INSTALL_ACTION,
            entity_id=match.device.device_path or match.spec.name,
            confirmation=human_confirmed,
            detail=(
                f"installing ZHA binds {match.spec.name} on "
                f"{match.device.device_path or 'an unidentified port'} "
                f"(match confidence: {match.confidence}) and forms a Zigbee "
                f"network; a wrong match takes over whatever else is on that "
                f"serial port"),
        )

        if self.is_zha_installed():
            return {"success": True, "step": "already_installed", "entry_id": None, "flow_id": None, "error": None}

        if not match.device.device_path:
            return {"success": False, "step": "no_path",
                    "error": f"No device path available for {match.spec.name}", "flow_id": None, "entry_id": None}

        # Step 1 — start the flow
        resp = self._flow_post(None, {"handler": "zha"})
        if not resp:
            return {"success": False, "step": "init", "error": "failed to start ZHA flow",
                    "flow_id": None, "entry_id": None}
        flow_id = resp.get("flow_id")
        if not flow_id:
            return {"success": False, "step": "init", "error": f"no flow_id in: {resp}",
                    "flow_id": None, "entry_id": None}

        try:
            # The flow's first step asks for the radio type (label string).
            # We pass the radio_type by matching against the step's select options.
            resp = self._advance_by_radio_label(flow_id, resp, match.spec.radio_type)
            if resp.get("type") != "form":
                return self._finalize(flow_id, resp)

            # Next step typically asks for the port + baudrate. Sometimes HA
            # also offers a "Pick a USB device" autoscan form — handle either.
            resp = self._advance_port_step(flow_id, resp, match)
            if resp.get("type") != "form":
                return self._finalize(flow_id, resp)

            # Some ZHA radios add a "network formation" choice step
            resp = self._advance_formation_step(flow_id, resp)
            return self._finalize(flow_id, resp)

        except Exception as exc:
            self._abort_flow(flow_id)
            return {"success": False, "step": "exception", "error": str(exc),
                    "flow_id": flow_id, "entry_id": None}

    def _flow_post(self, flow_id: Optional[str], body: dict) -> dict:
        """POST one config-flow step. **Mutating primitive — private.**

        Deliberately not gated itself. A config flow is several POSTs that are
        one decision, so gating each step would demand a fresh confirmation per
        step and make a half-created config entry the normal outcome of saying
        no. The single gate lives at the public entry point,
        :meth:`install_zha`, before the first call lands here.

        The residual: in-process code that calls this directly bypasses the
        gate. That is the same boundary
        :data:`~puffpai.ha_bridge.HUMAN_CONFIRMED` draws everywhere else —
        anything that can call a private method can already call ``requests`` —
        so it is a convention, not a control. Do not add a public caller of this
        that does not gate first.
        """
        if flow_id is None:
            url = f"{self._url}/api/config/config_entries/flow"
        else:
            url = f"{self._url}/api/config/config_entries/flow/{flow_id}"
        resp = self._rest.post(url, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _abort_flow(self, flow_id: str) -> None:
        """Cancel an in-progress config flow. **Deliberately not gated.**

        Same rule as :meth:`close_permit_join`: this only ever *un*-does a
        half-finished install. Requiring a confirmation to clean up would mean a
        refused or crashed install leaves a dangling flow behind, which is a
        worse state than the one it started in.
        """
        try:
            self._rest.delete(
                f"{self._url}/api/config/config_entries/flow/{flow_id}",
                timeout=10,
            )
        except Exception:
            pass

    @staticmethod
    def _pick_label(options: list, radio_type: str) -> Optional[str]:
        """Given a ZHA select's options (list of [value,label]), pick the one
        whose first token matches the radio_type (e.g. 'znp')."""
        want = radio_type.lower()
        mapping = {
            "znp": ("znp", "zstack", "cc2"),
            "ezsp": ("ezsp", "emberznet", "silicon labs", "silabs"),
            "deconz": ("deconz", "dresden"),
            "zigate": ("zigate",),
        }
        needles = mapping.get(want, (want,))
        for option in options:
            if isinstance(option, (list, tuple)) and len(option) >= 2:
                value, label = option[0], option[1]
            else:
                value = label = option
            blob = f"{value} {label}".lower()
            if any(n in blob for n in needles):
                return value
        return None

    def _advance_by_radio_label(self, flow_id: str, form: dict, radio_type: str) -> dict:
        schema = form.get("data_schema") or []
        for field_ in schema:
            if field_.get("type") == "select":
                chosen = self._pick_label(field_.get("options") or [], radio_type)
                if chosen:
                    return self._flow_post(flow_id, {field_.get("name", "radio_type"): chosen})
        # Some HA versions skip this step if only one radio matches discovery
        return form

    def _advance_port_step(self, flow_id: str, form: dict, match: ZigbeeMatch) -> dict:
        schema = form.get("data_schema") or []
        payload: dict[str, Any] = {}

        # Typical field names in ZHA port-step: path, baudrate, flow_control
        for field_ in schema:
            name = field_.get("name")
            if name in ("device", "path", "usb_path"):
                # If it's a select, pick the matching entry
                if field_.get("type") == "select":
                    options = field_.get("options") or []
                    chosen = None
                    target = match.device.device_path or ""
                    for opt in options:
                        if isinstance(opt, (list, tuple)) and len(opt) >= 1:
                            val = opt[0]
                        else:
                            val = opt
                        if target and target in str(val):
                            chosen = val
                            break
                    if chosen is None and options:
                        # Fall back to first "manual" entry if present, else the target path
                        chosen = match.device.device_path
                    payload[name] = chosen
                else:
                    payload[name] = match.device.device_path
            elif name == "baudrate":
                payload[name] = match.spec.baudrate
            elif name == "flow_control":
                payload[name] = match.spec.flow_control
            elif name == "radio_type":
                payload[name] = match.spec.radio_type

        if not payload:
            return form
        return self._flow_post(flow_id, payload)

    def _advance_formation_step(self, flow_id: str, form: dict) -> dict:
        """Most radios ask whether to form a new network or migrate. Default
        to forming a new network for a fresh install."""
        schema = form.get("data_schema") or []
        for field_ in schema:
            if field_.get("name") == "next_step_id" and field_.get("type") == "select":
                options = field_.get("options") or []
                for opt in options:
                    val = opt[0] if isinstance(opt, (list, tuple)) else opt
                    if "form" in str(val).lower() or "new" in str(val).lower():
                        return self._flow_post(flow_id, {"next_step_id": val})
        return form

    def _finalize(self, flow_id: str, resp: dict) -> dict:
        type_ = resp.get("type")
        if type_ == "create_entry":
            return {
                "success": True,
                "step": "create_entry",
                "flow_id": flow_id,
                "entry_id": (resp.get("result") or {}).get("entry_id"),
                "error": None,
            }
        if type_ == "abort":
            return {
                "success": False,
                "step": "abort",
                "flow_id": flow_id,
                "entry_id": None,
                "error": resp.get("reason") or "flow aborted",
            }
        # Still a form we didn't handle — report what's asked for
        return {
            "success": False,
            "step": resp.get("step_id") or "unknown_form",
            "flow_id": flow_id,
            "entry_id": None,
            "error": f"flow stopped at form step; required: "
                     f"{[s.get('name') for s in (resp.get('data_schema') or [])]}",
        }

    # -- permit-join ----------------------------------------------------------

    def open_permit_join(self, duration: int = 60, ieee: Optional[str] = None,
                         *, human_confirmed: Any = False) -> dict:
        """Open — or with ``duration=0``, close — the Zigbee pairing window.

        If ``ieee`` is provided, permit-join is routed through that specific
        Zigbee router/coordinator (useful when bulbs are far from the hub),
        which also moves *where in the house* a device can join from.

        **Opening is gated; closing is not.** An open window admits any Zigbee
        device within radio range for its whole duration, and the appliance
        cannot enumerate in advance who that will be — the risk is physical
        proximity, not an entity changing state. ``duration=0`` is ZHA's close
        (there is no separate stop service), and shortening exposure never needs
        permission, so it runs unconfirmed by design.

        Order of operations, and each step is load-bearing:

        1. validate ``duration`` and ``ieee`` — a malformed argument is refused
           **before any request**, so garbage costs nothing and cannot be
           coerced into a longer window than the caller meant;
        2. gate, when ``duration > 0``, before any read — a refused call makes
           zero network calls of any kind;
        3. only then check that ZHA exists, and actuate.

        The actuation goes through :meth:`~puffpai.ha_bridge.HABridge.call_service`
        rather than this class's own session, so it passes the choke point and
        keeps passing it as that gate grows. Note that ``call_service`` honours
        ``ha.enabled``: with Home Assistant switched off in config this now
        reports a failure instead of reaching past the kill switch, which the
        old direct POST did.

        Args:
            human_confirmed: pass :data:`~puffpai.ha_bridge.HUMAN_CONFIRMED` —
                the token object, not ``True``. Keyword-only, so a caller that
                splats external input cannot fill it positionally, and a decoded
                JSON ``true`` binds a value the token check rejects.

        Returns:
            ``{"success": bool, "duration": int, "ieee": str|None,
            "closed": bool, "error": str|None, "duration_source":
            "zha.permit"|"ha_script"|None, "script_entity": str|None}``.
            ``duration_source == "ha_script"`` means HA refused the direct
            service for permission and the owner's script opened the window
            instead, so ``duration`` is the script's fixed length, not the
            requested one. That path also carries ``verified`` and
            ``last_triggered``: the window counts as open only once the
            script entity's ``last_triggered`` has moved. When it never moves,
            this returns the ordinary failure shape with
            ``reason == SCRIPT_NOT_TRIGGERED`` and no window is claimed.

        Raises:
            HABridgeError: malformed ``duration`` or ``ieee`` (message starts
                ``"invalid "``, so the API renders it 400).
            ConfirmationRequired: opening without a human confirmation. Carries
                ``proposal = {service, entity_id, domains}`` for the existing
                409 handler.
        """
        duration = validate_permit_duration(duration)
        ieee = validate_ieee(ieee)
        closing = duration == PERMIT_DURATION_MIN
        target = ieee or PAIRING_WINDOW_TARGET

        if not closing:
            where = (f"through router {ieee}" if ieee
                     else "on the coordinator")
            self._gate(
                service=PERMIT_ACTION,
                entity_id=target,
                confirmation=human_confirmed,
                detail=(
                    f"this opens the Zigbee pairing window {where} for "
                    f"{duration}s, during which any Zigbee device within radio "
                    f"range may join the network — the joiners cannot be "
                    f"enumerated in advance, so this needs a person, not a "
                    f"caller's say-so"),
            )

        if not self.is_zha_installed():
            return {"success": False, "duration": duration, "ieee": ieee,
                    "closed": closing,
                    "error": "ZHA integration is not installed",
                    "duration_source": None, "script_entity": None}

        service_data: dict[str, Any] = {"duration": duration}
        if ieee:
            service_data["ieee"] = ieee

        bridge = self._bridge_for_service_calls()
        # Hand the token down only to a bridge that declares it applies the gate
        # itself. Same rule the Steward's proxy follows: on an injected double
        # that does not enforce, the keyword would land in ``**kwargs`` and be
        # forwarded to HA as if it were service data. Withholding it weakens
        # nothing — ``_gate`` above has already returned the verdict.
        if getattr(bridge, "ENFORCES_CONFIRMATION", False):
            service_data["human_confirmed"] = human_confirmed

        try:
            bridge.call_service(PERMIT_DOMAIN, PERMIT_SERVICE, None,
                                **service_data)
        except ConfirmationRequired:
            # Checked before HABridgeError, which it subclasses: a refusal from
            # the choke point is the system working and must reach the 409
            # handler intact, not be flattened into an error string here.
            raise
        except HABridgeError as exc:
            # HA refused. If it refused for *permission* — this appliance's HA
            # user is deliberately not an administrator and ``zha.permit`` is
            # an admin-only service — retry through the script the owner's
            # administrator defined for exactly this, and only that. Any other
            # error, or no script, returns the refusal unchanged so the
            # existing plain-words message still reaches the owner.
            fallback = self._permit_via_script(
                bridge, closing=closing, human_confirmed=human_confirmed,
                error=str(exc))
            if fallback is not None:
                return fallback
            return {"success": False, "duration": duration, "ieee": ieee,
                    "closed": closing, "error": str(exc),
                    "duration_source": None, "script_entity": None}

        logger.info("zha.permit: pairing window %s (%ss)%s",
                    "closed" if closing else "opened", duration,
                    f" via {ieee}" if ieee else "")
        return {"success": True, "duration": duration, "ieee": ieee,
                "closed": closing, "error": None,
                "duration_source": DURATION_SOURCE_SERVICE,
                "script_entity": None}

    def close_permit_join(self, ieee: Optional[str] = None) -> dict:
        """Close the Zigbee pairing window now. **Never gated.**

        A named method rather than a bare ``open_permit_join(duration=0)`` so
        that the one ungated actuation in this module is obvious to anyone
        auditing it, and so the reason is written where the exemption is: this
        can only *reduce* exposure. There is no argument to this that opens
        anything, and no confirmation parameter to forget to pass.
        """
        return self.open_permit_join(duration=PERMIT_DURATION_MIN, ieee=ieee)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ZigbeeManagerError(Exception):
    """Raised when ZigbeeManager cannot talk to HA."""
