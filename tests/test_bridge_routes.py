"""Tests for puffpai.bridge_routes — the /bridge Blueprint.

The blueprint is mounted on a bare Flask app here so the routes can be
exercised without importing the whole api_server. One test additionally pins
the property that matters for security: an app-level ``before_request`` guard
runs for blueprint routes, which is the ONLY thing standing between /bridge/*
and an unauthenticated caller (api_server.py:905-906).
"""

import importlib
import os
import sys
from unittest.mock import MagicMock

import pytest
from flask import Flask, jsonify

from puffpai import bridge_routes
from tests.modpatch import patched_modules

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(bridge_routes.bridge_bp)
    return app.test_client()


class _FakeInventory:
    def __init__(self):
        self.refreshed = 0

    def devices(self):
        return [{"key": "AA:BB:CC:00:00:01", "mac": "AA:BB:CC:00:00:01",
                 "ip": "192.168.1.20", "display_name": "Living Room TV"}]

    def refresh(self):
        self.refreshed += 1
        return self.devices()

    def get(self, key):
        return self.devices()[0] if key in (
            "AA:BB:CC:00:00:01", "192.168.1.20") else None

    def status(self):
        return {"last_refresh": "2026-09-09T10:00:00+00:00",
                "inventory_age_seconds": 42, "device_count": 1,
                "new_24h": 0, "sources": {"arp": {"ok": True, "count": 1}},
                "refresher_running": True}


class _FakeLedger:
    def __init__(self):
        self.pulled = 0

    def pull(self):
        self.pulled += 1
        return {"pulled": 3, "written": 2, "zone3": True, "enforce": False}

    def summary(self, hours=24):
        return {"hours": hours, "entry_count": 4, "device_count": 1,
                "devices": [{"key": "AA:BB:CC:00:00:01", "total_queries": 4,
                             "telemetry_attempts": 3, "top_domains": []}]}

    def device_summary(self, mac, hours=24):
        return {"hours": hours, "key": mac, "mac": mac, "client_ip": "",
                "total_queries": 4, "telemetry_attempts": 3, "blocked": 1,
                "monitored": 2, "allowed": 1, "last_seen": "",
                "top_domains": [{"domain": "samsungacr.com", "count": 2,
                                 "category": "TELEMETRY"}]}

    def status(self):
        return {"ledger_path": "/data/bridge_telemetry.jsonl",
                "ledger_bytes": 1024, "last_pull": "", "last_pull_rows": 0,
                "zone3_enabled": True}


@pytest.fixture
def fakes(monkeypatch):
    import puffpai.bridge_inventory as bi
    import puffpai.bridge_ledger as bl
    inventory, ledger = _FakeInventory(), _FakeLedger()
    monkeypatch.setattr(bi, "get_inventory", lambda: inventory)
    monkeypatch.setattr(bl, "get_ledger", lambda: ledger)
    return inventory, ledger


# ── /bridge/devices ─────────────────────────────────────────────────────────

def test_devices_returns_roll_and_status(client, fakes):
    resp = client.get("/bridge/devices")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["count"] == 1
    assert body["devices"][0]["display_name"] == "Living Room TV"
    assert body["inventory_age_seconds"] == 42


def test_devices_does_not_scan(client, fakes):
    inventory, _ = fakes
    client.get("/bridge/devices")
    assert inventory.refreshed == 0


def test_devices_reports_failure_as_500(client, monkeypatch):
    import puffpai.bridge_inventory as bi

    def boom():
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(bi, "get_inventory", boom)
    resp = client.get("/bridge/devices")
    assert resp.status_code == 500
    assert resp.get_json()["success"] is False


def test_devices_failure_leaks_no_exception_text(client, monkeypatch):
    import puffpai.bridge_inventory as bi

    def boom():
        raise RuntimeError("SECRET-MARKER")

    monkeypatch.setattr(bi, "get_inventory", boom)
    resp = client.get("/bridge/devices")
    body = resp.get_json()
    assert "SECRET-MARKER" not in resp.get_data(as_text=True)
    assert body["success"] is False
    assert "error" in body and "code" in body


# ── /bridge/refresh ─────────────────────────────────────────────────────────

def test_refresh_scans_then_pulls(client, fakes):
    inventory, ledger = fakes
    resp = client.post("/bridge/refresh")
    assert resp.status_code == 200
    body = resp.get_json()
    assert inventory.refreshed == 1
    assert ledger.pulled == 1
    assert body["count"] == 1
    assert body["telemetry"]["written"] == 2


