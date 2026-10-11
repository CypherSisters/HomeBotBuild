"""Hardening of HA management: PIN handling, scoping and model limits.

Each block pins one rule:

1. PIN leak: with a slot open, a PIN on ANY chat entry point is caught before
   routing; it never reaches a model, history, per-user memory or a log, the
   turn is registered private, and a spaced PIN is normalised.
2. Model-made approvals: the LLM tools list requests and approve NOTHING.
3. Validator holes: service+action, platform+trigger, trailing newline,
   non-ASCII ids, risky switches, empty/odd modes; scene/switch members at
   propose; an approval is not spent by a refused apply; a named record is the
   only record applied.
4. Questions and deletes never file proposals; a builder failure is a plain
   reply, not a fall-through.
5. The streaming paths reach the HA handlers.
Also: duplicate match needs a shared target; a cancelled PIN prompt voids the
approval; the slot is per conversation; the close report goes to its thread.

Run: python -m pytest -q -p no:cacheprovider tests/test_ha_validator_and_routing.py
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_ha_automation_builder import LIVE, LIVE_NO_DUPS, TURN_1, _auto   # noqa: E402

from puffpai import ha_automation_builder as hab        # noqa: E402
from puffpai import ha_provisioning as hp               # noqa: E402

PIN = "4821"


class _Bridge:
    def __init__(self, states=LIVE):
        self.states = states

    def get_all_states(self, force=False):
        return self.states


@pytest.fixture
def api(monkeypatch):
    from puffpai import api_server as a

    class _Tools:
        _ha_bridge = _Bridge()
        _llm_chat = None

        def run(self, *args, **kw):
            raise AssertionError("the tool agent must not run")

    monkeypatch.setattr(a, "_get_assistant_tools", lambda: _Tools())
    hab.clear_draft()
    a._ha_pin_reset()
    yield a
    hab.clear_draft()
    a._ha_pin_reset()


@pytest.fixture
def model(api, monkeypatch):
    """Records anything that would reach a model or be persisted."""
    seen = {"llm": [], "saved": [], "memory": [], "applied": []}

    def fake_stream(msg, *a, **k):
        seen["llm"].append(msg)
        yield "MODEL"

    monkeypatch.setattr(api, "_stream_ollama_tokens", fake_stream)
    monkeypatch.setattr(api, "_save_conversation",
                        lambda conv: seen["saved"].append(
                            [m.get("content") for m in conv.get("messages", [])]))
    monkeypatch.setattr(api, "_remember_for_user",
                        lambda uid, text: seen["memory"].append(text))
    monkeypatch.setattr(api, "_apply_ha_changes_with_pin",
                        lambda key, pin: seen["applied"].append((key, pin))
                        or "applied-for-you")
    return seen


def _client(api):
    return api.app.test_client(), {"X-API-Key": api._API_KEY}


ENTRY = [
    ("/chat/stream", lambda m, c: {"message": m, "conversation_id": c}),
    ("/v1/chat/completions", lambda m, c: {"model": "puffpai", "stream": True,
                                           "conversation_id": c,
                                           "messages": [{"role": "user", "content": m}]}),
    ("/v1/chat/completions", lambda m, c: {"model": "puffpai", "stream": False,
                                           "conversation_id": c,
                                           "messages": [{"role": "user", "content": m}]}),
    ("/api/chat", lambda m, c: {"conversation_id": c,
                                "messages": [{"role": "user", "content": m}]}),
    ("/conversation", lambda m, c: {"text": m, "conversation_id": c}),
    ("/chat/send", lambda m, c: {"message": m, "conversation_id": c}),
]


# ── 1. PIN leak ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path,body", ENTRY)
@pytest.mark.parametrize("typed", [PIN, "4 8 2 1"])
def test_pin_is_caught_before_routing_on_every_entry_point(
        api, model, capsys, path, body, typed):
    client, headers = _client(api)
    api._ha_pin_slot_add("automation.puffpai_sofa_off", "conv-pin")
    resp = client.post(path, json=body(typed, "conv-pin"), headers=headers)
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200, text
    assert "applied-for-you" in text
    assert model["applied"] == [("conv-pin", PIN)]
    assert model["llm"] == []
    assert not any(PIN in str(m) or typed in str(m) for m in model["saved"])
    assert model["memory"] == []
    assert api._is_registered_private_turn(typed)
    out = capsys.readouterr().out
    assert PIN not in out and typed not in out


@pytest.mark.parametrize("path,body", ENTRY)
def test_a_pin_in_another_conversation_is_kept_private_but_not_used(
        api, model, path, body):
    # Unkeyed slot, PIN arrives on another conversation.
    client, headers = _client(api)
    api._ha_pin_slot_add("automation.puffpai_sofa_off")
    text = client.post(path, json=body(PIN, "probe-1"),
                       headers=headers).get_data(as_text=True)
    assert "it looks like a PIN" in text
    assert model["applied"] == [] and model["llm"] == []
    assert not any(PIN in str(m) for m in model["saved"])
    assert api._is_registered_private_turn(PIN)
    assert api._ha_pin_slot_ids() == ["automation.puffpai_sofa_off"]


def test_a_pin_with_no_slot_is_not_caught_and_builds_nothing(api, model):
    assert api._ha_pin_intercept(PIN, "conv-x") is None
    assert not hab.claims_turn(PIN)
    hab.handle_turn(TURN_1, LIVE)                  # a pending duplicate draft
    reply = hab.handle_turn(PIN, LIVE)
    assert PIN not in (reply or "")


def test_cancel_voids_the_approval_at_once(api):
    record = {"entity_id": "automation.puffpai_x",
              "resolved_at": "2026-10-09T06:00:00+00:00"}
    api._ha_pin_slot_add("automation.puffpai_x", "c1", record)
    assert "cancelled" in api._try_ha_change_pin_followup("cancel", "c1")
    assert record["resolved_at"] in hp._spent_approvals
    hp._spent_approvals.discard(record["resolved_at"])


# ── 2. model-made approvals ─────────────────────────────────────────

@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFFPAI_HA_PROVISIONING", "1")
    import puffpai.agent_activity as aa
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()
    hp.reset_state()
    yield cr
    hp.reset_state()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()


def test_the_llm_approval_tools_approve_nothing(queue, monkeypatch):
    from puffpai import zigbee_pairing as zp
    from puffpai.assistant_tools import AssistantTools
    for eid, action in (("zha.pairing_window", "zigbee_pair"),
                        ("automation.puffpai_x", "ha_provision"),
                        ("light.new_bulb", "control_device")):
        queue.create_request(entity_id=eid, label=eid, action=action,
                             reason="r", requested_by="steward", context={})
    fired = []
    monkeypatch.setattr(queue, "resolve", lambda *a, **k: fired.append("resolve"))
    monkeypatch.setattr(hp, "apply_approved_change",
                        lambda **k: fired.append("apply"))
    monkeypatch.setattr(zp, "complete_approved_pairing",
                        lambda *a, **k: fired.append("pair"))

    class Watcher:
        def get_pending(self):
            class D:
                entity_id, friendly_name = "light.new_bulb", "New Bulb"
            return [D()]

        def approve_device(self, *a, **k):
            fired.append("watcher-approve")

    import puffpai.ha_device_watcher as hdw
    monkeypatch.setattr(hdw, "get_watcher", lambda: Watcher())
    tools = AssistantTools().tool_dict
    for arg in ("", "zha.pairing_window", "automation.puffpai_x", "light.new_bulb"):
        out = tools["sentinel_approve_device"].invoke({"device_name": arg})
        assert "can't approve" in out
    out = tools["ha_approve_pending_device"].invoke({"device_and_mode": ""})
    assert "can't approve it myself" in out
    assert fired == []
    assert {r["status"] for r in queue._load()} == {"open"}


# ── 3. validator holes ──────────────────────────────────────────────

T = [{"platform": "time", "at": "10:00:00"}]


@pytest.mark.parametrize("payload", [
    {"trigger": T, "action": [{"service": "light.turn_on", "action": "lock.unlock",
                               "target": {"entity_id": "light.a"}}]},
    {"trigger": T, "action": [{"action": "lock.unlock", "service": "light.turn_on",
                               "target": {"entity_id": "light.a"}}]},
    {"trigger": [{"platform": "time", "trigger": "homeassistant", "at": "10:00"}],
     "action": [{"service": "light.turn_on", "target": {"entity_id": "light.a"}}]},
    {"trigger": T, "action": [{"service": "light.turn_on",
                               "target": {"entity_id": "light.a\n"}}]},
    {"trigger": T, "action": [{"service": "light.turn_on",
                               "target": {"entity_id": "light.а"}}]},
    {"trigger": T, "action": [{"service": "switch.toggle",
                               "target": {"entity_id": "switch.garage_door_opener"}}]},
    {"trigger": T, "action": [{"service": "switch.turn_on", "target": {
        "entity_id": "switch.zigbee2mqtt_bridge_permit_join"}}]},
    {"action": [{"service": "light.turn_on", "target": {"entity_id": "light.a"}}]},
    {"trigger": T, "action": []},
    {"trigger": T, "mode": "parallel", "max": 100000,
     "action": [{"service": "light.turn_on", "target": {"entity_id": "light.a"}}]},
])
def test_validator_holes_are_closed(payload):
    with pytest.raises(hp.ProvisioningError):
        hp._validate_payload("automation", payload)


def test_outdoor_switch_is_not_a_door():
    hp._validate_payload("automation", _auto(action=[{
        "service": "switch.turn_off", "target": {"entity_id": ["switch.outdoor_fountain"]}}]))


def test_scene_and_switch_members_are_checked_at_propose(queue):
    states = [{"entity_id": "scene.unlock_all", "attributes": {
                  "entity_id": ["lock.front_door"]}},
              {"entity_id": "switch.relay_1", "attributes": {
                  "friendly_name": "Garage Opener"}}]
    for target in ("scene.unlock_all", "switch.relay_1"):
        service = target.split(".")[0] + ".turn_on"
        with pytest.raises(hp.ProvisioningError):
            hp.propose_ha_change("automation", "puffpai_t", _auto(action=[{
                "service": service, "target": {"entity_id": [target]}}]),
                "why", bridge=_Bridge(states))
    assert queue.list_open() == []


def test_a_refused_apply_does_not_spend_the_approval(queue, monkeypatch):
    from puffpai.security import ha_token_source as hts
    from puffpai.ha_bridge import HUMAN_CONFIRMED
    monkeypatch.setattr(hts, "resolve_ha_admin_token", lambda session_id=None:
                        hts.Resolution("ADMIN-" + "x" * 40,
                                       hts.SOURCE_VAULT_SESSION, "u"))
    hp.propose_ha_change("automation", "puffpai_kitchen_off", _auto(), "why")
    approved = queue.resolve("automation.puffpai_kitchen_off", approved=True,
                             by="user_via_ui")
    exists = _Bridge([{"entity_id": "automation.puffpai_kitchen_off",
                       "attributes": {}}])
    out = hp.apply_approved_change(request=approved, bridge=exists,
                                   human_confirmed=HUMAN_CONFIRMED)
    assert out["already_exists"] is True
    assert approved["resolved_at"] not in hp._spent_approvals


def test_a_named_record_is_the_only_record_applied(queue):
    hp.propose_ha_change("automation", "puffpai_a", _auto(), "why")
    queue.resolve("automation.puffpai_a", approved=True, by="user_via_ui")
    stale = {"entity_id": "automation.puffpai_a",
             "resolved_at": "2026-01-01T00:00:00+00:00"}
    assert hp._recent_approval(stale) is None
    assert hp._recent_approval(None) is not None


# ── 4. questions, deletes, failures ─────────────────────────────────

@pytest.fixture
def proposals(monkeypatch):
    calls = []
    monkeypatch.setattr(hp, "propose_ha_change", lambda *a, **k: calls.append(a)
                        or {"proposed": True})
    monkeypatch.setattr(hp, "propose_automation_off",
                        lambda eid, name, **k: calls.append(("off", eid))
                        or {"proposed": True})
    return calls


HISTORY = [{"role": "user",
            "content": "turn off the sofa lights if no motion after midnight every night"}]


@pytest.mark.parametrize("text", [
    "do I have an automation for the porch lights?", "do my automations work?",
    "is there a routine that turns off the lights at midnight",
    "I always forget to turn off the lights",
    "whenever you get a chance turn off the kitchen lights",
])
@pytest.mark.parametrize("history", [None, HISTORY])
def test_questions_never_file(proposals, text, history):
    hab.handle_turn(text, LIVE_NO_DUPS, history=history)
    assert proposals == []


def test_delete_offers_a_switch_off_through_consent(proposals):
    reply = hab.handle_turn("delete the automation that turns off the sofa "
                            "lights", LIVE)
    assert reply.startswith("I can't delete automations, only switch them off.")
    assert "'Sofa Motion Lights Off'" in reply and "Approve?" in reply
    assert proposals == [("off", "automation.sofa_motion_lights_off")]


def test_a_failing_builder_is_a_plain_reply(api, monkeypatch):
    monkeypatch.setattr(hp, "propose_ha_change",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(api, "_ha_states_for_turn", lambda: LIVE_NO_DUPS)
    reply = api._try_instant_answer("turn off the sofa lights if no motion")
    assert "nothing was changed" in reply


def test_a_question_reaches_the_read_answer(api, proposals):
    reply = api._try_automation_author("do I have an automation for the porch lights?")
    assert reply and "Approve?" not in reply and proposals == []


# ── 5. streaming paths reach the HA handlers ────────────────────────

STREAMS = [ENTRY[0], ENTRY[1]]


def _stream_reply(api, path, body, msg, conv="conv-s"):
    client, headers = _client(api)
    return client.post(path, json=body(msg, conv), headers=headers).get_data(
        as_text=True)


@pytest.mark.parametrize("path,body", STREAMS)
@pytest.mark.parametrize("answer,expect", [("replace it", "Approve?"),
                                           ("keep both", "Approve?"),
                                           ("cancel", "I won't make it")])
def test_draft_answers_on_streams(api, model, proposals, path, body, answer, expect):
    hab.handle_turn("turn off the sofa lights if no motion", LIVE)
    assert hab.pending_draft() is not None
    text = _stream_reply(api, path, body, answer)
    assert expect in text and model["llm"] == []


@pytest.mark.parametrize("path,body", STREAMS)
def test_yes_reaches_the_consent_handler_on_streams(api, model, monkeypatch, path, body):
    hits = []
    monkeypatch.setattr(api, "_try_consent_approval",
                        lambda m, c=None: hits.append(m) or "CONSENT-HANDLED")
    assert "CONSENT-HANDLED" in _stream_reply(api, path, body, "yes")
    assert model["llm"] == []


@pytest.mark.parametrize("path,body", STREAMS)
def test_reopen_and_see_lights_on_streams(api, model, monkeypatch, path, body):
    from puffpai import zigbee_pairing as zp
    monkeypatch.setattr(zp, "request_pairing",
                        lambda *a, **k: {"message": "REOPEN-FILED"})
    zp._set_reopen_offer()
    assert "REOPEN-FILED" in _stream_reply(api, path, body, "yes, open it again")
    zp.clear_reopen_offer()
    monkeypatch.setattr(zp, "read_report", lambda: {})
    assert "I can see" in _stream_reply(api, path, body, "do you see the bulbs")
    assert model["llm"] == []


# ── also-fix items ──────────────────────────────────────────────────

def test_name_only_duplicates_do_not_count(proposals):
    states = LIVE_NO_DUPS + [
        {"entity_id": "automation.garage_motion_lights_off", "state": "on",
         "attributes": {"friendly_name": "Garage motion lights off"}},
        {"entity_id": "automation.kitchen_lights_off_at_night", "state": "on",
         "attributes": {"friendly_name": "Kitchen lights off at night"}}]
    reply = hab.handle_turn("turn off the sofa lights if no motion", states)
    assert "Garage" not in reply and "Approve?" in reply


def test_risky_switch_target_is_refused_with_a_reason(proposals):
    states = LIVE_NO_DUPS + [{"entity_id": "switch.relay_1", "state": "off",
                              "attributes": {"friendly_name": "Garage Opener"}}]
    reply = hab.handle_turn("turn on the garage opener switch every night at 10 pm",
                            states)
    assert "won't put Garage Opener in an automation" in reply
    assert proposals == []


def test_close_report_goes_to_the_thread_that_started_pairing(api, monkeypatch):
    saved = []
    monkeypatch.setattr(api, "_save_conversation", lambda c: saved.append(c["id"]))
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(api, "_chat_history", [
        {"id": "conv-start", "messages": [{"role": "user", "content": "pair",
                                           "timestamp": "2026-10-09T01:00:00+00:00"}]},
        {"id": "conv-newer", "messages": [{"role": "user", "content": "hi",
                                           "timestamp": now}]}])
    assert api._post_to_recent_thread("closed", "conv-start")
    assert saved == ["conv-start"]
