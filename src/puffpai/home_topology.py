"""
PuffPAI Home Blueprint — one room resolver for the whole appliance
==================================================================

``GET /ha/rooms`` returned a single room called "Unassigned" holding all 182
devices, and the Smart Home page was reduced to guessing rooms out of entity
names. The cause was two lines in :mod:`puffpai.ha_bridge`:

* ``_get_area_map()`` fetched ``config/area_registry/list`` over **REST**. That
  command exists only on the WebSocket API, so the call failed on every
  request and the area map was permanently ``{}``.
* ``_parse_device()`` read ``attributes.area_id`` off a state object. Home
  Assistant never puts an area there — area membership lives in the *device*
  and *entity registries*, which are WebSocket-only too.

The registries are already in the building: ``bridge_inventory``'s five-minute
refresh pulls all three over the WebSocket and hands them to
:func:`puffpai.bridge_ha.set_registries`. This module is the resolver on top of
that cache, and it is deliberately the ONLY one — ``/ha/home``, ``/ha/rooms``,
``list_devices``, ``find_devices`` and the voice tools all come through here,
so "which room is the floor lamp in" cannot have two answers.

Resolution order for a device's area, highest first:

1. the owner's own **layout placement** (:mod:`puffpai.home_layout`) — where
   they dragged the device on the map. It outranks HA because it is the more
   recent, more deliberate statement and it is the only one they *can* make:
   the everyday HA user this appliance holds is non-admin by design, so
   correcting a wrong HA area at the source is not available to them. A
   placement that could not win would be a control that does nothing;
2. an **entity**'s ``area_id`` — the most specific thing HA knows;
3. the **device**'s ``area_id``;
4. ``null``.

A placement naming an ``area_id`` that exists in neither HA's area registry
nor the layout's own rooms is **ignored**, and resolution falls through to HA.
Deleting a custom room must not strand every device that was in it in a room
that no longer draws.

Each device reports which rung it landed on as ``area_source``
(``ha`` | ``layout`` | ``none``), because "the owner placed this" and "HA knows
this" are different facts and the page draws them differently.

Read-only throughout: nothing here calls a service, and nothing here writes to
Home Assistant. The one write in the Home Blueprint feature is the layout file, and that lives in
:mod:`puffpai.home_layout`.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from puffpai.ha_bridge import CONTROLLABLE_DOMAINS, SAFETY_CRITICAL_DOMAINS

logger = logging.getLogger(__name__)

#: Which entity speaks for a device, most-actuated first. A ZHA bulb carries a
#: light, an update and four diagnostic sensors; the page needs the light.
#: ``lock`` sits below ``cover`` only because a device holding both is a garage
#: opener, whose cover IS the thing you operate.
PRIMARY_DOMAIN_ORDER: tuple[str, ...] = (
    "light", "switch", "climate", "media_player", "cover", "lock", "camera",
    "fan", "sensor",
)

#: Entity domain -> the ``kind`` the blueprint draws. Anything unlisted is
#: "other"; a device with no entities at all is a "hub" (a coordinator — a ZHA
#: stick, a Hue bridge — is exactly the device HA registers with none).
KIND_BY_DOMAIN: dict[str, str] = {
    "light": "light",
    "switch": "switch",
    "climate": "climate",
    "media_player": "media",
    "camera": "camera",
    "lock": "lock",
    "cover": "cover",
    "sensor": "sensor",
    "binary_sensor": "sensor",
    "fan": "switch",
}

#: Per-domain attribute whitelist. The state blob carries entity pictures,
#: access tokens (``camera`` has one), supported-feature bitmaps and whole
#: media queues; a map that needs a brightness has no business shipping any of
#: it to a browser. Anything not named here is dropped.
ATTRIBUTE_WHITELIST: dict[str, frozenset[str]] = {
    "light": frozenset({"brightness", "color_mode", "rgb_color"}),
    "climate": frozenset({"current_temperature", "temperature", "hvac_mode",
                          "hvac_modes"}),
    "media_player": frozenset({"volume_level", "media_title"}),
    "sensor": frozenset({"device_class", "unit_of_measurement", "battery"}),
    "binary_sensor": frozenset({"device_class"}),
    "cover": frozenset({"device_class"}),
    "camera": frozenset({"device_class"}),
    "fan": frozenset({"device_class"}),
}

#: Carried for every domain: a battery reading and a device class are what the
#: glyph is drawn from, whatever the entity happens to be.
COMMON_ATTRIBUTES = frozenset({"battery", "device_class"})

#: Domains that are not a *thing in the house*. On the live hub 18 of 44
#: "devices" were entries like "Home Assistant Supervisor", the TTS engines and
#: the owner's own automations: real registry rows, nothing you can point at in
#: a room. A floorplan that draws them is asking the owner to place furniture
#: that does not exist, so ``/ha/home`` omits any device whose every enabled
#: entity sits in here (see :func:`_is_non_physical` for the exact rule) and
#: reports the count in ``hidden.non_physical`` instead.
#:
#: Three deliberate calls about the membership:
#:
#: * ``assist_satellite`` is NOT here. A satellite is a microphone on a shelf —
#:   physical, and one of the things the owner most wants placed;
#: * helpers (``input_*``, ``timer``, ``counter``, ``schedule``) and HA's own
#:   furniture (``sun``, ``person``, ``zone``, ``weather``, ``calendar``,
#:   ``todo``, ``tag``, ``notify``, ``persistent_notification``) are here: they
#:   are state the hub keeps, not hardware;
#: * ``update``, ``event`` and ``button`` are here because a device that has
#:   *only* those is an integration's service entry, never a thing. A real bulb
#:   carries an ``update`` entity too, but it also carries its ``light``, so the
#:   all-of-them rule keeps it.
#:
#: This set is used by the ``/ha/home`` payload builder alone. ``ha_bridge``'s
#: room resolution and the voice tools must keep seeing automations and scripts
#: — "run the goodnight script" depends on it.
NON_PHYSICAL_DOMAINS: frozenset[str] = frozenset({
    # HA's own machinery and the assist pipeline's software halves
    "automation", "script", "scene", "stt", "tts", "wake_word", "ai_task",
    "conversation", "persistent_notification", "notify", "tag",
    # service-entry-only entities
    "update", "event", "button",
    # helpers
    "input_boolean", "input_button", "input_number", "input_select",
    "input_text", "input_datetime", "timer", "counter", "schedule",
    # hub-kept state about the world, not hardware in the house
    "zone", "person", "sun", "weather", "calendar", "todo",
})

#: A registry pull costs one authenticated WebSocket round trip against a
#: vault-gated token. When HA is down, a 5-second dashboard poll must not turn
#: into a 5-second reconnect storm, so a cold cache is re-pulled at most this
#: often.
_PULL_COOLDOWN_SECONDS = 30.0

_pull_lock = threading.Lock()
_last_pull_attempt = 0.0


# ---------------------------------------------------------------------------
# Registry access — the cache first, one pull when it is cold, never a raise
# ---------------------------------------------------------------------------

def _maybe_pull() -> None:
    """Fill a cold registry cache. Swallows every failure by design."""
    global _last_pull_attempt
    with _pull_lock:
        now = time.monotonic()
        if now - _last_pull_attempt < _PULL_COOLDOWN_SECONDS:
            return
        _last_pull_attempt = now
    try:
        from puffpai.bridge_inventory import _ha_registry_records
        _ha_registry_records()          # hands the result to bridge_ha itself
    except Exception as exc:
        logger.info("HA registry pull for /ha/home failed: %s", exc)


def registry_snapshot(*, allow_pull: bool = True) -> dict[str, Any]:
    """``{ok, method, age_seconds, devices, entities, areas, floors}``.

    Never raises: a route that cannot reach Home Assistant still has a page to
    draw, it just draws it without rooms.
    """
    from puffpai import bridge_ha
    devices, entities, areas = bridge_ha.cached_registries()
    if not devices and not entities and allow_pull:
        _maybe_pull()
        devices, entities, areas = bridge_ha.cached_registries()
    age = bridge_ha.registries_age_seconds()
    ok = bool(devices or entities)
    return {
        "ok": ok,
        "method": "registry" if ok else "states",
        "age_seconds": None if age is None else round(age, 1),
        "devices": devices,
        "entities": entities,
        "areas": areas,
        "floors": bridge_ha.cached_floors(),
    }


def _inventory_links() -> dict[str, str]:
    """``first entity_id -> LAN inventory key``, or {} when unavailable.

    Reuses :func:`puffpai.bridge_ha._inventory_links` rather than inventing a
    second HA-to-LAN join: the bucket view and the blueprint disagreeing about
    which network row a device is would be worse than having no key at all.
    """
    try:
        from puffpai.bridge_ha import _inventory_links as links_of
        links, _self = links_of()
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("inventory links unavailable: %s", exc)
        return {}
    return {entity_id: str(link.get("key") or "")
            for entity_id, link in links.items() if link.get("key")}


# ---------------------------------------------------------------------------
# Pure helpers — everything below is a function of its arguments
# ---------------------------------------------------------------------------

def _is_hidden(entity: dict[str, Any]) -> bool:
    """Disabled or hidden in HA's registry. The owner said "not this one"."""
    return bool(entity.get("disabled_by") or entity.get("hidden_by"))


