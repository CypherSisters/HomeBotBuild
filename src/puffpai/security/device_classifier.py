"""
PuffPAI Device Classifier
Classifies network devices by MAC OUI and assigns security policies.

Uses a subset of the IEEE OUI database (public data).
Safety-critical devices (medical, fire alarms) get hardcoded allowlists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class DeviceCategory(Enum):
    COMPUTER = "computer"
    PHONE = "phone"
    TABLET = "tablet"
    SMART_SPEAKER = "smart_speaker"
    IOT_CAMERA = "iot_camera"
    SMART_TV = "smart_tv"
    PRINTER = "printer"
    GAMING = "gaming"
    NETWORK_INFRA = "network_infra"
    MEDICAL_DEVICE = "medical_device"
    SAFETY_DEVICE = "safety_device"
    IOT_APPLIANCE = "iot_appliance"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TrafficPolicy:
    category: DeviceCategory
    allowed_outbound_ports: tuple
    allow_external_access: bool
    allow_local_discovery: bool
    description: str
    is_safety_critical: bool = False


DEVICE_POLICIES = {
    DeviceCategory.COMPUTER: TrafficPolicy(
        DeviceCategory.COMPUTER, (80, 443, 53, 22, 993, 587, 143, 110),
        True, True, "Full internet access for computers"),
    DeviceCategory.PHONE: TrafficPolicy(
        DeviceCategory.PHONE, (80, 443, 53, 993, 587, 5353),
        True, True, "Full internet access for phones"),
    DeviceCategory.TABLET: TrafficPolicy(
        DeviceCategory.TABLET, (80, 443, 53, 5353),
        True, True, "Internet access for tablets"),
    DeviceCategory.SMART_SPEAKER: TrafficPolicy(
        DeviceCategory.SMART_SPEAKER, (80, 443, 53, 8443, 5353),
        True, True, "Cloud access for voice assistants"),
    DeviceCategory.IOT_CAMERA: TrafficPolicy(
        DeviceCategory.IOT_CAMERA, (443, 53, 8883),
        False, True, "Restricted: cameras should not phone home freely"),
    DeviceCategory.SMART_TV: TrafficPolicy(
        DeviceCategory.SMART_TV, (80, 443, 53, 8443),
        True, True, "Streaming access for smart TVs"),
    DeviceCategory.PRINTER: TrafficPolicy(
        DeviceCategory.PRINTER, (53,),
        False, True, "Printers: local only, no internet"),
    DeviceCategory.GAMING: TrafficPolicy(
        DeviceCategory.GAMING, (80, 443, 53, 3478, 3479, 3480),
        True, True, "Gaming consoles need online services"),
    DeviceCategory.NETWORK_INFRA: TrafficPolicy(
        DeviceCategory.NETWORK_INFRA, (53, 123, 80, 443),
        True, True, "Network infrastructure"),
    DeviceCategory.MEDICAL_DEVICE: TrafficPolicy(
        DeviceCategory.MEDICAL_DEVICE, (443, 53),
        False, False, "SAFETY-CRITICAL: Medical devices -- never block",
        is_safety_critical=True),
    DeviceCategory.SAFETY_DEVICE: TrafficPolicy(
        DeviceCategory.SAFETY_DEVICE, (443, 53, 80),
        True, True, "SAFETY-CRITICAL: Fire/CO/security alarms -- never block",
        is_safety_critical=True),
    DeviceCategory.IOT_APPLIANCE: TrafficPolicy(
        DeviceCategory.IOT_APPLIANCE, (443, 53, 8883, 1883),
        False, True, "IoT appliances: restricted external access"),
    DeviceCategory.UNKNOWN: TrafficPolicy(
        DeviceCategory.UNKNOWN, (53,),
        False, False, "Unknown devices: deny by default, DNS only"),
}

# Subset of IEEE OUI database -- common home network devices
_OUI_DATABASE = {
    # Apple
    "00:03:93": "Apple", "00:1B:63": "Apple", "00:1E:C2": "Apple",
    "00:25:00": "Apple", "A4:83:E7": "Apple", "AC:BC:32": "Apple",
    "B8:17:C2": "Apple", "D8:1D:72": "Apple", "F0:D1:A9": "Apple",
    # Samsung
    "00:07:AB": "Samsung", "00:16:32": "Samsung", "00:21:19": "Samsung",
    "00:26:37": "Samsung", "50:01:BB": "Samsung",
    # Amazon (Echo, Ring, Fire)
    "00:FC:8B": "Amazon", "10:CE:A9": "Amazon", "34:D2:70": "Amazon",
    "44:65:0D": "Amazon", "68:54:FD": "Amazon", "74:C2:46": "Amazon",
    "F0:81:73": "Amazon",
    # Google (Nest, Chromecast, Home)
    "00:1A:11": "Google", "48:D6:D5": "Google", "54:60:09": "Google",
    "F4:F5:D8": "Google",
    # Ring / Wyze
    "B0:09:DA": "Ring", "5C:47:5E": "Ring",
    "2C:AA:8E": "Wyze", "D0:3F:27": "Wyze",
    # TP-Link / Kasa
    "50:C7:BF": "TP-Link", "60:32:B1": "TP-Link", "D8:07:B6": "TP-Link",
    # Sonos
    "00:0E:58": "Sonos", "48:A6:B8": "Sonos", "94:9F:3E": "Sonos",
    # HP / Canon / Epson Printers
    "00:11:85": "HP", "00:17:A4": "HP", "3C:D9:2B": "HP",
    "00:00:85": "Canon", "00:1E:8F": "Canon",
    "00:00:48": "Epson", "00:1B:A9": "Epson",
    # Microsoft / Sony / Nintendo
    "00:50:F2": "Microsoft", "7C:1E:52": "Microsoft",
    "00:04:1F": "Sony", "28:0D:FC": "Sony",
    "00:17:AB": "Nintendo", "34:AF:2C": "Nintendo",
    # Philips Hue / Roku / Nest / Ecobee
    "00:17:88": "Philips Hue", "EC:B5:FA": "Philips Hue",
    "DC:3A:5E": "Roku", "B0:A7:37": "Roku",
    "18:B4:30": "Nest", "64:16:66": "Nest",
    "44:61:32": "Ecobee",
    # Intel
    "00:1B:21": "Intel", "00:1E:65": "Intel", "00:22:FB": "Intel",
    # Espressif (ESP8266/ESP32 IoT)
    "24:0A:C4": "Espressif", "5C:CF:7F": "Espressif",
    "A4:CF:12": "Espressif", "84:CC:A8": "Espressif",
    # Tuya / Raspberry Pi
    "D8:F1:5B": "Tuya",
    "B8:27:EB": "Raspberry Pi", "DC:A6:32": "Raspberry Pi",
    # Network infra
    "00:14:6C": "NETGEAR", "00:22:3F": "NETGEAR",
    "04:18:D6": "Ubiquiti", "80:2A:A8": "Ubiquiti",
    "00:14:BF": "Linksys", "00:1D:7E": "Linksys",
}

_MANUFACTURER_CATEGORIES = {
    "Apple": DeviceCategory.COMPUTER,
    "Samsung": DeviceCategory.PHONE,
    "Intel": DeviceCategory.COMPUTER,
    "Microsoft": DeviceCategory.COMPUTER,
    "Raspberry Pi": DeviceCategory.COMPUTER,
    "Amazon": DeviceCategory.SMART_SPEAKER,
    "Google": DeviceCategory.SMART_SPEAKER,
    "Sonos": DeviceCategory.SMART_SPEAKER,
    "Ring": DeviceCategory.IOT_CAMERA,
    "Wyze": DeviceCategory.IOT_CAMERA,
    "Philips Hue": DeviceCategory.IOT_APPLIANCE,
    "Nest": DeviceCategory.IOT_APPLIANCE,
    "Ecobee": DeviceCategory.IOT_APPLIANCE,
    "Espressif": DeviceCategory.IOT_APPLIANCE,
    "Tuya": DeviceCategory.IOT_APPLIANCE,
    "TP-Link": DeviceCategory.IOT_APPLIANCE,
    "Roku": DeviceCategory.SMART_TV,
    "Sony": DeviceCategory.GAMING,
    "Nintendo": DeviceCategory.GAMING,
    "HP": DeviceCategory.PRINTER,
    "Canon": DeviceCategory.PRINTER,
    "Epson": DeviceCategory.PRINTER,
    "NETGEAR": DeviceCategory.NETWORK_INFRA,
    "Ubiquiti": DeviceCategory.NETWORK_INFRA,
    "Linksys": DeviceCategory.NETWORK_INFRA,
}

_MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$')


def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to uppercase colon-separated format."""
    if not isinstance(mac, str):
        raise ValueError("MAC address must be a string")
    mac = mac.strip().upper().replace('-', ':')
    if not _MAC_PATTERN.match(mac):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return mac


