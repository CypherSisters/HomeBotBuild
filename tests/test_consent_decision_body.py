"""A consent decision is never inferred from a key that is not there.

``POST /sentinel/consent/requests/<entity_id>`` read ``approve`` and nothing else,
Reading ``approve`` and nothing else, through ``bool(body.get('approve'))``,
would turn ``{"approved": true}`` (the spelling most callers guess) into a
recorded DENIAL with a 200 saying the decision had been recorded: the owner said
yes, the appliance wrote no, and the queue entry closed so a second attempt
404s.

Two things hold together: both spellings are accepted, and a body that carries
neither is refused instead of treated as a denial. "I cannot tell what you
decided" has exactly one safe reading on a consent route, and it is not "no".

Run:
    py -3 -m pytest tests/test_consent_decision_body.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                              # noqa: E402
from puffpai.security import consent_requests as creq       # noqa: E402

AUTH = {"X-API-Key": api_server._API_KEY}
ENTITY = "camera.front_door"


@pytest.fixture
def queued(tmp_path, monkeypatch):
    """One open consent request, in a disposable queue file."""
    monkeypatch.setitem(vars(creq), "REQUESTS_PATH",
                        str(tmp_path / "consent_requests.json"))
    monkeypatch.setitem(vars(creq), "ACTIVITY_LOG",
                        str(tmp_path / "agent_activity.jsonl"))
    creq.create_request(ENTITY, "Front door camera", "dashboard_add_camera",
                        "the owner asked for it on the dashboard")
    api_server._rate_limit_store.clear()
    api_server.app.config["TESTING"] = True
    return api_server.app.test_client()


def status_of(entity_id: str = ENTITY) -> str:
    for item in creq._load():
        if item.get("entity_id") == entity_id:
            return str(item.get("status"))
    return "missing"


# ---------------------------------------------------------------------------
# 1. both spellings mean what they say
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["approve", "approved"])
def test_true_in_either_spelling_records_an_approval(queued, key):
    resp = queued.post(f'/sentinel/consent/requests/{ENTITY}',
                       headers=AUTH, json={key: True})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert status_of() == "approved"


@pytest.mark.parametrize("key", ["approve", "approved"])
def test_false_in_either_spelling_records_a_denial(queued, key):
    """A denial still has to be expressible — that is the whole point of a
    consent queue, and the fix must not quietly make 'no' unsayable."""
    resp = queued.post(f'/sentinel/consent/requests/{ENTITY}',
                       headers=AUTH, json={key: False})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert status_of() == "denied"


# ---------------------------------------------------------------------------
# 2. no decision is a refusal, not a denial
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {},                                  # nothing at all
    {"note": "looks fine to me"},        # a note but no verdict
    {"approve": None},                   # JSON's "no value" is not "no"
    {"aprove": True},                    # a typo must not read as a denial
])
def test_a_body_with_no_decision_is_400_and_changes_nothing(queued, body):
    resp = queued.post(f'/sentinel/consent/requests/{ENTITY}',
                       headers=AUTH, json=body)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert status_of() == "open", "a request with no decision was resolved anyway"
    error = resp.get_json()["error"]
    assert "approve" in error and "Nothing was recorded" in error


def test_the_refusal_names_both_spellings(queued):
    """The message has to be actionable: the caller guessed wrong once already."""
    error = queued.post(f'/sentinel/consent/requests/{ENTITY}',
                        headers=AUTH, json={}).get_json()["error"]
    assert '"approve": true' in error
    assert 'approved' in error


def test_a_non_object_body_is_still_400(queued):
    """The pre-existing envelope check must survive the new one."""
    resp = queued.post(f'/sentinel/consent/requests/{ENTITY}', headers=AUTH,
                       data=json.dumps([1, 2, 3]),
                       content_type='application/json')
    assert resp.status_code == 400
    assert status_of() == "open"
