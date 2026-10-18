"""Wiring: the parts, connected.

W1  the summary's lines come from the parts' providers, one per section;
W2  "Looks good" builds what it approved, in the terminal and the browser;
W3  the one ability list hears from live cards, routines and books;
W4  the routines read the live cards and the books; the clock refreshes cards;
W5  one home block, read by live cards, the model's context and privacy;
W7  the Knowledge shelf lists downloaded books;
W9  every outside request carries the one descriptive name.

Nothing here reaches the network: the one sample look a live card needs is a
stand-in that returns a saved reply.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _wizard_driver import drive_in_process  # noqa: E402
from livedata_support import FIXTURES  # noqa: E402

from puffpai import brain_health, hardware_census, setup_ops  # noqa: E402
from puffpai.config import get_config  # noqa: E402
from puffpai.wizard import factory, netcheck, proposal, sources_suggest, wording  # noqa: E402

G4 = "A garden helper for my vegetable beds in Ohio"
G1 = ("I want an astronomy bot that knows everything about space, shows me cool "
      "space stuff, and works with my smart telescope")
PIN = "Telescope-Infinity-9042"


def _sctx(goal, internet=True, **kw):
    base = dict(goal=goal, terms=proposal.goal_terms(goal), name="Bot",
                mode=wording.STANDARD, shared={}, previews={},
                args=types.SimpleNamespace(), cfg=None, internet=internet)
    base.update(kw)
    return proposal.SummaryContext(**base)


# -- W1 -------------------------------------------------------------------------

@pytest.mark.parametrize("goal", [G4, G1])
def test_each_section_has_one_provider_and_ids_are_unique(goal):
    items, notes = proposal.collect_items(_sctx(goal))
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids))
    # No live info section: what the bot reads from outside
    # services is an ingredient of a job in the build plan.
    assert not [i for i in items if i["section"] == "live_info"
                or i["apply"]["kind"] in ("live_feed", "live_source")]
    updates = [i for i in items if i["section"] == "updates"]
    assert [i["id"] for i in updates] == ["updates:daily_update"]
    private = {i["id"] for i in items if i["section"] == "private"}
    assert {"private:vault", "private:chat_details"} <= private
    assert all(not proposal.toggleable(i) for i in items
               if i["section"] == "private")
    assert not [n for n in notes if "could not say" in n], notes


def test_gadget_lines_come_from_the_live_catalog_only(monkeypatch):
    # Gadget lines ("Your ..., if you have Home Assistant") are offered only
    # where Home Assistant is in use.
    from puffpai import setup_ops
    monkeypatch.setattr(setup_ops, "ha_configured", lambda: True)
    items, _ = proposal.collect_items(_sctx(G1))
    devices = [i for i in items if i["section"] == "devices"]
    assert any(i["id"].startswith("device:") for i in devices)
    assert all(i["id"].startswith(("device:", "devices:home_assistant"))
               for i in devices)
    monkeypatch.setattr(setup_ops, "ha_configured", lambda: False)
    items, _ = proposal.collect_items(_sctx(G1))
    assert not [i for i in items if i["id"].startswith("device:")]
    assert not [i for i in items
                if "if you have Home Assistant" in str(i.get("label"))]


def test_library_books_are_offered_once_whichever_list_found_them(monkeypatch):
    from puffpai.catalogs import knowledge_resolver
    books = knowledge_resolver.proposal_items(G1, [])
    assert books, "the saved library has books for a space goal"
    planned = {"items": [{"id": books[0]["apply"]["ref"]}]}
    items, _ = proposal.collect_items(_sctx(G1, previews={"sources": planned}))
    refs = [i["apply"]["ref"] for i in items if i["section"] == "knowledge"]
    assert books[0]["apply"]["ref"] not in refs
    assert all(i["apply"]["kind"] == "knowledge.add" for i in items
               if i["id"].startswith("knowledge:kiwix:"))


def test_an_offline_build_ticks_no_live_info_and_no_book():
    items, _ = proposal.collect_items(_sctx(G4, internet=False))
    for item in items:
        if item["section"] in ("live_info", "knowledge") and item["needs_yes"]:
            assert not item["default_on"], item["id"]


# -- W2: the applier ------------------------------------------------------------

def _ctx(items, *, online=True, shared=None):
    state = {"approved": True, "items": [
        dict(i, on=True) for i in items], "non_interactive": False}
    base = {"summary": state, "interview": {"goal": G4},
            "posture": {"build_network": "online" if online else "offline",
                        "run_egress": "enumerated"}}
    base.update(shared or {})
    return types.SimpleNamespace(shared=base, _sections={})


def _row(item_id, kind, ref, label="A thing"):
    return {"id": item_id, "section": "x", "label": label,
            "apply": {"kind": kind, "ref": ref}, "outside_host": None,
            "needs_yes": True, "size_bytes": None}


def test_looks_good_builds_each_kind_and_records_every_outcome(monkeypatch):
    from puffpai import offers
    from puffpai.knowledge import find_more
    added = []
    monkeypatch.setattr(find_more, "add", lambda ref, **kw: (
        added.append((ref, kw)) or {"ok": True, "status": "fetched"}))
    monkeypatch.setattr(offers, "apply_item", lambda apply, by="owner": {
        "ok": True, "message": "Done."})
    got = factory.apply_approved(_ctx([
        _row("knowledge:kiwix:x", "knowledge.add", "kiwix:x", "A book"),
        _row("live_info:api_seeds:seedinat", "live_feed", "seed:inaturalist",
             "Live: iNaturalist"),
        _row("updates:daily_update", "routine", "daily_update@08:00",
             "A daily garden update"),
        _row("devices:x", "ha_integration", "hue", "Your lights"),
        _row("private:vault", "none", ""),
        _row("thinking:brain", "model_pull", "qwen2.5:7b-instruct"),
    ]))
    assert added == [("kiwix:x", {"size_acknowledged": True, "via": "wizard"})]
    rows = got["shared"]["factory"]["items"]
    assert [(r["kind"], r["ok"]) for r in rows] == [
        ("knowledge.add", True), ("live_feed", False), ("routine", True),
        ("ha_integration", False)]
    # The live card is left to the build plan, said so, and is no gap.
    assert rows[1]["status"] == "waiting"
    # The one that could not be built is a plain gap, never silence.
    assert len(got["gaps"]) == 1
    assert got["gaps"][0]["what"] == "Your lights was not added."
    assert factory.added_lines(got["shared"]) == [
        "A book: downloaded. It is learned when your bot next starts.",
        "A daily garden update: switched on."]


def test_looks_good_never_makes_a_live_card(monkeypatch):
    """An approved live card line (an older summary) is never drafted or
    installed by the factory, online or not: the build plan makes it."""
    from puffpai import feed_generator
    monkeypatch.setattr(feed_generator, "draft_feed_app",
                        lambda *a, **k: pytest.fail("the factory drafted a live card"))
    monkeypatch.setattr(feed_generator, "install_draft",
                        lambda *a, **k: pytest.fail("the factory installed a live card"))
    for online in (True, False):
        got = factory.apply_approved(_ctx([_row("live_info:a:b", "live_feed", "b",
                                                "Live: Odd")], online=online))
        assert got["gaps"] == []
        assert got["shared"]["factory"]["items"][0]["status"] == "waiting"
    assert "live_feed" not in factory.BUILT_IN_KINDS
    with pytest.raises(ValueError):
        factory.register_plan_applier("live_feed", lambda item, ctx: {})


# -- W2 end to end, terminal and browser ------------------------------------------

@pytest.fixture
def garden_machine(monkeypatch, tmp_path):
    """Online, nothing downloaded, and every sample look answered from disk."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLUMNS", "80")
    get_config(_force_reload=True)
    census = hardware_census.run_census()
    census.setdefault("memory", {})["total_gb"] = 32.0
    monkeypatch.setattr(hardware_census, "run_census",
                        lambda *a, **k: copy.deepcopy(census))
    monkeypatch.setattr(setup_ops, "list_models", lambda *a, **k: [])
    monkeypatch.setattr(setup_ops, "ollama_survey", lambda *a, **k: [])
    monkeypatch.setattr(netcheck, "internet_reachable", lambda *a, **k: True)
    monkeypatch.setattr(sources_suggest, "suggest", lambda *a, **k: [])
    monkeypatch.setattr(brain_health, "pull_model", lambda *a, **k: {
        "ok": False, "message": "tests never download"})
    monkeypatch.setattr(setup_ops, "fetch_source", lambda *a, **k: {
        "ok": False, "reason": "tests never download"})
    from puffpai.security import egress_doors
    body = (FIXTURES / "samples" / "inat.json").read_bytes()
    looks = []

    def probe(url, *, host, build_online, max_bytes=0, **_kw):
        assert build_online is True
        looks.append(host)
        return {"status": 200, "content_type": "application/json",
                "bytes": len(body), "truncated": False, "body": body}
    monkeypatch.setattr(egress_doors, "build_probe_get", probe)
    yield looks
    get_config(_force_reload=True)


