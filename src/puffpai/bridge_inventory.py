"""
PuffPAI Zone 3 (Bridge) — Device Inventory
==========================================

"See the devices." A read-only, observe-only inventory of everything on the
private LAN, merged from up to four sources and persisted to
``bridge_devices.json`` under ``$PUFFPAI_DATA_DIR``.

This module **never blocks, quarantines or changes anything**. It reads the
ARP/neighbour table, mDNS, the Home Assistant entity registry and (when Zone 3
is deployed) AdGuard's client list, and writes one merged ledger of what lives
on the network. Enforcement is a separate, owner-flipped switch that lives in
``puffpai.zone3`` — not here.

Relationship to :mod:`puffpai.device_sentinel`
----------------------------------------------
The sentinel is a *daemon* that alerts on and quarantines **new** devices. This
is a *ledger* of **all** devices. They share ideas but not lifecycle:

* ARP parsing follows ``device_sentinel._scan_ip`` (device_sentinel.py:384-410)
  — same IP/MAC regexes, same ``FF:FF:FF:FF:FF:FF`` and
  :data:`~puffpai.device_sentinel.IGNORED_MAC_PREFIXES` filters, and that
  constant is imported rather than copied. It is lifted into a free function
  here rather than called on a ``DeviceSentinel`` instance for two reasons:
  constructing the daemon loads its baseline and replays its whole alert log,
  and ``_scan_ip`` shells out to ``arp -a`` only, which on the Orin target is a
  deprecated net-tools binary that may be absent. :func:`_scan_arp` prefers
  ``ip neigh`` and falls back to ``arp -a``.
* HA comes from :class:`puffpai.ha_bridge.HABridge` rather than
  ``DeviceSentinel._scan_ha``, because the latter yields only entities the
  watcher considers *new* — useless for a full inventory.

The sentinel daemon stays off: ``PUFFPAI_SENTINEL_ENABLED`` is untouched by
this module.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from puffpai.statedir import resolve_runtime_dir

# Constant-only import: no daemon is constructed and no thread is started.
from puffpai.device_sentinel import IGNORED_MAC_PREFIXES

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"

#: How often the background refresher re-scans, in seconds.
DEFAULT_REFRESH_SECONDS = 300

#: Env flag that turns the inventory (and its refresher) on independently of
#: the Zone 3 config flag. Either one is enough.
ENABLE_ENV = "PUFFPAI_BRIDGE_INVENTORY"

#: Test/deployment override hook, matching the house pattern in
#: :mod:`puffpai.device_sentinel`. Assign a Path to pin the file.
_DEVICES_FILE: Path | None = None

_BROADCAST_MAC = "FF:FF:FF:FF:FF:FF"

_IP_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
_MAC_RE = re.compile(r"([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}")

#: Interface names whose neighbours are container/VM plumbing, not the LAN.
_VIRTUAL_IFACE_RE = re.compile(
    r"^(docker|veth|br-|virbr|vmnet|lo|tun|tap|cni|flannel|kube|zt)", re.I)

#: Cache of CIDRs discovered on virtual/container interfaces, refreshed once
#: per :meth:`BridgeInventory.refresh`. See :func:`_virtual_subnets`.
_virtual_subnets_cache: list[ipaddress.IPv4Network] = []


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    """Resolve $PUFFPAI_DATA_DIR through the shared stdlib-only leaf."""
    return Path(resolve_runtime_dir(str(_DEFAULT_DATA_DIR)))


def devices_file() -> Path:
    """Path to the persisted device roll."""
    return _DEVICES_FILE or _data_dir() / "bridge_devices.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Address helpers
# ---------------------------------------------------------------------------

def normalize_mac(mac: str) -> str:
    """Upper-case colon-separated MAC, or "" if it does not look like one."""
    if not mac:
        return ""
    candidate = mac.strip().replace("-", ":").upper()
    if not _MAC_RE.fullmatch(candidate):
        return ""
    return candidate


def _virtual_subnets() -> list[ipaddress.IPv4Network]:
    """CIDRs configured on container/VM interfaces, from ``ip -br addr``.

    Linux only — empty on failure or on the Windows dev box. This is how
    Docker/virtual neighbours get excluded from the LAN roll: not by a
    blanket RFC1918 sub-range (that also blanks real home LANs like an iOS
    hotspot's 172.20.10.0/24), but by the actual subnet configured on an
    interface whose name matches :data:`_VIRTUAL_IFACE_RE` — ``docker0``,
    ``br-...``, ``veth...``, ``virbr...``, and so on.
    """
    out = _run(["ip", "-br", "addr"], timeout=5)
    if not out:
        return []
    nets: list[ipaddress.IPv4Network] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        iface = parts[0]
        if not _VIRTUAL_IFACE_RE.match(iface):
            continue
        for token in parts[2:]:
            if "/" not in token or ":" in token:
                continue  # skip IPv6 tokens
            try:
                net = ipaddress.ip_network(token, strict=False)
            except ValueError:
                continue
            if isinstance(net, ipaddress.IPv4Network):
                nets.append(net)
    return nets


def is_lan_ip(ip: str) -> bool:
    """True for a private-LAN address we are willing to inventory.

    Excludes loopback, link-local, multicast, anything public, and any
    address that falls inside a subnet discovered on a container/VM
    interface (see :func:`_virtual_subnets`). All of RFC1918 otherwise
    counts as LAN, including 172.16-31.0.0/16: blanket-excluding it as
    "Docker's pool" would blank real home LANs such as an iOS hotspot's
    172.20.10.0/24.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not addr.is_private or addr.is_loopback or addr.is_link_local:
        return False
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return False
    return not any(addr in net for net in _virtual_subnets_cache)


def _is_ignorable_mac(mac: str) -> bool:
    """Broadcast, all-zero, or a host-local virtual adapter.

    An empty MAC is deliberately NOT ignorable here: "" means *unknown*, not
    *seen and rejected*. A MAC-less ARP row (state REACHABLE/STALE with no
    ``lladdr`` — as opposed to FAILED/INCOMPLETE, which ``_parse_ip_neigh``
    already drops before this ever runs) is a real device; it just has to key
    on ``ip:<addr>`` instead of a MAC. Treating "" as ignorable would eat
    that row before it could ever reach the ``ip:<addr>`` fallback in
    :meth:`BridgeInventory.refresh`.
    """
    if mac == _BROADCAST_MAC:
        return True
    if mac and set(mac.replace(":", "")) == {"0"}:
        return True
    return bool(mac) and mac.startswith(IGNORED_MAC_PREFIXES)


def _own_addresses() -> set[str]:
    """Best-effort set of this host's own IPv4 addresses.

    Prefers ``ip -br addr`` (Linux target): every IPv4 configured on a
    non-virtual interface, so the Orin's own LAN address is found even when
    it never shows up as a neighbour of itself in the ARP/neighbour table.
    Falls back to the ``socket``-based approach (the Windows dev box, or a
    Linux host without ``ip``).
    """
    found: set[str] = {"127.0.0.1"}
    out = _run(["ip", "-br", "addr"], timeout=5)
    if out:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            iface = parts[0]
            if _VIRTUAL_IFACE_RE.match(iface):
                continue
            for token in parts[2:]:
                if "/" not in token or ":" in token:
                    continue  # skip IPv6 tokens
                addr = token.split("/", 1)[0]
                try:
                    parsed = ipaddress.ip_address(addr)
                except ValueError:
                    continue
                if isinstance(parsed, ipaddress.IPv4Address):
                    found.add(str(parsed))
        if len(found) > 1:
            return found
    try:
        hostname = socket.gethostname()
        found.add(socket.gethostbyname(hostname))
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            found.add(info[4][0])
    except Exception:
        pass
    try:
        # No packet is sent; connect() on UDP just picks the outbound route,
        # which is how we learn the address the LAN actually sees us on.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("10.255.255.255", 1))
            found.add(sock.getsockname()[0])
    except Exception:
        pass
    return found


def _own_mac() -> str:
    """Best-effort MAC of this host's own (non-virtual) primary interface.

    Reads the interface name from ``ip -br addr``, then its MAC from
    ``ip -br link`` or, failing that, ``/sys/class/net/<iface>/address``.
    Empty on the Windows dev box or when ``ip`` is unavailable.
    """
    out = _run(["ip", "-br", "addr"], timeout=5)
    iface = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        candidate = parts[0]
        if _VIRTUAL_IFACE_RE.match(candidate):
            continue
        if any("/" in token and ":" not in token for token in parts[2:]):
            iface = candidate
            break
    if not iface:
        return ""
    link_out = _run(["ip", "-br", "link", "show", iface], timeout=5)
    match = _MAC_RE.search(link_out)
    if match:
        return normalize_mac(match.group(0))
    try:
        sys_path = Path(f"/sys/class/net/{iface}/address")
        if sys_path.exists():
            return normalize_mac(sys_path.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    return ""


def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a read-only command, returning stdout or "" on any failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:
        logger.debug("%s unavailable: %s", cmd[0], exc)
        return ""
    return result.stdout or ""


def default_gateway() -> str:
    """The default-route gateway IP, or "" if it cannot be determined."""
    out = _run(["ip", "route", "show", "default"], timeout=5)
    match = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", out)
    if match:
        return match.group(1)
    # Windows dev box.
    out = _run(["route", "print", "0.0.0.0"], timeout=8)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            if _IP_RE.fullmatch(parts[2]):
                return parts[2]
    return ""


# ---------------------------------------------------------------------------
# Source: ARP / neighbour table
# ---------------------------------------------------------------------------

def _parse_ip_neigh(output: str) -> list[dict[str, str]]:
    """Parse ``ip neigh`` lines: ``<ip> dev <iface> lladdr <mac> <state>``."""
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        if not _IP_RE.fullmatch(ip):
            continue
        iface = ""
        mac = ""
        for idx, token in enumerate(parts):
            if token == "dev" and idx + 1 < len(parts):
                iface = parts[idx + 1]
            elif token == "lladdr" and idx + 1 < len(parts):
                mac = normalize_mac(parts[idx + 1])
        if _VIRTUAL_IFACE_RE.match(iface):
            continue
        if "FAILED" in line or "INCOMPLETE" in line:
            continue
        rows.append({"ip": ip, "mac": mac, "iface": iface})
    return rows


def _parse_arp_a(output: str) -> list[dict[str, str]]:
    """Parse ``arp -a`` output. Follows device_sentinel.py:395-404."""
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        ip = _IP_RE.search(line)
        mac = _MAC_RE.search(line)
        if not ip or not mac:
            continue
        rows.append({
            "ip": ip.group(1),
            "mac": normalize_mac(mac.group(0)),
            "iface": "",
        })
    return rows


def _scan_arp() -> list[dict[str, str]]:
    """Neighbour table as ``{ip, mac, iface}`` rows, LAN-filtered.

    ``ip neigh`` first (the Linux target; it also names the interface, which is
    how container bridges get dropped), ``arp -a`` second (the Windows dev box,
    and Linux hosts that still ship net-tools).
    """
    rows = _parse_ip_neigh(_run(["ip", "neigh", "show"]))
    if not rows:
        rows = _parse_arp_a(_run(["arp", "-a"]))
    return [
        row for row in rows
        if is_lan_ip(row["ip"]) and not _is_ignorable_mac(row["mac"])
    ]


# ---------------------------------------------------------------------------
# Source: mDNS (avahi)
# ---------------------------------------------------------------------------

_AVAHI_DECIMAL_ESCAPE_RE = re.compile(r"\\(\d{3})")


def _unescape_avahi(name: str) -> str:
    """Undo ``avahi-browse -p``'s escaping of names/hostnames/service types.

    ``-p`` (parseable output) escapes ``;`` and other bytes as ``\\NNN``
    decimal escapes (e.g. ``\\064`` for ``@``), and separately escapes a
    literal ``.`` as ``\\.`` and a literal ``\\`` as ``\\\\``. Applied to every
    name/hostname/service field pulled from that output.
    """
    if not name:
        return name
    text = _AVAHI_DECIMAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1))), name)
    text = text.replace("\\.", ".").replace("\\\\", "\\")
    return text


