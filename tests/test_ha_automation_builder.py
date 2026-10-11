"""Household automations from a fixed schema.

"create a new workflow that turns off the living room sofa lights if no
motion after midnight every night" must build an automation, never answer
"I don't have a tool that can create or edit Home Assistant automations".
What is pinned here:

* ordinary phrasings parse into the closed schema, and the rule text resolves to
  entities that exist in the state list (Sofa 1, Sofa 2, Living Room Motion);
* nothing is invented: unknown targets ask a question;
* the payload the builder renders passes the enforcing allowlist in
  ha_provisioning, and that allowlist refuses locks, templates, area targets,
  blueprints, code-running services and unknown triggers on its own;
* a model slot-filler is validated like the parser and its entity ids dropped;
* the duplicate guard asks "replace or keep both" for the two existing
  automations that look like the same job, and "replace" files a switch-off, never a delete.

Run: python -m pytest -q -p no:cacheprovider tests/test_ha_automation_builder.py
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import ha_automation_builder as hab          # noqa: E402
from puffpai import ha_provisioning as hp                 # noqa: E402

TURN_1 = ("please create a new workflow that turns off the livingroom sofa "
          "lights if no motion after midnight every night. Then I want them "
          "to turn on automatically a half hour before sunset every night.")
TURN_4 = ("turns off the livingroom sofa lights if no motion after midnight "
          "every night. Then I want them to turn on automatically a half "
          "hour before sunset every night.")


def _st(eid, name, state="on", **attrs):
    return {"entity_id": eid, "state": state,
            "attributes": {"friendly_name": name, **attrs}}


#: A typical house's state list (entity ids and names, no secrets).
LIVE = [
    _st("light.living_room_sofa_1", "Sofa 1"),
    _st("light.living_room_sofa_2", "Sofa 2"),
    _st("light.desk_status_led", "Desk Status LED"),
    _st("light.home_assistant_voice_abc123_led_ring", "Voice LED Ring"),
    _st("light.kitchen", "Kitchen"),
    _st("binary_sensor.living_room_motion", "Living Room Motion", "off",
        device_class="motion"),
    _st("binary_sensor.living_room_occupancy", "Living Room Occupancy", "on",
        device_class="occupancy"),
    _st("binary_sensor.bedroom_motion", "Bedroom Motion", "on",
        device_class="motion"),
    _st("binary_sensor.back_door_motion", "Back Door Motion",
        "off", device_class="motion"),
    _st("automation.sofa_motion_lights_off", "Sofa Motion Lights Off"),
    _st("automation.sunset_lights_on", "Sunset Lights On"),
    _st("automation.sunrise_lights_off", "Sunrise Lights Off"),
    _st("scene.movie", "Movie", entity_id=["light.kitchen", "lock.front_door"]),
    _st("scene.reading", "Reading", entity_id=["light.kitchen"]),
]
LIVE_NO_DUPS = [s for s in LIVE if not s["entity_id"].startswith("automation.")]


@pytest.fixture(autouse=True)
def _clean():
    hab.clear_draft()
    yield
    hab.clear_draft()


@pytest.fixture
def proposals(monkeypatch):
    calls = []

    def fake(kind, object_id, payload, reason, requested_by="steward", **kw):
        hp._validate_payload(kind, payload)          # the enforcing copy
        calls.append({"kind": kind, "object_id": object_id, "payload": payload,
                      "reason": reason, **kw})
        return {"ok": True, "proposed": True,
                "entity_id": f"automation.{object_id}"}

    monkeypatch.setattr(hp, "propose_ha_change", fake)
    return calls


# ── detection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    TURN_1,
    "go ahead and make the automations yourself with your agent team and "
    "permissions in HA",
    "yes add my automation workflow",
    "you make the workflow",
    "make a new routine for the porch",
    "set up a rule for the hallway lights",
])
def test_author_requests_are_recognised(text):
    assert hab.is_author_request(text)


@pytest.mark.parametrize("text", [
    TURN_4,
    "turn off the sofa lights if no motion after midnight",
    "every night at 10 pm turn off the kitchen light",
    "when the laundry door opens turn on the kitchen light",
    "turn on the kitchen light when motion is detected",
])
def test_rule_shaped_sentences_are_recognised(text):
    assert hab.is_rule_shaped(text)


@pytest.mark.parametrize("text", [
    "what automations do I have",
    "which automations are running",
    "do I have an automation for the porch lights?",
    "do my automations work?",
    "is there a routine that turns off the lights at midnight",
])
def test_automation_questions_are_reads_not_proposals(text, proposals):
    assert hab.is_automation_question(text)
    assert not hab.is_author_request(text) and not hab.is_rule_shaped(text)
    assert hab.handle_turn(text, LIVE) is None
    assert proposals == []


@pytest.mark.parametrize("text", [
    "turn off the lights now",
    "turn on the sofa lights",
    "how are you today",
    "tell me about your agent team",
    "who is on my agent team",
    "set a timer for 10 minutes",
    "remind me every night at 9 to take my meds",
    "is the living room light on",
    "as a rule I like it quiet",
])
def test_negatives_are_not_claimed(text):
    assert not hab.claims_turn(text)


# ── parsing + resolution ────────────────────────────────────────────

def test_first_turn_parses_into_two_rules():
    rules = hab.parse_rules(TURN_1)
    assert [r["action"] for r in rules] == ["turn_off", "turn_on"]
    off, on = rules
    assert off["trigger"] == "no_motion" and off["window"] == "after_midnight"
    assert off["minutes"] == 30 and off["minutes_defaulted"] is True
    assert "sofa" in off["target_phrase"]
    assert on["trigger"] == "sun" and on["sun_event"] == "sunset"
    assert on["sun_offset_minutes"] == -30
    assert on["target_phrase"] == off["target_phrase"]     # "them"


def test_resolution_uses_live_entities_only():
    index = hab._states_index(LIVE)
    res = hab.resolve_rule(hab.parse_rules(TURN_1)[0], index)
    assert [t["entity_id"] for t in res.targets] == [
        "light.living_room_sofa_1", "light.living_room_sofa_2"]
    assert res.sensor["entity_id"] == "binary_sensor.living_room_motion"
    assert hab.summary(res) == (
        "Every night after midnight, if Living Room Motion has seen nothing "
        "for 30 minutes, turn off Sofa 1 and Sofa 2.")


def test_unknown_target_asks_instead_of_inventing():
    index = hab._states_index(LIVE)
    spec = hab.parse_rules("turn off the garage lights every night at 11 pm")[0]
    res = hab.resolve_rule(spec, index)
    assert res.targets == [] and "couldn't find" in res.question


def test_payload_passes_the_allowlist_and_alias_slug_is_the_object_id():
    index = hab._states_index(LIVE)
    for spec in hab.parse_rules(TURN_1):
        res = hab.resolve_rule(spec, index)
        payload = hab.build_payload(res)
        hp._validate_payload("automation", payload)
        alias, oid = hab.alias_and_object_id(res)
        assert hp._OBJECT_ID_RE.match(oid)
        assert hab._slug(payload["alias"]) == oid
        assert oid.startswith("puffpai_")
        rendered = json.dumps(payload)
        assert "{{" not in rendered and "lock." not in rendered


def test_after_midnight_payload_shape():
    index = hab._states_index(LIVE)
    res = hab.resolve_rule(hab.parse_rules(TURN_1)[0], index)
    p = hab.build_payload(res)
    assert p["trigger"][0] == {"platform": "time", "at": "00:00:00"}
    assert p["trigger"][1]["to"] == "off" and p["trigger"][1]["for"]["minutes"] == 30
    assert {"condition": "sun", "before": "sunrise"} in p["condition"]
    assert p["action"] == [{"service": "light.turn_off", "target": {
        "entity_id": ["light.living_room_sofa_1", "light.living_room_sofa_2"]}}]


# ── the enforcing allowlist, on its own ─────────────────────────────

def _auto(**over):
    base = {"alias": "PuffPAI test", "mode": "single",
            "trigger": [{"platform": "time", "at": "22:00:00"}],
            "condition": [],
            "action": [{"service": "light.turn_off",
                        "target": {"entity_id": ["light.kitchen"]}}]}
    base.update(over)
    return base


@pytest.mark.parametrize("payload", [
    _auto(action=[{"service": "lock.unlock", "target": {"entity_id": ["lock.front_door"]}}]),
    _auto(action=[{"service": "alarm_control_panel.alarm_disarm",
                   "target": {"entity_id": ["alarm_control_panel.home"]}}]),
    _auto(action=[{"service": "zha.permit", "data": {"duration": 254}}]),
    _auto(action=[{"service": "script.turn_on", "target": {"entity_id": ["script.x"]}}]),
    _auto(action=[{"service": "shell_command.x"}]),
    _auto(action=[{"service": "rest_command.exfil"}]),
    _auto(action=[{"service": "light.turn_off", "target": {"area_id": "kitchen"}}]),
    _auto(action=[{"service": "light.turn_off", "target": {"entity_id": ["lock.front_door"]}}]),
    _auto(action=[{"service": "{{ svc }}"}]),
    _auto(action=[{"service": "light.turn_on", "target": {"entity_id": ["light.kitchen"]},
                   "data": {"brightness_pct": "{{ 100 }}"}}]),
    _auto(action=[{"choose": []}]),
    _auto(action=[{"delay": "00:01:00"}]),
    _auto(trigger=[{"platform": "webhook", "webhook_id": "x"}]),
    _auto(trigger=[{"platform": "template", "value_template": "{{ true }}"}]),
    _auto(trigger=[{"platform": "time", "at": "input_datetime.x"}]),
    _auto(condition=[{"condition": "template", "value_template": "x"}]),
    _auto(use_blueprint={"path": "x.yaml"}),
    _auto(variables={"x": 1}),
    _auto(action=[{"service": "scene.turn_off", "target": {"entity_id": ["scene.movie"]}}]),
])
def test_allowlist_refuses_everything_outside_the_shape(payload):
    with pytest.raises(hp.ProvisioningError):
        hp._validate_payload("automation", payload)


def test_scene_with_a_lock_member_is_refused_at_apply_time():
    class Bridge:
        def get_all_states(self, force=False):
            return LIVE
    bad = _auto(action=[{"service": "scene.turn_on",
                         "target": {"entity_id": ["scene.movie"]}}])
    good = _auto(action=[{"service": "scene.turn_on",
                          "target": {"entity_id": ["scene.reading"]}}])
    assert "lock.front_door" in hp._automation_targets_safe(Bridge(), bad)
    assert hp._automation_targets_safe(Bridge(), good) is None


# ── model slot filler ───────────────────────────────────────────────

def test_model_fill_is_validated_and_entity_ids_are_dropped():
    reply = json.dumps([
        {"action": "turn_off", "target_phrase": "light.living_room_sofa_1",
         "trigger": "time", "time": "23:30"},
        {"action": "unlock", "target_phrase": "front door", "trigger": "time",
         "time": "01:00"},
        {"action": "turn_on", "target_phrase": "kitchen", "trigger": "webhook"},
    ])
    rules = hab.fill_with_model("whatever", lambda prompt: reply)
    assert len(rules) == 1
    assert rules[0]["target_phrase"] == "living room sofa 1"
    assert "." not in rules[0]["target_phrase"]


# ── end to end: duplicates, replace, keep both ──────────────────────

def test_duplicates_are_named_and_nothing_is_filed_until_answered(proposals):
    reply = hab.handle_turn(TURN_1, LIVE)
    assert "'Sofa Motion Lights Off'" in reply
    # A duplicate must share a TARGET word; "Sunset Lights On"
    # shares only the trigger and "lights", so it is not one.
    assert "Sunset Lights" not in reply
    assert "replace" in reply and "keep both" in reply
    assert proposals == []
    assert hab.pending_draft()["stage"] == "duplicate"


def test_replace_files_a_switch_off_of_the_old_ones(proposals):
    hab.handle_turn(TURN_1, LIVE)
    reply = hab.handle_turn("replace them", LIVE)
    assert len(proposals) == 2
    assert proposals[0]["replaces"] == ["automation.sofa_motion_lights_off"]
    assert proposals[1]["replaces"] == []
    assert "not delete" in reply and reply.endswith(hab.APPROVE_BOTH_TAIL)
    assert hab.pending_draft() is None


def test_keep_both_files_without_replaces(proposals):
    hab.handle_turn(TURN_1, LIVE)
    hab.handle_turn("keep both", LIVE)
    assert [p["replaces"] for p in proposals] == [[], []]


def test_no_duplicates_files_straight_away_with_the_plain_summary(proposals):
    reply = hab.handle_turn(TURN_1, LIVE_NO_DUPS)
    assert len(proposals) == 2
    assert reply == (
        "Every night after midnight, if Living Room Motion has seen nothing "
        "for 30 minutes, turn off Sofa 1 and Sofa 2. Every day half an hour "
        "before sunset, turn on Sofa 1 and Sofa 2." + hab.APPROVE_BOTH_TAIL)


def test_an_old_rule_in_history_is_never_built(proposals):
    # Only the current turn, or a pending draft, builds.
    history = [{"user": TURN_4, "assistant": "I don't have a tool..."}]
    reply = hab.handle_turn("you make the workflow", LIVE_NO_DUPS,
                            history=history)
    assert proposals == [] and reply == hab.ASK_FOR_RULE


def test_bare_author_request_without_history_asks_for_the_rule(proposals):
    assert hab.handle_turn("you make the workflow", LIVE) == hab.ASK_FOR_RULE
    assert proposals == []


# ── provisioning: replaces + per-call vault session, end to end ─────

ADMIN = "ADMIN-TOKEN-" + "d" * 40


class _Resp:
    def __init__(self, status=200):
        self.status_code = status
        self.content = b""
        self.text = ""

    def json(self):
        return {}


class _Session:
    calls = []

    def __init__(self):
        self.headers = {}

    def get(self, url, **kw):
        return _Resp(404)

    def post(self, url, json=None, **kw):
        _Session.calls.append((url, json))
        return _Resp(200)

    def close(self):
        pass


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
    monkeypatch.setattr(hp, "_ha_base_url", lambda: "http://ha.test")
    monkeypatch.setattr(hp, "VERIFY_ATTEMPTS", 1)
    monkeypatch.setattr(hp, "VERIFY_DELAY_SECONDS", 0.0)
    import requests
    _Session.calls = []
    monkeypatch.setattr(requests, "Session", _Session)
    yield cr
    hp.reset_state()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()


def test_apply_switches_off_replaced_and_uses_the_given_session(queue, monkeypatch, caplog):
    from puffpai.security import ha_token_source as hts
    from puffpai.ha_bridge import HUMAN_CONFIRMED
    seen_sids = []

    def resolve(session_id=None):
        seen_sids.append(session_id)
        return hts.Resolution(ADMIN, hts.SOURCE_VAULT_SESSION, "unlocked")

    monkeypatch.setattr(hts, "resolve_ha_admin_token", resolve)
    payload = _auto(alias="PuffPAI kitchen off at 2200")
    hp.propose_ha_change("automation", "puffpai_kitchen_off_at_2200", payload,
                         "Every day at 10:00 PM, turn off Kitchen.",
                         replaces=["automation.sunrise_lights_off"])
    req = queue.list_open()[0]
    assert "switch OFF automation.sunrise_lights_off (not delete it)" in req["reason"]
    approved = queue.resolve(req["entity_id"], approved=True, by="user_via_ui")

    class Bridge:
        def get_all_states(self, force=False):
            created = any("config/automation/config" in u for u, _ in _Session.calls)
            return [{"entity_id": "automation.puffpai_kitchen_off_at_2200",
                     "attributes": {}}] if created else []

    with caplog.at_level("DEBUG"):
        out = hp.apply_approved_change(request=approved, bridge=Bridge(),
                                       human_confirmed=HUMAN_CONFIRMED,
                                       vault_session_id="sid-123")
    assert out["ok"] and out["switched_off"] == ["automation.sunrise_lights_off"]
    assert seen_sids == ["sid-123"]
    urls = [u for u, _ in _Session.calls]
    assert urls[-1].endswith("/api/services/automation/turn_off")
    assert not any("delete" in u for u in urls)
    assert ADMIN not in caplog.text
    assert "is in Home Assistant and turned on" in out["message"]


def test_replaces_must_be_automations(queue):
    with pytest.raises(hp.ProvisioningError):
        hp.propose_ha_change("automation", "puffpai_x", _auto(), "why",
                             replaces=["lock.front_door"])