def _record() -> dict:
    with open(os.path.join(get_config().CONFIG_DIR, "wizard_run.json"),
              encoding="utf-8") as handle:
        return json.load(handle)


def _installed_and_allowed() -> list[str]:
    from puffpai import app_registry, feeds
    out = []
    for app_id in app_registry.installed_ids():
        try:
            if feeds.service_state(app_id)["state"] == "open":
                out.append(app_id)
        except Exception:
            continue
    return out


# -- W3 / W4 --------------------------------------------------------------------

def test_the_ability_list_hears_from_cards_routines_and_books():
    from puffpai import build_abilities, factory_wiring, feeds, routines
    from puffpai.knowledge import find_more
    wired = factory_wiring.connect()
    assert "routines:feed_reader" in wired
    for fn in (feeds.ability_rows, routines.ability_rows, find_more.ability_rows):
        assert fn in build_abilities.providers()
    ids = {row["id"] for row in build_abilities.abilities("a garden helper")}
    # The books row speaks for the one library row (no duplicate).
    assert "routines.daily_update" in ids and "knowledge.add_more" in ids
    assert "knowledge.books" not in ids and "knowledge_library" in ids


def test_routines_read_the_live_cards_and_the_books(monkeypatch):
    from puffpai import factory_wiring, feeds, routines
    monkeypatch.setattr(feeds, "feed_apps", lambda: [({"id": "sky"}, {})])
    monkeypatch.setattr(feeds, "card_state", lambda app_id, now=None: {
        "name": "Sky tonight", "credit": "NASA", "fetched_at": "2026-09-23",
        "card": {"kind": "value", "value": "clear", "unit": ""}})
    factory_wiring.connect()
    readings, problem = routines.read_feeds()
    assert readings == [{"id": "sky", "name": "Sky tonight", "credit": "NASA",
                         "value": "clear", "unit": "",
                         "updated_at": "2026-09-23"}]

    class Coll:
        def get(self, **kw):
            return {"documents": ["Tomatoes like sun. They need water daily."],
                    "metadatas": [{"source_label": "Garden book",
                                   "title": "Tomatoes"}]}
    picker = factory_wiring.library_picker(
        lambda: types.SimpleNamespace(knowledge_collection=Coll()))
    got = picker(["tomato"])
    assert got["credit"] == "Garden book"


