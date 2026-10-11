"""
PuffPAI Zone 3 (Bridge) — the devices Home Assistant manages
============================================================

The device roll in :mod:`puffpai.bridge_inventory` can only see things that
put a packet on the LAN. Most of this house's smart devices cannot: a Zigbee
bulb or a Z-Wave lock has no IP address and no way to phone anywhere. Their
outside communication happens *entirely* through Home Assistant — the hub at
``home_assistant.url`` — talking to a vendor cloud on their behalf, or it does
not happen at all.

So the roll showed 18 network devices and none of the ~100 Home Assistant
manages, and that reads as "PuffPAI cannot see my house" when the truth is
"those devices are not ON the network in the sense ARP means". This module is
the other half of the answer: it folds HA's registries into **buckets**, one
per integration, so the main view stays a page long and the detail is a click
away.

Five tiers, and the distinction is the whole point:

``local``
    The integration speaks to the device on this LAN and to nobody else — ZHA,
    Z-Wave JS, ESPHome, Matter, a Hue bridge. Nothing leaves the house.
``cloud``
    The integration reaches a vendor's servers to do its job — Tuya, Ring,
    SmartThings, Alexa. Every command and every state change is somebody
    else's telemetry, and the DNS lookups for it are attributed here.
``wifi_direct``
    The device has its own address and is already a row in the network roll
    (``ha_match`` is set). It is counted in both places on purpose: the owner
    asking "what does HA manage" and the owner asking "what is on my network"
    are asking about the same object from two sides.
``unknown``
    An integration this module has no opinion about. Named, never hidden — a
    bucket labelled with its integration is a question the owner can answer,
    and a device silently dropped is not.
``system``
    Home Assistant's own furniture: the supervisor, the backup job, the sun
    position, the Pi's own power sensor, the helpers the owner defined. These
    are not devices anyone can have an opinion about and they are never
    offered a policy — a "block the sun" selector is noise that buries the
    three rows that matter. Shown, counted, collapsed by default.

Read-only throughout. Nothing here changes an integration, an entity, or a
firewall rule; the policy controls live on the Firewall page.

The registries are **not** fetched here. Opening a second HA WebSocket per
request would double the auth traffic against a vault-gated token for data the
inventory already pulls every five minutes, so
``bridge_inventory._ha_registry_records`` hands them to :func:`set_registries`
on each refresh and this module reads that cache.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TIER_LOCAL = "local"
TIER_CLOUD = "cloud"
TIER_WIFI = "wifi_direct"
TIER_UNKNOWN = "unknown"
TIER_SYSTEM = "system"

#: Integration name prefixes that are Home Assistant's own furniture. ``input_``
#: covers ``input_boolean``, ``input_number`` and the rest of the helper family
#: without listing each one.
SYSTEM_INTEGRATION_PREFIXES: tuple[str, ...] = ("input_",)

#: Integration domain -> tier. A module-level dict on purpose: this is the one
#: table that needs extending as the house grows, and extending it must not
#: mean reading the rest of the file.
#:
#: An integration listed here keeps its tier even when the device also holds a
#: LAN address. A Shelly relay on wifi is still locally controlled; demoting it
#: to ``wifi_direct`` because it happens to answer ARP would lose the fact the
#: owner actually cares about, which is whether a vendor is in the loop.
#: ``wifi_direct`` is therefore the fallback for integrations this table does
#: not name, not an override of it.
INTEGRATION_TIERS: dict[str, str] = {
    # -- local: the radio or the socket ends inside this house ---------------
    "zha": TIER_LOCAL,
    "zigbee": TIER_LOCAL,
    "zigbee2mqtt": TIER_LOCAL,
    "zwave_js": TIER_LOCAL,
    "esphome": TIER_LOCAL,
    "mqtt": TIER_LOCAL,
    "hue": TIER_LOCAL,
    "deconz": TIER_LOCAL,
    "shelly": TIER_LOCAL,
    "tasmota": TIER_LOCAL,
    "homekit_controller": TIER_LOCAL,
    "matter": TIER_LOCAL,
    "thread": TIER_LOCAL,
    "sonos": TIER_LOCAL,
    "cast": TIER_LOCAL,
    "roku": TIER_LOCAL,
    "kasa": TIER_LOCAL,
    "tplink": TIER_LOCAL,
    "wled": TIER_LOCAL,
    "unifi": TIER_LOCAL,
    "unifiprotect": TIER_LOCAL,
    "ipp": TIER_LOCAL,
    "printer": TIER_LOCAL,
    "upnp": TIER_LOCAL,
    "dlna_dmr": TIER_LOCAL,
    "brother": TIER_LOCAL,
    "samsungtv": TIER_LOCAL,
    "androidtv": TIER_LOCAL,
    # -- local: has its own IP, but the conversation still ends in the house --
    # These also show up in the network roll; see ``linked_keys``. Keeping them
    # local is the point: a Reolink camera reachable at 192.168.1.x is not a vendor
    # cloud, and calling it wifi_direct would lose that.
    "reolink": TIER_LOCAL,
    "apple_tv": TIER_LOCAL,
    "music_assistant": TIER_LOCAL,
    "vlc_telnet": TIER_LOCAL,
    "ollama": TIER_LOCAL,
    "plex": TIER_LOCAL,
    "radarr": TIER_LOCAL,
    "sonarr": TIER_LOCAL,
    "frigate": TIER_LOCAL,
    "octoprint": TIER_LOCAL,
    "wiz": TIER_LOCAL,
    "yeelight": TIER_LOCAL,
    "lifx": TIER_LOCAL,
    "broadlink": TIER_LOCAL,
    "xiaomi_miio": TIER_LOCAL,
    "denonavr": TIER_LOCAL,
    "webostv": TIER_LOCAL,
    "lg_webos": TIER_LOCAL,
    "bluetooth": TIER_LOCAL,
    "ibeacon": TIER_LOCAL,
    "enphase_envoy": TIER_LOCAL,
    "tesla_wall_connector": TIER_LOCAL,
    # -- cloud: a vendor's servers are in the control path -------------------
    "tuya": TIER_CLOUD,
    "localtuya": TIER_CLOUD,
    "ring": TIER_CLOUD,
    "nest": TIER_CLOUD,
    "google_assistant": TIER_CLOUD,
    "google": TIER_CLOUD,
    "alexa": TIER_CLOUD,
    "amazon": TIER_CLOUD,
    "amazon_polly": TIER_CLOUD,
    "smartthings": TIER_CLOUD,
    "ecobee": TIER_CLOUD,
    "honeywell": TIER_CLOUD,
    "lyric": TIER_CLOUD,
    "myq": TIER_CLOUD,
    "wyze": TIER_CLOUD,
    "august": TIER_CLOUD,
    "rachio": TIER_CLOUD,
    "sensibo": TIER_CLOUD,
    "meross": TIER_CLOUD,
    "meross_cloud": TIER_CLOUD,
    "govee": TIER_CLOUD,
    "switchbot": TIER_CLOUD,
    "switchbot_cloud": TIER_CLOUD,
    "spotify": TIER_CLOUD,
    "netatmo": TIER_CLOUD,
    "withings": TIER_CLOUD,
    "home_assistant": TIER_CLOUD,
    # -- cloud: a server somewhere answers, even when nothing was "smart" ----
    # Weather, translation and notification integrations feel like furniture
    # and are not: each one is a request that leaves the house on a schedule.
    "openweathermap": TIER_CLOUD,
    "google_translate": TIER_CLOUD,
    # Nabu Casa. Named ``cloud`` by Home Assistant, and it is exactly that.
    "cloud": TIER_CLOUD,
    "accuweather": TIER_CLOUD,
    "weatherapi": TIER_CLOUD,
    "opensky": TIER_CLOUD,
    "twilio": TIER_CLOUD,
    "telegram": TIER_CLOUD,
    "discord": TIER_CLOUD,
    "pushover": TIER_CLOUD,
    "ifttt": TIER_CLOUD,
    "openai_conversation": TIER_CLOUD,
    "anthropic": TIER_CLOUD,
    "elevenlabs": TIER_CLOUD,
    "todoist": TIER_CLOUD,
    "google_calendar": TIER_CLOUD,
    "google_mail": TIER_CLOUD,
    "icloud": TIER_CLOUD,
    "fitbit": TIER_CLOUD,
    "tesla": TIER_CLOUD,
    "ecovacs": TIER_CLOUD,
    "roborock": TIER_CLOUD,

    # -- system: Home Assistant's own furniture, never offered a policy ------
    "hassio": TIER_SYSTEM,
    "backup": TIER_SYSTEM,
    "sun": TIER_SYSTEM,
    "raspberry_pi": TIER_SYSTEM,
    "rpi_power": TIER_SYSTEM,
    "homeassistant": TIER_SYSTEM,
    "hacs": TIER_SYSTEM,
    "mobile_app": TIER_SYSTEM,
    "met": TIER_SYSTEM,
    "shopping_list": TIER_SYSTEM,
    "person": TIER_SYSTEM,
    "zone": TIER_SYSTEM,
    "template": TIER_SYSTEM,
    "group": TIER_SYSTEM,
    "schedule": TIER_SYSTEM,
    "tag": TIER_SYSTEM,
    "webhook": TIER_SYSTEM,
    "default_config": TIER_SYSTEM,
}

#: Integration -> the DNS suffixes its cloud answers on. Used to attribute the
#: hub's outbound lookups to the integration that caused them, because every
#: one of those lookups is recorded against a single client IP (the hub's) and
#: is otherwise an undifferentiated pile.
#:
#: Suffix match, not substring: ``ring.com`` matches ``fw.ring.com`` and never
#: ``notring.com``. Extend freely; order does not matter because the longest
#: matching suffix wins.
INTEGRATION_DOMAIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "tuya": ("tuyaus.com", "tuyaeu.com", "tuyacn.com", "tuya.com"),
    "ring": ("ring.com",),
    "nest": ("nest.com", "home.nest.com"),
    "smartthings": ("smartthings.com", "samsungiotcloud.com"),
    "alexa": ("amazonalexa.com", "amazon.com", "a2z.com"),
    "google": ("googleapis.com", "google.com"),
    "ecobee": ("ecobee.com",),
    "wyze": ("wyze.com", "wyzecam.com"),
    "govee": ("govee.com",),
    "meross": ("meross.com",),
    "switchbot": ("switch-bot.com",),
    "august": ("august.com",),
    "myq": ("myq-cloud.com",),
    "honeywell": ("honeywell.com",),
    "lyric": ("honeywellhome.com",),
    "sensibo": ("sensibo.com",),
    "hue": ("meethue.com",),
    "sonos": ("sonos.com",),
    "roku": ("roku.com",),
    "spotify": ("spotify.com", "scdn.co"),
    # Same servers as ``tuya``; ``tuya`` is declared first and keeps them (see
    # :func:`_suffix_index`). Listed so every cloud integration has an entry.
    "localtuya": ("tuyaus.com", "tuyaeu.com", "tuyacn.com", "tuya.com"),
    "google_assistant": ("googleusercontent.com",),
    "amazon": ("amazon.com", "a2z.com"),
    "amazon_polly": ("polly.amazonaws.com",),
    "rachio": ("rach.io", "rachio.com"),
    "meross_cloud": ("meross.com",),
    "switchbot_cloud": ("switch-bot.com",),
    "netatmo": ("netatmo.com", "netatmo.net"),
    "withings": ("withings.com", "withings.net"),
    "home_assistant": ("home-assistant.io",),
    "openweathermap": ("api.openweathermap.org", "openweathermap.org"),
    "google_translate": ("translate.google.com", "translate.googleapis.com"),
    "cloud": ("nabucasa.com", "cloud.nabu.casa"),
    "accuweather": ("accuweather.com", "dataservice.accuweather.com"),
    "weatherapi": ("weatherapi.com",),
    "opensky": ("opensky-network.org",),
    "twilio": ("twilio.com",),
    "telegram": ("telegram.org", "t.me"),
    "discord": ("discord.com", "discordapp.com"),
    "pushover": ("pushover.net",),
    "ifttt": ("ifttt.com",),
    "openai_conversation": ("api.openai.com", "openai.com"),
    "anthropic": ("api.anthropic.com", "anthropic.com"),
    "elevenlabs": ("elevenlabs.io",),
    "todoist": ("todoist.com",),
    # Bare ``googleapis.com`` deliberately stays with ``google`` above: giving
    # it to the calendar would attribute every Nest and Assistant lookup in the
    # house to a calendar sync.
    "google_calendar": ("calendar.google.com",),
    "google_mail": ("gmail.com", "mail.google.com"),
    "icloud": ("icloud.com", "icloud-content.com"),
    "fitbit": ("fitbit.com",),
    "tesla": ("tesla.com", "teslamotors.com"),
    "ecovacs": ("ecovacs.com", "ecouser.net"),
    "roborock": ("roborock.com",),
}

#: :mod:`puffpai.zone3.telemetry_domains` is keyed by *manufacturer* and this
#: module is keyed by *integration*. One small bridge between the two, so the
#: curated per-vendor lists count toward the right bucket without being copied.
_MANUFACTURER_INTEGRATIONS: dict[str, str] = {
    "Samsung": "smartthings",
    "Amazon": "alexa",
    "Google": "google",
    "Nest": "nest",
    "Ring": "ring",
    "Wyze": "wyze",
    "TP-Link": "tplink",
    "Tuya": "tuya",
    "Ecobee": "ecobee",
    "Philips Hue": "hue",
    "Sonos": "sonos",
    "Roku": "roku",
    "LG": "lg",
    "Espressif": "esphome",
}

#: Entity domains the drill-down names explicitly. Anything else is "other" —
#: a bucket row is a summary, and thirty single-entity columns is not one.
ENTITY_KINDS = (
    "light", "switch", "sensor", "binary_sensor", "climate", "camera",
    "media_player", "lock", "cover", "fan", "vacuum",
)

#: The synthetic bucket for hub traffic no integration claims.
HA_SELF_BUCKET = f"{TIER_CLOUD}:home_assistant"

#: Ledger categories that count as "somebody is being told about this house".
_WATCHING = ("TELEMETRY", "ADS", "TRACKING")

BUCKET_ID_RE = re.compile(
    r"^(local|cloud|wifi_direct|unknown|system):[a-z0-9_.-]{1,64}$")

_TELEMETRY_HOURS = 24

# ---------------------------------------------------------------------------
# Registry cache — written by the inventory refresh, read here.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_registries: tuple[list[Any], list[Any], list[Any]] | None = None
_floors: list[Any] = []
_fetched_at: float = 0.0


def set_registries(devices: Iterable[Any], entities: Iterable[Any],
                   areas: Iterable[Any],
                   floors: Iterable[Any] | None = None) -> None:
    """Hand this module the registries the inventory just fetched.

    *floors* is optional because ``config/floor_registry/list`` does not exist
    on older Home Assistant: the pull tolerates it failing and simply does not
    pass it, which must not cost this cache the three registries that did
    arrive.
    """
    global _registries, _floors, _fetched_at
    with _lock:
        _registries = (list(devices), list(entities), list(areas))
        if floors is not None:
            _floors = list(floors)
        _fetched_at = time.time()


def cached_registries() -> tuple[list[Any], list[Any], list[Any]]:
    """``(devices, entities, areas)``; three empty lists before the first pull."""
    with _lock:
        if _registries is None:
            return ([], [], [])
        return (list(_registries[0]), list(_registries[1]), list(_registries[2]))


def cached_floors() -> list[Any]:
    """The floor registry, or [] when HA never served one."""
    with _lock:
        return list(_floors)


def registries_age_seconds() -> float | None:
    """Seconds since the last pull, or None if there has never been one."""
    with _lock:
        if _registries is None or not _fetched_at:
            return None
        return max(0.0, time.time() - _fetched_at)


def reset() -> None:
    """Drop the cache. Tests only."""
    global _registries, _floors, _fetched_at
    with _lock:
        _registries = None
        _floors = []
        _fetched_at = 0.0


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def integration_of(device: dict[str, Any],
                   entities: list[dict[str, Any]]) -> str:
    """The integration domain behind an HA device.

    The entity registry's ``platform`` is authoritative — it is literally the
    integration that created the entity. ``identifiers[0][0]`` is the fallback,
    because a device with no entities left still carries the domain that
    registered it.
    """
    for entity in entities:
        platform = str(entity.get("platform") or "").strip().lower()
        if platform:
            return platform
    for ident in device.get("identifiers") or []:
        if isinstance(ident, (list, tuple)) and ident:
            value = str(ident[0]).strip().lower()
            if value:
                return value
    return TIER_UNKNOWN


def tier_for(integration: str, on_network: bool) -> str:
    """Tier for an integration. See :data:`INTEGRATION_TIERS` for the rule."""
    name = (integration or "").strip().lower()
    known = INTEGRATION_TIERS.get(name)
    if known:
        return known
    if name.startswith(SYSTEM_INTEGRATION_PREFIXES):
        return TIER_SYSTEM
    # A device with no integration at all is a question, not a wifi device,
    # even when something on the LAN happens to answer for it.
    if name in ("", TIER_UNKNOWN):
        return TIER_UNKNOWN
    return TIER_WIFI if on_network else TIER_UNKNOWN


def bucket_id(tier: str, integration: str) -> str:
    return f"{tier}:{integration}"


def bucket_for_devices(linked_entity_ids: set[str]) -> dict[str, str]:
    """``entity_id -> bucket id`` keyed by each HA device's FIRST entity id.

    Pure over the registry cache and the set it is handed: it never reads the
    inventory. The inventory calls this from inside its own merge lock to stamp
    ``bucket`` onto the rows it joined, and a helper that reached back into
    ``get_inventory().devices()`` there would deadlock on that same lock.

    The first entity id is the join key because that is the one
    ``bridge_inventory._registry_records`` records as ``ha_entity_id``.
    """
    ha_devices, ha_entities, _areas = cached_registries()
    by_device: dict[str, list[dict[str, Any]]] = {}
    for entity in ha_entities:
        if not isinstance(entity, dict):
            continue
        device_id = entity.get("device_id")
        if device_id:
            by_device.setdefault(str(device_id), []).append(entity)

    mapping: dict[str, str] = {}
    for device in ha_devices:
        if not isinstance(device, dict):
            continue
        own = by_device.get(str(device.get("id") or ""), [])
        entity_ids = sorted(
            str(e.get("entity_id")) for e in own if e.get("entity_id"))
        if not entity_ids:
            continue
        first = entity_ids[0]
        integration = integration_of(device, own)
        mapping[first] = bucket_id(
            tier_for(integration, first in linked_entity_ids), integration)
    return mapping


def _entity_kind(entity_id: str) -> str:
    domain = str(entity_id or "").split(".", 1)[0].strip().lower()
    return domain if domain in ENTITY_KINDS else "other"


# ---------------------------------------------------------------------------
# Telemetry attribution
# ---------------------------------------------------------------------------

def ha_host() -> str:
    """The hub's address, from ``home_assistant.url``, or "" if unconfigured."""
    try:
        from puffpai.bridge_inventory import _ha_resolve
        url, _token, _enabled = _ha_resolve()
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("HA url unavailable: %s", exc)
        return ""
    if not url:
        return ""
    try:
        return urlparse(url if "//" in url else f"http://{url}").hostname or ""
    except ValueError:
        return ""


