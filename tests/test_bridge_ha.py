"""
The managed-device view: HA's registries folded into buckets.

The registries themselves are never fetched here — the token is vault-gated
and the WebSocket is the inventory's business — so every test seeds
``bridge_ha.set_registries`` with the exact shapes HA sends
(``config/device_registry/list``, ``config/entity_registry/list``,
``config/area_registry/list``) and asserts on the fold.

What is actually under test is the tier rule, because it is the only judgement
call in the module: an integration in the table keeps its tier even when the
device is also a row on the network roll, and ``wifi_direct`` is the fallback
for integrations the table does not name. That ordering is deliberate and it
is the thing a future edit is most likely to get backwards.
"""

from __future__ import annotations

import pytest

from puffpai import bridge_ha


# ── fixtures ────────────────────────────────────────────────────────────────

AREAS = [
    {"area_id": "kitchen", "name": "Kitchen"},
    {"area_id": "porch", "name": "Porch"},
]


def _device(did, name, integration, area=None, manufacturer="", model=""):
    return {
        "id": did,
        "name": name,
        "area_id": area,
        "manufacturer": manufacturer,
        "model": model,
        "identifiers": [[integration, f"{did}-serial"]],
    }


def _entity(entity_id, device_id, platform, area=None, name=""):
    return {"entity_id": entity_id, "device_id": device_id,
            "platform": platform, "area_id": area, "name": name}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bridge_ha.reset()
    # Every test that does not say otherwise has no hub traffic and no
    # inventory: attribution and the wifi_direct link are opt-in.
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: "")
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: ({}, None))
    yield
    bridge_ha.reset()


def _seed(devices, entities, areas=AREAS):
    bridge_ha.set_registries(devices, entities, areas)


# ── tier rules ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("integration,expected", [
    ("zha", bridge_ha.TIER_LOCAL),
    ("zwave_js", bridge_ha.TIER_LOCAL),
    ("esphome", bridge_ha.TIER_LOCAL),
    ("hue", bridge_ha.TIER_LOCAL),
    ("shelly", bridge_ha.TIER_LOCAL),
    ("matter", bridge_ha.TIER_LOCAL),
    ("tuya", bridge_ha.TIER_CLOUD),
    ("ring", bridge_ha.TIER_CLOUD),
    ("smartthings", bridge_ha.TIER_CLOUD),
    ("wyze", bridge_ha.TIER_CLOUD),
    ("myq", bridge_ha.TIER_CLOUD),
])
def test_table_tiers_do_not_depend_on_having_an_address(integration, expected):
    """Both ways: a table entry keeps its tier joined or not."""
    assert bridge_ha.tier_for(integration, False) == expected
    assert bridge_ha.tier_for(integration, True) == expected


def test_an_unlisted_integration_with_a_network_row_is_wifi_direct():
    assert bridge_ha.tier_for("some_new_thing", True) == bridge_ha.TIER_WIFI


def test_an_unlisted_integration_with_no_network_row_is_unknown():
    assert bridge_ha.tier_for("some_new_thing", False) == bridge_ha.TIER_UNKNOWN


def test_integration_comes_from_the_entity_platform_first():
    """The device identifier says "mqtt"; the entity says who really made it."""
    device = _device("d1", "Bulb", "mqtt")
    entities = [_entity("light.bulb", "d1", "zha")]
    assert bridge_ha.integration_of(device, entities) == "zha"


def test_integration_falls_back_to_the_device_identifier():
    device = _device("d1", "Bulb", "zwave_js")
    assert bridge_ha.integration_of(device, []) == "zwave_js"


def test_a_device_with_neither_is_unknown():
    assert bridge_ha.integration_of({"id": "d1"}, []) == "unknown"


# ── the fold ────────────────────────────────────────────────────────────────

