"""The Bridge ships closed, and it holds what each setting promises.

On a fresh HomeBot (fresh data and state folders, the shipped config, no
owner overlay, no environment switches) every outside service reads OFF:
every switch in ``security.egress_doors`` and every entry the outside list
keeps in ``outside_switches.json``. Only an explicit yes from the owner turns
one on, and it turns on that one only. The same holds after a full HomeBot
preset build (``puffpai.homebot_preset.run_build``).

The Bridge settings, as the owner decided them:

* Moat: only the outside services the owner switched on reach the internet.
  A yes written in a settings file does not count. The home network still
  works.
* Drawbridge Up: nothing reaches the internet, switched on or not. The home
  network still works (Home Assistant, the media server, phones, devices at
  home).

Nothing here sends a packet: the connect-time check is called directly with
the address a connection would be made to.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import sys

import pytest

from puffpai import build_manifest
from puffpai import config as config_mod
from puffpai import outside_services, zone_bridge
from puffpai.security import (bridge_guard, egress_doors, outside_switches)

#: Environment names that can force a service on or off from outside.
_ENV_SWITCHES = ("PUFFPAI_BUILD_PROBES", "PUFFPAI_WIZARD_NET_PROBE",
                 egress_doors.APPS_KILL_ENV)
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
              "https_proxy", "all_proxy")

#: Row kinds on "What leaves your computer" that the owner can switch.
_SWITCHABLE_KINDS = ("built_in", "as_built", "card", "mcp", "box", "app_web")

HOME = ("192.168.1.20", 8123)          # a Home Assistant box at home
INTERNET = ("93.184.216.34", 443)      # a public address (never dialled)


def _forget_everything() -> None:
    egress_doors.reset_cache()
    egress_doors._reset_app_consent_cache()  # noqa: SLF001
    outside_switches.forget()
    bridge_guard.forget_enforced()
    bridge_guard.forget_names()


@pytest.fixture
def fresh_hbb(tmp_path, monkeypatch):
    """A fresh install: new data and state folders, nothing switched."""
    data = tmp_path / "data"
    state = data / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(data))
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(state))
    for name in list(os.environ):
        if name.startswith("PUFFPAI_EGRESS_"):
            monkeypatch.delenv(name, raising=False)
    for name in _ENV_SWITCHES + _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    # No owner overlay yet: the file a fresh install would have is not there.
    egress_doors.set_overlay_override(str(state / egress_doors.OVERLAY_NAME))
    config_mod.get_config(_force_reload=True)
    _forget_everything()
    yield {"data": str(data), "state": str(state)}
    _forget_everything()


def _write_bridge(posture: str, run_egress: str = "enumerated") -> None:
    """Write the build record where the build writes it (the state root)."""
    path = build_manifest.manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"posture": {"build_network": "online",
                               "run_egress": run_egress},
                   "bridge": {"posture": posture}}, fh)
    _forget_everything()


def _switch_ids(rows: list[dict]) -> list[str]:
    """Every id the outside list keeps a switch for, plus the services that
    follow another one's switch."""
    ids = [r["id"] for r in rows if r.get("kind") in ("as_built", "built_in", "mcp")]
    ids += list(zone_bridge.SWITCH_FOLLOWS)
    return sorted(set(ids))


def _everything_off(where: str) -> list[str]:
    """Every registered service, every outside-list row and every switch
    entry, checked. Returns the ones that are NOT off (empty = all off)."""
    _forget_everything()
    try:
        from puffpai import feeds
        feeds.sync_doors()
    except ImportError as exc:  # another part of the build is mid-change
        pytest.fail(f"{where}: live-card services could not be registered: {exc}")
    on: list[str] = []
    for d in egress_doors.all_doors():
        allowed, why = egress_doors.egress_allowed(d.id)
        if allowed:
            on.append(f"egress switch {d.id} is on ({why})")
    rows = outside_services.rows()
    for r in rows:
        if r["state"] == "on":
            on.append(f"outside row {r['id']} reads on: {r['state_line']}")
    for sid in _switch_ids(rows):
        if outside_switches.get(sid) is not None:
            on.append(f"switch {sid} has an entry on a fresh build")
        if not outside_switches.is_off(sid):
            on.append(f"switch {sid} is not off")
    return on


# ── a fresh HomeBot ──────────────────────────────────────────────────────────

def test_every_built_in_service_defaults_off():
    assert egress_doors.DOORS, "the registry lists no services"
    assert [d.id for d in egress_doors.DOORS if d.default is not False] == []


def test_a_fresh_homebot_has_every_outside_service_off(fresh_hbb):
    assert not os.path.exists(outside_switches.path())
    assert _everything_off("fresh install") == []


