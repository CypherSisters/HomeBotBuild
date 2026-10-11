"""
PuffPAI Home Assistant Device Watcher
======================================
Monitors Home Assistant for new entities and manages a device approval queue.

puff puff pass

Flow:
    1. Periodically polls HA for all entities
    2. Compares against known/approved device list
    3. New devices go into a pending approval queue
    4. User approves → classify device → apply Zone3 lockdown policy
    5. User rejects → optionally block via firewall

Usage:
    from puffpai.ha_device_watcher import get_watcher

    watcher = get_watcher()
    watcher.check_for_new_devices()       # Poll HA now
    pending = watcher.get_pending()        # Devices awaiting approval
    watcher.approve_device(entity_id, lockdown=True)
    watcher.reject_device(entity_id, block=True)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any

from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

# ── state location (lazy) ────────────────────────────────────────────
#
# Three files: the known-device list, the pending-approval queue, and the
# approval audit trail. None of them may live under the code tree, which a
# hardened unit mounts read-only; the approval log in particular is the only
# record of which HA entities the user let in, and a failed append there is a
# silently missing audit entry. They resolve on
# first use through the one resolver that defines $PUFFPAI_DATA_DIR
# semantics; the default (<repo>/data) is preserved verbatim.
#
# NOTE: security/consent_gate.py reads ha_device_approvals.jsonl as its
# LEGACY_APPROVALS and shares this <repo>/data default (file-level-adopting
# its own old state). If the two defaults differ, with the env var unset the
# reader looks somewhere the writer never wrote and entities covered only by
# the legacy blanket approval score a hard "pending" instead of the
# "legacy_blanket" flag. Both converge on the env root when $PUFFPAI_DATA_DIR
# is set. Keep this default and consent_gate's in step.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"

#: Cached state directory; ``None`` = unresolved. Resettable: assigning None
#: (or reloading the module) makes the next call re-resolve from the
#: environment, which the long-lived get_watcher() singleton otherwise never
#: does. An override below wins over this cache outright.
_data_dir_cache: Path | None = None

# The four historical constant names stay usable as test/ops override hooks:
# ``None`` means "resolve", any path pins that file. An override beats both the
# cache and the environment, so the long-lived get_watcher() singleton honours
# one set mid-life.
#
# Deliberately plain globals rather than a PEP 562 __getattr__ shim that
# synthesises a resolved path on read. monkeypatch.setattr records the old
# value with getattr, so against a shim it records a *resolved path* and
# re-assigns it at teardown -- a temporary override becomes permanent,
# $PUFFPAI_DATA_DIR stops being honoured for the rest of the process, and the
# approval trail quietly goes back to the read-only code tree. Observed live on
# another module during this migration.
_DATA_DIR = None
_KNOWN_DEVICES_FILE = None
_PENDING_DEVICES_FILE = None
_APPROVAL_LOG_FILE = None


def _override(name: str) -> Path | None:
    """Return an explicitly assigned module-level path, if there is one.

    str or Path both work -- this is an ops escape hatch, and one written the
    other way round should not silently resolve somewhere else instead.
    Nothing else counts: a bare ``MagicMock`` satisfies os.fspath (it answers
    ``__fspath__`` with a junk name), and honouring that would scatter the
    approval trail to a junk path relative to the CWD.
    """
    value = globals().get(name)
    if not isinstance(value, (str, PurePath)):
        return None
    return Path(value) if value else None


def _data_dir() -> Path:
    """Directory holding watcher state, resolved per call (cached, hookable).

    Lazy by construction: :mod:`puffpai.statedir` is a stdlib-only leaf, but
    the resolution it performs imports ``puffpai.config`` (which reaches the
    secrets provider), so it must not run at import time.
    """
    override = _override("_DATA_DIR")
    if override is not None:
        return override
    global _data_dir_cache
    if _data_dir_cache is None:
        _data_dir_cache = Path(resolve_runtime_dir(str(_DEFAULT_DATA_DIR)))
    return _data_dir_cache


def _known_devices_file() -> Path:
    override = _override("_KNOWN_DEVICES_FILE")
    return override if override is not None else _data_dir() / "ha_known_devices.json"


def _pending_devices_file() -> Path:
    override = _override("_PENDING_DEVICES_FILE")
    return override if override is not None else _data_dir() / "ha_pending_devices.json"


def _approval_log_file() -> Path:
    override = _override("_APPROVAL_LOG_FILE")
    return override if override is not None else _data_dir() / "ha_device_approvals.jsonl"


# Domains we watch for new devices (controllable smart home stuff)
WATCHED_DOMAINS = frozenset([
    "light", "switch", "fan", "cover", "climate", "media_player",
    "lock", "vacuum", "camera", "siren", "remote", "sensor",
    "binary_sensor", "number", "select", "button",
])

# Domains that are always auto-approved (internal HA stuff)
AUTO_APPROVE_DOMAINS = frozenset([
    "automation", "script", "scene", "input_boolean", "input_number",
    "input_select", "input_text", "input_datetime", "input_button",
    "counter", "timer", "schedule", "zone", "person", "sun",
    "weather", "update", "tts", "stt", "wake_word", "conversation",
    "assist_satellite", "todo", "event",
])

# Entity ID prefixes for HA-internal sensors that should be auto-approved
# (these aren't real smart home devices, just HA's own metrics)
_AUTO_APPROVE_SENSOR_PREFIXES = (
    "sensor.backup_", "sensor.sun_", "sensor.energy_",
    "sensor.hacs", "sensor.rpi_power",
)


@dataclass
class PendingDevice:
    """A device awaiting user approval."""
    entity_id: str
    domain: str
    friendly_name: str
    state: str
    manufacturer: str | None = None
    model: str | None = None
    suggested_action: str = "lockdown"  # lockdown, balanced, open, allow
    category: str = "unknown"
    discovered_at: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Don't persist huge attribute dicts
        d["attributes"] = {
            k: v for k, v in self.attributes.items()
            if k in ("friendly_name", "device_class", "manufacturer",
                     "model", "sw_version", "hw_version", "via_device",
                     "supported_color_modes", "supported_features")
        }
        return d


@dataclass
class KnownDevice:
    """An approved or rejected device."""
    entity_id: str
    domain: str
    friendly_name: str
    status: str  # "approved", "rejected", "auto"
    security_mode: str | None = None  # lockdown, balanced, open, allow
    approved_at: str = ""
    category: str = "unknown"


class HADeviceWatcher:
    """Watches Home Assistant for new entities and manages approval queue."""

    def __init__(self):
        self._lock = threading.Lock()
        self._known: dict[str, KnownDevice] = {}
        self._pending: dict[str, PendingDevice] = {}
        self._load_state()

    # -- persistence -----------------------------------------------------------

    def _load_state(self) -> None:
        """Load known and pending devices from disk."""
        _data_dir().mkdir(parents=True, exist_ok=True)

        known_file = _known_devices_file()
        if known_file.exists():
            try:
                with open(known_file, "r") as f:
                    data = json.load(f)
                for eid, d in data.items():
                    self._known[eid] = KnownDevice(**d)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Corrupt known devices file, starting fresh")

        pending_file = _pending_devices_file()
        if pending_file.exists():
            try:
                with open(pending_file, "r") as f:
                    data = json.load(f)
                for eid, d in data.items():
                    self._pending[eid] = PendingDevice(**d)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Corrupt pending devices file, starting fresh")

    def _save_known(self) -> None:
        # Make the parent of the file actually being written, not the resolved
        # data dir: with a per-file override the two differ, and the state root
        # of a relocated deployment may not exist yet.
        known_file = _known_devices_file()
        known_file.parent.mkdir(parents=True, exist_ok=True)
        with open(known_file, "w") as f:
            json.dump({eid: asdict(d) for eid, d in self._known.items()}, f, indent=2)

    def _save_pending(self) -> None:
        pending_file = _pending_devices_file()
        pending_file.parent.mkdir(parents=True, exist_ok=True)
        with open(pending_file, "w") as f:
            json.dump({eid: d.to_dict() for eid, d in self._pending.items()}, f, indent=2)

    def _log_approval(self, entity_id: str, action: str, details: dict) -> None:
        """Append to the approval audit log."""
        log_file = _approval_log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "entity_id": entity_id,
            "action": action,
            **details,
        }
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    # -- discovery -------------------------------------------------------------

    def check_for_new_devices(self) -> list[PendingDevice]:
        """Poll HA for entities and return any newly discovered devices.

        - Known (approved/rejected) devices are skipped
        - Auto-approve domains (automation, script, etc.) are silently added
        - Everything else goes into the pending queue
        """
        from puffpai.ha_bridge import get_bridge, HABridgeError

        bridge = get_bridge()
        try:
            states = bridge.get_all_states(force=True)
        except HABridgeError as exc:
            logger.error("Cannot poll HA for devices: %s", exc)
            return []

        new_pending: list[PendingDevice] = []

        with self._lock:
            for entity in states:
                eid = entity.get("entity_id", "")
                domain = eid.split(".")[0] if "." in eid else ""

                # Skip if already known or pending
                if eid in self._known or eid in self._pending:
                    continue

                # Auto-approve internal HA domains
                if domain in AUTO_APPROVE_DOMAINS:
                    self._known[eid] = KnownDevice(
                        entity_id=eid,
                        domain=domain,
                        friendly_name=entity.get("attributes", {}).get("friendly_name", eid),
                        status="auto",
                        approved_at=datetime.now().isoformat(),
                    )
                    continue

                # Auto-approve known HA-internal sensor prefixes
                if eid.startswith(_AUTO_APPROVE_SENSOR_PREFIXES):
                    self._known[eid] = KnownDevice(
                        entity_id=eid,
                        domain=domain,
                        friendly_name=entity.get("attributes", {}).get("friendly_name", eid),
                        status="auto",
                        approved_at=datetime.now().isoformat(),
                    )
                    continue

                # Skip domains we don't watch
                if domain not in WATCHED_DOMAINS:
                    # Still mark as known so we don't re-process
                    self._known[eid] = KnownDevice(
                        entity_id=eid,
                        domain=domain,
                        friendly_name=entity.get("attributes", {}).get("friendly_name", eid),
                        status="auto",
                        approved_at=datetime.now().isoformat(),
                    )
                    continue

                # New device that needs approval
                attrs = entity.get("attributes", {})
                manufacturer = attrs.get("manufacturer")
                model = attrs.get("model")
                friendly_name = attrs.get("friendly_name", eid)

                # Guess suggested action based on domain
                suggested = self._suggest_action(domain, manufacturer, friendly_name)

                pending = PendingDevice(
                    entity_id=eid,
                    domain=domain,
                    friendly_name=friendly_name,
                    state=entity.get("state", "unknown"),
                    manufacturer=manufacturer,
                    model=model,
                    suggested_action=suggested,
                    category=self._guess_category(domain, friendly_name),
                    discovered_at=datetime.now().isoformat(),
                    attributes=attrs,
                )
                self._pending[eid] = pending
                new_pending.append(pending)

            self._save_known()
            self._save_pending()

        if new_pending:
            logger.info(
                "Discovered %d new device(s): %s",
                len(new_pending),
                ", ".join(d.friendly_name for d in new_pending),
            )

        return new_pending

    def _suggest_action(self, domain: str, manufacturer: str | None, name: str) -> str:
        """Suggest a security action based on device type."""
        name_lower = (name or "").lower()
        mfg_lower = (manufacturer or "").lower()

        # IoT appliances → lockdown by default
        if domain in ("camera", "siren", "vacuum", "lock"):
            return "lockdown"

        # Smart speakers/media — balanced (need cloud for functionality)
        if domain == "media_player":
            if any(kw in name_lower for kw in ("echo", "alexa", "google", "homepod", "sonos")):
                return "balanced"
            return "lockdown"

        # Climate devices (smart thermostats) — balanced
        if domain == "climate":
            return "balanced"

        # Sensors — lockdown (they shouldn't phone home)
        if domain in ("sensor", "binary_sensor"):
            return "lockdown"

        # Lights, switches, fans — lockdown
        if domain in ("light", "switch", "fan", "cover"):
            return "lockdown"

        return "balanced"

    def _guess_category(self, domain: str, name: str) -> str:
        """Rough category guess for display purposes."""
        name_lower = (name or "").lower()
        mapping = {
            "light": "smart_light",
            "switch": "smart_switch",
            "camera": "security_camera",
            "climate": "thermostat",
            "lock": "smart_lock",
            "vacuum": "robot_vacuum",
            "fan": "smart_fan",
            "cover": "smart_cover",
            "media_player": "media_player",
            "siren": "siren",
            "sensor": "sensor",
            "binary_sensor": "sensor",
        }
        # Check for washer/dryer in name
        if any(kw in name_lower for kw in ("washer", "washing")):
            return "smart_washer"
        if any(kw in name_lower for kw in ("dryer", "drying")):
            return "smart_dryer"
        if any(kw in name_lower for kw in ("dishwasher",)):
            return "smart_dishwasher"
        if any(kw in name_lower for kw in ("fridge", "refrigerator")):
            return "smart_fridge"
        if any(kw in name_lower for kw in ("oven", "range", "stove")):
            return "smart_oven"

        return mapping.get(domain, "unknown")

    # -- approval actions ------------------------------------------------------

    def get_pending(self) -> list[PendingDevice]:
        """Return all devices awaiting approval."""
        with self._lock:
            return list(self._pending.values())

    def get_pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def get_pending_device(self, entity_id: str) -> PendingDevice | None:
        with self._lock:
            return self._pending.get(entity_id)

    def approve_device(
        self,
        entity_id: str,
        security_mode: str = "lockdown",
        apply_zone3: bool = True,
    ) -> dict[str, Any]:
        """Approve a pending device.

        Args:
            entity_id: The HA entity_id to approve
            security_mode: "lockdown", "balanced", "open", or "allow"
            apply_zone3: If True, apply Zone3 DNS policy via Pi-hole

        Returns:
            Result dict with success status, details, and Zone3 result if applied.
        """
        with self._lock:
            device = self._pending.pop(entity_id, None)

        if device is None:
            return {"success": False, "error": f"No pending device with entity_id '{entity_id}'"}

        now = datetime.now().isoformat()

        # Add to known devices
        known = KnownDevice(
            entity_id=entity_id,
            domain=device.domain,
            friendly_name=device.friendly_name,
            status="approved",
            security_mode=security_mode,
            approved_at=now,
            category=device.category,
        )

        with self._lock:
            self._known[entity_id] = known
            self._save_known()
            self._save_pending()

        result: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "friendly_name": device.friendly_name,
            "security_mode": security_mode,
            "approved_at": now,
        }

        # Apply Zone3 lockdown if requested
        if apply_zone3 and security_mode != "allow":
            zone3_result = self._apply_zone3_policy(device, security_mode)
            result["zone3"] = zone3_result

        self._log_approval(entity_id, "approved", {
            "security_mode": security_mode,
            "zone3_applied": apply_zone3,
            "category": device.category,
            "manufacturer": device.manufacturer,
        })

        logger.info("Approved device %s (%s) with %s mode", entity_id, device.friendly_name, security_mode)
        return result

    def reject_device(
        self,
        entity_id: str,
        block_firewall: bool = False,
    ) -> dict[str, Any]:
        """Reject a pending device.

        Args:
            entity_id: The HA entity_id to reject
            block_firewall: If True, also block the device at the firewall level
        """
        with self._lock:
            device = self._pending.pop(entity_id, None)

        if device is None:
            return {"success": False, "error": f"No pending device with entity_id '{entity_id}'"}

        now = datetime.now().isoformat()

        known = KnownDevice(
            entity_id=entity_id,
            domain=device.domain,
            friendly_name=device.friendly_name,
            status="rejected",
            approved_at=now,
            category=device.category,
        )

        with self._lock:
            self._known[entity_id] = known
            self._save_known()
            self._save_pending()

        result: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "friendly_name": device.friendly_name,
            "status": "rejected",
        }

        if block_firewall:
            fw_result = self._block_via_firewall(device)
            result["firewall"] = fw_result

        self._log_approval(entity_id, "rejected", {
            "firewall_blocked": block_firewall,
            "category": device.category,
        })

        logger.info("Rejected device %s (%s)", entity_id, device.friendly_name)
        return result

    def approve_all_pending(self, security_mode: str = "lockdown", apply_zone3: bool = True) -> list[dict]:
        """Approve all pending devices with the same security mode."""
        pending_ids = [d.entity_id for d in self.get_pending()]
        return [
            self.approve_device(eid, security_mode=security_mode, apply_zone3=apply_zone3)
            for eid in pending_ids
        ]

    def reset_device(self, entity_id: str) -> dict[str, Any]:
        """Move a known device back to pending (re-evaluate)."""
        with self._lock:
            known = self._known.pop(entity_id, None)
        if known is None:
            return {"success": False, "error": f"Device '{entity_id}' not in known list"}

        with self._lock:
            self._save_known()

        # Will be re-discovered on next check
        return {"success": True, "entity_id": entity_id, "message": "Device reset, will re-appear on next scan"}

    # -- zone3 integration -----------------------------------------------------

    def _apply_zone3_policy(self, device: PendingDevice, security_mode: str) -> dict:
        """Apply Zone3 DNS lockdown policy for a device."""
        try:
            from puffpai.zone3 import PolicyEngine, SecurityMode

            mode_map = {
                "lockdown": SecurityMode.LOCKDOWN,
                "balanced": SecurityMode.BALANCED,
                "open": SecurityMode.OPEN,
                "allow": SecurityMode.ALWAYS_ALLOW,
            }
            mode = mode_map.get(security_mode, SecurityMode.BALANCED)

            engine = PolicyEngine()

            # We need the device's IP and MAC for Zone3.
            # Try to get them from HA entity attributes.
            ip = device.attributes.get("ip_address", "")
            mac = device.attributes.get("mac", "")

            if not ip:
                # Try to find IP from network scanner if not in HA attributes
                ip = self._resolve_device_ip(device)

            if not ip:
                return {
                    "success": False,
                    "message": "Cannot determine device IP address. Zone3 policy not applied. "
                               "You may need to manually assign the IP in the Zone3 settings.",
                }

            result = engine.apply_policy(
                ip=ip,
                mac=mac or "unknown",
                mode=mode,
                manufacturer=device.manufacturer,
                hostname=device.friendly_name,
                category=device.category,
                is_safety_critical=False,
            )
            return result

        except ImportError:
            return {"success": False, "message": "Zone3 module not available"}
        except Exception as exc:
            logger.error("Zone3 policy application failed: %s", exc)
            return {"success": False, "message": str(exc)}

    def _resolve_device_ip(self, device: PendingDevice) -> str:
        """Try to find a device's IP address via network scan."""
        try:
            from puffpai.network_scanner import scan_network
            network_devices = scan_network()
            name_lower = device.friendly_name.lower()
            mfg_lower = (device.manufacturer or "").lower()

            for nd in network_devices:
                hostname = nd.get("hostname", "").lower()
                if name_lower in hostname or hostname in name_lower:
                    return nd.get("ip", "")
                if mfg_lower and mfg_lower in hostname:
                    return nd.get("ip", "")
        except Exception:
            pass
        return ""

    def _block_via_firewall(self, device: PendingDevice) -> dict:
        """Block a device at the firewall level."""
        try:
            from puffpai.firewall_manager import get_firewall_backend
            fw = get_firewall_backend()

            ip = device.attributes.get("ip_address", "")
            if not ip:
                ip = self._resolve_device_ip(device)

            if not ip:
                return {"success": False, "message": "Cannot determine device IP for firewall block"}

            return fw.block_device(ip, f"Rejected by PuffPAI: {device.friendly_name}")
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    # -- notification helpers --------------------------------------------------

    def get_notification_summary(self) -> dict[str, Any] | None:
        """Return a notification-ready summary if there are pending devices.

        Returns None if no pending devices.
        """
        pending = self.get_pending()
        if not pending:
            return None

        devices_summary = []
        for d in pending:
            devices_summary.append({
                "entity_id": d.entity_id,
                "name": d.friendly_name,
                "type": d.category,
                "manufacturer": d.manufacturer or "Unknown",
                "suggested_action": d.suggested_action,
            })

        return {
            "count": len(pending),
            "message": f"PuffPAI found {len(pending)} new device(s) on your network",
            "devices": devices_summary,
        }

    def get_conversational_prompt(self) -> str | None:
        """Return a natural language prompt for the assistant's conversation flow.

        Returns None if no pending devices.
        """
        pending = self.get_pending()
        if not pending:
            return None

        lines = [f"I found {len(pending)} new device(s) on your Home Assistant:"]
        for i, d in enumerate(pending, 1):
            mfg = f" by {d.manufacturer}" if d.manufacturer else ""
            lines.append(f"  {i}. {d.friendly_name}{mfg} ({d.domain}) — suggested: {d.suggested_action}")

        lines.append("")
        lines.append("Would you like me to:")
        lines.append("  - Approve and lock down communications (recommended)")
        lines.append("  - Approve with balanced access (allows cloud features)")
        lines.append("  - Reject and block from the network")
        lines.append("")
        lines.append("Just say 'yes, lock them down' or tell me what you'd like for each one.")

        return "\n".join(lines)

    # -- stats -----------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return overview stats."""
        with self._lock:
            approved = sum(1 for d in self._known.values() if d.status == "approved")
            rejected = sum(1 for d in self._known.values() if d.status == "rejected")
            auto = sum(1 for d in self._known.values() if d.status == "auto")
            return {
                "known_devices": len(self._known),
                "approved": approved,
                "rejected": rejected,
                "auto_approved": auto,
                "pending": len(self._pending),
            }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: HADeviceWatcher | None = None
_lock = threading.Lock()


def get_watcher(*, _force_reload: bool = False) -> HADeviceWatcher:
    """Return the global HADeviceWatcher singleton."""
    global _instance
    if _instance is None or _force_reload:
        with _lock:
            if _instance is None or _force_reload:
                _instance = HADeviceWatcher()
    return _instance