def _suffix_index() -> dict[str, str]:
    """DNS suffix -> integration, folding in the curated per-vendor lists."""
    index: dict[str, str] = {}
    try:
        from puffpai.zone3.telemetry_domains import TelemetryDB
        db = TelemetryDB()
        for manufacturer in db.manufacturers:
            integration = _MANUFACTURER_INTEGRATIONS.get(manufacturer)
            if not integration:
                continue
            for entry in db.get_domains(manufacturer, include_common=False):
                index.setdefault(entry.domain.strip().lower(), integration)
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("telemetry domain DB unavailable: %s", exc)
    # Declared last so this module's own table wins a collision with the
    # curated vendor DB: it is the one a reader of this file can see and edit.
    # Within the table itself the FIRST declaration wins, so a narrower
    # integration listed later (``localtuya``, which speaks to the same
    # servers) cannot steal the suffixes from the one that owns them.
    mine: dict[str, str] = {}
    for integration, suffixes in INTEGRATION_DOMAIN_SUFFIXES.items():
        for suffix in suffixes:
            mine.setdefault(suffix.strip().lower(), integration)
    index.update(mine)
    return index


def attribute_domain(domain: str, index: dict[str, str] | None = None) -> str:
    """Integration a hub lookup belongs to, or "" when nothing claims it.

    Longest suffix first, so ``home.nest.com`` cannot be stolen by a shorter
    entry and ``tuyaus.com`` is not read as ``tuya.com``'s child.
    """
    name = (domain or "").strip().lower().rstrip(".")
    if not name:
        return ""
    table = index if index is not None else _suffix_index()
    parts = name.split(".")
    for start in range(len(parts) - 1):
        candidate = ".".join(parts[start:])
        hit = table.get(candidate)
        if hit:
            return hit
    return ""