def test_a_fresh_homebot_lists_no_row_as_on_until_switched(fresh_hbb):
    rows = outside_services.rows()
    assert rows, "the outside list is empty"
    for r in rows:
        if r.get("kind") == "as_built":
            assert r["state"] == "off", r["id"]
            assert r["state_line"] == outside_services.OFF_UNTIL_ON, r["id"]
            assert r["can_switch"] is True, r["id"]
    page = outside_services.page()
    # The reading helper's row brings its own words from the book reader.
    own = dict(page, services=[r for r in page["services"]
                               if r.get("kind") != "reading_helper"])
    assert "BuildBot" not in json.dumps(own)
    assert " door" not in json.dumps(own).lower()


def test_an_owner_yes_turns_on_that_one_service_only(fresh_hbb):
    outside_switches.set_switch("library", True, by="owner")
    _forget_everything()
    assert outside_switches.is_off("library") is False
    assert outside_services.row("library")["state"] == "on"
    still_on = [line for line in _everything_off("after one yes")
                if "library" not in line]
    assert still_on == []

    # A built-in service: the owner's switch is what opens it. (A derived
    # service, the connection check, only ever looks at services already on.)
    outside_switches.set_switch("weather", True, by="owner")
    _forget_everything()
    assert egress_doors.egress_allowed("weather")[0] is True
    others = [d.id for d in egress_doors.all_doors()
              if d.id != "weather" and not d.derived
              and egress_doors.egress_allowed(d.id)[0]]
    assert others == []


def test_an_owner_no_wins_over_an_earlier_yes(fresh_hbb):
    outside_switches.set_switch("code_search", True, by="owner")
    outside_switches.set_switch("code_search", False, by="owner")
    _forget_everything()
    assert outside_switches.is_off("code_search") is True


# ── after a full HomeBot preset build ────────────────────────────────────────

def _preset_build():
    try:
        mod = importlib.import_module("puffpai.homebot_preset")
    except ModuleNotFoundError as exc:
        if exc.name == "puffpai.homebot_preset":
            return None
        raise
    fn = getattr(mod, "run_build", None)
    return fn if callable(fn) else None


def test_a_full_homebot_preset_build_leaves_every_outside_service_off(fresh_hbb):
    build = _preset_build()
    if build is None:
        pytest.skip("the HomeBot preset build (puffpai.homebot_preset.run_build) "
                    "is not in this tree, so there is no preset build to check")
    params = inspect.signature(build).parameters
    kwargs = {}
    if "data_dir" in params:
        kwargs["data_dir"] = fresh_hbb["data"]
    if "state_dir" in params:
        kwargs["state_dir"] = fresh_hbb["state"]
    record = build(**kwargs)
    config_mod.get_config(_force_reload=True)
    assert isinstance(record, dict)
    assert _everything_off("after the HomeBot preset build") == []
    manifest = record.get("manifest") or {}
    posture = manifest.get("posture") or {}
    assert list(posture.get("enabled_switches") or []) == []
    assert not any((posture.get("egress_switches") or {}).values())
    # The HomeBot build pins its own Bridge: Moat, which is what is enforced.
    assert (manifest.get("bridge") or {}).get("posture") == "moat"
    _forget_everything()
    assert bridge_guard.reach_level() == zone_bridge.LEVEL_SWITCHED_ON
    assert bridge_guard.home_network_allowed() is True


# ── what each Bridge setting holds ───────────────────────────────────────────

def _check(address):
    """The connect-time check, as a connection from this test would meet it."""
    bridge_guard.check(None, address, sys._getframe(0))


