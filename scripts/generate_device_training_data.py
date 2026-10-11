#!/usr/bin/env python3
"""
Generate synthetic training data for the device ML classifier.

Creates a JSON file of labeled device feature vectors from
realistic device signal patterns.

Usage:
    python scripts/generate_device_training_data.py
    python scripts/generate_device_training_data.py --output data/device_training.json --count 500

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

# Add src/ to path so we can import puffpai modules
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from puffpai.security.device_ml import DeviceFeatureExtractor


# ── Synthetic device templates ───────────────────────────────────────────────

DEVICE_TEMPLATES = {
    "computer": [
        {"mac": "00:1B:21:XX:XX:XX", "hostname": "DESKTOP-{}", "mdns": ["_smb._tcp", "_rdp._tcp"]},
        {"mac": "A4:83:E7:XX:XX:XX", "hostname": "MacBook-Pro-{}", "mdns": ["_afpovertcp._tcp", "_smb._tcp"]},
        {"mac": "00:22:FB:XX:XX:XX", "hostname": "laptop-{}", "mdns": ["_ssh._tcp"]},
        {"mac": "B8:27:EB:XX:XX:XX", "hostname": "raspberrypi-{}", "mdns": ["_ssh._tcp", "_http._tcp"]},
        {"mac": "7C:1E:52:XX:XX:XX", "hostname": "workstation-{}", "mdns": ["_smb._tcp"]},
    ],
    "phone": [
        {"mac": "00:16:32:XX:XX:XX", "hostname": "Galaxy-S{}", "mdns": []},
        {"mac": "D8:1D:72:XX:XX:XX", "hostname": "iPhone-{}", "mdns": []},
        {"mac": "50:01:BB:XX:XX:XX", "hostname": "Pixel-{}", "mdns": []},
    ],
    "tablet": [
        {"mac": "AC:BC:32:XX:XX:XX", "hostname": "iPad-{}", "mdns": []},
        {"mac": "00:21:19:XX:XX:XX", "hostname": "Galaxy-Tab-{}", "mdns": []},
    ],
    "smart_speaker": [
        {"mac": "34:D2:70:XX:XX:XX", "hostname": "echo-dot-{}", "mdns": ["_spotify-connect._tcp"]},
        {"mac": "54:60:09:XX:XX:XX", "hostname": "Google-Home-{}", "mdns": ["_googlecast._tcp"]},
        {"mac": "94:9F:3E:XX:XX:XX", "hostname": "Sonos-{}", "mdns": ["_sonos._tcp"]},
        {"mac": "F0:D1:A9:XX:XX:XX", "hostname": "HomePod-{}", "mdns": ["_airplay._tcp"]},
    ],
    "iot_camera": [
        {"mac": "B0:09:DA:XX:XX:XX", "hostname": "Ring-Doorbell-{}", "mdns": []},
        {"mac": "2C:AA:8E:XX:XX:XX", "hostname": "Wyze-Cam-{}", "mdns": []},
        {"mac": "00:00:00:XX:XX:XX", "hostname": "Arlo-Pro-{}", "mdns": []},
        {"mac": "00:00:00:XX:XX:XX", "hostname": "Blink-Mini-{}", "mdns": []},
    ],
    "smart_tv": [
        {"mac": "DC:3A:5E:XX:XX:XX", "hostname": "Roku-Ultra-{}", "mdns": ["_airplay._tcp"]},
        {"mac": "00:00:00:XX:XX:XX", "hostname": "Samsung-TV-{}", "mdns": ["_airplay._tcp"], "ssdp": "Samsung UE55 DLNA/1.0"},
        {"mac": "00:00:00:XX:XX:XX", "hostname": "LG-TV-{}", "mdns": [], "ssdp": "LG webOS TV 55NANO81"},
        {"mac": "48:D6:D5:XX:XX:XX", "hostname": "Chromecast-{}", "mdns": ["_googlecast._tcp"]},
    ],
    "printer": [
        {"mac": "3C:D9:2B:XX:XX:XX", "hostname": "HP-DeskJet-{}", "mdns": ["_printer._tcp", "_ipp._tcp"]},
        {"mac": "00:1E:8F:XX:XX:XX", "hostname": "Canon-Pixma-{}", "mdns": ["_printer._tcp", "_ipp._tcp"]},
        {"mac": "00:1B:A9:XX:XX:XX", "hostname": "Epson-ET-{}", "mdns": ["_printer._tcp"]},
    ],
    "gaming": [
        {"mac": "28:0D:FC:XX:XX:XX", "hostname": "PlayStation-{}", "mdns": []},
        {"mac": "7C:1E:52:XX:XX:XX", "hostname": "Xbox-One-{}", "mdns": [], "ssdp": "Xbox One S"},
        {"mac": "34:AF:2C:XX:XX:XX", "hostname": "Nintendo-Switch-{}", "mdns": []},
    ],
    "network_infra": [
        {"mac": "00:22:3F:XX:XX:XX", "hostname": "NETGEAR-Router-{}", "mdns": []},
        {"mac": "04:18:D6:XX:XX:XX", "hostname": "UniFi-AP-{}", "mdns": []},
        {"mac": "00:14:BF:XX:XX:XX", "hostname": "Linksys-{}", "mdns": []},
    ],
    "iot_appliance": [
        {"mac": "EC:B5:FA:XX:XX:XX", "hostname": "Philips-Hue-Bridge-{}", "mdns": ["_hue._tcp", "_hap._tcp"]},
        {"mac": "44:61:32:XX:XX:XX", "hostname": "Ecobee-{}", "mdns": ["_homekit._tcp"]},
        {"mac": "24:0A:C4:XX:XX:XX", "hostname": "ESP-{}", "mdns": []},
        {"mac": "D8:F1:5B:XX:XX:XX", "hostname": "Tuya-Smart-{}", "mdns": []},
        {"mac": "60:32:B1:XX:XX:XX", "hostname": "Kasa-Smart-Plug-{}", "mdns": []},
    ],
    "unknown": [
        {"mac": "AA:BB:CC:XX:XX:XX", "hostname": "device-{}", "mdns": []},
        {"mac": "11:22:33:XX:XX:XX", "hostname": "{}", "mdns": []},
        {"mac": "FF:FF:FF:XX:XX:XX", "hostname": "unknown-{}", "mdns": []},
    ],
}


def _random_hex() -> str:
    return f"{random.randint(0, 255):02X}"


def _random_mac_suffix() -> str:
    return f"{_random_hex()}:{_random_hex()}:{_random_hex()}"


def generate_samples(count_per_category: int = 30) -> list[dict]:
    """Generate synthetic device samples with labels."""
    extractor = DeviceFeatureExtractor()
    samples = []

    for category, templates in DEVICE_TEMPLATES.items():
        for i in range(count_per_category):
            template = random.choice(templates)
            mac = template["mac"].replace("XX:XX:XX", _random_mac_suffix())
            hostname = template["hostname"].format(random.randint(100, 9999))
            mdns = template.get("mdns", [])
            ssdp = template.get("ssdp", "")

            features = extractor.extract(
                mac=mac, hostname=hostname,
                mdns_services=mdns, ssdp_header=ssdp,
            )

            samples.append({
                "features": features.to_list(),
                "label": category,
                "mac": mac,
                "hostname": hostname,
            })

    random.shuffle(samples)
    return samples


def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic device training data")
    parser.add_argument("--output", default="data/device_training.json",
                        help="Output JSON file path")
    parser.add_argument("--count", type=int, default=30,
                        help="Samples per device category")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)
    samples = generate_samples(args.count)

    output_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, 'w') as f:
        json.dump({
            "version": 1,
            "feature_names": DeviceFeatures.feature_names() if False else [
                "oui_hash", "hostname_len", "hostname_digit_ratio",
                "hostname_hyphen_count", "hostname_has_brand",
                "mdns_service_count", "mdns_has_printer", "mdns_has_airplay",
                "mdns_has_smb", "ssdp_present", "ssdp_header_len",
            ],
            "n_samples": len(samples),
            "samples": samples,
        }, f, indent=2)

    print(f"Generated {len(samples)} samples across {len(DEVICE_TEMPLATES)} categories")
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    main()
