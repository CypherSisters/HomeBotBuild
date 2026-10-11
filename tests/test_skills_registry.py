"""An installed skill as the rest of the appliance sees it: rows, doors, status.

Three properties:

* an installed skill adds a capability row that ``get``, ``find_for_dispatch``
  and ``validate`` all see -- a row the registry cannot look up is a capability
  that dispatches but cannot be attributed or probed;
* switching a skill off, or tampering with its files, takes it OFFLINE
  immediately (the probe, not the declaration, is the fresher answer);
* app doors and skill doors are independent tables. ``register_app_doors``
  clears ``_APP_DOORS`` wholesale, so sharing one table would mean an app
  rescan silently deleting a skill's door -- and leaving the owner's ``true``
  resolving against nothing.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai import capability_probes as probes                # noqa: E402
from puffpai import capability_registry as cr                  # noqa: E402
from puffpai.security import egress_doors                      # noqa: E402
from puffpai.skills import manifest as skill_manifest          # noqa: E402
from puffpai.skills import store                               # noqa: E402
from skillkit import make_draft                                # noqa: E402

SKILL_ID = "iss_position"
CAP_ID = f"skill.{SKILL_ID}"


def install_directly(tested=True, enabled=True):
    """Put a valid, approved-looking skill on disk without the consent dance."""
    folder = store.skill_dir(SKILL_ID)
    make_draft(folder)
    _clean, shas = skill_manifest.validate_dir(folder)
    with open(os.path.join(folder, "installed.json"), "w", encoding="utf-8") as h:
        json.dump({"approved_at": "2026-09-16T00:00:00+00:00",
                   "approved_by": "user_via_ui", "sha256": shas, "version": 1,
                   "hosts": ["api.wheretheiss.at"]}, h)
    store.write_state(SKILL_ID, dict(store.DEFAULT_STATE, enabled=enabled,
                                     last_test={"ok": tested, "at": "now",
                                                "log_tail": ""}))
    store.rescan()
    return folder


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    store.set_root(str(tmp_path / "skills"))
    egress_doors.clear_skill_doors()
    egress_doors.clear_app_doors()
    cr.clear_skill_overlay()
    probes.reset_cache()
    yield
    store.set_root(None)
    egress_doors.clear_skill_doors()
    egress_doors.clear_app_doors()
    cr.clear_skill_overlay()
    probes.reset_cache()


# ── the row ─────────────────────────────────────────────────────────

def test_an_installed_skill_appears_as_a_capability():
    before = len(cr.all_capabilities())
    install_directly()
    rows = cr.all_capabilities()
    assert len(rows) == before + 1
    row = cr.get(CAP_ID)
    assert row is not None, "get() cannot see an installed skill"
    assert row.domain == "skills"
    assert row.origin == f"skill:{SKILL_ID}"
    assert row.agent == "toolsmith"
    assert "api.wheretheiss.at" in row.data_source


def test_the_row_binds_no_tools_and_offers_no_lever_to_the_model():
    install_directly()
    row = cr.get(CAP_ID)
    assert row.all_tools == (), (
        "a skill row named a tool; Phase 1 ships no run_skill tool and a row "
        "that names a missing tool makes validate() report the registry as "
        "dishonest -- correctly")
    assert row.keywords == (), (
        "a skill row carries keywords, which would widen which tools the model "
        "is offered for unrelated queries")


def test_validate_stays_clean_with_a_skill_installed():
    names = {"ha_control_device", "web_search"}
    before = cr.validate(names)
    install_directly()
    after = cr.validate(names)
    assert after == before, [p for p in after if p not in before]


def test_the_trigger_dispatches_and_only_as_a_whole_utterance():
    install_directly()
    assert cr.find_for_dispatch("where is the iss").id == CAP_ID
    assert cr.find_for_dispatch("Where is the ISS?").id == CAP_ID
    assert cr.find_for_dispatch("i wonder where is the iss these days") is None


def test_the_skills_manage_row_exists_for_attribution():
    row = cr.get("skills.manage")
    assert row is not None and row.domain == "skills"
    assert row.agent == "toolsmith"
    from puffpai.chat_activity import FASTPATH_CAPABILITY
    assert FASTPATH_CAPABILITY["_try_skill_dispatch"] == "skills.manage"
    assert FASTPATH_CAPABILITY["_try_capability_gap"] == "introspection.capabilities"


# ── status follows the probe, not the declaration ───────────────────

@pytest.fixture
def subsystem_on(monkeypatch):
    """Answer the subsystem question yes, so the PER-SKILL reasons are what
    these tests are reading. ``skills.enabled`` ships false by default,
    and that is its own test below."""
    from puffpai.skills import dispatch
    monkeypatch.setattr(dispatch, "available", lambda: (True, ""))
    probes.reset_cache()
    yield
    probes.reset_cache()


def test_the_kill_switch_alone_takes_every_skill_offline():
    """skills.enabled ships false: an installed skill is listed and OFFLINE."""
    install_directly()
    probes.reset_cache()
    row = cr.get(CAP_ID)
    assert cr.runtime_status(row) == cr.OFFLINE
    available, reason = cr.runtime_availability(row)
    assert available is False and "skills.enabled" in reason


def test_the_env_switch_wins_over_the_config(monkeypatch):
    from puffpai.skills import dispatch
    monkeypatch.setenv(dispatch.DISABLE_ENV, "1")
    monkeypatch.setattr("puffpai.config.get_config",
                        lambda: type("C", (), {"skills": type("S", (), {
                            "enabled": True})()})())
    assert dispatch.enabled() is False
    usable, reason = dispatch.available()
    assert usable is False and dispatch.DISABLE_ENV in reason


def test_a_disabled_skill_is_offline(subsystem_on):
    install_directly()
    store.set_enabled(SKILL_ID, False)
    probes.reset_cache()
    row = cr.get(CAP_ID)
    assert cr.runtime_status(row) == cr.OFFLINE
    available, reason = cr.runtime_availability(row)
    assert available is False and "switched this skill off" in reason


def test_a_tampered_skill_is_offline_and_says_why(subsystem_on):
    folder = install_directly()
    with open(os.path.join(folder, "skill.py"), "a", encoding="utf-8") as handle:
        handle.write("\n# not the code you approved\n")
    store.rescan()
    probes.reset_cache()
    row = cr.get(CAP_ID)
    assert row is not None
    assert cr.runtime_status(row) == cr.OFFLINE
    available, reason = cr.runtime_availability(row)
    assert available is False and "approved" in reason


def test_an_untested_skill_is_offline(subsystem_on):
    install_directly(tested=False)
    probes.reset_cache()
    assert cr.runtime_status(cr.get(CAP_ID)) == cr.OFFLINE


# ── doors ───────────────────────────────────────────────────────────

def test_installing_registers_a_shut_door_for_the_declared_host():
    install_directly()
    door_id = egress_doors.skill_door_id(SKILL_ID, 0)
    door = egress_doors.door(door_id)
    assert door is not None
    assert door.hosts == ("api.wheretheiss.at",)
    assert door.default is False
    allowed, reason = egress_doors.egress_allowed(door_id)
    assert allowed is False, "a skill door opened without the owner"
    assert door in egress_doors.all_doors()
    assert egress_doors.skill_of(door) == SKILL_ID


def test_removing_a_skill_drops_its_row_and_its_door():
    install_directly()
    assert cr.get(CAP_ID) is not None
    assert store.remove(SKILL_ID) is True
    assert cr.get(CAP_ID) is None
    assert egress_doors.door(egress_doors.skill_door_id(SKILL_ID, 0)) is None
    assert cr.find_for_dispatch("where is the iss") is None
    # the code is set aside, not deleted -- but nothing reads it back
    assert any(p.startswith(SKILL_ID) for p in os.listdir(store.removed_root()))
    assert store.installed_ids() == []


def test_an_app_rescan_leaves_skill_doors_standing():
    """``register_app_doors`` clears its own table wholesale. Two tables."""
    install_directly()
    skill_door = egress_doors.skill_door_id(SKILL_ID, 0)
    egress_doors.register_app_doors([("weather", [{"hosts": ["api.weather.example"],
                                                   "purpose": "p"}])])
    assert egress_doors.door(skill_door) is not None, (
        "an app rescan deleted a skill's door")
    assert egress_doors.door("app:weather:0") is not None


def test_a_skill_rescan_leaves_app_doors_standing():
    egress_doors.register_app_doors([("weather", [{"hosts": ["api.weather.example"],
                                                   "purpose": "p"}])])
    install_directly()
    assert egress_doors.door("app:weather:0") is not None, (
        "a skill rescan deleted an app's door")
    assert egress_doors.door(egress_doors.skill_door_id(SKILL_ID, 0)) is not None
    assert len(egress_doors.all_doors()) == len(egress_doors.DOORS) + 2


def test_a_skill_door_id_cannot_shadow_a_built_in_one():
    egress_doors.register_skill_doors([("web_search", [{"hosts": ["x.example.com"]}])])
    assert egress_doors.door("web_search").hosts != ("x.example.com",)


def test_the_flag_namespace_is_separate_from_the_app_one():
    app_flag = egress_doors.app_door_flag("iss_position", 0, ["api.wheretheiss.at"])
    skill_flag = egress_doors.skill_door_flag("iss_position", 0,
                                              ["api.wheretheiss.at"])
    assert app_flag.startswith("app_") and skill_flag.startswith("skill_")
    assert app_flag != skill_flag, (
        "an app and a skill with the same id would share one consent flag")


def test_a_skill_that_declares_no_host_gets_no_door():
    folder = store.skill_dir(SKILL_ID)
    make_draft(folder, manifest_patch={"permissions": {
        "network_hosts": [], "files": [], "ha_domains": [], "secrets": []}},
        fixtures={"responses": {"https://api.wheretheiss.at/v1/x": {"json": {}}},
                  "cases": [{"inputs": {},
                             "expect": {"speech_contains": ["x"],
                                        "data": {"y": 1}}}]})
    assert store.door_entries() == []


# ── a rescan is also the flag collector ───────────────────
#
# Unregistering a door is not the same as forgetting the owner's ``true`` for
# it. ``rescan()`` prunes the skill flag namespace down to exactly what
# installed, ENABLED skills are entitled to, so a flag can never outlive the
# skill it belonged to and wait for the next door of that name.

@pytest.fixture
def own_overlay(tmp_path):
    egress_doors.set_overlay_override(str(tmp_path / "state" / "egress_doors.json"))
    egress_doors.reset_cache()
    yield
    egress_doors.set_overlay_override(None)
    egress_doors.reset_cache()


def open_the_door():
    from puffpai.security import egress_admin

    egress_admin.set_door_open(egress_doors.skill_door_id(SKILL_ID, 0), True)


def skill_flags():
    path = Path(egress_doors.overlay_path())
    if not path.exists():
        return {}
    doors = json.loads(path.read_text(encoding="utf-8")).get("doors", {})
    return {k: v for k, v in doors.items() if k.startswith("skill_")}


def test_rescan_leaves_an_enabled_skills_flag_alone(own_overlay):
    install_directly()
    open_the_door()
    flag = egress_doors.skill_door_flag(SKILL_ID, 0, ["api.wheretheiss.at"])
    assert skill_flags() == {flag: True}
    store.rescan()
    store.rescan()
    assert skill_flags() == {flag: True}, "a rescan closed a live skill's door"
    allowed, why = egress_doors.egress_allowed(
        egress_doors.skill_door_id(SKILL_ID, 0))
    assert allowed is True, why


def test_rescan_clears_the_flag_of_a_skill_that_is_gone(own_overlay):
    import shutil

    install_directly()
    open_the_door()
    assert skill_flags()
    shutil.rmtree(store.skill_dir(SKILL_ID))
    store.rescan()
    assert skill_flags() == {}, (
        "a removed skill's consent survived the rescan that removed its door")
    # Idempotent: a second rescan with nothing to do writes nothing and fails
    # nothing.
    store.rescan()
    assert skill_flags() == {}


def test_rescan_clears_the_flag_of_a_skill_that_is_switched_off(own_overlay):
    install_directly()
    open_the_door()
    assert skill_flags()
    store.set_enabled(SKILL_ID, False, reason="you asked me to")
    assert skill_flags() == {}
    allowed, _why = egress_doors.egress_allowed(
        egress_doors.skill_door_id(SKILL_ID, 0))
    assert allowed is False, "a switched-off skill kept an open door"


def test_a_stale_flag_for_an_unrelated_skill_is_pruned_too(own_overlay):
    """An orphan found on deploy: a key from a skill that is not on
    this box at all. Nothing can consult it, and it is still a stored yes."""
    from puffpai.security import egress_admin

    install_directly()
    open_the_door()
    live = egress_doors.skill_door_flag(SKILL_ID, 0, ["api.wheretheiss.at"])
    orphan = "skill_gone_away_0_deadbeef"
    path = Path(egress_doors.overlay_path())
    doors = json.loads(path.read_text(encoding="utf-8"))["doors"]
    doors[orphan] = True
    egress_admin._atomic_json_write(
        str(path), {"version": 1, "kind": egress_doors.OVERLAY_KIND,
                    "doors": doors},
        egress_doors.OVERLAY_FILE_MODE, egress_doors.OVERLAY_DIR_MODE)
    egress_doors.reset_cache()
    assert orphan in skill_flags()

    store.rescan()
    assert skill_flags() == {live: True}


def test_clear_skill_flags_refuses_an_unusable_id(own_overlay):
    """A prefix delete's failure mode is deleting too much: a junk id matches
    nothing rather than a namespace."""
    from puffpai.security import egress_admin

    install_directly()
    open_the_door()
    before = skill_flags()
    assert before
    for junk in ("", "   ", "*", "../..", "Iss Position", None):
        assert egress_admin.clear_skill_flags(junk) == 0
    assert skill_flags() == before
