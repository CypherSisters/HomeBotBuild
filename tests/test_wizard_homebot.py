"""What the HomeBot wizard promises that BuildBot's did not.

Persona defaults, the optional Home Assistant path (both branches), validated
storage, the household profiles step, a never-empty surface allowlist, and the
manifest's contract guard surviving the port.

Every test runs against a temporary data dir and config dir, so nothing here
touches an installed build. No network, no Ollama, no services.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A whole install's worth of writable state, isolated to this test."""
    import puffpai.config as config

    data_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(config_dir))
    config.get_config(_force_reload=True)
    yield {"data": data_dir, "config": config_dir}
    config.get_config(_force_reload=True)


# ── persona defaults ────────────────────────────────────────────────────────

def test_persona_is_preselected_and_renameable(scratch):
    """This is HomeBot's wizard, so the persona arrives filled in."""
    from puffpai import setup_ops

    defaults = setup_ops.homebot_defaults()
    assert defaults["persona"] == "homebot"
    assert defaults["bot_name"] == "HomeBot"
    assert defaults["pack"] == "homebot"
    assert defaults["goal"].strip(), "the goal step is pre-filled, not blank"

    # ...and it is a default, not a lock.
    setup_ops.set_identity("Bramble")
    from puffpai.config import get_config
    assert get_config(_force_reload=True).identity.name == "Bramble"


def test_the_default_pack_and_the_apps_it_names_are_actually_here():
    """A preselected pack that is not in the tree is a promise, not a default."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pack = os.path.join(repo, "packs", "homebot", "pack.json")
    assert os.path.isfile(pack)
    installed = set(os.listdir(os.path.join(repo, "apps")))
    assert {"calendar", "freshtable", "alarmflow"} <= installed


def test_the_default_goal_matches_real_household_capabilities(scratch):
    """The pre-filled goal has to actually select something from the catalog."""
    from puffpai import setup_ops

    proposal = setup_ops.build_interview(
        setup_ops.HOMEBOT_DEFAULTS["goal"], setup_ops.hardware_census())
    assert proposal.get("flat"), "the shipped goal matched nothing in the catalog"


# ── surfaces: absence is not "configured to zero" ───────────────────────────

def test_surfaces_never_come_back_empty_by_accident(scratch):
    """A goal that matched nothing yields the household defaults, marked so."""
    from puffpai import setup_ops

    empty = {"goal": "", "surfaces": [], "reasons": {}, "sensitive": []}
    settled = setup_ops.ensure_surfaces(empty)
    assert settled["surfaces"], "an empty allowlist would refuse everything"
    assert settled["defaulted"] is True
    assert all(settled["reasons"][s] for s in settled["surfaces"])


def test_an_owner_who_removed_everything_is_left_alone(scratch):
    """A configured zero is a decision; only ABSENCE gets the defaults."""
    from puffpai import setup_ops

    chosen_empty = {"goal": "x", "surfaces": [], "reasons": {},
                    "owner_edited": True}
    assert setup_ops.ensure_surfaces(chosen_empty)["surfaces"] == []


def test_default_surfaces_are_all_real_surfaces(scratch):
    """Nothing outside the catalog's taxonomy can reach the allowlist."""
    from puffpai import build_interview, setup_ops

    taxonomy = build_interview.surface_taxonomy()
    settled = setup_ops.default_surfaces_derivation()
    assert settled["surfaces"]
    assert all(s in taxonomy for s in settled["surfaces"])


# ── Home Assistant is optional ──────────────────────────────────────────────

def test_home_assistant_absent_is_a_complete_build(scratch):
    """A "no" is recorded as a stated absence and nothing nags afterwards."""
    from puffpai import setup_ops

    block = setup_ops.ha_block(present=False)
    assert block["present"] is False
    assert block["url"] == ""
    assert block["token_configured"] is False
    integrations = setup_ops.integrations_block(block)
    assert integrations["home_assistant"]["present"] is False


def test_home_assistant_present_never_puts_the_token_in_the_manifest(scratch):
    """The token goes to the HA config file and nowhere else."""
    from puffpai import build_manifest, setup_ops

    token = "FAKE-HA-TOKEN-0d1e2f3a4b5c"
    # posture='island' stated explicitly. A Home Assistant on another box IS
    # the island posture; the write path now defaults to 'sealed' when nothing
    # has declared one (there is no manifest in a scratch dir), and that
    # refusal has its own test in tests/test_lan_posture_wiring.py. This test
    # is about where the token lands, so it declares the posture rather than
    # testing the posture.
    setup_ops.set_ha_config("http://homeassistant.local:8123", token,
                            posture="island")

    ha = setup_ops.ha_block(present=True, url=setup_ops.ha_url(),
                            token_configured=True)
    manifest = build_manifest.assemble_manifest(
        setup_ops.hardware_census(),
        integrations=setup_ops.integrations_block(ha))
    path = build_manifest.write_manifest(manifest)

    assert ha["token_configured"] is True
    assert token not in json.dumps(manifest)
    with open(path, encoding="utf-8") as fh:
        assert token not in fh.read()

    # It did reach the one file that is supposed to hold it.
    ha_file = os.path.join(str(scratch["config"]), "home_assistant_config.json")
    with open(ha_file, encoding="utf-8") as fh:
        assert token in fh.read()