def test_buckets_count_devices_entities_areas_and_kinds():
    _seed(
        [_device("d1", "Lamp", "zha", area="kitchen", manufacturer="Ikea"),
         _device("d2", "Sensor", "zha", area="porch")],
        [_entity("light.lamp", "d1", "zha"),
         _entity("switch.lamp_relay", "d1", "zha"),
         _entity("binary_sensor.porch", "d2", "zha")],
    )
    rows = bridge_ha.buckets()
    assert [r["id"] for r in rows] == ["local:zha"]
    row = rows[0]
    assert row["device_count"] == 2
    assert row["entity_count"] == 3
    assert row["areas"] == {"Kitchen": 1, "Porch": 1}
    assert row["kinds"] == {"light": 1, "switch": 1, "binary_sensor": 1}


def test_an_entity_domain_outside_the_named_kinds_is_other():
    _seed([_device("d1", "Thing", "zha")],
          [_entity("update.thing_firmware", "d1", "zha")])
    assert bridge_ha.buckets()[0]["kinds"] == {"other": 1}


def test_a_device_with_no_area_is_unassigned_not_blank():
    _seed([_device("d1", "Lamp", "zha")], [_entity("light.lamp", "d1", "zha")])
    assert bridge_ha.buckets()[0]["areas"] == {"Unassigned": 1}


def test_an_area_on_the_entity_stands_in_for_a_device_with_none():
    _seed([_device("d1", "Lamp", "zha")],
          [_entity("light.lamp", "d1", "zha", area="porch")])
    assert bridge_ha.buckets()[0]["areas"] == {"Porch": 1}


# ── the wifi_direct link ────────────────────────────────────────────────────

def test_a_joined_device_on_an_unlisted_integration_becomes_wifi_direct(monkeypatch):
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: ({
        "sensor.gizmo": {"key": "AA:BB:CC:00:00:01", "name": "Gizmo",
                         "match": "mac"}}, None))
    _seed([_device("d1", "Gizmo", "gizmo_cloud")],
          [_entity("sensor.gizmo", "d1", "gizmo_cloud")])
    rows = bridge_ha.buckets()
    assert [r["id"] for r in rows] == ["wifi_direct:gizmo_cloud"]
    assert rows[0]["linked_keys"] == ["AA:BB:CC:00:00:01"]


def test_the_link_carries_the_join_rule_and_row_into_the_drilldown(monkeypatch):
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: ({
        "sensor.gizmo": {"key": "AA:BB:CC:00:00:01", "name": "Gizmo",
                         "match": "mac"}}, None))
    _seed([_device("d1", "Gizmo", "gizmo_cloud")],
          [_entity("sensor.gizmo", "d1", "gizmo_cloud")])
    device = bridge_ha.bucket("wifi_direct:gizmo_cloud")["devices"][0]
    assert device["ha_match"] == "mac"
    assert device["linked_key"] == "AA:BB:CC:00:00:01"
    assert device["linked_name"] == "Gizmo"


def test_the_link_joins_on_the_first_entity_id_only(monkeypatch):
    """The inventory records ONE entity id per HA device, the sorted first."""
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: ({
        "zz.last": {"key": "K", "name": "n", "match": "ip"}}, None))
    _seed([_device("d1", "Gizmo", "gizmo_cloud")],
          [_entity("aa.first", "d1", "gizmo_cloud"),
           _entity("zz.last", "d1", "gizmo_cloud")])
    assert [r["id"] for r in bridge_ha.buckets()] == ["unknown:gizmo_cloud"]


def test_bucket_for_devices_is_pure_over_the_registry_cache():
    """The inventory calls this inside its merge lock; it must not read back."""
    _seed([_device("d1", "Gizmo", "gizmo_cloud"),
           _device("d2", "Lamp", "zha")],
          [_entity("sensor.gizmo", "d1", "gizmo_cloud"),
           _entity("light.lamp", "d2", "zha")])
    assert bridge_ha.bucket_for_devices({"sensor.gizmo"}) == {
        "sensor.gizmo": "wifi_direct:gizmo_cloud",
        "light.lamp": "local:zha",
    }