def _hub_rows(hours: int) -> list[dict[str, Any]]:
    """Ledger rows whose client is the hub. Empty on any ledger fault."""
    host = ha_host()
    if not host:
        return []
    try:
        from puffpai.bridge_ledger import get_ledger
        rows = get_ledger().read(hours)
    except Exception as exc:
        logger.info("bridge ledger unavailable for HA attribution: %s", exc)
        return []
    return [r for r in rows if str(r.get("client_ip") or "") == host]


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------

#: Integrations whose "device" IS this appliance. Home Assistant registers the
#: Ollama endpoint as a device with no MAC and no entity HA can match, so the
#: ordinary ``ha_match`` join never fires — yet the hardware answering is the
#: box the owner is reading this page on, and it is already a row in the roll.
_SELF_INTEGRATIONS = ("ollama",)


def _inventory_links() -> tuple[dict[str, dict[str, str]], dict[str, str] | None]:
    """``(entity_id -> link, self_link)`` for rows HA actually joined.

    The second element is the appliance's own row (``is_self``), used to link
    the integrations in :data:`_SELF_INTEGRATIONS` to hardware that is in the
    roll under its own address rather than leaving them looking cloudless.
    """
    links: dict[str, dict[str, str]] = {}
    self_link: dict[str, str] | None = None
    try:
        from puffpai.bridge_inventory import get_inventory
        rows = get_inventory().devices()
    except Exception as exc:
        logger.info("inventory unavailable for HA bucketing: %s", exc)
        return links, None
    for row in rows:
        if self_link is None and row.get("is_self"):
            self_link = {
                "key": str(row.get("key") or ""),
                "name": str(row.get("display_name") or ""),
                "match": "self",
            }
        entity_id = str(row.get("ha_entity_id") or "")
        if not entity_id or not row.get("ha_match"):
            continue
        links[entity_id] = {
            "key": str(row.get("key") or ""),
            "name": str(row.get("display_name") or ""),
            "match": str(row.get("ha_match") or ""),
        }
    return links, self_link


