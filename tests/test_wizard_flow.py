"""Tests for the wizard v2 flow as the CLI actually walks it.

Some behaviour is checked elsewhere in this tree: the interactive recovery
ceremony is the Secure card's (``tests/test_wizard_secure.py`` and
``tests/test_wizard_interactive.py``); there is no ``puffpai build
export/import`` subcommand; and the config-health acknowledgment lives inside
the manifest card (``wizard.host._setup_manifest_step``), checked by the
non-interactive test below.

The modules underneath (posture, the zones, surfaces, config health, bundles)
have their own tests. This file is about the *wiring*: that Step 0 comes first
and its gates really close later menus, that the vault ceremony goes through the
path that issues a recovery code, that a corrupt identity config is acknowledged
rather than ignored or fatal, and that the sneakernet subcommand round-trips.

The load-bearing one is
``test_setup_seals_the_vault_through_the_recovery_path``. ``seal_vault`` creates
a PIN-only vault with no recovery code, so wiring it would run the whole "write
this down, it is your only way back in" ceremony and hand the owner nothing.
That test exists to make sure that can never be true again.

Run:
    pytest tests/test_wizard_flow.py
"""
from __future__ import annotations

import argparse
import json
import re
import os

import pytest

from puffpai import build_manifest, cli, config_health, setup_ops, zone_vault
from puffpai.config import get_config

GOOD_PIN = "Telescope7x"


@pytest.fixture
def scratch(tmp_path):
    """Point the config singleton at an isolated data dir for one test."""
    prev = os.environ.get("PUFFPAI_DATA_DIR")
    os.environ["PUFFPAI_DATA_DIR"] = str(tmp_path)
    get_config(_force_reload=True)
    try:
        yield tmp_path
    finally:
        if prev is not None:
            os.environ["PUFFPAI_DATA_DIR"] = prev
        else:
            os.environ.pop("PUFFPAI_DATA_DIR", None)
        get_config(_force_reload=True)


@pytest.fixture
def no_ollama(monkeypatch):
    """Keep the wizard off the network: Ollama is up with nothing installed."""
    monkeypatch.setattr(setup_ops, "list_models", lambda *a, **k: [])


def _run_setup(*extra: str) -> int:
    return cli.main(["setup", "--yes", "--skip-ha", "--skip-knowledge", *extra])


def _manifest() -> dict:
    return build_manifest.read_manifest() or {}


# -- the posture step comes first, and its gates close later menus ------------

def test_posture_is_recorded_with_stated_vs_defaulted(scratch, no_ollama, capsys):
    assert _run_setup("--build-network", "offline", "--run-egress", "sealed") == 0
    posture = _manifest()["posture"]
    assert posture["build_network"] == "offline"
    assert posture["build_network_source"] == "stated"
    assert posture["run_egress"] == "sealed"
    # Posture is printed before the census, not after it. The census is the
    # Discover card's own title rather than a step banner; where it comes in
    # the run is what this asserts.
    out = capsys.readouterr().out
    assert (out.index("What this build may reach")
            < out.index("What this machine is"))


def test_skipped_posture_records_the_default_as_defaulted(scratch, no_ollama):
    assert _run_setup() == 0
    posture = _manifest()["posture"]
    assert posture["build_network"] == "online"
    assert posture["run_egress"] == "island"
    assert posture["build_network_source"] == "defaulted"
    assert posture["run_egress_source"] == "defaulted"


def test_offline_build_is_never_offered_a_download(scratch, no_ollama, capsys):
    assert _run_setup("--build-network", "offline") == 0
    out = capsys.readouterr().out
    assert "You chose an offline build" in out
    assert "re-run setup where downloads are allowed" in out
    assert "GB to download" not in out


def test_online_build_still_sees_downloadable_brains(scratch, no_ollama, capsys):
    assert _run_setup("--build-network", "online") == 0
    assert "GB to download" in capsys.readouterr().out


def test_sealed_build_is_not_offered_home_assistant_and_is_told_why(
        scratch, no_ollama, capsys):
    # --skip-ha is deliberately NOT passed: the posture is what closes the step.
    assert cli.main(["setup", "--yes", "--skip-knowledge",
                     "--run-egress", "sealed"]) == 0
    out = capsys.readouterr().out
    assert "you chose a sealed build, so nothing on the network is offered" in out
    assert "Connect a Home Assistant instance" not in out


def test_island_build_still_reaches_the_home_assistant_step(
        scratch, no_ollama, capsys):
    assert cli.main(["setup", "--yes", "--skip-knowledge", "--skip-ha",
                     "--run-egress", "island"]) == 0
    assert "not being asked for on this run" in capsys.readouterr().out


def test_brain_step_discloses_the_routing_tier(scratch, no_ollama, capsys):
    assert _run_setup("--model", "llama3.2:3b") == 0
    out = capsys.readouterr().out
    assert "Routing tier:" in out
    assert "single steps" in out  # what the tier costs, said out loud
    brain = _manifest()["brain"]
    assert brain["routing_tier"] in ("light", "guided", "rails")
    assert brain["supports_planning"] is False


