"""
PuffPAI Zone 3 — Network Edge Security (the Bridge)

The boundary between your home and the internet.
DNS sinkhole, device fingerprinting, outbound telemetry blocking.

This zone faces the internet but cannot read Zones 1 or 2.

Components:
    AdGuardController — AdGuard Home REST API; the ACTIVE sinkhole path,
                        running on the appliance itself
    PiholeController  — Pi-hole API integration; legacy, not the active path
    TelemetryDB       — Known phone-home domains per manufacturer
    PolicyEngine      — Per-device security modes (lockdown/balanced/open)
"""

from .adguard_controller import AdGuardController, AdGuardStatus, block_rule
from .pihole_controller import PiholeController
from .telemetry_domains import TelemetryDB
from .policy_engine import SecurityMode, DevicePolicy, PolicyEngine

__all__ = [
    "AdGuardController",
    "AdGuardStatus",
    "block_rule",
    "PiholeController",
    "TelemetryDB",
    "SecurityMode",
    "DevicePolicy",
    "PolicyEngine",
]
