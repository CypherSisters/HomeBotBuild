"""
PuffPAI Device Sentinel
=======================
Continuous-monitoring daemon that watches three device surfaces:

  1. IP network (ARP table on the host running PuffPAI)
  2. USB hardware plugged into the Home Assistant host (via HA's usb/scan WS)
  3. Home Assistant entities (via HADeviceWatcher)

Any device that appears which isn't in our baseline is:
  - Immediately quarantined (firewall block for IPs, HA rejection for entities)
  - Surfaced as a chat-visible alert via the local /conversation endpoint
  - Recorded to data/sentinel_alerts.jsonl for audit

Design notes:
  - First run seeds the baseline with everything currently visible — no alerts
    for pre-existing devices. This avoids a false "your whole house is new!"
    storm on initial startup.
  - USB devices cannot be physically disconnected from software, so for USB
    we alert + refuse to auto-onboard but leave the device powered.
  - The daemon runs in a background thread; stopping the API server stops it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# statedir is a stdlib-only leaf, so importing it at module scope keeps this
# module as import-light as it was: it defers the puffpai.config import (and
# with it the secrets provider) to call time.
from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Env-unset default: the repo-root ``data`` directory, shared with
#: agent_events, consent_gate and zone3/policy_engine. The sentinel has
#: always written here, so there is no legacy location to adopt.
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"

#: Resolved data directory; ``None`` = not resolved yet.
_data_dir_cache: Path | None = None

# The three paths below are not import-time constants resolved while this
# module is still being imported. They are lazy for the reason spelled out
# in puffpai.statedir: resolving at import drags puffpai.config into the import
# graph, and with it the secrets provider, which is the cycle risk the leaf
# exists to avoid.
#
# They stay plain module globals defaulting to None -- the house override-hook
# standard -- and NOT a PEP 562 ``__getattr__`` shim serving a resolved path.
# With a shim, ``monkeypatch.setattr`` reads the synthesized value as the "old"
# one and its undo writes that concrete path back into the globals, pinning it:
# every later resolution in the process returns the pinned path and silently
# ignores $PUFFPAI_DATA_DIR. test_agent_events patches _ALERTS_FILE exactly that
# way. Defaulting to None makes the undo restore None, which re-arms resolution.
#
# Assign one to pin it; the resolvers below check these before resolving, so
# test_sentinel's direct assignment and monkeypatch both still win.
_DATA_DIR: Path | None = None
_BASELINE_FILE: Path | None = None
_ALERTS_FILE: Path | None = None


def _resolve_data_dir() -> Path:
    """Resolve the writable state directory through the shared leaf.

    $PUFFPAI_DATA_DIR is how the hardened systemd deployment relocates
    writable state -- baseline and audit trail -- out of the read-only install
    tree. A hand-rolled replica of :func:`puffpai.config.resolve_state_dir`'s
    env handling (strip, ``~`` expansion, a relative value joined to the repo
    root, not the CWD) is free to drift per module, so this delegates to
    :func:`puffpai.statedir.resolve_runtime_dir`, which reaches that canon.

    Reads the env at call time and holds no cache, so the differential tests
    in ``tests/test_sentinel.py`` still see each change.
    """
    return Path(resolve_runtime_dir(str(_DEFAULT_DATA_DIR)))


def _override(name: str) -> Path | None:
    """Return an explicitly assigned module-level path, if there is one."""
    value = globals().get(name)
    return Path(value) if value else None


def _data_dir() -> Path:
    """Directory holding sentinel state, resolved on first use and cached."""
    global _data_dir_cache
    override = _override("_DATA_DIR")
    if override is not None:
        return override
    if _data_dir_cache is None:
        _data_dir_cache = _resolve_data_dir()
    return _data_dir_cache


def _baseline_file() -> Path:
    return _override("_BASELINE_FILE") or _data_dir() / "sentinel_baseline.json"


def _alerts_file() -> Path:
    return _override("_ALERTS_FILE") or _data_dir() / "sentinel_alerts.jsonl"


DEFAULT_INTERVAL_SECONDS = 120  # 2 minutes

# Suppress re-alerts for the same device within this window (seconds).
# Duplicate alerts ~1s apart were observed in production logs (2026-04/05).
ALERT_DEDUP_WINDOW_SECONDS = 600

# MAC OUI prefixes belonging to the host's own virtual adapters.
# 00:15:5D = Microsoft Hyper-V / WSL2 vNICs -- alerting (or quarantining!)
# these would target the host's own virtualization stack.
IGNORED_MAC_PREFIXES = ("00:15:5D",)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Fingerprint:
    """Stable identifier for a device across scans."""
    source: str          # "ip" | "usb" | "ha"
    key: str             # MAC address, vid:pid:serial, or entity_id
    label: str = ""      # human-readable
    extra: dict = field(default_factory=dict)

    def tag(self) -> str:
        return f"{self.source}:{self.key}"


@dataclass
class Alert:
    id: str
    created_at: str
    source: str
    device_key: str
    label: str
    summary: str
    quarantined: bool
    quarantine_detail: str
    acknowledged: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class DeviceSentinel:
    """Background thread that scans for new devices every N seconds."""

    def __init__(
        self,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        on_alert: Optional[Callable[[Alert], None]] = None,
    ):
        self._interval = max(30, int(interval_seconds))
        self._on_alert = on_alert
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._baseline: set[str] = set()
        self._alerts: list[Alert] = []
        self._last_scan_at: Optional[str] = None
        self._last_scan_counts: dict[str, int] = {}
        self._seeded = False
        self._errors: list[str] = []
        self._selftest: Optional[dict] = None  # quarantine self-test result

        self._load_state()

    # -- persistence ---------------------------------------------------------

    def _load_state(self) -> None:
        # Resolved through the accessors, never the module globals: the paths
        # are lazy now, so a bare name would be a NameError (and would miss a
        # test's override besides).
        _data_dir().mkdir(parents=True, exist_ok=True)
        baseline_file = _baseline_file()
        if baseline_file.exists():
            try:
                raw = json.loads(baseline_file.read_text())
                self._baseline = set(raw.get("tags", []))
                self._seeded = bool(raw.get("seeded"))
            except Exception:
                logger.warning("sentinel baseline corrupt; starting fresh")

        alerts_file = _alerts_file()
        if alerts_file.exists():
            try:
                for line in alerts_file.read_text().splitlines():
                    if line.strip():
                        self._alerts.append(Alert(**json.loads(line)))
            except Exception:
                logger.warning("sentinel alerts log corrupt; starting fresh")

    def _save_baseline(self) -> None:
        _data_dir().mkdir(parents=True, exist_ok=True)
        _baseline_file().write_text(json.dumps({
            "tags": sorted(self._baseline),
            "seeded": self._seeded,
            "updated_at": datetime.now().isoformat(),
        }, indent=2))

    def _append_alert(self, alert: Alert) -> None:
        _data_dir().mkdir(parents=True, exist_ok=True)
        with _alerts_file().open("a") as f:
            f.write(json.dumps(alert.to_dict()) + "\n")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the background scan thread if not already running."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="puffpai-sentinel", daemon=True,
        )
        self._thread.start()
        logger.info("DeviceSentinel started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Prove quarantine capability at startup: an auto-quarantine attempt
        # can fail silently (elevation errors on stdout). Never trust
        # code-in-repo == behavior.
        try:
            self.run_quarantine_selftest()
        except Exception as exc:
            logger.exception("quarantine self-test crashed: %s", exc)
        # Initial kick is immediate so baseline seeds on first boot;
        # thereafter we sleep between scans.
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:
                logger.exception("sentinel scan failed: %s", exc)
                self._errors.append(f"{datetime.now().isoformat()}: {exc}")
                self._errors = self._errors[-20:]
            # Sleep in small slices so stop() is snappy
            for _ in range(self._interval):
                if self._stop.is_set():
                    return
                time.sleep(1)

    # -- quarantine self-test --------------------------------------------------

    SELFTEST_IP = "192.0.2.123"  # RFC 5737 TEST-NET-1: never routable, safe to block

    def run_quarantine_selftest(self) -> dict:
        """Create, then remove, a firewall block for a TEST-NET IP and record
        the outcome. Proves at every daemon start that quarantine actually
        works (elevation, netsh/nft availability, rule syntax).

        Result is appended to sentinel_alerts.jsonl with source="selftest"
        and kept on self._selftest for the status endpoint.
        """
        detail = {"block": None, "unblock": None}
        ok = False
        try:
            from puffpai.firewall_manager import get_firewall_backend
            fw = get_firewall_backend()
            if not fw.is_available():
                detail["block"] = f"backend {fw.backend_name()} unavailable"
            else:
                blk = fw.block_device(
                    self.SELFTEST_IP, reason="sentinel startup self-test")
                detail["block"] = blk.get("message", str(blk.get("success")))
                if blk.get("success"):
                    unb = fw.unblock_device(self.SELFTEST_IP)
                    detail["unblock"] = unb.get(
                        "message", str(unb.get("success")))
                    ok = bool(unb.get("success"))
        except Exception as exc:
            detail["block"] = detail["block"] or f"exception: {exc}"

        summary = (
            "Quarantine self-test PASSED — firewall block/unblock verified"
            if ok else
            f"Quarantine self-test FAILED — {detail} — auto-quarantine will "
            f"NOT protect this network until fixed (check elevation)"
        )
        alert = Alert(
            id=str(uuid.uuid4()),
            created_at=datetime.now().isoformat(),
            source="selftest",
            device_key=self.SELFTEST_IP,
            label="quarantine self-test",
            summary=summary,
            quarantined=ok,
            quarantine_detail=json.dumps(detail),
            acknowledged=ok,  # only failures demand attention
        )
        self._append_alert(alert)
        self._selftest = {"passed": ok, "at": alert.created_at, **detail}
        (logger.info if ok else logger.error)("%s", summary)
        if not ok and self._on_alert:
            try:
                self._on_alert(alert)  # surface failure in chat, not just logs
            except Exception:
                pass
        return self._selftest

    # -- the scan ------------------------------------------------------------

    def scan_once(self) -> dict[str, Any]:
        """Run one scan across all sources, update baseline, emit alerts.

        Returns a summary dict useful for the API status endpoint.
        """
        with self._lock:
            seen: list[Fingerprint] = []
            counts: dict[str, int] = {}
            for source, fingerprints in (
                ("ip", self._scan_ip()),
                ("usb", self._scan_usb()),
                ("ha", self._scan_ha()),
            ):
                fps = list(fingerprints)
                counts[source] = len(fps)
                seen.extend(fps)

            self._last_scan_counts = counts
            self._last_scan_at = datetime.now().isoformat()

            if not self._seeded:
                # First scan ever: seed baseline, emit no alerts.
                for fp in seen:
                    self._baseline.add(fp.tag())
                self._seeded = True
                self._save_baseline()
                logger.info("Sentinel baseline seeded with %d devices", len(self._baseline))
                return {
                    "seeded": True,
                    "baseline_size": len(self._baseline),
                    "new_devices": 0,
                    "counts": counts,
                }

            new_devices: list[Fingerprint] = []
            for fp in seen:
                if fp.tag() not in self._baseline:
                    new_devices.append(fp)
                    self._baseline.add(fp.tag())

            for fp in new_devices:
                self._handle_new_device(fp)

            if new_devices:
                self._save_baseline()

            return {
                "seeded": True,
                "baseline_size": len(self._baseline),
                "new_devices": len(new_devices),
                "counts": counts,
            }

    # -- sources -------------------------------------------------------------

    def _scan_ip(self) -> Iterable[Fingerprint]:
        import subprocess
        import re
        try:
            result = subprocess.run(
                ["arp", "-a"], capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:
            logger.warning("arp failed: %s", exc)
            return
        for line in (result.stdout or "").splitlines():
            ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            mac = re.search(r"([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}", line)
            if not ip or not mac:
                continue
            mac_norm = mac.group(0).replace("-", ":").upper()
            if mac_norm == "FF:FF:FF:FF:FF:FF":
                continue
            if mac_norm.startswith(IGNORED_MAC_PREFIXES):
                # Host-local virtual adapter (Hyper-V/WSL2) -- not a LAN device.
                continue
            yield Fingerprint(
                source="ip",
                key=mac_norm,
                label=f"{ip.group(1)} ({mac_norm})",
                extra={"ip": ip.group(1), "mac": mac_norm, "raw": line.strip()},
            )

    def _scan_usb(self) -> Iterable[Fingerprint]:
        try:
            from puffpai.zigbee_manager import ZigbeeManager
            mgr = ZigbeeManager()
            for dev in mgr.list_usb_devices():
                serial = dev.serial_number or (dev.device_path or "")
                key = f"{dev.vid}:{dev.pid}:{serial}"
                label = dev.description or dev.manufacturer or f"USB {dev.vid}:{dev.pid}"
                yield Fingerprint(
                    source="usb",
                    key=key,
                    label=label,
                    extra={
                        "vid": dev.vid, "pid": dev.pid,
                        "serial": dev.serial_number,
                        "path": dev.device_path,
                        "description": dev.description,
                        "manufacturer": dev.manufacturer,
                    },
                )
        except Exception as exc:
            logger.warning("usb scan failed: %s", exc)

    def _scan_ha(self) -> Iterable[Fingerprint]:
        try:
            from puffpai.ha_device_watcher import get_watcher
            watcher = get_watcher()
            # Ask the watcher to refresh; it persists its own known set.
            newly_pending = watcher.check_for_new_devices()
            for pd in newly_pending:
                yield Fingerprint(
                    source="ha",
                    key=pd.entity_id,
                    label=pd.friendly_name or pd.entity_id,
                    extra={
                        "entity_id": pd.entity_id,
                        "domain": pd.domain,
                        "manufacturer": pd.manufacturer,
                        "category": pd.category,
                    },
                )
        except Exception as exc:
            logger.warning("ha scan failed: %s", exc)

    # -- quarantine + alert --------------------------------------------------

    def _handle_new_device(self, fp: Fingerprint) -> None:
        # Dedup: if we already alerted on this device recently, don't re-alert.
        now = datetime.now()
        for prev in reversed(self._alerts):
            if prev.device_key != fp.key:
                continue
            try:
                prev_at = datetime.fromisoformat(prev.created_at)
            except ValueError:
                break
            if (now - prev_at).total_seconds() < ALERT_DEDUP_WINDOW_SECONDS:
                logger.info("Suppressing duplicate alert for %s (last alert %s)",
                            fp.key, prev.created_at)
                return
            break

        quarantined, detail = self._quarantine(fp)

        summary = self._build_summary(fp, quarantined, detail)
        alert = Alert(
            id=uuid.uuid4().hex[:12],
            created_at=datetime.now().isoformat(),
            source=fp.source,
            device_key=fp.key,
            label=fp.label,
            summary=summary,
            quarantined=quarantined,
            quarantine_detail=detail,
        )
        self._alerts.append(alert)
        self._append_alert(alert)

        # Event inflow (orchestration Phase 1): let the agent team reason about
        # the new device. Wrapped defensively -- monitoring must keep running
        # even if the agent team is off, wedged, or misconfigured.
        try:
            from puffpai.agent_events import submit_agent_event
            submit_agent_event("device_sentinel", "network_new_device", {
                "mac": fp.key if fp.source == "ip" else None,
                "ip": fp.extra.get("ip"),
                "name": fp.label,
                "source": fp.source,
            })
        except Exception as exc:
            logger.warning("agent event submission failed: %s", exc)

        if self._on_alert is not None:
            try:
                self._on_alert(alert)
            except Exception as exc:
                logger.warning("on_alert callback failed: %s", exc)

    def _quarantine(self, fp: Fingerprint) -> tuple[bool, str]:
        """Block-first policy. Returns (quarantined, detail_string)."""
        if fp.source == "ip":
            ip = fp.extra.get("ip")
            if not ip:
                return False, "no IP address captured"
            try:
                from puffpai.firewall_manager import get_firewall_backend
                fw = get_firewall_backend()
                result = fw.block_device(ip, reason=f"PuffPAI sentinel: unknown device {fp.label}")
                if result.get("success"):
                    return True, f"firewall blocked {ip}"
                return False, f"firewall block failed: {result.get('message')}"
            except Exception as exc:
                return False, f"firewall error: {exc}"

        if fp.source == "ha":
            entity_id = fp.extra.get("entity_id")
            if not entity_id:
                return False, "no entity_id captured"
            try:
                from puffpai.ha_device_watcher import get_watcher
                result = get_watcher().reject_device(entity_id, block_firewall=True)
                if result.get("success"):
                    return True, f"HA device rejected + firewall blocked"
                return False, f"HA rejection failed: {result.get('error')}"
            except Exception as exc:
                return False, f"HA rejection error: {exc}"

        if fp.source == "usb":
            # Physical USB devices cannot be disabled from software safely.
            # Alert loudly so user can unplug if malicious.
            return False, "USB devices cannot be auto-disabled — review and unplug if unexpected"

        return False, f"unknown source: {fp.source}"

    @staticmethod
    def _build_summary(fp: Fingerprint, quarantined: bool, detail: str) -> str:
        action = "blocked" if quarantined else "flagged (no auto-action taken)"
        if fp.source == "ip":
            ip = fp.extra.get("ip", "?")
            return (f"New device on your network: {fp.label} at {ip}. "
                    f"It has been {action}. {detail}")
        if fp.source == "usb":
            return (f"New USB hardware plugged into the Home Assistant host: {fp.label}. "
                    f"It has been {action}. Review and unplug if you did not add this.")
        if fp.source == "ha":
            return (f"New Home Assistant entity appeared: {fp.label}. "
                    f"It has been {action}. {detail}")
        return f"New {fp.source} device: {fp.label}. {detail}"

    # -- public read api -----------------------------------------------------

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "interval_seconds": self._interval,
            "baseline_size": len(self._baseline),
            "seeded": self._seeded,
            "last_scan_at": self._last_scan_at,
            "last_scan_counts": dict(self._last_scan_counts),
            "pending_alerts": sum(1 for a in self._alerts if not a.acknowledged),
            "total_alerts": len(self._alerts),
            "recent_errors": list(self._errors[-5:]),
            "quarantine_selftest": self._selftest,
        }

    def list_alerts(self, include_acknowledged: bool = False) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self._alerts
                    if include_acknowledged or not a.acknowledged]

    def acknowledge(self, alert_id: str) -> dict:
        with self._lock:
            for a in self._alerts:
                if a.id == alert_id:
                    a.acknowledged = True
                    return {"success": True, "id": alert_id}
        return {"success": False, "error": "alert not found"}

    def approve_device(self, alert_id: str) -> dict:
        """Acknowledge alert and un-quarantine the device (whitelist it)."""
        with self._lock:
            alert = next((a for a in self._alerts if a.id == alert_id), None)
        if alert is None:
            return {"success": False, "error": "alert not found"}

        unblock_detail = ""
        if alert.source == "ip":
            try:
                from puffpai.firewall_manager import get_firewall_backend
                fw = get_firewall_backend()
                # We need the IP from the original alert; device_key is MAC
                # so look up from recent ARP.
                ip = self._ip_for_mac(alert.device_key)
                if ip:
                    r = fw.unblock_device(ip)
                    unblock_detail = f"unblocked {ip}: {r.get('message') or 'ok'}"
                else:
                    unblock_detail = "IP no longer in ARP; no firewall rule to remove"
            except Exception as exc:
                unblock_detail = f"unblock error: {exc}"
        elif alert.source == "ha":
            unblock_detail = ("Call /devices/<entity_id>/approve to re-approve the HA "
                              "entity with a security mode.")
        elif alert.source == "usb":
            unblock_detail = "USB devices have no software block to remove."

        with self._lock:
            alert.acknowledged = True

        return {"success": True, "id": alert_id, "detail": unblock_detail}

    def _ip_for_mac(self, mac: str) -> Optional[str]:
        for fp in self._scan_ip():
            if fp.key == mac.upper():
                return fp.extra.get("ip")
        return None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[DeviceSentinel] = None
_instance_lock = threading.Lock()


def get_sentinel(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    on_alert: Optional[Callable[[Alert], None]] = None,
) -> DeviceSentinel:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = DeviceSentinel(
                    interval_seconds=interval_seconds,
                    on_alert=on_alert,
                )
    return _instance