# ── attribution ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain,expected", [
    ("a1.tuyaus.com", "tuya"),
    ("tuya.com", "tuya"),
    ("fw.ring.com", "ring"),
    ("home.nest.com", "nest"),
    ("api.smartthings.com", "smartthings"),
    ("alexa.amazonalexa.com", "alexa"),
    ("nothing-here.example.org", ""),
    ("", ""),
])
def test_domains_attribute_to_an_integration_by_suffix(domain, expected):
    assert bridge_ha.attribute_domain(domain) == expected


def test_a_suffix_match_never_fires_on_a_lookalike():
    assert bridge_ha.attribute_domain("notring.com") == ""


def _hub_ledger(monkeypatch, rows):
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: "192.168.50.140")
    monkeypatch.setattr(
        bridge_ha, "_hub_rows",
        lambda hours: [r for r in rows if r["client_ip"] == "192.168.50.140"])


def test_hub_lookups_land_on_the_integration_that_caused_them(monkeypatch):
    _hub_ledger(monkeypatch, [
        {"client_ip": "192.168.50.140", "domain": "a1.tuyaus.com",
         "category": "TELEMETRY"},
        {"client_ip": "192.168.50.140", "domain": "a1.tuyaus.com",
         "category": "TELEMETRY"},
        {"client_ip": "192.168.50.140", "domain": "px.tuya.com",
         "category": "CLOUD"},
        # Somebody else's row: same domain, different client, not the hub's.
        {"client_ip": "192.168.50.99", "domain": "a1.tuyaus.com",
         "category": "TELEMETRY"},
    ])
    _seed([_device("d1", "Plug", "tuya")], [_entity("switch.plug", "d1", "tuya")])
    row = bridge_ha.buckets()[0]
    assert row["id"] == "cloud:tuya"
    assert row["lookups"] == 3
    assert row["watching"] == 2
    assert row["top_domains"] == [
        {"domain": "a1.tuyaus.com", "count": 2},
        {"domain": "px.tuya.com", "count": 1},
    ]


def test_unattributed_hub_lookups_fall_to_the_home_assistant_bucket(monkeypatch):
    _hub_ledger(monkeypatch, [
        {"client_ip": "192.168.50.140", "domain": "version.home-assistant.io",
         "category": "other"},
        {"client_ip": "192.168.50.140", "domain": "analytics.example.net",
         "category": "TRACKING"},
    ])
    _seed([_device("d1", "Lamp", "zha")], [_entity("light.lamp", "d1", "zha")])
    by_id = {r["id"]: r for r in bridge_ha.buckets()}
    assert by_id[bridge_ha.HA_SELF_BUCKET]["lookups"] == 2
    assert by_id[bridge_ha.HA_SELF_BUCKET]["watching"] == 1
    assert by_id[bridge_ha.HA_SELF_BUCKET]["device_count"] == 0
    assert by_id[bridge_ha.HA_SELF_BUCKET]["label"] == "Home Assistant itself"
    assert by_id["local:zha"]["lookups"] == 0


def test_a_recognised_cloud_with_no_registry_devices_lands_on_the_hub(monkeypatch):
    """The hub really made the lookup; inventing a device-less tuya bucket
    would claim a device exists that the registries do not have."""
    _hub_ledger(monkeypatch, [
        {"client_ip": "192.168.50.140", "domain": "a1.tuyaus.com",
         "category": "TELEMETRY"},
    ])
    _seed([_device("d1", "Lamp", "zha")], [_entity("light.lamp", "d1", "zha")])
    by_id = {r["id"]: r for r in bridge_ha.buckets()}
    assert "cloud:tuya" not in by_id
    assert by_id[bridge_ha.HA_SELF_BUCKET]["lookups"] == 1


def test_counts_roll_the_buckets_up(monkeypatch):
    _hub_ledger(monkeypatch, [
        {"client_ip": "192.168.50.140", "domain": "fw.ring.com",
         "category": "TELEMETRY"},
    ])
    _seed([_device("d1", "Lamp", "zha"), _device("d2", "Doorbell", "ring")],
          [_entity("light.lamp", "d1", "zha"),
           _entity("camera.doorbell", "d2", "ring"),
           _entity("binary_sensor.doorbell", "d2", "ring")])
    assert bridge_ha.counts() == {
        "buckets": 2, "devices": 2, "entities": 3, "cloud_buckets": 1,
        "lookups_24h": 1, "watching_24h": 1,
    }