def test_the_ha_default_url_is_the_one_people_actually_have():
    from puffpai import setup_ops
    assert setup_ops.DEFAULT_HA_URL == "http://homeassistant.local:8123"


# ── storage ─────────────────────────────────────────────────────────────────

def test_storage_defaults_live_under_this_install(scratch):
    from puffpai import setup_ops

    defaults = setup_ops.storage_defaults()
    root = str(scratch["data"])
    assert defaults["state_dir"].startswith(root)
    assert defaults["knowledge_dir"].startswith(root)
    assert defaults["vault_dir"].startswith(root)
    # A household with no media library is a normal household.
    assert defaults["media_dir"] == ""


@pytest.mark.parametrize("value,ok", [
    ("relative/path", False),
    ("", False),
])
def test_storage_paths_are_validated(value, ok, scratch):
    from puffpai import setup_ops
    assert setup_ops.validate_storage_path(value)[0] is ok


def test_a_creatable_absolute_path_is_accepted_and_a_file_is_not(scratch, tmp_path):
    from puffpai import setup_ops

    assert setup_ops.validate_storage_path(str(tmp_path / "not-yet"))[0] is True
    a_file = tmp_path / "a_file.txt"
    a_file.write_text("x", encoding="utf-8")
    ok, reason = setup_ops.validate_storage_path(str(a_file))
    assert ok is False and "file" in reason


def test_media_may_be_empty_but_nothing_else_may(scratch):
    from puffpai import setup_ops
    assert setup_ops.validate_storage_path("", allow_empty=True)[0] is True


def test_apply_storage_writes_the_keys_this_build_reads(scratch, tmp_path):
    from puffpai import setup_ops
    from puffpai.config import get_config

    answers = {
        "state_dir": str(tmp_path / "s"),
        "knowledge_dir": str(tmp_path / "k"),
        "vault_dir": str(tmp_path / "v"),
        "media_dir": "",
    }
    result = setup_ops.apply_storage(answers)
    assert result["ok"] is True
    cfg = get_config(_force_reload=True)
    assert cfg.paths.state_dir == answers["state_dir"]
    assert cfg.paths.vault_data_dir == answers["vault_dir"]
    assert cfg.paths.zim_dirs == [answers["knowledge_dir"]]
    for key in ("state_dir", "knowledge_dir", "vault_dir"):
        assert os.path.isdir(answers[key]), f"{key} was not created"


def test_bad_storage_writes_nothing_at_all(scratch, tmp_path):
    """Half-applied storage is worse than refused storage."""
    from puffpai import setup_ops

    before = setup_ops._read_json(setup_ops._config_path())
    result = setup_ops.apply_storage({
        "state_dir": "still/relative",
        "knowledge_dir": str(tmp_path / "k"),
        "vault_dir": str(tmp_path / "v"),
        "media_dir": "",
    })
    assert result["ok"] is False
    assert "state_dir" in result["problems"]
    assert setup_ops._read_json(setup_ops._config_path()) == before


def test_storage_block_distinguishes_no_media_from_unanswered(scratch):
    from puffpai import setup_ops

    block = setup_ops.storage_block({"state_dir": "/s", "media_dir": ""})
    assert block["media_library"] == ""
    assert block["media_library_present"] is False


# ── household profiles ──────────────────────────────────────────────────────

def test_the_profiles_step_creates_an_owner(scratch):
    from puffpai import setup_ops

    assert setup_ops.household_status()["count"] == 0
    state = setup_ops.create_household("Rowan", [{"name": "Wren"}])
    assert state["owner"] == "Rowan"
    assert "Wren" in state["names"]
    assert setup_ops.household_block(state) == {"profiles": 2, "owner_set": True}


def test_the_owner_profile_lands_in_this_builds_own_store(scratch):
    from puffpai import profile_manager, setup_ops

    setup_ops.create_household("Rowan")
    with open(profile_manager.profiles_path(), encoding="utf-8") as fh:
        stored = json.load(fh)
    names = [p.get("display_name") for p in stored.get("profiles", [])]
    assert "Rowan" in names


# ── the vault seam ──────────────────────────────────────────────────────────

def test_the_wizards_pin_floor_is_stricter_than_the_vaults(scratch):
    """The wizard may be stricter than the vault; never laxer."""
    from puffpai import setup_ops, zone_vault
    from puffpai.security.vault import Vault, VaultError

    # The vault's own floor is four characters. The wizard refuses that.
    assert setup_ops.validate_vault_pin("1234")[0] is False
    assert zone_vault._MIN_PIN_LENGTH >= 8

    # Anything the wizard accepts, the vault accepts.
    pin = "7hx2Qm4T"
    assert setup_ops.validate_vault_pin(pin)[0] is True
    handle = Vault(vault_dir=str(scratch["data"] / "vault_probe"))
    handle.initialize(pin)   # must not raise
    assert handle.is_initialized
    with pytest.raises(VaultError):
        handle.initialize(pin)