def _build(hours: int = _TELEMETRY_HOURS) -> dict[str, dict[str, Any]]:
    """Every bucket, keyed by id, devices included. The one place that folds."""
    ha_devices, ha_entities, ha_areas = cached_registries()

    area_names: dict[str, str] = {}
    for area in ha_areas:
        if not isinstance(area, dict):
            continue
        area_id = str(area.get("area_id") or area.get("id") or "")
        if area_id:
            area_names[area_id] = str(area.get("name") or "")

    by_device: dict[str, list[dict[str, Any]]] = {}
    for entity in ha_entities:
        if not isinstance(entity, dict):
            continue
        device_id = entity.get("device_id")
        if device_id:
            by_device.setdefault(str(device_id), []).append(entity)

    links, self_link = _inventory_links()
    buckets: dict[str, dict[str, Any]] = {}

    for device in ha_devices:
        if not isinstance(device, dict):
            continue
        own = by_device.get(str(device.get("id") or ""), [])
        entity_ids = sorted(
            str(e.get("entity_id")) for e in own if e.get("entity_id"))
        link = links.get(entity_ids[0]) if entity_ids else None

        integration = integration_of(device, own)
        if link is None and integration in _SELF_INTEGRATIONS:
            link = self_link
        tier = tier_for(integration, link is not None)
        bid = bucket_id(tier, integration)

        area_id = str(device.get("area_id") or "")
        if not area_id:
            for entity in own:
                if entity.get("area_id"):
                    area_id = str(entity["area_id"])
                    break
        area = area_names.get(area_id, "") or "Unassigned"

        bucket = buckets.setdefault(bid, _empty_bucket(bid, tier, integration))
        bucket["device_count"] += 1
        bucket["entity_count"] += len(entity_ids)
        bucket["areas"][area] = bucket["areas"].get(area, 0) + 1
        for entity_id in entity_ids:
            kind = _entity_kind(entity_id)
            bucket["kinds"][kind] = bucket["kinds"].get(kind, 0) + 1
        if link:
            bucket["linked_keys"].append(link["key"])
        bucket["devices"].append({
            "name": str(device.get("name_by_user") or device.get("name") or ""),
            "area": area,
            "manufacturer": str(device.get("manufacturer") or ""),
            "model": str(device.get("model") or ""),
            "ha_match": link["match"] if link else "",
            "linked_key": link["key"] if link else "",
            "linked_name": link["name"] if link else "",
            "entities": [
                {
                    "entity_id": str(e.get("entity_id") or ""),
                    "domain": _entity_kind(str(e.get("entity_id") or "")),
                    "name": str(e.get("name") or e.get("original_name") or ""),
                }
                for e in own if e.get("entity_id")
            ],
        })

    _attribute(buckets, hours)
    return buckets


