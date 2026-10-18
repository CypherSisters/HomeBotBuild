"""The plan and "What leaves your computer" give the same list.

In one wedding-planner build the plan said nothing leaves, then only GitHub;
after the build every page said 12 outside services were on, 11 of them "as
BuildBot comes" and none named on the plan, and 99 searches had gone out
before the plan was shown. The rule: the plan names every outside
service that will be on for this bot, with what the looking already sent,
and anything it does not name is off once the bot is made. The plan, the
"What leaves" page, the sidebar count and the home page agree.

No network: the outside-traffic log lines are written by hand.
"""
from __future__ import annotations

import json
import os
import time
import types

import pytest

from puffpai import front_page, outside_services, plan_outside, zone_bridge
from puffpai.config import get_config
from puffpai.security import (bridge_guard, egress_doors, egress_ledger,
                              outside_switches, privacy_explainer)
from puffpai.wizard import factory, proposal, wording


def _manifest(bridge="fenced", run_egress="enumerated", switches=()):
    cfg = get_config()
    os.makedirs(cfg.CONFIG_DIR, exist_ok=True)
    data = {"posture": {"build_network": "online", "run_egress": run_egress,
                        "egress_switches": {k: True for k in switches}},
            "bridge": {"posture": bridge}}
    with open(os.path.join(cfg.CONFIG_DIR, "build_manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(data, fh)
    # A real install has its settings file; without one every built-in
    # service stays shut (fail closed).
    with open(os.path.join(cfg.CONFIG_DIR, "puffpai_config.json"), "w",
              encoding="utf-8") as fh:
        json.dump({}, fh)
    bridge_guard.forget_enforced()
    egress_doors.reset_cache()
    outside_switches.forget()


@pytest.fixture(autouse=True)
def _forget():
    yield
    bridge_guard.forget_enforced()
    egress_doors.reset_cache()
    outside_switches.forget()


def _looked(n_code=3, n_pictures=2):
    """What the looking step sent, as the real exits log it."""
    for i in range(n_code):
        egress_ledger.record("code_search", host=("api.github.com", "registry.npmjs.org",
                                                  "codeberg.org")[i % 3],
                             outcome="done", what="code_search", bytes_in=100)
    egress_ledger.record("code_search", host="huggingface.co", outcome="refused",
                         code="switched_off", what="code_search")
    for i in range(n_pictures):
        egress_ledger.record("picture_search", host=("commons.wikimedia.org",
                                                     "api.openverse.org")[i % 2],
                             outcome="done", what="picture_search", bytes_in=100)


SHARED = {"needs_look": "online"}

APP = {"id": "apps_code:bunny", "section": "apps_code", "label": "Bunny's Big Day",
       "apply": {"kind": "code_project", "ref": "bunny"},
       "outside_host": "codeload.github.com", "on": True}
WEATHER = {"id": "outside_services:door:weather", "section": "outside_services",
           "label": "Weather forecast", "apply": {"kind": "door", "ref": "weather"},
           "outside_host": "api.open-meteo.com", "on": True}


def _shared():
    return dict(SHARED, needs_look_at=time.time() - 60)


def _on_ids():
    return sorted(r["id"] for r in outside_services.rows() if r["state"] == "on")


# -- the list itself ------------------------------------------------------------

def test_only_look_on_this_computer_keeps_no_looking_tool():
    _manifest()
    _looked()
    shared = {"needs_look": "here", "needs_look_at": time.time() - 60}
    assert plan_outside.already_sent(shared) == []
    assert plan_outside.stay_on(shared, [], "only_listed") == []


def test_a_look_before_this_run_is_not_counted():
    _manifest()
    _looked()
    shared = {"needs_look": "online", "needs_look_at": time.time() + 3600}
    assert plan_outside.already_sent(shared) == []


def test_keeping_the_bot_off_the_internet_keeps_nothing_on():
    _manifest()
    _looked()
    assert plan_outside.stay_on(_shared(), [APP, WEATHER], "offline") == []


def test_with_the_bridge_up_nothing_unswitched_is_named():
    _manifest(bridge="drawbridge_up")
    _looked()
    kept = plan_outside.stay_on(_shared(), [APP], "only_listed")
    assert kept == []


# -- the plan, the build, and every count ------------------------------------

def _settle_after_build(items, reach="only_listed", switches=("weather",)):
    # What the build leaves behind: the ticked built-in service is on.
    _manifest(switches=switches)
    return plan_outside.settle(_shared(), items, reach, {"items": []})


def test_a_plan_with_nothing_kept_says_so_with_consent_words():
    _manifest()
    plan = plan_outside.footer(shared={}, items=[], reach="only_listed",
                               name="WeddingBot", mode=wording.SIMPLE)
    assert plan["count"] == 0
    assert "Only what you say yes to leaves your computer." in plan["line"]
    assert "everything outside is off until you switch it on" in plan["line"]
    plan_outside.settle({}, [], "only_listed", {"items": []})
    assert _on_ids() == []


def test_settle_never_switches_anything_on():
    _manifest()
    outside_switches.set_switch("code_search", False, by="owner")
    _looked()
    plan_outside.settle(_shared(), [APP], "only_listed", {"items": []})
    assert outside_switches.get("code_search") is False
    assert "code_search" not in _on_ids()


def test_what_the_owner_switched_on_themself_stays_on_and_is_named():
    _manifest()
    outside_switches.set_switch("library", True, by="owner")
    kept = [e["id"] for e in plan_outside.stay_on({}, [], "only_listed")]
    assert kept == ["library"]
    plan_outside.settle({}, [], "only_listed", {"items": []})
    assert _on_ids() == ["library"]


def test_a_live_card_line_is_mapped_to_the_card_it_made():
    kept = [{"id": plan_outside.CARD_PLACEHOLDER + "live_info:x", "item_id": "live_info:x",
             "name": "Sky", "websites": [], "why": ""}]
    ids = plan_outside._expected_ids(kept, {"items": [
        {"id": "live_info:x", "ok": True, "app_id": "sky-abc"}]})
    assert ids == {"card-sky-abc"}


# -- through the real plan and the real build step --------------------------

def _brain_preview():
    return {"model": "qwen2.5:7b-instruct", "gates": {"may_download_models": True},
            "menu": {"installed_models": ["qwen2.5:7b-instruct"],
                     "options": [{"id": "qwen2.5:7b-instruct", "label": "Qwen2.5 7B",
                                  "disk_gb": 5, "fit": "comfortable",
                                  "installed": True}]}}


@pytest.fixture
def no_catalog_lines(monkeypatch):
    monkeypatch.setattr(factory, "PROVIDERS", tuple(
        (name, lambda sctx: []) for name, _fn in factory.PROVIDERS))


# -- the Bridge page says what is true ----------------------------------------