def test_sealing_the_vault_issues_a_code_and_records_neither_secret(scratch):
    from puffpai import setup_ops

    block, code = setup_ops.seal_vault_with_recovery("7hx2Qm4T")
    assert block["recovery_issued"] is True
    assert code and code not in json.dumps(block)
    assert "7hx2Qm4T" not in json.dumps(block)
    assert setup_ops.vault_initialized() is True
    # The manifest describes THIS build's primitives, not BuildBot's.
    assert block["key_derivation"] == "scrypt"
    assert block["cipher"] == "aes-256-gcm"


# ── the manifest's contracts cannot be switched off ─────────────────────────

def test_contracts_survive_the_port_and_cannot_be_disabled(scratch):
    from puffpai import build_manifest, setup_ops

    manifest = build_manifest.assemble_manifest(setup_ops.hardware_census())
    assert manifest["contracts"], "the contract block is not optional"

    # Every route into the manifest carries the same guard.
    forged = dict(manifest)
    forged["contracts"] = {"disable_vault": True}
    forged["surfaces"] = dict(forged["surfaces"], allow_all_egress=True)
    forged["integrations"] = {"home_assistant": {"disable_consent": True}}

    clean = build_manifest.sanitize_manifest(forged)
    assert clean["contracts"] == manifest["contracts"]
    assert "allow_all_egress" not in json.dumps(clean)
    assert "disable_consent" not in json.dumps(clean)
    assert build_manifest.validate_manifest(forged), \
        "a forged manifest must produce warnings, not silence"


def test_a_written_manifest_is_the_sanitized_one(scratch):
    from puffpai import build_manifest, setup_ops

    manifest = build_manifest.assemble_manifest(setup_ops.hardware_census())
    manifest["egress"] = {"posture": "island", "bypass_egress": True}
    path = build_manifest.write_manifest(manifest)
    with open(path, encoding="utf-8") as fh:
        assert "bypass_egress" not in fh.read()


# ── the bridge posture maps onto the real door registry ─────────────────────

def test_the_egress_table_is_the_door_registry(scratch):
    from puffpai import build_posture
    from puffpai.security import egress_doors

    ids = {row["id"] for row in build_posture.egress_switches()}
    assert ids == {d.id for d in egress_doors.DOORS}
    assert all(row["enabled"] is False for row in build_posture.egress_switches())


def test_opening_a_door_in_the_wizard_opens_the_flag_the_runtime_reads(scratch):
    from puffpai import setup_ops
    from puffpai.security import egress_doors

    posture = setup_ops.assess_posture("online", "enumerated", ["weather"])
    setup_ops.persist_posture(posture)
    written = setup_ops._read_json(setup_ops._config_path())["egress_doors"]

    weather = egress_doors.door("weather")
    assert written[weather.config_flag] is True
    # Everything else is written False EXPLICITLY - a table that lists only
    # what is open cannot be read as a statement about what is shut.
    for d in egress_doors.DOORS:
        if d.id != "weather":
            assert written[d.config_flag] is False


def test_a_posture_the_wizard_cannot_apply_is_recorded_not_half_applied(scratch):
    from puffpai import setup_ops

    setup_ops.record_pending_apply("bridge_consent_tier", {"needs": "x"})
    pending = setup_ops.pending_apply()
    assert [p["kind"] for p in pending] == ["bridge_consent_tier"]
    # Recording twice does not stack duplicates.
    setup_ops.record_pending_apply("bridge_consent_tier", {"needs": "y"})
    assert len(setup_ops.pending_apply()) == 1


# ── the step list ───────────────────────────────────────────────────────────

def test_every_step_is_numbered_and_ascii():
    from puffpai import setup_cli

    assert len(setup_cli.SETUP_STEPS) == len(set(setup_cli.SETUP_STEPS))
    for name in setup_cli.SETUP_STEPS:
        assert name.isascii()
        assert setup_cli.step_banner(name).startswith("Step ")
    assert "Home Assistant" in setup_cli.SETUP_STEPS
    # Home Assistant is asked BEFORE the Bridge: it changes the suggestion.
    assert (setup_cli.SETUP_STEPS.index("Home Assistant")
            < setup_cli.SETUP_STEPS.index("Zone 3, the Bridge"))
    # All three zones, always.
    for zone in ("Zone 1, the Vault", "Zone 2, the Brain", "Zone 3, the Bridge"):
        assert zone in setup_cli.SETUP_STEPS


def test_a_renamed_step_is_a_failure_here_not_a_wrong_number_on_screen():
    from puffpai import setup_cli
    with pytest.raises(ValueError):
        setup_cli.enter_step("A step nobody declared")


def test_the_setup_verb_is_registered_without_disturbing_the_others():
    from puffpai import cli

    parser = cli.build_parser()
    args = parser.parse_args(["setup", "--yes", "--ollama-url", "http://x:1"])
    assert args.yes is True and args.ollama_url == "http://x:1"
    for verb in ("version", "chat", "discover-zim", "hatch", "certificate"):
        assert parser.parse_args([verb] if verb != "ingest-zim" else [verb, "p"])
