"""Saying "approve pairing" out loud finishes the job the dashboard tap does.

When the pairing assistant asks "say approve pairing
here, or tap Approve on the dashboard", Whisper can transcribe the owner's answer
as "Approved pairing.". If nothing deterministic catches that answer to
PuffPAI's own question, the turn routes to the agent instead, which can spend
a long time thinking and call ``ha_discover_new_devices`` without ever
opening the window.

What is pinned here:

* the phrasings match (including the past tense + full stop Whisper produced),
  and the neighbouring sentences do NOT;
* an approval with nothing open claims nothing — the turn falls through
  untouched, so an unrelated "yes" is never hijacked;
* a bare "yes" counts only while the question is fresh;
* approving runs the SAME completion hook the dashboard route runs, once per
  open request, oldest first;
* denying resolves the request negative and completes nothing.

Nothing here may reach a real Home Assistant: both completion hooks are
replaced with recorders.

Run: py -3.14 -m pytest tests/test_consent_voice_approval.py -q
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import consent_voice                    # noqa: E402


# ══════════════════════════════════════════════════════════════════════
#  The phrase rules (pure — no store, no I/O)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", [
    "approve pairing",
    "Approved pairing.",                 # what Whisper actually produced
    "approve the pairing",
    "approve it",
    "approve that",
    "Approve",
    "yes, approve",
    "go ahead and approve",
    "approve the request",
])
def test_explicit_approvals_match(text):
    assert consent_voice.classify(text) == ("approve", True)


@pytest.mark.parametrize("text", [
    "deny the pairing",
    "reject it",
    "don't approve that",
    "cancel the pairing",
    "Denied.",
])
def test_explicit_denials_match(text):
    assert consent_voice.classify(text) == ("deny", True)


@pytest.mark.parametrize("text,verdict", [
    ("yes", "approve"),
    ("go ahead", "approve"),
    ("do it", "approve"),
    ("no", "deny"),
    ("never mind", "deny"),
])
def test_bare_answers_match_but_are_not_explicit(text, verdict):
    assert consent_voice.classify(text) == (verdict, False)


@pytest.mark.parametrize("text", [
    "pair my new light bulbs",
    "did you find any new devices",
    "what did the board approve last year",
    "turn on the porch light",
    "approve the budget for the new dishwasher",
    "",
])
def test_neighbouring_phrasings_are_not_verdicts(text):
    assert consent_voice.classify(text)[0] is None


# ══════════════════════════════════════════════════════════════════════
#  State isolation — the queue lives under a tmp $PUFFPAI_DATA_DIR
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def queue(tmp_path, monkeypatch):
    """Reloaded consent queue rooted at a tmp state dir (see test_zigbee_pairing)."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

    import puffpai.agent_activity as aa
    import puffpai.security.consent_gate as cg
    import puffpai.security.consent_requests as cr

    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()

    yield cr

    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    importlib.reload(cg)
    importlib.reload(cr)
    aa.reset_cache()


def _age(cr, entity_id: str, seconds: float) -> None:
    """Backdate an open request's ``created_at`` by *seconds*."""
    items = cr._load()
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    for req in items:
        if req.get("entity_id") == entity_id:
            req["created_at"] = when.isoformat()
    cr._save(items)


class Recorder:
    """Stands in for a completion hook. Records, actuates nothing."""

    def __init__(self, message="the pairing window is open for 2 minutes"):
        self.calls: list[dict] = []
        self.message = message

    def zigbee(self, actuator=None, *, request=None):
        self.calls.append(dict(request or {}))
        return {"ok": True, "opened": True, "message": self.message}

    def provision(self, request=None, *, bridge=None, human_confirmed=False):
        self.calls.append(dict(request or {}))
        entity = (request or {}).get("entity_id")
        return {"ok": True, "created": True, "message": f"created {entity}"}


@pytest.fixture
def api(monkeypatch):
    """``api_server`` with the HA bridge accessor neutered."""
    from puffpai import api_server

    class _Tools:
        _ha_bridge = object()

    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _Tools())
    return api_server


# ══════════════════════════════════════════════════════════════════════
#  Nothing open → nothing claimed
# ══════════════════════════════════════════════════════════════════════

def test_an_approval_with_nothing_open_claims_nothing(queue, api):
    """Resolves nothing. This path answers with a SENTENCE — never a side effect."""
    assert consent_voice.decide("approve pairing") == (None, [])
    assert api._try_consent_approval("approve pairing") == \
        consent_voice.NOTHING_OPEN_APPROVE
    assert queue.list_open() == []