def _empty_bucket(bid: str, tier: str, integration: str) -> dict[str, Any]:
    return {
        "id": bid,
        "tier": tier,
        "integration": integration,
        "device_count": 0,
        "entity_count": 0,
        "areas": {},
        "kinds": {},
        "linked_keys": [],
        "devices": [],
        "lookups": 0,
        "watching": 0,
        "top_domains": [],
    }


def _attribute(buckets: dict[str, dict[str, Any]], hours: int) -> None:
    """Fold the hub's 24h lookups into the buckets that caused them."""
    rows = _hub_rows(hours)
    if not rows:
        return
    index = _suffix_index()
    by_integration: dict[str, str] = {}
    for bid, bucket in buckets.items():
        by_integration.setdefault(str(bucket["integration"]), bid)

    counters: dict[str, dict[str, Any]] = {}
    for row in rows:
        domain = str(row.get("domain") or "").strip().lower()
        integration = attribute_domain(domain, index)
        # An integration whose cloud we recognise but whose devices are not in
        # the registries has nowhere honest to go: the hub really did make the
        # lookup, so it lands on the hub's own bucket rather than inventing a
        # bucket with no devices in it.
        target = by_integration.get(integration, "") if integration else ""
        if not target:
            target = HA_SELF_BUCKET
        counter = counters.setdefault(
            target, {"lookups": 0, "watching": 0, "domains": {}})
        counter["lookups"] += 1
        if str(row.get("category") or "").upper() in _WATCHING:
            counter["watching"] += 1
        if domain:
            counter["domains"][domain] = counter["domains"].get(domain, 0) + 1

    for bid, counter in counters.items():
        bucket = buckets.get(bid)
        if bucket is None:
            if bid != HA_SELF_BUCKET:               # pragma: no cover - defensive
                continue
            bucket = _empty_bucket(bid, TIER_CLOUD, "home_assistant")
            bucket["label"] = "Home Assistant itself"
            buckets[bid] = bucket
        bucket["lookups"] = counter["lookups"]
        bucket["watching"] = counter["watching"]
        bucket["top_domains"] = [
            {"domain": d, "count": n}
            for d, n in sorted(counter["domains"].items(),
                               key=lambda kv: (-kv[1], kv[0]))[:5]
        ]


