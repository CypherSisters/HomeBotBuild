"""The goal step offers the scaffold, and the surfaces step loads what it wrote.

A no-match goal does not end with the household defaults and nothing about the
thing the owner actually asked for: step 7 offers a blank persona - one app,
one pack, in the owner's own data directory - and step 8 puts that app's surface
on the allowlist with a reason that names it.

Every test runs against a temporary data dir and config dir. No network, no
Ollama, no services: the scaffold writes JSON and the registry rescans.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import app_registry     # noqa: E402
from puffpai import setup_cli        # noqa: E402
from puffpai import setup_ops        # noqa: E402
from puffpai import persona_scaffold  # noqa: E402

NO_MATCH_GOAL = "a bot for my vintage synthesiser patch bay"


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
    app_registry.rescan()
    yield {"data": data_dir, "config": config_dir}
    config.get_config(_force_reload=True)
    app_registry.rescan()


def _args(**overrides) -> argparse.Namespace:
    """The flags the goal step reads, defaulted the way argparse would."""
    base = {"goal": None, "scaffold": False, "lists": None, "bot_name": None,
            "yes": True}
    base.update(overrides)
    return argparse.Namespace(**base)


# ── the offer ───────────────────────────────────────────────────────────────

def test_a_no_match_goal_scaffolds_on_request(scratch, capsys):
    """--scaffold writes the app and the pack, under the DATA dir."""
    proposal = setup_ops.build_interview(NO_MATCH_GOAL, {})
    assert proposal.get("scaffold_offer") is True

    persona = setup_cli._maybe_scaffold_persona(
        _args(scaffold=True, lists="tools,projects"), NO_MATCH_GOAL, proposal,
        bot_name="HomeBot", non_interactive=True)

    assert persona is not None
    assert persona["scaffolded"] is True
    assert persona["lists"] == ["tools", "projects"]
    assert persona["surfaces"] == [persona_scaffold.persona_surface(persona["app"])]
    # Under the data dir, and nowhere near the checkout.
    assert persona["app_manifest"].startswith(str(scratch["data"]))
    assert persona["pack_manifest"].startswith(str(scratch["data"]))
    with open(persona["app_manifest"], encoding="utf-8") as fh:
        assert app_registry.validate_manifest(json.load(fh)) is None
    assert persona["app"] in app_registry.installed_ids(app_registry.rescan())
    # The app is named after ITS id, never after the bot: two things called
    # HomeBot on one dashboard would read as a mistake.
    assert persona["name"] != "HomeBot"
    out = capsys.readouterr().out
    assert "Made " in out and "tools, projects" in out


def test_naming_lists_is_asking_for_the_scaffold(scratch):
    """--lists with no --scaffold is not a flag somebody passed by accident."""
    proposal = setup_ops.build_interview(NO_MATCH_GOAL, {})
    persona = setup_cli._maybe_scaffold_persona(
        _args(lists="tools"), NO_MATCH_GOAL, proposal, bot_name="HomeBot",
        non_interactive=True)
    assert persona is not None and persona["lists"] == ["tools"]


def test_a_non_interactive_run_never_scaffolds_unasked(scratch, capsys):
    """Nobody is there to answer, and this is not a default anyone chose."""
    proposal = setup_ops.build_interview(NO_MATCH_GOAL, {})
    persona = setup_cli._maybe_scaffold_persona(
        _args(), NO_MATCH_GOAL, proposal, bot_name="HomeBot",
        non_interactive=True)
    assert persona is None
    out = capsys.readouterr().out
    assert "no persona was scaffolded" in out
    assert "puffpai persona new" in out
    assert not os.path.isdir(app_registry.user_apps_dir())


def test_a_goal_that_matched_something_is_not_offered_a_scaffold(scratch):
    """HomeBot's own default goal matches real apps; nothing is written."""
    goal = setup_ops.homebot_defaults()["goal"]
    proposal = setup_ops.build_interview(goal, {})
    assert not proposal.get("scaffold_offer")
    assert setup_cli._maybe_scaffold_persona(
        _args(), goal, proposal, bot_name="HomeBot",
        non_interactive=True) is None


