"""Zone 3 engine factory, enforce persistence, and Bridge routing.

Three things are pinned here, all of which were broken or absent before:

1. **One engine.** The four ``zone3_*`` LLM tools used to build their own
   ``PolicyEngine`` against ``PiholeController`` — a sinkhole never deployed on
   this appliance — while the HTTP routes talked to AdGuard. PuffPAI's spoken
   answer and her dashboard described different systems. There is now one
   factory, and a test that no Pi-hole construction survives in the tool layer.

2. **The enforce flag survives a restart.** ``POST /zone3/enforce`` changed the
   running object and nothing else, so an owner who turned filtering on lost it
   at the next power cut with no indication it had ever been on. The flag now
   lands in an owner overlay, exactly like the egress doors.

3. **The words reach the right tool.** "what devices are on my network" must
   reach the Bridge inventory, and telemetry-blocking language must reach the
   Zone 3 path.

Run: pytest tests/test_zone3_engine.py -q
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from puffpai import capability_registry as reg
from puffpai.zone3 import engine as z3


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSISTANT_TOOLS = REPO_ROOT / "src" / "puffpai" / "assistant_tools.py"


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """Point the enforce overlay at a temp file and clear the env tier."""
    monkeypatch.delenv(z3.ENFORCE_ENV, raising=False)
    path = tmp_path / "state" / "zone3_enforce.json"
    z3.set_overlay_path_for_tests(str(path))
    z3.reset_engine()
    try:
        yield path
    finally:
        z3.set_overlay_path_for_tests(None)
        z3.reset_engine()


# ── the overlay: precedence and durability ───────────────────────────────────

def test_overlay_roundtrips(overlay):
    assert z3.read_overlay() is None          # nothing written yet
    z3.write_overlay(True)
    assert overlay.exists()
    assert z3.read_overlay() is True
    z3.write_overlay(False)
    assert z3.read_overlay() is False


def test_overlay_file_is_owner_only(overlay):
    z3.write_overlay(True)
    if os.name != "nt":                        # Windows implements a subset
        assert (overlay.stat().st_mode & 0o777) == z3.OVERLAY_FILE_MODE


def test_overlay_records_what_it_is(overlay):
    z3.write_overlay(True)
    payload = json.loads(overlay.read_text(encoding="utf-8"))
    assert payload["kind"] == z3.OVERLAY_KIND
    assert payload["enforce"] is True
    assert "updated" in payload


def test_env_beats_overlay(overlay, monkeypatch):
    z3.write_overlay(False)
    monkeypatch.setenv(z3.ENFORCE_ENV, "1")
    assert z3.resolve_enforce() is True
    assert z3.enforce_source() == "env"


def test_overlay_beats_config(overlay, monkeypatch):
    monkeypatch.setattr(z3, "_config_enforce", lambda: False)
    z3.write_overlay(True)
    assert z3.resolve_enforce() is True
    assert z3.enforce_source() == "overlay"


def test_config_decides_when_nothing_else_does(overlay, monkeypatch):
    monkeypatch.setattr(z3, "_config_enforce", lambda: True)
    assert z3.resolve_enforce() is True
    assert z3.enforce_source() == "config"


def test_broken_overlay_falls_through_rather_than_reading_as_off(overlay,
                                                                monkeypatch):
    """A corrupt file must not be mistaken for a deliberate 'off'."""
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(z3, "_config_enforce", lambda: True)
    assert z3.read_overlay() is None
    assert z3.resolve_enforce() is True
    assert z3.enforce_source() == "config"


def test_unparseable_env_is_ignored_not_treated_as_off(overlay, monkeypatch):
    monkeypatch.setenv(z3.ENFORCE_ENV, "sure-why-not")
    z3.write_overlay(True)
    assert z3._env_override() is None
    assert z3.resolve_enforce() is True
    assert z3.enforce_source() == "overlay"


def test_default_is_observe_not_enforce(overlay, monkeypatch):
    """The fail-safe direction. An unreadable world means WATCHING, never
    silently filtering a household's DNS."""
    monkeypatch.setattr(z3, "_config_enforce", lambda: False)
    assert z3.resolve_enforce() is False


# ── the singleton ────────────────────────────────────────────────────────────

class _FakeAdGuard:
    def __init__(self, **kwargs):
        self.base_url = kwargs.get("base_url", "")
        self.enforce = bool(kwargs.get("enforce"))
        self.protection = None
        self.constructed_with = dict(kwargs)

    def set_protection(self, on):
        self.protection = bool(on)
        return True


class _FakePolicyEngine:
    def __init__(self, adguard=None, **_kwargs):
        self._adguard = adguard
        self.set_enforce_calls: list = []

    def set_enforce(self, enabled):
        self.set_enforce_calls.append(bool(enabled))
        self._adguard.enforce = bool(enabled)
        return {"success": True, "enforce": bool(enabled), "message": "ok"}


