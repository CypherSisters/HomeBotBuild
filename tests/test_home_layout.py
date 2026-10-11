"""The floorplan store: puffpai.home_layout and the /ha/home/layout routes.

This file is the only writer in the Home Blueprint feature, and its input is an
HTTP PUT, so the validator is the security surface: an unbounded ``placements``
map, a megabyte room name or an unknown key are all either a disk-filling
primitive or a dashboard that has drifted from the contract. Every reject class
in the spec gets a test.

The store never touches Home Assistant — the everyday HA user is non-admin by
design and the geometry is PuffPAI's own — so nothing here needs a hub, and the
path is redirected into a tmp dir for the duration.

Run: pytest tests/test_home_layout.py -q
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

from tests.modpatch import patched_modules

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai import home_layout  # noqa: E402

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


GOOD = {
    "version": 1,
    "floors": [{"floor_id": "ground", "name": "Ground", "level": 0}],
    "rooms": [{"area_id": "living_room", "name": "Living Room",
               "floor_id": "ground", "x": 0, "y": 0, "w": 8, "h": 6,
               "custom": False}],
    "placements": {"entity:light.hall_strip": {"area_id": "living_room",
                                               "x": 0.4, "y": 0.6}},
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store at a tmp file. Never the real state dir."""
    path = tmp_path / "state" / "config" / "home_layout.json"
    monkeypatch.setattr(home_layout, "_LAYOUT_PATH", str(path))
    return path


def with_key(payload, key, value):
    copy = json.loads(json.dumps(payload))
    copy[key] = value
    return copy


# ══════════════════════════════════════════════════════════════════════
#  Validator — accept
# ══════════════════════════════════════════════════════════════════════

class TestValidateAccepts:
    def test_the_contract_example(self):
        assert home_layout.validate(GOOD) == (True, "")

    def test_a_bare_minimum_layout(self):
        assert home_layout.validate({"version": 1})[0] is True

    def test_what_a_get_returned_can_be_put_back(self):
        """The dashboard round-trips the GET body; ``seeded`` must not 400."""
        body = dict(GOOD, seeded=True, updated_at="2026-09-18T00:00:00+00:00")
        assert home_layout.validate(body) == (True, "")

    def test_a_custom_room_with_no_floor(self):
        body = with_key(GOOD, "rooms", [{"area_id": "puff_workshop",
                                         "name": "Workshop", "floor_id": None,
                                         "x": 1, "y": 2, "w": 3, "h": 3,
                                         "custom": True}])
        assert home_layout.validate(body) == (True, "")


# ══════════════════════════════════════════════════════════════════════
#  Validator — every reject class in the spec
# ══════════════════════════════════════════════════════════════════════