def test_a_refused_persona_is_a_sentence_not_a_crash(scratch, capsys):
    """The shipped pack id is taken, so scaffolding it writes nothing."""
    from puffpai import persona_scaffold

    proposal = setup_ops.build_interview(NO_MATCH_GOAL, {})
    monkey = _args(scaffold=True, lists="tools")
    # Force the collision the suggestion normally steps around.
    original = persona_scaffold.suggest_id
    try:
        persona_scaffold.suggest_id = lambda name: "homebot"
        persona = setup_cli._maybe_scaffold_persona(
            monkey, NO_MATCH_GOAL, proposal, bot_name="HomeBot",
            non_interactive=True)
    finally:
        persona_scaffold.suggest_id = original
    assert persona is None
    assert "Nothing was written" in capsys.readouterr().out
    assert [p["id"] for p in app_registry.load_packs()] == ["homebot"]


# ── the surfaces step honours it ────────────────────────────────────────────

def test_the_scaffolded_surface_joins_the_allowlist_with_its_reason(scratch):
    derivation = setup_ops.default_surfaces_derivation("")
    before = set(derivation["surfaces"])
    persona = {"app": "workshop", "name": "Workshop", "surfaces": ["daily.notes"]}

    out = setup_cli._with_persona_surfaces(derivation, persona)
    assert set(out["surfaces"]) == before | {"daily.notes"}
    assert "Workshop is your own app" in out["reasons"]["daily.notes"]
    # The household defaults' own sensitive surfaces survive: this is a union,
    # never a recomputation from the one entry added here.
    assert set(derivation["sensitive"]) <= set(out["sensitive"])
    block = setup_ops.surfaces_block(out)
    assert block["allowlist"] == out["surfaces"]
    assert "daily.notes" in block["reasons"]


def test_no_persona_leaves_the_derivation_exactly_as_it_was(scratch):
    derivation = setup_ops.default_surfaces_derivation("")
    assert setup_cli._with_persona_surfaces(derivation, None) is derivation
    assert setup_cli._with_persona_surfaces(
        derivation, {"app": "x", "surfaces": ["not.a.surface"]}) is derivation


# ── the record of it ────────────────────────────────────────────────────────

def test_the_manifest_records_the_scaffold_beside_the_shipped_persona(scratch):
    """persona.scaffolded, not persona: this build is still HomeBot."""
    from puffpai import build_manifest

    census = setup_ops.hardware_census()
    persona = {"scaffolded": True, "app": "workshop", "pack": "workshop",
               "name": "Workshop", "goal": NO_MATCH_GOAL, "lists": ["tools"],
               "surfaces": ["daily.notes"], "app_manifest": "a", "pack_manifest": "b",
               "note": "n"}
    preview = setup_ops.preview_manifest(census, identity={"identity": "homebot",
                                                          "name": "HomeBot"})
    manifest = preview["manifest"]
    manifest["persona"] = {"id": "homebot", "pack": "homebot", "name": "HomeBot",
                           "scaffolded": persona}
    path = setup_ops.commit_manifest(manifest)
    written = build_manifest.read_manifest()
    assert written["persona"]["name"] == "HomeBot"
    assert written["persona"]["scaffolded"]["app"] == "workshop"
    assert str(scratch["data"]) in path or str(scratch["config"]) in path


def test_the_new_flags_are_registered_on_the_setup_verb():
    parser = argparse.ArgumentParser()
    setup_cli.add_arguments(parser)
    args = parser.parse_args(["--yes", "--scaffold", "--lists", "tools,projects"])
    assert args.scaffold is True and args.lists == "tools,projects"
    assert parser.parse_args(["--yes"]).scaffold is False