@pytest.fixture
def fake_zone3(monkeypatch, overlay):
    """A Zone 3 that is 'enabled' with a stand-in AdGuard, no network."""
    import puffpai.zone3 as zone3_pkg

    class _Cfg:
        class zone3:
            enabled = True
            adguard_url = "http://127.0.0.1:3000"
            adguard_username = "puff"
            adguard_password = "secret"
            adguard_enforce = False

    monkeypatch.setattr("puffpai.config.get_config", lambda: _Cfg())
    monkeypatch.setattr(zone3_pkg, "AdGuardController", _FakeAdGuard)
    monkeypatch.setattr(zone3_pkg, "PolicyEngine", _FakePolicyEngine)
    z3.reset_engine()
    yield
    z3.reset_engine()


def test_get_engine_is_a_singleton(fake_zone3):
    first = z3.get_engine()
    assert first is not None
    assert z3.get_engine() is first


def test_reset_engine_rebuilds(fake_zone3):
    first = z3.get_engine()
    z3.reset_engine()
    assert z3.get_engine() is not first


def test_engine_is_none_when_zone3_disabled(monkeypatch, overlay):
    class _Cfg:
        class zone3:
            enabled = False

    monkeypatch.setattr("puffpai.config.get_config", lambda: _Cfg())
    z3.reset_engine()
    assert z3.get_engine() is None


def test_enforce_is_baked_into_the_controller(fake_zone3):
    """Monitor-first is the state of the OBJECT, not a flag call sites check."""
    z3.write_overlay(True)
    z3.reset_engine()
    engine = z3.get_engine()
    assert engine._adguard.constructed_with["enforce"] is True


def test_the_credential_is_passed_but_never_logged(fake_zone3, caplog):
    with caplog.at_level("INFO"):
        z3.get_engine()
    assert "secret" not in caplog.text


# ── the switch persists ──────────────────────────────────────────────────────

def test_set_enforce_persistent_writes_the_overlay(fake_zone3):
    result = z3.set_enforce_persistent(True)
    assert result["success"] is True
    assert result["persistent"] is True
    assert z3.read_overlay() is True
    assert result["source"] == "overlay"


def test_enforcement_survives_a_restart(fake_zone3):
    """The whole point. Flip it on, drop the process's memory of it, and the
    rebuilt engine must still be enforcing."""
    z3.set_enforce_persistent(True)
    z3.reset_engine()                      # stands in for a service restart
    rebuilt = z3.get_engine()
    assert rebuilt._adguard.constructed_with["enforce"] is True
    assert z3.resolve_enforce() is True


def test_turning_it_off_also_persists(fake_zone3):
    z3.set_enforce_persistent(True)
    z3.set_enforce_persistent(False)
    z3.reset_engine()
    assert z3.get_engine()._adguard.constructed_with["enforce"] is False


def test_an_env_override_refuses_rather_than_writing_an_inert_flag(
        fake_zone3, monkeypatch):
    monkeypatch.setenv(z3.ENFORCE_ENV, "0")
    result = z3.set_enforce_persistent(True)
    assert result["success"] is False
    assert result["persistent"] is False
    assert z3.ENFORCE_ENV in result["message"]
    assert z3.read_overlay() is None        # nothing was written


def test_a_failed_write_is_reported_not_swallowed(fake_zone3, monkeypatch):
    def _boom(_value):
        raise OSError("read-only file system")
    monkeypatch.setattr(z3, "write_overlay", _boom)
    result = z3.set_enforce_persistent(True)
    assert result["success"] is False
    assert result["persistent"] is False
    assert "read-only file system" in result["message"]


def test_engine_is_disabled_means_no_silent_success(monkeypatch, overlay):
    class _Cfg:
        class zone3:
            enabled = False

    monkeypatch.setattr("puffpai.config.get_config", lambda: _Cfg())
    z3.reset_engine()
    result = z3.set_enforce_persistent(True)
    assert result["success"] is False
    assert result["persistent"] is False


# ── the tool layer no longer builds a Pi-hole ────────────────────────────────

def test_no_pihole_survives_in_the_tool_layer():
    source = ASSISTANT_TOOLS.read_text(encoding="utf-8")
    assert "PiholeController(" not in source, (
        "a zone3_* tool is building a Pi-hole again; this appliance runs "
        "AdGuard, so that tool would describe a system that does not exist")


def test_every_zone3_tool_goes_through_the_one_factory():
    source = ASSISTANT_TOOLS.read_text(encoding="utf-8")
    assert "from puffpai.zone3.engine import get_engine" in source


# ── routing: the words reach the right tool ──────────────────────────────────