#: Sort weight per tier. Cloud first because it is the tier that answers the
#: question the owner actually asked; unknown last because it is a to-do, not
#: a finding.
_TIER_ORDER = {TIER_CLOUD: 0, TIER_WIFI: 1, TIER_LOCAL: 2,
               TIER_UNKNOWN: 3, TIER_SYSTEM: 4}


def _summary(bucket: dict[str, Any]) -> dict[str, Any]:
    """A bucket without its device list — what the roll shows."""
    return {
        "id": bucket["id"],
        "tier": bucket["tier"],
        "integration": bucket["integration"],
        "label": bucket.get("label", ""),
        "device_count": bucket["device_count"],
        "entity_count": bucket["entity_count"],
        "areas": dict(bucket["areas"]),
        "kinds": dict(bucket["kinds"]),
        "linked_keys": list(bucket["linked_keys"]),
        "lookups": bucket["lookups"],
        "watching": bucket["watching"],
        "top_domains": list(bucket["top_domains"]),
    }


def buckets(hours: int = _TELEMETRY_HOURS) -> list[dict[str, Any]]:
    """Every bucket, cloud first and loudest first within it."""
    built = _build(hours)
    rows = [_summary(b) for b in built.values()]
    rows.sort(key=lambda b: (
        _TIER_ORDER.get(b["tier"], 9),
        -b["lookups"] if b["tier"] == TIER_CLOUD else 0,
        -b["device_count"],
        b["integration"],
    ))
    return rows