def _scan_mdns() -> dict[str, dict[str, Any]]:
    """Map IP -> ``{names, services}`` from ``avahi-browse -art -p``.

    The parseable (``-p``) resolved record is::

        =;eth0;IPv4;Living Room TV;_airplay._tcp;local;tv.local;192.168.50.20;7000;...

    Absent avahi (Windows dev, or a box without the daemon) this returns {} and
    the inventory simply carries no mDNS names.
    """
    out = _run(["avahi-browse", "-art", "-p", "-k"], timeout=20)
    if not out:
        return {}
    by_ip: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        if not line.startswith("="):
            continue
        fields = line.split(";")
        if len(fields) < 8:
            continue
        proto, name, service, host, ip = (
            fields[2], fields[3], fields[4], fields[6], fields[7])
        if proto != "IPv4" or not is_lan_ip(ip):
            continue
        entry = by_ip.setdefault(ip, {"names": set(), "services": set()})
        for candidate in (name, host):
            if candidate:
                entry["names"].add(_unescape_avahi(candidate).strip())
        if service:
            entry["services"].add(_unescape_avahi(service))
    return {
        ip: {"names": sorted(v["names"]), "services": sorted(v["services"])}
        for ip, v in by_ip.items()
    }


# ---------------------------------------------------------------------------
# Source: Home Assistant
# ---------------------------------------------------------------------------