def _declared_tool_names() -> set:
    """Tool names actually defined in the tool layer, read from source.

    Reading the source rather than instantiating ``AssistantTools`` keeps this test
    free of an Ollama connection while still failing if a declared tool is
    renamed out from under the registry.
    """
    source = ASSISTANT_TOOLS.read_text(encoding="utf-8")
    return set(re.findall(r"^\s+def (\w+)\(", source, re.MULTILINE))


def test_the_bridge_and_zone3_capabilities_declare_only_tools_that_exist():
    """Scoped to bridge and zone3 capabilities.

    A whole-registry ``validate`` would be the better test and is not usable
    here: four capabilities (``home.onboarding``, ``utility.registry_read``,
    ``utility.documents``, ``utility.exec``) declare tools defined outside
    ``assistant_tools``, the Toolsmith tier and the HA onboarding path, which this
    source scrape cannot see. Pinning those would report an
    unrelated condition as this test's failure. What matters here is that every
    Bridge and Zone 3 tool named in the registry really is a tool.
    """
    live = _declared_tool_names()
    for cap_id in ("bridge.inventory", "security.zone3"):
        cap = reg.get(cap_id)
        assert cap is not None, cap_id
        missing = [t for t in cap.all_tools if t not in live]
        assert missing == [], f"{cap_id} declares non-existent {missing}"


def test_every_new_tool_is_registered_not_just_defined():
    """A tool absent from the returned list is unreachable — the brain can
    never bind it. Defining one and forgetting the list is the standing way a
    PuffPAI tool ships dead."""
    source = ASSISTANT_TOOLS.read_text(encoding="utf-8")
    for name in ("zone3_enforce", "bridge_list_devices",
                 "bridge_device_telemetry", "bridge_status", "bridge_refresh"):
        assert re.search(rf"^\s+{name},\s*$", source, re.MULTILINE), (
            f"{name} is defined but not in the returned tool list")


@pytest.mark.parametrize("said", [
    "what devices are on my network",
    "what's on my network",
    "list my devices",
    "what is my tv talking to",
    "any new devices on my network",
])
def test_network_roll_questions_bind_the_bridge_inventory(said):
    bound = reg.tools_for_query(said, _declared_tool_names())
    assert "bridge_list_devices" in bound, (said, bound)


def test_the_plain_question_dispatches_straight_to_the_roll():
    """Whole-utterance dispatch: this sentence IS the request, so it is
    answered by the authority tool rather than left to the model."""
    cap = reg.find_for_dispatch("what devices are on my network")
    assert cap is not None and cap.id == "bridge.inventory"
    assert cap.authority_tool == "bridge_list_devices"


def test_a_sentence_that_merely_mentions_the_roll_does_not_dispatch():
    """The containment bug this layer exists to avoid."""
    assert reg.find_for_dispatch(
        "how do i ask what devices are on my network in spanish") is None


@pytest.mark.parametrize("said", [
    "block my samsung tv telemetry",
    "block my tv's telemetry",
    "turn on enforcement",
    "stop my thermostat from phoning home",
])
def test_telemetry_blocking_language_binds_the_zone3_path(said):
    bound = reg.tools_for_query(said, _declared_tool_names())
    assert "zone3_enforce" in bound or "zone3_secure_device" in bound, (said, bound)


def test_the_enforcement_switch_is_never_auto_dispatched():
    """A state-changing capability must not be answerable by a read dispatch.
    ``security.zone3`` declares no dispatch_phrases; the model has to choose the
    tool, and the tool only proposes."""
    cap = reg.get("security.zone3")
    assert cap is not None
    assert cap.dispatch_phrases == ()


def test_the_bridge_roll_is_declared_read_only():
    cap = reg.get("bridge.inventory")
    assert cap is not None
    assert cap.access == reg.READ
    assert cap.agent == "sentinel"


def test_zone3_is_drawbridges():
    assert reg.get("security.zone3").agent == "sentinel"


# ── the HTTP gate: the switch is not reachable with the API key alone ────────
#
# These use the same lazy-import fixture pattern as tests/test_api_server.py.
# The point they pin is narrow and load-bearing: an unticketed POST to a Zone 3
# write must change NOTHING and answer 409 with a proposal. The API key is held
# by the dashboard, the voice service and every LLM tool in the appliance, so a
# key-only gate would put the household's DNS filter inside reach of one
# poisoned tool call.

@pytest.fixture(scope="module")
def api_mod():
    import importlib
    import sys
    from unittest.mock import MagicMock
    from tests.modpatch import patched_modules  # type: ignore

    # api_server does `from app_data import ...` at module level, so it is
    # importable only with src/puffpai itself on the path. Same setup as
    # tests/test_api_server.py.
    src_dir = str(REPO_ROOT / "src" / "puffpai")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod


