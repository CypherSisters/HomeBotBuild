"""
PuffPAI Zone 3 — Device Security Policy Engine

Maps classified devices to security modes and applies DNS-level
blocking via Pi-hole.

Three modes:
    LOCKDOWN  — Block ALL outbound DNS except essential services.
                Device loses cloud features. Maximum privacy.
    BALANCED  — Block telemetry, ads, and tracking. Allow cloud
                services that the device needs to function.
    OPEN      — Block ads and cross-device tracking only.
                Minimal interference with device functionality.

Safety-critical devices (medical, fire alarms) are ALWAYS set to
ALWAYS_ALLOW and cannot be changed to lockdown.

The conversational flow:
    1. Network scan discovers devices (ARP + MAC)
    2. DeviceClassifier identifies each device
    3. PolicyEngine suggests a mode per device
    4. User confirms or adjusts
    5. PolicyEngine applies DNS rules via PiholeController
"""

from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Optional

from puffpai.security.device_classifier import (
    DeviceCategory,
    DeviceClassifier,
    DEVICE_POLICIES,
    classify_device,
)
# _STATE_DIR_ENV is the env-var NAME only; statedir is a stdlib-only leaf, so
# importing it at module scope pulls in nothing. tests/test_statedir.py pins
# that name to config.STATE_DIR_ENV, so reading it here cannot drift from canon.
from puffpai.statedir import _STATE_DIR_ENV, resolve_runtime_dir
from puffpai.zone3.telemetry_domains import TelemetryDB, DomainCategory
from puffpai.zone3.pihole_controller import PiholeController
from puffpai.zone3.adguard_controller import (AdGuardController, block_rule,
                                              _validate_client)

logger = logging.getLogger(__name__)

#: Categories that may NEVER be blocked, whatever mode is asked for.
#:
#: ``device_classifier`` flags MEDICAL_DEVICE and SAFETY_DEVICE as
#: ``is_safety_critical`` and that flag is load-bearing elsewhere (the
#: firewall policy reads it), so it is not widened here. NETWORK_INFRA is
#: added on top for Zone 3 only: blocking the router's or the access point's
#: name resolution takes the house off the network, and taking the house off
#: the network is not a privacy improvement. The set is defined by CATEGORY
#: rather than by the classifier's confidence, so a mis-scored router is still
#: protected.
_NEVER_BLOCK_CATEGORIES: frozenset[DeviceCategory] = frozenset({
    DeviceCategory.MEDICAL_DEVICE,
    DeviceCategory.SAFETY_DEVICE,
    DeviceCategory.NETWORK_INFRA,
})


def _is_protected(category: str | DeviceCategory | None,
                  is_safety_critical: bool = False) -> bool:
    """Is this device un-blockable regardless of the requested mode?

    Unknown category text counts as NOT protected — the caller's
    ``is_safety_critical`` flag still decides — because silently protecting
    everything we cannot parse would turn a typo into a disabled Zone 3.
    """
    if is_safety_critical:
        return True
    if isinstance(category, DeviceCategory):
        return category in _NEVER_BLOCK_CATEGORIES
    try:
        return DeviceCategory(str(category)) in _NEVER_BLOCK_CATEGORIES
    except ValueError:
        return False

# ── state location (lazy) ─────────────────────────────────────────────
#
# The managed-device ledger is the record of which devices this engine has
# put into LOCKDOWN and which are ALWAYS_ALLOW (medical, fire alarms). It
# resolves on first use through the one resolver that defines
# $PUFFPAI_DATA_DIR semantics, not as an import-time constant: on a hardened
# unit only the deployment's ReadWritePaths root is writable, and a ledger
# that cannot be saved comes back empty after every restart, i.e.
# safety-critical devices silently lose their ALWAYS_ALLOW marking.
#
# NOTE ON THE DEFAULT: this file is src/puffpai/zone3/policy_engine.py, so
# three parents up is <repo>/src, NOT the repo root. The default is the real
# <repo>/data; <repo>/src/data (inside the read-only code tree) stays as a
# read-only legacy fallback below so a ledger written there is not orphaned.
_SRC_DIR = Path(__file__).resolve().parent.parent.parent     # <repo>/src
_REPO_ROOT = _SRC_DIR.parent                                 # <repo>
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"
_LEGACY_MANAGED_DEVICES_FILE = _SRC_DIR / "data" / "zone3_managed_devices.json"

#: Test/ops override: an explicit ledger path, honoured before env and legacy.
#: ``None`` means "resolve".
_MANAGED_DEVICES_FILE: Path | str | None = None


def _ledger_override() -> str | None:
    """Return the explicitly assigned ledger path, if there is one.

    str or Path both count -- silently ignoring a Path-valued override and
    resolving around it would put the ledger somewhere the operator did not
    ask for. Nothing else does: a bare ``MagicMock`` from a decorator-style
    ``@patch`` satisfies os.fspath (it answers ``__fspath__`` with a junk
    name), and honouring that would write policy state to a junk path
    relative to the CWD. Mirrors firewall_manager._override.
    """
    value = _MANAGED_DEVICES_FILE
    if not isinstance(value, (str, PurePath)):
        return None
    return os.fspath(value) or None

#: Cached ledger path; ``None`` = unresolved. A reload re-runs this assignment,
#: which is how tests re-resolve after changing the environment.
_managed_devices_file: Path | None = None


def _managed_devices_path() -> Path:
    """Absolute path of the managed-device ledger, resolved on first use.

    Lazy by construction: :mod:`puffpai.statedir` is a stdlib-only leaf, but
    the resolution it performs imports ``puffpai.config`` (which reaches the
    secrets provider), so it must not run at import time.

    Legacy adoption is FILE-level, not the directory-level ``legacy=`` of the
    canon: <repo>/data exists in every checkout, so a directory probe would
    never fall through to the old <repo>/src/data. It fires only when nothing
    else decided the location -- no override hook, no $PUFFPAI_DATA_DIR, and
    no ledger at the new path -- and it only *reads* from there; nothing is
    copied or moved implicitly.

    The env test is "is the var set", NOT "did the resolved dir move". Those
    differ exactly when an operator points $PUFFPAI_DATA_DIR at the default
    data dir (absolute <repo>/data, or the relative value "data", which joins
    to the repo root): a dir comparison reads that as "unset", adopts, and the
    engine then reads AND WRITES the ledger in the read-only code tree with an
    env var explicitly set. The canon has the same shape -- resolve_state_dir
    returns on a non-empty env_root before it ever probes legacy.
    """
    global _managed_devices_file
    override = _ledger_override()
    if override:
        return Path(override)
    if _managed_devices_file is None:
        path = Path(resolve_runtime_dir(
            str(_DEFAULT_DATA_DIR))) / "zone3_managed_devices.json"
        legacy = Path(_LEGACY_MANAGED_DEVICES_FILE)
        env_set = bool(os.environ.get(_STATE_DIR_ENV, "").strip())
        if not env_set and not path.exists() and legacy.is_file():
            logger.warning(
                "using legacy managed-device ledger %s; move it to %s to "
                "silence this", legacy, path,
            )
            path = legacy
        _managed_devices_file = path
    return _managed_devices_file