def get_oui_prefix(mac: str) -> str:
    """Extract OUI prefix (first 3 octets) from a MAC address."""
    return normalize_mac(mac)[:8]


class DeviceClassifier:
    """Classifies network devices by MAC OUI lookup."""

    @dataclass
    class ClassificationResult:
        mac: str
        manufacturer: str | None
        category: DeviceCategory
        policy: TrafficPolicy
        confidence: str  # "high", "medium", "low"

        @property
        def is_safety_critical(self) -> bool:
            return self.policy.is_safety_critical

    def __init__(self):
        self._oui_db = _OUI_DATABASE
        self._manufacturer_map = _MANUFACTURER_CATEGORIES

    def classify(self, mac: str):
        """Classify a device by MAC address."""
        try:
            normalized = normalize_mac(mac)
        except ValueError:
            return self.ClassificationResult(
                mac=mac, manufacturer=None,
                category=DeviceCategory.UNKNOWN,
                policy=DEVICE_POLICIES[DeviceCategory.UNKNOWN],
                confidence="low")

        oui = get_oui_prefix(normalized)
        manufacturer = self._oui_db.get(oui)
        if manufacturer is None:
            return self.ClassificationResult(
                mac=normalized, manufacturer=None,
                category=DeviceCategory.UNKNOWN,
                policy=DEVICE_POLICIES[DeviceCategory.UNKNOWN],
                confidence="low")

        category = self._manufacturer_map.get(manufacturer, DeviceCategory.UNKNOWN)
        return self.ClassificationResult(
            mac=normalized, manufacturer=manufacturer,
            category=category, policy=DEVICE_POLICIES[category],
            confidence="high" if category != DeviceCategory.UNKNOWN else "medium")

    def classify_with_hostname(self, mac: str, hostname: str = ""):
        """Classify using both MAC OUI and hostname hints."""
        result = self.classify(mac)
        hostname_lower = (hostname or "").lower()
        if not hostname_lower:
            return result

        # Refine Apple devices
        if result.manufacturer == "Apple":
            if 'iphone' in hostname_lower:
                cat = DeviceCategory.PHONE
            elif 'ipad' in hostname_lower:
                cat = DeviceCategory.TABLET
            elif any(k in hostname_lower for k in ('macbook', 'imac', 'mac-mini')):
                cat = DeviceCategory.COMPUTER
            elif 'appletv' in hostname_lower:
                cat = DeviceCategory.SMART_TV
            elif 'homepod' in hostname_lower:
                cat = DeviceCategory.SMART_SPEAKER
            else:
                return result
            return self.ClassificationResult(
                mac=result.mac, manufacturer="Apple",
                category=cat, policy=DEVICE_POLICIES[cat], confidence="high")

        # Detect medical/safety devices by hostname
        medical_kw = ['cpap', 'insulin', 'glucose', 'pacemaker', 'oximeter', 'medical']
        safety_kw = ['smoke', 'fire', 'carbon-monoxide', 'co-detector', 'nest-protect']
        if any(kw in hostname_lower for kw in medical_kw):
            return self.ClassificationResult(
                mac=result.mac, manufacturer=result.manufacturer,
                category=DeviceCategory.MEDICAL_DEVICE,
                policy=DEVICE_POLICIES[DeviceCategory.MEDICAL_DEVICE],
                confidence="medium")
        if any(kw in hostname_lower for kw in safety_kw):
            return self.ClassificationResult(
                mac=result.mac, manufacturer=result.manufacturer,
                category=DeviceCategory.SAFETY_DEVICE,
                policy=DEVICE_POLICIES[DeviceCategory.SAFETY_DEVICE],
                confidence="medium")
        return result

    def can_block(self, mac: str, hostname: str = ""):
        """Check whether a device is safe to block."""
        result = self.classify_with_hostname(mac, hostname)
        if result.is_safety_critical:
            return False, (f"SAFETY-CRITICAL: {result.category.value} device. "
                           "Blocking could endanger health or safety.")
        if result.category == DeviceCategory.NETWORK_INFRA:
            return False, (f"Network infrastructure ({result.manufacturer}). "
                           "Blocking could disconnect you.")
        return True, f"OK to block: {result.category.value} ({result.manufacturer or 'unknown'})"


