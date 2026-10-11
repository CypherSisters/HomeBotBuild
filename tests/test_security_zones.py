"""Tests for the security zones module."""

import pytest
from puffpai.security.zones import (
    DataZone, ZonePolicy, ZONE_CLASSIFICATIONS,
    get_zone_for_file, get_policy, check_access
)


class TestDataZone:
    def test_vault_is_zone_1(self):
        assert DataZone.VAULT.value == 1

    def test_intelligence_is_zone_2(self):
        assert DataZone.INTELLIGENCE.value == 2

    def test_network_edge_is_zone_3(self):
        assert DataZone.NETWORK_EDGE.value == 3


class TestZonePolicy:
    def test_vault_requires_auth(self):
        policy = ZONE_CLASSIFICATIONS[DataZone.VAULT]["policy"]
        assert policy.requires_auth is True
        assert policy.encrypted_at_rest is True
        assert policy.network_accessible is False

    def test_vault_allows_auth_operations(self):
        policy = ZONE_CLASSIFICATIONS[DataZone.VAULT]["policy"]
        assert policy.allows("read_with_auth") is True
        assert policy.allows("write_with_auth") is True
        assert policy.allows("read") is False

    def test_intelligence_no_auth(self):
        policy = ZONE_CLASSIFICATIONS[DataZone.INTELLIGENCE]["policy"]
        assert policy.requires_auth is False
        assert policy.allows("read") is True
        assert policy.allows("query") is True

    def test_network_edge_network_accessible(self):
        policy = ZONE_CLASSIFICATIONS[DataZone.NETWORK_EDGE]["policy"]
        assert policy.network_accessible is True
        assert policy.allows("block") is True


class TestGetZoneForFile:
    def test_profiles_is_vault(self):
        assert get_zone_for_file("profiles.json") == DataZone.VAULT

    def test_chat_history_is_vault(self):
        assert get_zone_for_file("chat_history/") == DataZone.VAULT

    def test_feedback_is_vault(self):
        assert get_zone_for_file("feedback_data.jsonl") == DataZone.VAULT

    def test_ha_config_is_vault(self):
        assert get_zone_for_file("home_assistant_config.json") == DataZone.VAULT

    def test_models_is_intelligence(self):
        assert get_zone_for_file("models/") == DataZone.INTELLIGENCE

    def test_blocked_devices_is_edge(self):
        assert get_zone_for_file("blocked_devices.json") == DataZone.NETWORK_EDGE

    def test_unknown_file_returns_none(self):
        assert get_zone_for_file("random_file.txt") is None


class TestCheckAccess:
    def test_vault_denied_without_auth(self):
        assert check_access(DataZone.VAULT, "read_with_auth", authenticated=False) is False

    def test_vault_allowed_with_auth(self):
        assert check_access(DataZone.VAULT, "read_with_auth", authenticated=True) is True

    def test_intelligence_allowed_without_auth(self):
        assert check_access(DataZone.INTELLIGENCE, "read", authenticated=False) is True

    def test_invalid_operation_denied(self):
        assert check_access(DataZone.INTELLIGENCE, "delete", authenticated=True) is False