# ── sorting ─────────────────────────────────────────────────────────────────

def test_cloud_first_loudest_first_then_wifi_local_unknown(monkeypatch):
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: ({
        "sensor.gizmo": {"key": "K", "name": "Gizmo", "match": "ip"}}, None))
    _hub_ledger(monkeypatch, [
        {"client_ip": "192.168.50.140", "domain": "fw.ring.com", "category": "CLOUD"},
        {"client_ip": "192.168.50.140", "domain": "a1.tuyaus.com", "category": "CLOUD"},
        {"client_ip": "192.168.50.140", "domain": "a2.tuyaus.com", "category": "CLOUD"},
    ])
    _seed(
        [_device("d1", "Lamp", "zha"),
         _device("d2", "Doorbell", "ring"),
         _device("d3", "Plug", "tuya"),
         _device("d4", "Gizmo", "gizmo_cloud"),
         _device("d5", "Widget", "widget_co")],
        [_entity("light.lamp", "d1", "zha"),
         _entity("camera.doorbell", "d2", "ring"),
         _entity("switch.plug", "d3", "tuya"),
         _entity("sensor.gizmo", "d4", "gizmo_cloud"),
         _entity("sensor.widget", "d5", "widget_co")],
    )
    assert [r["id"] for r in bridge_ha.buckets()] == [
        "cloud:tuya",              # 2 lookups
        "cloud:ring",              # 1 lookup
        "wifi_direct:gizmo_cloud",
        "local:zha",
        "unknown:widget_co",
    ]


def test_within_a_non_cloud_tier_the_bigger_bucket_leads():
    _seed(
        [_device("d1", "A", "zha"), _device("d2", "B", "zha"),
         _device("d3", "C", "esphome")],
        [_entity("light.a", "d1", "zha"), _entity("light.b", "d2", "zha"),
         _entity("sensor.c", "d3", "esphome")],
    )
    assert [r["id"] for r in bridge_ha.buckets()] == ["local:zha", "local:esphome"]


# ── id validation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "", "tuya", "cloud:", ":tuya", "CLOUD:tuya", "cloud:TUYA",
    "hybrid:tuya", "cloud:tuya/../etc", "cloud:tu ya", "cloud:tuya;drop",
    "cloud:" + "x" * 65, "../../etc/passwd", "cloud:tuya\n",
])
def test_a_malformed_bucket_id_is_refused_before_anything_is_read(bad):
    assert bridge_ha.bucket(bad) is None


def test_a_well_formed_id_for_a_bucket_that_does_not_exist_is_none():
    _seed([_device("d1", "Lamp", "zha")], [_entity("light.lamp", "d1", "zha")])
    assert bridge_ha.bucket("cloud:tuya") is None


def test_a_well_formed_id_returns_the_devices_and_their_entities():
    _seed([_device("d1", "Lamp", "zha", area="kitchen",
                   manufacturer="Ikea", model="TRADFRI")],
          [_entity("light.lamp", "d1", "zha", name="Lamp"),
           _entity("sensor.lamp_power", "d1", "zha")])
    detail = bridge_ha.bucket("local:zha")
    assert detail["id"] == "local:zha"
    assert len(detail["devices"]) == 1
    device = detail["devices"][0]
    assert device["name"] == "Lamp"
    assert device["area"] == "Kitchen"
    assert device["manufacturer"] == "Ikea"
    assert device["model"] == "TRADFRI"
    assert sorted(e["entity_id"] for e in device["entities"]) == [
        "light.lamp", "sensor.lamp_power"]
    assert {e["domain"] for e in device["entities"]} == {"light", "sensor"}


def test_the_roll_never_carries_the_device_list():
    """A bucket row is a summary; the devices are one request further in."""
    _seed([_device("d1", "Lamp", "zha")], [_entity("light.lamp", "d1", "zha")])
    assert "devices" not in bridge_ha.buckets()[0]