class TestValidateRejects:
    def _refused(self, body):
        ok, reason = home_layout.validate(body)
        assert ok is False and reason
        return reason

    def test_not_an_object(self):
        self._refused([GOOD])

    def test_wrong_version(self):
        assert "version" in self._refused(with_key(GOOD, "version", 2))
        assert "version" in self._refused(with_key(GOOD, "version", True))

    def test_unknown_top_level_key(self):
        assert "unknown key" in self._refused(with_key(GOOD, "wallpaper", "x"))

    def test_unknown_nested_keys(self):
        assert "unknown room key" in self._refused(
            with_key(GOOD, "rooms", [dict(GOOD["rooms"][0], colour="red")]))
        assert "unknown floor key" in self._refused(
            with_key(GOOD, "floors", [dict(GOOD["floors"][0], height=3)]))
        assert "unknown placement key" in self._refused(
            with_key(GOOD, "placements", {"dev_a": {"area_id": "living_room",
                                                    "z": 1}}))

    def test_too_many_rooms(self):
        rooms = [{"area_id": f"room_{i}", "name": "R", "x": 0, "y": 0,
                  "w": 1, "h": 1} for i in range(65)]
        assert "64 rooms" in self._refused(with_key(GOOD, "rooms", rooms))

    def test_too_many_placements(self):
        placements = {f"dev_{i}": {"area_id": "living_room"} for i in range(1025)}
        assert "1024 placements" in self._refused(
            with_key(GOOD, "placements", placements))

    def test_too_many_floors(self):
        floors = [{"floor_id": f"f{i}", "name": "F", "level": 0} for i in range(9)]
        assert "8 floors" in self._refused(with_key(GOOD, "floors", floors))

    def test_bad_ids(self):
        assert "area_id" in self._refused(
            with_key(GOOD, "rooms", [dict(GOOD["rooms"][0], area_id="Living Room")]))
        assert "device_id" in self._refused(
            with_key(GOOD, "placements", {"A" * 129: {"area_id": "living_room"}}))
        assert "area_id" in self._refused(
            with_key(GOOD, "placements", {"dev_a": {"area_id": ""}}))

    def test_duplicate_ids(self):
        assert "duplicate" in self._refused(
            with_key(GOOD, "rooms", GOOD["rooms"] * 2))

    def test_long_and_control_character_names(self):
        assert "64 characters" in self._refused(
            with_key(GOOD, "rooms", [dict(GOOD["rooms"][0], name="x" * 65)]))
        assert "control characters" in self._refused(
            with_key(GOOD, "rooms", [dict(GOOD["rooms"][0], name="Living\nRoom")]))

    def test_geometry_out_of_range(self):
        for key, value in (("x", -1), ("y", 201), ("w", 0), ("h", 1.5)):
            reason = self._refused(
                with_key(GOOD, "rooms", [dict(GOOD["rooms"][0], **{key: value})]))
            assert key in reason

    def test_placement_coordinates_are_fractions(self):
        assert "out of range" in self._refused(
            with_key(GOOD, "placements", {"dev_a": {"area_id": "living_room",
                                                    "x": 1.5}}))
        assert "must be a number" in self._refused(
            with_key(GOOD, "placements", {"dev_a": {"area_id": "living_room",
                                                    "y": "0.5"}}))

    def test_a_number_too_big_to_be_a_float_is_a_reject_not_a_crash(self):
        """``float(10**400)`` raises OverflowError — JSON ints have no width.

        This body used to reach ``float(value)`` inside the range check and
        take the whole PUT route out as a 500. Every numeric field gets the
        pair, positive and negative.
        """
        for value in (10 ** 400, -(10 ** 400)):
            for key in ("x", "y"):
                reason = self._refused(with_key(GOOD, "placements", {
                    "dev_a": {"area_id": "living_room", key: value}}))
                assert "out of range" in reason
            for key in ("x", "y", "w", "h"):
                reason = self._refused(with_key(GOOD, "rooms", [
                    dict(GOOD["rooms"][0], **{key: value})]))
                assert key in reason

    def test_nan_and_infinity_are_not_coordinates(self):
        # NaN fails every ordering comparison silently, so a bare
        # ``0.0 <= x <= 1.0`` is not a check at all against it.
        for value in (float("nan"), float("inf"), float("-inf")):
            body = dict(GOOD, placements={"dev_a": {"area_id": "living_room",
                                                    "x": value}})
            assert "out of range" in self._refused(body)

    def test_seeded_must_be_a_boolean(self):
        assert "seeded" in self._refused(with_key(GOOD, "seeded", "yes"))
        assert "seeded" in self._refused(with_key(GOOD, "seeded", 1))

    def test_updated_at_must_be_a_short_plain_string(self):
        for value in (123, "x" * 65, "2026-09-18\n\rok"):
            assert "updated_at" in self._refused(
                with_key(GOOD, "updated_at", value))

    def test_validate_is_total_even_when_something_unforeseen_raises(self,
                                                                    monkeypatch):
        """The wrapper, exercised: an unexpected raise is a 400, not a 500."""
        def boom(*_args, **_kwargs):
            raise RuntimeError("unforeseen")
        monkeypatch.setattr(home_layout, "_validate", boom)
        assert home_layout.validate(GOOD) == (False, "invalid layout")

    def test_oversize_payload(self):
        # Every per-item limit is satisfied — 1024 placements, ids inside 128
        # characters — and the total is still a quarter of a megabyte. The size
        # cap is the backstop that makes the other caps enough.
        big = with_key(GOOD, "placements", {
            f"dev_{i}".ljust(128, "x"): {"area_id": "a".ljust(128, "b"),
                                         "x": 0.123456, "y": 0.654321}
            for i in range(1024)})
        assert "bytes" in self._refused(big)

    def test_wrong_container_types(self):
        assert self._refused(with_key(GOOD, "rooms", {}))
        assert self._refused(with_key(GOOD, "floors", {}))
        assert self._refused(with_key(GOOD, "placements", []))


# ══════════════════════════════════════════════════════════════════════
#  Store
# ══════════════════════════════════════════════════════════════════════