#: Set by :func:`_ha_records` on every call, since it always swallows its own
#: exceptions and degrades to []. :meth:`BridgeInventory.refresh` reads these
#: right after calling it to populate ``sources.ha`` in :meth:`status`, so
#: the dashboard can show *why* ``ha_name`` is empty across a whole refresh,
#: not just that it is. ``_ha_last_method`` doubles as the success flag: it is
#: "" only when neither path produced records.
_ha_last_entity_count = 0
_ha_last_device_count = 0
_ha_last_error = ""
_ha_last_method = ""

#: WebSocket read budget. The registries are three small list commands.
_HA_WS_TIMEOUT = 20
#: Frames read while waiting for one command's result before giving up. HA
#: interleaves nothing on a fresh socket, but a bounded loop cannot hang the
#: refresher on a chatty or wedged connection.
_HA_WS_MAX_FRAMES = 200

#: A bare 12-hex-digit device-registry identifier (ESPHome, Shelly, Tasmota all
#: use the separator-less MAC as their identifier) — the only identifier shape
#: worth reading as a MAC. A guess here is harmless: it can only ever join to a
#: MAC the ARP table actually saw.
_BARE_MAC_RE = re.compile(r"^[0-9a-fA-F]{12}$")
#: A 12-hex run ANYWHERE in a string, not anchored: Home Assistant's device
#: identifiers embed the MAC rather than being it — ``("esphome",
#: "24a160aabbcc")``, ``("shelly", "shellyplug-s-AABBCCDDEEFF")``,
#: ``("tasmota", "tasmota_AABBCCDDEEFF_switch")``, ``("tuya", "…aabbccddeeff")``.
#: The lookarounds stop a 16-hex Zigbee IEEE address from being read as a MAC
#: with four characters shaved off either end.
_EMBEDDED_MAC_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{12})(?![0-9a-fA-F])")


def _coerce_mac(value: str) -> str:
    """Normalise a MAC written in ANY of the forms HA records it in.

    ``normalize_mac`` only accepts colon/dash-separated text. HA's
    ``connections`` carry lower-case colon form most of the time, but
    integrations also write ``aa-bb-cc-dd-ee-ff``, ``aabbccddeeff`` and
    ``AA:BB:CC:DD:EE:FF``. All four are the same address, and the separator
    is the reason a device did not join.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    direct = normalize_mac(raw)
    if direct:
        return direct
    stripped = re.sub(r"[^0-9a-fA-F]", "", raw)
    if _BARE_MAC_RE.fullmatch(stripped):
        return normalize_mac(":".join(stripped[i:i + 2] for i in range(0, 12, 2)))
    return ""


def _embedded_mac(value: str) -> str:
    """MAC embedded in an identifier string, or ""."""
    raw = (value or "").strip()
    if not raw:
        return ""
    for candidate in (raw, re.sub(r"[^0-9a-fA-F]", "", raw)):
        match = _EMBEDDED_MAC_RE.search(candidate)
        if match:
            got = normalize_mac(":".join(
                match.group(1)[i:i + 2] for i in range(0, 12, 2)))
            if got:
                return got
    return ""


def _url_ip(url: str) -> str:
    """The IPv4 address inside a ``configuration_url``, or "".

    Many integrations set ``configuration_url`` to the device's own web UI —
    ``http://192.168.50.53/`` — which is the only address HA records for them.
    A hostname URL yields "" on purpose: a name is joined by the name rule,
    where the one-row-only guard applies.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    host = raw.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
    host = host.split(":", 1)[0]
    return host if _IP_RE.fullmatch(host) else ""


def _name_key(name: str) -> str:
    """Case- and separator-insensitive form of a device name, or ""."""
    folded = re.sub(r"[\s_\-]+", "", (name or "").strip().lower())
    if folded.endswith(".local"):
        folded = folded[:-len(".local")]
    return folded


def _ha_resolve() -> tuple[str, str, bool]:
    """(url, token, enabled) through the one chain the app already uses.

    Thin seam over :func:`puffpai.ha_events._resolve_ha` so tests can stub the
    credential lookup without reaching the vault.
    """
    from puffpai.ha_events import _resolve_ha
    return _resolve_ha()


def _ha_ws_connect(url: str):
    """Open the HA WebSocket. Split out so tests can inject a fake socket."""
    import websocket  # imported lazily so the package stays optional
    return websocket.create_connection(url, timeout=_HA_WS_TIMEOUT)


def _ws_result(ws, msg_id: int, command: str) -> list[Any]:
    """Send one HA WS command and return its ``result`` list."""
    ws.send(json.dumps({"id": msg_id, "type": command}))
    for _ in range(_HA_WS_MAX_FRAMES):
        raw = ws.recv()
        if not raw:
            break
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if message.get("id") != msg_id or message.get("type") != "result":
            continue
        if not message.get("success"):
            code = (message.get("error") or {}).get("code") or "unknown"
            raise RuntimeError(f"{command} refused: {code}")
        result = message.get("result")
        return list(result) if isinstance(result, list) else []
    raise RuntimeError(f"{command}: no result frame")