# -- Step 4: the vault ceremony ---------------------------------------------

def test_setup_seals_the_vault_through_the_recovery_path(
        scratch, no_ollama, monkeypatch):
    """The CLI must reach seal_vault_with_recovery, and never seal_vault.

    seal_vault (and setup_ops.create_vault under it) make a PIN-only vault. If
    the wizard ever wires one of those, the owner is walked through the whole
    "write this code down" ceremony and silently given no code at all.
    """
    calls = {"with_recovery": 0}
    real = zone_vault.seal_vault_with_recovery

    def spy(pin, *, vault=None):
        calls["with_recovery"] += 1
        return real(pin, vault=vault)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "the wizard used a PIN-only vault path; the owner would get no "
            "recovery code after being told to write one down"
        )

    # The Secure card seals through ``setup_ops.create_vault(...,
    # with_recovery=True)`` rather than calling ``zone_vault`` itself, so what
    # is forbidden here is the PIN-ONLY paths and what is spied on is the
    # issuing one. The property is the one this test was written for: nothing
    # may seal a vault without issuing the code the owner is told to write down.
    real_create = setup_ops.create_vault

    def create_spy(pin, *, with_recovery=False, **kw):
        if not with_recovery:
            forbidden()
        calls["with_recovery"] += 1
        return real_create(pin, with_recovery=True, **kw)

    monkeypatch.setattr(zone_vault, "seal_vault", forbidden)
    monkeypatch.setattr(setup_ops, "create_vault", create_spy)

    assert _run_setup("--vault-pin", GOOD_PIN) == 0
    assert calls["with_recovery"] == 1
    assert setup_ops.vault_initialized() is True

    block = _manifest()["vault"]
    assert block["configured"] is True and block["sealed"] is True
    assert block["recovery_issued"] is True
    # Nobody read it back, so it is not recorded as confirmed.
    assert block["recovery_confirmed"] is False


def test_non_interactive_without_a_pin_skips_the_vault_and_says_so(
        scratch, no_ollama, capsys):
    assert _run_setup() == 0
    out = capsys.readouterr().out
    assert "recovery code nobody reads is not a recovery code" in out
    assert setup_ops.vault_initialized() is False
    assert _manifest()["vault"]["configured"] is False


def test_non_interactive_with_a_pin_states_that_nobody_confirmed(
        scratch, no_ollama, capsys):
    assert _run_setup("--vault-pin", GOOD_PIN) == 0
    out = capsys.readouterr().out
    assert "NO ONE CONFIRMED THIS RECOVERY CODE." in out


def test_manifest_never_carries_the_pin_or_the_code(scratch, no_ollama, capsys):
    assert _run_setup("--vault-pin", GOOD_PIN) == 0
    out = capsys.readouterr().out
    # The code is printed once in this mode; pull it back out and make sure it
    # did not also land on disk.
    # This tree's recovery code is six groups of four, not five.
    code = next(line.strip() for line in out.splitlines()
                if re.fullmatch(r"(?:[A-Z0-9]{4}-){5}[A-Z0-9]{4}", line.strip()))
    body = json.dumps(_manifest())
    assert code not in body
    assert GOOD_PIN not in body


def _ceremony_args() -> argparse.Namespace:
    return argparse.Namespace(skip_vault=False, vault_password=None)

def test_shared_create_vault_can_issue_a_recovery_code(scratch):
    """The dashboard face must be able to issue a code, like the CLI face.

    /setup/vault goes through setup_ops.create_vault. Without an issuance path
    there, an owner who onboards in the browser gets a PIN-only vault and is
    stranded the first time they forget the PIN.
    """
    code = setup_ops.create_vault("correct horse battery staple",
                                  with_recovery=True)
    # Six groups of four here, five in BuildBot. The shape is checked
    # so a code that stopped being a code would fail; the count is this tree's.
    assert code and code.count("-") == 5
    assert setup_ops.vault_initialized() is True
    # It was handed back for one screen, not written anywhere.
    for root, _dirs, names in os.walk(scratch):
        for name in names:
            with open(os.path.join(root, name), "rb") as fh:
                assert code.encode() not in fh.read(), f"code leaked into {name}"


def test_legacy_create_vault_stays_pin_only(scratch):
    """Existing callers must not silently start issuing codes."""
    assert setup_ops.create_vault("correct horse battery staple") is None
    assert setup_ops.vault_initialized() is True
    block = setup_ops.vault_block(configured=True, sealed=True)
    assert block["recovery_issued"] is False