# ── Enhanced heuristic classifiers ────────────────────────────────────────

# Hostname pattern → DeviceCategory
_HOSTNAME_PATTERNS = [
    # Smart TVs & streaming
    (re.compile(r'roku', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'fire[-\s]?tv', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'chromecast', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'samsung[-\s]?tv', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'lg[-\s]?tv', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'vizio', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'appletv', re.I), DeviceCategory.SMART_TV),
    # IoT cameras
    (re.compile(r'ring[-\s]?(doorbell|cam|stick)', re.I), DeviceCategory.IOT_CAMERA),
    (re.compile(r'wyze[-\s]?cam', re.I), DeviceCategory.IOT_CAMERA),
    (re.compile(r'arlo', re.I), DeviceCategory.IOT_CAMERA),
    (re.compile(r'blink[-\s]?(cam|mini)', re.I), DeviceCategory.IOT_CAMERA),
    (re.compile(r'nest[-\s]?cam', re.I), DeviceCategory.IOT_CAMERA),
    # Smart speakers
    (re.compile(r'echo[-\s]?(dot|show|pop|studio)?', re.I), DeviceCategory.SMART_SPEAKER),
    (re.compile(r'google[-\s]?home', re.I), DeviceCategory.SMART_SPEAKER),
    (re.compile(r'homepod', re.I), DeviceCategory.SMART_SPEAKER),
    (re.compile(r'sonos', re.I), DeviceCategory.SMART_SPEAKER),
    # Printers
    (re.compile(r'(hp|canon|epson|brother)[-\s]?(printer|deskjet|officejet|laserjet|envy|pixma)', re.I), DeviceCategory.PRINTER),
    # Gaming
    (re.compile(r'playstation|ps[45]', re.I), DeviceCategory.GAMING),
    (re.compile(r'xbox', re.I), DeviceCategory.GAMING),
    (re.compile(r'nintendo[-\s]?switch', re.I), DeviceCategory.GAMING),
    # Computers
    (re.compile(r'(macbook|imac|mac[-\s]?mini|mac[-\s]?pro)', re.I), DeviceCategory.COMPUTER),
    (re.compile(r'desktop|laptop|workstation', re.I), DeviceCategory.COMPUTER),
    # Phones & tablets
    (re.compile(r'iphone', re.I), DeviceCategory.PHONE),
    (re.compile(r'galaxy|pixel|oneplus', re.I), DeviceCategory.PHONE),
    (re.compile(r'ipad', re.I), DeviceCategory.TABLET),
    # Network infra
    (re.compile(r'(router|gateway|access[-\s]?point|ap|mesh|eero|orbi|unifi)', re.I), DeviceCategory.NETWORK_INFRA),
    # IoT appliances
    (re.compile(r'(hue|wemo|kasa|tuya|smartthings|ecobee|nest[-\s]?thermostat)', re.I), DeviceCategory.IOT_APPLIANCE),
    # Safety
    (re.compile(r'(smoke|fire[-\s]?alarm|co[-\s]?detector|nest[-\s]?protect)', re.I), DeviceCategory.SAFETY_DEVICE),
    # Medical
    (re.compile(r'(cpap|insulin|glucose|oximeter|medical)', re.I), DeviceCategory.MEDICAL_DEVICE),
]