def test_refresh_is_post_only(client, fakes):
    assert client.get("/bridge/refresh").status_code == 405


def test_refresh_survives_a_ledger_failure(client, monkeypatch, fakes):
    import puffpai.bridge_ledger as bl

    def boom():
        raise RuntimeError("SECRET-MARKER")

    monkeypatch.setattr(bl, "get_ledger", boom)
    resp = client.post("/bridge/refresh")
    # The device roll is the part that works without AdGuard deployed; a dead
    # sinkhole must not take the whole panel down.
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["count"] == 1
    assert body["telemetry"]["zone3"] is False
    assert "SECRET-MARKER" not in resp.get_data(as_text=True)
    assert body["telemetry"]["error"] == "telemetry pull failed"


# ── /bridge/telemetry ───────────────────────────────────────────────────────

def test_telemetry_summary_default_window(client, fakes):
    body = client.get("/bridge/telemetry/summary").get_json()
    assert body["success"] is True
    assert body["hours"] == 24
    assert body["devices"][0]["telemetry_attempts"] == 3


@pytest.mark.parametrize("query,expected", [
    ("?hours=1", 1),
    ("?hours=48", 48),
    ("?hours=168", 168),
    ("?hours=99999", 168),      # clamped up-bound
    ("?hours=0", 1),            # clamped low-bound
    ("?hours=-5", 1),
    ("?hours=abc", 24),         # unparseable reads as the default
    ("", 24),
])
def test_hours_is_clamped(client, fakes, query, expected):
    body = client.get(f"/bridge/telemetry/summary{query}").get_json()
    assert body["hours"] == expected


def test_telemetry_device_includes_inventory_record(client, fakes):
    body = client.get(
        "/bridge/telemetry/device/AA:BB:CC:00:00:01?hours=48").get_json()
    assert body["success"] is True
    assert body["hours"] == 48
    assert body["device"]["display_name"] == "Living Room TV"
    assert body["top_domains"][0]["category"] == "TELEMETRY"


def test_telemetry_device_unknown_still_answers(client, fakes):
    body = client.get("/bridge/telemetry/device/FF:FF:FF:00:00:00").get_json()
    assert body["success"] is True
    assert body["device"] is None       # not in the inventory
    assert "total_queries" in body


def test_telemetry_device_accepts_an_ip(client, fakes):
    body = client.get("/bridge/telemetry/device/192.168.1.20").get_json()
    assert body["device"]["ip"] == "192.168.1.20"


def test_telemetry_summary_failure_leaks_no_exception_text(client, monkeypatch):
    import puffpai.bridge_ledger as bl

    def boom():
        raise RuntimeError("SECRET-MARKER")

    monkeypatch.setattr(bl, "get_ledger", boom)
    resp = client.get("/bridge/telemetry/summary")
    assert resp.status_code == 500
    assert "SECRET-MARKER" not in resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is False
    assert "error" in body and "code" in body


def test_telemetry_device_failure_leaks_no_exception_text(client, monkeypatch):
    import puffpai.bridge_ledger as bl

    def boom(*a, **kw):
        raise RuntimeError("SECRET-MARKER")

    class _BoomLedger:
        def device_summary(self, mac, hours=24):
            raise RuntimeError("SECRET-MARKER")

    monkeypatch.setattr(bl, "get_ledger", lambda: _BoomLedger())
    resp = client.get("/bridge/telemetry/device/AA:BB:CC:00:00:01")
    assert resp.status_code == 500
    assert "SECRET-MARKER" not in resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is False
    assert "error" in body and "code" in body


# ── /bridge/ha ──────────────────────────────────────────────────────────────

@pytest.fixture
def ha_registries():
    """Seed the bucket view's registry cache, with no hub traffic."""
    from puffpai import bridge_ha
    bridge_ha.reset()
    bridge_ha.set_registries(
        [{"id": "d1", "name": "Plug", "area_id": "kitchen",
          "identifiers": [["tuya", "s1"]]},
         {"id": "d2", "name": "Lamp", "area_id": "kitchen",
          "identifiers": [["zha", "s2"]]}],
        [{"entity_id": "switch.plug", "device_id": "d1", "platform": "tuya"},
         {"entity_id": "light.lamp", "device_id": "d2", "platform": "zha"}],
        [{"area_id": "kitchen", "name": "Kitchen"}],
    )
    yield bridge_ha
    bridge_ha.reset()