def _registry_records(devices: Iterable[Any], entities: Iterable[Any],
                      areas: Iterable[Any]) -> list[dict[str, Any]]:
    """Fold HA's three registries into joinable ``{mac, ip, name, area, ...}``.

    The device registry is the only place HA reliably records a device's MAC
    (``connections: [["mac", "aa:bb:.."]]``) — entity *attributes* almost never
    carry one, which is why an attribute scrape matches almost nothing.
    """
    area_names: dict[str, str] = {}
    for area in areas:
        if not isinstance(area, dict):
            continue
        area_id = area.get("area_id") or area.get("id") or ""
        if area_id:
            area_names[str(area_id)] = str(area.get("name") or "")

    entities_by_device: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        device_id = entity.get("device_id")
        if device_id:
            entities_by_device.setdefault(str(device_id), []).append(entity)

    records: list[dict[str, Any]] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        mac = ""
        ip = ""
        # Where the MAC came from, so the merge can say WHICH rule joined a
        # device (``ha_match``). A MAC dug out of an identifier is a weaker
        # claim than one HA recorded as a connection, and the owner reading
        # the roll deserves to be told which it was.
        mac_from_identifier = False
        for conn in device.get("connections") or []:
            if not isinstance(conn, (list, tuple)) or len(conn) != 2:
                continue
            kind, value = str(conn[0]).lower(), str(conn[1]).strip()
            if kind == "mac":
                if not mac:
                    mac = _coerce_mac(value)
            elif kind in ("ip", "ipv4", "host", "hostname"):
                # ``host`` connections are usually an address and sometimes a
                # name; only an address is joinable here.
                if not ip and _IP_RE.fullmatch(value):
                    ip = value
        for ident in device.get("identifiers") or []:
            if not isinstance(ident, (list, tuple)):
                continue
            for part in ident:
                value = str(part).strip()
                if not ip and _IP_RE.fullmatch(value):
                    ip = value
                    continue
                if not mac:
                    embedded = _embedded_mac(value)
                    if embedded:
                        mac = embedded
                        mac_from_identifier = True
        if not ip:
            # Last address of all: the device's own web UI. For a whole class
            # of integrations (printers, Shelly, Hue bridges, cameras) this is
            # the ONLY address HA ever records.
            ip = _url_ip(str(device.get("configuration_url") or ""))

        name = str(device.get("name_by_user") or device.get("name") or "")
        # A record with no address at all is kept: dropping it would make
        # the name rule impossible, and many HA devices are known to a
        # household only by name.
        if not mac and not ip and not _name_key(name):
            continue

        own_entities = entities_by_device.get(str(device.get("id") or ""), [])
        entity_ids = sorted(
            str(e.get("entity_id")) for e in own_entities if e.get("entity_id"))
        area_id = str(device.get("area_id") or "")
        if not area_id:
            # A device with no area of its own can still have entities that
            # were placed in one individually.
            for entity in own_entities:
                if entity.get("area_id"):
                    area_id = str(entity["area_id"])
                    break
        records.append({
            "mac": mac,
            "mac_from_identifier": mac_from_identifier,
            "ip": ip,
            "entity_id": entity_ids[0] if entity_ids else "",
            "entity_ids": entity_ids,
            "name": name,
            "area": area_names.get(area_id, ""),
            "manufacturer": str(device.get("manufacturer") or ""),
            "model": str(device.get("model") or ""),
        })
    return records


def _ha_registry_records() -> tuple[list[dict[str, Any]], int, int]:
    """``(records, device_count, entity_count)`` from HA's WebSocket registries.

    Raises on anything that stops it — the caller turns that into the
    attribute-scrape fallback and a recorded error type.
    """
    url, token, enabled = _ha_resolve()
    if not enabled or not url or not token:
        raise RuntimeError("Home Assistant is not configured")
    from puffpai.ha_events import ws_url_for
    ws = _ha_ws_connect(ws_url_for(url))
    try:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected HA greeting: {hello.get('type')!r}")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            # Never echo the response: an auth failure body can carry the token.
            raise RuntimeError("HA websocket authentication failed")
        devices = _ws_result(ws, 1, "config/device_registry/list")
        entities = _ws_result(ws, 2, "config/entity_registry/list")
        areas = _ws_result(ws, 3, "config/area_registry/list")
        # Floors arrived in HA 2024.4 and areas carry a ``floor_id`` only from
        # there on. Optional on purpose: an older hub answers this command with
        # an error, and losing the three registries that DID arrive because the
        # house has no floors would be the worse trade.
        try:
            floors = _ws_result(ws, 4, "config/floor_registry/list")
        except Exception as exc:
            logger.debug("HA floor registry unavailable: %s", exc)
            floors = None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    # Hand the raw registries to the bucket view before folding them. It needs
    # fields this fold drops (the entity ``platform``, every entity id, the
    # areas by id) and must not open a second WebSocket of its own to get them:
    # the token is vault-gated and one pull per refresh is the budget.
    try:
        from puffpai import bridge_ha
        bridge_ha.set_registries(devices, entities, areas, floors)
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("HA bucket cache not updated: %s", exc)
    return (_registry_records(devices, entities, areas),
            len(devices), len(entities))


def _ha_attribute_records() -> tuple[list[dict[str, Any]], int]:
    """The legacy fallback: scrape MAC/IP out of entity *attributes*.

    Uses :meth:`puffpai.ha_bridge.HABridge.list_devices` with
    ``controllable_only=False`` so sensors and device-trackers — the entities
    most likely to carry such an attribute at all — are included.
    """
    from puffpai.ha_bridge import get_bridge
    devices = get_bridge().list_devices(controllable_only=False)
    records: list[dict[str, Any]] = []
    for dev in devices:
        attrs = dev.attributes or {}
        mac = ""
        ip = ""
        for key, value in attrs.items():
            if not isinstance(value, str):
                continue
            lowered = key.lower()
            if not mac and "mac" in lowered:
                mac = normalize_mac(value)
            if not ip and ("ip" in lowered and "zip" not in lowered):
                if _IP_RE.fullmatch(value.strip()):
                    ip = value.strip()
        if not mac and not ip:
            continue
        records.append({
            "mac": mac,
            "ip": ip,
            "entity_id": dev.entity_id,
            "entity_ids": [dev.entity_id],
            "name": dev.friendly_name or dev.entity_id,
            "area": dev.area_name or "",
            "manufacturer": "",
            "model": "",
        })
    return records, len(devices)


def _ha_records() -> list[dict[str, Any]]:
    """HA devices that expose a MAC or IP, with their name, area and make.

    Registry-first (WebSocket ``config/*_registry/list``), attribute-scrape
    second. Any failure degrades to the next path and finally to an empty
    list; the reason is recorded in the module globals above so
    ``sources.ha`` can say *why* rather than just showing zero.
    """
    global _ha_last_entity_count, _ha_last_device_count
    global _ha_last_error, _ha_last_method
    _ha_last_error = ""
    _ha_last_method = ""
    _ha_last_entity_count = 0
    _ha_last_device_count = 0

    try:
        records, device_count, entity_count = _ha_registry_records()
    except Exception as exc:
        logger.info("HA registry (websocket) unavailable: %s", exc)
        _ha_last_error = type(exc).__name__
    else:
        _ha_last_method = "registry"
        _ha_last_device_count = device_count
        _ha_last_entity_count = entity_count
        return records

    try:
        records, entity_count = _ha_attribute_records()
    except Exception as exc:
        logger.info("HA attribute scrape unavailable: %s", exc)
        _ha_last_error = _ha_last_error or type(exc).__name__
        return []
    _ha_last_method = "attributes"
    _ha_last_entity_count = entity_count
    return records


# ---------------------------------------------------------------------------
# Source: AdGuard client list
# ---------------------------------------------------------------------------

