"""
PuffPAI Security Package
Three-zone security architecture for privacy-forward personal AI.

Zone 1 (VAULT):         PII, encrypted at rest, MFA-gated
Zone 2 (INTELLIGENCE):  Models, knowledge base, persona config
Zone 3 (NETWORK_EDGE):  Firewall rules, device state, scan results
"""

from .zones import DataZone, ZonePolicy, ZONE_CLASSIFICATIONS
from .audit_log import AuditLog
from .data_classifier import DataClassifier
from .device_classifier import DeviceClassifier

__all__ = [
    "DataZone",
    "ZonePolicy",
    "ZONE_CLASSIFICATIONS",
    "AuditLog",
    "DataClassifier",
    "DeviceClassifier",
]