def test_ha_buckets_lists_the_managed_view(client, fakes, ha_registries,
                                           monkeypatch):
    monkeypatch.setattr(ha_registries, "ha_host", lambda: "")
    body = client.get("/bridge/ha/buckets").get_json()
    assert body["success"] is True
    assert body["hours"] == 24
    assert [b["id"] for b in body["buckets"]] == ["cloud:tuya", "local:zha"]
    assert body["counts"]["devices"] == 2
    assert body["counts"]["cloud_buckets"] == 1


def test_ha_bucket_returns_one_bucket_with_its_devices(client, fakes,
                                                       ha_registries,
                                                       monkeypatch):
    monkeypatch.setattr(ha_registries, "ha_host", lambda: "")
    body = client.get("/bridge/ha/bucket/local:zha").get_json()
    assert body["success"] is True
    assert body["bucket"]["id"] == "local:zha"
    assert [d["name"] for d in body["bucket"]["devices"]] == ["Lamp"]


@pytest.mark.parametrize("bad", [
    "nope", "hybrid:tuya", "cloud:", "CLOUD:tuya", "cloud:tu ya",
    "cloud:tuya/../../etc/passwd",
])
def test_a_malformed_bucket_id_is_a_404(client, fakes, ha_registries, bad):
    resp = client.get(f"/bridge/ha/bucket/{bad}")
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


def test_a_wellformed_id_with_no_bucket_is_also_a_404(client, fakes,
                                                      ha_registries,
                                                      monkeypatch):
    monkeypatch.setattr(ha_registries, "ha_host", lambda: "")
    assert client.get("/bridge/ha/bucket/cloud:ring").status_code == 404


def test_ha_routes_never_leak_the_exception_text(client, fakes, monkeypatch):
    from puffpai import bridge_ha

    def boom(*_a, **_k):
        raise RuntimeError("SECRET-MARKER")

    monkeypatch.setattr(bridge_ha, "buckets", boom)
    monkeypatch.setattr(bridge_ha, "bucket", boom)
    for path in ("/bridge/ha/buckets", "/bridge/ha/bucket/cloud:tuya"):
        resp = client.get(path)
        assert resp.status_code == 500
        assert "SECRET-MARKER" not in resp.get_data(as_text=True)


def test_status_carries_the_bucket_counts(client, fakes, ha_registries,
                                          monkeypatch):
    monkeypatch.setattr(ha_registries, "ha_host", lambda: "")
    body = client.get("/bridge/status").get_json()
    assert body["ha_buckets"]["buckets"] == 2
    assert body["ha_buckets"]["devices"] == 2


# ── /bridge/status ──────────────────────────────────────────────────────────

def test_status_reports_observe_mode(client, fakes, monkeypatch):
    class _Zone3:
        enabled = True
        adguard_enforce = False

    class _Cfg:
        zone3 = _Zone3()

    import puffpai.config as config
    import puffpai.bridge_inventory as bi
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    monkeypatch.setattr(bi, "is_enabled", lambda: True)

    body = client.get("/bridge/status").get_json()
    assert body["success"] is True
    assert body["observe_only"] is True
    assert body["zone3_enabled"] is True
    assert body["enforce"] is False
    assert body["mode"] == "observe"
    assert body["inventory"]["device_count"] == 1
    assert body["inventory_enabled"] is True
    assert body["ledger"]["zone3_enabled"] is True
    # No absolute on-disk path leaves the box: basename only.
    assert "ledger_path" not in body["ledger"]
    assert body["ledger"]["ledger_file"] == "bridge_telemetry.jsonl"
    assert body["ledger"]["ledger_bytes"] == 1024


def test_status_reports_enforce_mode(client, fakes, monkeypatch):
    class _Zone3:
        enabled = True
        adguard_enforce = True

    class _Cfg:
        zone3 = _Zone3()

    import puffpai.config as config
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    assert client.get("/bridge/status").get_json()["mode"] == "enforce"


def test_status_survives_a_missing_zone3_config(client, fakes, monkeypatch):
    class _Cfg:
        pass

    import puffpai.config as config
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    body = client.get("/bridge/status").get_json()
    assert body["zone3_enabled"] is False
    assert body["mode"] == "observe"


def test_status_never_claims_enforce_while_zone3_is_off(client, fakes,
                                                        monkeypatch):
    class _Zone3:
        enabled = False
        adguard_enforce = True      # a stale flag on a disabled subsystem

    class _Cfg:
        zone3 = _Zone3()

    import puffpai.config as config
    monkeypatch.setattr(config, "get_config", lambda: _Cfg())
    assert client.get("/bridge/status").get_json()["mode"] == "observe"