#: Set by :func:`_adguard_records` on every call, for ``sources.adguard``.
#: ``ok`` is "we reached AdGuard", NOT "AdGuard had clients": an install often
#: has zero *persistent* clients (nobody has named one in the DNS console), and
#: reporting that as ``ok: false`` would read as "AdGuard is broken" while it
#: answers 200.
_adguard_last_error = "disabled"
_adguard_last_persistent = 0
_adguard_last_auto = 0


def _adguard_settings() -> tuple[str, str, str] | None:
    """(base_url, username, password), or None when Zone 3 is off."""
    try:
        from puffpai.config import get_config
        zone3 = getattr(get_config(), "zone3", None)
    except Exception as exc:
        logger.info("Zone 3 config unavailable: %s", exc)
        return None
    if zone3 is None or not getattr(zone3, "enabled", False):
        return None
    return (
        str(getattr(zone3, "adguard_url", "")).rstrip("/"),
        str(getattr(zone3, "adguard_username", "")),
        str(getattr(zone3, "adguard_password", "")),
    )


def _adguard_records() -> tuple[dict[str, str], list[dict[str, str]]]:
    """``(persistent id -> name, auto_clients)`` from ``/control/clients``.

    Read directly rather than through
    :meth:`~puffpai.zone3.adguard_controller.AdGuardController.clients`, which
    narrows the same response to its ``clients`` list and drops
    ``auto_clients`` — the runtime clients AdGuard discovered from actual DNS
    activity, and often the only populated list of the two. Same
    read-only ``GET`` and same basic auth the controller uses; nothing is
    written. (:mod:`puffpai.bridge_ledger` reads ``/control/querylog`` the same
    way, and for the same reason.)
    """
    global _adguard_last_error, _adguard_last_persistent, _adguard_last_auto
    _adguard_last_error = "disabled"
    _adguard_last_persistent = 0
    _adguard_last_auto = 0

    settings = _adguard_settings()
    if settings is None:
        return {}, []
    base_url, username, password = settings
    if not base_url:
        _adguard_last_error = "NoAdGuardURL"
        return {}, []
    try:
        from puffpai.security.lan_http import lan_get
        # AdGuard's control API (config `zone3.adguard_url`, 127.0.0.1:3000) —
        # read-only client list. `lan_get` resolves the configured URL and
        # refuses anything that is not loopback/RFC1918/ULA before the socket,
        # so an edited config cannot turn device naming into an outbound call.
        resp = lan_get(f"{base_url}/control/clients",
                       auth=(username, password) if username else None,
                       timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.info("AdGuard client list unavailable: %s", exc)
        _adguard_last_error = type(exc).__name__
        return {}, []
    if not isinstance(payload, dict):
        _adguard_last_error = "UnexpectedPayload"
        return {}, []

    _adguard_last_error = ""
    mapping: dict[str, str] = {}
    for client in payload.get("clients") or []:
        if not isinstance(client, dict):
            continue
        _adguard_last_persistent += 1
        name = str(client.get("name") or "")
        for ident in client.get("ids") or []:
            if isinstance(ident, str) and ident:
                mapping[ident.upper() if ":" in ident else ident] = name

    auto: list[dict[str, str]] = []
    for client in payload.get("auto_clients") or []:
        if not isinstance(client, dict):
            continue
        _adguard_last_auto += 1
        ip = str(client.get("ip") or "").strip()
        if not ip:
            continue
        auto.append({
            "ip": ip,
            "name": str(client.get("name") or ""),
            "source": str(client.get("source") or ""),
        })
    return mapping, auto


# ---------------------------------------------------------------------------
# Merged record
# ---------------------------------------------------------------------------

@dataclass
class BridgeDevice:
    """One thing on the LAN, as seen by every source that can see it."""

    key: str                       # MAC when we have one, else "ip:<addr>"
    ip: str = ""
    mac: str = ""
    hostname: str = ""
    mdns_names: list[str] = field(default_factory=list)
    mdns_services: list[str] = field(default_factory=list)
    ha_entity_id: str = ""
    ha_name: str = ""
    ha_area: str = ""
    #: Which join rule matched Home Assistant to this row:
    #: "mac" | "ip" | "identifier" | "name", or "" when HA never saw it.
    ha_match: str = ""
    adguard_name: str = ""
    #: True for the Home Assistant host itself. It is not one device among the
    #: eighteen: it is the only route to the ~100 that have no address of their
    #: own, and a roll that lists it as an ordinary row hides that.
    hub: bool = False
    #: How many devices that hub manages, when it is one.
    manages: int = 0
    #: ``tier:integration`` of the Home-Assistant bucket this row belongs to
    #: (see :mod:`puffpai.bridge_ha`), or "" when HA never joined it.
    bucket: str = ""
    manufacturer: str = ""
    category: str = "unknown"
    classified_by: str = ""
    first_seen: str = ""
    last_seen: str = ""
    seen_by: list[str] = field(default_factory=list)
    is_gateway: bool = False
    is_self: bool = False

    @property
    def display_name(self) -> str:
        """Best human label available, in descending order of trust."""
        return (self.ha_name or self.adguard_name
                or (self.mdns_names[0] if self.mdns_names else "")
                or self.hostname or self.ip or self.mac or self.key)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["display_name"] = self.display_name
        return data


def _classify(mac: str, hostname: str,
              mdns_services: list[str]) -> tuple[str, str]:
    """(manufacturer, category) from the shared Zone 3 classifier."""
    try:
        from puffpai.security.device_classifier import classify_device
        result = classify_device(
            mac=mac or "00:00:00:00:00:00",
            hostname=hostname or None,
            mdns_services=mdns_services or None,
        )
        return (result.manufacturer or "", result.category.value)
    except Exception as exc:
        logger.debug("classification failed for %s: %s", mac or hostname, exc)
        return ("", "unknown")


# ---------------------------------------------------------------------------
# Classification enrichment: layered on top of device_classifier, never
# inside it. The shared classifier is being extended concurrently elsewhere;
# this only fills gaps it leaves (empty manufacturer, "unknown"/"computer"
# category) using a small local OUI table and hostname/mDNS heuristics.
# ---------------------------------------------------------------------------

#: A few well-known OUI prefixes the shared classifier does not carry.
#: Deliberately small and deliberately conservative: an OUI we are not sure
#: of is left out rather than guessed at.
_OUI_MANUFACTURERS: dict[str, str] = {
    "D8:EC:5E": "Linksys",
    "2C:CF:67": "Raspberry Pi",
    "AC:AE:19": "Roku",
}

_PRINTER_HOSTNAME_RE = re.compile(r"^(Canon|HP|Epson|Brother)", re.I)
_PRINTER_SERVICES = {"_ipp._tcp", "_printer._tcp", "_pdl-datastream._tcp"}
_LINKSYS_HOSTNAME_RE = re.compile(r"^(Linksys\d+|myrouter)", re.I)
_APPLE_SERVICES = {"_airplay._tcp", "_raop._tcp"}
_APPLE_NAME_RE = re.compile(r"apple|iphone|ipad|macbook|imac|appletv", re.I)


def _is_locally_administered_mac(mac: str) -> bool:
    """True when the MAC's U/L bit marks it as software-randomised.

    Per 802: the second hex digit of the first octet is one of
    ``2``/``6``/``A``/``E`` for a locally-administered (i.e. randomised, not
    factory-assigned) address. An OUI lookup on such a MAC is meaningless.
    """
    if not mac or ":" not in mac:
        return False
    first_octet = mac.split(":", 1)[0]
    if len(first_octet) != 2:
        return False
    return first_octet[1].upper() in ("2", "6", "A", "E")


def _oui_manufacturer(mac: str) -> str:
    if not mac:
        return ""
    prefix = mac.upper()[:8]
    return _OUI_MANUFACTURERS.get(prefix, "")


def _ha_url_host() -> str:
    """Host portion of the configured Home Assistant URL, or "" on any failure."""
    try:
        from puffpai.config import get_config
        cfg = get_config()
        for attr in ("ha_url", "home_assistant_url", "url", "base_url"):
            candidate = getattr(getattr(cfg, "ha", None), attr, None)
            if candidate:
                break
        else:
            candidate = None
        if not candidate:
            return ""
        from urllib.parse import urlparse
        return (urlparse(str(candidate)).hostname or "").strip()
    except Exception:
        return ""


def _enrich_classification(device: "BridgeDevice") -> None:
    """Fill manufacturer/category gaps the shared classifier leaves.

    Runs after :func:`_classify` has already populated
    ``device.manufacturer``/``device.category``, and records which rule (if
    any) fired as ``device.classified_by``.
    """
    classified_by = "classifier" if (device.manufacturer or
                                      device.category not in ("unknown", "computer")) else ""

    if not device.manufacturer and device.mac:
        if _is_locally_administered_mac(device.mac):
            device.manufacturer = "(randomised MAC)"
            classified_by = classified_by or "oui"
        else:
            oui = _oui_manufacturer(device.mac)
            if oui:
                device.manufacturer = oui
                classified_by = "oui"

    if device.category in ("unknown", "computer"):
        hostname = device.hostname or ""
        services = set(device.mdns_services or [])
        names = " ".join([hostname, *device.mdns_names])

        matched_printer_service = next(
            (s for s in _PRINTER_SERVICES if s in services), None)
        ha_host = _ha_url_host()

        if _PRINTER_HOSTNAME_RE.match(hostname):
            device.category = "printer"
            classified_by = "hostname"
        elif matched_printer_service:
            device.category = "printer"
            classified_by = f"mdns:{matched_printer_service}"
        elif _LINKSYS_HOSTNAME_RE.match(hostname):
            device.category = "network_infra"
            classified_by = "hostname"
        elif "homeassistant" in hostname.lower() or (
                ha_host and device.ip == ha_host):
            device.category = "home_assistant"
            device.manufacturer = device.manufacturer or "Home Assistant"
            classified_by = "hostname"
        elif (services & _APPLE_SERVICES) and _APPLE_NAME_RE.search(names):
            device.category = "streaming_box"
            device.manufacturer = device.manufacturer or "Apple"
            classified_by = f"mdns:{sorted(services & _APPLE_SERVICES)[0]}"
        elif "_googlecast._tcp" in services:
            device.category = "streaming_box"
            device.manufacturer = device.manufacturer or "Google"
            classified_by = "mdns:_googlecast._tcp"
        elif "_spotify-connect._tcp" in services:
            device.category = "smart_speaker"
            classified_by = "mdns:_spotify-connect._tcp"
        elif "_roku._tcp" in services:
            device.category = "streaming_box"
            device.manufacturer = device.manufacturer or "Roku"
            classified_by = "mdns:_roku._tcp"
        elif _oui_manufacturer(device.mac) == "Roku":
            device.category = "streaming_box"
            device.manufacturer = device.manufacturer or "Roku"
            classified_by = "oui"

    device.classified_by = classified_by


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class BridgeInventory:
    """Merged, persisted, observe-only device roll.

    Thread-safe: :meth:`refresh` holds a lock, and readers take the same lock
    so the background refresher cannot hand out a half-merged roll.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._devices: dict[str, BridgeDevice] = {}
        self._last_refresh: str = ""
        self._source_health: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        path = devices_file()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("bridge_devices.json corrupt; starting fresh")
            return
        for item in raw.get("devices", []):
            try:
                item.pop("display_name", None)
                device = BridgeDevice(**item)
            except TypeError:
                continue
            self._devices[device.key] = device
        self._last_refresh = raw.get("last_refresh", "")

    def _save(self) -> None:
        path = devices_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_refresh": self._last_refresh,
            "devices": [d.to_dict() for d in self._devices.values()],
        }
        tmp = path.with_suffix(".json.tmp")
        # 0600: this file holds per-device MAC addresses and domain history.
        # os.open + O_CREAT with an explicit mode avoids a window where the
        # file briefly exists at the process umask before being chmod'd.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
        tmp.replace(path)

    # -- the merge -----------------------------------------------------------

    def refresh(self) -> list[dict[str, Any]]:
        """Re-scan every source and merge into the persisted roll.

        Merge order is ARP → mDNS → HA → AdGuard, keyed by MAC and falling back
        to ``ip:<addr>`` when a source gives no MAC. ``first_seen`` is sticky:
        a device that was already in the roll keeps its original timestamp
        forever, which is what makes "N new in 24h" mean anything.
        """
        now = _utcnow()
        health: dict[str, Any] = {}

        def _bail() -> list[dict[str, Any]]:
            """Stop was requested mid-scan: abort without merging or writing."""
            with self._lock:
                return [d.to_dict() for d in self._sorted()]

        global _virtual_subnets_cache
        _virtual_subnets_cache = _virtual_subnets()

        if self._stop.is_set():
            return _bail()
        arp_rows = _scan_arp()
        health["arp"] = {"ok": bool(arp_rows), "count": len(arp_rows)}

        if self._stop.is_set():
            return _bail()
        mdns = _scan_mdns()
        health["mdns"] = {"ok": bool(mdns), "count": len(mdns)}

        if self._stop.is_set():
            return _bail()
        ha_rows = _ha_records()
        # ``matched`` is filled in after the merge: it counts HA records that
        # actually joined a device on the LAN, which is the number that says
        # whether the join key works — not how many records had *some* address.
        health["ha"] = {
            "ok": bool(_ha_last_method),
            "entities": _ha_last_entity_count,
            "devices": _ha_last_device_count,
            "matched": 0,
            # Filled in by the merge; declared here so a pass that bails
            # early still reports the shape the dashboard reads.
            "unmatched": {"no_address": 0, "address_not_on_lan": 0,
                          "no_inventory_row": 0},
            "method": _ha_last_method,
            "error": _ha_last_error,
        }

        if self._stop.is_set():
            return _bail()
        adguard, adguard_auto = _adguard_records()
        health["adguard"] = {
            "ok": _adguard_last_error == "",
            "persistent": _adguard_last_persistent,
            "auto": _adguard_last_auto,
            "error": _adguard_last_error,
        }

        if self._stop.is_set():
            return _bail()

        gateway = default_gateway()
        mine = _own_addresses()
        own_mac = _own_mac()

        with self._lock:
            # 1. ARP is the spine: it is the only source that reliably pairs an
            #    IP with a MAC, so it decides each device's key.
            ip_to_key: dict[str, str] = {}
            for row in arp_rows:
                key = row["mac"] or f"ip:{row['ip']}"
                device = self._devices.get(key) or BridgeDevice(
                    key=key, first_seen=now)
                device.ip = row["ip"]
                device.mac = row["mac"]
                device.last_seen = now
                device.is_gateway = bool(gateway) and row["ip"] == gateway
                device.is_self = row["ip"] in mine
                self._mark(device, "arp")
                self._devices[key] = device
                ip_to_key[row["ip"]] = key

            # 2. mDNS: names and service types, joined on IP.
            for ip, info in mdns.items():
                key = ip_to_key.get(ip) or f"ip:{ip}"
                device = self._devices.get(key) or BridgeDevice(
                    key=key, ip=ip, first_seen=now)
                device.ip = device.ip or ip
                device.mdns_names = info["names"]
                device.mdns_services = info["services"]
                if not device.hostname and info["names"]:
                    device.hostname = info["names"][0]
                device.last_seen = now
                self._mark(device, "mdns")
                self._devices[key] = device
                ip_to_key.setdefault(ip, key)

            # 3. Home Assistant: join on MAC when the entity exposes one,
            #    otherwise on IP. HA contributes the friendly name and the area,
            #    which is the label a person actually recognises.
            #    Four rules, strongest first, because HA records an address
            #    for far fewer devices than it has: a MAC/IP join alone can
            #    match only one device in dozens (typically the router), while
            #    the rest are on the LAN under a key that join does not look
            #    at.
            #
            #    A name-only join is last and deliberately timid: it fires
            #    only when exactly ONE inventory row answers to that name, so
            #    "Bedroom" (three rows on this network) never joins by name
            #    and a wrong label is preferred not-shown over shown.
            name_to_keys: dict[str, set[str]] = {}
            for device in self._devices.values():
                for candidate in ([device.hostname] + list(device.mdns_names)):
                    folded = _name_key(candidate)
                    if folded:
                        name_to_keys.setdefault(folded, set()).add(device.key)

            ha_makes: dict[str, str] = {}
            unmatched = {"no_address": 0, "address_not_on_lan": 0,
                         "no_inventory_row": 0}
            for record in ha_rows:
                key = ""
                rule = ""
                mac = record.get("mac") or ""
                ip = record.get("ip") or ""
                if mac and mac in self._devices:
                    key = mac
                    rule = ("identifier"
                            if record.get("mac_from_identifier") else "mac")
                elif ip and ip in ip_to_key:
                    key = ip_to_key[ip]
                    rule = "ip"
                else:
                    named = name_to_keys.get(_name_key(record.get("name") or ""))
                    if named and len(named) == 1:
                        key = next(iter(named))
                        rule = "name"
                if not key or key not in self._devices:
                    # Why it did not join — the owner's only way to tell an
                    # integration with no address from a broken join key.
                    if not mac and not ip:
                        unmatched["no_address"] += 1
                    elif ip and not is_lan_ip(ip):
                        unmatched["address_not_on_lan"] += 1
                    else:
                        unmatched["no_inventory_row"] += 1
                    continue
                device = self._devices[key]
                device.ha_entity_id = record["entity_id"]
                device.ha_name = record["name"]
                device.ha_area = record["area"]
                device.ha_match = rule
                if record.get("manufacturer"):
                    ha_makes[key] = str(record["manufacturer"])
                self._mark(device, "ha")
                health["ha"]["matched"] += 1
            health["ha"]["unmatched"] = unmatched

            # 4. AdGuard: the client name the owner already typed into the DNS
            #    console, joined on either identifier form.
            for ident, name in adguard.items():
                key = ident if ident in self._devices else ip_to_key.get(ident, "")
                if not key or key not in self._devices:
                    continue
                device = self._devices[key]
                device.adguard_name = name
                self._mark(device, "adguard")

            # 4b. AdGuard's auto-discovered clients: things that are actively
            #     resolving DNS. A device that answers no ARP probe and
            #     advertises no mDNS is still *on the network* if it is asking
            #     this box to resolve names, so it gets a row of its own.
            for row in adguard_auto:
                ip = row["ip"]
                if not is_lan_ip(ip):
                    continue
                key = ip_to_key.get(ip) or f"ip:{ip}"
                device = self._devices.get(key) or BridgeDevice(
                    key=key, ip=ip, first_seen=now)
                device.ip = device.ip or ip
                device.last_seen = now
                if row["name"] and not device.adguard_name:
                    device.adguard_name = row["name"]
                self._mark(device, "adguard")
                self._devices[key] = device
                ip_to_key.setdefault(ip, key)

            # 5. Classify last, so hostname and mDNS hints are already merged.
            for device in self._devices.values():
                manufacturer, category = _classify(
                    device.mac, device.hostname or device.ha_name,
                    device.mdns_services)
                device.manufacturer = manufacturer
                device.category = category
                _enrich_classification(device)
                # HA's device registry knows the make for anything it has
                # integrated; use it only where nothing else could say.
                if not device.manufacturer and ha_makes.get(device.key):
                    device.manufacturer = ha_makes[device.key]
                    device.classified_by = device.classified_by or "ha"

            # 6. Self: mark this host's own device(s) even when ARP never sees
            #    them (a host has no neighbour-table entry for itself), fill a
            #    missing MAC from ``ip -br link``, and override the category —
            #    this runs after classification on purpose so it wins.
            for ip in mine:
                if ip == "127.0.0.1":
                    continue
                key = ip_to_key.get(ip)
                if key is None:
                    key = (own_mac if own_mac else f"ip:{ip}")
                    device = self._devices.get(key) or BridgeDevice(
                        key=key, ip=ip, first_seen=now)
                    device.ip = device.ip or ip
                    ip_to_key[ip] = key
                else:
                    device = self._devices[key]
                device.is_self = True
                if not device.mac and own_mac:
                    device.mac = own_mac
                device.last_seen = now
                device.category = "puffpai"
                device.classified_by = "self"
                self._mark(device, "self")
                self._devices[key] = device

            # 7. The hub, and which bucket each joined row belongs to. Alex
            #    sees eighteen rows and owns about a hundred devices; the
            #    hundred are real, they simply have no IP to be seen at. Both
            #    facts belong on the roll: the HA host is labelled as the hub
            #    and told how many it manages, and every row HA joined carries
            #    the bucket it also appears in on the managed view.
            #
            #    ``bucket_for_devices`` is pure over the registry cache on
            #    purpose — anything that read back through ``devices()`` here
            #    would re-enter this same lock.
            try:
                from puffpai import bridge_ha
                linked = {d.ha_entity_id for d in self._devices.values()
                          if d.ha_match and d.ha_entity_id}
                bucket_of = bridge_ha.bucket_for_devices(linked)
                hub_host = bridge_ha.ha_host()
            except Exception as exc:
                logger.info("HA bucket stamping skipped: %s", exc)
                bucket_of, hub_host = {}, ""
            for device in self._devices.values():
                device.bucket = (bucket_of.get(device.ha_entity_id, "")
                                 if device.ha_match else "")
                if hub_host and device.ip == hub_host:
                    device.hub = True
                    device.manages = _ha_last_device_count

            if self._stop.is_set():
                # Stop was requested mid-merge: the in-memory devices dict may
                # already carry this pass's rows, but a write after stop is
                # exactly the orphan-write this check exists to prevent.
                return [d.to_dict() for d in self._sorted()]

            self._last_refresh = now
            self._source_health = health
            self._save()
            return [d.to_dict() for d in self._sorted()]

    @staticmethod
    def _mark(device: BridgeDevice, source: str) -> None:
        if source not in device.seen_by:
            device.seen_by = sorted(device.seen_by + [source])

    def _sorted(self) -> list[BridgeDevice]:
        return sorted(
            self._devices.values(),
            key=lambda d: (not d.is_gateway, d.ha_area or "￿",
                           d.display_name.lower()))

    # -- readers -------------------------------------------------------------

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [d.to_dict() for d in self._sorted()]

    def get(self, key: str) -> dict[str, Any] | None:
        """Look up by MAC (case-insensitive), inventory key, or IP."""
        with self._lock:
            wanted = (key or "").strip()
            device = (self._devices.get(normalize_mac(wanted) or wanted)
                      or self._devices.get(f"ip:{wanted}"))
            if device is None:
                for candidate in self._devices.values():
                    if candidate.ip == wanted:
                        device = candidate
                        break
            return device.to_dict() if device else None

    def key_for_ip(self, ip: str) -> str:
        """Inventory key (MAC when known) for a client IP, or ""."""
        with self._lock:
            for device in self._devices.values():
                if device.ip == ip:
                    return device.key
        return ""

    def mac_for_ip(self, ip: str) -> str:
        """MAC for a client IP, or "" when the device has no known MAC."""
        with self._lock:
            for device in self._devices.values():
                if device.ip == ip:
                    return device.mac
        return ""

    def new_since(self, hours: int = 24) -> int:
        """Devices whose ``first_seen`` falls inside the window."""
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        count = 0
        with self._lock:
            for device in self._devices.values():
                stamp = _parse_ts(device.first_seen)
                if stamp is not None and stamp >= cutoff:
                    count += 1
        return count

    def status(self) -> dict[str, Any]:
        with self._lock:
            age = None
            stamp = _parse_ts(self._last_refresh)
            if stamp is not None:
                age = max(0, int(datetime.now(timezone.utc).timestamp() - stamp))
            return {
                "last_refresh": self._last_refresh,
                "inventory_age_seconds": age,
                "device_count": len(self._devices),
                "new_24h": self.new_since(24),
                "sources": dict(self._source_health),
                "refresher_running": bool(
                    self._thread and self._thread.is_alive()),
            }

    # -- background refresher ------------------------------------------------

    def start_refresher(self, interval: int = DEFAULT_REFRESH_SECONDS) -> bool:
        """Start the 5-minute re-scan thread. Idempotent; returns True if running."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, args=(max(30, int(interval)),),
                name="bridge-inventory", daemon=True)
            self._thread.start()
            logger.info("Bridge inventory refresher started (%ss)", interval)
            return True

    def stop_refresher(self, timeout: float = 5) -> bool:
        """Signal the refresher to stop and join it.

        Only clears ``_thread`` (and returns True) when the thread actually
        exited within ``timeout``. If it is still alive — e.g. blocked deep
        inside a slow source — ``_thread`` is left in place and a warning is
        logged, so a caller can never mistake a still-running thread for a
        stopped one and start writing again out from under it.
        """
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        if thread.is_alive():
            thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "bridge inventory refresher did not stop within %ss", timeout)
            return False
        self._thread = None
        return True

    def _loop(self, interval: int) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                logger.warning("bridge inventory refresh failed: %s", exc)
            self._stop.wait(interval)


