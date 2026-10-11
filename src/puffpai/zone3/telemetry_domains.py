"""
PuffPAI Zone 3 — Telemetry Domain Database

Curated lists of known phone-home / telemetry / tracking domains
per smart device manufacturer.

These domains are used by the policy engine to block outbound
telemetry from smart home devices at the DNS level.

Sources: Public research, community blocklists, device traffic analysis.
Each entry includes the domain and a category tag for transparency.

Categories:
    telemetry  — Device usage data, analytics, crash reports
    ads        — Advertising and ad tracking
    tracking   — Cross-device / behavioral tracking
    cloud      — Cloud services (may break functionality if blocked)
    update     — Firmware/software updates (usually should NOT be blocked)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DomainCategory(Enum):
    TELEMETRY = "telemetry"
    ADS = "ads"
    TRACKING = "tracking"
    CLOUD = "cloud"
    UPDATE = "update"


@dataclass(frozen=True)
class TelemetryDomain:
    domain: str
    category: DomainCategory
    description: str = ""


# ── Samsung ────────────────────────────────────────────────────────────
_SAMSUNG_DOMAINS = [
    TelemetryDomain("samsungacr.com", DomainCategory.TELEMETRY, "Samsung ACR (automatic content recognition)"),
    TelemetryDomain("samsungads.com", DomainCategory.ADS, "Samsung ad network"),
    TelemetryDomain("samsung-dmb.com", DomainCategory.TELEMETRY, "Samsung device management beacon"),
    TelemetryDomain("pavv.co.kr", DomainCategory.TELEMETRY, "Samsung TV telemetry"),
    TelemetryDomain("samsungosp.com", DomainCategory.TELEMETRY, "Samsung online service platform"),
    TelemetryDomain("internetat.tv", DomainCategory.ADS, "Samsung TV ads"),
    TelemetryDomain("samsungelectronics.com", DomainCategory.TELEMETRY, "Samsung telemetry endpoint"),
    TelemetryDomain("gpm.samsungapps.com", DomainCategory.ADS, "Samsung app store ads"),
    TelemetryDomain("config.samsungads.com", DomainCategory.ADS, "Samsung ad config"),
    TelemetryDomain("nmetrics.samsung.com", DomainCategory.TELEMETRY, "Samsung metrics"),
    TelemetryDomain("analytics.samsungknox.com", DomainCategory.TELEMETRY, "Samsung Knox analytics"),
]

# ── LG ─────────────────────────────────────────────────────────────────
_LG_DOMAINS = [
    TelemetryDomain("lgtvsdp.com", DomainCategory.TELEMETRY, "LG TV SDP telemetry"),
    TelemetryDomain("lgsmartad.com", DomainCategory.ADS, "LG TV smart ads"),
    TelemetryDomain("lgappstv.com", DomainCategory.ADS, "LG TV app store ads"),
    TelemetryDomain("ibas.lgappstv.com", DomainCategory.ADS, "LG TV ad targeting"),
    TelemetryDomain("us.info.lgsmartad.com", DomainCategory.ADS, "LG smart ad info"),
    TelemetryDomain("us.lgtvsdp.com", DomainCategory.TELEMETRY, "LG TV data platform US"),
    TelemetryDomain("ad.lgappstv.com", DomainCategory.ADS, "LG TV ad delivery"),
    TelemetryDomain("nudge.lgtvcommon.com", DomainCategory.ADS, "LG TV nudge ads"),
    TelemetryDomain("lgtvonline.lge.com", DomainCategory.TELEMETRY, "LG TV online services"),
]

# ── Amazon (Echo, Ring, Fire TV) ───────────────────────────────────────
_AMAZON_DOMAINS = [
    TelemetryDomain("device-metrics-us.amazon.com", DomainCategory.TELEMETRY, "Amazon device metrics"),
    TelemetryDomain("device-metrics-us-2.amazon.com", DomainCategory.TELEMETRY, "Amazon device metrics 2"),
    TelemetryDomain("mads-adtech.a2z.com", DomainCategory.ADS, "Amazon ad tech"),
    TelemetryDomain("adtago.s3.amazonaws.com", DomainCategory.ADS, "Amazon ad tagging"),
    TelemetryDomain("amazon-adsystem.com", DomainCategory.ADS, "Amazon ad system"),
    TelemetryDomain("aax-us-east.amazon-adsystem.com", DomainCategory.ADS, "Amazon ad exchange"),
    TelemetryDomain("fls-na.amazon.com", DomainCategory.TRACKING, "Amazon fire logging"),
    TelemetryDomain("unagi.amazon.com", DomainCategory.TELEMETRY, "Amazon device telemetry"),
    TelemetryDomain("cognito-identity.us-east-1.amazonaws.com", DomainCategory.TRACKING, "Amazon identity tracking"),
]

# ── Google (Nest, Chromecast, Home) ────────────────────────────────────
_GOOGLE_DOMAINS = [
    TelemetryDomain("clientservices.googleapis.com", DomainCategory.TELEMETRY, "Google client services"),
    TelemetryDomain("crashlyticsreports-pa.googleapis.com", DomainCategory.TELEMETRY, "Google crash reports"),
    TelemetryDomain("firebaselogging-pa.googleapis.com", DomainCategory.TELEMETRY, "Firebase logging"),
    TelemetryDomain("app-measurement.com", DomainCategory.TRACKING, "Firebase analytics"),
    TelemetryDomain("pagead2.googlesyndication.com", DomainCategory.ADS, "Google ad syndication"),
    TelemetryDomain("adservice.google.com", DomainCategory.ADS, "Google ad service"),
    TelemetryDomain("googleadservices.com", DomainCategory.ADS, "Google ad services"),
    TelemetryDomain("play.googleapis.com", DomainCategory.TELEMETRY, "Google Play telemetry"),
]

# ── Roku ───────────────────────────────────────────────────────────────
_ROKU_DOMAINS = [
    TelemetryDomain("logs.roku.com", DomainCategory.TELEMETRY, "Roku logging"),
    TelemetryDomain("scribe.logs.roku.com", DomainCategory.TELEMETRY, "Roku analytics"),
    TelemetryDomain("cooper.logs.roku.com", DomainCategory.TELEMETRY, "Roku telemetry"),
    TelemetryDomain("p.ads.roku.com", DomainCategory.ADS, "Roku ads"),
    TelemetryDomain("ads.roku.com", DomainCategory.ADS, "Roku ad platform"),
    TelemetryDomain("cloudservices.roku.com", DomainCategory.TELEMETRY, "Roku cloud services"),
]

# ── Wyze ───────────────────────────────────────────────────────────────
_WYZE_DOMAINS = [
    TelemetryDomain("wyze-mars-service.wyzecam.com", DomainCategory.TELEMETRY, "Wyze telemetry"),
    TelemetryDomain("wyze-general-api.wyzecam.com", DomainCategory.TELEMETRY, "Wyze API analytics"),
]

# ── Ring ───────────────────────────────────────────────────────────────
_RING_DOMAINS = [
    TelemetryDomain("ring.com", DomainCategory.CLOUD, "Ring cloud (blocks all Ring services)"),
    TelemetryDomain("api.ring.com", DomainCategory.CLOUD, "Ring API"),
    TelemetryDomain("fw.ring.com", DomainCategory.TELEMETRY, "Ring firmware telemetry"),
    TelemetryDomain("nw.ring.com", DomainCategory.TELEMETRY, "Ring network telemetry"),
    TelemetryDomain("app.ring.com", DomainCategory.TELEMETRY, "Ring app analytics"),
]

# ── TP-Link / Kasa ────────────────────────────────────────────────────
_TPLINK_DOMAINS = [
    TelemetryDomain("euw1-api.tplinkcloud.com", DomainCategory.CLOUD, "TP-Link cloud EU"),
    TelemetryDomain("use1-api.tplinkcloud.com", DomainCategory.CLOUD, "TP-Link cloud US"),
    TelemetryDomain("n-devs.tplinkcloud.com", DomainCategory.TELEMETRY, "TP-Link device telemetry"),
    TelemetryDomain("n-deventry.tplinkcloud.com", DomainCategory.TELEMETRY, "TP-Link entry telemetry"),
]

# ── Tuya (white-label IoT) ────────────────────────────────────────────
_TUYA_DOMAINS = [
    TelemetryDomain("a2.tuyaus.com", DomainCategory.CLOUD, "Tuya US cloud"),
    TelemetryDomain("a3.tuyaus.com", DomainCategory.CLOUD, "Tuya US cloud 3"),
    TelemetryDomain("m1.tuyaus.com", DomainCategory.TELEMETRY, "Tuya US MQTT"),
    TelemetryDomain("log.tuyaus.com", DomainCategory.TELEMETRY, "Tuya logging"),
]

# ── Ecobee ─────────────────────────────────────────────────────────────
_ECOBEE_DOMAINS = [
    TelemetryDomain("api.ecobee.com", DomainCategory.CLOUD, "Ecobee API"),
    TelemetryDomain("home-api.ecobee.com", DomainCategory.CLOUD, "Ecobee home API"),
]

# ── Philips Hue ────────────────────────────────────────────────────────
_HUE_DOMAINS = [
    TelemetryDomain("diagnostics.meethue.com", DomainCategory.TELEMETRY, "Hue diagnostics"),
    TelemetryDomain("www2.meethue.com", DomainCategory.TELEMETRY, "Hue telemetry"),
]

# ── Sonos ──────────────────────────────────────────────────────────────
_SONOS_DOMAINS = [
    TelemetryDomain("msmetrics.ws.sonos.com", DomainCategory.TELEMETRY, "Sonos metrics"),
    TelemetryDomain("analytics.sonos.com", DomainCategory.TELEMETRY, "Sonos analytics"),
]

# ── Espressif / Generic IoT ───────────────────────────────────────────
_GENERIC_IOT_DOMAINS = [
    TelemetryDomain("cloudfront.net", DomainCategory.TRACKING, "CDN tracking (common IoT)"),
]

# ── Common tracking / ad networks (device-agnostic) ──────────────────
_COMMON_TRACKING = [
    TelemetryDomain("doubleclick.net", DomainCategory.ADS, "Google DoubleClick"),
    TelemetryDomain("googlesyndication.com", DomainCategory.ADS, "Google ad syndication"),
    TelemetryDomain("googletagmanager.com", DomainCategory.TRACKING, "Google Tag Manager"),
    TelemetryDomain("google-analytics.com", DomainCategory.TRACKING, "Google Analytics"),
    TelemetryDomain("facebook.com", DomainCategory.TRACKING, "Facebook tracking"),
    TelemetryDomain("connect.facebook.net", DomainCategory.TRACKING, "Facebook connect"),
    TelemetryDomain("graph.facebook.com", DomainCategory.TRACKING, "Facebook graph API"),
    TelemetryDomain("amplitude.com", DomainCategory.TRACKING, "Amplitude analytics"),
    TelemetryDomain("mixpanel.com", DomainCategory.TRACKING, "Mixpanel analytics"),
    TelemetryDomain("segment.io", DomainCategory.TRACKING, "Segment analytics"),
    TelemetryDomain("branch.io", DomainCategory.TRACKING, "Branch deep linking / tracking"),
    TelemetryDomain("appsflyer.com", DomainCategory.TRACKING, "AppsFlyer attribution"),
    TelemetryDomain("adjust.com", DomainCategory.TRACKING, "Adjust attribution"),
]


# ═══════════════════════════════════════════════════════════════════════
#  Telemetry Database
# ═══════════════════════════════════════════════════════════════════════

# Manufacturer name → domain list mapping
# These keys match the manufacturer names in device_classifier.py
_MANUFACTURER_TELEMETRY: dict[str, list[TelemetryDomain]] = {
    "Samsung": _SAMSUNG_DOMAINS,
    "LG": _LG_DOMAINS,
    "Amazon": _AMAZON_DOMAINS,
    "Google": _GOOGLE_DOMAINS,
    "Roku": _ROKU_DOMAINS,
    "Wyze": _WYZE_DOMAINS,
    "Ring": _RING_DOMAINS,
    "TP-Link": _TPLINK_DOMAINS,
    "Tuya": _TUYA_DOMAINS,
    "Ecobee": _ECOBEE_DOMAINS,
    "Philips Hue": _HUE_DOMAINS,
    "Sonos": _SONOS_DOMAINS,
    "Espressif": _GENERIC_IOT_DOMAINS,
    "Nest": _GOOGLE_DOMAINS,  # Nest uses Google infrastructure
}


class TelemetryDB:
    """Lookup telemetry domains for a given manufacturer or device category.

    Used by the PolicyEngine to determine which domains to block
    based on the device's classification.
    """

    def __init__(self):
        self._db = _MANUFACTURER_TELEMETRY
        self._common = _COMMON_TRACKING

    @property
    def manufacturers(self) -> list[str]:
        """List all manufacturers with telemetry data."""
        return list(self._db.keys())

    def get_domains(
        self,
        manufacturer: str,
        include_common: bool = True,
        categories: set[DomainCategory] | None = None,
    ) -> list[TelemetryDomain]:
        """Get telemetry domains for a manufacturer.

        Args:
            manufacturer: Manufacturer name (e.g., "Samsung", "Amazon")
            include_common: Include common cross-device tracking domains
            categories: Filter by domain category (None = all)

        Returns:
            List of TelemetryDomain entries.
        """
        domains = list(self._db.get(manufacturer, []))
        if include_common:
            domains.extend(self._common)

        if categories:
            domains = [d for d in domains if d.category in categories]

        return domains

    def get_domain_strings(
        self,
        manufacturer: str,
        include_common: bool = True,
        categories: set[DomainCategory] | None = None,
    ) -> list[str]:
        """Get just the domain name strings (for Pi-hole blocklist)."""
        return [d.domain for d in self.get_domains(manufacturer, include_common, categories)]

    def get_telemetry_only(self, manufacturer: str) -> list[str]:
        """Get only telemetry + ads + tracking domains (not cloud/update).

        This is the "balanced" mode list — blocks spying but keeps
        device functionality working.
        """
        safe_categories = {DomainCategory.TELEMETRY, DomainCategory.ADS, DomainCategory.TRACKING}
        return self.get_domain_strings(manufacturer, include_common=True, categories=safe_categories)

    def get_all_outbound(self, manufacturer: str) -> list[str]:
        """Get ALL known domains including cloud services.

        This is the "lockdown" mode list — blocks everything.
        Device may lose cloud features.
        """
        return self.get_domain_strings(manufacturer, include_common=True)

    def get_ads_only(self, manufacturer: str) -> list[str]:
        """Get only ads and tracking domains (not telemetry/cloud).

        This is the "open" mode list — minimal blocking,
        just removes ads and cross-device tracking.
        """
        ad_categories = {DomainCategory.ADS, DomainCategory.TRACKING}
        return self.get_domain_strings(manufacturer, include_common=True, categories=ad_categories)

    def get_summary(self, manufacturer: str) -> dict:
        """Get a summary of telemetry data for a manufacturer."""
        domains = self.get_domains(manufacturer, include_common=False)
        return {
            "manufacturer": manufacturer,
            "total_domains": len(domains),
            "by_category": {
                cat.value: len([d for d in domains if d.category == cat])
                for cat in DomainCategory
            },
            "domains": [{"domain": d.domain, "category": d.category.value, "desc": d.description} for d in domains],
        }