def test_status_degrades_when_everything_is_broken(client, monkeypatch):
    import puffpai.bridge_inventory as bi
    import puffpai.bridge_ledger as bl

    def boom():
        raise RuntimeError("SECRET-MARKER")

    monkeypatch.setattr(bi, "get_inventory", boom)
    monkeypatch.setattr(bl, "get_ledger", boom)
    resp = client.get("/bridge/status")
    assert "SECRET-MARKER" not in resp.get_data(as_text=True)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "error" in body["inventory"]
    assert "error" in body["ledger"]


# ── registration + auth coverage ────────────────────────────────────────────

def test_register_mounts_routes_without_starting_the_refresher(monkeypatch):
    import puffpai.bridge_inventory as bi
    started = []

    class _Inv:
        def start_refresher(self, interval):
            started.append(interval)
            return True

    monkeypatch.setattr(bi, "is_enabled", lambda: False)
    monkeypatch.setattr(bi, "get_inventory", lambda: _Inv())

    app = Flask(__name__)
    bridge_routes.register(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/bridge/devices" in rules
    assert "/bridge/status" in rules
    assert started == []          # routes mounted, scanner idle


def test_register_starts_the_refresher_when_enabled(monkeypatch):
    import puffpai.bridge_inventory as bi
    started = []

    class _Inv:
        def start_refresher(self, interval):
            started.append(interval)
            return True

    monkeypatch.setattr(bi, "is_enabled", lambda: True)
    monkeypatch.setattr(bi, "get_inventory", lambda: _Inv())

    bridge_routes.register(Flask(__name__))
    assert started == [bi.DEFAULT_REFRESH_SECONDS]


@pytest.mark.parametrize("path,method", [
    ("/bridge/devices", "get"),
    ("/bridge/refresh", "post"),
    ("/bridge/telemetry/summary", "get"),
    ("/bridge/telemetry/device/AA:BB:CC:00:00:01", "get"),
    ("/bridge/status", "get"),
    ("/bridge/ha/buckets", "get"),
    ("/bridge/ha/bucket/cloud:tuya", "get"),
])
def test_every_route_is_covered_by_an_app_level_guard(fakes, path, method):
    """The whole point: api_server's before_request API-key guard reaches here.

    api_server gates the app, not each route (api_server.py:905-906). If Flask
    ever stopped running app-level hooks for blueprint routes, /bridge/* would
    silently become unauthenticated — so pin the behaviour rather than trusting it.
    """
    app = Flask(__name__)
    app.register_blueprint(bridge_routes.bridge_bp)

    def deny():
        return jsonify({"error": "Unauthorized"}), 401

    app.before_request(deny)
    resp = getattr(app.test_client(), method)(path)
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Unauthorized"


# ── real wiring ──────────────────────────────────────────────────────────────

def test_mounted_on_real_api_server():
    """The /bridge/* blueprint is actually mounted on the real app, gated.

    Every other test here exercises the blueprint on a bare Flask app; that
    proves the blueprint's own behaviour but not that ``api_server`` actually
    registers it, nor that nobody later added a route here to
    ``_PUBLIC_ENDPOINTS`` or ``endpoint_gate.LOCAL_ONLY_ENDPOINTS`` (either of
    which would silently drop the API-key gate for real callers). This
    follows ``tests/test_api_server.py``'s own env/mock setup so the import
    succeeds the same way theirs does.
    """
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}

    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")

        app = mod.app
        rules = {r.rule: r.endpoint for r in app.url_map.iter_rules()}

        expected = [
            "/bridge/devices",
            "/bridge/refresh",
            "/bridge/status",
            "/bridge/telemetry/summary",
            "/bridge/telemetry/device/<path:mac>",
            "/bridge/ha/buckets",
            "/bridge/ha/bucket/<path:bucket_id>",
        ]
        for rule in expected:
            assert rule in rules, f"{rule} is not mounted on the real app"

        bridge_rules = {rule for rule in rules if rule.startswith("/bridge")}
        assert bridge_rules == set(expected), (
            "unexpected /bridge* rule(s) mounted: "
            f"{bridge_rules - set(expected)}; missing: {set(expected) - bridge_rules}")

        from puffpai.security import endpoint_gate

        for rule in expected:
            endpoint = rules[rule]
            assert endpoint not in mod._PUBLIC_ENDPOINTS, (
                f"{rule} ({endpoint}) must not be in api_server._PUBLIC_ENDPOINTS")
            assert endpoint not in endpoint_gate.LOCAL_ONLY_ENDPOINTS, (
                f"{rule} ({endpoint}) must not be in "
                "endpoint_gate.LOCAL_ONLY_ENDPOINTS")