def bucket(bucket_id_: str, hours: int = _TELEMETRY_HOURS) -> dict[str, Any] | None:
    """One bucket with its devices and their entities, or None if there is none."""
    wanted = (bucket_id_ or "").strip()
    if not BUCKET_ID_RE.match(wanted):
        return None
    found = _build(hours).get(wanted)
    if found is None:
        return None
    detail = _summary(found)
    detail["devices"] = list(found["devices"])
    return detail


def counts(hours: int = _TELEMETRY_HOURS) -> dict[str, int]:
    """The one-line strip: bucket/device/cloud/lookup totals for ``/bridge/status``."""
    rows = buckets(hours)
    return {
        "buckets": len(rows),
        "devices": sum(r["device_count"] for r in rows),
        "entities": sum(r["entity_count"] for r in rows),
        "cloud_buckets": sum(1 for r in rows if r["tier"] == TIER_CLOUD),
        "lookups_24h": sum(r["lookups"] for r in rows),
        "watching_24h": sum(r["watching"] for r in rows),
    }


__all__ = [
    "INTEGRATION_TIERS", "INTEGRATION_DOMAIN_SUFFIXES", "ENTITY_KINDS",
    "BUCKET_ID_RE", "HA_SELF_BUCKET", "SYSTEM_INTEGRATION_PREFIXES",
    "TIER_LOCAL", "TIER_CLOUD", "TIER_WIFI", "TIER_UNKNOWN", "TIER_SYSTEM",
    "set_registries", "cached_registries", "cached_floors",
    "registries_age_seconds", "reset",
    "integration_of", "tier_for", "bucket_id", "bucket_for_devices",
    "attribute_domain", "ha_host",
    "buckets", "bucket", "counts",
]