# ── degradation ─────────────────────────────────────────────────────────────

def test_no_registries_yet_is_an_empty_roll_not_an_error():
    assert bridge_ha.buckets() == []
    assert bridge_ha.counts()["devices"] == 0


def test_junk_in_the_registries_is_skipped_rather_than_raising():
    _seed(["not a dict", None, _device("d1", "Lamp", "zha")],
          ["nope", _entity("light.lamp", "d1", "zha")],
          areas=["nope", {"area_id": "kitchen", "name": "Kitchen"}])
    assert [r["id"] for r in bridge_ha.buckets()] == ["local:zha"]


def test_a_ledger_fault_leaves_the_buckets_standing(monkeypatch):
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: "192.168.50.140")

    def _boom(*_a, **_k):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr("puffpai.bridge_ledger.get_ledger", _boom)
    _seed([_device("d1", "Lamp", "zha")], [_entity("light.lamp", "d1", "zha")])
    rows = bridge_ha.buckets()
    assert [r["id"] for r in rows] == ["local:zha"]
    assert rows[0]["lookups"] == 0


def test_no_configured_hub_means_no_attribution(monkeypatch):
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: "")
    called = []
    monkeypatch.setattr("puffpai.bridge_ledger.get_ledger",
                        lambda: called.append(1))
    _seed([_device("d1", "Lamp", "zha")], [_entity("light.lamp", "d1", "zha")])
    assert bridge_ha.buckets()[0]["lookups"] == 0
    assert called == []


# ── the tiers Alex's house actually returned ──────────────────────────────
#
# The live ``/bridge/ha/buckets`` on the house came back with 19 buckets and
# 13 of them in ``unknown`` — which meant the Firewall page, which only draws
# ``cloud:*`` rows, showed her nothing at all. Every integration below is one
# the hub really reported. The parametrisation is the regression: this is the
# exact list that was wrong, so it is the exact list that has to stay right.

LIVE_INTEGRATION_TIERS = [
    # was already right
    ("music_assistant", bridge_ha.TIER_LOCAL),
    ("homekit_controller", bridge_ha.TIER_LOCAL),
    ("upnp", bridge_ha.TIER_LOCAL),
    ("esphome", bridge_ha.TIER_LOCAL),
    ("ipp", bridge_ha.TIER_LOCAL),
    ("zha", bridge_ha.TIER_LOCAL),
    # was unknown, is local: real hardware in this house, own IP, no vendor
    ("reolink", bridge_ha.TIER_LOCAL),
    ("apple_tv", bridge_ha.TIER_LOCAL),
    ("ollama", bridge_ha.TIER_LOCAL),
    ("vlc_telnet", bridge_ha.TIER_LOCAL),
    # was unknown, is cloud: these leave the house on a schedule
    ("openweathermap", bridge_ha.TIER_CLOUD),
    ("google_translate", bridge_ha.TIER_CLOUD),
    # was unknown, is system: Home Assistant's own furniture
    ("hassio", bridge_ha.TIER_SYSTEM),
    ("backup", bridge_ha.TIER_SYSTEM),
    ("sun", bridge_ha.TIER_SYSTEM),
    ("raspberry_pi", bridge_ha.TIER_SYSTEM),
    ("rpi_power", bridge_ha.TIER_SYSTEM),
    # the genuinely unattributed device stays a question
    ("unknown", bridge_ha.TIER_UNKNOWN),
]


@pytest.mark.parametrize("integration,tier", LIVE_INTEGRATION_TIERS)
def test_the_live_house_integrations_land_in_the_stated_tier(integration, tier):
    assert bridge_ha.tier_for(integration, on_network=False) == tier


@pytest.mark.parametrize("integration,tier", LIVE_INTEGRATION_TIERS)
def test_a_network_row_never_moves_a_classified_integration(integration, tier):
    """``wifi_direct`` is the fallback for the unlisted, not an override."""
    assert bridge_ha.tier_for(integration, on_network=True) == tier