def _parse_ts(value: str) -> float | None:
    if not value:
        return None
    from puffpai.bridge_ledger import _normalize_iso
    text = _normalize_iso(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Singleton + enablement
# ---------------------------------------------------------------------------

_inventory: BridgeInventory | None = None
_inventory_lock = threading.Lock()


def get_inventory() -> BridgeInventory:
    global _inventory
    if _inventory is None:
        with _inventory_lock:
            if _inventory is None:
                _inventory = BridgeInventory()
    return _inventory


def reset_inventory() -> None:
    """Drop the singleton. Tests only."""
    global _inventory
    with _inventory_lock:
        if _inventory is not None:
            _inventory.stop_refresher()
        _inventory = None


def is_enabled() -> bool:
    """True when the inventory refresher should run.

    Either the env flag or ``zone3.enabled`` is enough. The config is read
    through :func:`puffpai.config.get_config` and every field access is a
    ``getattr`` — this module must not require a ``Zone3Config`` change to
    import cleanly.
    """
    if os.environ.get(ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from puffpai.config import get_config
        return bool(getattr(getattr(get_config(), "zone3", None), "enabled", False))
    except Exception:
        return False


__all__ = [
    "BridgeDevice",
    "BridgeInventory",
    "DEFAULT_REFRESH_SECONDS",
    "ENABLE_ENV",
    "default_gateway",
    "devices_file",
    "get_inventory",
    "is_enabled",
    "is_lan_ip",
    "normalize_mac",
    "reset_inventory",
]
