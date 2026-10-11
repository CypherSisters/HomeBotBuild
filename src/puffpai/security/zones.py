"""
PuffPAI Three-Zone Data Model

Zone 1 (VAULT):         PII, encrypted at rest, requires auth
Zone 2 (INTELLIGENCE):  Models, knowledge base, persona config
Zone 3 (NETWORK_EDGE):  Firewall rules, device state, scan results

Every file and data object in PuffPAI belongs to exactly one zone.
Zone policies are enforced at the data access layer.
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass
from typing import Any


class DataZone(Enum):
    """Security zones ordered by sensitivity (higher = more sensitive)."""
    VAULT = 1           # Zone 1: PII, encrypted, MFA-gated
    INTELLIGENCE = 2    # Zone 2: Models, knowledge, persona config
    NETWORK_EDGE = 3    # Zone 3: Firewall rules, device state, scan results


@dataclass(frozen=True)
class ZonePolicy:
    """Immutable security policy for a data zone."""
    zone: DataZone
    encrypted_at_rest: bool
    requires_auth: bool
    network_accessible: bool
    allowed_operations: tuple[str, ...]

    def allows(self, operation: str) -> bool:
        """Check if an operation is permitted in this zone."""
        return operation in self.allowed_operations


ZONE_CLASSIFICATIONS: dict[DataZone, dict[str, Any]] = {
    DataZone.VAULT: {
        "description": "Personal identifiable information -- encrypted, auth-gated",
        "files": [
            "profiles.json",
            "chat_history/",
            "feedback_data.jsonl",
            "home_assistant_config.json",
        ],
        "policy": ZonePolicy(
            zone=DataZone.VAULT,
            encrypted_at_rest=True,
            requires_auth=True,
            network_accessible=False,
            allowed_operations=(
                "read_with_auth", "write_with_auth", "delete_with_auth",
            ),
        ),
    },
    DataZone.INTELLIGENCE: {
        "description": "AI models, knowledge base, persona configuration",
        "files": [
            "kiwix_rag_db/",
            "models/",
            "config/persona.txt",
            "config/puffpai_config.json",
        ],
        "policy": ZonePolicy(
            zone=DataZone.INTELLIGENCE,
            encrypted_at_rest=True,
            requires_auth=False,
            network_accessible=False,
            allowed_operations=("read", "index", "query"),
        ),
    },
    DataZone.NETWORK_EDGE: {
        "description": "Firewall rules, device state, network scan results",
        "files": [
            "blocked_devices.json",
            "ha_seen_devices.json",
        ],
        "policy": ZonePolicy(
            zone=DataZone.NETWORK_EDGE,
            encrypted_at_rest=False,
            requires_auth=False,
            network_accessible=True,
            allowed_operations=("read", "write", "scan", "block", "unblock"),
        ),
    },
}


def get_zone_for_file(filename: str) -> DataZone | None:
    """Determine which zone a file belongs to by its name or path."""
    for zone, config in ZONE_CLASSIFICATIONS.items():
        for pattern in config["files"]:
            if pattern.endswith("/"):
                if filename.startswith(pattern) or filename == pattern.rstrip("/"):
                    return zone
            else:
                if filename == pattern or filename.endswith(f"/{pattern}"):
                    return zone
    return None


def get_policy(zone: DataZone) -> ZonePolicy:
    """Get the security policy for a zone."""
    return ZONE_CLASSIFICATIONS[zone]["policy"]


def check_access(zone: DataZone, operation: str, authenticated: bool = False) -> bool:
    """Check whether an operation is allowed on a zone."""
    policy = get_policy(zone)
    if not policy.allows(operation):
        return False
    if policy.requires_auth and not authenticated:
        return False
    return True