def test_a_bare_yes_with_nothing_open_claims_nothing(queue, api):
    assert api._try_consent_approval("yes") is None


def test_a_camera_request_is_not_approvable_by_voice(queue, api):
    """Only the two windowed actions. A device consent is answered in the UI."""
    queue.create_request("camera.shed", "Shed Camera", "control_device",
                         "Sentinel blocked an actuation")
    assert api._try_consent_approval("approve it") is None
    assert queue.list_open()[0]["status"] == "open"


# ══════════════════════════════════════════════════════════════════════
#  Approving runs the dashboard's own completion hook
# ══════════════════════════════════════════════════════════════════════

def test_approved_pairing_opens_the_window_once(queue, api, monkeypatch):
    from puffpai import zigbee_pairing as zp

    recorder = Recorder()
    monkeypatch.setattr(zp, "complete_approved_pairing", recorder.zigbee)
    queue.create_request(zp.PAIRING_ENTITY_ID, "Zigbee pairing",
                         zp.PAIRING_ACTION, "owner asked to pair bulbs")

    reply = api._try_consent_approval("Approved pairing.")

    assert len(recorder.calls) == 1
    assert reply and recorder.message in reply
    assert reply.startswith("Approved.")
    resolved = queue._load()[0]
    assert resolved["status"] == "approved"
    assert resolved["resolved_by"] == "user_via_voice"


def test_a_fresh_bare_yes_approves_and_a_stale_one_does_not(queue, api,
                                                            monkeypatch):
    from puffpai import zigbee_pairing as zp

    recorder = Recorder()
    monkeypatch.setattr(zp, "complete_approved_pairing", recorder.zigbee)
    queue.create_request(zp.PAIRING_ENTITY_ID, "Zigbee pairing",
                         zp.PAIRING_ACTION, "owner asked to pair bulbs")
    _age(queue, zp.PAIRING_ENTITY_ID, 120)          # two minutes ago

    assert api._try_consent_approval("yes") is not None
    assert len(recorder.calls) == 1

    # ...and the same question, gone cold, is not answered by a passing "yes".
    queue.create_request("zigbee.pair2", "Zigbee pairing", zp.PAIRING_ACTION,
                         "again")
    _age(queue, "zigbee.pair2", 600)                # ten minutes ago
    assert api._try_consent_approval("yes") is None
    assert len(recorder.calls) == 1
    assert queue.list_open()[0]["status"] == "open"


def test_two_provisioning_requests_are_applied_oldest_first(queue, api,
                                                            monkeypatch):
    from puffpai import ha_provisioning as prov

    recorder = Recorder()
    monkeypatch.setattr(prov, "apply_approved_change", recorder.provision)
    # "approve the changes" takes several only
    # when Puff offered them together (one batch, as the permit scripts are).
    queue.create_request("script.permit_join", "Permit join script",
                         prov.PROVISION_ACTION, "so pairing can open",
                         context={"batch": "permit-scripts"})
    queue.create_request("script.permit_stop", "Permit stop script",
                         prov.PROVISION_ACTION, "so pairing can close",
                         context={"batch": "permit-scripts"})
    _age(queue, "script.permit_join", 300)
    _age(queue, "script.permit_stop", 60)

    reply = api._try_consent_approval("approve the changes")

    assert [c["entity_id"] for c in recorder.calls] == ["script.permit_join",
                                                        "script.permit_stop"]
    assert "created script.permit_join" in reply
    assert "created script.permit_stop" in reply
    assert all(r["status"] == "approved" for r in queue._load())
    assert all(r["resolved_by"] == "user_via_voice" for r in queue._load())


def test_the_provisioning_writer_gets_the_in_process_token(queue, api,
                                                           monkeypatch):
    """The token is the dashboard route's second key — voice presents it too."""
    from puffpai import ha_provisioning as prov
    from puffpai.ha_bridge import HUMAN_CONFIRMED

    seen = {}

    def _apply(request=None, *, bridge=None, human_confirmed=False):
        seen["token"] = human_confirmed
        return {"ok": True, "message": "done"}

    monkeypatch.setattr(prov, "apply_approved_change", _apply)
    queue.create_request("script.permit_join", "Permit join script",
                         prov.PROVISION_ACTION, "so pairing can open")

    api._try_consent_approval("approve it")

    assert seen["token"] is HUMAN_CONFIRMED


# ══════════════════════════════════════════════════════════════════════
#  Denying
# ══════════════════════════════════════════════════════════════════════