def test_moat_allows_only_what_the_owner_switched_on(fresh_hbb):
    _write_bridge("moat")
    assert bridge_guard.reach_level() == zone_bridge.LEVEL_SWITCHED_ON
    assert bridge_guard.home_network_allowed() is True

    # A yes written in the settings file does not open a service at Moat.
    cfg = config_mod.get_config()
    path = os.path.join(cfg.CONFIG_DIR, "puffpai_config.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    flag = egress_doors.door("weather").config_flag
    data.setdefault(egress_doors.CONFIG_SECTION, {})[flag] = True
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    _forget_everything()
    assert egress_doors.egress_allowed("weather")[0] is False

    # The owner's own switch does.
    outside_switches.set_switch("weather", True, by="owner")
    outside_switches.set_switch("library", True, by="owner")
    _forget_everything()
    assert egress_doors.egress_allowed("weather")[0] is True
    assert outside_switches.is_off("library") is False
    assert outside_switches.is_off("code_search") is True

    # The home network still works; the internet outside a listed exit
    # (each of which checks the owner's switch) does not.
    _check(HOME)
    with pytest.raises(bridge_guard.BridgeRefused) as refused:
        _check(INTERNET)
    assert refused.value.user_message == zone_bridge.REFUSED_LINE


def test_drawbridge_up_lets_nothing_reach_the_internet(fresh_hbb):
    outside_switches.set_switch("weather", True, by="owner")
    outside_switches.set_switch("library", True, by="owner")
    _write_bridge("drawbridge_up")
    assert bridge_guard.reach_level() == zone_bridge.LEVEL_SEALED
    assert bridge_guard.home_network_allowed() is True

    # Switched on or not, nothing is used.
    assert egress_doors.egress_allowed("weather")[0] is False
    assert outside_switches.is_off("library") is True
    row = outside_services.row("library")
    assert row["state"] == "off" and row["can_switch"] is False

    # The home network still works.
    _check(HOME)
    _check(("10.0.0.5", 8096))         # a media server at home
    with pytest.raises(bridge_guard.BridgeRefused) as refused:
        _check(INTERNET)
    assert refused.value.user_message == zone_bridge.SEALED_LINE

    enforced = bridge_guard.enforced()
    assert enforced["home_network"] is True
    assert enforced["line"] == zone_bridge.ENFORCED_LINES[zone_bridge.LEVEL_SEALED]


def test_a_this_computer_only_build_does_not_reach_home_either(fresh_hbb):
    _write_bridge("drawbridge_up", run_egress="sealed")
    assert bridge_guard.home_network_allowed() is False
    with pytest.raises(bridge_guard.BridgeRefused) as refused:
        _check(HOME)
    assert refused.value.user_message == zone_bridge.LOCAL_ONLY_REFUSED_LINE
    with pytest.raises(bridge_guard.BridgeRefused):
        _check(INTERNET)


def test_a_bridge_record_nobody_can_read_is_the_strict_one(fresh_hbb):
    path = build_manifest.manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    _forget_everything()
    assert bridge_guard.reach_level() == zone_bridge.LEVEL_SEALED
    with pytest.raises(bridge_guard.BridgeRefused):
        _check(INTERNET)


def test_a_proxy_at_home_is_refused_with_the_bridge_up(fresh_hbb, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://192.168.1.30:3128")
    _write_bridge("drawbridge_up")
    with pytest.raises(bridge_guard.BridgeRefused):
        _check(("192.168.1.30", 3128))


def test_the_guard_reads_the_record_where_the_build_writes_it(
        fresh_hbb, tmp_path, monkeypatch):
    # A separate config folder (an install that keeps its code tree
    # read-only) must not hide the Bridge the build chose.
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(other))
    config_mod.get_config(_force_reload=True)
    _write_bridge("drawbridge_up")
    assert os.path.dirname(build_manifest.manifest_path()) != str(other)
    assert bridge_guard.reach_level() == zone_bridge.LEVEL_SEALED
    with pytest.raises(bridge_guard.BridgeRefused):
        _check(INTERNET)


@pytest.mark.parametrize("state_posture,config_posture", [
    ("moat", "drawbridge_up"), ("drawbridge_up", "fenced")])
def test_two_records_in_two_places_hold_the_stricter(
        fresh_hbb, tmp_path, monkeypatch, state_posture, config_posture):
    # An older build may have left its record in the config folder. Neither
    # place may loosen the Bridge the other one records.
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(other))
    config_mod.get_config(_force_reload=True)
    _write_bridge(state_posture)
    with open(other / "build_manifest.json", "w", encoding="utf-8") as fh:
        json.dump({"posture": {"run_egress": "enumerated"},
                   "bridge": {"posture": config_posture}}, fh)
    _forget_everything()
    assert bridge_guard.reach_level() == zone_bridge.LEVEL_SEALED


# ── the words ────────────────────────────────────────────────────────────────

def test_the_bridge_words_say_what_is_enforced():
    menu = {p["id"]: p for p in zone_bridge.posture_menu()}
    assert set(menu) == {"open_yard", "fenced", "moat", "drawbridge_up"}
    moat = json.dumps(menu["moat"]).lower()
    assert "only the outside services you switched on" in moat
    assert "asks" not in moat and "prompt" not in moat
    up = json.dumps(menu["drawbridge_up"]).lower()
    assert "nothing reaches the internet" in up
    assert "home network still works" in up
    for source in (zone_bridge.load_postures(), zone_bridge._FALLBACK_POSTURES):  # noqa: SLF001
        text = json.dumps(source)
        assert "door" not in text.lower()
        assert "—" not in text and "–" not in text
    for line in list(zone_bridge.ENFORCED_LINES.values()) + [
            zone_bridge.LOCAL_ONLY_LINE, zone_bridge.SEALED_LINE,
            zone_bridge.REFUSED_LINE, zone_bridge.LOCAL_ONLY_REFUSED_LINE]:
        assert "door" not in line.lower() and "BuildBot" not in line
