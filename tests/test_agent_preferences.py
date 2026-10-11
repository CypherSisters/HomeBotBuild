"""Per-build agent preferences — display names and toggles.

The safety-relevant property under test is a negative one: renaming the team is
cosmetic and cannot reach anything that routes, reviews, or holds a credential.
The rest is making sure a hand-edited config cannot produce a blank tile, an
invented agent, or a silently-ignored toggle.

Run: pytest tests/test_agent_preferences.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents import preferences as prefs


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    """No inherited env, no cached section, config pointed at a temp file."""
    monkeypatch.delenv("PUFFPAI_PLANNING_LOOP_ENABLED", raising=False)
    prefs.reset_cache()
    yield
    prefs.reset_cache()


def write_config(monkeypatch, tmp_path, agents_section) -> None:
    path = tmp_path / "puffpai_config.json"
    path.write_text(json.dumps({"agents": agents_section}), encoding="utf-8")
    monkeypatch.setattr(prefs, "_config_path", lambda: str(path))
    prefs.reset_cache()


# ── display names ───────────────────────────────────────────────────

def test_defaults_are_the_role_names(monkeypatch):
    monkeypatch.setattr(prefs, "_config_path", lambda: None)
    prefs.reset_cache()
    assert prefs.display_names()["concierge"] == "Concierge"
    assert prefs.display_name("caretaker") == "Caretaker"


def test_a_preset_renames_the_whole_team(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, {"name_preset": "default"})
    names = prefs.display_names()
    assert names == dict(prefs.PRESETS["default"])


def test_overrides_layer_on_top_of_a_preset(monkeypatch, tmp_path):
    """Take a preset, change one name, keep the rest — no restating six names."""
    write_config(monkeypatch, tmp_path, {
        "name_preset": "default", "names": {"caretaker": "Neptr"}})
    names = prefs.display_names()
    assert names["caretaker"] == "Neptr"
    assert names["concierge"] == prefs.PRESETS["default"]["concierge"]


def test_overrides_work_without_a_preset(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, {"names": {"steward": "Butlerbot"}})
    names = prefs.display_names()
    assert names["steward"] == "Butlerbot"
    assert names["sentinel"] == "Drawbridge"


def test_every_agent_always_has_a_name(monkeypatch, tmp_path):
    """A partial or junk config must never leave a blank on screen."""
    write_config(monkeypatch, tmp_path, {
        "name_preset": "no_such_preset",
        "names": {"concierge": "", "steward": None, "sentinel": 42}})
    names = prefs.display_names()
    assert set(names) == set(prefs.AGENT_IDS)
    assert all(names[a] for a in prefs.AGENT_IDS)
    assert names["concierge"] == "Concierge"   # empty → role name
    assert names["steward"] == "Steward"       # wrong type → role name


def test_names_cannot_invent_an_agent(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, {"names": {"ice_king": "Ice King"}})
    assert set(prefs.display_names()) == set(prefs.AGENT_IDS)


def test_names_are_sanitised(monkeypatch, tmp_path):
    """These reach a dashboard tile and a TTS voice."""
    write_config(monkeypatch, tmp_path, {
        "names": {"concierge": "Finn\n\tthe\x00 Human", "steward": "J" * 200}})
    names = prefs.display_names()
    assert names["concierge"] == "Finn the Human"
    assert len(names["steward"]) == prefs.MAX_NAME_LEN


def test_unknown_agent_id_returns_itself(monkeypatch):
    monkeypatch.setattr(prefs, "_config_path", lambda: None)
    prefs.reset_cache()
    assert prefs.display_name("nobody") == "nobody"


def test_unreadable_config_falls_back_silently(monkeypatch, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(prefs, "_config_path", lambda: str(path))
    prefs.reset_cache()
    assert prefs.display_names()["concierge"] == "Concierge"


# ── toggles ─────────────────────────────────────────────────────────

def test_flag_reads_config(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, {"planning_loop_enabled": True})
    assert prefs.flag("PUFFPAI_PLANNING_LOOP_ENABLED",
                      "planning_loop_enabled") is True


def test_flag_defaults_off_when_unset(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, {})
    assert prefs.flag("PUFFPAI_PLANNING_LOOP_ENABLED",
                      "planning_loop_enabled") is False


@pytest.mark.parametrize("env,expected", [("0", False), ("1", True),
                                          ("off", False), ("on", True)])
def test_environment_overrides_config(monkeypatch, tmp_path, env, expected):
    """An operator must be able to force a feature off from the unit file."""
    write_config(monkeypatch, tmp_path, {"planning_loop_enabled": True})
    monkeypatch.setenv("PUFFPAI_PLANNING_LOOP_ENABLED", env)
    assert prefs.flag("PUFFPAI_PLANNING_LOOP_ENABLED",
                      "planning_loop_enabled") is expected


def test_empty_environment_value_falls_through_to_config(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, {"planning_loop_enabled": True})
    monkeypatch.setenv("PUFFPAI_PLANNING_LOOP_ENABLED", "  ")
    assert prefs.flag("PUFFPAI_PLANNING_LOOP_ENABLED",
                      "planning_loop_enabled") is True


def test_planning_loop_reads_the_same_switch(monkeypatch, tmp_path):
    from puffpai.agents.planning_loop import planning_loop_enabled
    write_config(monkeypatch, tmp_path, {"planning_loop_enabled": True})
    assert planning_loop_enabled() is True
    monkeypatch.setenv("PUFFPAI_PLANNING_LOOP_ENABLED", "0")
    assert planning_loop_enabled() is False


# ── the negative property: names are cosmetic ───────────────────────

def test_renaming_does_not_move_the_bus_or_the_veto(monkeypatch, tmp_path):
    """The whole reason display names are a separate layer."""
    write_config(monkeypatch, tmp_path, {"names": {"concierge": "Finn"}})
    from puffpai.agents import build_team
    bus = build_team(engines={"concierge_engine": None})

    for agent_id in prefs.AGENT_IDS:
        assert bus.get(agent_id) is not None, "routing still uses internal ids"
    assert bus.get("Finn") is None, "a display name must not resolve on the bus"
    health = bus.health()
    assert health["reviewer"] == "sentinel"
    assert health["reviewer_wired"] is True
    assert bus.get("sentinel").scope.value == "sentinel"


def test_roster_reports_both_ids_and_display_names(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, {"names": {"archivist": "Princess Bubblegum"}})
    from puffpai.agents.registry import roster_summary
    rows = {r["name"]: r for r in roster_summary()}
    assert set(rows) == set(prefs.AGENT_IDS), "internal ids unchanged"
    assert rows["archivist"]["display_name"] == "Princess Bubblegum"
    assert rows["archivist"]["scope"] == "archivist"


def test_presets_cover_every_agent():
    for name, preset in prefs.PRESETS.items():
        assert set(preset) == set(prefs.AGENT_IDS), f"{name} preset is incomplete"
    assert "default" in prefs.available_presets()


# ── the planning loop must not eat the fast path ────────────────────
#
# Turning the loop on originally made it run FIRST, before any resolution, so
# every message paid for a planning call: a rule-matched question went from
# 0.03s to 14.2s live. These pin the gate that fixed it.

def test_a_resolved_single_step_request_skips_planning():
    from puffpai.agents.concierge import ConciergeAgent, resolve_intent
    text = "turn on the kitchen light"
    resolution = resolve_intent(text)
    assert resolution.reason != "no rule matched", "premise: the regex matches"
    assert ConciergeAgent._should_plan(text, resolution) is False


def test_a_multi_step_request_plans_even_when_resolved():
    from puffpai.agents.concierge import ConciergeAgent, resolve_intent
    text = "turn on the shed light and then take a picture of the shed"
    assert ConciergeAgent._should_plan(text, resolve_intent(text)) is True


def test_an_unresolvable_request_plans():
    """Where a plan is the only remaining way to make progress."""
    from puffpai.agents.concierge import ConciergeAgent, resolve_intent
    text = "make the house ready for guests this evening"
    resolution = resolve_intent(text)
    assert resolution.reason == "no rule matched", "premise: nothing matched"
    assert ConciergeAgent._should_plan(text, resolution) is True


def test_a_bare_and_does_not_trigger_planning():
    """'temperature and humidity' joins two nouns, not two actions."""
    from puffpai.agents.concierge import ConciergeAgent, resolve_intent
    text = "what is the temperature and humidity in the living room"
    resolution = resolve_intent(text)
    if resolution.reason != "no rule matched":
        assert ConciergeAgent._should_plan(text, resolution) is False


# ── the gate was always right; the router never let these through ───
#
# These four classified as RAG live on the Orin, so the Concierge never saw
# them and the loop never ran. Routing is fixed in puffpai.router; this pins
# the other half — that once they arrive, the gate sends the compound ones to
# the loop and still lets the single-step one take the fast path.

@pytest.mark.parametrize("text", [
    "check the temperature in the living room and then tell me "
    "what devices are here",
    "read the living room temperature and then show me every device",
    "list my devices and then check the living room temperature",
    "what is the temperature in the living room and then list my devices",
])
def test_misrouted_multi_step_house_requests_plan(text):
    from puffpai.agents.concierge import ConciergeAgent, resolve_intent
    assert ConciergeAgent._should_plan(text, resolve_intent(text)) is True


def test_the_single_step_sibling_still_skips_planning():
    """"what devices are here" is one lookup — it must not start costing a
    planning call just because its compound cousins now reach the Concierge."""
    from puffpai.agents.concierge import ConciergeAgent, resolve_intent
    text = "what devices are here"
    resolution = resolve_intent(text)
    assert resolution.intent == "list_devices"
    assert resolution.reason != "no rule matched", "premise: the regex matches"
    assert ConciergeAgent._should_plan(text, resolution) is False