def test_an_unlisted_integration_with_a_network_row_is_still_wifi_direct():
    assert bridge_ha.tier_for("gizmo_cloud", True) == bridge_ha.TIER_WIFI
    assert bridge_ha.tier_for("gizmo_cloud", False) == bridge_ha.TIER_UNKNOWN


def test_a_device_with_no_integration_stays_unknown_even_when_on_the_lan():
    """``unknown:unknown`` is a question about a device, not a wifi finding."""
    assert bridge_ha.tier_for("unknown", True) == bridge_ha.TIER_UNKNOWN
    assert bridge_ha.tier_for("", True) == bridge_ha.TIER_UNKNOWN


@pytest.mark.parametrize("helper", [
    "input_boolean", "input_number", "input_select", "input_text",
    "input_datetime", "input_button",
])
def test_the_helper_family_is_system_without_being_listed_one_by_one(helper):
    assert bridge_ha.tier_for(helper, False) == bridge_ha.TIER_SYSTEM


def test_nabu_casa_is_cloud_not_system():
    """HA calls the integration ``cloud`` and it is one: it leaves the house."""
    assert bridge_ha.INTEGRATION_TIERS["cloud"] == bridge_ha.TIER_CLOUD
    assert "nabucasa.com" in bridge_ha.INTEGRATION_DOMAIN_SUFFIXES["cloud"]


def test_plex_moved_out_of_cloud():
    """A Plex server is a box in the house; the page said a vendor was told."""
    assert bridge_ha.INTEGRATION_TIERS["plex"] == bridge_ha.TIER_LOCAL


# ── system is never policy-eligible ─────────────────────────────────────────

def test_system_is_not_offered_a_policy_anywhere_in_the_roll():
    """The eligible set is the cloud set. Nothing system-tier may enter it."""
    _seed(
        [_device("d1", "Supervisor", "hassio"),
         _device("d2", "Sun", "sun"),
         _device("d3", "Backup", "backup"),
         _device("d4", "Weather", "openweathermap"),
         _device("d5", "Lamp", "zha")],
        [_entity("sensor.sup", "d1", "hassio"),
         _entity("sun.sun", "d2", "sun"),
         _entity("sensor.backup", "d3", "backup"),
         _entity("weather.home", "d4", "openweathermap"),
         _entity("light.lamp", "d5", "zha")])
    rows = bridge_ha.buckets()
    eligible = {r["id"] for r in rows if r["tier"] == bridge_ha.TIER_CLOUD}
    assert eligible == {"cloud:openweathermap"}
    assert not any(bid.startswith("system:") for bid in eligible)
    system_ids = {r["id"] for r in rows if r["tier"] == bridge_ha.TIER_SYSTEM}
    assert system_ids == {"system:hassio", "system:sun", "system:backup"}


def test_system_buckets_are_still_named_and_reachable():
    """Never offered a policy is not the same as hidden."""
    _seed([_device("d1", "Supervisor", "hassio")],
          [_entity("sensor.sup", "d1", "hassio")])
    detail = bridge_ha.bucket("system:hassio")
    assert detail is not None
    assert detail["tier"] == bridge_ha.TIER_SYSTEM
    assert detail["devices"][0]["name"] == "Supervisor"


def test_the_bucket_id_pattern_admits_system():
    assert bridge_ha.BUCKET_ID_RE.match("system:hassio")
    assert not bridge_ha.BUCKET_ID_RE.match("systemic:hassio")


# ── cloud entries carry the domains that make them cloud ────────────────────

def test_every_cloud_integration_has_domain_suffixes():
    """A cloud bucket with no suffixes can never be attributed a lookup."""
    cloud = {name for name, tier in bridge_ha.INTEGRATION_TIERS.items()
             if tier == bridge_ha.TIER_CLOUD}
    missing = sorted(cloud - set(bridge_ha.INTEGRATION_DOMAIN_SUFFIXES))
    assert missing == []


def test_no_system_integration_claims_a_cloud_domain():
    for name in bridge_ha.INTEGRATION_DOMAIN_SUFFIXES:
        tier = bridge_ha.INTEGRATION_TIERS.get(name)
        assert tier != bridge_ha.TIER_SYSTEM, f"{name} is system-tier"