# mDNS service type → DeviceCategory
_MDNS_SERVICE_MAP = {
    "_airplay._tcp": DeviceCategory.SMART_TV,
    "_raop._tcp": DeviceCategory.SMART_TV,
    "_googlecast._tcp": DeviceCategory.SMART_TV,
    "_spotify-connect._tcp": DeviceCategory.SMART_SPEAKER,
    "_sonos._tcp": DeviceCategory.SMART_SPEAKER,
    "_printer._tcp": DeviceCategory.PRINTER,
    "_ipp._tcp": DeviceCategory.PRINTER,
    "_ipps._tcp": DeviceCategory.PRINTER,
    "_pdl-datastream._tcp": DeviceCategory.PRINTER,
    "_scanner._tcp": DeviceCategory.PRINTER,
    "_hue._tcp": DeviceCategory.IOT_APPLIANCE,
    "_hap._tcp": DeviceCategory.IOT_APPLIANCE,  # HomeKit
    "_homekit._tcp": DeviceCategory.IOT_APPLIANCE,
    "_smb._tcp": DeviceCategory.COMPUTER,
    "_afpovertcp._tcp": DeviceCategory.COMPUTER,
    "_ssh._tcp": DeviceCategory.COMPUTER,
    "_rdp._tcp": DeviceCategory.COMPUTER,
    "_http._tcp": DeviceCategory.COMPUTER,  # generic, low confidence
}

