"""Tests for puffpai.bridge_inventory — the Zone 3 "see the devices" roll.

Every scanner is mocked. Nothing here touches the real LAN, Home Assistant, or
AdGuard, and the sentinel daemon is never constructed.
"""

import json
import os
import threading
import time

import pytest

from puffpai import bridge_inventory as bi

#: Captured before any fixture stubs them out, so a test that wants the real
#: source function back can ask for it by name.
_REAL_HA_RECORDS = bi._ha_records
_REAL_ADGUARD_RECORDS = bi._adguard_records


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Pin the device file into tmp_path and drop the singleton around each test."""
    monkeypatch.setattr(bi, "_DEVICES_FILE", tmp_path / "bridge_devices.json")
    monkeypatch.setattr(bi, "_virtual_subnets_cache", [])
    # No test may reach a real Home Assistant: the registry path resolves
    # credentials first, so stubbing that stops it before any socket opens.
    monkeypatch.setattr(bi, "_ha_resolve", lambda: ("", "", False))
    bi.reset_inventory()
    yield
    bi.reset_inventory()


@pytest.fixture
def no_sources(monkeypatch):
    """Every source empty by default; individual tests override what they need."""
    monkeypatch.setattr(bi, "_scan_arp", lambda: [])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {})
    monkeypatch.setattr(bi, "_ha_records", lambda: [])
    monkeypatch.setattr(bi, "_adguard_records", lambda: ({}, []))
    monkeypatch.setattr(bi, "default_gateway", lambda: "")
    monkeypatch.setattr(bi, "_own_addresses", lambda: set())


# ── address helpers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip,expected", [
    ("192.168.1.20", True),
    ("192.168.50.40", True),
    ("172.16.5.9", True),         # legitimate LAN
    ("172.17.0.2", True),         # 172.16/12 is RFC1918; no blanket exclusion
    ("172.31.255.1", True),
    ("172.20.10.5", True),        # iOS hotspot LAN — must not be blanked
    ("127.0.0.1", False),
    ("169.254.10.1", False),      # link-local
    ("8.8.8.8", False),           # public
    ("not-an-ip", False),
])
def test_is_lan_ip(ip, expected):
    assert bi.is_lan_ip(ip) is expected


def test_is_lan_ip_excludes_discovered_virtual_subnet(monkeypatch):
    monkeypatch.setattr(
        bi, "_virtual_subnets_cache",
        [__import__("ipaddress").ip_network("172.17.0.0/16")])
    assert bi.is_lan_ip("172.17.0.5") is False
    assert bi.is_lan_ip("172.20.10.5") is True    # untouched, different subnet


@pytest.mark.parametrize("raw,expected", [
    ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
    ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"),
    ("incomplete", ""),
    ("", ""),
])
def test_normalize_mac(raw, expected):
    assert bi.normalize_mac(raw) == expected


def test_ignorable_macs():
    assert bi._is_ignorable_mac("FF:FF:FF:FF:FF:FF")
    assert bi._is_ignorable_mac("00:00:00:00:00:00")
    # Empty is "unknown", not "ignorable" — a MAC-less row still keys on IP.
    assert not bi._is_ignorable_mac("")
    # Hyper-V / WSL2 virtual adapter, from device_sentinel.IGNORED_MAC_PREFIXES
    assert bi._is_ignorable_mac("00:15:5D:01:02:03")
    assert not bi._is_ignorable_mac("AA:BB:CC:DD:EE:FF")


def test_virtual_subnets_parses_ip_br_addr(monkeypatch):
    ip_br_addr = (
        "lo               UNKNOWN        127.0.0.1/8 ::1/128\n"
        "eno1             UP             192.168.50.5/24\n"
        "docker0          UP             172.17.0.1/16\n"
        "br-abc123def456  DOWN           172.18.0.1/16\n"
    )
    monkeypatch.setattr(bi, "_run", lambda cmd, timeout=10: ip_br_addr)
    nets = bi._virtual_subnets()
    # "lo" also matches _VIRTUAL_IFACE_RE and contributes 127.0.0.0/8; is_lan_ip
    # already excludes loopback separately, so that overlap is harmless.
    assert {"172.17.0.0/16", "172.18.0.0/16"} <= set(str(n) for n in nets)
    assert "192.168.50.0/24" not in set(str(n) for n in nets)


def test_virtual_subnets_empty_on_failure(monkeypatch):
    monkeypatch.setattr(bi, "_run", lambda cmd, timeout=10: "")
    assert bi._virtual_subnets() == []


def test_refresh_discovers_virtual_subnet_and_updates_is_lan_ip(monkeypatch, no_sources):
    ip_br_addr = "docker0          UP             172.17.0.1/16\n"

    def fake_run(cmd, timeout=10):
        return ip_br_addr if cmd[:2] == ["ip", "-br"] else ""

    monkeypatch.setattr(bi, "_run", fake_run)
    bi.get_inventory().refresh()
    # refresh() populated _virtual_subnets_cache from "ip -br addr" above;
    # is_lan_ip must now exclude the discovered docker range but keep a real
    # home LAN (iOS hotspot) address untouched.
    assert bi.is_lan_ip("172.17.0.5") is False
    assert bi.is_lan_ip("172.20.10.5") is True


# ── ARP / neighbour parsing ─────────────────────────────────────────────────

IP_NEIGH = """\
192.168.1.1 dev eth0 lladdr 3c:37:86:11:22:33 REACHABLE
192.168.1.20 dev eth0 lladdr b8:27:eb:44:55:66 STALE
192.168.1.99 dev eth0  FAILED
172.17.0.2 dev docker0 lladdr 02:42:ac:11:00:02 REACHABLE
192.168.50.55 dev wlan0 lladdr d0:03:4b:77:88:99 REACHABLE
"""


def test_parse_ip_neigh_drops_virtual_and_failed():
    rows = bi._parse_ip_neigh(IP_NEIGH)
    ips = [r["ip"] for r in rows]
    assert "192.168.1.99" not in ips           # FAILED entry
    assert "172.17.0.2" not in ips             # dev docker0
    assert ips == ["192.168.1.1", "192.168.1.20", "192.168.50.55"]
    assert rows[0]["mac"] == "3C:37:86:11:22:33"


def test_parse_ip_neigh_keeps_reachable_row_with_no_lladdr():
    # REACHABLE/STALE with no lladdr is a real device that just hasn't been
    # ARPed yet — unlike FAILED/INCOMPLETE, it must survive to key on IP.
    output = "192.168.1.201 dev eth0  REACHABLE\n"
    rows = bi._parse_ip_neigh(output)
    assert rows == [{"ip": "192.168.1.201", "mac": "", "iface": "eth0"}]


def test_scan_arp_keeps_mac_less_reachable_row(monkeypatch):
    output = "192.168.1.201 dev eth0  REACHABLE\n"
    monkeypatch.setattr(bi, "_run", lambda cmd, timeout=10: output if cmd[0] == "ip" else "")
    rows = bi._scan_arp()
    assert rows == [{"ip": "192.168.1.201", "mac": "", "iface": "eth0"}]


def test_refresh_keys_mac_less_arp_row_on_ip(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.201", "mac": "", "iface": "eth0"},
    ])
    device = bi.get_inventory().refresh()[0]
    assert device["key"] == "ip:192.168.1.201"
    assert device["mac"] == ""
    assert device["seen_by"] == ["arp"]


def test_parse_arp_a_windows_format():
    output = """