@pytest.mark.parametrize("domain,integration", [
    ("api.openweathermap.org", "openweathermap"),
    ("translate.googleapis.com", "google_translate"),
    ("cloud.nabu.casa", "cloud"),
    ("api.openai.com", "openai_conversation"),
    ("api.anthropic.com", "anthropic"),
])
def test_the_new_cloud_domains_attribute_to_their_integration(domain,
                                                              integration):
    assert bridge_ha.attribute_domain(domain) == integration


def test_the_generic_google_suffix_was_not_stolen_by_the_calendar():
    assert bridge_ha.attribute_domain("oauth2.googleapis.com") == "google"


# ── the appliance's own row ─────────────────────────────────────────────────

def test_ollama_links_to_the_appliances_own_inventory_row(monkeypatch):
    """The model server IS this box: link it, do not leave it looking orphaned."""
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: (
        {}, {"key": "192.168.50.40", "name": "PuffPAI", "match": "self"}))
    _seed([_device("d1", "Ollama", "ollama")],
          [_entity("conversation.ollama", "d1", "ollama")])
    rows = bridge_ha.buckets()
    assert [r["id"] for r in rows] == ["local:ollama"]
    assert rows[0]["linked_keys"] == ["192.168.50.40"]
    device = bridge_ha.bucket("local:ollama")["devices"][0]
    assert device["linked_key"] == "192.168.50.40"
    assert device["linked_name"] == "PuffPAI"
    assert device["ha_match"] == "self"


def test_ollama_stays_local_when_there_is_no_self_row(monkeypatch):
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: ({}, None))
    _seed([_device("d1", "Ollama", "ollama")],
          [_entity("conversation.ollama", "d1", "ollama")])
    rows = bridge_ha.buckets()
    assert [r["id"] for r in rows] == ["local:ollama"]
    assert rows[0]["linked_keys"] == []


#: The real function, captured before the autouse fixture replaces it.
_REAL_INVENTORY_LINKS = bridge_ha._inventory_links


def test_the_self_row_is_read_from_the_inventory_is_self_flag(monkeypatch):
    class _Inv:
        def devices(self):
            return [
                {"key": "AA:BB", "display_name": "Lamp", "ha_entity_id": "",
                 "ha_match": "", "is_self": False},
                {"key": "192.168.50.40", "display_name": "PuffPAI",
                 "ha_entity_id": "", "ha_match": "", "is_self": True},
            ]

    monkeypatch.setattr("puffpai.bridge_inventory.get_inventory",
                        lambda: _Inv())
    _links, self_link = _REAL_INVENTORY_LINKS()
    assert self_link == {"key": "192.168.50.40", "name": "PuffPAI",
                         "match": "self"}


# ── sort order ──────────────────────────────────────────────────────────────

def test_system_sorts_last_after_unknown(monkeypatch):
    monkeypatch.setattr(bridge_ha, "_inventory_links", lambda: (
        {"sensor.gizmo": {"key": "K", "name": "Gizmo", "match": "ip"}}, None))
    _seed(
        [_device("d1", "Lamp", "zha"),
         _device("d2", "Doorbell", "ring"),
         _device("d3", "Gizmo", "gizmo_cloud"),
         _device("d4", "Widget", "widget_co"),
         _device("d5", "Supervisor", "hassio")],
        [_entity("light.lamp", "d1", "zha"),
         _entity("binary_sensor.ding", "d2", "ring"),
         _entity("sensor.gizmo", "d3", "gizmo_cloud"),
         _entity("sensor.widget", "d4", "widget_co"),
         _entity("sensor.sup", "d5", "hassio")])
    assert [r["tier"] for r in bridge_ha.buckets()] == [
        bridge_ha.TIER_CLOUD, bridge_ha.TIER_WIFI, bridge_ha.TIER_LOCAL,
        bridge_ha.TIER_UNKNOWN, bridge_ha.TIER_SYSTEM,
    ]
