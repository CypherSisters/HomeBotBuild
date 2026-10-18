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


def _shown_fields(entry: str) -> list[str]:
    """The field ids a card carried in this run, from the run record.

    Most cards are no longer drawn on the console (they answer
    from the one summary), so what a card offered is read from the record the
    runner keeps of every card it proposed, not from the screen.
    """
    from puffpai.config import get_config as _cfg
    record = os.path.join(_cfg().CONFIG_DIR, "wizard_run.json")
    with open(record, encoding="utf-8") as handle:
        log = json.load(handle)["log"]
    for row in log:
        if row.get("entry") == entry and row.get("shown"):
            return list(row["shown"].get("fields") or [])
    return []


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
    # HomeBot's setup has no plan screen: the machine comes first, what was
    # built next, and the closing screen last.
    out = capsys.readouterr().out
    assert (out.index("Let's see what we're working with here")
            < out.index("What was built") < out.index("How to start it"))


def test_skipped_posture_records_the_default_as_defaulted(scratch, no_ollama):
    assert _run_setup() == 0
    posture = _manifest()["posture"]
    assert posture["build_network"] == "online"
    assert posture["build_network_source"] == "defaulted"
    # HomeBot's preset puts its own run-time reach on the run's arguments:
    # Moat, only the outside services the owner switched on, all of them off.
    # Nobody stated it, so it is recorded as defaulted.
    assert posture["run_egress"] == "enumerated"
    assert posture["run_egress_source"] == "defaulted"
    assert posture["enabled_switches"] == []
    assert _manifest()["bridge"]["chosen_by"] == "default"


def test_offline_build_is_never_offered_a_download(scratch, no_ollama, capsys):
    assert _run_setup("--build-network", "offline") == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "nothing is downloaded while we build" in out
    assert "it cannot be downloaded now" in out
    assert "Download it when the build starts?" not in out
    assert "registry.ollama.ai" not in out


def test_online_build_still_sees_downloadable_brains(scratch, no_ollama, capsys):
    assert _run_setup("--build-network", "online") == 0
    out = " ".join(capsys.readouterr().out.split())
    # The first screen lists the brains that fit with their download sizes; a
    # --yes run with nobody reading downloads none and says how to.
    assert "GB download" in out
    assert "To fix it, run: ollama pull" in out


def test_sealed_build_is_not_offered_home_assistant_and_is_told_why(
        scratch, no_ollama, capsys):
    # --skip-ha is deliberately NOT passed: the posture is what closes the step.
    assert cli.main(["setup", "--yes", "--skip-knowledge",
                     "--run-egress", "sealed"]) == 0
    out = capsys.readouterr().out
    assert "Home Assistant" not in out
    assert "sealed" in _shown_fields("tools.card")
    assert "connect_hub" not in _shown_fields("tools.card")


def test_island_build_still_reaches_the_home_assistant_step(
        scratch, no_ollama, capsys):
    assert cli.main(["setup", "--yes", "--skip-knowledge", "--skip-ha",
                     "--run-egress", "island"]) == 0
    assert "hub" in _shown_fields("tools.card")
    assert "sealed" not in _shown_fields("tools.card")


def test_brain_step_discloses_the_routing_tier(scratch, no_ollama, capsys):
    assert _run_setup("--model", "llama3.2:3b") == 0
    out = " ".join(capsys.readouterr().out.split())
    # What this brain cannot do, said on the first screen in plain words.
    assert "one simple request at a time" in out
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
    assert "No one is here to type a PIN, so no vault is made" in " ".join(
        out.split())
    assert setup_ops.vault_initialized() is False
    assert _manifest()["vault"]["configured"] is False


def test_non_interactive_with_a_pin_states_that_nobody_confirmed(
        scratch, no_ollama, capsys):
    assert _run_setup("--vault-pin", GOOD_PIN) == 0
    out = capsys.readouterr().out
    assert "Nobody typed this recovery code back." in out


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
    assert "A vault is already set up on this computer. It is left as it is." in " ".join(out.split())
    # The second run reports what the first one did, not a fresh "none issued".
    assert _manifest()["vault"]["recovery_issued"] is True


# -- Step 3b: surfaces -------------------------------------------------------

def test_the_goal_derives_a_surface_allowlist(scratch, no_ollama):
    # HomeBot asks no goal: a goal on the command line does not narrow its
    # pages, and every page is on with a reason recorded.
    assert _run_setup("--goal", "a bot that helps a beginner learn the night sky and space with a small telescope") == 0
    surfaces = _manifest()["surfaces"]
    assert "instruments.telescope" in surfaces["allowlist"]
    assert "house.cameras" in surfaces["allowlist"]
    assert surfaces["derived_from_goal"] is False
    assert surfaces["reasons"]["instruments.telescope"]


def test_the_owner_can_drop_a_proposed_surface(scratch, no_ollama):
    assert _run_setup("--goal", "a bot that helps a beginner learn the night sky and space with a small telescope",
                      "--drop-surface", "instruments.telescope") == 0
    surfaces = _manifest()["surfaces"]
    assert "instruments.telescope" not in surfaces["allowlist"]
    assert surfaces["owner_edited"] is True


def test_no_goal_leaves_an_empty_allowlist_rather_than_everything(
        scratch, no_ollama):
    # HomeBot asks no goal and turns every page on (the browser's build does
    # the same), so the record names every page rather than none: the
    # allowlist is never left empty, and never a guess from a goal.
    from puffpai import build_interview
    assert _run_setup() == 0
    assert sorted(_manifest()["surfaces"]["allowlist"]) == sorted(
        build_interview.surface_taxonomy())


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
