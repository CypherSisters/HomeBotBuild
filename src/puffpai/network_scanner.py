import subprocess
import requests
import json
import re
import os
import sys

from puffpai.config import get_config


def load_automation_url():
    """Load the automation engine (Node-RED) URL from centralized config."""
    cfg = get_config()
    return cfg.nodered.url


def scan_network():
    result = subprocess.run(
        ['arp', '-a'], capture_output=True, text=True, shell=False
    )
    output = result.stdout

    devices = []
    for line in output.split('\n'):
        ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
        mac_match = re.search(
            r'([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})', line
        )
        if ip_match:
            device = {
                'ip': ip_match.group(1),
                'mac': mac_match.group(0) if mac_match else 'unknown',
                'raw': line.strip()
            }
            devices.append(device)

    return devices

if __name__ == "__main__":
    # Allow CLI override: python network_scanner.py http://<automation-host>:5678
    if len(sys.argv) > 1:
        automation_url = sys.argv[1]
    else:
        automation_url = load_automation_url()

    devices = scan_network()
    print(f"Found {len(devices)} devices")

    try:
        response = requests.post(
            f'{automation_url}/webhook/network-scan',
            json={'devices': devices, 'count': len(devices)},
            timeout=10
        )
        print(f"automation response: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"automation engine unreachable at {automation_url}: {e}")
        print("Raw results:")
        for d in devices:
            print(f"  {d['ip']}  {d['mac']}")