def test_pin_validation_covers_the_vault_floor_too(scratch):
    """A PIN the prompt accepts must be one the seal accepts.

    zone_vault.validate_pin allows six mixed characters; the vault's own floor
    is eight. Checking only the first would let a PIN through the prompt and
    then have the seal raise, which reads as the wizard changing its mind.
    """
    ok, reason = setup_ops.validate_vault_pin("ab1cd2")
    assert ok is False
    assert "8 characters" in reason
    assert setup_ops.validate_vault_pin(GOOD_PIN) == (True, "")


def test_an_existing_vault_is_left_alone_on_a_re_run(scratch, no_ollama, capsys):
    assert _run_setup("--vault-pin", GOOD_PIN) == 0
    capsys.readouterr()
    assert _run_setup("--vault-pin", GOOD_PIN) == 0
    out = capsys.readouterr().out
    assert "left untouched" in out
    # The second run reports what the first one did, not a fresh "none issued".
    assert _manifest()["vault"]["recovery_issued"] is True


# -- Step 3b: surfaces -------------------------------------------------------

def test_the_goal_derives_a_surface_allowlist(scratch, no_ollama):
    assert _run_setup("--goal", "a bot that helps a beginner learn the night sky and space with a small telescope") == 0
    surfaces = _manifest()["surfaces"]
    assert "instruments.telescope" in surfaces["allowlist"]
    assert "house.cameras" not in surfaces["allowlist"]
    assert surfaces["derived_from_goal"] is True
    assert surfaces["reasons"]["instruments.telescope"]


def test_the_owner_can_drop_a_proposed_surface(scratch, no_ollama):
    assert _run_setup("--goal", "a bot that helps a beginner learn the night sky and space with a small telescope",
                      "--drop-surface", "instruments.telescope") == 0
    surfaces = _manifest()["surfaces"]
    assert "instruments.telescope" not in surfaces["allowlist"]
    assert surfaces["owner_edited"] is True


def test_no_goal_leaves_an_empty_allowlist_rather_than_everything(
        scratch, no_ollama):
    assert _run_setup() == 0
    assert _manifest()["surfaces"]["allowlist"] == []


# -- Step 6: the Bridge ------------------------------------------------------

def test_a_sealed_build_forces_drawbridge_up(scratch, no_ollama):
    assert _run_setup("--run-egress", "sealed", "--bridge-posture", "open_yard") == 0
    bridge = _manifest()["bridge"]
    assert bridge["posture"] == "drawbridge_up"
    assert bridge["reconciled"] is True
    assert "stricter" in bridge["note"]


def test_skipping_the_bridge_records_the_safe_default(scratch, no_ollama):
    assert _run_setup("--skip-bridge") == 0
    bridge = _manifest()["bridge"]
    assert bridge["posture"] == "fenced"
    assert bridge["chosen_by"] in ("default", "override")


# -- Task 4: the config-health acknowledgment gate ---------------------------

def _degrade(name: str) -> None:
    config_health.record(name, f"config/{name}.json", config_health.UNPARSEABLE,
                         "JSON parse error: test", reduced=True)


def test_identity_degradation_never_hard_refuses_a_non_interactive_build(
        scratch, no_ollama, capsys):
    _degrade("capability_catalog")
    assert _run_setup() == 0
    out = capsys.readouterr().out
    # The manifest card says which files could not be read, in its own
    # words (BuildBot's linear gate said "changes what your build IS"). What
    # matters, and what is asserted, is that the degraded config is named on
    # screen and recorded rather than swallowed.
    assert "capability_catalog" in out
    manifest = _manifest()
    assert manifest, "an offline owner must not be left with no manifest"
    assert manifest["config_health"]["degraded"] is True
    assert "capability_catalog" in manifest["config_health"]["configs"]


def test_setup_status_carries_posture_zones_surfaces_and_health(
        scratch, no_ollama):
    assert _run_setup("--goal", "a night sky bot", "--vault-pin", GOOD_PIN,
                      "--run-egress", "sealed") == 0
    status = setup_ops.setup_status(probe_ollama=False)

    assert status["posture"]["run_egress"] == "sealed"
    assert status["zones"]["vault"]["initialized"] is True
    assert status["zones"]["vault"]["recovery_issued"] is True
    assert status["zones"]["bridge"]["posture"] == "drawbridge_up"
    assert "zim_dir" in status["zones"]["brain"]
    assert status["surfaces"]["allowlist"]
    assert status["config_health"]["degraded"] is False
    assert status["manifest"]["exists"] is True

    body = json.dumps(status)
    assert GOOD_PIN not in body


# -- ASCII: this is the Windows console face ---------------------------------

def test_the_whole_wizard_prints_ascii_only(scratch, no_ollama, capsys):
    assert _run_setup("--goal", "a bot that helps a beginner learn the night sky and space with a small telescope",
                      "--vault-pin", GOOD_PIN) == 0
    out = capsys.readouterr().out
    offenders = sorted({ch for ch in out if ord(ch) > 127})
    assert not offenders, f"non-ASCII in wizard output: {offenders}"