@pytest.fixture
def api_client(api_mod, fake_zone3):
    api_mod._rate_limit_store.clear()
    with api_mod.app.test_client() as c:
        yield c, api_mod._API_KEY, api_mod


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_enforce_without_a_ticket_is_refused_with_a_proposal(api_client):
    client, key, _mod = api_client
    res = client.post("/zone3/enforce", json={"enabled": True},
                      headers=_auth(key))
    assert res.status_code == 409
    body = res.get_json()
    assert body["needs_confirmation"] is True
    assert body["confirm_endpoint"] == "/zone3/confirm"
    assert body["proposal"]["headline"]
    # And nothing happened.
    assert z3.read_overlay() is None
    assert z3.resolve_enforce() is False


def test_minting_a_ticket_actuates_nothing(api_client):
    client, key, _mod = api_client
    res = client.post("/zone3/confirm",
                      json={"kind": "zone3_enforce", "target": "zone3",
                            "action": "on"},
                      headers=_auth(key))
    assert res.status_code == 201
    assert res.get_json()["ticket"]
    assert z3.read_overlay() is None       # minting is side-effect free
    assert z3.resolve_enforce() is False


def test_a_ticket_turns_it_on_and_records_it(api_client):
    client, key, _mod = api_client
    mint = client.post("/zone3/confirm",
                       json={"kind": "zone3_enforce", "target": "zone3",
                             "action": "on"},
                       headers=_auth(key)).get_json()
    res = client.post("/zone3/enforce",
                      json={"enabled": True, "ticket": mint["ticket"]},
                      headers=_auth(key))
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["success"] is True
    assert body["persistent"] is True
    assert z3.read_overlay() is True


def test_a_ticket_is_single_use(api_client):
    client, key, _mod = api_client
    mint = client.post("/zone3/confirm",
                       json={"kind": "zone3_enforce", "target": "zone3",
                             "action": "on"},
                       headers=_auth(key)).get_json()
    first = client.post("/zone3/enforce",
                        json={"enabled": True, "ticket": mint["ticket"]},
                        headers=_auth(key))
    assert first.status_code == 200
    second = client.post("/zone3/enforce",
                         json={"enabled": True, "ticket": mint["ticket"]},
                         headers=_auth(key))
    assert second.status_code == 409


def test_a_ticket_for_on_cannot_turn_it_off(api_client):
    """The binding is the whole point: a real ticket for a different act is
    refused, not honoured."""
    client, key, _mod = api_client
    mint = client.post("/zone3/confirm",
                       json={"kind": "zone3_enforce", "target": "zone3",
                             "action": "on"},
                       headers=_auth(key)).get_json()
    res = client.post("/zone3/enforce",
                      json={"enabled": False, "ticket": mint["ticket"]},
                      headers=_auth(key))
    assert res.status_code == 409


def test_a_forged_ticket_is_refused(api_client):
    client, key, _mod = api_client
    res = client.post("/zone3/enforce",
                      json={"enabled": True, "ticket": "not-a-real-ticket"},
                      headers=_auth(key))
    assert res.status_code == 409
    assert z3.read_overlay() is None


def test_an_ha_ticket_cannot_redeem_against_zone3(api_client):
    """Two gates, one ticket store — the kinds must not be interchangeable."""
    client, key, _mod = api_client
    mint = client.post("/ha/confirm",
                       json={"kind": "control", "entity_id": "zone3",
                             "action": "on"},
                       headers=_auth(key))
    assert mint.status_code == 201
    res = client.post("/zone3/enforce",
                      json={"enabled": True,
                            "ticket": mint.get_json()["ticket"]},
                      headers=_auth(key))
    assert res.status_code == 409


def test_zone3_kinds_are_refused_by_the_ha_mint(api_client):
    client, key, _mod = api_client
    res = client.post("/ha/confirm",
                      json={"kind": "zone3_enforce", "entity_id": "zone3",
                            "action": "on"},
                      headers=_auth(key))
    assert res.status_code == 400


def test_securing_a_device_is_gated_too(api_client):
    client, key, _mod = api_client
    res = client.post("/zone3/secure",
                      json={"ip": "192.168.50.52", "mac": "00:00:5E:00:53:52"},
                      headers=_auth(key))
    assert res.status_code == 409
    assert res.get_json()["proposal"]["kind"] == "zone3_secure"


def test_changing_a_mode_is_gated_too(api_client):
    client, key, _mod = api_client
    res = client.post("/zone3/policy",
                      json={"ip": "192.168.50.52", "mac": "00:00:5E:00:53:52",
                            "mode": "lockdown"},
                      headers=_auth(key))
    assert res.status_code == 409
    body = res.get_json()
    assert body["proposal"]["kind"] == "zone3_mode"
    assert body["proposal"]["action"] == "lockdown"