def test_denying_records_the_no_and_completes_nothing(queue, api, monkeypatch):
    from puffpai import zigbee_pairing as zp

    recorder = Recorder()
    monkeypatch.setattr(zp, "complete_approved_pairing", recorder.zigbee)
    queue.create_request(zp.PAIRING_ENTITY_ID, "Zigbee pairing",
                         zp.PAIRING_ACTION, "owner asked to pair bulbs")

    reply = api._try_consent_approval("deny the pairing")

    assert recorder.calls == []
    assert reply and reply.lower().startswith("denied")
    resolved = queue._load()[0]
    assert resolved["status"] == "denied"
    assert resolved["resolved_by"] == "user_via_voice"


# ══════════════════════════════════════════════════════════════════════
#  The surface stays the two files it was
# ══════════════════════════════════════════════════════════════════════

def test_voice_is_a_recognised_human_resolver():
    """``_recent_approval`` refuses a ``by`` it does not know as a human."""
    from puffpai import ha_provisioning, zigbee_pairing

    assert "user_via_voice" in zigbee_pairing.HUMAN_RESOLVERS
    assert "user_via_voice" in ha_provisioning.HUMAN_RESOLVERS


def test_the_matcher_never_names_a_completion_hook():
    """consent_voice decides; only the approval surfaces complete."""
    source = Path(consent_voice.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]        # past the module docstring
    assert "complete_approved_pairing" not in body
    assert "apply_approved_change" not in body


# ══════════════════════════════════════════════════════════════════════
# Nothing open → an EXPLICIT verdict is DECLINED instantly
# ══════════════════════════════════════════════════════════════════════
#
# Without this, "approve pairing" with an empty queue can send the model
# through sentinel_approve_device and then ha_approve_pending_device before it
# settles on the right decline. The answer
# needs one list_open() call, not a model with device-approval tools in reach.

def test_an_explicit_approval_with_nothing_open_declines_instantly(queue, api):
    assert queue.list_open() == []

    reply = api._try_consent_approval("approve pairing")

    assert reply == consent_voice.NOTHING_OPEN_APPROVE
    assert reply == "Nothing is waiting for your approval right now."
    assert queue.list_open() == []           # no side effects, no store write


def test_an_explicit_denial_with_nothing_open_gets_its_own_sentence(queue, api):
    reply = api._try_consent_approval("deny the pairing")

    assert reply == consent_voice.NOTHING_OPEN_DENY
    assert reply == "There's nothing open to decline."
    assert queue.list_open() == []


@pytest.mark.parametrize("said", ["yes", "go ahead", "sure", "do it",
                                  "no", "not now", "never mind"])
def test_a_bare_affirmation_with_nothing_open_still_falls_through(queue, api,
                                                                  said):
    """A stray "yes" is an answer to something else entirely — leave the turn."""
    assert api._try_consent_approval(said) is None
    assert consent_voice.nothing_open_reply(said) is None


@pytest.mark.parametrize("said", ["what's the weather", "approve the budget",
                                  "pair the bulbs", ""])
def test_unrelated_sentences_are_untouched(queue, api, said):
    assert consent_voice.nothing_open_reply(said) is None


def test_something_open_is_never_called_nothing(queue, api, monkeypatch):
    """An OLD open request is past the spoken window but still waiting."""
    from puffpai import zigbee_pairing as zp

    queue.create_request(zp.PAIRING_ENTITY_ID, "Zigbee pairing",
                         zp.PAIRING_ACTION, "owner asked to pair bulbs")
    _age(queue, zp.PAIRING_ENTITY_ID,
         consent_voice.EXPLICIT_WINDOW_SECONDS + 600)

    # Too old to resolve out loud, so the fastpath claims nothing at all —
    # rather than telling the owner nothing is waiting while it is.
    assert consent_voice.decide("approve pairing") == (None, [])
    assert consent_voice.nothing_open_reply("approve pairing") is None
    assert api._try_consent_approval("approve pairing") is None


def test_an_unreadable_queue_never_claims_nothing_is_waiting(monkeypatch):
    from puffpai.security import consent_requests as cr

    def _boom():
        raise OSError("queue unreadable")

    monkeypatch.setattr(cr, "list_open", _boom)
    assert consent_voice.nothing_open_reply("approve pairing") is None


def test_the_decline_reaches_no_completion_hook(queue, api, monkeypatch):
    from puffpai import ha_provisioning as prov
    from puffpai import zigbee_pairing as zp

    recorder = Recorder()
    monkeypatch.setattr(zp, "complete_approved_pairing", recorder.zigbee)
    monkeypatch.setattr(prov, "apply_approved_change", recorder.provision)

    assert api._try_consent_approval("approve pairing") == \
        consent_voice.NOTHING_OPEN_APPROVE
    assert recorder.calls == []