# -- W5 -------------------------------------------------------------------------

def test_one_home_block_for_cards_context_and_privacy():
    from puffpai import build_context, feeds, home_location
    from puffpai.security import privacy_explainer
    home_location.set_home_coords(39.96118, -82.99879, label="Columbus, Ohio",
                                  timezone_name="America/New_York")
    assert feeds.home_block() == {"lat": 40.0, "lon": -83.0,
                                  "timezone": "America/New_York"}
    assert "Columbus, Ohio" in build_context.home_line()
    lines = privacy_explainer.plain_summary(vault=None, services=[],
                                            model_url="http://127.0.0.1:11435")
    assert any("town only (Columbus, Ohio)" in line for line in lines)


# -- W7 -------------------------------------------------------------------------

# -- W9 -------------------------------------------------------------------------

def test_every_outside_request_carries_the_one_descriptive_name():
    import puffpai
    from puffpai import source_fetch
    from puffpai.catalogs import live
    from puffpai.security import egress_doors
    # HomeBot names itself.
    assert puffpai.USER_AGENT.startswith("PuffPAI-HomeBot/")
    assert "https://github.com/CypherSisters/HomeBotBuild" in puffpai.USER_AGENT
    assert egress_doors.FEED_USER_AGENT == live.USER_AGENT == \
        source_fetch.USER_AGENT == puffpai.USER_AGENT


# -- W11 ------------------------------------------------------------------------

def test_the_closing_screen_check_finds_launchers_beside_the_install_record(
        tmp_path):
    """install.cmd -StateDir keeps the settings file in the state folder and
    the launchers in the install folder; the check looked in the wrong one and
    failed every installed build (found running install.cmd)."""
    from puffpai import prove
    root, state = tmp_path / "install", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    (state / "buildbot.env").write_text("PUFFPAI_PORT=8270\n", encoding="utf-8")
    (root / "run-backend.ps1").write_text("", encoding="utf-8")
    check = prove._check_handoff({"env_file": str(state / "buildbot.env"),
                                  "root": str(root),
                                  "backend": ".\\run-backend.ps1",
                                  "dashboard": "", "dashboard_url": ""})
    assert check["result"] == prove.PASS, check