#: The bucket ledger lives NEXT TO the device ledger, whatever resolved that
#: one -- override hook, $PUFFPAI_DATA_DIR or the legacy fallback. Resolving it
#: independently would let the two halves of one policy land in two different
#: directories on a box that moved its data dir, and a restart would then come
#: back with the devices but not the integrations (or the reverse).
MANAGED_BUCKETS_NAME = "zone3_managed_buckets.json"


def _managed_buckets_path() -> Path:
    """Absolute path of the managed-bucket ledger."""
    return _managed_devices_path().parent / MANAGED_BUCKETS_NAME


class SecurityMode(Enum):
    """Device security mode — controls DNS blocking level."""
    LOCKDOWN = "lockdown"         # Block everything except essentials
    BALANCED = "balanced"         # Block telemetry/ads, allow cloud
    OPEN = "open"                 # Block ads/tracking only
    ALWAYS_ALLOW = "always_allow" # Safety-critical — never block


# Default mode suggestions per device category
_DEFAULT_MODES: dict[DeviceCategory, SecurityMode] = {
    DeviceCategory.COMPUTER: SecurityMode.OPEN,
    DeviceCategory.PHONE: SecurityMode.OPEN,
    DeviceCategory.TABLET: SecurityMode.OPEN,
    DeviceCategory.SMART_SPEAKER: SecurityMode.BALANCED,
    DeviceCategory.IOT_CAMERA: SecurityMode.LOCKDOWN,
    DeviceCategory.SMART_TV: SecurityMode.BALANCED,
    DeviceCategory.PRINTER: SecurityMode.LOCKDOWN,
    DeviceCategory.GAMING: SecurityMode.OPEN,
    DeviceCategory.NETWORK_INFRA: SecurityMode.ALWAYS_ALLOW,
    DeviceCategory.MEDICAL_DEVICE: SecurityMode.ALWAYS_ALLOW,
    DeviceCategory.SAFETY_DEVICE: SecurityMode.ALWAYS_ALLOW,
    DeviceCategory.IOT_APPLIANCE: SecurityMode.BALANCED,
    DeviceCategory.UNKNOWN: SecurityMode.LOCKDOWN,
}


@dataclass
class DevicePolicy:
    """A managed device with its assigned security policy."""
    ip: str
    mac: str
    manufacturer: Optional[str]
    category: str  # DeviceCategory.value
    hostname: str = ""
    mode: str = "balanced"  # SecurityMode.value
    is_safety_critical: bool = False
    managed: bool = True
    blocked_domains: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    user_confirmed: bool = False
    #: WHO put this row here: ``"owner"`` (an owner-gated HTTP route redeemed a
    #: consent ticket) or ``"tool"`` (a chat/LLM tool recorded it as a dry
    #: run). The distinction is load-bearing, not decorative: the enforce
    #: switch turns EVERY recorded rule live at once, so a row the model
    #: recorded while nobody was looking becomes a rule the household lives
    #: under -- and until this field existed the enforce proposal could not
    #: tell the owner which rows those were. Defaults to "owner" so a ledger
    #: written by an older build does not read as model-authored.
    recorded_by: str = "owner"
    recorded_at: str = ""

    @property
    def security_mode(self) -> SecurityMode:
        return SecurityMode(self.mode)

    @property
    def device_category(self) -> DeviceCategory:
        return DeviceCategory(self.category)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DevicePolicy:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


#: Recorded modes for a bucket, keyed by SecurityMode, as one line of plain
#: English each. These strings are shown to the owner in the 409 proposal, so
#: they say what BREAKS, not what is "optimised".
BUCKET_MODE_EFFECTS: dict[str, str] = {
    SecurityMode.OPEN.value:
        "Ads and cross-device tracking for this vendor, and nothing else. "
        "The integration keeps working.",
    SecurityMode.BALANCED.value:
        "This vendor's telemetry, ads and tracking. The cloud API the "
        "integration actually needs is left alone, so it keeps working.",
    SecurityMode.LOCKDOWN.value:
        "EVERY known domain for this vendor, including the cloud API. THIS "
        "BREAKS THE INTEGRATION: the devices in it stop responding to Home "
        "Assistant until you set this back.",
    SecurityMode.ALWAYS_ALLOW.value:
        "Nothing is blocked for this vendor.",
}


def bucket_lockdown_warning(integration: str, device_count: int = 0) -> str:
    """The sentence an owner must read before a bucket goes to LOCKDOWN.

    One function so the 409 proposal, the engine result and the dashboard
    cannot describe the same act in three different strengths of language.
    """
    where = (f" The {device_count} device(s) in it stop responding to Home "
             f"Assistant." if device_count else "")
    return (f"This BREAKS the {integration} integration: PuffPAI refuses the "
            f"vendor's cloud API for Home Assistant, not just its "
            f"telemetry.{where} Set it back to BALANCED to undo it.")


@dataclass
class BucketPolicy:
    """A Home Assistant integration bucket with its assigned mode.

    The subject of a bucket rule is NOT the smart plug. It is Home Assistant
    itself: a cloud-backed integration means the hub holds the vendor
    credential and the hub is the client that talks to the vendor cloud, so a
    rule scoped to the plug's own IP would refuse nothing at all. Every rule
    this produces carries ``$client=<the hub's address>``, and ``client_ip``
    records which address that was when the policy was recorded -- so a rule
    block that stopped matching after the hub moved is diagnosable rather than
    mysterious.
    """
    bucket_id: str
    integration: str
    tier: str
    mode: str = "balanced"
    device_count: int = 0
    client_ip: str = ""
    blocked_domains: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    recorded_by: str = "owner"
    recorded_at: str = ""

    @property
    def security_mode(self) -> SecurityMode:
        return SecurityMode(self.mode)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> BucketPolicy:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _write_private_json(path, payload) -> None:
    """Write JSON owner-only (0600) and atomically: tmp + replace.

    The device and bucket ledgers name the household's devices and the
    vendor domains PuffPAI intends to refuse for them; they sit beside the
    enforce overlay, which is already 0600, and should not be looser.
    """
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


