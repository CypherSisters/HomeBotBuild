"""Tests for device classification and traffic policies."""

import pytest
from puffpai.security.device_classifier import (
    DeviceClassifier, DeviceCategory, DEVICE_POLICIES,
    normalize_mac, get_oui_prefix
)


@pytest.fixture
def classifier():
    return DeviceClassifier()


class TestNormalizeMAC:
    def test_colon_format(self):
        assert normalize_mac("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"

    def test_hyphen_format(self):
        assert normalize_mac("AA-BB-CC-DD-EE-FF") == "AA:BB:CC:DD:EE:FF"

    def test_rejects_invalid(self):
        with pytest.raises(ValueError):
            normalize_mac("not-a-mac")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            normalize_mac("")


class TestDeviceClassifier:
    def test_classify_amazon_echo(self, classifier):
        result = classifier.classify("44:65:0D:AA:BB:CC")
        assert result.manufacturer == "Amazon"
        assert result.category == DeviceCategory.SMART_SPEAKER
        assert result.confidence == "high"

    def test_classify_apple_device(self, classifier):
        result = classifier.classify("A4:83:E7:AA:BB:CC")
        assert result.manufacturer == "Apple"
        assert result.category == DeviceCategory.COMPUTER

    def test_classify_unknown_mac(self, classifier):
        result = classifier.classify("AA:BB:CC:DD:EE:FF")
        assert result.category == DeviceCategory.UNKNOWN
        assert result.confidence == "low"

    def test_classify_ring_camera(self, classifier):
        result = classifier.classify("B0:09:DA:AA:BB:CC")
        assert result.manufacturer == "Ring"
        assert result.category == DeviceCategory.IOT_CAMERA

    def test_classify_hp_printer(self, classifier):
        result = classifier.classify("00:11:85:AA:BB:CC")
        assert result.manufacturer == "HP"
        assert result.category == DeviceCategory.PRINTER

    def test_classify_invalid_mac(self, classifier):
        result = classifier.classify("invalid")
        assert result.category == DeviceCategory.UNKNOWN


class TestHostnameRefinement:
    def test_apple_iphone(self, classifier):
        result = classifier.classify_with_hostname(
            "A4:83:E7:AA:BB:CC", "Johns-iPhone")
        assert result.category == DeviceCategory.PHONE

    def test_apple_ipad(self, classifier):
        result = classifier.classify_with_hostname(
            "A4:83:E7:AA:BB:CC", "My-iPad")
        assert result.category == DeviceCategory.TABLET

    def test_medical_device_keyword(self, classifier):
        result = classifier.classify_with_hostname(
            "AA:BB:CC:DD:EE:FF", "cpap-machine")
        assert result.category == DeviceCategory.MEDICAL_DEVICE
        assert result.is_safety_critical is True

    def test_fire_alarm_keyword(self, classifier):
        result = classifier.classify_with_hostname(
            "AA:BB:CC:DD:EE:FF", "nest-protect-smoke")
        assert result.category == DeviceCategory.SAFETY_DEVICE
        assert result.is_safety_critical is True


class TestCanBlock:
    def test_can_block_normal_device(self, classifier):
        can, reason = classifier.can_block("44:65:0D:AA:BB:CC")
        assert can is True

    def test_cannot_block_medical(self, classifier):
        can, reason = classifier.can_block("AA:BB:CC:DD:EE:FF", "cpap-monitor")
        assert can is False
        assert "SAFETY-CRITICAL" in reason

    def test_cannot_block_network_infra(self, classifier):
        can, reason = classifier.can_block("00:14:6C:AA:BB:CC")  # NETGEAR
        assert can is False
        assert "Network infrastructure" in reason


class TestTrafficPolicies:
    def test_unknown_devices_deny_by_default(self):
        policy = DEVICE_POLICIES[DeviceCategory.UNKNOWN]
        assert policy.allow_external_access is False
        assert policy.allowed_outbound_ports == (53,)  # DNS only

    def test_medical_is_safety_critical(self):
        policy = DEVICE_POLICIES[DeviceCategory.MEDICAL_DEVICE]
        assert policy.is_safety_critical is True

    def test_cameras_restricted(self):
        policy = DEVICE_POLICIES[DeviceCategory.IOT_CAMERA]
        assert policy.allow_external_access is False

    def test_printers_local_only(self):
        policy = DEVICE_POLICIES[DeviceCategory.PRINTER]
        assert policy.allow_external_access is False


# ── Enhanced heuristic classifiers ────────────────────────────────────────

from puffpai.security.device_classifier import (
    classify_by_hostname, classify_by_mdns, classify_by_ssdp, classify_device,
)


class TestClassifyByHostname:
    def test_roku(self):
        assert classify_by_hostname("Roku-5F3A") == DeviceCategory.SMART_TV

    def test_fire_tv(self):
        assert classify_by_hostname("FireTV-Stick") == DeviceCategory.SMART_TV

    def test_ring_doorbell(self):
        assert classify_by_hostname("Ring-Doorbell-Pro") == DeviceCategory.IOT_CAMERA

    def test_echo_dot(self):
        assert classify_by_hostname("Echo-Dot-3rd") == DeviceCategory.SMART_SPEAKER

    def test_macbook(self):
        assert classify_by_hostname("MacBook-Pro") == DeviceCategory.COMPUTER

    def test_iphone(self):
        assert classify_by_hostname("iPhone-15-Pro") == DeviceCategory.PHONE

    def test_nest_protect(self):
        assert classify_by_hostname("Nest-Protect-2nd") == DeviceCategory.SAFETY_DEVICE

    def test_no_match_returns_none(self):
        assert classify_by_hostname("generic-device") is None

    def test_empty_returns_none(self):
        assert classify_by_hostname("") is None


class TestClassifyByMdns:
    def test_airplay(self):
        assert classify_by_mdns("_airplay._tcp") == DeviceCategory.SMART_TV

    def test_printer(self):
        assert classify_by_mdns("_printer._tcp") == DeviceCategory.PRINTER

    def test_ipp(self):
        assert classify_by_mdns("_ipp._tcp") == DeviceCategory.PRINTER

    def test_ssh(self):
        assert classify_by_mdns("_ssh._tcp") == DeviceCategory.COMPUTER

    def test_homekit(self):
        assert classify_by_mdns("_homekit._tcp") == DeviceCategory.IOT_APPLIANCE

    def test_unknown(self):
        assert classify_by_mdns("_unknown._tcp") is None

    def test_empty(self):
        assert classify_by_mdns("") is None


class TestClassifyBySsdp:
    def test_roku(self):
        assert classify_by_ssdp("Roku/DVP-10.5") == DeviceCategory.SMART_TV

    def test_xbox(self):
        assert classify_by_ssdp("Xbox/10.0") == DeviceCategory.GAMING

    def test_sonos(self):
        assert classify_by_ssdp("Sonos/78.0") == DeviceCategory.SMART_SPEAKER

    def test_no_match(self):
        assert classify_by_ssdp("generic-server/1.0") is None


class TestClassifyDevice:
    def test_known_mac_returns_oui_result(self):
        result = classify_device("00:03:93:AA:BB:CC")  # Apple
        assert result.manufacturer == "Apple"
        assert result.confidence == "high"

    def test_unknown_mac_with_hostname(self):
        result = classify_device("AA:BB:CC:DD:EE:FF", hostname="Roku-Ultra")
        assert result.category == DeviceCategory.SMART_TV

    def test_unknown_mac_with_mdns(self):
        result = classify_device("AA:BB:CC:DD:EE:FF",
                                 mdns_services=["_printer._tcp"])
        assert result.category == DeviceCategory.PRINTER

    def test_unknown_mac_with_ssdp(self):
        result = classify_device("AA:BB:CC:DD:EE:FF",
                                 ssdp_header="Xbox/10.0")
        assert result.category == DeviceCategory.GAMING

    def test_priority_hostname_over_mdns(self):
        """Hostname should take priority over mDNS when OUI is unknown."""
        result = classify_device("AA:BB:CC:DD:EE:FF",
                                 hostname="Roku-5F3A",
                                 mdns_services=["_printer._tcp"])
        assert result.category == DeviceCategory.SMART_TV

    def test_all_unknown_returns_unknown(self):
        result = classify_device("AA:BB:CC:DD:EE:FF")
        assert result.category == DeviceCategory.UNKNOWN
