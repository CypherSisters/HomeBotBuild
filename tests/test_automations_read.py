"""J5: "what automations do I have" is the UNION of the three stores.

"I don't have any personal automations configured"
after 25 s on the agent route, while the box held Home Assistant automations
and scripts, six saved workflows and a Node-RED runtime with flows on it.
"""

import pytest

from puffpai.agents import automations_read as ar

HA = [{"name": "Porch light at sunset"}, {"name": "Away mode"}]
SCRIPTS = [{"name": "Goodnight"}]
SAVED = [{"intent": f"workflow {n}"} for n in range(6)]
FLOWS = [{"name": "daily briefing"}, {"name": "weather alerts"}]


# ── the trigger table ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "What automations do I have?",
    "what automations do i have",
    "which automations do I have",
    "how many automations do I have",
    "do i have any automations",
    "list my automations",
    "show me my workflows",
    "what workflows do I have",
    "what routines do I have",
    "what node-red flows do I have",
    "tell me what automations are set up",
])
def test_every_read_phrasing_matches(text):
    assert ar.is_automations_question(text)


@pytest.mark.parametrize("text", [
    "create an automation that turns the porch light on",
    "can you make a new workflow for me",
    "set up an automation for the lights",
    "write me a node-red flow",
    "what can you do",
    "turn on the porch light",
])
def test_write_requests_and_unrelated_turns_are_declined(text):
    assert not ar.is_automations_question(text)


# ── the union, and the false negative that must be impossible ────────────

def test_no_source_may_be_dropped():
    reply = ar.reply_for(HA, SCRIPTS, SAVED, FLOWS)
    assert "2 Home Assistant automations" in reply
    assert "1 script" in reply
    assert "6 saved workflows" in reply
    assert "2 Node-RED flows" in reply


@pytest.mark.parametrize("kwargs", [
    {"ha_automations": HA},
    {"ha_scripts": SCRIPTS},
    {"saved": SAVED},
    {"nodered": FLOWS},
])
def test_none_configured_is_impossible_when_any_source_is_non_empty(kwargs):
    reply = ar.reply_for(**kwargs)
    assert "Nothing is set up" not in reply
    assert reply.startswith("You have ")


def test_an_unreadable_nodered_is_mentioned_without_a_count():
    reply = ar.reply_for(HA, [], [], [], nodered_readable=False)
    assert "your Node-RED flows" in reply
    assert "0 Node-RED" not in reply
    assert "Nothing is set up" not in reply


def test_an_unreadable_nodered_alone_still_never_says_none():
    reply = ar.reply_for([], [], [], [], nodered_readable=False)
    assert "Nothing is set up" not in reply
    assert "your Node-RED flows" in reply


def test_every_source_readable_and_empty_may_say_none():
    reply = ar.reply_for([], [], [], [], nodered_readable=True)
    assert reply.startswith("Nothing is set up right now")
    assert ar.BOUNDARY in reply


# ── names and the standing boundary ──────────────────────────────────────

def test_at_most_five_names_are_spoken():
    reply = ar.reply_for(HA, SCRIPTS, SAVED, FLOWS)
    listed = reply.split("They include: ", 1)[1].split(".")[0]
    assert len(listed.split(", ")) == ar.MAX_NAMES == 5


def test_the_boundary_sentence_stays():
    assert ar.reply_for(HA).endswith(ar.BOUNDARY)
    assert "draft a new Home Assistant automation" in ar.BOUNDARY
    assert "approve" in ar.BOUNDARY


def test_singular_counts_read_naturally():
    reply = ar.reply_for([{"name": "Away mode"}])
    assert "1 Home Assistant automation." in reply
    assert "It's: Away mode." in reply


def test_duplicate_names_are_counted_once():
    reply = ar.reply_for([{"name": "Away mode"}, {"name": "Away mode"}])
    assert "1 Home Assistant automation" in reply


def test_entity_ids_are_used_when_there_is_no_friendly_name():
    reply = ar.reply_for([{"entity_id": "automation.porch"}])
    assert "automation.porch" in reply


# ── the dispatcher wiring ───────────────────────────────────────────────

def test_it_is_registered_once_and_ahead_of_the_registry_reads():
    from tests.test_instant_answer_precedence import _call_order

    order = [name for _lineno, name in _call_order()]
    assert order.count("_try_automations_read") == 1
    assert order.index("_try_automations_read") < order.index("_try_registry_read")
    assert order.index("_try_automations_read") < order.index("_try_capability_read")


def test_an_unreadable_source_never_becomes_an_empty_one(monkeypatch):
    from puffpai import api_server

    class DeadBridge:
        def get_all_states(self):
            raise RuntimeError("HA unreachable")

    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: DeadBridge())
    monkeypatch.setattr(api_server, "_get_agent_team", lambda: None)
    monkeypatch.setattr(api_server, "_bridge_list_workflows",
                        lambda: {"success": False, "result": {"error": "down"}})
    reply = api_server._try_automations_read("what automations do I have")
    assert "Nothing is set up" not in reply
    assert "your Node-RED flows" in reply


def test_the_live_union_is_read_from_all_three_stores(monkeypatch):
    from puffpai import api_server

    class Bridge:
        def get_all_states(self):
            return [
                {"entity_id": "automation.porch",
                 "attributes": {"friendly_name": "Porch light at sunset"}},
                {"entity_id": "script.goodnight",
                 "attributes": {"friendly_name": "Goodnight"}},
                {"entity_id": "light.kitchen", "attributes": {}},
            ]

    class Majordomo:
        def list_tasks(self):
            return SAVED

    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: Bridge())
    monkeypatch.setattr(api_server, "_get_agent_team",
                        lambda: {"majordomo": Majordomo()})
    monkeypatch.setattr(api_server, "_bridge_list_workflows",
                        lambda: {"success": True,
                                 "result": {"workflows": FLOWS}})
    reply = api_server._try_automations_read("What automations do I have?")
    assert "1 Home Assistant automation" in reply
    assert "1 script" in reply
    assert "6 saved workflows" in reply
    assert "2 Node-RED flows" in reply
    # A light is not an automation.
    assert "kitchen" not in reply.lower()