Interface: 192.168.1.10 --- 0x5
  Internet Address      Physical Address      Type
  192.168.1.1           3c-37-86-11-22-33     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
"""
    rows = bi._parse_arp_a(output)
    macs = [r["mac"] for r in rows]
    assert "3C:37:86:11:22:33" in macs
    assert "FF:FF:FF:FF:FF:FF" in macs   # _scan_arp, not the parser, filters it


def test_scan_arp_prefers_ip_neigh_and_filters(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd[0])
        return IP_NEIGH if cmd[0] == "ip" else ""

    monkeypatch.setattr(bi, "_run", fake_run)
    rows = bi._scan_arp()
    assert calls == ["ip"]                        # arp -a never reached
    assert {r["ip"] for r in rows} == {
        "192.168.1.1", "192.168.1.20", "192.168.50.55"}


def test_scan_arp_falls_back_to_arp_a(monkeypatch):
    arp_out = "  192.168.1.7   aa-bb-cc-dd-ee-ff  dynamic\n"

    def fake_run(cmd, timeout=10):
        return "" if cmd[0] == "ip" else arp_out

    monkeypatch.setattr(bi, "_run", fake_run)
    rows = bi._scan_arp()
    assert rows == [{"ip": "192.168.1.7", "mac": "AA:BB:CC:DD:EE:FF", "iface": ""}]


# ── mDNS ────────────────────────────────────────────────────────────────────

def test_scan_mdns_parses_resolved_records(monkeypatch):
    avahi = (
        "+;eth0;IPv4;Living\\032Room;_airplay._tcp;local\n"
        "=;eth0;IPv4;Living\\032Room;_airplay._tcp;local;tv.local;192.168.1.20;7000;\n"
        "=;eth0;IPv4;Printer;_ipp._tcp;local;hp.local;192.168.1.30;631;\n"
        "=;eth0;IPv6;Ghost;_ipp._tcp;local;x.local;fe80::1;631;\n"
    )
    monkeypatch.setattr(bi, "_run", lambda cmd, timeout=10: avahi)
    result = bi._scan_mdns()
    assert set(result) == {"192.168.1.20", "192.168.1.30"}   # IPv6 row dropped
    assert result["192.168.1.20"]["names"] == ["Living Room", "tv.local"]
    assert result["192.168.1.20"]["services"] == ["_airplay._tcp"]


def test_scan_mdns_absent_avahi_is_empty(monkeypatch):
    monkeypatch.setattr(bi, "_run", lambda cmd, timeout=10: "")
    assert bi._scan_mdns() == {}


# ── H2: avahi -p decimal/dot/backslash escapes ─────────────────────────────

def test_unescape_avahi_decimal_escape():
    assert bi._unescape_avahi("32BEF25ABC5E\\064Park") == "32BEF25ABC5E@Park"


def test_unescape_avahi_dot_escape():
    assert bi._unescape_avahi("70-35-60-63\\.1 Bedroom") == "70-35-60-63.1 Bedroom"


def test_unescape_avahi_backslash_escape():
    assert bi._unescape_avahi("a\\\\b") == "a\\b"


def test_unescape_avahi_empty_is_unchanged():
    assert bi._unescape_avahi("") == ""


def test_scan_mdns_unescapes_avahi_names(monkeypatch):
    avahi = (
        "=;eth0;IPv4;32BEF25ABC5E\\064Park;_googlecast._tcp;local;"
        "70-35-60-63\\.1\\032Bedroom;192.168.1.40;8009;\n"
    )
    monkeypatch.setattr(bi, "_run", lambda cmd, timeout=10: avahi)
    result = bi._scan_mdns()
    assert result["192.168.1.40"]["names"] == [
        "32BEF25ABC5E@Park", "70-35-60-63.1 Bedroom"]


# ── merge ───────────────────────────────────────────────────────────────────

def test_refresh_merges_all_sources(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.1", "mac": "3C:37:86:11:22:33", "iface": "eth0"},
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.20": {"names": ["Living Room TV"],
                         "services": ["_airplay._tcp"]},
    })
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "B8:27:EB:44:55:66", "ip": "", "entity_id": "media_player.tv",
        "name": "Living Room TV", "area": "Living Room",
    }])
    monkeypatch.setattr(bi, "_adguard_records", lambda: (
        {"192.168.1.20": "the tv"}, []))
    monkeypatch.setattr(bi, "default_gateway", lambda: "192.168.1.1")
    monkeypatch.setattr(bi, "_own_addresses", lambda: {"192.168.1.1"})

    devices = bi.get_inventory().refresh()
    by_mac = {d["mac"]: d for d in devices}

    tv = by_mac["B8:27:EB:44:55:66"]
    assert tv["seen_by"] == ["adguard", "arp", "ha", "mdns"]
    assert tv["ha_area"] == "Living Room"
    assert tv["ha_entity_id"] == "media_player.tv"
    assert tv["adguard_name"] == "the tv"
    assert tv["mdns_services"] == ["_airplay._tcp"]
    assert tv["display_name"] == "Living Room TV"
    assert tv["is_gateway"] is False

    router = by_mac["3C:37:86:11:22:33"]
    # H3: it is also this host's own address (mocked above), so the self
    # pass marks it too, in addition to "arp".
    assert router["seen_by"] == ["arp", "self"]
    assert router["is_gateway"] is True
    assert router["is_self"] is True


def test_refresh_classifies_manufacturer_and_category(monkeypatch, no_sources):
    # B8:27:EB is the Raspberry Pi Foundation OUI in device_classifier.
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.50", "mac": "B8:27:EB:00:00:01", "iface": "eth0"},
    ])
    device = bi.get_inventory().refresh()[0]
    assert device["category"]                      # always populated
    assert isinstance(device["manufacturer"], str)


def test_first_seen_is_sticky_across_refreshes(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    inv = bi.get_inventory()
    first = inv.refresh()[0]
    second = inv.refresh()[0]
    assert second["first_seen"] == first["first_seen"]
    assert second["last_seen"] >= first["last_seen"]


def test_device_without_mac_keys_on_ip(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.77": {"names": ["mystery"], "services": []},
    })
    device = bi.get_inventory().refresh()[0]
    assert device["key"] == "ip:192.168.1.77"
    assert device["mac"] == ""
    assert device["seen_by"] == ["mdns"]


def test_ha_record_without_a_matching_device_is_dropped(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "AA:AA:AA:AA:AA:AA", "ip": "10.0.0.9",
        "entity_id": "sensor.ghost", "name": "Ghost", "area": "Nowhere",
    }])
    # HA never invents a network device; it only decorates one ARP/mDNS found.
    assert bi.get_inventory().refresh() == []


def test_refresh_persists_and_reloads(monkeypatch, no_sources, tmp_path):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    bi.get_inventory().refresh()
    path = tmp_path / "bridge_devices.json"
    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["devices"][0]["mac"] == "B8:27:EB:44:55:66"

    bi.reset_inventory()
    reloaded = bi.get_inventory().devices()
    assert reloaded[0]["mac"] == "B8:27:EB:44:55:66"


def test_corrupt_devices_file_starts_fresh(monkeypatch, no_sources, tmp_path):
    (tmp_path / "bridge_devices.json").write_text("{ not json")
    bi.reset_inventory()
    assert bi.get_inventory().devices() == []


# ── lookups + status ────────────────────────────────────────────────────────

def test_lookups_by_mac_ip_and_key(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    inv = bi.get_inventory()
    inv.refresh()
    assert inv.get("b8:27:eb:44:55:66")["ip"] == "192.168.1.20"
    assert inv.get("B8:27:EB:44:55:66")["ip"] == "192.168.1.20"
    assert inv.get("192.168.1.20")["mac"] == "B8:27:EB:44:55:66"
    assert inv.get("nope") is None
    assert inv.mac_for_ip("192.168.1.20") == "B8:27:EB:44:55:66"
    assert inv.mac_for_ip("192.168.1.99") == ""
    assert inv.key_for_ip("192.168.1.20") == "B8:27:EB:44:55:66"


def test_status_reports_age_counts_and_source_health(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    inv = bi.get_inventory()
    inv.refresh()
    status = inv.status()
    assert status["device_count"] == 1
    assert status["new_24h"] == 1
    assert status["inventory_age_seconds"] is not None
    assert status["inventory_age_seconds"] < 60
    assert status["sources"]["arp"] == {"ok": True, "count": 1}
    assert status["sources"]["mdns"] == {"ok": False, "count": 0}
    assert status["refresher_running"] is False


def test_new_since_ignores_old_devices(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    inv = bi.get_inventory()
    inv.refresh()
    inv._devices["B8:27:EB:44:55:66"].first_seen = "2020-01-01T00:00:00+00:00"
    assert inv.new_since(24) == 0


# ── enablement + degradation ────────────────────────────────────────────────

def test_is_enabled_env_flag(monkeypatch):
    monkeypatch.setenv(bi.ENABLE_ENV, "1")
    assert bi.is_enabled() is True
    monkeypatch.setenv(bi.ENABLE_ENV, "0")
    # Falls through to the config flag, which is False by default in tests.
    assert isinstance(bi.is_enabled(), bool)


def test_is_enabled_reads_zone3_flag_without_touching_config_py(monkeypatch):
    monkeypatch.delenv(bi.ENABLE_ENV, raising=False)

    class _Zone3:
        enabled = True

    class _Cfg:
        zone3 = _Zone3()

    import puffpai.config as config
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    assert bi.is_enabled() is True


def test_is_enabled_survives_missing_zone3_field(monkeypatch):
    monkeypatch.delenv(bi.ENABLE_ENV, raising=False)

    class _Cfg:
        pass

    import puffpai.config as config
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    assert bi.is_enabled() is False


def test_ha_records_degrade_when_bridge_unavailable(monkeypatch):
    import puffpai.ha_bridge as ha_bridge

    def boom(**kwargs):
        raise RuntimeError("vault locked")

    monkeypatch.setattr(ha_bridge, "get_bridge", boom)
    assert bi._ha_records() == []
    # H5: the failure is recorded (error type name only), not just swallowed.
    assert bi._ha_last_error == "RuntimeError"
    assert bi._ha_last_entity_count == 0


# ── H3: this host's own device ──────────────────────────────────────────────

def test_refresh_marks_own_ip_as_self_even_without_arp_entry(monkeypatch, no_sources):
    """The Orin never appears as its own ARP neighbour, so H3 must add it."""
    monkeypatch.setattr(bi, "_own_addresses", lambda: {"192.168.50.40"})
    monkeypatch.setattr(bi, "_own_mac", lambda: "AA:BB:CC:DD:EE:FF")
    devices = bi.get_inventory().refresh()
    assert len(devices) == 1
    me = devices[0]
    assert me["ip"] == "192.168.50.40"
    assert me["is_self"] is True
    assert me["mac"] == "AA:BB:CC:DD:EE:FF"
    assert me["category"] == "puffpai"


def test_refresh_fills_missing_mac_for_arp_seen_self(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.50.40": {"names": ["PuffPAI"], "services": []},
    })
    monkeypatch.setattr(bi, "_own_addresses", lambda: {"192.168.50.40"})
    monkeypatch.setattr(bi, "_own_mac", lambda: "AA:BB:CC:DD:EE:FF")
    device = bi.get_inventory().refresh()[0]
    assert device["is_self"] is True
    assert device["mac"] == "AA:BB:CC:DD:EE:FF"
    assert device["category"] == "puffpai"


def test_own_mac_parses_ip_br_addr_and_link(monkeypatch):
    def fake_run(cmd, timeout=10):
        if cmd[:3] == ["ip", "-br", "addr"]:
            return "eth0             UP             192.168.50.40/24 \n"
        if cmd[:4] == ["ip", "-br", "link", "show"]:
            return "eth0             UP             aa:bb:cc:dd:ee:ff <BROADCAST,MULTICAST,UP>\n"
        return ""
    monkeypatch.setattr(bi, "_run", fake_run)
    assert bi._own_mac() == "AA:BB:CC:DD:EE:FF"


def test_own_addresses_prefers_ip_br_addr(monkeypatch):
    ip_br_addr = (
        "lo               UNKNOWN        127.0.0.1/8 \n"
        "docker0          DOWN           172.17.0.1/16 \n"
        "eth0             UP             192.168.50.40/24 \n"
    )
    monkeypatch.setattr(bi, "_run", lambda cmd, timeout=10: ip_br_addr)
    found = bi._own_addresses()
    assert "192.168.50.40" in found
    assert "172.17.0.1" not in found   # docker0 is virtual, excluded


# ── H4: manufacturer/category enrichment ────────────────────────────────────

def test_locally_administered_mac_is_randomised_not_ouid(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.60", "mac": "02:11:22:33:44:55", "iface": "eth0"},
    ])
    device = bi.get_inventory().refresh()[0]
    assert device["manufacturer"] == "(randomised MAC)"


def test_oui_table_linksys(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.61", "mac": "D8:EC:5E:11:22:33", "iface": "eth0"},
    ])
    # Force the shared classifier to say "computer"/no manufacturer, as it
    # does for a Linksys mesh node.
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "computer"))
    device = bi.get_inventory().refresh()[0]
    assert device["manufacturer"] == "Linksys"
    assert device["classified_by"] == "oui"


def test_hostname_heuristic_canon_printer(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.62", "mac": "00:11:22:33:44:66", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.62": {"names": ["Canon MG3600"], "services": []},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "computer"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "printer"
    assert device["classified_by"] == "hostname"


def test_mdns_service_heuristic_ipp_printer(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.63", "mac": "00:11:22:33:44:77", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.63": {"names": ["some-printer"], "services": ["_ipp._tcp"]},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "unknown"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "printer"
    assert device["classified_by"] == "mdns:_ipp._tcp"


def test_hostname_heuristic_linksys_network_infra(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.70", "mac": "00:11:22:33:44:88", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.50.70": {"names": ["Linksys42"], "services": []},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "computer"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "network_infra"


def test_hostname_heuristic_home_assistant(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.65", "mac": "00:11:22:33:44:99", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.65": {"names": ["homeassistant.local"], "services": []},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "unknown"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "home_assistant"
    assert device["manufacturer"] == "Home Assistant"


def test_mdns_heuristic_googlecast_streaming_box(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.66", "mac": "00:11:22:33:44:AA", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.66": {"names": ["Living Room Chromecast"],
                         "services": ["_googlecast._tcp"]},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "unknown"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "streaming_box"
    assert device["manufacturer"] == "Google"


def test_mdns_heuristic_airplay_needs_apple_ish_name(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.67", "mac": "00:11:22:33:44:BB", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.67": {"names": ["Living Room AppleTV"],
                         "services": ["_airplay._tcp"]},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "unknown"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "streaming_box"
    assert device["manufacturer"] == "Apple"


def test_mdns_heuristic_spotify_connect_smart_speaker(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.68", "mac": "00:11:22:33:44:CC", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.1.68": {"names": ["Kitchen Speaker"],
                         "services": ["_spotify-connect._tcp"]},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "unknown"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "smart_speaker"


def test_mdns_heuristic_roku_streaming_box(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.71", "mac": "AC:AE:19:11:22:33", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.50.71": {"names": ["Roku"], "services": ["_roku._tcp"]},
    })
    monkeypatch.setattr(bi, "_classify", lambda mac, hostname, services: ("", "unknown"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "streaming_box"
    assert device["manufacturer"] == "Roku"


def test_classifier_result_kept_when_not_unknown_or_computer(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.70", "mac": "00:11:22:33:44:DD", "iface": "eth0"},
    ])
    monkeypatch.setattr(
        bi, "_classify",
        lambda mac, hostname, services: ("Samsung", "smart_tv"))
    device = bi.get_inventory().refresh()[0]
    assert device["category"] == "smart_tv"
    assert device["manufacturer"] == "Samsung"
    assert device["classified_by"] == "classifier"


# ── H5: HA source health in status() ────────────────────────────────────────

def test_status_sources_ha_reports_error_and_counts(monkeypatch):
    # Leave _ha_records as the real function so it actually hits (and
    # swallows) the broken bridge below, exactly as refresh() would.
    import puffpai.ha_bridge as ha_bridge

    def boom(**kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(bi, "_scan_arp", lambda: [])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {})
    monkeypatch.setattr(bi, "_adguard_records", lambda: ({}, []))
    monkeypatch.setattr(bi, "default_gateway", lambda: "")
    monkeypatch.setattr(bi, "_own_addresses", lambda: set())
    monkeypatch.setattr(ha_bridge, "get_bridge", boom)

    bi.get_inventory().refresh()
    status = bi.get_inventory().status()
    ha_health = status["sources"]["ha"]
    assert ha_health["ok"] is False
    assert ha_health["error"] == "RuntimeError"
    assert ha_health["entities"] == 0
    assert ha_health["matched"] == 0


def test_status_sources_ha_ok_when_reachable(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_ha_records", lambda: [])
    monkeypatch.setattr(bi, "_ha_last_error", "", raising=False)
    monkeypatch.setattr(bi, "_ha_last_method", "attributes", raising=False)
    monkeypatch.setattr(bi, "_ha_last_entity_count", 3, raising=False)
    bi.get_inventory().refresh()
    ha_health = bi.get_inventory().status()["sources"]["ha"]
    assert ha_health["ok"] is True
    assert ha_health["entities"] == 3
    assert ha_health["method"] == "attributes"
    assert ha_health["matched"] == 0


# ── D1: the Home Assistant device registry (WebSocket) ──────────────────────

DEVICE_REGISTRY = [
    {"id": "dev1", "name": "Samsung TV", "name_by_user": "Living Room TV",
     "manufacturer": "Samsung", "model": "QN90A", "area_id": "living_room",
     "connections": [["mac", "b8:27:eb:44:55:66"]], "identifiers": []},
    {"id": "dev2", "name": "Sun", "connections": [], "identifiers": []},
    {"id": "dev3", "name": "Porch Plug", "manufacturer": "Shelly",
     "connections": [], "identifiers": [["shelly", "aabbccddeeff"]]},
]
ENTITY_REGISTRY = [
    {"entity_id": "media_player.tv", "device_id": "dev1"},
    {"entity_id": "sensor.tv_power", "device_id": "dev1"},
    {"entity_id": "switch.porch", "device_id": "dev3", "area_id": "porch"},
    {"entity_id": "sun.sun", "device_id": None},
]
AREA_REGISTRY = [
    {"area_id": "living_room", "name": "Living Room"},
    {"area_id": "porch", "name": "Porch"},
]


class _FakeWS:
    """The three registry commands, over a scripted HA WebSocket."""

    def __init__(self, *, auth_ok=True, fail=None):
        self._auth_ok = auth_ok
        self._fail = fail or set()
        self._outbox = [{"type": "auth_required", "ha_version": "2026.8.3"}]
        self.closed = False
        self._results = {
            "config/device_registry/list": DEVICE_REGISTRY,
            "config/entity_registry/list": ENTITY_REGISTRY,
            "config/area_registry/list": AREA_REGISTRY,
        }

    def send(self, raw):
        message = json.loads(raw)
        kind = message.get("type")
        if kind == "auth":
            self._outbox.append(
                {"type": "auth_ok"} if self._auth_ok else {"type": "auth_invalid"})
            return
        if kind in self._fail:
            self._outbox.append({"id": message["id"], "type": "result",
                                 "success": False,
                                 "error": {"code": "unauthorized"}})
            return
        self._outbox.append({"id": message["id"], "type": "result",
                             "success": True, "result": self._results[kind]})

    def recv(self):
        return json.dumps(self._outbox.pop(0))

    def close(self):
        self.closed = True


@pytest.fixture
def ha_registry(monkeypatch):
    """Point _ha_records() at the scripted registry socket above."""
    sockets = []

    def connect(url):
        assert url == "ws://192.168.50.140:8123/api/websocket"
        sockets.append(_FakeWS())
        return sockets[-1]

    monkeypatch.setattr(
        bi, "_ha_resolve", lambda: ("http://192.168.50.140:8123", "tok3n", True))
    monkeypatch.setattr(bi, "_ha_ws_connect", connect)
    return sockets


def test_ha_records_use_the_device_registry(ha_registry):
    records = bi._ha_records()
    by_mac = {r["mac"]: r for r in records}

    assert bi._ha_last_method == "registry"
    assert bi._ha_last_error == ""
    assert bi._ha_last_device_count == 3
    assert bi._ha_last_entity_count == 4

    tv = by_mac["B8:27:EB:44:55:66"]
    assert tv["name"] == "Living Room TV"          # name_by_user wins
    assert tv["area"] == "Living Room"
    assert tv["manufacturer"] == "Samsung"
    assert tv["model"] == "QN90A"
    assert tv["entity_ids"] == ["media_player.tv", "sensor.tv_power"]
    assert tv["entity_id"] == "media_player.tv"

    # A separator-less identifier (ESPHome/Shelly's habit) reads as a MAC, and
    # the area comes from its one entity because the device has none.
    plug = by_mac["AA:BB:CC:DD:EE:FF"]
    assert plug["area"] == "Porch"
    assert plug["manufacturer"] == "Shelly"

    # A device with neither a connection nor a usable identifier is KEPT, but
    # with no address: it can only ever join by the name rule, which fires
    # only when exactly one inventory row answers to that name. Dropping it
    # here would make the name rule impossible for the many HA records that
    # carry no address.
    assert len(records) == 3
    addressless = [r for r in records if not r["mac"] and not r["ip"]]
    assert [r["name"] for r in addressless] == ["Sun"]
    assert ha_registry[0].closed is True


def test_refresh_joins_ha_registry_to_an_arp_row(monkeypatch, no_sources,
                                                 ha_registry):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_ha_records", _REAL_HA_RECORDS)

    devices = bi.get_inventory().refresh()
    tv = devices[0]
    assert tv["ha_name"] == "Living Room TV"
    assert tv["ha_area"] == "Living Room"
    assert tv["ha_entity_id"] == "media_player.tv"
    assert "ha" in tv["seen_by"]

    ha_health = bi.get_inventory().status()["sources"]["ha"]
    assert ha_health == {
        "ok": True, "entities": 4, "devices": 3, "matched": 1,
        # The Shelly has a MAC nothing on the LAN answers for; the cloud
        # device has no address at all. Both counted, so the owner can tell
        # "HA knows things this network cannot see" from "the join is broken".
        "unmatched": {"no_address": 1, "address_not_on_lan": 0,
                      "no_inventory_row": 1},
        "method": "registry", "error": ""}


def test_ha_registry_device_without_connections_never_matches(
        monkeypatch, no_sources, ha_registry):
    """The Shelly is in HA but nothing on the LAN answers for it."""
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_ha_records", _REAL_HA_RECORDS)
    devices = bi.get_inventory().refresh()
    # Two HA records, one LAN device: HA never invents a network device.
    assert len(devices) == 1
    assert bi.get_inventory().status()["sources"]["ha"]["matched"] == 1


def test_ha_records_fall_back_to_attributes_when_the_socket_fails(monkeypatch):
    """WS down → attribute scrape, with the WS failure recorded by type."""
    def boom(url):
        raise ConnectionRefusedError("no route")

    monkeypatch.setattr(
        bi, "_ha_resolve", lambda: ("http://192.168.50.140:8123", "tok3n", True))
    monkeypatch.setattr(bi, "_ha_ws_connect", boom)

    class _Dev:
        entity_id = "device_tracker.phone"
        friendly_name = "Phone"
        area_name = "Kitchen"
        attributes = {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.44"}

    class _Bridge:
        def list_devices(self, controllable_only=True):
            return [_Dev()]

    import puffpai.ha_bridge as ha_bridge
    monkeypatch.setattr(ha_bridge, "get_bridge", lambda **kw: _Bridge())

    records = bi._ha_records()
    assert bi._ha_last_method == "attributes"
    assert bi._ha_last_error == "ConnectionRefusedError"
    assert bi._ha_last_entity_count == 1
    assert records[0]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert records[0]["ip"] == "192.168.1.44"


def test_ha_records_report_a_refused_registry_command(monkeypatch):
    def connect(url):
        return _FakeWS(fail={"config/device_registry/list"})

    monkeypatch.setattr(
        bi, "_ha_resolve", lambda: ("http://192.168.50.140:8123", "tok3n", True))
    monkeypatch.setattr(bi, "_ha_ws_connect", connect)
    import puffpai.ha_bridge as ha_bridge

    def no_bridge(**kwargs):
        raise RuntimeError("vault locked")

    monkeypatch.setattr(ha_bridge, "get_bridge", no_bridge)
    assert bi._ha_records() == []
    assert bi._ha_last_method == ""
    assert bi._ha_last_error == "RuntimeError"     # the refusal, not the vault


def test_ha_records_never_open_a_socket_without_a_token(monkeypatch):
    def never(url):
        raise AssertionError("must not connect without credentials")

    monkeypatch.setattr(bi, "_ha_resolve", lambda: ("http://x:8123", "", True))
    monkeypatch.setattr(bi, "_ha_ws_connect", never)
    import puffpai.ha_bridge as ha_bridge
    monkeypatch.setattr(
        ha_bridge, "get_bridge",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("locked")))
    assert bi._ha_records() == []
    assert bi._ha_last_error == "RuntimeError"


# ── D2: AdGuard clients + auto_clients ──────────────────────────────────────

CLIENTS_PAYLOAD = {
    "clients": [
        {"name": "the tv", "ids": ["192.168.1.20", "aa:bb:cc:dd:ee:ff"]},
        {"name": "nameless", "ids": []},
        "not-a-dict",
    ],
    "auto_clients": [
        {"ip": "192.168.1.30", "name": "printer.lan", "source": "rDNS"},
        {"ip": "192.168.1.20", "name": "", "source": "ARP"},
        {"ip": "172.17.0.3", "name": "", "source": "ARP"},   # docker, dropped
        {"ip": "", "name": "junk", "source": "etc/hosts"},
    ],
    "supported_tags": [],
}


@pytest.fixture
def adguard_http(monkeypatch):
    """Zone 3 on, with /control/clients served from CLIENTS_PAYLOAD."""
    class _Zone3:
        enabled = True
        adguard_url = "http://192.168.50.140:3000/"
        adguard_username = "puffpai"
        adguard_password = "secret"

    class _Cfg:
        zone3 = _Zone3()

    calls = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return CLIENTS_PAYLOAD

    def fake_get(url, auth=None, timeout=None):
        calls["url"] = url
        calls["auth"] = auth
        return _Resp()

    import puffpai.config as config
    from puffpai.security import lan_http
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    # Patched at puffpai.security.lan_http, not at ``requests``: the AdGuard
    # readers go through the house-local wrapper now, which resolves the
    # configured URL and refuses anything that is not loopback/RFC1918/ULA
    # before a socket opens. Patching ``requests`` here would patch nothing
    # and the test would dial the real address in the config.
    monkeypatch.setattr(lan_http, "lan_get", fake_get)
    return calls


def test_adguard_records_reads_both_lists(adguard_http):
    mapping, auto = bi._adguard_records()

    assert adguard_http["url"] == "http://192.168.50.140:3000/control/clients"
    assert adguard_http["auth"] == ("puffpai", "secret")

    assert mapping["192.168.1.20"] == "the tv"
    assert mapping["AA:BB:CC:DD:EE:FF"] == "the tv"   # MACs upper-cased to match
    assert auto == [
        {"ip": "192.168.1.30", "name": "printer.lan", "source": "rDNS"},
        {"ip": "192.168.1.20", "name": "", "source": "ARP"},
        {"ip": "172.17.0.3", "name": "", "source": "ARP"},
    ]
    assert bi._adguard_last_error == ""
    assert bi._adguard_last_persistent == 2
    assert bi._adguard_last_auto == 4


def test_refresh_creates_a_row_for_a_dns_only_device(monkeypatch, no_sources,
                                                     adguard_http):
    """A device ARP and mDNS never saw is still visible if it resolves DNS."""
    monkeypatch.setattr(
        bi, "_virtual_subnets_cache",
        [__import__("ipaddress").ip_network("172.17.0.0/16")])
    monkeypatch.setattr(bi, "_virtual_subnets", lambda: list(
        bi._virtual_subnets_cache))
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "B8:27:EB:44:55:66", "iface": "eth0"},
    ])
    monkeypatch.setattr(bi, "_adguard_records", _REAL_ADGUARD_RECORDS)

    devices = bi.get_inventory().refresh()
    by_key = {d["key"]: d for d in devices}

    # The persistent client decorated the ARP-known TV...
    assert by_key["B8:27:EB:44:55:66"]["adguard_name"] == "the tv"
    assert "adguard" in by_key["B8:27:EB:44:55:66"]["seen_by"]
    # ...and the DNS-only printer got a row of its own, keyed on its IP.
    printer = by_key["ip:192.168.1.30"]
    assert printer["adguard_name"] == "printer.lan"
    assert printer["seen_by"] == ["adguard"]
    # The docker-bridge auto client is not on the LAN and never appears.
    assert "ip:172.17.0.3" not in by_key


def test_status_sources_adguard_is_ok_with_zero_persistent_clients(
        monkeypatch, no_sources, adguard_http):
    """The deployed box: AdGuard answers 200 with no persistent clients.

    That is "nobody has named a client", not "AdGuard is down" — the old
    ``ok: bool(mapping)`` reported the healthy case as a failure.
    """
    monkeypatch.setattr(bi, "_adguard_records", _REAL_ADGUARD_RECORDS)
    monkeypatch.setitem(CLIENTS_PAYLOAD, "clients", [])
    bi.get_inventory().refresh()
    health = bi.get_inventory().status()["sources"]["adguard"]
    assert health == {"ok": True, "persistent": 0, "auto": 4, "error": ""}


def test_adguard_records_empty_when_zone3_off(monkeypatch):
    class _Zone3:
        enabled = False

    class _Cfg:
        zone3 = _Zone3()

    import puffpai.config as config
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    assert bi._adguard_records() == ({}, [])
    assert bi._adguard_last_error == "disabled"


def test_adguard_records_record_a_transport_failure(monkeypatch, adguard_http):
    import requests
    from puffpai.security import lan_http

    def boom(url, auth=None, timeout=None):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(lan_http, "lan_get", boom)
    assert bi._adguard_records() == ({}, [])
    assert bi._adguard_last_error == "RequestException"


def test_run_returns_empty_when_command_missing(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(bi.subprocess, "run", boom)
    assert bi._run(["definitely-not-a-real-binary"]) == ""


def test_refresher_starts_and_stops(monkeypatch, no_sources):
    inv = bi.get_inventory()
    assert inv.start_refresher(interval=30) is True
    assert inv.start_refresher(interval=30) is True   # idempotent
    assert inv.status()["refresher_running"] is True
    assert inv.stop_refresher() is True
    assert inv.status()["refresher_running"] is False


def test_stop_refresher_exits_promptly_and_writes_nothing_after_stop(
        monkeypatch, no_sources, tmp_path):
    """A slow source must not delay stop, and must not write after it.

    ``_scan_arp`` blocks on an Event that only the main thread controls, so
    the refresher is guaranteed to be mid-scan when ``stop_refresher()`` is
    called. The refresh loop's between-source stop check must make it bail
    out of that in-flight pass without merging or writing.
    """
    release = threading.Event()
    entered = threading.Event()
    save_calls = []

    real_save = bi.BridgeInventory._save

    def counting_save(self):
        save_calls.append(True)
        return real_save(self)

    def slow_scan_arp():
        entered.set()
        release.wait(timeout=5)
        return []

    monkeypatch.setattr(bi, "_scan_arp", slow_scan_arp)
    monkeypatch.setattr(bi.BridgeInventory, "_save", counting_save)

    inv = bi.get_inventory()
    inv.start_refresher(interval=30)
    assert entered.wait(timeout=5), "refresher never entered the slow source"

    result = {}

    def _stop():
        result["stopped"] = inv.stop_refresher(timeout=5)

    stopper = threading.Thread(target=_stop)
    start = time.monotonic()
    stopper.start()
    # stop_refresher() has set the stop event and is now blocked in join();
    # unblock the scan so the loop can notice the stop flag and exit.
    release.set()
    stopper.join(timeout=5)
    elapsed = time.monotonic() - start

    assert result["stopped"] is True
    assert elapsed < 5, "stop_refresher took as long as the full join timeout"
    assert save_calls == []
    assert not (tmp_path / "bridge_devices.json").exists()


# ── file permissions ─────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode bits only")
def test_devices_file_written_0600(tmp_path, monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "AA:BB:CC:00:00:01"}])
    inv = bi.BridgeInventory()
    inv.refresh()
    path = tmp_path / "bridge_devices.json"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_devices_file_write_succeeds_on_any_os(tmp_path, monkeypatch, no_sources):
    """Windows-safe: the 0600 open flags must not break the write itself."""
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.1.20", "mac": "AA:BB:CC:00:00:01"}])
    inv = bi.BridgeInventory()
    inv.refresh()
    path = tmp_path / "bridge_devices.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["devices"]


# ── the Home Assistant join: four rules, and why a device did not join ───────
#
# A typical sources.ha read looks like
# {"devices": 46, "entities": 385, "matched": 1, "method": "registry"}, the
# registry path works and exactly one device (the router) joins. HA records
# a colon-separated MAC for very few integrations; everything else is known by
# an identifier with the MAC baked into it, by the address in its
# configuration_url, or by nothing but a name. Each rule below is one of those.

def _device(**over):
    """One HA device-registry row, with the fields the join reads."""
    row = {"id": "d1", "name": "Thing", "name_by_user": "",
           "connections": [], "identifiers": [], "area_id": ""}
    row.update(over)
    return row


def test_registry_normalises_a_mac_in_any_separator_form():
    for written in ("aa:bb:cc:11:22:33", "AA-BB-CC-11-22-33",
                    "aabbcc112233", "AABBCC112233"):
        records = bi._registry_records(
            [_device(connections=[["mac", written]])], [], [])
        assert records[0]["mac"] == "AA:BB:CC:11:22:33", written
        assert records[0]["mac_from_identifier"] is False


def test_registry_reads_an_ip_from_connections_and_configuration_url():
    from_conn = bi._registry_records(
        [_device(connections=[["ip", "192.168.50.53"]])], [], [])
    assert from_conn[0]["ip"] == "192.168.50.53"

    from_host = bi._registry_records(
        [_device(connections=[["host", "192.168.50.54"]])], [], [])
    assert from_host[0]["ip"] == "192.168.50.54"

    from_url = bi._registry_records(
        [_device(configuration_url="http://192.168.50.55/settings")], [], [])
    assert from_url[0]["ip"] == "192.168.50.55"

    # A hostname URL is NOT an address: it goes to the name rule, which has
    # the one-row-only guard. Guessing here would join the wrong device.
    by_name = bi._registry_records(
        [_device(configuration_url="http://shelly-kitchen.local/")], [], [])
    assert by_name[0]["ip"] == ""


@pytest.mark.parametrize("identifier", [
    "24a160aabbcc",                       # ESPHome
    "shellyplug-s-AABBCCDDEEFF",          # Shelly
    "tasmota_AABBCCDDEEFF_switch",        # Tasmota
    "tuya.aabbccddeeff",                  # Tuya
    "aa:bb:cc:dd:ee:ff",                  # a plain MAC used as an identifier
])
def test_registry_digs_a_mac_out_of_an_identifier(identifier):
    records = bi._registry_records(
        [_device(identifiers=[["esphome", identifier]])], [], [])
    assert records[0]["mac"].count(":") == 5
    assert records[0]["mac_from_identifier"] is True


def test_a_zigbee_ieee_address_is_not_read_as_a_mac():
    """16 hex digits is an IEEE address, not a MAC with 4 shaved off."""
    records = bi._registry_records(
        [_device(identifiers=[["mqtt", "0x00124b0022aabbcc"]])], [], [])
    assert records[0]["mac"] == ""


def test_join_by_mac_from_connections(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.20", "mac": "B8:27:EB:44:55:66"}])
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "B8:27:EB:44:55:66", "ip": "", "entity_id": "light.lamp",
        "name": "Lamp", "area": "Den", "mac_from_identifier": False}])
    device = bi.get_inventory().refresh()[0]
    assert device["ha_match"] == "mac"
    assert device["ha_name"] == "Lamp"


def test_join_by_identifier_mac_is_reported_as_identifier(monkeypatch,
                                                          no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.21", "mac": "24:A1:60:AA:BB:CC"}])
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "24:A1:60:AA:BB:CC", "ip": "", "entity_id": "switch.plug",
        "name": "Plug", "area": "", "mac_from_identifier": True}])
    device = bi.get_inventory().refresh()[0]
    assert device["ha_match"] == "identifier"


def test_join_by_ip(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.87", "mac": "00:00:5E:00:53:87"}])
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "", "ip": "192.168.50.87", "entity_id": "sensor.printer",
        "name": "Canon G3070", "area": "Office"}])
    device = bi.get_inventory().refresh()[0]
    assert device["ha_match"] == "ip"
    assert device["ha_area"] == "Office"


def test_join_by_name_when_exactly_one_row_answers(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.61", "mac": "00:00:5E:00:53:61"}])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.50.61": {"names": ["G-O-R-R.local"], "services": []}})
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "", "ip": "", "entity_id": "sensor.gorr",
        "name": "g o r r", "area": "Study"}])
    device = bi.get_inventory().refresh()[0]
    assert device["ha_match"] == "name"
    assert device["ha_name"] == "g o r r"


def test_an_ambiguous_name_joins_nothing(monkeypatch, no_sources):
    """Three rows on this network are called "Bedroom". None may claim it."""
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.76", "mac": "AA:AA:AA:00:00:01"},
        {"ip": "192.168.50.77", "mac": "AA:AA:AA:00:00:02"}])
    monkeypatch.setattr(bi, "_scan_mdns", lambda: {
        "192.168.50.76": {"names": ["Bedroom"], "services": []},
        "192.168.50.77": {"names": ["bedroom"], "services": []}})
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "", "ip": "", "entity_id": "light.bedroom",
        "name": "Bedroom", "area": "Bedroom"}])
    devices = bi.get_inventory().refresh()
    assert [d["ha_match"] for d in devices] == ["", ""]
    assert all(d["ha_name"] == "" for d in devices)


def test_status_reports_why_ha_records_did_not_join(monkeypatch, no_sources):
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.20", "mac": "B8:27:EB:44:55:66"}])
    monkeypatch.setattr(bi, "_ha_records", lambda: [
        # joins
        {"mac": "B8:27:EB:44:55:66", "ip": "", "entity_id": "light.lamp",
         "name": "Lamp", "area": ""},
        # a cloud integration: no address of any kind
        {"mac": "", "ip": "", "entity_id": "sensor.cloud",
         "name": "Weather Service", "area": ""},
        # an address, but not on this LAN
        {"mac": "", "ip": "8.8.8.8", "entity_id": "sensor.remote",
         "name": "Remote", "area": ""},
        # a LAN address nothing on the roll answers to
        {"mac": "", "ip": "192.168.50.199", "entity_id": "sensor.gone",
         "name": "Gone", "area": ""},
    ])
    inv = bi.get_inventory()
    inv.refresh()
    ha = inv.status()["sources"]["ha"]
    assert ha["matched"] == 1
    assert ha["unmatched"] == {"no_address": 1, "address_not_on_lan": 1,
                               "no_inventory_row": 1}


# ── the hub row and the bucket stamp ────────────────────────────────────────

def test_the_home_assistant_host_is_marked_as_the_hub(monkeypatch, no_sources):
    """The roll's most important row. Eighteen devices answer ARP and about a
    hundred do not; the hundred reach the outside only through this one, and a
    row that reads like any other hides that."""
    from puffpai import bridge_ha
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.140", "mac": "00:00:5E:00:53:99"},
        {"ip": "192.168.50.20", "mac": "B8:27:EB:44:55:66"}])
    monkeypatch.setattr(bi, "_ha_records", lambda: [])
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: "192.168.50.140")
    monkeypatch.setattr(bi, "_ha_last_device_count", 46)

    by_ip = {d["ip"]: d for d in bi.get_inventory().refresh()}
    assert by_ip["192.168.50.140"]["hub"] is True
    assert by_ip["192.168.50.140"]["manages"] == 46
    assert by_ip["192.168.50.20"]["hub"] is False
    assert by_ip["192.168.50.20"]["manages"] == 0


def test_a_joined_row_carries_the_bucket_it_also_appears_in(monkeypatch,
                                                            no_sources):
    from puffpai import bridge_ha
    bridge_ha.reset()
    bridge_ha.set_registries(
        [{"id": "d1", "name": "Camera", "identifiers": [["ring", "s1"]]}],
        [{"entity_id": "camera.doorbell", "device_id": "d1",
          "platform": "ring"}],
        [])
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: "")
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.30", "mac": "AA:BB:CC:00:00:30"}])
    monkeypatch.setattr(bi, "_ha_records", lambda: [{
        "mac": "AA:BB:CC:00:00:30", "ip": "", "entity_id": "camera.doorbell",
        "name": "Doorbell", "area": "Porch"}])
    try:
        device = bi.get_inventory().refresh()[0]
        assert device["ha_match"] == "mac"
        assert device["bucket"] == "cloud:ring"
    finally:
        bridge_ha.reset()


def test_a_row_ha_never_joined_carries_no_bucket(monkeypatch, no_sources):
    from puffpai import bridge_ha
    bridge_ha.reset()
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: "")
    monkeypatch.setattr(bi, "_scan_arp", lambda: [
        {"ip": "192.168.50.31", "mac": "AA:BB:CC:00:00:31"}])
    assert bi.get_inventory().refresh()[0]["bucket"] == ""


def test_the_registry_fetch_hands_the_raw_registries_to_the_bucket_view(
        ha_registry):
    """The bucket view must never open a WebSocket of its own: the token is
    vault-gated and one pull per refresh is the whole budget."""
    from puffpai import bridge_ha
    bridge_ha.reset()
    try:
        bi._ha_records()
        devices, entities, areas = bridge_ha.cached_registries()
        assert devices and entities
        assert all(isinstance(d, dict) for d in devices)
    finally:
        bridge_ha.reset()