class TestStore:
    def test_missing_file_is_the_empty_layout(self, store):
        assert home_layout.load() == {"version": 1, "floors": [], "rooms": [],
                                      "placements": {}, "seeded": False}

    def test_round_trip(self, store):
        stored = home_layout.save(GOOD)
        assert stored["seeded"] is True and stored["updated_at"]
        loaded = home_layout.load()
        assert loaded["rooms"] == GOOD["rooms"]
        assert loaded["placements"] == GOOD["placements"]
        assert loaded["seeded"] is True
        assert json.loads(store.read_text(encoding="utf-8"))["version"] == 1

    def test_write_is_atomic_and_leaves_no_temp_behind(self, store):
        home_layout.save(GOOD)
        assert store.exists()
        assert not os.path.exists(str(store) + ".tmp")

    def test_a_corrupt_file_reads_as_empty_rather_than_raising(self, store):
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{not json", encoding="utf-8")
        assert home_layout.load()["seeded"] is False

    def test_seeded_is_never_stored(self, store):
        home_layout.save(dict(GOOD, seeded=True))
        assert "seeded" not in json.loads(store.read_text(encoding="utf-8"))

    def test_path_follows_the_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(home_layout, "_LAYOUT_PATH", None)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        path = home_layout.layout_path()
        assert path.endswith(os.path.join("config", "home_layout.json"))
        assert str(tmp_path) in path


# ══════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def api_app():
    """api_server with the firewall mocked — the fixture test_api_server uses."""
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
def client(api_app, store):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}


class TestLayoutRoutes:
    def test_get_requires_the_api_key(self, client):
        c, _ = client
        assert c.get("/ha/home/layout").status_code == 401

    def test_get_with_no_file_is_the_empty_layout(self, client):
        c, headers = client
        body = c.get("/ha/home/layout", headers=headers).get_json()
        assert body["success"] is True and body["seeded"] is False
        assert body["rooms"] == [] and body["placements"] == {}

    def test_put_then_get_round_trips(self, client, store):
        c, headers = client
        resp = c.put("/ha/home/layout", json=GOOD, headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["rooms"] == GOOD["rooms"]
        body = c.get("/ha/home/layout", headers=headers).get_json()
        assert body["seeded"] is True
        assert body["placements"] == GOOD["placements"]

    def test_put_rejects_an_invalid_layout(self, client, store):
        c, headers = client
        resp = c.put("/ha/home/layout", json=with_key(GOOD, "version", 7),
                     headers=headers)
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False and "version" in body["error"]
        assert not store.exists()                  # nothing was written

    def test_put_rejects_a_non_json_body(self, client):
        c, headers = client
        resp = c.put("/ha/home/layout", data="version=1", headers=headers,
                     content_type="text/plain")
        assert resp.status_code == 400
        assert "json" in resp.get_json()["error"].lower()

    def test_put_rejects_an_oversize_body(self, client):
        c, headers = client
        resp = c.put("/ha/home/layout", data=b"x" * (home_layout.MAX_BYTES + 1),
                     headers=headers, content_type="application/json")
        assert resp.status_code == 400
        assert "bytes" in resp.get_json()["error"]

    def test_home_route_is_keyed_and_answers_from_the_registry(self, client,
                                                               monkeypatch):
        """``/ha/home`` smoke test: keyed, offline-safe, layout applied."""
        from puffpai import bridge_ha, home_topology
        c, headers = client
        assert c.get("/ha/home").status_code == 401

        bridge_ha.set_registries(
            [{"id": "dev_plug", "name": "Desk Plug"}],
            [{"entity_id": "switch.test_desk_plug", "device_id": "dev_plug",
              "platform": "tuya"}],
            [{"area_id": "living_room", "name": "Living Room"}],
            [{"floor_id": "ground", "name": "Ground", "level": 0}])
        monkeypatch.setattr(home_topology, "_maybe_pull", lambda: None)
        monkeypatch.setattr(home_topology, "_inventory_links", dict)
        # No hub and no LAN in a test run: states are unavailable, the route
        # answers anyway, and nothing here opens a socket.
        monkeypatch.setattr(
            "puffpai.ha_bridge.get_bridge",
            lambda: (_ for _ in ()).throw(RuntimeError("no hub in tests")))
        home_layout.save(with_key(GOOD, "placements",
                                  {"dev_plug": {"area_id": "living_room"}}))
        try:
            body = c.get("/ha/home", headers=headers).get_json()
        finally:
            bridge_ha.reset()
        assert body["success"] is True
        assert body["registry"]["ok"] is True
        assert body["areas"][0]["name"] == "Living Room"
        assert body["floors"][0]["floor_id"] == "ground"
        device = body["devices"][0]
        assert device["device_id"] == "dev_plug"
        assert (device["area_id"], device["area_source"]) == ("living_room",
                                                              "layout")
        assert device["tier"] == "cloud"

    def test_put_answers_400_for_an_unfloatable_number(self, client, store):
        """The route's half of the OverflowError: a reason, not a 500."""
        c, headers = client
        resp = c.put("/ha/home/layout", headers=headers,
                     content_type="application/json",
                     data=json.dumps(with_key(GOOD, "placements", {
                         "dev_a": {"area_id": "living_room", "x": 10 ** 400}})))
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False and body["error"]
        assert not store.exists()

    def test_put_rejects_malformed_json(self, client):
        c, headers = client
        resp = c.put("/ha/home/layout", data=b"{nope", headers=headers,
                     content_type="application/json")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid JSON"
