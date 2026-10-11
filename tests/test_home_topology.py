"""The room resolver: puffpai.home_topology and the ha_bridge repair.

``GET /ha/rooms`` reported ONE room — "Unassigned" — holding all 182 devices,
because ``_get_area_map`` asked REST for a WebSocket-only command and
``_parse_device`` read an ``attributes.area_id`` Home Assistant never sets.
These tests pin the replacement: the registries decide, entity beats device,
the owner's layout is the last word before "nowhere", and a hub that is down
degrades to a device list rather than an exception.

Every registry record here is fabricated and nothing in this file may reach a
real Home Assistant: the bridge used is a real ``HABridge`` subclass whose
``_request`` is a recorder, and the registry cache is filled by hand.

Run: pytest tests/test_home_topology.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai import bridge_ha, home_topology  # noqa: E402
from puffpai.ha_bridge import HABridge  # noqa: E402


# ── fabricated registries ──────────────────────────────────────────────────

AREAS = [
    {"area_id": "living_room", "name": "Living Room", "floor_id": "ground"},
    {"area_id": "main_bedroom", "name": "Main Bedroom", "floor_id": "upstairs"},
]

FLOORS = [
    {"floor_id": "ground", "name": "Ground", "level": 0},
    {"floor_id": "upstairs", "name": "Upstairs", "level": 1},
]

DEVICES = [
    # Device area says bedroom; its light entity overrides it to living room.
    {"id": "dev_lamp", "name": "Floor Lamp", "area_id": "main_bedroom",
     "manufacturer": "Acme", "model": "L1"},
    # No area anywhere — the layout places it.
    {"id": "dev_plug", "name": "Desk Plug", "manufacturer": "Acme", "model": "P1"},
    # HA's own furniture.
    {"id": "dev_sun", "name": "Sun", "identifiers": [["sun", "sun"]]},
    # A door lock: safety-critical, and its area comes from the device row.
    {"id": "dev_lock", "name": "Front Door", "area_id": "living_room"},
]

ENTITIES = [
    {"entity_id": "light.test_floor_lamp", "device_id": "dev_lamp",
     "platform": "zha", "area_id": "living_room", "name": "Floor Lamp"},
    {"entity_id": "sensor.test_lamp_power", "device_id": "dev_lamp",
     "platform": "zha"},
    {"entity_id": "switch.test_lamp_child_lock", "device_id": "dev_lamp",
     "platform": "zha", "disabled_by": "integration"},
    {"entity_id": "switch.test_desk_plug", "device_id": "dev_plug",
     "platform": "tuya"},
    {"entity_id": "sun.sun", "device_id": "dev_sun", "platform": "sun"},
    {"entity_id": "lock.test_front_door", "device_id": "dev_lock",
     "platform": "zwave_js"},
    # Device-less entity (a helper/template the owner made).
    {"entity_id": "light.test_hall_strip", "device_id": None,
     "platform": "wled", "area_id": "living_room"},
]

STATES = [
    {"entity_id": "light.test_floor_lamp", "state": "on",
     "attributes": {"friendly_name": "Floor Lamp", "brightness": 180,
                    "entity_picture": "/secret/token.png",
                    "access_token": "must-not-leak",
                    "supported_features": 44}},
    {"entity_id": "sensor.test_lamp_power", "state": "12.5",
     "attributes": {"unit_of_measurement": "W", "device_class": "power"}},
    {"entity_id": "switch.test_desk_plug", "state": "off", "attributes": {}},
    {"entity_id": "lock.test_front_door", "state": "locked", "attributes": {}},
    {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}},
    {"entity_id": "light.test_hall_strip", "state": "off",
     "attributes": {"friendly_name": "Hall Strip"}},
]

LAYOUT = {
    "version": 1,
    "floors": [{"floor_id": "loft", "name": "Loft", "level": 2}],
    "rooms": [{"area_id": "puff_workshop", "name": "Workshop",
               "floor_id": "loft", "x": 0, "y": 0, "w": 4, "h": 4,
               "custom": True}],
    "placements": {"dev_plug": {"area_id": "puff_workshop", "x": 0.5, "y": 0.5}},
}


def build(**kwargs):
    params = dict(states=STATES, devices=DEVICES, entities=ENTITIES,
                  areas=AREAS, floors=FLOORS, layout=LAYOUT)
    params.update(kwargs)
    return home_topology.build_home(**params)


def device_by_id(body, device_id):
    for device in body["devices"]:
        if device["device_id"] == device_id:
            return device
    return None


# ══════════════════════════════════════════════════════════════════════
#  Area resolution
# ══════════════════════════════════════════════════════════════════════

class TestAreaResolution:
    def test_entity_area_overrides_device_area(self):
        lamp = device_by_id(build(), "dev_lamp")
        assert lamp["area_id"] == "living_room"       # not main_bedroom
        assert lamp["area_source"] == "ha"

    def test_device_area_used_when_no_entity_area(self):
        lock = device_by_id(build(), "dev_lock")
        assert (lock["area_id"], lock["area_source"]) == ("living_room", "ha")

    def test_layout_placement_is_the_last_word_before_none(self):
        plug = device_by_id(build(), "dev_plug")
        assert (plug["area_id"], plug["area_source"]) == ("puff_workshop", "layout")

    def test_no_area_anywhere_is_null(self):
        plug = device_by_id(build(layout=None), "dev_plug")
        assert plug["area_id"] is None
        assert plug["area_source"] == "none"

    def test_placement_beats_an_entity_area(self):
        """The owner's map outranks the hub.

        ``dev_lamp`` carries an entity ``area_id`` of ``living_room`` — the
        strongest thing HA has to say — and the placement still wins. The
        everyday HA user is non-admin by design, so the map is the only place
        the owner can correct a wrong area; a placement that lost would be a
        control that does nothing.
        """
        layout = dict(LAYOUT,
                      placements={"dev_lamp": {"area_id": "puff_workshop"}})
        lamp = device_by_id(build(layout=layout), "dev_lamp")
        assert (lamp["area_id"], lamp["area_source"]) == ("puff_workshop",
                                                          "layout")

    def test_placement_beats_a_device_area(self):
        layout = dict(LAYOUT,
                      placements={"dev_lock": {"area_id": "puff_workshop"}})
        lock = device_by_id(build(layout=layout), "dev_lock")
        assert (lock["area_id"], lock["area_source"]) == ("puff_workshop",
                                                          "layout")

    def test_placement_into_an_ha_area_is_still_sourced_layout(self):
        layout = dict(LAYOUT,
                      placements={"dev_lamp": {"area_id": "main_bedroom"}})
        lamp = device_by_id(build(layout=layout), "dev_lamp")
        assert (lamp["area_id"], lamp["area_source"]) == ("main_bedroom",
                                                          "layout")

    def test_a_dangling_placement_falls_through_to_home_assistant(self):
        """A deleted custom room must not strand the devices that were in it."""
        layout = dict(LAYOUT,
                      placements={"dev_lamp": {"area_id": "puff_deleted"}})
        lamp = device_by_id(build(layout=layout), "dev_lamp")
        assert (lamp["area_id"], lamp["area_source"]) == ("living_room", "ha")

    def test_a_dangling_placement_with_no_ha_area_is_null(self):
        layout = dict(LAYOUT,
                      placements={"dev_plug": {"area_id": "puff_deleted"}})
        plug = device_by_id(build(layout=layout), "dev_plug")
        assert (plug["area_id"], plug["area_source"]) == (None, "none")

    def test_custom_room_is_listed_as_an_area(self):
        areas = {a["area_id"]: a for a in build()["areas"]}
        assert areas["living_room"]["source"] == "ha"
        assert areas["living_room"]["floor_id"] == "ground"
        assert areas["puff_workshop"]["source"] == "layout"

    def test_floors_merge_registry_and_layout(self):
        floors = {f["floor_id"]: f for f in build()["floors"]}
        assert floors["upstairs"]["level"] == 1
        assert floors["loft"]["name"] == "Loft"


# ══════════════════════════════════════════════════════════════════════
#  Devices, entities, tiers
# ══════════════════════════════════════════════════════════════════════

class TestDevices:
    def test_device_less_entity_becomes_its_own_device(self):
        strip = device_by_id(build(), "entity:light.test_hall_strip")
        assert strip is not None
        assert strip["primary_entity_id"] == "light.test_hall_strip"
        assert strip["area_id"] == "living_room"
        assert strip["kind"] == "light"

    def test_disabled_entity_is_skipped(self):
        lamp = device_by_id(build(), "dev_lamp")
        ids = [e["entity_id"] for e in lamp["entities"]]
        assert "switch.test_lamp_child_lock" not in ids
        assert ids == ["light.test_floor_lamp", "sensor.test_lamp_power"]

    def test_primary_entity_prefers_light_over_sensor(self):
        lamp = device_by_id(build(), "dev_lamp")
        assert lamp["primary_entity_id"] == "light.test_floor_lamp"
        assert lamp["kind"] == "light"

    def test_system_tier_omitted_by_default(self):
        assert device_by_id(build(), "dev_sun") is None

    def test_system_tier_included_on_request(self):
        sun = device_by_id(build(include_system=True), "dev_sun")
        assert sun is not None and sun["tier"] == "system"

    def test_tier_and_integration_come_from_the_registry(self):
        assert device_by_id(build(), "dev_lamp")["tier"] == "local"
        assert device_by_id(build(), "dev_plug")["integration"] == "tuya"
        assert device_by_id(build(), "dev_plug")["tier"] == "cloud"

    def test_bridge_key_is_the_inventory_key_or_null(self):
        linked = build(links={"light.test_floor_lamp": "AA:BB:CC:00:00:01"})
        assert device_by_id(linked, "dev_lamp")["bridge_key"] == "AA:BB:CC:00:00:01"
        assert device_by_id(linked, "dev_plug")["bridge_key"] is None

    def test_safety_critical_is_flagged_for_a_lock(self):
        lock = device_by_id(build(), "dev_lock")["entities"][0]
        assert lock["safety_critical"] is True
        assert lock["controllable"] is True
        lamp = device_by_id(build(), "dev_lamp")["entities"][0]
        assert lamp["safety_critical"] is False


#: A house with the live hub's problem: 18 of 44 "devices" were HA's own
#: machinery. These are fabricated in the same shapes.
NON_PHYSICAL_DEVICES = [
    {"id": "dev_auto", "name": "Goodnight"},          # automation + script only
    {"id": "dev_tts", "name": "Piper"},               # a speech engine
    {"id": "dev_stick", "name": "SkyConnect"},        # a coordinator: NO entities
    {"id": "dev_sat", "name": "Kitchen Satellite"},   # a microphone on a shelf
    {"id": "dev_bulb2", "name": "Hall Bulb"},         # light + update + event
    {"id": "dev_printer", "name": "Printer"},         # only an update entity…
]

NON_PHYSICAL_ENTITIES = [
    {"entity_id": "automation.goodnight", "device_id": "dev_auto",
     "platform": "automation"},
    {"entity_id": "script.goodnight", "device_id": "dev_auto",
     "platform": "script"},
    {"entity_id": "tts.piper", "device_id": "dev_tts", "platform": "tts"},
    {"entity_id": "wake_word.openwakeword", "device_id": "dev_tts",
     "platform": "wyoming"},
    {"entity_id": "assist_satellite.kitchen", "device_id": "dev_sat",
     "platform": "wyoming"},
    {"entity_id": "light.hall_bulb", "device_id": "dev_bulb2",
     "platform": "zha"},
    {"entity_id": "update.hall_bulb_firmware", "device_id": "dev_bulb2",
     "platform": "zha"},
    {"entity_id": "event.hall_bulb_button", "device_id": "dev_bulb2",
     "platform": "zha"},
    {"entity_id": "update.printer_firmware", "device_id": "dev_printer",
     "platform": "ipp"},
    # A helper the owner made, with no device of its own.
    {"entity_id": "input_boolean.guest_mode", "device_id": None,
     "platform": "input_boolean"},
    {"entity_id": "conversation.home_assistant", "device_id": None,
     "platform": "conversation"},
]


def build_mixed(**kwargs):
    params = dict(states=[], devices=DEVICES + NON_PHYSICAL_DEVICES,
                  entities=ENTITIES + NON_PHYSICAL_ENTITIES,
                  areas=AREAS, floors=FLOORS, layout=LAYOUT)
    params.update(kwargs)
    return home_topology.build_home(**params)


class TestNonPhysicalFilter:
    """Only things in the house get drawn on the map."""

    def test_automation_and_script_only_device_is_omitted(self):
        assert device_by_id(build_mixed(), "dev_auto") is None

    def test_speech_engines_are_omitted(self):
        assert device_by_id(build_mixed(), "dev_tts") is None

    def test_an_update_only_service_entry_is_omitted(self):
        assert device_by_id(build_mixed(), "dev_printer") is None

    def test_a_coordinator_with_no_entities_stays_as_a_hub(self):
        stick = device_by_id(build_mixed(), "dev_stick")
        assert stick is not None and stick["kind"] == "hub"

    def test_an_assist_satellite_is_physical(self):
        sat = device_by_id(build_mixed(), "dev_sat")
        assert sat is not None
        assert "assist_satellite" not in home_topology.NON_PHYSICAL_DOMAINS

    def test_one_real_entity_among_non_physical_ones_keeps_the_device(self):
        bulb = device_by_id(build_mixed(), "dev_bulb2")
        assert bulb is not None and bulb["kind"] == "light"

    def test_a_helper_with_no_device_is_omitted(self):
        assert device_by_id(build_mixed(), "entity:input_boolean.guest_mode") is None
        assert device_by_id(build_mixed(), "entity:conversation.home_assistant") is None

    def test_a_device_on_the_lan_is_never_hidden(self):
        """A bridge_key means an address on the LAN: hardware, whatever its
        entities look like."""
        body = build_mixed(links={"update.printer_firmware": "AA:BB:CC:00:00:09"})
        printer = device_by_id(body, "dev_printer")
        assert printer is not None
        assert printer["bridge_key"] == "AA:BB:CC:00:00:09"

    def test_the_physical_house_is_untouched(self):
        ids = {d["device_id"] for d in build_mixed()["devices"]}
        assert {"dev_lamp", "dev_plug", "dev_lock",
                "entity:light.test_hall_strip"} <= ids

    def test_hidden_counts_what_it_left_out(self):
        hidden = build_mixed()["hidden"]
        # dev_auto, dev_tts, dev_printer, conversation. The input_boolean
        # helper was ALREADY hidden before this change — its platform matches
        # bridge_ha's input_ prefix, so it is system tier — and it is counted
        # where it is actually caught rather than moved.
        assert hidden["non_physical"] == 4
        assert hidden["system"] == 2                   # dev_sun + the helper
        assert set(hidden) == {"non_physical", "system"}

    def test_include_system_brings_them_all_back(self):
        body = build_mixed(include_system=True)
        ids = {d["device_id"] for d in body["devices"]}
        assert {"dev_auto", "dev_tts", "dev_printer", "dev_sun",
                "entity:input_boolean.guest_mode"} <= ids
        assert body["hidden"] == {"non_physical": 0, "system": 0}

    def test_the_plain_house_hides_nothing_new(self):
        assert build()["hidden"] == {"non_physical": 0, "system": 1}

    def test_the_rule_is_all_or_nothing(self):
        assert home_topology._is_non_physical(["automation.a", "light.b"], "") is False
        assert home_topology._is_non_physical(["automation.a"], "") is True
        assert home_topology._is_non_physical([], "") is False      # a hub
        assert home_topology._is_non_physical(["automation.a"], "AA:BB") is False

    def test_the_voice_tools_still_see_automations(self):
        """The filter is the /ha/home payload's alone: entity_area_ids, which
        ha_bridge and the voice tools resolve rooms through, must not learn it.
        "Run the goodnight script" depends on that."""
        entities = ENTITIES + NON_PHYSICAL_ENTITIES + [
            {"entity_id": "automation.porch", "device_id": None,
             "platform": "automation", "area_id": "living_room"}]
        mapping = home_topology.entity_area_ids(
            DEVICES + NON_PHYSICAL_DEVICES, entities, {}, {"living_room"})
        assert mapping["automation.porch"] == "living_room"


class TestAttributeWhitelist:
    def test_only_whitelisted_attributes_survive(self):
        lamp = device_by_id(build(), "dev_lamp")["entities"][0]
        assert lamp["attributes"] == {"brightness": 180}

    def test_secrets_in_the_state_blob_never_leak(self):
        blob = repr(build())
        assert "must-not-leak" not in blob
        assert "entity_picture" not in blob
        assert "supported_features" not in blob

    def test_sensor_keeps_its_unit(self):
        power = device_by_id(build(), "dev_lamp")["entities"][1]
        assert power["attributes"] == {"device_class": "power",
                                       "unit_of_measurement": "W"}

    def test_filter_attributes_is_pure_and_total(self):
        assert home_topology.filter_attributes("light", None) == {}
        assert home_topology.filter_attributes("nosuch", {"battery": 5}) == {
            "battery": 5}


class TestRegistryDown:
    """HA unreachable: a device list from states, no areas, no exception."""

    def test_states_only_still_lists_devices(self):
        body = home_topology.build_home(states=STATES)
        ids = {d["device_id"] for d in body["devices"]}
        assert "entity:light.test_floor_lamp" in ids
        assert body["areas"] == [] and body["floors"] == []
        lamp = device_by_id(body, "entity:light.test_floor_lamp")
        assert lamp["area_id"] is None and lamp["area_source"] == "none"
        assert lamp["entities"][0]["attributes"] == {"brightness": 180}

    def test_registry_snapshot_reports_not_ok_when_cache_is_cold(self, monkeypatch):
        bridge_ha.reset()
        monkeypatch.setattr(home_topology, "_maybe_pull", lambda: None)
        snapshot = home_topology.registry_snapshot()
        assert snapshot["ok"] is False
        assert snapshot["method"] == "states"
        assert snapshot["devices"] == []

    def test_registry_snapshot_reports_ok_with_a_warm_cache(self):
        bridge_ha.set_registries(DEVICES, ENTITIES, AREAS, FLOORS)
        try:
            snapshot = home_topology.registry_snapshot()
            assert snapshot["ok"] is True
            assert snapshot["method"] == "registry"
            assert snapshot["age_seconds"] is not None
            assert len(snapshot["floors"]) == 2
        finally:
            bridge_ha.reset()


# ══════════════════════════════════════════════════════════════════════
#  The ha_bridge repair — /ha/rooms and the voice tools
# ══════════════════════════════════════════════════════════════════════

class WireTrap(HABridge):
    """A real HABridge with the wire cut: any HTTP call is a test failure."""

    def __init__(self, states):
        self.wire: list = []
        self._states = states
        self._enabled = True
        self._url = "http://ha.invalid"

        class _NoCache:
            def get(self, key): return None
            def set(self, key, value): pass
            def invalidate(self, key=None): pass

        self._cache = _NoCache()

    def _request(self, endpoint, method="GET", data=None, timeout=None,
                 stream=False):
        self.wire.append((endpoint, method, data))
        raise AssertionError(f"the bridge reached out to HA: {endpoint}")

    def get_all_states(self, *, force: bool = False):
        return self._states


@pytest.fixture
def warm_registries(monkeypatch):
    bridge_ha.set_registries(DEVICES, ENTITIES, AREAS, FLOORS)
    monkeypatch.setattr(home_topology, "_maybe_pull", lambda: None)
    # No layout by default: these tests must not read (or depend on) whatever
    # floorplan the machine running them happens to have on disk.
    monkeypatch.setattr(home_topology, "layout_rooms_and_placements",
                        lambda: ({}, {}))
    yield
    bridge_ha.reset()


class TestBridgeRooms:
    def test_rooms_are_real_areas_not_one_unassigned_pile(self, warm_registries):
        bridge = WireTrap(STATES)
        rooms = {r.name: r for r in bridge.get_rooms()}
        assert "Living Room" in rooms
        assert bridge.wire == []                       # no REST call was made
        entity_ids = {d.entity_id for d in rooms["Living Room"].devices}
        assert "light.test_floor_lamp" in entity_ids
        assert "lock.test_front_door" in entity_ids

    def test_area_name_reaches_the_voice_tools(self, warm_registries):
        bridge = WireTrap(STATES)
        found = bridge.find_devices("floor lamp")
        assert [d.area_name for d in found] == ["Living Room"]
        listed = {d.entity_id: d for d in bridge.list_devices()}
        assert listed["light.test_floor_lamp"].area_id == "living_room"

    def test_a_placement_reaches_rooms_and_the_voice_tools(self, warm_registries,
                                                           monkeypatch):
        """/ha/rooms and /ha/home must not disagree about the floor lamp.

        The lamp has an HA entity area (``living_room``); the owner dragged it
        into their own Workshop. Both surfaces now say Workshop.
        """
        monkeypatch.setattr(
            home_topology, "layout_rooms_and_placements",
            lambda: ({"puff_workshop": "Workshop"},
                     {"dev_lamp": {"area_id": "puff_workshop"}}))
        bridge = WireTrap(STATES)
        listed = {d.entity_id: d for d in bridge.list_devices()}
        lamp = listed["light.test_floor_lamp"]
        assert (lamp.area_id, lamp.area_name) == ("puff_workshop", "Workshop")
        rooms = {r.name: r for r in bridge.get_rooms()}
        assert "light.test_floor_lamp" in {d.entity_id
                                           for d in rooms["Workshop"].devices}
        assert bridge.wire == []

    def test_a_dangling_placement_leaves_the_bridge_on_ha(self, warm_registries,
                                                          monkeypatch):
        monkeypatch.setattr(
            home_topology, "layout_rooms_and_placements",
            lambda: ({}, {"dev_lamp": {"area_id": "puff_deleted"}}))
        bridge = WireTrap(STATES)
        listed = {d.entity_id: d for d in bridge.list_devices()}
        assert listed["light.test_floor_lamp"].area_id == "living_room"

    def test_no_registry_degrades_to_unassigned_without_raising(self, monkeypatch):
        bridge_ha.reset()
        monkeypatch.setattr(home_topology, "_maybe_pull", lambda: None)
        monkeypatch.setattr(home_topology, "layout_rooms_and_placements",
                            lambda: ({}, {}))
        bridge = WireTrap(STATES)
        rooms = bridge.get_rooms()
        assert [r.area_id for r in rooms] == ["unassigned"]
        assert bridge.wire == []