# SSDP server header patterns → DeviceCategory
_SSDP_PATTERNS = [
    (re.compile(r'roku', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'samsung.*tv', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'lg.*webos', re.I), DeviceCategory.SMART_TV),
    (re.compile(r'philips.*hue', re.I), DeviceCategory.IOT_APPLIANCE),
    (re.compile(r'sonos', re.I), DeviceCategory.SMART_SPEAKER),
    (re.compile(r'belkin.*wemo', re.I), DeviceCategory.IOT_APPLIANCE),
    (re.compile(r'xbox', re.I), DeviceCategory.GAMING),
    (re.compile(r'playstation', re.I), DeviceCategory.GAMING),
]


def classify_by_hostname(hostname: str) -> DeviceCategory | None:
    """Classify a device by its hostname using pattern matching.

    Returns the DeviceCategory or None if no pattern matches.
    """
    if not hostname:
        return None
    for pattern, category in _HOSTNAME_PATTERNS:
        if pattern.search(hostname):
            return category
    return None


def classify_by_mdns(service_type: str) -> DeviceCategory | None:
    """Classify a device by its mDNS service type.

    Args:
        service_type: mDNS service string (e.g. "_airplay._tcp").

    Returns the DeviceCategory or None if not recognized.
    """
    if not service_type:
        return None
    return _MDNS_SERVICE_MAP.get(service_type.lower().strip())


def classify_by_ssdp(server_header: str) -> DeviceCategory | None:
    """Classify a device by its SSDP Server/USN header.

    Returns the DeviceCategory or None if no pattern matches.
    """
    if not server_header:
        return None
    for pattern, category in _SSDP_PATTERNS:
        if pattern.search(server_header):
            return category
    return None


def classify_device(
    mac: str,
    hostname: str | None = None,
    mdns_services: list[str] | None = None,
    ssdp_header: str | None = None,
    ml_classifier: Any = None,
) -> "DeviceClassifier.ClassificationResult":
    """Combined device classification using all available signals.

    Priority: hostname > mDNS > SSDP > OUI (MAC lookup) > ML fallback.
    Higher-specificity signals override lower ones.

    Args:
        mac: Device MAC address.
        hostname: Optional hostname string.
        mdns_services: Optional list of mDNS service types.
        ssdp_header: Optional SSDP Server/USN header.
        ml_classifier: Optional DeviceMLClassifier instance for ML fallback
            when heuristic classification returns UNKNOWN.

    Returns a ClassificationResult with the best available classification.
    """
    classifier = DeviceClassifier()

    # Start with MAC OUI lookup (baseline)
    result = classifier.classify_with_hostname(mac, hostname or "")

    # If OUI gives a definitive answer with hostname refinement, use it
    if result.confidence == "high" and result.category != DeviceCategory.UNKNOWN:
        return result

    # Try hostname pattern matching (highest priority among heuristics)
    if hostname:
        hostname_cat = classify_by_hostname(hostname)
        if hostname_cat is not None:
            return DeviceClassifier.ClassificationResult(
                mac=result.mac, manufacturer=result.manufacturer,
                category=hostname_cat, policy=DEVICE_POLICIES[hostname_cat],
                confidence="medium")

    # Try mDNS service types
    if mdns_services:
        for svc in mdns_services:
            mdns_cat = classify_by_mdns(svc)
            if mdns_cat is not None:
                return DeviceClassifier.ClassificationResult(
                    mac=result.mac, manufacturer=result.manufacturer,
                    category=mdns_cat, policy=DEVICE_POLICIES[mdns_cat],
                    confidence="medium")

    # Try SSDP header
    if ssdp_header:
        ssdp_cat = classify_by_ssdp(ssdp_header)
        if ssdp_cat is not None:
            return DeviceClassifier.ClassificationResult(
                mac=result.mac, manufacturer=result.manufacturer,
                category=ssdp_cat, policy=DEVICE_POLICIES[ssdp_cat],
                confidence="medium")

    # ML fallback when all heuristics return UNKNOWN
    if result.category == DeviceCategory.UNKNOWN and ml_classifier is not None:
        try:
            ml_cat = ml_classifier.predict(
                mac=mac, hostname=hostname or "",
                mdns_services=mdns_services, ssdp_header=ssdp_header or "",
            )
            if ml_cat != DeviceCategory.UNKNOWN:
                return DeviceClassifier.ClassificationResult(
                    mac=result.mac, manufacturer=result.manufacturer,
                    category=ml_cat, policy=DEVICE_POLICIES[ml_cat],
                    confidence="low")
        except Exception:
            pass  # ML failure should never break classification

    return result
