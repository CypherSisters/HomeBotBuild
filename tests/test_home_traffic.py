"""The Traffic layer: puffpai.home_traffic and GET /ha/home/traffic.

The floorplan's second layer says which of the things in the house are talking
to the internet. It joins the blueprint's devices, the LAN inventory and the
telemetry ledger's existing rollup — and every string in two of those three
came off the network, chosen by the device itself. These tests pin:

* the join (bridge_key links a home device to its ledger row; an inventory row
  no home device claims lands in ``lan_only``);
* the house as it actually is today — an empty ledger and AdGuard counting
  nothing — reading as zeros and ``seeing_traffic: false`` rather than as a
  clean bill of health;
* hostile strings: control characters, a 5000-character hostname, a domain
  carrying ``<script>``, spaces, or a right-to-left override are cleaned or
  dropped before they reach a browser;
* the caps, the ``hours`` validation, and that the route is behind the API key.

Nothing here reaches Home Assistant, AdGuard or the LAN: the builder is pure
and the route test injects fakes.

Run: pytest tests/test_home_traffic.py -q
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

from tests.modpatch import patched_modules

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai import home_traffic  # noqa: E402

# api_server is imported as a top-level module by the route fixture below, the
# way tests/test_home_layout.py does it.
_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


# ── a small, honest house ──────────────────────────────────────────────────

HOME_DEVICES = [
    {"device_id": "dev_tv", "name": "Living Room TV", "bridge_key": "AA:BB:CC:00:00:01"},
    {"device_id": "dev_lamp", "name": "Floor Lamp", "bridge_key": "AA:BB:CC:00:00:02"},
    # Zigbee bulb: no address of its own, so no traffic row at all.
    {"device_id": "dev_zigbee", "name": "Hall Bulb", "bridge_key": None},
]

INVENTORY = [
    {"key": "AA:BB:CC:00:00:01", "ip": "192.168.1.20", "display_name": "Living Room TV",
     "category": "media", "manufacturer": "Roku", "hostname": "roku-tv",
     "is_gateway": False, "is_self": False},
    {"key": "AA:BB:CC:00:00:02", "ip": "192.168.1.21", "display_name": "Floor Lamp",
     "category": "light", "manufacturer": "Acme", "hostname": "lamp",
     "is_gateway": False, "is_self": False},
    # Never joined to HA: the printer and the router.
    {"key": "AA:BB:CC:00:00:03", "ip": "192.168.1.30", "display_name": "Printer",
     "category": "printer", "manufacturer": "Canon", "hostname": "canon-mx",
     "is_gateway": False, "is_self": False},
    {"key": "AA:BB:CC:00:00:FE", "ip": "192.168.1.1", "display_name": "Router",
     "category": "network", "manufacturer": "Linksys", "hostname": "myrouter",
     "is_gateway": True, "is_self": False},
]

SUMMARY = {
    "hours": 24,
    "entry_count": 41,
    "device_count": 2,
    "devices": [
        {"key": "AA:BB:CC:00:00:01", "mac": "AA:BB:CC:00:00:01",
         "client_ip": "192.168.1.20", "total_queries": 30, "telemetry_attempts": 12,
         "blocked": 4, "monitored": 6, "allowed": 20,
         "last_seen": "2026-09-19T10:00:00+00:00",
         "top_domains": [
             {"domain": "scribe.logs.roku.com", "count": 12, "category": "TELEMETRY"},
             {"domain": "netflix.com", "count": 8, "category": "other"},
         ]},
        {"key": "AA:BB:CC:00:00:03", "mac": "AA:BB:CC:00:00:03",
         "client_ip": "192.168.1.30", "total_queries": 11, "telemetry_attempts": 1,
         "blocked": 0, "monitored": 0, "allowed": 11,
         "last_seen": "2026-09-19T09:30:00+00:00",
         "top_domains": [{"domain": "cdn.canon.com", "count": 11,
                          "category": "other"}]},
    ],
}

ADGUARD = {"reachable": True, "protection_enabled": True, "queries_today": 4120}
POSTURE = {"enforcing": False, "mode": "observe"}


def build(**kwargs):
    params = dict(hours=24, home_devices=HOME_DEVICES, inventory=INVENTORY,
                  ledger_summary=SUMMARY, adguard=ADGUARD, posture=POSTURE,
                  generated_at="2026-09-19T12:00:00+00:00")
    params.update(kwargs)
    return home_traffic.build_traffic(**params)


def lan_row(body, key):
    for row in body["lan_only"]:
        if row["key"] == key:
            return row
    return None


# ══════════════════════════════════════════════════════════════════════
#  The join
# ══════════════════════════════════════════════════════════════════════

class TestLinking:
    def test_a_home_device_is_keyed_by_its_device_id(self):
        body = build()
        assert set(body["devices"]) == {"dev_tv", "dev_lamp"}

    def test_the_bridge_key_carries_the_ledger_row(self):
        tv = build()["devices"]["dev_tv"]
        assert tv["bridge_key"] == "AA:BB:CC:00:00:01"
        assert (tv["allowed"], tv["blocked"], tv["monitored"], tv["total"]) == (
            20, 4, 6, 30)
        assert tv["ip"] == "192.168.1.20"
        assert tv["last_seen"] == "2026-09-19T10:00:00+00:00"
        assert tv["top_domains"][0]["domain"] == "scribe.logs.roku.com"

    def test_a_linked_device_with_no_ledger_rows_is_zeros_not_missing(self):
        lamp = build()["devices"]["dev_lamp"]
        assert lamp["total"] == 0 and lamp["top_domains"] == []
        assert lamp["ip"] == "192.168.1.21"          # from the inventory
        assert lamp["last_seen"] == ""

    def test_a_device_with_no_bridge_key_has_no_row(self):
        assert "dev_zigbee" not in build()["devices"]

    def test_a_row_recorded_under_the_mac_still_links(self):
        summary = {"entry_count": 2, "devices": [
            {"key": "ip:192.168.1.20", "mac": "AA:BB:CC:00:00:01",
             "client_ip": "192.168.1.20", "total_queries": 2, "allowed": 2,
             "blocked": 0, "monitored": 0, "last_seen": "", "top_domains": []}]}
        assert build(ledger_summary=summary)["devices"]["dev_tv"]["total"] == 2

    def test_row_shape_is_exactly_the_contract(self):
        assert set(build()["devices"]["dev_tv"]) == {
            "bridge_key", "ip", "allowed", "blocked", "monitored", "total",
            "last_seen", "top_domains"}
        assert set(build()["devices"]["dev_tv"]["top_domains"][0]) == {
            "domain", "count", "category"}


class TestLanOnly:
    def test_unlinked_inventory_rows_only(self):
        keys = {row["key"] for row in build()["lan_only"]}
        assert keys == {"AA:BB:CC:00:00:03", "AA:BB:CC:00:00:FE"}

    def test_it_carries_the_inventory_labels_and_its_counts(self):
        printer = lan_row(build(), "AA:BB:CC:00:00:03")
        assert printer["display_name"] == "Printer"
        assert printer["category"] == "printer"
        assert printer["manufacturer"] == "Canon"
        assert printer["hostname"] == "canon-mx"
        assert printer["is_gateway"] is False and printer["is_self"] is False
        assert printer["total"] == 11 and printer["allowed"] == 11

    def test_the_gateway_is_flagged_not_hidden(self):
        assert lan_row(build(), "AA:BB:CC:00:00:FE")["is_gateway"] is True

    def test_busiest_first_and_stable(self):
        order = [row["key"] for row in build()["lan_only"]]
        assert order == ["AA:BB:CC:00:00:03", "AA:BB:CC:00:00:FE"]

    def test_no_mac_field_of_its_own(self):
        assert "mac" not in lan_row(build(), "AA:BB:CC:00:00:03")


# ══════════════════════════════════════════════════════════════════════
#  The house as it is today: nothing recorded, nothing counted
# ══════════════════════════════════════════════════════════════════════

class TestZeroDataHouse:
    def test_no_ledger_no_adguard_is_an_honest_blank(self):
        body = build(ledger_summary={}, adguard=None)
        assert body["success"] is True and body["hours"] == 24
        assert body["visibility"] == {
            "adguard_reachable": False, "queries_today": 0,
            "protection_enabled": False, "enforcing": False,
            "mode": "observe", "seeing_traffic": False}
        assert all(row["total"] == 0 for row in body["devices"].values())
        assert all(row["total"] == 0 for row in body["lan_only"])

    def test_adguard_counting_queries_is_enough_to_be_seeing_traffic(self):
        body = build(ledger_summary={"entry_count": 0, "devices": []})
        assert body["visibility"]["seeing_traffic"] is True

    def test_a_single_ledger_entry_is_enough(self):
        body = build(ledger_summary={"entry_count": 1, "devices": []},
                     adguard={"reachable": True, "queries_today": 0})
        assert body["visibility"]["seeing_traffic"] is True

    def test_an_empty_house_entirely(self):
        body = home_traffic.build_traffic()
        assert body["devices"] == {} and body["lan_only"] == []
        assert body["visibility"]["seeing_traffic"] is False
        assert body["generated_at"].endswith("+00:00")

    def test_enforcing_posture_is_reported(self):
        body = build(posture={"enforcing": True, "mode": "enforce"})
        assert body["visibility"]["enforcing"] is True
        assert body["visibility"]["mode"] == "enforce"


# ══════════════════════════════════════════════════════════════════════
#  Hostile strings — a device names itself
# ══════════════════════════════════════════════════════════════════════

class TestHostileStrings:
    def test_control_characters_are_stripped_from_a_name(self):
        row = [{"key": "AA:BB:CC:00:00:07", "ip": "192.168.1.7",
                "display_name": "Ro\x00ku\x1b[31m TV\r\n",
                "hostname": "ro\x07ku", "category": "media\x00",
                "manufacturer": "‮Roku"}]
        out = home_traffic.build_traffic(inventory=row)["lan_only"][0]
        # The ESC is a control character and the "[31m" it leaves behind is
        # litter, not a name: both go.
        assert out["display_name"] == "Roku TV"
        assert out["hostname"] == "roku"
        assert out["category"] == "media"
        assert out["manufacturer"] == "Roku"
        for value in out.values():
            if isinstance(value, str):
                assert not any(ord(c) < 32 or ord(c) == 127 for c in value)

    def test_a_five_thousand_character_hostname_is_capped(self):
        row = [{"key": "AA:BB:CC:00:00:08", "hostname": "h" * 5000,
                "display_name": "n" * 5000}]
        out = home_traffic.build_traffic(inventory=row)["lan_only"][0]
        assert len(out["hostname"]) == 64 and len(out["display_name"]) == 64

    @pytest.mark.parametrize("domain", [
        "<script>alert(1)</script>",
        "evil.com <script>",
        "two words.com",
        "exa‮mple.com",                 # right-to-left override
        "exa​mple.com",                 # zero-width space
        "example.com/../../etc/passwd",
        "http://example.com",
        "-leading-hyphen.com",
        "trailing-hyphen-.com",
        "a" * 300 + ".com",
        "",
        "..",
        "exam\x00ple.com",
    ])
    def test_a_domain_that_is_not_a_domain_is_dropped(self, domain):
        summary = {"entry_count": 1, "devices": [
            {"key": "AA:BB:CC:00:00:01", "mac": "", "client_ip": "192.168.1.20",
             "total_queries": 1, "allowed": 1, "blocked": 0, "monitored": 0,
             "last_seen": "", "top_domains": [{"domain": domain, "count": 1,
                                               "category": "other"}]}]}
        tv = build(ledger_summary=summary)["devices"]["dev_tv"]
        assert tv["top_domains"] == []
        assert tv["total"] == 1                    # the count still stands

    @pytest.mark.parametrize("raw,expect", [
        ("Example.COM", "example.com"),
        ("example.com.", "example.com"),           # trailing root dot
        ("xn--80ak6aa92e.com", "xn--80ak6aa92e.com"),   # punycode survives
        ("_dns-sd._udp.local", "_dns-sd._udp.local"),
        (" padded.example.org ", "padded.example.org"),
    ])
    def test_a_real_domain_survives(self, raw, expect):
        assert home_traffic.clean_domain(raw) == expect

    def test_an_invisible_character_never_becomes_a_trusted_domain(self):
        # The dangerous outcome is not a crash, it is a page that prints
        # "google.com" for a lookup of something else. A domain carrying any
        # invisible character is dropped whole rather than cleaned into a name
        # nobody asked for.
        assert home_traffic.clean_domain("goo​gle.com") == ""
        assert home_traffic.clean_domain("goo‮gle.com") == ""
        assert home_traffic.clean_domain("goo gle.com") == ""
        assert home_traffic.clean_domain("google.com") == "google.com"

    def test_a_bogus_ip_is_dropped_rather_than_echoed(self):
        row = [{"key": "k1", "ip": "192.168.1.999"},
               {"key": "k2", "ip": "<img src=x>"},
               {"key": "k3", "ip": "192.168.1.5"}]
        out = {r["key"]: r["ip"] for r in
               home_traffic.build_traffic(inventory=row)["lan_only"]}
        assert out == {"k1": "", "k2": "", "k3": "192.168.1.5"}

    def test_a_lying_count_cannot_become_a_negative_or_a_string(self):
        summary = {"entry_count": "many", "devices": [
            {"key": "AA:BB:CC:00:00:01", "total_queries": -5, "allowed": "twenty",
             "blocked": None, "monitored": True, "last_seen": 7,
             "top_domains": "not a list"}]}
        tv = build(ledger_summary=summary)["devices"]["dev_tv"]
        assert tv["total"] == 0 and tv["allowed"] == 0
        assert tv["blocked"] == 0 and tv["monitored"] == 0
        assert tv["top_domains"] == [] and tv["last_seen"] == "7"

    def test_junk_rows_never_raise(self):
        body = home_traffic.build_traffic(
            home_devices=[None, {}, {"device_id": "d", "bridge_key": ""}],
            inventory=[None, "x", {"ip": "10.0.0.1"}],
            ledger_summary={"devices": [None, 3]})
        assert body["devices"] == {} and body["lan_only"] == []


# ══════════════════════════════════════════════════════════════════════
#  Caps
# ══════════════════════════════════════════════════════════════════════

class TestCaps:
    def test_top_domains_is_capped_at_eight(self):
        many = [{"domain": f"d{i}.example.com", "count": 50 - i,
                 "category": "other"} for i in range(40)]
        summary = {"entry_count": 40, "devices": [
            {"key": "AA:BB:CC:00:00:01", "total_queries": 40, "allowed": 40,
             "blocked": 0, "monitored": 0, "last_seen": "",
             "top_domains": many}]}
        tv = build(ledger_summary=summary)["devices"]["dev_tv"]
        assert len(tv["top_domains"]) == home_traffic.MAX_TOP_DOMAINS == 8
        assert tv["top_domains"][0]["domain"] == "d0.example.com"

    def test_lan_only_is_capped_at_256(self):
        rows = [{"key": f"AA:BB:CC:00:{i // 256:02X}:{i % 256:02X}",
                 "ip": "192.168.1.9"} for i in range(900)]
        body = home_traffic.build_traffic(inventory=rows)
        assert len(body["lan_only"]) == home_traffic.MAX_LAN_ONLY_ROWS == 256

    def test_devices_is_capped_at_1024(self):
        devices = [{"device_id": f"d{i}", "bridge_key": f"k{i}"}
                   for i in range(1500)]
        body = home_traffic.build_traffic(home_devices=devices)
        assert len(body["devices"]) == home_traffic.MAX_DEVICE_ROWS == 1024


# ══════════════════════════════════════════════════════════════════════
#  hours
# ══════════════════════════════════════════════════════════════════════

class TestHours:
    @pytest.mark.parametrize("raw,expect", [
        (None, 24), ("", 24), ("1", 1), ("168", 168), ("24", 24), (" 6 ", 6),
    ])
    def test_accepted(self, raw, expect):
        assert home_traffic.parse_hours(raw) == expect

    @pytest.mark.parametrize("raw", [
        "0", "169", "-3", "abc", "1.5", "24; DROP", "1e3", "٤", True,
        "999999999999999999999999",
    ])
    def test_rejected(self, raw):
        assert home_traffic.parse_hours(raw) is None


# ══════════════════════════════════════════════════════════════════════
#  The route
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def api_app():
    """api_server with the firewall mocked — the fixture test_home_layout uses."""
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod.app, mod._API_KEY, mod


@pytest.fixture
def client(api_app):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}


@pytest.fixture
def injected(monkeypatch):
    """Every I/O source of the payload replaced. Nothing reaches the LAN."""
    monkeypatch.setattr(home_traffic, "_home_devices", lambda: HOME_DEVICES)
    monkeypatch.setattr(home_traffic, "_inventory_rows", lambda: INVENTORY)
    monkeypatch.setattr(home_traffic, "_ledger_summary", lambda hours: SUMMARY)
    monkeypatch.setattr(home_traffic, "_adguard_status", lambda: ADGUARD)
    monkeypatch.setattr(home_traffic, "_posture", lambda: POSTURE)


class TestRoute:
    def test_it_requires_the_api_key(self, client):
        c, _ = client
        assert c.get("/ha/home/traffic").status_code == 401
        assert c.get("/ha/home/traffic?hours=24").status_code == 401

    def test_two_hundred_with_injected_sources(self, client, injected):
        c, headers = client
        resp = c.get("/ha/home/traffic", headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True and body["hours"] == 24
        assert body["devices"]["dev_tv"]["total"] == 30
        assert {r["key"] for r in body["lan_only"]} == {
            "AA:BB:CC:00:00:03", "AA:BB:CC:00:00:FE"}
        assert body["visibility"]["seeing_traffic"] is True
        assert set(body) == {"success", "hours", "generated_at", "visibility",
                             "devices", "lan_only"}

    def test_hours_is_honoured_and_passed_to_the_rollup(self, client, injected,
                                                        monkeypatch):
        seen: list[int] = []
        monkeypatch.setattr(home_traffic, "_ledger_summary",
                            lambda hours: seen.append(hours) or SUMMARY)
        c, headers = client
        body = c.get("/ha/home/traffic?hours=6", headers=headers).get_json()
        assert body["hours"] == 6 and seen == [6]

    @pytest.mark.parametrize("raw", ["0", "169", "abc", "-1", "1.5"])
    def test_a_bad_hours_is_a_400(self, client, injected, raw):
        c, headers = client
        resp = c.get(f"/ha/home/traffic?hours={raw}", headers=headers)
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_a_dead_ledger_is_still_a_200(self, client, monkeypatch):
        c, headers = client
        monkeypatch.setattr(home_traffic, "_home_devices", lambda: HOME_DEVICES)
        monkeypatch.setattr(home_traffic, "_inventory_rows", lambda: INVENTORY)
        monkeypatch.setattr(home_traffic, "_posture", lambda: POSTURE)

        def boom(*_args, **_kwargs):
            raise RuntimeError("AdGuard is not deployed")
        monkeypatch.setattr("puffpai.bridge_ledger.get_ledger", boom)
        monkeypatch.setattr(
            "puffpai.zone3.adguard_controller.AdGuardController", boom)
        resp = c.get("/ha/home/traffic", headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["visibility"]["adguard_reachable"] is False
        assert body["visibility"]["seeing_traffic"] is False
        assert body["devices"]["dev_tv"]["total"] == 0

    def test_an_unexpected_failure_is_the_fixed_502_envelope(self, client,
                                                             monkeypatch):
        c, headers = client

        def boom(_hours):
            raise RuntimeError("secret internals 192.168.1.20 netflix.com")
        monkeypatch.setattr(home_traffic, "traffic_payload", boom)
        resp = c.get("/ha/home/traffic", headers=headers)
        assert resp.status_code == 502
        body = resp.get_json()
        assert body == {"success": False, "error": "home traffic unavailable"}

    def test_the_route_is_not_public(self, api_app):
        _app, _key, mod = api_app
        assert "ha_home_traffic" not in mod._PUBLIC_ENDPOINTS


# ══════════════════════════════════════════════════════════════════════
#  Counts a browser can hold
# ══════════════════════════════════════════════════════════════════════

class TestCounts:
    """Python integers have no ceiling; JSON's consumer does. A count that is
    not a number JavaScript can hold arrives as ``Infinity``, which then sorts
    and renders above every real device in the house."""

    def test_an_arbitrary_precision_int_is_clamped_to_the_js_safe_range(self):
        summary = {"entry_count": 10 ** 400, "devices": [
            {"key": "AA:BB:CC:00:00:01", "total_queries": 10 ** 400,
             "allowed": 2 ** 53, "blocked": 0, "monitored": 0,
             "top_domains": [{"domain": "a.example.com", "count": 10 ** 400,
                              "category": "ads"}]}]}
        tv = build(ledger_summary=summary)["devices"]["dev_tv"]
        assert tv["total"] == home_traffic.MAX_COUNT == 2 ** 53 - 1
        assert tv["allowed"] == home_traffic.MAX_COUNT
        assert tv["top_domains"][0]["count"] == home_traffic.MAX_COUNT
        assert len(str(tv["total"])) <= 16

    def test_adguards_own_number_is_clamped_too(self):
        body = build(adguard={"reachable": True, "queries_today": 10 ** 400})
        assert body["visibility"]["queries_today"] == home_traffic.MAX_COUNT
        assert body["visibility"]["seeing_traffic"] is True

    @pytest.mark.parametrize("raw,expect", [
        (0, 0), (7, 7), (-5, 0),
        (2 ** 53 - 1, 2 ** 53 - 1), (2 ** 53, 2 ** 53 - 1), (10 ** 400, 2 ** 53 - 1),
        (True, 0), (False, 0),                    # a bool is not one query
        (3.9, 3), (-0.5, 0), (1e300, 2 ** 53 - 1),
        (float("nan"), 0), (float("inf"), 0), (float("-inf"), 0),
        (None, 0), ([], 0), ({}, 0), (object(), 0),
    ])
    def test_the_count_matrix(self, raw, expect):
        assert home_traffic._count(raw) == expect

    @pytest.mark.parametrize("raw,expect", [
        ("12", 12), ("0", 0), ("9" * 16, 2 ** 53 - 1),
        ("", 0), (" 12", 0), ("12 ", 0), ("+12", 0), ("-12", 0),
        ("1e3", 0), ("12.0", 0), ("1٢", 0), ("١٢", 0),   # Arabic-Indic digits
        ("²", 0),                                        # isdigit() but not ASCII
        ("9" * 17, 0),
    ])
    def test_a_string_count_is_read_only_when_it_is_plain_ascii_digits(
            self, raw, expect):
        assert home_traffic._count(raw) == expect

    def test_a_string_entry_count_still_means_we_are_seeing_traffic(self):
        # The failure this guards: a rollup that starts spelling entry_count as
        # "3" makes the page say "we see nothing" — the one claim on this
        # panel that must never be wrong in the reassuring direction.
        body = build(ledger_summary={"entry_count": "3", "devices": []},
                     adguard={"reachable": True, "queries_today": 0})
        assert body["visibility"]["seeing_traffic"] is True


# ══════════════════════════════════════════════════════════════════════
#  Keys are identities, not labels
# ══════════════════════════════════════════════════════════════════════

class TestBridgeKeys:
    def test_a_key_at_the_cap_survives_whole_and_still_links(self):
        key = "A" * 64
        summary = {"entry_count": 1, "devices": [
            {"key": key, "total_queries": 9, "allowed": 9, "blocked": 0,
             "monitored": 0, "top_domains": []}]}
        body = home_traffic.build_traffic(
            home_devices=[{"device_id": "d1", "bridge_key": key}],
            ledger_summary=summary)
        row = body["devices"]["d1"]
        assert row["bridge_key"] == key and row["total"] == 9

    def test_two_long_keys_do_not_collide_into_one_row(self):
        # Truncating to the display cap would make these the same key, and the
        # page would show one device carrying two devices' traffic. Dropping is
        # the honest failure.
        a = "K" * 64 + "-aaa"
        b = "K" * 64 + "-bbb"
        body = home_traffic.build_traffic(
            home_devices=[{"device_id": "d1", "bridge_key": a},
                          {"device_id": "d2", "bridge_key": b}],
            inventory=[{"key": a, "ip": "192.168.1.30"},
                       {"key": b, "ip": "192.168.1.31"}])
        assert body["devices"] == {}
        assert body["lan_only"] == []

    def test_an_over_long_inventory_key_never_reaches_the_page(self):
        body = home_traffic.build_traffic(
            inventory=[{"key": "z" * 5000, "ip": "192.168.1.32"},
                       {"key": "AA:BB:CC:00:00:09", "ip": "192.168.1.33"}])
        assert [r["key"] for r in body["lan_only"]] == ["AA:BB:CC:00:00:09"]

    def test_a_real_key_is_untouched(self):
        body = build()
        assert body["devices"]["dev_tv"]["bridge_key"] == "AA:BB:CC:00:00:01"


# ══════════════════════════════════════════════════════════════════════
#  Sanitiser edges
# ══════════════════════════════════════════════════════════════════════

class TestSanitiserEdges:
    def test_an_orphaned_ansi_body_is_removed_with_its_escape(self):
        assert home_traffic.clean_text("\x1b[31mRED\x1b[0m") == "RED"
        assert home_traffic.clean_text("TV\x1b[2J\x1b[1;31m!") == "TV!"

    @pytest.mark.parametrize("raw", [
        "Lamp [kitchen]", "Bed [2]", "[31m", "Printer [HP] v2",
    ])
    def test_ordinary_bracketed_text_is_left_alone(self, raw):
        # No ESC in the original, so nothing here is an escape sequence and
        # the household's own naming survives.
        assert home_traffic.clean_text(raw) == raw

    @pytest.mark.parametrize("raw", [123, 0, 1.5, None, True, False, [], {},
                                     b"example.com"])
    def test_a_non_string_is_never_coerced_into_a_domain(self, raw):
        assert home_traffic.clean_domain(raw) == ""

    def test_hours_empty_and_padded_are_deliberately_the_default(self):
        # Documented on purpose (owner-accepted): a missing window is 24 hours,
        # and surrounding whitespace is tolerated. Only a *wrong* number is
        # rejected, because that would draw the wrong picture under a heading
        # saying otherwise.
        assert home_traffic.parse_hours("") == 24
        assert home_traffic.parse_hours(" 24") == 24
        assert home_traffic.parse_hours(None) == 24
        assert home_traffic.parse_hours("4") == 4
