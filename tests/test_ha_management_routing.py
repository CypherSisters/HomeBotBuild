"""HA management routing.

Pins, as they reach api_server:

* five ordinary automation phrasings reach the builder through _try_instant_answer,
  the layer BOTH _process_chat_query (/v1/chat/completions) and
  _process_voice_query (/api/chat, voice) consult before routing; the compound
  splitter stands down so a rule is never executed as "turn off now";
* "agent team" questions still get the roster; reads, small talk and plain
  commands are not claimed;
* an approval with the vault locked asks for the PIN inline, the PIN turn is
  vault-private, opens a session for the change only and logs out;
* "do you see the bulbs" is answered from live state; "pair both zigbee bulbs"
  is a pairing request; "yes, open it again" files a new pairing request.

Run: python -m pytest -q -p no:cacheprovider tests/test_ha_management_routing.py
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ha_automation_builder import LIVE, TURN_1, TURN_4      # noqa: E402

from puffpai import ha_automation_builder as hab                # noqa: E402
from puffpai import ha_provisioning as hp                       # noqa: E402

FIVE_TURNS = [
    TURN_1,
    "go ahead and make the automations yourself with your agent team and "
    "permissions in HA",
    "yes add my automation workflow",
    TURN_4,
    "you make the workflow",
]


class _Bridge:
    def __init__(self, states):
        self.states = states

    def get_all_states(self, force=False):
        return self.states


@pytest.fixture
def api(monkeypatch):
    from puffpai import api_server

    class _Tools:
        _ha_bridge = _Bridge(LIVE)
        _llm_chat = None

    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _Tools())
    hab.clear_draft()
    api_server._ha_pin_reset()
    yield api_server
    hab.clear_draft()
    api_server._ha_pin_reset()


@pytest.fixture
def proposals(monkeypatch):
    calls = []

    def fake(kind, object_id, payload, reason, requested_by="steward", **kw):
        hp._validate_payload(kind, payload)
        calls.append({"object_id": object_id, "payload": payload, **kw})
        return {"ok": True, "proposed": True}

    monkeypatch.setattr(hp, "propose_ha_change", fake)
    return calls


def _instant(api, text, history=None):
    return api._try_instant_answer(text, conversation_history=history,
                                   conversation_id="t-conv")


# ── the five phrasings ──────────────────────────────────────────────

def test_five_turns_in_order_reach_the_builder(api, proposals):
    history = []
    replies = []
    for text in FIVE_TURNS:
        reply = _instant(api, text, history)
        assert reply, text
        assert "seven AI agents" not in reply
        assert "don't have a tool" not in reply
        replies.append(reply)
        history.append({"user": text, "assistant": reply})
    # 1: both rules summarised, and the two existing automations named.
    assert "turn off Sofa 1 and Sofa 2" in replies[0]
    assert "'Sofa Motion Lights Off'" in replies[0]
    # 2 and 3 do not answer the replace/keep question, so it is asked again.
    assert "keep both" in replies[1] and "keep both" in replies[2]
    assert proposals == []


@pytest.mark.parametrize("text", FIVE_TURNS)
def test_each_phrasing_alone_never_builds_from_history(api, proposals, text):
    history = [{"user": TURN_1, "assistant": "earlier reply"}]
    reply = _instant(api, text, history)
    assert reply
    if text in (TURN_1, TURN_4):
        assert "Sofa 1 and Sofa 2" in reply
    else:
        assert reply == hab.ASK_FOR_RULE


def test_the_duplicate_answer_files_through_the_consent_path(api, proposals):
    _instant(api, TURN_1)
    reply = _instant(api, "replace them")
    assert len(proposals) == 2
    assert reply.endswith(hab.APPROVE_BOTH_TAIL)      # one ask, two requests


@pytest.mark.parametrize("text", [
    "what automations do I have",
    "turn off the lights now",
    "how are you today",
    "set a timer for 10 minutes",
])
def test_negatives_are_not_answered_by_the_builder(api, proposals, monkeypatch, text):
    reply = api._try_automation_author(text)
    assert proposals == []
    if text == "what automations do I have":
        assert reply and "Approve?" not in reply      # the READ answer
    else:
        assert reply is None


def test_agent_team_questions_keep_the_roster(api):
    assert api._try_automation_author("tell me about your agent team") is None
    assert api._try_automation_author("who is on my agent team") is None


def test_compound_splitter_stands_down_for_rules(api):
    def boom(_clause):
        raise AssertionError("a rule must never be executed clause by clause")
    assert api._try_compound_request(TURN_4, boom) is None
    assert api._try_compound_request(
        "turn off the sofa lights if no motion after midnight", boom) is None


def test_both_pipelines_consult_the_same_layers_first():
    from puffpai import api_server
    for fn in (api_server._process_chat_query_unwrapped,
               api_server._process_voice_query_unwrapped):
        src = inspect.getsource(fn)
        assert src.index("_try_compound_request(") < src.index("_try_instant_answer(")
    layer = inspect.getsource(api_server._try_instant_answer)
    assert layer.index("_try_automation_author(") < layer.index("_is_compound_request(")
    assert layer.index("_try_automation_author(") < layer.index("_try_list_agents(")


# ── approval with a locked vault: PIN inline ────────────────────────

ADMIN = "ADMIN-TOKEN-" + "e" * 40
PIN = "482913"


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
    yield cr
    hp.reset_state()
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()


class _VaultSession:
    session_id = "sid-pin-1"


class _Vault:
    is_initialized = True

    def __init__(self):
        self.logged_out = []
        self.auth_calls = 0

    def authenticate_mfa(self, pin, totp_code=None):
        from puffpai.security.vault import AuthenticationError
        self.auth_calls += 1
        if pin != PIN:
            raise AuthenticationError("bad pin")
        return _VaultSession()

    def logout(self, sid):
        self.logged_out.append(sid)


def test_locked_vault_approval_asks_for_the_pin_and_the_pin_turn_applies(
        api, queue, monkeypatch, capsys, caplog):
    from puffpai.security import ha_token_source as hts
    from puffpai import consent_voice

    def resolve(session_id=None):
        if session_id == "sid-pin-1":
            return hts.Resolution(ADMIN, hts.SOURCE_VAULT_SESSION, "unlocked")
        return hts.Resolution(None, None, hts.ADMIN_LOCKED_DETAIL)

    monkeypatch.setattr(hts, "resolve_ha_admin_token", resolve)
    applied = []
    real_apply = hp.apply_approved_change

    def spy(**kw):
        applied.append(kw.get("vault_session_id"))
        if kw.get("vault_session_id"):
            return {"ok": True, "message": "Done. 'PuffPAI x' is in Home Assistant and turned on."}
        return real_apply(**kw)

    monkeypatch.setattr(hp, "apply_approved_change", spy)
    vault = _Vault()
    monkeypatch.setattr(api, "_get_vault", lambda: vault)
    monkeypatch.setattr(api, "_vault_display_rl_check", lambda: (True, 0))
    monkeypatch.setattr(api, "_vault_display_rl_note", lambda: None)

    hp.propose_ha_change("automation", "puffpai_kitchen_off_at_2200", {
        "alias": "PuffPAI kitchen off at 2200",
        "trigger": [{"platform": "time", "at": "22:00:00"}],
        "action": [{"service": "light.turn_off",
                    "target": {"entity_id": ["light.kitchen"]}}]}, "why",
        conversation_id="t-conv")

    first = api._try_consent_approval("yes", "t-conv")
    assert first.startswith("Approved.") and api.HA_PIN_ASK in first
    assert "unlock" not in first.lower().replace("unlocked", "")
    assert applied == [None]

    with caplog.at_level("DEBUG"):
        reply = _instant(api, PIN)
    out = capsys.readouterr().out
    assert "is in Home Assistant" in reply
    assert applied[-1] == "sid-pin-1"
    assert vault.logged_out == ["sid-pin-1"]
    assert PIN not in out and PIN not in caplog.text
    assert api._ha_pin_slot_ids("t-conv") == []


def test_wrong_pin_never_applies_and_the_slot_dies_after_three(api, monkeypatch):
    vault = _Vault()
    monkeypatch.setattr(api, "_get_vault", lambda: vault)
    monkeypatch.setattr(api, "_vault_display_rl_check", lambda: (True, 0))
    monkeypatch.setattr(api, "_vault_display_rl_note", lambda: None)
    monkeypatch.setattr(hp, "apply_approved_change",
                        lambda **kw: pytest.fail("must not apply"))
    api._ha_pin_slot_add("automation.puffpai_x", "t-conv")
    assert "Try once more" in _instant(api, "000000")
    assert "Try once more" in _instant(api, "111111")
    assert "haven't made the change" in _instant(api, "222222")
    assert api._ha_pin_slot_ids("t-conv") == []


def test_a_bare_number_with_no_slot_is_not_a_pin(api, monkeypatch):
    monkeypatch.setattr(api, "_get_vault",
                        lambda: pytest.fail("no slot, no vault access"))
    assert api._try_ha_change_pin_followup("482913") is None


# ── see-lights, pairing filler, reopen ──────────────────────────────

def test_do_you_see_the_bulbs_is_answered_from_live_state(api, monkeypatch):
    from puffpai import zigbee_pairing as zp
    monkeypatch.setattr(zp, "read_report", lambda: {"new_entities": [
        {"entity_id": "light.living_room_sofa_1", "name": "Sofa 1"},
        {"entity_id": "light.living_room_sofa_2", "name": "Sofa 2"},
        {"entity_id": "button.living_room_sofa_1_identify", "name": "x"}]})
    reply = api._try_see_lights("do you see the smart bulbs")
    assert reply.startswith("Yes. From the last pairing window I can see "
                            "Sofa 1 (on) and Sofa 2 (on).")
    assert "LED" not in reply
    assert api._try_see_lights("do you see my two zig bee bulbs now")
    assert api._try_see_lights("did you find any new bulbs") is None
    assert api._try_see_lights("do you see the weather") is None


def test_pair_both_zigbee_bulbs_is_a_pairing_request():
    from puffpai.agents import concierge
    for text in ("pair both zigbee bulbs", "pair all of the bulbs",
                 "pair two new bulbs"):
        assert concierge.resolve_intent(text).intent == "zigbee_pair", text
    assert concierge.resolve_intent(
        "add both items to the shopping list").intent != "zigbee_pair"


def test_yes_open_it_again_files_a_new_pairing_request(api, monkeypatch):
    from puffpai import zigbee_pairing as zp
    filed = []
    monkeypatch.setattr(zp, "request_pairing",
                        lambda reason="", **kw: filed.append(reason) or
                        {"message": "I can open the Zigbee pairing window..."})
    zp.clear_reopen_offer()            # another test's empty window may linger
    assert api._try_pairing_reopen("yes, open it again") is None   # no offer
    zp._set_reopen_offer()
    assert api._try_pairing_reopen("turn on the lights") is None
    assert api._try_pairing_reopen("yes, open it again").startswith(
        "I can open the Zigbee pairing window")
    assert filed == ["asked to open it again"]
    assert not zp.reopen_offer_active()