class PolicyEngine:
    """Orchestrates device discovery, classification, and DNS policy enforcement.

    This is the main entry point for Zone 3 operations. It:
    1. Discovers devices on the network
    2. Classifies them using MAC OUI + hostname
    3. Suggests security modes based on device type
    4. Applies DNS blocking rules via Pi-hole
    5. Persists managed device state
    """

    # Pi-hole group names for each security mode
    _GROUP_NAMES = {
        SecurityMode.LOCKDOWN: "puffpai_lockdown",
        SecurityMode.BALANCED: "puffpai_balanced",
        SecurityMode.OPEN: "puffpai_open",
        SecurityMode.ALWAYS_ALLOW: "puffpai_allow_all",
    }

    #: Persistent-client name prefix in AdGuard. Namespaced so a client the
    #: owner made by hand in the AdGuard UI is never adopted or deleted by us.
    _CLIENT_PREFIX = "puffpai:"

    def __init__(
        self,
        pihole: PiholeController | None = None,
        managed_file: str | Path | None = None,
        adguard: AdGuardController | None = None,
        buckets_file: str | Path | None = None,
    ):
        self._pihole = pihole
        self._adguard = adguard
        self._telemetry = TelemetryDB()
        self._classifier = DeviceClassifier()
        self._managed_file = (Path(managed_file) if managed_file
                              else _managed_devices_path())
        self._buckets_file = (
            Path(buckets_file) if buckets_file
            else (self._managed_file.parent / MANAGED_BUCKETS_NAME
                  if managed_file else _managed_buckets_path()))
        self._devices: dict[str, DevicePolicy] = {}  # keyed by IP
        self._buckets: dict[str, BucketPolicy] = {}  # keyed by bucket id
        self._load_managed_devices()
        self._load_managed_buckets()

    # ── enforcement mode ──────────────────────────────────────────────

    @property
    def enforcing(self) -> bool:
        """True when the DNS controller is allowed to actually block."""
        return bool(self._adguard and self._adguard.enforce)

    def set_enforce(self, enabled: bool) -> dict:
        """Flip observe <-> enforce and re-push the whole policy.

        This is the ONE switch. It rewrites the rule block (commented out or
        live), and only when turning enforcement ON does it also turn AdGuard's
        global protection on — the two are deliberately coupled so there is no
        state where the rules are live but protection is off, or the reverse.

        Turning it OFF leaves protection off AND rewrites every rule as a
        comment, so a device stops being filtered even if AdGuard's protection
        toggle is flipped back by hand in the UI.
        """
        if self._adguard is None:
            return {"success": False,
                    "message": "No AdGuard controller configured"}
        self._adguard.enforce = bool(enabled)
        sync = self.sync_dns()
        if enabled:
            protection = self._adguard.set_protection(True)
        else:
            protection = self._adguard.set_protection(False)
        return {
            "success": bool(sync.get("success")) and protection,
            "enforce": bool(enabled),
            "protection_enabled": bool(enabled) and protection,
            "message": (
                "Zone 3 is ENFORCING: the rules below are live."
                if enabled else
                "Zone 3 is in monitor-first mode: every query is logged, "
                "nothing is blocked."),
            "sync": sync,
        }

    # ── Device Discovery ──────────────────────────────────────────────

    def discover_devices(self) -> list[dict]:
        """Scan the network and classify all discovered devices.

        Returns a list of device dicts with classification and
        suggested security mode.
        """
        from puffpai.network_scanner import scan_network
        raw_devices = scan_network()

        discovered = []
        for dev in raw_devices:
            ip = dev.get("ip", "")
            mac = dev.get("mac", "unknown")
            hostname = dev.get("hostname", "")

            # Skip devices without valid MAC
            if mac == "unknown" or not mac:
                continue

            # Classify
            result = classify_device(mac=mac, hostname=hostname)

            # Determine suggested mode
            suggested_mode = _DEFAULT_MODES.get(result.category, SecurityMode.BALANCED)

            device_info = {
                "ip": ip,
                "mac": result.mac,
                "hostname": hostname,
                "manufacturer": result.manufacturer,
                "category": result.category.value,
                "confidence": result.confidence,
                "is_safety_critical": result.is_safety_critical,
                "suggested_mode": suggested_mode.value,
                "policy_description": result.policy.description,
                "already_managed": ip in self._devices,
            }

            # If already managed, include current mode
            if ip in self._devices:
                device_info["current_mode"] = self._devices[ip].mode

            discovered.append(device_info)

        return discovered

    def get_unmanaged_devices(self) -> list[dict]:
        """Discover devices and return only unmanaged ones.

        This is the entry point for the conversational flow:
        "I found 2 new devices, would you like me to manage these?"
        """
        all_devices = self.discover_devices()
        return [d for d in all_devices if not d.get("already_managed")]

    # ── Policy Application ────────────────────────────────────────────

    def apply_policy(
        self,
        ip: str,
        mac: str,
        mode: SecurityMode,
        manufacturer: str | None = None,
        hostname: str = "",
        category: str = "unknown",
        is_safety_critical: bool = False,
        *,
        push: bool = False,
        recorded_by: str = "owner",
    ) -> dict:
        """Apply a security policy to a device.

        1. Coerces protected devices to ALWAYS_ALLOW (see below)
        2. Determines which domains to block based on mode + manufacturer
        3. Records the decision in the managed-device ledger — ALWAYS, in
           both observe and enforce mode; the ledger is what a re-sync
           replays, so a monitor-first run is not a run that forgot
        4. Pushes the whole policy to the DNS controller **only when the
           caller asks for it** (``push=True``)

        ``push`` DEFAULTS TO FALSE AND THAT IS THE POINT. The four ``zone3_*``
        LLM tools call this method; ending it in an unconditional
        ``sync_dns()`` would let a chat turn, or a document the model summarised
        that carried instructions, rewrite the household's whole AdGuard
        rule block and write ALWAYS_ALLOW client entries, from a path with no
        consent step anywhere in it. In observe mode the block is written
        commented out, which makes it *quiet*, not *harmless*: the client
        entries are live in BOTH postures, and the rule block is one enforce
        click from being live. Recording is the model's job; pushing is the
        owner's, and the owner-gated HTTP routes are the only callers that pass
        ``push=True``.

        ``recorded_by`` is ``"owner"`` or ``"tool"`` and is written into the
        ledger row, so the enforce proposal can mark the rows a model recorded.

        THE PROTECTION COERCION IS DELIBERATE. Refusing only LOCKDOWN on a
        safety-critical device is not enough: ``apply_policy(mode=BALANCED)``
        on a medical device would block forty telemetry domains, and the
        "always allow" promise would hold for exactly one of the four modes. A
        protected device is forced to ALWAYS_ALLOW whatever was asked for, the
        call still SUCCEEDS (refusing would leave the device unmanaged and
        therefore unprotected by the AdGuard client entry below), and the
        coercion is reported in ``mode_override`` so the caller can say so out
        loud.

        Returns result dict with success status and details.
        """
        protected = _is_protected(category, is_safety_critical)
        requested = mode
        if protected and mode != SecurityMode.ALWAYS_ALLOW:
            mode = SecurityMode.ALWAYS_ALLOW

        # Get domains to block based on mode
        domains_to_block = self._get_domains_for_mode(mode, manufacturer)

        # Legacy Pi-hole path, kept working for anyone still on it.
        pihole_result = {"success": True, "message": "Pi-hole not configured"}
        if self._pihole:
            if mode == SecurityMode.ALWAYS_ALLOW:
                pihole_result = {"success": True, "message": "No blocks applied (always allow)"}
            elif domains_to_block:
                comment = f"{category}/{mode.value}/{manufacturer or 'unknown'}"
                pihole_result = self._pihole.block_domains(domains_to_block, comment)

                # Assign client to group
                group_name = self._GROUP_NAMES.get(mode, "puffpai_balanced")
                self._pihole.assign_client_to_group(ip, group_name)

        # Save managed device
        now = datetime.now().isoformat()
        policy = DevicePolicy(
            ip=ip,
            mac=mac,
            manufacturer=manufacturer,
            category=category,
            hostname=hostname,
            mode=mode.value,
            is_safety_critical=protected,
            managed=True,
            blocked_domains=domains_to_block,
            created_at=self._devices.get(ip, DevicePolicy(ip="", mac="", manufacturer=None, category="unknown")).created_at or now,
            updated_at=now,
            # A tool-recorded row is NOT a confirmed one. This flag reads as
            # "the owner said yes to this", and a chat turn is not that.
            user_confirmed=(recorded_by != "tool"),
            recorded_by=("tool" if recorded_by == "tool" else "owner"),
            recorded_at=now,
        )
        self._devices[ip] = policy
        self._save_managed_devices()

        dns_result = self.sync_dns() if push else {
            "success": True, "applied": False, "pushed": False,
            "message": ("Recorded only. Nothing was written to the DNS "
                        "controller — that happens when the owner "
                        "approves it."),
        }

        result = {
            "success": bool(pihole_result.get("success", True)) and bool(dns_result.get("success", True)),
            "message": f"Applied {mode.value} policy to {ip} ({manufacturer or 'unknown'} {category})",
            "mode": mode.value,
            "enforcing": self.enforcing,
            "pushed": bool(push),
            "recorded_by": policy.recorded_by,
            "domains_blocked": (len(domains_to_block)
                                if (self.enforcing and push) else 0),
            "domains_recorded": len(domains_to_block),
            "dns_result": dns_result,
            "pihole_result": pihole_result,
        }
        if mode != requested:
            result["mode_override"] = mode.value
            result["message"] = (
                f"{category} is safety-critical or network infrastructure — "
                f"{requested.value} was not applied. {ip} is set to "
                f"ALWAYS_ALLOW and PuffPAI will never block it.")
        if not push:
            result["message"] += (
                " Recorded only — nothing was pushed to AdGuard; the owner "
                "approves that separately.")
        elif not self.enforcing:
            result["message"] += (
                " Zone 3 is in monitor-first mode: this is recorded and "
                "visible in AdGuard, but nothing is blocked yet.")
        return result

    # ── DNS synchronisation ───────────────────────────────────────────

    def _client_name(self, device: DevicePolicy) -> str:
        return f"{self._CLIENT_PREFIX}{device.ip}"

    #: Categories the BALANCED baseline blocks. CLOUD is deliberately absent —
    #: blocking a doorbell's cloud API stops the doorbell, and a privacy tool
    #: that breaks the doorbell gets switched off. LOCKDOWN adds CLOUD back per
    #: device, which is the whole difference between the two modes.
    _BALANCED_CATEGORIES = frozenset({
        DomainCategory.TELEMETRY, DomainCategory.ADS, DomainCategory.TRACKING,
    })

    def build_baseline_block(self) -> list[str]:
        """House-wide BALANCED rules, grouped and labelled by manufacturer.

        This is what makes "turn protection on" mean something on day one,
        before a single device has been discovered or classified: the moment
        enforcement is switched on, every device in the house has telemetry,
        ads and tracking refused, and nothing else.

        It is written as GLOBAL rules (no ``$client=``) on purpose. A rule
        keyed to an IP stops applying the moment DHCP moves that IP, which on
        a home router is a matter of days — so a per-device-only policy quietly
        decays into no policy. Devices that need to differ from the baseline
        get per-device rules below; devices that must never be filtered at all
        are excluded by their AdGuard client entry, not by a rule, and that
        exclusion outranks everything here.
        """
        lines: list[str] = [
            "! ── BASELINE: BALANCED, every device in this house ──",
            "! Telemetry, ads and cross-device tracking. Cloud services a "
            "device needs to work are NOT here — see LOCKDOWN, below.",
        ]
        seen: set[str] = set()
        for manufacturer in sorted(self._telemetry.manufacturers):
            entries = [
                d for d in self._telemetry.get_domains(
                    manufacturer, include_common=False,
                    categories=set(self._BALANCED_CATEGORIES))
                if d.domain not in seen
            ]
            if not entries:
                continue
            lines.append(f"! {manufacturer}")
            for entry in entries:
                seen.add(entry.domain)
                try:
                    lines.append(block_rule(entry.domain))
                except ValueError as exc:
                    logger.warning("Zone3 skipping baseline %s: %s",
                                   entry.domain, exc)
        common = [d for d in self._telemetry.get_domains(
            "", include_common=True, categories=set(self._BALANCED_CATEGORIES))
            if d.domain not in seen]
        if common:
            lines.append("! Common ad and tracking networks (any device)")
            for entry in common:
                seen.add(entry.domain)
                try:
                    lines.append(block_rule(entry.domain))
                except ValueError as exc:
                    logger.warning("Zone3 skipping baseline %s: %s",
                                   entry.domain, exc)
        return lines

    def build_rule_block(self) -> list[str]:
        """The complete rule body: the baseline, then per-device deltas.

        Pure: no I/O, no controller. That is what lets a test assert the exact
        rules a mode produces, and what lets the observe/enforce switch be a
        rendering decision made in exactly one place
        (:meth:`AdGuardController.replace_rule_block`) rather than a branch
        scattered through the policy engine.

        Per device, only the DIFFERENCE from the baseline is emitted:
          BALANCED      nothing — the baseline already is this policy.
          LOCKDOWN      the manufacturer's CLOUD domains, scoped to that
                        device, so one locked-down camera does not take the
                        rest of the house's cloud services with it.
          OPEN          ``@@`` exception rules re-allowing that manufacturer's
                        TELEMETRY domains. AdGuard's allowlist rules outrank
                        blocking rules, so this genuinely narrows the baseline
                        to ads and tracking for that one device.
          ALWAYS_ALLOW  nothing at all; the AdGuard client entry has filtering
                        switched off, which no rule can override.
        """
        lines: list[str] = list(self.build_baseline_block())
        if self._devices:
            lines.append("! ── PER-DEVICE, where it differs from the baseline ──")
        for device in sorted(self._devices.values(), key=lambda d: d.ip):
            protected = _is_protected(device.category, device.is_safety_critical)
            mode = device.security_mode
            label = (f"! {device.ip}  {device.manufacturer or 'unknown'}  "
                     f"{device.category}  {device.mode}")
            if protected or mode == SecurityMode.ALWAYS_ALLOW:
                lines.append(
                    f"{label}  — ALWAYS_ALLOW, never blocked (AdGuard client "
                    f"'{self._client_name(device)}' has filtering off)")
                continue
            if mode == SecurityMode.BALANCED:
                lines.append(f"{label}  — baseline covers this device")
                continue

            manufacturer = device.manufacturer or ""
            if mode == SecurityMode.LOCKDOWN:
                extra = [d.domain for d in self._telemetry.get_domains(
                    manufacturer, include_common=False)
                    if d.category not in self._BALANCED_CATEGORIES]
                if not extra:
                    lines.append(f"{label}  — nothing beyond the baseline is "
                                 f"known for {manufacturer or 'this device'}")
                    continue
                lines.append(f"{label}  — plus cloud services")
                lines.extend(self._client_rules(device, extra))
            elif mode == SecurityMode.OPEN:
                relaxed = [d.domain for d in self._telemetry.get_domains(
                    manufacturer, include_common=False,
                    categories={DomainCategory.TELEMETRY})]
                if not relaxed:
                    lines.append(f"{label}  — ads and tracking only; the "
                                 f"baseline holds no telemetry to re-allow")
                    continue
                lines.append(f"{label}  — telemetry re-allowed for this device")
                lines.extend(self._client_rules(device, relaxed, allow=True))
        lines.extend(self.build_bucket_block())
        return lines

    def _client_rules(self, device: DevicePolicy, domains: list[str], *,
                      allow: bool = False) -> list[str]:
        """Client-scoped rules, skipping (loudly) any row that will not build."""
        out: list[str] = []
        for domain in domains:
            try:
                rule = block_rule(domain, device.ip)
            except ValueError as exc:
                # One bad row must not cost the whole household its policy.
                logger.warning("Zone3 skipping rule %s/%s: %s",
                               device.ip, domain, exc)
                out.append(f"! SKIPPED {domain}: {exc}")
                continue
            out.append(f"@@{rule}" if allow else rule)
        return out

    def sync_dns(self) -> dict:
        """Push the ledger to AdGuard: the rule block plus per-client settings.

        Idempotent and total — it rewrites the whole block from the ledger
        rather than appending, so the rules in AdGuard cannot drift away from
        the recorded policy.

        The ALWAYS_ALLOW client entries are written in BOTH modes. They can
        only ever allow more traffic (``filtering_enabled: false`` makes
        AdGuard skip filtering for that client entirely), so writing them
        before enforcement is on means a smoke alarm is already un-blockable
        on the day the switch is flipped, rather than one API call later.
        """
        if self._adguard is None:
            return {"success": True, "applied": False,
                    "message": "No AdGuard controller configured"}
        if not self._adguard.is_reachable():
            return {"success": False, "applied": False,
                    "message": "AdGuard is not reachable"}

        protected_names: set[str] = set()
        client_errors: list[str] = []
        for device in self._devices.values():
            name = self._client_name(device)
            allow_all = (_is_protected(device.category, device.is_safety_critical)
                         or device.security_mode == SecurityMode.ALWAYS_ALLOW)
            if not allow_all:
                continue
            protected_names.add(name)
            try:
                if not self._adguard.upsert_client(name, [device.ip],
                                                   filtering_enabled=False):
                    client_errors.append(name)
            except ValueError as exc:
                client_errors.append(f"{name}: {exc}")

        # Retire client entries we made for devices that are no longer
        # protected (or no longer managed at all). Only ours — the prefix test
        # keeps a hand-made AdGuard client safe.
        for client in self._adguard.clients():
            name = client.get("name", "")
            if name.startswith(self._CLIENT_PREFIX) and name not in protected_names:
                self._adguard.delete_client(name)

        body = self.build_rule_block()
        ok = self._adguard.replace_rule_block(
            body, note=f"{len(self._devices)} managed device(s)")
        return {
            "success": ok and not client_errors,
            "applied": True,
            "enforcing": self.enforcing,
            "rules": sum(1 for ln in body if not ln.startswith("!")),
            "always_allow_clients": sorted(protected_names),
            "client_errors": client_errors,
            "managed_buckets": len(self._buckets),
        }

    def secure_device(self, ip: str, mac: str, hostname: str = "", *,
                      push: bool = False, recorded_by: str = "owner") -> dict:
        """Auto-classify and apply recommended policy to a device.

        This is the simple path: classify → suggest → apply.
        """
        result = classify_device(mac=mac, hostname=hostname)
        suggested_mode = _DEFAULT_MODES.get(result.category, SecurityMode.BALANCED)

        # Protected devices always get ALWAYS_ALLOW. apply_policy would coerce
        # this anyway; doing it here as well keeps the suggestion the caller
        # sees identical to the policy that lands.
        if _is_protected(result.category, result.is_safety_critical):
            suggested_mode = SecurityMode.ALWAYS_ALLOW

        return self.apply_policy(
            ip=ip,
            mac=mac,
            mode=suggested_mode,
            manufacturer=result.manufacturer,
            hostname=hostname,
            category=result.category.value,
            is_safety_critical=result.is_safety_critical,
            push=push,
            recorded_by=recorded_by,
        )

    def change_device_mode(self, ip: str, new_mode: SecurityMode, *,
                           push: bool = False,
                           recorded_by: str = "owner") -> dict:
        """Change the security mode for an already-managed device."""
        if ip not in self._devices:
            return {"success": False, "message": f"Device {ip} is not managed"}

        device = self._devices[ip]

        # apply_policy performs the coercion for every mode, not just
        # LOCKDOWN; this early return only makes the refusal legible.
        if (_is_protected(device.category, device.is_safety_critical)
                and new_mode != SecurityMode.ALWAYS_ALLOW):
            return {
                "success": False,
                "message": (f"{device.category} at {ip} is safety-critical or "
                            f"network infrastructure. PuffPAI will not put it "
                            f"in {new_mode.value}; it stays ALWAYS_ALLOW."),
                "mode": SecurityMode.ALWAYS_ALLOW.value,
                "mode_override": SecurityMode.ALWAYS_ALLOW.value,
            }

        return self.apply_policy(
            ip=device.ip,
            mac=device.mac,
            mode=new_mode,
            manufacturer=device.manufacturer,
            hostname=device.hostname,
            category=device.category,
            is_safety_critical=device.is_safety_critical,
            push=push,
            recorded_by=recorded_by,
        )


    # ── Bucket (Home Assistant integration) policy ────────────────

    #: Which telemetry categories each mode refuses for a VENDOR CLOUD.
    #: LOCKDOWN is absent on purpose: it is not a category list, it is "every
    #: domain we know for this vendor", including the cloud API itself.
    _BUCKET_MODE_CATEGORIES: dict[SecurityMode, frozenset] = {
        SecurityMode.OPEN: frozenset({DomainCategory.ADS,
                                      DomainCategory.TRACKING}),
        SecurityMode.BALANCED: frozenset({DomainCategory.TELEMETRY,
                                          DomainCategory.ADS,
                                          DomainCategory.TRACKING}),
    }

    @staticmethod
    def _bucket_facts(bucket_id: str) -> dict | None:
        """The bucket roll's row for *bucket_id*, or None. Read-only.

        Split out so a test can hand the engine a bucket without standing up
        Home Assistant, and so the import of :mod:`puffpai.bridge_ha` stays
        lazy: that module reaches the HA registries and the query ledger, and
        the policy engine is imported by paths that must not.
        """
        try:
            from puffpai import bridge_ha
            return bridge_ha.bucket(bucket_id)
        except Exception as exc:              # a read that fails is not a pass
            logger.warning("Zone3 bucket lookup failed for %s: %s",
                           bucket_id, exc)
            return None

    @staticmethod
    def _ha_client_ip() -> str:
        """The hub's address, or "" when it is not a usable rule selector.

        Empty is a REFUSAL upstream, never a fallback to an unscoped rule. A
        bucket rule without ``$client=`` is not a smaller version of this
        policy — it is a house-wide block of a vendor's cloud, applied to
        every device in the building including the ones the owner never named.
        """
        try:
            from puffpai import bridge_ha
            host = (bridge_ha.ha_host() or "").strip()
        except Exception as exc:
            logger.warning("Zone3 could not read the HA host: %s", exc)
            return ""
        if not host:
            return ""
        try:
            _validate_client(host)             # an IP, and not one of ours
        except (ValueError, TypeError):
            return ""
        return host

    def _bucket_domains(self, integration: str,
                        mode: SecurityMode) -> list[str]:
        """Vendor domains this mode refuses for *integration*, sorted, unique.

        Two sources, deliberately: :data:`bridge_ha.INTEGRATION_DOMAIN_SUFFIXES`
        holds the vendor's CLOUD suffixes (``tuyaus.com``, ``ring.com``) and
        the telemetry DB holds the curated per-manufacturer rows with a
        category on each. OPEN and BALANCED take only the categorised rows, so
        the cloud API survives and the integration keeps working. LOCKDOWN
        takes everything from both, which is exactly why it breaks it.
        """
        if mode == SecurityMode.ALWAYS_ALLOW:
            return []
        try:
            from puffpai.bridge_ha import (INTEGRATION_DOMAIN_SUFFIXES,
                                           _MANUFACTURER_INTEGRATIONS)
        except Exception as exc:              # pragma: no cover - defensive
            logger.warning("Zone3 bucket domains unavailable: %s", exc)
            return []

        key = (integration or "").strip().lower()
        manufacturers = [m for m, i in _MANUFACTURER_INTEGRATIONS.items()
                         if i == key]
        categories = self._BUCKET_MODE_CATEGORIES.get(mode)

        domains: set[str] = set()
        for manufacturer in manufacturers:
            entries = self._telemetry.get_domains(
                manufacturer, include_common=False,
                categories=set(categories) if categories else None)
            domains.update(e.domain.strip().lower() for e in entries)
        if categories is None:                # LOCKDOWN: the cloud API too
            domains.update(d.strip().lower()
                           for d in INTEGRATION_DOMAIN_SUFFIXES.get(key, ()))
        return sorted(d for d in domains if d)

    def apply_bucket_policy(self, bucket_id: str, mode: SecurityMode, *,
                            recorded_by: str = "owner",
                            push: bool = False) -> dict:
        """Record (and optionally push) a policy for one HA integration bucket.

        Same ``push``/``recorded_by`` contract as :meth:`apply_policy`, and for
        the same reason — see that docstring.

        Refusals, all of them fail-closed:

        * an id that is not a well-formed bucket id, or a bucket the roll does
          not know: there is nothing to count devices from, and a policy whose
          blast radius cannot be described cannot be proposed;
        * a bucket that is not ``cloud:*``: a local integration sends nothing
          to a vendor, so there is nothing to block and a rule here would only
          create the impression that something was done;
        * no usable Home Assistant address: see :meth:`_ha_client_ip`.
        """
        wanted = (bucket_id or "").strip()
        try:
            from puffpai.bridge_ha import BUCKET_ID_RE, TIER_CLOUD, TIER_LOCAL
        except Exception as exc:              # pragma: no cover - defensive
            return {"success": False,
                    "message": f"The Home Assistant bridge is unavailable: {exc}"}
        if not BUCKET_ID_RE.match(wanted):
            return {"success": False,
                    "message": (f"{wanted!r} is not an integration id. They "
                                f"look like 'cloud:ring'.")}

        facts = self._bucket_facts(wanted)
        if facts is None:
            return {"success": False,
                    "message": (f"PuffPAI does not know an integration called "
                                f"{wanted}. Ask her to re-read the Home "
                                f"Assistant registries first.")}

        tier = str(facts.get("tier") or wanted.split(":", 1)[0])
        integration = str(facts.get("integration")
                          or wanted.split(":", 1)[1])
        device_count = int(facts.get("device_count") or 0)

        if tier == TIER_LOCAL:
            return {"success": False, "tier": tier, "bucket": wanted,
                    "integration": integration, "device_count": device_count,
                    "message": (f"{integration} is local only — nothing "
                                f"leaves the house for it, so there is "
                                f"nothing to block. Blocking a name it never "
                                f"looks up would only look like protection.")}
        if tier != TIER_CLOUD:
            return {"success": False, "tier": tier, "bucket": wanted,
                    "integration": integration, "device_count": device_count,
                    "message": (f"Integration policy covers cloud-backed "
                                f"integrations. {integration} is '{tier}', "
                                f"and PuffPAI will not guess what its traffic "
                                f"is.")}

        client_ip = self._ha_client_ip()
        if not client_ip:
            return {"success": False, "bucket": wanted,
                    "integration": integration, "device_count": device_count,
                    "message": ("Home Assistant's address is not a usable "
                                "rule target, so PuffPAI cannot scope this to "
                                "the hub. She will not write a house-wide "
                                "block instead. Set home_assistant.url to the "
                                "hub's IP and try again.")}

        domains = self._bucket_domains(integration, mode)
        now = datetime.now().isoformat()
        existing = self._buckets.get(wanted)
        policy = BucketPolicy(
            bucket_id=wanted,
            integration=integration,
            tier=tier,
            mode=mode.value,
            device_count=device_count,
            client_ip=client_ip,
            blocked_domains=domains,
            created_at=(existing.created_at if existing else now) or now,
            updated_at=now,
            recorded_by=("tool" if recorded_by == "tool" else "owner"),
            recorded_at=now,
        )
        self._buckets[wanted] = policy
        self._save_managed_buckets()

        dns_result = self.sync_dns() if push else {
            "success": True, "applied": False, "pushed": False,
            "message": ("Recorded only. Nothing was written to the DNS "
                        "controller."),
        }

        message = (f"Recorded {integration} ({device_count} device(s)) as "
                   f"{mode.value.upper()}: {len(domains)} domain(s), scoped "
                   f"to Home Assistant.")
        result = {
            "success": bool(dns_result.get("success", True)),
            "bucket": wanted,
            "integration": integration,
            "tier": tier,
            "mode": mode.value,
            "device_count": device_count,
            "client_ip": client_ip,
            "pushed": bool(push),
            "recorded_by": policy.recorded_by,
            "enforcing": self.enforcing,
            "domains_recorded": len(domains),
            "domains_blocked": len(domains) if (self.enforcing and push) else 0,
            "effect": BUCKET_MODE_EFFECTS.get(mode.value, ""),
            "dns_result": dns_result,
        }
        if mode == SecurityMode.LOCKDOWN:
            result["breaks_integration"] = True
            result["warning"] = bucket_lockdown_warning(integration,
                                                        device_count)
            message += " " + result["warning"]
        else:
            result["breaks_integration"] = False
        if not push:
            message += (" Recorded only — nothing was pushed to AdGuard.")
        result["message"] = message
        return result

    def remove_bucket_policy(self, bucket_id: str, *,
                             push: bool = False) -> dict:
        """Stop managing an integration. Same push contract as everything else."""
        wanted = (bucket_id or "").strip()
        if wanted not in self._buckets:
            return {"success": False,
                    "message": f"{wanted} is not under integration policy"}
        removed = self._buckets.pop(wanted)
        self._save_managed_buckets()
        dns_result = self.sync_dns() if push else {
            "success": True, "applied": False, "pushed": False}
        return {"success": bool(dns_result.get("success", True)),
                "bucket": wanted, "integration": removed.integration,
                "pushed": bool(push), "dns_result": dns_result,
                "message": (f"{removed.integration} is no longer under "
                            f"integration policy.")}

    def get_managed_buckets(self) -> list[dict]:
        """Every recorded bucket policy, id order."""
        return [b.to_dict() for b in sorted(self._buckets.values(),
                                            key=lambda b: b.bucket_id)]

    def get_bucket_policy(self, bucket_id: str) -> dict | None:
        found = self._buckets.get((bucket_id or "").strip())
        return found.to_dict() if found else None

    def build_bucket_block(self) -> list[str]:
        """Bucket rules: the vendor's domains, scoped to the HUB's client.

        Pure, like :meth:`build_rule_block` — no controller, no I/O.
        """
        lines: list[str] = []
        if not self._buckets:
            return lines
        lines.append("! ── HOME ASSISTANT INTEGRATIONS, scoped to the hub ──")
        lines.append("! These rules name the HUB as the client, because the "
                     "hub is what talks to the vendor's cloud.")
        for policy in sorted(self._buckets.values(),
                             key=lambda b: b.bucket_id):
            mode = policy.security_mode
            provenance = ("  (recorded by a chat tool, not yet approved)"
                          if policy.recorded_by == "tool" else "")
            label = (f"! {policy.bucket_id}  {policy.integration}  "
                     f"{policy.device_count} device(s)  "
                     f"{policy.mode}{provenance}")
            if mode == SecurityMode.ALWAYS_ALLOW or not policy.blocked_domains:
                lines.append(f"{label}  — nothing blocked")
                continue
            if mode == SecurityMode.LOCKDOWN:
                lines.append(f"{label}  — BREAKS THIS INTEGRATION "
                             f"(the cloud API is included)")
            else:
                lines.append(label)
            for domain in policy.blocked_domains:
                try:
                    lines.append(block_rule(domain, policy.client_ip))
                except ValueError as exc:
                    logger.warning("Zone3 skipping bucket rule %s/%s: %s",
                                   policy.bucket_id, domain, exc)
                    lines.append(f"! SKIPPED {domain}: {exc}")
        return lines

    def _load_managed_buckets(self):
        """Load recorded bucket policies from disk."""
        try:
            if self._buckets_file.exists():
                with open(self._buckets_file, "r") as f:
                    data = json.load(f)
                for row in data or []:
                    policy = BucketPolicy.from_dict(row)
                    if policy.bucket_id:
                        self._buckets[policy.bucket_id] = policy
        except (json.JSONDecodeError, IOError, TypeError) as e:
            logger.warning("Failed to load managed buckets: %s", e)

    def _save_managed_buckets(self):
        """Persist recorded bucket policies."""
        try:
            _write_private_json(
                self._buckets_file,
                [b.to_dict() for b in self._buckets.values()])
        except (IOError, OSError) as e:
            logger.error("Failed to save managed buckets: %s", e)

    def remove_device(self, ip: str) -> dict:
        """Stop managing a device and remove its DNS blocks."""
        if ip not in self._devices:
            return {"success": False, "message": f"Device {ip} is not managed"}

        device = self._devices[ip]

        # Unblock domains in Pi-hole
        if self._pihole and device.blocked_domains:
            self._pihole.unblock_domains(device.blocked_domains)

        del self._devices[ip]
        self._save_managed_devices()

        # Rewriting the whole block from the (now shorter) ledger is what
        # actually removes this device's rules; sync_dns also retires the
        # AdGuard client entry, because the loop there deletes any
        # ``puffpai:`` client that is no longer in the protected set.
        dns_result = self.sync_dns()
        if self._adguard is not None:
            self._adguard.delete_client(self._client_name(device))

        return {
            "success": bool(dns_result.get("success", True)),
            "message": f"Removed {ip} from Zone 3 management",
            "unblocked_domains": len(device.blocked_domains),
            "dns_result": dns_result,
        }

    # ── Query / Status ────────────────────────────────────────────────

    def get_managed_devices(self) -> list[dict]:
        """Get all managed devices and their policies."""
        return [d.to_dict() for d in self._devices.values()]

    def get_device_status(self, ip: str) -> dict | None:
        """Get the policy status of a specific managed device."""
        if ip in self._devices:
            return self._devices[ip].to_dict()
        return None

    def get_zone3_status(self) -> dict:
        """Get overall Zone 3 status summary."""
        pihole_status = None
        if self._pihole:
            try:
                pihole_status = {
                    "reachable": self._pihole.is_reachable(),
                }
                if pihole_status["reachable"]:
                    s = self._pihole.get_status()
                    pihole_status.update({
                        "blocking_enabled": s.blocking_enabled,
                        "domains_blocked": s.domains_blocked,
                        "queries_today": s.dns_queries_today,
                        "ads_blocked_today": s.ads_blocked_today,
                    })
            except Exception as e:
                pihole_status = {"reachable": False, "error": str(e)}

        mode_counts = {}
        for d in self._devices.values():
            mode_counts[d.mode] = mode_counts.get(d.mode, 0) + 1

        adguard_status: dict | None = None
        if self._adguard:
            try:
                s = self._adguard.status()
                adguard_status = {
                    "reachable": s.reachable,
                    "version": s.version,
                    "protection_enabled": s.protection_enabled,
                    "active_puffpai_rules": s.rules_count,
                    "queries_today": s.queries_today,
                    "blocked_today": s.blocked_today,
                    "url": self._adguard.base_url,
                }
                if s.reachable:
                    adguard_status["upstreams"] = self._adguard.upstreams()
            except Exception as e:  # never let a status call raise
                adguard_status = {"reachable": False, "error": str(e)}

        recorded = sum(len(d.blocked_domains) for d in self._devices.values())
        bucket_recorded = sum(len(b.blocked_domains)
                              for b in self._buckets.values())
        return {
            "managed_devices": len(self._devices),
            # The integration half of the policy. Reported separately from the
            # device half rather than folded into it: they have different
            # subjects (a device vs. the hub) and different failure modes (a
            # LOCKDOWN bucket breaks an integration outright), so one combined
            # number would hide the loud one inside the quiet one.
            "managed_buckets": self.get_managed_buckets(),
            "managed_bucket_count": len(self._buckets),
            "bucket_recorded_domains": bucket_recorded,
            "bucket_blocked_domains": (bucket_recorded if self.enforcing
                                       else 0),
            "mode_distribution": mode_counts,
            "safety_critical_count": sum(
                1 for d in self._devices.values()
                if _is_protected(d.category, d.is_safety_critical)),
            # Two different numbers on purpose. "recorded" is what the policy
            # says; "blocked" is what is actually in force. In monitor-first
            # mode they differ, and collapsing them into one field is how an
            # operator concludes the house is filtered when it is not.
            "total_recorded_domains": recorded,
            "total_blocked_domains": recorded if self.enforcing else 0,
            "enforcing": self.enforcing,
            "mode": "enforce" if self.enforcing else "monitor-first",
            "adguard": adguard_status,
            "pihole": pihole_status,
        }

    def generate_security_report(self, ip: str) -> dict:
        """Generate a human-readable security report for a device.

        Shows what PuffPAI is blocking and why.
        """
        if ip not in self._devices:
            return {"success": False, "message": f"Device {ip} is not managed"}

        device = self._devices[ip]
        manufacturer = device.manufacturer or "Unknown"

        # Get telemetry summary
        summary = self._telemetry.get_summary(manufacturer) if manufacturer != "Unknown" else {}

        # Get recent DNS queries from whichever controller is wired up.
        recent_queries = []
        if self._adguard:
            try:
                recent_queries = self._adguard.recent_queries(client_ip=ip, limit=20)
            except Exception:
                pass
        elif self._pihole:
            try:
                recent_queries = self._pihole.get_recent_queries(client_ip=ip, limit=20)
            except Exception:
                pass

        return {
            "success": True,
            "device": device.to_dict(),
            "telemetry_summary": summary,
            "recent_queries": recent_queries,
            "enforcing": self.enforcing,
            "explanation": self._explain_mode(device),
        }

    def _explain_mode(self, device: DevicePolicy) -> str:
        """Generate a human-readable explanation of a device's policy."""
        mode = device.security_mode
        manufacturer = device.manufacturer or "this device"
        cat = device.category

        # Said first, every time, so no explanation can be read as a claim
        # that something is being blocked when Zone 3 is still only watching.
        if not self.enforcing and mode != SecurityMode.ALWAYS_ALLOW:
            return (
                f"WATCHING, NOT BLOCKING. Zone 3 is in monitor-first mode: "
                f"every name this {cat} looks up is written to the query log, "
                f"and nothing is refused. The {mode.value} policy below is "
                f"recorded and ready — {len(device.blocked_domains)} domains "
                f"for {manufacturer} — and takes effect the moment enforcement "
                f"is switched on.")

        if mode == SecurityMode.ALWAYS_ALLOW:
            return (f"This {cat} is safety-critical. PuffPAI will NEVER block its "
                    f"network access. It can always communicate freely.")

        if mode == SecurityMode.LOCKDOWN:
            return (f"LOCKDOWN: All known outbound domains for {manufacturer} are blocked. "
                    f"This {cat} cannot phone home, send telemetry, or load ads. "
                    f"Cloud-dependent features will not work. "
                    f"{len(device.blocked_domains)} domains blocked.")

        if mode == SecurityMode.BALANCED:
            return (f"BALANCED: Telemetry, ads, and tracking domains for {manufacturer} "
                    f"are blocked, but cloud services needed for the device to function "
                    f"are allowed. {len(device.blocked_domains)} domains blocked.")

        if mode == SecurityMode.OPEN:
            return (f"OPEN: Only ads and cross-device tracking are blocked for {manufacturer}. "
                    f"The device retains full functionality. "
                    f"{len(device.blocked_domains)} domains blocked.")

        return f"Unknown mode: {mode.value}"

    # ── Pi-hole Group Setup ───────────────────────────────────────────

    def setup_pihole_groups(self) -> dict:
        """Create the PuffPAI groups in Pi-hole.

        Call this once after Pi-hole is first deployed.
        """
        if not self._pihole:
            return {"success": False, "message": "Pi-hole not configured"}

        results = []
        for mode, group_name in self._GROUP_NAMES.items():
            result = self._pihole.create_group(
                group_name,
                f"PuffPAI Zone 3 — {mode.value} security policy",
            )
            results.append({"group": group_name, **result})

        all_ok = all(r.get("success") for r in results)
        return {
            "success": all_ok,
            "message": f"Created {len(results)} Pi-hole groups",
            "groups": results,
        }

    # ── Internal ──────────────────────────────────────────────────────

    def _get_domains_for_mode(
        self,
        mode: SecurityMode,
        manufacturer: str | None,
    ) -> list[str]:
        """Get the domain blocklist for a given mode and manufacturer."""
        if mode == SecurityMode.ALWAYS_ALLOW:
            return []
        if not manufacturer:
            # Unknown manufacturer — use common tracking domains only
            if mode == SecurityMode.LOCKDOWN:
                return self._telemetry.get_domain_strings("", include_common=True)
            elif mode == SecurityMode.BALANCED:
                return self._telemetry.get_telemetry_only("")
            else:
                return self._telemetry.get_ads_only("")

        if mode == SecurityMode.LOCKDOWN:
            return self._telemetry.get_all_outbound(manufacturer)
        elif mode == SecurityMode.BALANCED:
            return self._telemetry.get_telemetry_only(manufacturer)
        elif mode == SecurityMode.OPEN:
            return self._telemetry.get_ads_only(manufacturer)
        return []

    def _load_managed_devices(self):
        """Load managed devices from disk."""
        try:
            if self._managed_file.exists():
                with open(self._managed_file, "r") as f:
                    data = json.load(f)
                    for d in data:
                        policy = DevicePolicy.from_dict(d)
                        self._devices[policy.ip] = policy
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load managed devices: %s", e)

    def _save_managed_devices(self):
        """Persist managed devices to disk."""
        try:
            _write_private_json(
                self._managed_file,
                [d.to_dict() for d in self._devices.values()])
        except (IOError, OSError) as e:
            logger.error("Failed to save managed devices: %s", e)
