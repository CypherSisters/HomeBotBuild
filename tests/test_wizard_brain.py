"""Brain as one card: the menu, the pull, and the check that it answers.

The machine is pinned the way ``tests/test_wizard_discover.py`` pins it, and
Ollama is stubbed everywhere: no test here reaches a model, a registry or a
socket. The control for "did it change what it writes" is the pair of step
functions (``cli._setup_brain_step`` and ``cli._setup_brain_ready_step``) run
with the same scripted inputs.

Two rules this file is really about:

* **The pull only happens where the step function pulls** -- the owner said
* yes on
  the card and the posture allows a download. A ``--yes`` run starts no
  download, and an offline build is offered none.
* **A missing model is a gap, not a stop.** Brain may block, and the wizard
  uses that for nothing.

Run:
    pytest tests/test_wizard_brain.py
"""
from __future__ import annotations

import argparse
import copy
import types

import pytest

from puffpai import brain_health, cli, hardware_census, setup_cli, setup_ops
from puffpai.config import get_config
from puffpai.wizard import cards, loader, runner as wizard_runner

INSTALLED = "qwen2.5:7b-instruct"
URL = "http://127.0.0.1:11434"


@pytest.fixture
def steady(monkeypatch, tmp_path):
    """One census, one data dir, and an Ollama that answers from a dict."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    cfg = get_config(_force_reload=True)
    census = hardware_census.run_census()
    monkeypatch.setattr(hardware_census, "run_census",
                        lambda *a, **k: copy.deepcopy(census))
    monkeypatch.setattr(setup_ops, "list_models", lambda *a, **k: [INSTALLED])
    monkeypatch.setattr(setup_ops, "brain_status", lambda url, model, **k: {
        "status": (brain_health.CONNECTED if model == INSTALLED
                   else brain_health.MODEL_MISSING),
        "installed": [INSTALLED]})
    yield cfg
    get_config(_force_reload=True)


def _args(**kw):
    base = dict(yes=True, model=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _module():
    packages, refusals = loader.load_builtin_packages()
    assert refusals == []
    return loader.load_module(packages["brain"]), packages["brain"]


def _shared(cfg, **over):
    census = hardware_census.run_census()
    shared = {"census": census, "ollama_url": URL,
              "installed_models": [INSTALLED],
              "gates": setup_ops.posture_gates(
                  setup_ops.assess_posture("online", "island", []))}
    shared.update(over)
    return shared


def _ctx(cfg, package, *, args=None, sections=None, shared=None,
         non_interactive=True):
    return wizard_runner.RunContext(
        args=args or _args(), cfg=cfg, non_interactive=non_interactive,
        shared=_shared(cfg) if shared is None else shared,
        sections=sections if sections is not None else {}, package=package)


# -- propose ------------------------------------------------------------------

def test_propose_returns_a_card_that_validates_and_writes_nothing(steady):
    module, package = _module()
    ctx = _ctx(steady, package)
    card = cards.validate_card(module.propose(ctx)["card"])
    assert card["skill"] == "brain" and card["section"] == "brain"
    assert card["title"] == "What does the thinking"
    assert "model" in cards.field_map(card)
    assert ctx._sections == {}, "propose wrote a section"


def test_the_card_says_what_the_tier_costs(steady):
    module, package = _module()
    card = cards.validate_card(module.propose(_ctx(steady, package))["card"])
    assert "Routing tier" in cards.field_map(card)["tier"]["current"]


def test_an_installed_model_costs_nothing_and_is_not_consequential(steady):
    module, package = _module()
    ctx = _ctx(steady, package, args=_args(model=INSTALLED))
    card = cards.validate_card(module.propose(ctx)["card"])
    assert card["estimate"] == {"bytes": 0, "seconds": 0, "basis": "measured"}
    assert card["consequential"] is False
    assert "download_now" not in cards.field_map(card)


def test_a_download_is_sized_from_the_catalog_and_never_timed(steady):
    module, package = _module()
    ctx = _ctx(steady, package, shared=_shared(steady, installed_models=[]))
    card = cards.validate_card(module.propose(ctx)["card"])
    estimate = card["estimate"]
    assert estimate["basis"] in ("calibrated", "unknown")
    assert estimate["seconds"] is None, "a time nobody measured was offered"
    if estimate["basis"] == "calibrated":
        assert estimate["bytes"] > 0
        assert card["consequential_reasons"] == ["heavy_bytes"]
        assert cards.field_map(card)["download_now"]["proposed"] is False


def test_an_offline_build_is_offered_no_download(steady):
    module, package = _module()
    gates = setup_ops.posture_gates(
        setup_ops.assess_posture("offline", "island", []))
    ctx = _ctx(steady, package,
               shared=_shared(steady, gates=gates, installed_models=[]))
    card = cards.validate_card(module.propose(ctx)["card"])
    fields = cards.field_map(card)
    assert "download_now" not in fields
    offered = {opt["value"] for opt in (fields.get("model") or
                                        {"options": []})["options"]}
    assert offered <= {INSTALLED}


def test_a_run_with_no_census_yet_is_a_card_and_not_an_exception(steady):
    module, package = _module()
    ctx = _ctx(steady, package, shared={})
    card = cards.validate_card(module.propose(ctx)["card"])
    assert card["status"] in ("ready", "needs_you")


# -- apply --------------------------------------------------------------------

def test_apply_writes_what_the_two_old_steps_wrote(steady, capsys):
    module, package = _module()
    ctx = _ctx(steady, package, args=_args(model=INSTALLED))
    ctx.proposal = module.propose(ctx)
    new = module.apply(ctx, {})

    # BUILDBOT NOTE: BuildBot keeps its pre-card step
    # (``cli._setup_*_step``) as a live control. This tree has no such
    # function -- its pre-card wizard is ``setup_cli._step_*``, an earlier
    # generation that writes a different object (HomeBot defaults, a richer
    # recommendation block), so it cannot stand in as the control. What the
    # card writes is asserted directly instead.
    capsys.readouterr()

    assert new["section"] == "brain"
    assert new["data"]["model"] == INSTALLED
    choice = new["data"]["choice"]
    assert choice["ready"] is True
    assert choice["brain_status"] == "connected"
    assert new["shared"]["brain_ready"]["ready"] is True
    assert new["shared"]["brain_ready"]["status"] == "connected"


def test_a_yes_run_never_starts_a_download(steady, monkeypatch):
    pulls = []
    monkeypatch.setattr(brain_health, "pull_model",
                        lambda *a, **k: pulls.append(a) or {"ok": True})
    module, package = _module()
    ctx = _ctx(steady, package, args=_args(model="not-here:8b"),
               shared=_shared(steady, installed_models=[]))
    ctx.proposal = module.propose(ctx)
    result = module.apply(ctx, {})

    assert pulls == [], "a non-interactive run started a download"
    assert result["data"]["ready"] is False
    assert result["gap"]["fix"]["id"] == "setup.rerun"
    assert result["shared"]["brain_ready"]["pull_command"]


def test_an_offline_build_defers_the_pull_with_a_gap(steady, monkeypatch):
    pulls = []
    monkeypatch.setattr(brain_health, "pull_model",
                        lambda *a, **k: pulls.append(a) or {"ok": True})
    module, package = _module()
    gates = setup_ops.posture_gates(
        setup_ops.assess_posture("offline", "sealed", []))
    ctx = _ctx(steady, package, args=_args(model="not-here:8b"),
               shared=_shared(steady, gates=gates, installed_models=[]),
               non_interactive=False)
    ctx.proposal = module.propose(ctx)
    result = module.apply(ctx, {"download_now": True})

    assert pulls == [], "an offline build downloaded a model"
    assert "offline build" in result["gap"]["impact"]


def test_saying_yes_pulls_through_the_path_that_already_exists(steady,
                                                               monkeypatch):
    pulled = {}

    def fake_pull(model, url=None, **kw):
        pulled.update({"model": model, "url": url})
        return {"ok": True, "message": f"{model} is installed."}

    monkeypatch.setattr(brain_health, "pull_model", fake_pull)
    module, package = _module()
    ctx = _ctx(steady, package, args=_args(model="not-here:8b"),
               shared=_shared(steady, installed_models=[]),
               non_interactive=False)
    ctx.proposal = module.propose(ctx)
    result = module.apply(ctx, {"download_now": True})

    assert pulled == {"model": "not-here:8b", "url": URL}
    assert result["data"]["ready"] is True
    assert result["shared"]["brain_ready"]["status"] == brain_health.CONNECTED


def test_a_failed_pull_is_a_gap_and_never_reports_ready(steady, monkeypatch):
    monkeypatch.setattr(brain_health, "pull_model", lambda *a, **k: {
        "ok": False, "message": "The download did not finish: connection lost."})
    module, package = _module()
    ctx = _ctx(steady, package, args=_args(model="not-here:8b"),
               shared=_shared(steady, installed_models=[]),
               non_interactive=False)
    ctx.proposal = module.propose(ctx)
    result = module.apply(ctx, {"download_now": True})
    assert result["data"]["ready"] is False
    assert "did not finish" in result["gap"]["why"]


def test_an_unreachable_ollama_records_the_remedy(steady, monkeypatch):
    monkeypatch.setattr(setup_ops, "brain_status", lambda *a, **k: {
        "status": brain_health.UNREACHABLE, "installed": []})
    module, package = _module()
    ctx = _ctx(steady, package, args=_args(model=INSTALLED))
    ctx.proposal = module.propose(ctx)
    result = module.apply(ctx, {})
    assert result["shared"]["brain_ready"]["remedy"]
    assert result["data"]["ready"] is False


def test_a_typed_model_is_the_one_that_is_recorded(steady):
    module, package = _module()
    ctx = _ctx(steady, package)
    ctx.proposal = module.propose(ctx)
    data = module.apply(ctx, {"model": "qwen2.5:14b"})["data"]
    assert data["model"] == "qwen2.5:14b"
    assert data["choice"]["chosen_by"] == "owner"


# -- verify -------------------------------------------------------------------

def test_verify_asks_the_ollama_and_not_the_proposal(steady):
    module, package = _module()
    sections = {"machine": {"ollama_url": URL},
                "brain": {"model": INSTALLED, "choice": {"model": INSTALLED}}}
    verdict = module.verify(_ctx(steady, package, sections=sections))
    assert verdict["status"] == "pass"
    assert INSTALLED in verdict["evidence"]


def test_a_model_that_is_not_installed_is_a_gap_not_a_stop(steady):
    module, package = _module()
    sections = {"machine": {"ollama_url": URL},
                "brain": {"model": "not-here:8b"}}
    verdict = module.verify(_ctx(steady, package, sections=sections))
    assert verdict["status"] == "gap"
    assert "ollama pull" in verdict["gap"]["impact"]


def test_an_unreachable_ollama_is_a_gap_and_not_an_exception(steady,
                                                              monkeypatch):
    monkeypatch.setattr(setup_ops, "brain_status", lambda *a, **k: {
        "status": brain_health.UNREACHABLE, "installed": []})
    module, package = _module()
    sections = {"machine": {"ollama_url": URL}, "brain": {"model": INSTALLED}}
    verdict = module.verify(_ctx(steady, package, sections=sections))
    assert verdict["status"] == "gap"
    assert verdict["gap"]["skill"] == "brain"


def test_no_model_recorded_is_a_gap_with_a_fix(steady):
    module, package = _module()
    verdict = module.verify(_ctx(steady, package,
                                 sections={"machine": {"ollama_url": URL},
                                           "brain": {}}))
    assert verdict["status"] == "gap"
    assert verdict["gap"]["fix"]["id"] == "setup.rerun"


# -- through the runner -------------------------------------------------------

def test_the_runner_walks_the_lifecycle_and_records_what_it_showed(steady,
                                                                   tmp_path):
    cfg = types.SimpleNamespace(CONFIG_DIR=str(tmp_path / "config"),
                                ollama=steady.ollama, DATA_DIR=steady.DATA_DIR,
                                paths=steady.paths)
    run = wizard_runner.Runner(
        args=_args(), cfg=cfg, non_interactive=True,
        schedule=(wizard_runner.Step("brain.card", "brain",
                                     wizard_runner.V2_PART),),
        renderer=lambda card, **kw: {"model": INSTALLED})
    run.shared.update(_shared(steady))
    run.sections["machine"] = {"ollama_url": URL}
    run.run()

    row = run.log[-1]
    assert row["outcome"] == "done"
    assert row["shown"]["title"] == "What does the thinking"
    assert row["verified"]["status"] == "pass"
    assert run.sections["brain"]["model"] == INSTALLED
    assert run.stale[:1] == ["context"] and run.stale[-2:] == ["prove",
                                                               "handoff"]


# -- the pull itself: the door is asked, and no answer means no ------------------

@pytest.mark.parametrize("failure", ["check_raises", "registry_unimportable"])
def test_a_door_check_that_cannot_answer_never_pulls(monkeypatch, failure):
    """``pull_model`` fails closed: no answer from the registry, no POST.

    It used to treat any exception from the door check as "allowed" and ask
    Ollama for gigabytes from registry.ollama.ai. An unanswerable check is not
    the owner's consent, so the refusal must still carry the by-hand command.
    """
    import sys

    import requests

    if failure == "check_raises":
        from puffpai.security import egress_doors

        def _boom(_door_id):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(egress_doors, "egress_allowed", _boom)
    else:
        monkeypatch.setitem(sys.modules, "puffpai.security.egress_doors", None)

    posts = []

    def _no_post(*a, **k):
        posts.append(a)
        raise AssertionError("pull_model POSTed with no answer from the door")

    monkeypatch.setattr(requests, "post", _no_post)
    outcome = brain_health.pull_model("not-here:8b", URL)

    assert posts == []
    assert outcome["ok"] is False
    assert "Nothing was downloaded" in outcome["message"]
    assert brain_health.pull_command("not-here:8b", URL) in outcome["message"]