def _domain(entity_id: str) -> str:
    return str(entity_id or "").split(".", 1)[0].strip().lower()


def entities_by_device(entities: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """``device_id -> entities``. Entities with no device land under ``""``."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        if not isinstance(entity, dict) or not entity.get("entity_id"):
            continue
        if _is_hidden(entity):
            continue
        grouped.setdefault(str(entity.get("device_id") or ""), []).append(entity)
    for own in grouped.values():
        own.sort(key=lambda e: str(e.get("entity_id")))
    return grouped


def area_names(areas: Iterable[Any]) -> dict[str, str]:
    """``area_id -> name`` from the area registry."""
    names: dict[str, str] = {}
    for area in areas:
        if not isinstance(area, dict):
            continue
        area_id = str(area.get("area_id") or area.get("id") or "")
        if area_id:
            names[area_id] = str(area.get("name") or area_id)
    return names


def entity_area_ids(devices: Iterable[Any], entities: Iterable[Any],
                    placements: dict[str, Any] | None = None,
                    known_areas: set[str] | None = None) -> dict[str, str]:
    """``entity_id -> area_id``, in the one resolution order.

    This is the map ``ha_bridge`` needs: it works entity-at-a-time, where the
    blueprint works device-at-a-time. Both go through :func:`_resolve_area`, so
    ``/ha/rooms``, ``list_devices``, ``find_devices`` and ``/ha/home`` cannot
    give the floor lamp two different rooms — including when the owner has
    moved it on the map.

    *placements* is keyed the way the layout keys it: by device id, and by
    ``entity:<entity_id>`` for an entity HA registered without a device.
    """
    placements = placements if isinstance(placements, dict) else {}
    known = known_areas if known_areas is not None else set()

    device_areas: dict[str, str] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        area_id = str(device.get("area_id") or "")
        if area_id:
            device_areas[str(device.get("id") or "")] = area_id

    mapping: dict[str, str] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("entity_id") or "")
        if not entity_id:
            continue
        device_id = str(entity.get("device_id") or "")
        candidates = [str(entity.get("area_id") or ""),
                      device_areas.get(device_id, "")]
        area_id, _source = _resolve_area(
            candidates, device_id or f"entity:{entity_id}", placements, known)
        if area_id:
            mapping[entity_id] = area_id
    return mapping


def layout_rooms_and_placements() -> tuple[dict[str, str], dict[str, Any]]:
    """``(area_id -> room name, device_id -> placement)``. Never raises.

    The one door onto the layout file for consumers outside this module, so
    ``ha_bridge`` does not have to learn the store's shape to honour a
    placement.
    """
    try:
        from puffpai import home_layout
        layout = home_layout.load()
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("home layout unavailable: %s", exc)
        return {}, {}
    rooms: dict[str, str] = {}
    for room in (layout.get("rooms") or []):
        if not isinstance(room, dict):
            continue
        area_id = str(room.get("area_id") or "")
        if area_id:
            rooms[area_id] = str(room.get("name") or area_id)
    placements = layout.get("placements")
    return rooms, placements if isinstance(placements, dict) else {}


def filter_attributes(domain: str, attributes: Any) -> dict[str, Any]:
    """The whitelisted subset of an entity's attributes for *domain*."""
    if not isinstance(attributes, dict):
        return {}
    allowed = ATTRIBUTE_WHITELIST.get(domain, frozenset()) | COMMON_ATTRIBUTES
    return {key: attributes[key] for key in allowed if key in attributes}


def _primary(entity_ids: list[str]) -> str | None:
    for domain in PRIMARY_DOMAIN_ORDER:
        for entity_id in entity_ids:
            if _domain(entity_id) == domain:
                return entity_id
    return None


def _kind(primary_entity_id: str | None, has_entities: bool) -> str:
    if primary_entity_id is None:
        return "other" if has_entities else "hub"
    return KIND_BY_DOMAIN.get(_domain(primary_entity_id), "other")


def _is_non_physical(entity_ids: list[str], bridge_key: str) -> bool:
    """True when this device is HA machinery rather than a thing in a room.

    Three guards, in order:

    * a device with a ``bridge_key`` is on the LAN with an address of its own,
      so it is hardware whatever its entities look like — never hidden;
    * a device with **zero** enabled entities stays. That is the shape of a
      coordinator (a ZHA stick, a Hue bridge), which the blueprint draws as a
      ``hub``; hiding it would be the opposite of the intent. "all of an empty
      set" is vacuously true, so this case has to be spelled out;
    * otherwise it is hidden only when *every* enabled entity is non-physical.
      One ``light`` among six ``update``\\ s is a bulb.
    """
    if bridge_key:
        return False
    if not entity_ids:
        return False
    return all(_domain(entity_id) in NON_PHYSICAL_DOMAINS
               for entity_id in entity_ids)


def _entity_payload(entity: dict[str, Any] | None, entity_id: str,
                    state: dict[str, Any] | None) -> dict[str, Any]:
    attrs = (state or {}).get("attributes") or {}
    domain = _domain(entity_id)
    name = ""
    if entity:
        name = str(entity.get("name") or entity.get("original_name") or "")
    if not name:
        name = str(attrs.get("friendly_name") or entity_id)
    return {
        "entity_id": entity_id,
        "domain": domain,
        "name": name,
        "state": str((state or {}).get("state") or "unavailable"),
        "attributes": filter_attributes(domain, attrs),
        "controllable": domain in CONTROLLABLE_DOMAINS,
        "safety_critical": domain in SAFETY_CRITICAL_DOMAINS,
    }


def _resolve_area(candidates: list[str], device_id: str,
                  placements: dict[str, Any],
                  known_areas: set[str]) -> tuple[str | None, str]:
    """``(area_id, source)`` for one device. The resolution order lives here.

    The owner's placement is consulted first — see the module docstring for why
    their map outranks the hub — but only if the room it names still exists,
    in HA's areas or in the layout's own rooms. An unknown ``area_id`` is
    treated as absent rather than trusted: it is either a room that was
    deleted, or a value that arrived from somewhere the map never drew, and in
    both cases falling through to HA is the answer that keeps the device
    findable. ``known_areas`` is therefore an allow-list, with no "empty means
    anything goes" escape hatch.
    """
    placement = placements.get(device_id)
    if isinstance(placement, dict):
        area_id = str(placement.get("area_id") or "")
        if area_id and area_id in known_areas:
            return area_id, "layout"
    for area_id in candidates:
        if area_id:
            return area_id, "ha"
    return None, "none"


def build_home(*, states: Iterable[Any] = (), devices: Iterable[Any] = (),
               entities: Iterable[Any] = (), areas: Iterable[Any] = (),
               floors: Iterable[Any] = (), layout: dict[str, Any] | None = None,
               links: dict[str, str] | None = None,
               include_system: bool = False) -> dict[str, Any]:
    """The floors/areas/devices body of ``/ha/home``. Pure — no HA, no disk."""
    from puffpai import bridge_ha

    # Materialised once: the arguments are declared Iterable and this function
    # walks each of them more than once.
    devices = list(devices)
    entities = list(entities)
    areas = list(areas)
    floors = list(floors)

    layout = layout or {}
    placements = layout.get("placements") if isinstance(layout, dict) else {}
    placements = placements if isinstance(placements, dict) else {}
    links = links or {}

    states_by_id: dict[str, dict[str, Any]] = {}
    for raw in states:
        if isinstance(raw, dict) and raw.get("entity_id"):
            states_by_id[str(raw["entity_id"])] = raw

    names = area_names(areas)
    out_areas = [
        {"area_id": area_id, "name": names[area_id],
         "floor_id": _floor_of(areas, area_id), "source": "ha"}
        for area_id in sorted(names, key=lambda a: names[a].lower())
    ]
    known = set(names)
    for room in (layout.get("rooms") or []) if isinstance(layout, dict) else []:
        if not isinstance(room, dict):
            continue
        area_id = str(room.get("area_id") or "")
        if not area_id or area_id in known:
            continue
        known.add(area_id)
        out_areas.append({"area_id": area_id,
                          "name": str(room.get("name") or area_id),
                          "floor_id": room.get("floor_id") or None,
                          "source": "layout"})

    out_floors: list[dict[str, Any]] = []
    seen_floors: set[str] = set()
    for floor in list(floors) + list(
            (layout.get("floors") or []) if isinstance(layout, dict) else []):
        if not isinstance(floor, dict):
            continue
        floor_id = str(floor.get("floor_id") or floor.get("id") or "")
        if not floor_id or floor_id in seen_floors:
            continue
        seen_floors.add(floor_id)
        level = floor.get("level")
        out_floors.append({
            "floor_id": floor_id,
            "name": str(floor.get("name") or floor_id),
            "level": level if isinstance(level, int) and not isinstance(level, bool) else 0,
        })

    grouped = entities_by_device(entities)
    claimed: set[str] = set()
    out_devices: list[dict[str, Any]] = []
    # What the page left out, so it can say so rather than silently shrink.
    hidden = {"non_physical": 0, "system": 0}

    for device in devices:
        if not isinstance(device, dict):
            continue
        device_id = str(device.get("id") or "")
        own = grouped.get(device_id, [])
        entity_ids = [str(e["entity_id"]) for e in own]
        claimed.update(entity_ids)

        integration = bridge_ha.integration_of(device, own)
        bridge_key = ""
        for entity_id in entity_ids:
            bridge_key = links.get(entity_id, "")
            if bridge_key:
                break
        tier = bridge_ha.tier_for(integration, bool(bridge_key))
        if not include_system:
            if tier == bridge_ha.TIER_SYSTEM:
                hidden["system"] += 1
                continue
            if _is_non_physical(entity_ids, bridge_key):
                hidden["non_physical"] += 1
                continue

        candidates = [str(e.get("area_id") or "") for e in own]
        candidates.append(str(device.get("area_id") or ""))
        area_id, source = _resolve_area(candidates, device_id, placements, known)
        primary = _primary(entity_ids)
        out_devices.append({
            "device_id": device_id,
            "name": str(device.get("name_by_user") or device.get("name") or device_id),
            "manufacturer": str(device.get("manufacturer") or ""),
            "model": str(device.get("model") or ""),
            "area_id": area_id,
            "area_source": source,
            "kind": _kind(primary, bool(entity_ids)),
            "primary_entity_id": primary,
            "integration": integration,
            "tier": tier,
            "bridge_key": bridge_key or None,
            "entities": [
                _entity_payload(entity, str(entity["entity_id"]),
                                states_by_id.get(str(entity["entity_id"])))
                for entity in own
            ],
        })

    # Entities HA registered without a device, plus — when the registries are
    # empty entirely — every state there is. Both are the same shape: one
    # entity standing in for its own device.
    loose: list[tuple[dict[str, Any] | None, str]] = [
        (entity, str(entity["entity_id"])) for entity in grouped.get("", [])
    ]
    if not devices and not entities:
        loose = [(None, entity_id) for entity_id in states_by_id]
    for entity, entity_id in loose:
        if entity_id in claimed:
            continue
        domain = _domain(entity_id)
        integration = str((entity or {}).get("platform") or "").strip().lower()
        bridge_key = links.get(entity_id) or ""
        # A loose entity is still asked for its tier with has_link=False, as it
        # always was: the bridge_key below is consulted only by the
        # non-physical rule, so adding that rule cannot move a device between
        # tiers or change what /bridge/ha/buckets says about it.
        tier = bridge_ha.tier_for(integration, False)
        if not include_system:
            if tier == bridge_ha.TIER_SYSTEM:
                hidden["system"] += 1
                continue
            if _is_non_physical([entity_id], bridge_key):
                hidden["non_physical"] += 1
                continue
        device_id = f"entity:{entity_id}"
        area_id, source = _resolve_area(
            [str((entity or {}).get("area_id") or "")], device_id, placements, known)
        payload = _entity_payload(entity, entity_id, states_by_id.get(entity_id))
        out_devices.append({
            "device_id": device_id,
            "name": payload["name"],
            "manufacturer": "",
            "model": "",
            "area_id": area_id,
            "area_source": source,
            "kind": _kind(entity_id if domain in PRIMARY_DOMAIN_ORDER else None, True),
            "primary_entity_id": entity_id,
            "integration": integration,
            "tier": tier,
            "bridge_key": bridge_key or None,
            "entities": [payload],
        })

    out_devices.sort(key=lambda d: (d["name"].lower(), d["device_id"]))
    return {"floors": out_floors, "areas": out_areas, "devices": out_devices,
            "hidden": hidden}


def _floor_of(areas: Iterable[Any], area_id: str) -> str | None:
    for area in areas:
        if not isinstance(area, dict):
            continue
        if str(area.get("area_id") or area.get("id") or "") == area_id:
            return str(area.get("floor_id")) if area.get("floor_id") else None
    return None


# ---------------------------------------------------------------------------
# The route's answer
# ---------------------------------------------------------------------------

def home_payload(*, include_system: bool = False) -> dict[str, Any]:
    """The whole ``GET /ha/home`` body. Never raises to the route."""
    snapshot = registry_snapshot()
    states: list[Any] = []
    try:
        from puffpai.ha_bridge import get_bridge
        states = get_bridge().get_all_states()
    except Exception as exc:
        logger.info("HA states unavailable for /ha/home: %s", exc)

    try:
        from puffpai import home_layout
        layout = home_layout.load()
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("home layout unavailable: %s", exc)
        layout = {}

    body = build_home(
        states=states,
        devices=snapshot["devices"], entities=snapshot["entities"],
        areas=snapshot["areas"], floors=snapshot["floors"],
        layout=layout, links=_inventory_links(),
        include_system=include_system,
    )
    return {
        "success": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "registry": {"ok": snapshot["ok"], "method": snapshot["method"],
                     "age_seconds": snapshot["age_seconds"]},
        **body,
    }


__all__ = [
    "PRIMARY_DOMAIN_ORDER", "KIND_BY_DOMAIN", "ATTRIBUTE_WHITELIST",
    "COMMON_ATTRIBUTES", "NON_PHYSICAL_DOMAINS",
    "registry_snapshot", "entities_by_device", "area_names", "entity_area_ids",
    "layout_rooms_and_placements", "filter_attributes", "build_home",
    "home_payload",
]
